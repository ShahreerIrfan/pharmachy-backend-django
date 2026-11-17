"""
backend/apps/core/pos_rx_safety_service.py
Architecture Decision: ADR-0227 (P227).
Phase 12: POS Prescription Safety Gate, Pharmacist Approval, Antibiotic Register & Offline Limitations.

Integrates directly with:
- MedicineProduct & MedicineClassificationSnapshot (ADR-0130)
- PrescriptionRecord & PrescriptionItem (ADR-0010 / ADR-0033)
- PosSaleOrder, PosSaleOrderLine, PosDomainService, PosSearchCartService, PosHeldCartService & PosDeliveryService
"""

from __future__ import annotations

import datetime
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    AntibioticDispenseRegisterEntry,
    AntibioticRegisterStatus,
    ClassificationType,
    MedicineClassificationSnapshot,
    MedicineCurationStatus,
    MedicineProduct,
    PosRegulatoryClass,
    PosRxGateStatus,
    PosRxSafetyAudit,
    PosSaleLifecycleState,
    PosSaleOrder,
    PrescriptionItem,
    PrescriptionRecord,
    PrescriptionStatus,
    SaleInvoice,
    StaffLifecycleStatus,
    StaffMember,
    Tenant,
)
from apps.core.pos_domain_service import PosDomainService
from apps.identity.models import UserRole


def q2(value: Any) -> Decimal:
    return Decimal(str(value or "0.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class _RxRejectionAbort(Exception):
    """Rolls back atomic checkout mutation while preserving immutable Rx rejection audit."""

    def __init__(self, *, sale: PosSaleOrder, audit_kwargs: Dict[str, Any], exc: Exception, mark_offline_blocked: bool = False):
        super().__init__(str(exc))
        self.sale_id = sale.sale_id
        self.audit_kwargs = audit_kwargs
        self.exc = exc
        self.mark_offline_blocked = mark_offline_blocked


class PosRxSafetyService:
    """
    Canonical server authority for POS prescription classification, registered pharmacist
    clinical sign-off, cart-edit invalidation, offline Rx blocking, and antibiotic register posting.
    """

    PHARMACIST_PRIMARY_ROLES = {
        UserRole.STAFF_PHARMACIST,
        UserRole.SUPERADMIN,
        "STAFF_PHARMACIST",
        "SUPERADMIN",
    }
    OWNER_CONDITIONAL_ROLES = {
        UserRole.PHARMACY_OWNER,
        "PHARMACY_OWNER",
    }
    PHARMACIST_ROLES = {
        UserRole.STAFF_PHARMACIST,
        UserRole.PHARMACY_OWNER,
        UserRole.BRANCH_MANAGER,
        UserRole.SUPERADMIN,
        "STAFF_PHARMACIST",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "SUPERADMIN",
    }

    ANTIMICROBIAL_TIERS = {
        PosRegulatoryClass.ANTIMICROBIAL_ACCESS,
        PosRegulatoryClass.ANTIMICROBIAL_WATCH,
        PosRegulatoryClass.ANTIMICROBIAL_RESERVE,
    }

    CLASS_SEVERITY = {
        PosRegulatoryClass.OTC: 0,
        PosRegulatoryClass.PRESCRIPTION_ONLY: 1,
        PosRegulatoryClass.ANTIMICROBIAL_ACCESS: 2,
        PosRegulatoryClass.ANTIMICROBIAL_WATCH: 3,
        PosRegulatoryClass.ANTIMICROBIAL_RESERVE: 4,
        PosRegulatoryClass.NARCOTIC_SCHEDULED: 5,
    }

    @classmethod
    def classify_product(cls, product: MedicineProduct) -> Dict[str, Any]:
        """
        Determines live regulatory classification from current MedicineClassificationSnapshot
        combined with MedicineProduct master flags and curation status.
        """
        snap = (
            MedicineClassificationSnapshot.objects.filter(product=product, is_current=True)
            .order_by("-version_number")
            .first()
        )
        curation = str(getattr(product, "curation_status", MedicineCurationStatus.APPROVED) or "").upper()
        snap_type = str(snap.classification_type if snap else "").upper()

        is_recalled_or_banned = (
            curation == MedicineCurationStatus.RECALLED
            or snap_type in (ClassificationType.RECALLED, ClassificationType.BANNED)
        )

        # Resolve classification conservatively (strictest active requirement wins)
        candidates: List[str] = [PosRegulatoryClass.OTC]
        if bool(getattr(product, "is_prescription_only", False)) or snap_type == ClassificationType.PRESCRIPTION_ONLY:
            candidates.append(PosRegulatoryClass.PRESCRIPTION_ONLY)
        if bool(getattr(product, "is_antibiotic", False)):
            candidates.append(PosRegulatoryClass.ANTIMICROBIAL_ACCESS)
        if snap_type in (
            ClassificationType.ANTIMICROBIAL_ACCESS,
            ClassificationType.ANTIMICROBIAL_WATCH,
            ClassificationType.ANTIMICROBIAL_RESERVE,
        ):
            candidates.append(snap_type)
        if bool(getattr(product, "is_narcotic", False)) or snap_type == ClassificationType.NARCOTIC_SCHEDULED:
            candidates.append(PosRegulatoryClass.NARCOTIC_SCHEDULED)

        tier = max(candidates, key=lambda c: cls.CLASS_SEVERITY.get(c, 0))
        requires_rx = tier != PosRegulatoryClass.OTC or is_recalled_or_banned
        is_antibiotic = tier in cls.ANTIMICROBIAL_TIERS or bool(getattr(product, "is_antibiotic", False))
        is_narcotic = tier == PosRegulatoryClass.NARCOTIC_SCHEDULED or bool(getattr(product, "is_narcotic", False))
        snap_version = int(snap.version_number) if snap else int(getattr(product, "version_number", 1) or 1)

        return {
            "regulatory_classification": tier,
            "snapshot_version": snap_version,
            "snapshot_id": snap.snapshot_id if snap else None,
            "regulatory_source": snap.regulatory_source if snap else getattr(product, "regulatory_source", "DGDA_BANGLADESH"),
            "requires_rx_verification": bool(requires_rx),
            "is_antibiotic": bool(is_antibiotic),
            "is_narcotic": bool(is_narcotic),
            "is_blocked_recall_or_ban": bool(is_recalled_or_banned),
        }

    @classmethod
    def _resolve_pharmacist_actor(
        cls,
        user: Any,
        tenant: Tenant,
        branch_id: str,
        *,
        explicit_pcb: str = "",
    ) -> str:
        """
        Enforces strict registered-pharmacist role boundary and resolves PCB registration no.
        Non-pharmacist CASHIER / BRANCH_MANAGER / FINANCE_OFFICER / INVENTORY_CLERK / AUDITOR
        are strictly blocked with HTTP 403 ERR_PHARMACIST_APPROVAL_REQUIRED.
        PHARMACY_OWNER may approve ONLY when holding a verified PCB registration number.
        """
        role = str(getattr(user, "role", "") or "").strip().upper()
        user_pcb = str(getattr(user, "pcb_registration_no", "") or "").strip()

        staff = (
            StaffMember.objects.filter(
                tenant=tenant,
                is_active=True,
                lifecycle_status=StaffLifecycleStatus.ACTIVE,
            )
            .filter(mobile_number=str(getattr(user, "phone_number", "") or ""))
            .first()
        )
        staff_pcb = str(staff.pcb_registration_no or "").strip() if staff else ""
        cleaned_explicit = str(explicit_pcb or "").strip()[:64]
        resolved_pcb = cleaned_explicit or user_pcb or staff_pcb

        if role in cls.PHARMACIST_PRIMARY_ROLES:
            if not resolved_pcb:
                uid_part = str(getattr(user, "user_id", "") or getattr(user, "pk", "") or "PHARM")[:8].upper()
                resolved_pcb = f"PCB-{uid_part}"
            return resolved_pcb

        if role in cls.OWNER_CONDITIONAL_ROLES:
            if resolved_pcb:
                return resolved_pcb
            raise PermissionDenied(
                {
                    "error_code": "ERR_PHARMACIST_APPROVAL_REQUIRED",
                    "detail": (
                        "Pharmacy Owner without registered PCB credential cannot sign off Rx medicines. "
                        "A registered STAFF_PHARMACIST approval or verified PCB registration number is required."
                    ),
                    "message_en": "Registered Pharmacist approval (with PCB registration) is required for Rx/antibiotic sales.",
                    "message_bn": "প্রেসক্রিপশন/অ্যান্টিবায়োটিক অনুমোদনের জন্য নিবন্ধিত ফার্মাসিস্ট ও PCB নম্বর প্রয়োজন।",
                }
            )

        raise PermissionDenied(
            {
                "error_code": "ERR_PHARMACIST_APPROVAL_REQUIRED",
                "detail": (
                    f"Role '{role}' is not authorized to perform clinical Rx verification/rejection. "
                    f"Only a registered STAFF_PHARMACIST in branch '{branch_id}' may approve Rx sales."
                ),
                "message_en": f"Role '{role}' cannot approve prescriptions; registered Pharmacist required.",
                "message_bn": f"'{role}' রোল প্রেসক্রিপশন অনুমোদন করতে পারবে না; নিবন্ধিত ফার্মাসিস্ট প্রয়োজন।",
            }
        )

    @classmethod
    def _record_rx_audit(
        cls,
        *,
        sale: PosSaleOrder,
        action: str,
        from_gate_status: str,
        to_gate_status: str,
        user: Any = None,
        prescription: Optional[PrescriptionRecord] = None,
        decision: str = "",
        reason: str = "",
        rx_reference_code: str = "",
        pharmacist_pcb_no: str = "",
        is_offline_attempt: bool = False,
        was_rejected: bool = False,
        rejection_code: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PosRxSafetyAudit:
        role = str(getattr(user, "role", "") or "").upper()
        # Filter out any sensitive PHI keys defensively before storing metadata
        safe_meta: Dict[str, Any] = {}
        for k, v in (metadata or {}).items():
            low = str(k).lower()
            if any(bad in low for bad in ("nid", "national_id", "address", "phone", "password", "token_raw")):
                continue
            safe_meta[k] = v

        return PosRxSafetyAudit.objects.create(
            rx_audit_id=f"rxaud-{uuid.uuid4().hex[:12]}",
            tenant=sale.tenant,
            branch=sale.branch,
            sale=sale,
            prescription=prescription or sale.prescription,
            action=str(action)[:48],
            from_gate_status=str(from_gate_status or "")[:32],
            to_gate_status=str(to_gate_status or "")[:32],
            decision=str(decision or "")[:32],
            reason=str(reason or "")[:255],
            rx_reference_code=str(rx_reference_code or sale.rx_reference_code or "")[:64],
            pharmacist_pcb_no=str(pharmacist_pcb_no or sale.pharmacist_pcb_no_snapshot or "")[:64],
            actor_user=user if getattr(user, "pk", None) else None,
            actor_role=role[:48],
            is_offline_attempt=bool(is_offline_attempt),
            was_rejected=bool(was_rejected),
            rejection_code=str(rejection_code or "")[:64],
            metadata_json=safe_meta,
        )

    @classmethod
    def evaluate_and_sync_sale_rx_gate(
        cls,
        sale: PosSaleOrder,
        actor_user: Any = None,
        *,
        mutate: bool = True,
        reason: str = "",
    ) -> Dict[str, Any]:
        """
        Inspects all lines on a PosSaleOrder against live MedicineClassificationSnapshot rules.
        Invalidates prior cart clearance immediately if an unverified Rx item was added,
        quantity scaled past verified base units, or product classification escalated.
        """
        lines = list(sale.lines.select_related("product").order_by("line_number"))
        any_rx = False
        has_antibiotic = False
        has_narcotic = False
        has_blocked = False
        unverified_line_ids: List[str] = []
        line_summaries: List[Dict[str, Any]] = []
        invalidated_reason = ""

        is_terminal = sale.lifecycle_state in (
            PosSaleLifecycleState.FINALIZED,
            PosSaleLifecycleState.VOIDED,
            PosSaleLifecycleState.REVERSED,
            PosSaleLifecycleState.FULLY_RETURNED,
        )

        for line in lines:
            cinfo = cls.classify_product(line.product)
            tier = cinfo["regulatory_classification"]
            snap_ver = cinfo["snapshot_version"]
            line_req_rx = bool(cinfo["requires_rx_verification"])
            is_abx = bool(cinfo["is_antibiotic"])
            is_narc = bool(cinfo["is_narcotic"])
            line_blocked = bool(cinfo["is_blocked_recall_or_ban"])

            if line_req_rx:
                any_rx = True
            if is_abx:
                has_antibiotic = True
            if is_narc:
                has_narcotic = True
            if line_blocked:
                has_blocked = True

            # Check if line clearance is still valid against live classification & quantity
            line_escalated = (
                not is_terminal
                and line.rx_verified
                and (
                    cls.CLASS_SEVERITY.get(tier, 0) > cls.CLASS_SEVERITY.get(line.regulatory_classification, 0)
                    or (
                        line.classification_version_snapshot is not None
                        and snap_ver > int(line.classification_version_snapshot)
                        and line_req_rx
                    )
                    or (int(line.rx_verified_base_units or 0) > 0 and int(line.base_units_total) > int(line.rx_verified_base_units))
                )
            )
            if line_escalated and mutate:
                line.rx_verified = False
                line.rx_verified_base_units = 0
                invalidated_reason = (
                    invalidated_reason
                    or reason
                    or f"Cart line '{line.product.brand_name}' classification/quantity changed after Rx verification."
                )

            if mutate and not is_terminal:
                changed_fields: List[str] = []
                if line.requires_rx_verification != line_req_rx:
                    line.requires_rx_verification = line_req_rx
                    changed_fields.append("requires_rx_verification")
                if line.regulatory_classification != tier:
                    line.regulatory_classification = tier
                    changed_fields.append("regulatory_classification")
                if line.classification_version_snapshot != snap_ver:
                    line.classification_version_snapshot = snap_ver
                    changed_fields.append("classification_version_snapshot")
                if line_escalated:
                    changed_fields.extend(["rx_verified", "rx_verified_base_units"])
                if not line_req_rx and (line.rx_verified or line.rx_verified_base_units != 0):
                    line.rx_verified = False
                    line.rx_verified_base_units = 0
                    changed_fields.extend(["rx_verified", "rx_verified_base_units"])
                if changed_fields:
                    line.save(update_fields=list(dict.fromkeys(changed_fields + ["updated_at"])))

            is_line_cleared = (not line_req_rx) or (bool(line.rx_verified) and not line_escalated)
            if line_req_rx and not is_line_cleared:
                unverified_line_ids.append(line.line_id)

            line_summaries.append(
                {
                    "line_id": line.line_id,
                    "product_id": line.product_id,
                    "brand_name": line.product.brand_name,
                    "generic_name": line.product.generic_name,
                    "packaging_unit": line.packaging_unit,
                    "quantity_units": int(line.quantity_units),
                    "base_units_total": int(line.base_units_total),
                    "regulatory_classification": tier,
                    "classification_version": snap_ver,
                    "requires_rx_verification": line_req_rx,
                    "is_antibiotic": is_abx,
                    "is_narcotic": is_narc,
                    "is_blocked_recall_or_ban": line_blocked,
                    "rx_verified": bool(is_line_cleared and line_req_rx),
                    "rx_verified_base_units": int(line.rx_verified_base_units or 0),
                }
            )

        prev_gate = str(sale.rx_gate_status or PosRxGateStatus.NOT_REQUIRED)
        if mutate and not is_terminal:
            update_fields: List[str] = []
            if not any_rx:
                if sale.requires_rx_verification:
                    sale.requires_rx_verification = False
                    update_fields.append("requires_rx_verification")
                if sale.rx_gate_status != PosRxGateStatus.NOT_REQUIRED:
                    sale.rx_gate_status = PosRxGateStatus.NOT_REQUIRED
                    update_fields.append("rx_gate_status")
                if sale.rx_verified_at is not None:
                    sale.rx_verified_at = None
                    sale.rx_verified_by = None
                    sale.pharmacist_pcb_no_snapshot = ""
                    update_fields.extend(["rx_verified_at", "rx_verified_by", "pharmacist_pcb_no_snapshot"])
                if sale.lifecycle_state == PosSaleLifecycleState.PENDING_RX_REVIEW:
                    sale.lifecycle_state = PosSaleLifecycleState.DRAFT
                    update_fields.append("lifecycle_state")
                if update_fields:
                    sale.save(update_fields=list(dict.fromkeys(update_fields + ["updated_at"])))
            else:
                if not sale.requires_rx_verification:
                    sale.requires_rx_verification = True
                    update_fields.append("requires_rx_verification")

                if unverified_line_ids:
                    if sale.rx_verified_at is not None:
                        sale.rx_verified_at = None
                        sale.rx_verified_by = None
                        sale.rx_gate_status = PosRxGateStatus.PENDING_PHARMACIST
                        sale.rx_decision_reason = invalidated_reason or (
                            reason or "Unverified Rx medicine added or modified after prior verification."
                        )
                        update_fields.extend(
                            ["rx_verified_at", "rx_verified_by", "rx_gate_status", "rx_decision_reason"]
                        )
                        if sale.lifecycle_state == PosSaleLifecycleState.DRAFT:
                            sale.lifecycle_state = PosSaleLifecycleState.PENDING_RX_REVIEW
                            update_fields.append("lifecycle_state")
                        sale.save(update_fields=list(dict.fromkeys(update_fields + ["updated_at"])))
                        cls._record_rx_audit(
                            sale=sale,
                            action="RX_CLEARANCE_INVALIDATED",
                            from_gate_status=prev_gate,
                            to_gate_status=PosRxGateStatus.PENDING_PHARMACIST,
                            user=actor_user,
                            reason=sale.rx_decision_reason,
                            metadata={"unverified_line_ids": unverified_line_ids},
                        )
                    else:
                        target_gate = (
                            PosRxGateStatus.REJECTED
                            if sale.rx_gate_status == PosRxGateStatus.REJECTED and not invalidated_reason
                            else PosRxGateStatus.PENDING_PHARMACIST
                        )
                        if sale.rx_gate_status != target_gate:
                            sale.rx_gate_status = target_gate
                            update_fields.append("rx_gate_status")
                        if sale.lifecycle_state == PosSaleLifecycleState.DRAFT:
                            sale.lifecycle_state = PosSaleLifecycleState.PENDING_RX_REVIEW
                            update_fields.append("lifecycle_state")
                        if update_fields:
                            sale.save(update_fields=list(dict.fromkeys(update_fields + ["updated_at"])))
                        if prev_gate == PosRxGateStatus.NOT_REQUIRED and target_gate == PosRxGateStatus.PENDING_PHARMACIST:
                            cls._record_rx_audit(
                                sale=sale,
                                action="RX_GATE_TRIGGERED",
                                from_gate_status=prev_gate,
                                to_gate_status=target_gate,
                                user=actor_user,
                                reason="Controlled/Rx medicine requires Pharmacist verification before checkout",
                                metadata={"unverified_line_ids": unverified_line_ids},
                            )
                else:
                    # Check if external PrescriptionRecord already VERIFIED & attached to sale
                    rx_ok = bool(sale.rx_verified_at) or bool(
                        sale.prescription and sale.prescription.status in (PrescriptionStatus.VERIFIED, PrescriptionStatus.DISPENSED)
                    )
                    if rx_ok and sale.rx_gate_status != PosRxGateStatus.APPROVED:
                        sale.rx_gate_status = PosRxGateStatus.APPROVED
                        update_fields.append("rx_gate_status")
                    if update_fields:
                        sale.save(update_fields=list(dict.fromkeys(update_fields + ["updated_at"])))

        all_rx_approved = (
            (not any_rx)
            or (
                not unverified_line_ids
                and bool(sale.rx_verified_at)
                and sale.rx_gate_status == PosRxGateStatus.APPROVED
            )
        )
        can_finalize_online = (not has_blocked) and all_rx_approved
        can_finalize_offline = (not has_blocked) and (not has_narcotic) and (not any_rx or all_rx_approved)

        offline_block_reason_en = ""
        offline_block_reason_bn = ""
        if has_blocked:
            offline_block_reason_en = "Cart contains a RECALLED or BANNED medicine that cannot be sold online or offline."
            offline_block_reason_bn = "কার্টে প্রত্যাহৃত (RECALLED) বা নিষিদ্ধ (BANNED) ওষুধ রয়েছে যা বিক্রি করা সম্পূর্ণ নিষিদ্ধ।"
        elif has_narcotic:
            offline_block_reason_en = (
                "Offline limitation: Narcotics and Schedule-G controlled substances are strictly prohibited from offline dispensing under DGDA statutory regulations."
            )
            offline_block_reason_bn = (
                "অফলাইন সীমাবদ্ধতা: ডিজিডিএ সংবিধিবদ্ধ নিয়ম অনুযায়ী মাদক ও সিডিউল-জি ওষুধ অফলাইনে বিক্রয় করা সম্পূর্ণ নিষিদ্ধ।"
            )
        elif any_rx and not all_rx_approved:
            offline_block_reason_en = (
                "Offline limitation: unverified Rx/antibiotic/narcotic medicines cannot be finalized offline. "
                "Park cart to Held Queue and reconnect for online Pharmacist approval."
            )
            offline_block_reason_bn = (
                "অফলাইন সীমাবদ্ধতা: ফার্মাসিস্টের অনুমোদন ছাড়া প্রেসক্রিপশন/অ্যান্টিবায়োটিক ওষুধ অফলাইনে বিক্রি সম্পন্ন করা যাবে না। "
                "কার্টটি হোল্ড করে অন-ডিউটি ফার্মাসিস্টের অনুমোদন নিন অথবা অনলাইনে সংযুক্ত হোন।"
            )

        rx = sale.prescription
        return {
            "sale_id": sale.sale_id,
            "tenant_id": sale.tenant_id,
            "branch_id": sale.branch_id,
            "order_number": sale.order_number,
            "lifecycle_state": sale.lifecycle_state,
            "requires_rx_verification": bool(any_rx),
            "rx_gate_status": str(sale.rx_gate_status),
            "prescription_id": sale.prescription_id,
            "doctor_name": rx.doctor_name if rx else "",
            "doctor_bmdc_no": rx.doctor_bmdc_no if rx else "",
            "patient_token": rx.patient_token if rx else "",
            "prescription_status": rx.status if rx else "",
            "rx_reference_code": str(sale.rx_reference_code or ""),
            "pharmacist_pcb_no_snapshot": str(sale.pharmacist_pcb_no_snapshot or ""),
            "rx_verified_by_email": sale.rx_verified_by.email if sale.rx_verified_by else "",
            "rx_verified_at": sale.rx_verified_at.isoformat() if sale.rx_verified_at else None,
            "rx_decision_reason": str(sale.rx_decision_reason or ""),
            "rx_safety_policy_version": str(sale.rx_safety_policy_version or "DGDA-POLICY-v2026.1"),
            "requires_antibiotic_register": bool(has_antibiotic),
            "has_narcotic": bool(has_narcotic),
            "has_blocked_recall_or_ban": bool(has_blocked),
            "unverified_line_ids": unverified_line_ids,
            "line_classifications": line_summaries,
            "can_finalize_online": bool(can_finalize_online),
            "can_finalize_offline": bool(can_finalize_offline),
            "offline_block_reason_en": offline_block_reason_en,
            "offline_block_reason_bn": offline_block_reason_bn,
            "version": int(sale.version),
        }

    @classmethod
    @transaction.atomic
    def attach_prescription_to_sale(cls, user: Any, sale: PosSaleOrder, data: Optional[Dict[str, Any]] = None) -> PosSaleOrder:
        data = data or {}
        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=sale.branch_id, require_write=True, allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES
        )

        if sale.lifecycle_state in (
            PosSaleLifecycleState.FINALIZED,
            PosSaleLifecycleState.VOIDED,
            PosSaleLifecycleState.REVERSED,
            PosSaleLifecycleState.FULLY_RETURNED,
        ):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "detail": f"Cannot attach prescription to {sale.lifecycle_state} sale.",
                    "message_en": f"Cannot attach prescription to a {sale.lifecycle_state} order.",
                    "message_bn": f"{sale.lifecycle_state} অর্ডারে প্রেসক্রিপশন সংযুক্ত করা যাবে না।",
                }
            )

        raw_rx_id = str(data.get("prescription_id") or "").strip()
        rx_ref = str(data.get("rx_reference_code") or "").strip()[:64]
        prev_gate = str(sale.rx_gate_status)

        if raw_rx_id:
            rx = PrescriptionRecord.objects.filter(pk=raw_rx_id).first()
            if not rx or rx.tenant_id != tenant.tenant_id or rx.branch_id != branch.branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_CROSS_TENANT_OR_BRANCH_RX",
                        "detail": "Prescription record not found within current tenant and branch scope.",
                        "message_en": "Prescription does not belong to your authorized branch.",
                        "message_bn": "প্রেসক্রিপশনটি আপনার অনুমোদিত শাখার অন্তর্ভুক্ত নয়।",
                    }
                )
            if rx.status in (PrescriptionStatus.REJECTED, PrescriptionStatus.CANCELLED):
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_PRESCRIPTION_STATUS",
                        "detail": f"Cannot attach prescription in '{rx.status}' status.",
                        "message_en": f"Prescription is {rx.status} and cannot be used for dispensing.",
                        "message_bn": f"প্রেসক্রিপশনটির অবস্থা '{rx.status}' হওয়ায় ব্যবহার করা যাবে না।",
                    }
                )
            sale.prescription = rx
            sale.rx_reference_code = rx_ref or rx.prescription_id[:64]
        else:
            doctor_name = str(data.get("doctor_name") or "").strip()[:255]
            doctor_bmdc_no = str(data.get("doctor_bmdc_no") or "").strip()[:64]
            if not doctor_name or len(doctor_bmdc_no) < 3:
                raise ValidationError(
                    {
                        "error_code": "ERR_RX_DOCUMENTATION_REQUIRED",
                        "doctor_bmdc_no": "Doctor name and valid BMDC registration number (>= 3 chars) are required.",
                        "message_en": "Enter prescriber Doctor Name and BMDC Reg. No. (>= 3 characters).",
                        "message_bn": "চিকিৎসকের নাম এবং সঠিক BMDC রেজিস্ট্রেশন নম্বর (কমপক্ষে ৩ অক্ষর) দিন।",
                    }
                )
            raw_pt = str(data.get("patient_token") or "").strip()[:64]
            # Minimize PII strictly: never allow raw 10/13/17-digit NID or mobile strings directly as patient_token
            if raw_pt and raw_pt.isdigit() and len(raw_pt) >= 10:
                raw_pt = f"PT-HASH-{abs(hash(raw_pt)) % 1000000:06d}"
            patient_token = raw_pt or f"PT-{sale.sale_id[-6:].upper()}"

            raw_date = str(data.get("prescription_date") or "").strip()
            rx_date = timezone.now().date()
            if raw_date:
                try:
                    rx_date = datetime.date.fromisoformat(raw_date)
                except ValueError as exc:
                    raise ValidationError(
                        {
                            "error_code": "ERR_INVALID_PRESCRIPTION_DATE",
                            "prescription_date": "Use YYYY-MM-DD format.",
                            "message_en": "Prescription date must follow YYYY-MM-DD format.",
                            "message_bn": "প্রেসক্রিপশনের তারিখ YYYY-MM-DD ফরম্যাটে দিন।",
                        }
                    ) from exc
            today = timezone.now().date()
            if rx_date > today or (today - rx_date).days > 365:
                raise ValidationError(
                    {
                        "error_code": "ERR_EXPIRED_OR_FUTURE_PRESCRIPTION",
                        "prescription_date": "Prescription date cannot be in the future or older than 365 days.",
                        "message_en": "Prescription date is invalid (future-dated or older than 365 days).",
                        "message_bn": "প্রেসক্রিপশনের তারিখ ভবিষ্যতের বা ৩৬৫ দিনের বেশি পুরোনো হতে পারবে না।",
                    }
                )

            rx = PrescriptionRecord.objects.create(
                prescription_id=f"rx-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                branch=branch,
                doctor_name=doctor_name,
                doctor_bmdc_no=doctor_bmdc_no,
                patient_token=patient_token,
                diagnosis=str(data.get("diagnosis") or "")[:255],
                status=PrescriptionStatus.PENDING,
                notes=str(data.get("notes") or "")[:500],
                prescription_date=rx_date,
            )
            dosage_instr = str(data.get("dosage_instructions") or "As prescribed by physician")[:255]
            dur_days = data.get("duration_days", 7)
            try:
                dur_int = max(1, min(365, int(dur_days)))
            except (TypeError, ValueError):
                dur_int = 7
            for ln in sale.lines.select_related("product").filter(requires_rx_verification=True):
                PrescriptionItem.objects.create(
                    prescription=rx,
                    product=ln.product,
                    dosage_instructions=dosage_instr,
                    duration_days=dur_int,
                    quantity_prescribed=max(1, int(ln.base_units_total)),
                )
            sale.prescription = rx
            sale.rx_reference_code = rx_ref or rx.prescription_id[:64]

        sale.version += 1
        sale.updated_by = user
        sale.save(update_fields=["prescription", "rx_reference_code", "version", "updated_by", "updated_at"])

        cls.evaluate_and_sync_sale_rx_gate(sale, actor_user=user, reason="Prescription metadata attached")
        cls._record_rx_audit(
            sale=sale,
            action="PRESCRIPTION_ATTACHED",
            from_gate_status=prev_gate,
            to_gate_status=str(sale.rx_gate_status),
            user=user,
            prescription=rx,
            reason=f"Linked Rx {rx.prescription_id} (BMDC {rx.doctor_bmdc_no})",
            rx_reference_code=sale.rx_reference_code,
        )
        return sale

    @classmethod
    @transaction.atomic
    def verify_sale_rx(
        cls,
        user: Any,
        sale: PosSaleOrder,
        data: Optional[Dict[str, Any]] = None,
        *,
        notes: str = "",
    ) -> PosSaleOrder:
        data = data or {}
        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=sale.branch_id, require_write=True, allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES
        )

        # Strict Pharmacist / PCB authorization barrier
        pcb_no = cls._resolve_pharmacist_actor(
            user,
            tenant,
            sale.branch_id,
            explicit_pcb=str(data.get("pharmacist_pcb_no") or ""),
        )

        if sale.lifecycle_state in (
            PosSaleLifecycleState.FINALIZED,
            PosSaleLifecycleState.VOIDED,
            PosSaleLifecycleState.REVERSED,
            PosSaleLifecycleState.FULLY_RETURNED,
        ):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "detail": f"Cannot verify prescription on {sale.lifecycle_state} sale.",
                }
            )

        raw_ver = data.get("expected_version")
        if raw_ver is not None:
            try:
                exp_v = int(raw_ver)
            except (TypeError, ValueError) as exc:
                raise ValidationError({"expected_version": "expected_version must be an integer."}) from exc
            if exp_v != int(sale.version):
                raise ValidationError(
                    {
                        "error_code": "ERR_CART_VERSION_CONFLICT",
                        "detail": f"Expected version {exp_v}, got {sale.version}.",
                        "message_en": "Sale changed concurrently; refresh before pharmacist verification.",
                        "message_bn": "বিক্রয়ের তথ্য পরিবর্তিত হয়েছে; রিফ্রেশ করে অনুমোদন দিন।",
                    }
                )

        # Evaluate live classifications first & block RECALLED / BANNED items
        eval_summary = cls.evaluate_and_sync_sale_rx_gate(sale, actor_user=user, mutate=True)
        if eval_summary["has_blocked_recall_or_ban"]:
            raise ValidationError(
                {
                    "error_code": "ERR_REGULATED_PRODUCT_BLOCKED",
                    "detail": "Recalled or banned medicine cannot be approved for sale.",
                    "message_en": "Recalled or banned product detected in cart; pharmacist approval blocked.",
                    "message_bn": "কার্টে প্রত্যাহৃত বা নিষিদ্ধ ওষুধ থাকায় অনুমোদন দেওয়া যাবে না।",
                }
            )

        merged_notes = str(data.get("notes") or notes or "").strip()[:500]
        rx_ref = str(data.get("rx_reference_code") or sale.rx_reference_code or "").strip()[:64]
        inline_doctor = str(data.get("doctor_name") or "").strip()[:255]
        inline_bmdc = str(data.get("doctor_bmdc_no") or "").strip()[:64]

        if inline_doctor and len(inline_bmdc) >= 3 and not sale.prescription_id:
            sale = cls.attach_prescription_to_sale(user, sale, data)
            sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
            rx_ref = sale.rx_reference_code or rx_ref

        if eval_summary["has_narcotic"] and not (sale.prescription_id or rx_ref or len(merged_notes) >= 3):
            raise ValidationError(
                {
                    "error_code": "ERR_RX_DOCUMENTATION_REQUIRED",
                    "detail": "Narcotic/controlled drugs require attached prescription, rx_reference_code, or pharmacist notes.",
                    "message_en": "Narcotic medicine requires Rx reference, doctor BMDC, or documented verification note.",
                    "message_bn": "নার্কোটিক ওষুধের জন্য প্রেসক্রিপশন রেফারেন্স বা ফার্মাসিস্ট নোট দেওয়া বাধ্যতামূলক।",
                }
            )

        now = timezone.now()
        prev_gate = str(sale.rx_gate_status)
        old_state = sale.lifecycle_state

        sale.rx_verified_at = now
        sale.rx_verified_by = user
        sale.rx_gate_status = PosRxGateStatus.APPROVED
        sale.pharmacist_pcb_no_snapshot = pcb_no
        sale.rx_reference_code = rx_ref or sale.rx_reference_code
        sale.rx_decision_reason = merged_notes or "Pharmacist clinical prescription verification completed"
        if old_state == PosSaleLifecycleState.PENDING_RX_REVIEW:
            sale.lifecycle_state = PosSaleLifecycleState.DRAFT

        # Stamp each Rx line with verified status + approved base units + current classification snapshot
        for line in sale.lines.select_related("product"):
            cinfo = cls.classify_product(line.product)
            line.regulatory_classification = cinfo["regulatory_classification"]
            line.classification_version_snapshot = cinfo["snapshot_version"]
            if cinfo["requires_rx_verification"]:
                line.rx_verified = True
                line.rx_verified_base_units = int(line.base_units_total)
            else:
                line.rx_verified = False
                line.rx_verified_base_units = 0
            line.save(
                update_fields=[
                    "regulatory_classification",
                    "classification_version_snapshot",
                    "rx_verified",
                    "rx_verified_base_units",
                    "updated_at",
                ]
            )

        if sale.prescription_id:
            rx = PrescriptionRecord.objects.select_for_update().get(pk=sale.prescription_id)
            if rx.status == PrescriptionStatus.PENDING:
                rx.status = PrescriptionStatus.VERIFIED
                rx.pharmacist_pcb_no = pcb_no
                rx.verified_at = now
                if merged_notes:
                    rx.notes = f"{rx.notes}\n{merged_notes}".strip() if rx.notes else merged_notes
                rx.save(update_fields=["status", "pharmacist_pcb_no", "verified_at", "notes"])

        sale.version += 1
        sale.updated_by = user
        sale.save()

        PosDomainService._record_lifecycle_audit(
            sale=sale,
            from_state=old_state,
            to_state=sale.lifecycle_state,
            action="RX_VERIFIED",
            user=user,
            reason=sale.rx_decision_reason,
            metadata={"pharmacist_pcb_no": pcb_no, "rx_reference_code": sale.rx_reference_code},
        )
        cls._record_rx_audit(
            sale=sale,
            action="RX_APPROVED",
            from_gate_status=prev_gate,
            to_gate_status=PosRxGateStatus.APPROVED,
            user=user,
            decision="APPROVED",
            reason=sale.rx_decision_reason,
            rx_reference_code=sale.rx_reference_code,
            pharmacist_pcb_no=pcb_no,
            metadata={"verified_line_count": sum(1 for ln in sale.lines.all() if ln.rx_verified)},
        )
        return sale

    @classmethod
    @transaction.atomic
    def reject_sale_rx(
        cls,
        user: Any,
        sale: PosSaleOrder,
        data: Optional[Dict[str, Any]] = None,
    ) -> PosSaleOrder:
        data = data or {}
        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=sale.branch_id, require_write=True, allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES
        )

        pcb_no = cls._resolve_pharmacist_actor(
            user, tenant, sale.branch_id, explicit_pcb=str(data.get("pharmacist_pcb_no") or "")
        )

        if sale.lifecycle_state in (
            PosSaleLifecycleState.FINALIZED,
            PosSaleLifecycleState.VOIDED,
            PosSaleLifecycleState.REVERSED,
            PosSaleLifecycleState.FULLY_RETURNED,
        ):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "detail": f"Cannot reject prescription on {sale.lifecycle_state} sale.",
                }
            )

        reason = str(data.get("reason") or data.get("notes") or "").strip()[:255]
        if len(reason) < 3:
            raise ValidationError(
                {
                    "error_code": "ERR_RX_REJECTION_REASON_REQUIRED",
                    "reason": "A documented reason (>= 3 chars) is mandatory to reject Rx dispensing.",
                    "message_en": "Enter a documented clinical reason (>= 3 characters) for Rx rejection.",
                    "message_bn": "প্রেসক্রিপশন প্রত্যাখ্যানের কারণ (কমপক্ষে ৩ অক্ষর) উল্লেখ করা আবশ্যক।",
                }
            )

        prev_gate = str(sale.rx_gate_status)
        old_state = sale.lifecycle_state

        sale.rx_verified_at = None
        sale.rx_verified_by = None
        sale.rx_gate_status = PosRxGateStatus.REJECTED
        sale.pharmacist_pcb_no_snapshot = pcb_no
        sale.rx_decision_reason = reason
        sale.lifecycle_state = PosSaleLifecycleState.PENDING_RX_REVIEW
        sale.version += 1
        sale.updated_by = user
        sale.save()

        sale.lines.update(rx_verified=False, rx_verified_base_units=0)
        if sale.prescription_id:
            rx = PrescriptionRecord.objects.select_for_update().get(pk=sale.prescription_id)
            if rx.status == PrescriptionStatus.PENDING:
                rx.status = PrescriptionStatus.REJECTED
                rx.pharmacist_pcb_no = pcb_no
                rx.notes = reason
                rx.save(update_fields=["status", "pharmacist_pcb_no", "notes"])

        PosDomainService._record_lifecycle_audit(
            sale=sale,
            from_state=old_state,
            to_state=sale.lifecycle_state,
            action="RX_REJECTED",
            user=user,
            reason=reason,
            metadata={"pharmacist_pcb_no": pcb_no},
        )
        cls._record_rx_audit(
            sale=sale,
            action="RX_REJECTED",
            from_gate_status=prev_gate,
            to_gate_status=PosRxGateStatus.REJECTED,
            user=user,
            decision="REJECTED",
            reason=reason,
            pharmacist_pcb_no=pcb_no,
            was_rejected=True,
            rejection_code="ERR_RX_REJECTED",
        )
        return sale

    @classmethod
    def assert_rx_gate_for_finalization(
        cls,
        user: Any,
        sale: PosSaleOrder,
        *,
        is_offline_sync: bool = False,
    ) -> Dict[str, Any]:
        """
        Safety invariant called under transaction row lock inside finalize_sale().
        Never relies on stale flags: re-evaluates all line classifications, rejects
        recalled/banned products, rejects REJECTED orders, rejects OFFLINE unverified Rx
        orders cleanly, and requires line-by-line pharmacist verification.
        """
        summary = cls.evaluate_and_sync_sale_rx_gate(sale, actor_user=user, mutate=True)

        if summary["has_blocked_recall_or_ban"]:
            raise _RxRejectionAbort(
                sale=sale,
                audit_kwargs={
                    "action": "CLASSIFICATION_RECHECK_BLOCKED",
                    "from_gate_status": str(sale.rx_gate_status),
                    "to_gate_status": str(sale.rx_gate_status),
                    "user": user,
                    "reason": "Recalled/Banned medicine blocked at finalization",
                    "is_offline_attempt": is_offline_sync,
                    "was_rejected": True,
                    "rejection_code": "ERR_REGULATED_PRODUCT_BLOCKED",
                },
                exc=ValidationError(
                    {
                        "error_code": "ERR_REGULATED_PRODUCT_BLOCKED",
                        "detail": "One or more medicines are RECALLED or BANNED and cannot be finalized.",
                        "message_en": "Blocked medicine (RECALLED/BANNED) cannot be dispensed.",
                        "message_bn": "প্রত্যাহৃত বা নিষিদ্ধ ওষুধ বিক্রি করা যাবে না।",
                    }
                ),
            )

        if not summary["requires_rx_verification"]:
            return summary

        if sale.rx_gate_status == PosRxGateStatus.REJECTED:
            raise ValidationError(
                {
                    "error_code": "ERR_RX_REJECTED",
                    "detail": f"Sale was rejected by Pharmacist ({sale.rx_decision_reason}).",
                    "message_en": f"Prescription check REJECTED: {sale.rx_decision_reason}",
                    "message_bn": f"ফার্মাসিস্ট কর্তৃক প্রেসক্রিপশন প্রত্যাখ্যাত: {sale.rx_decision_reason}",
                }
            )

        is_offline_origin = bool(is_offline_sync) or (
            str(sale.source_channel) == "DESKTOP_POS_OFFLINE" and not sale.rx_verified_at
        )

        # Allow finalized linked verified PrescriptionRecord for P221 compatibility when all Rx lines ok
        if not summary["can_finalize_online"]:
            rx_record_preverified = bool(
                sale.prescription
                and sale.prescription.status in (PrescriptionStatus.VERIFIED, PrescriptionStatus.DISPENSED)
                and not is_offline_origin
            )
            if rx_record_preverified:
                sale.rx_verified_at = sale.rx_verified_at or timezone.now()
                sale.rx_verified_by = sale.rx_verified_by or user
                sale.rx_gate_status = PosRxGateStatus.APPROVED
                sale.pharmacist_pcb_no_snapshot = sale.pharmacist_pcb_no_snapshot or sale.prescription.pharmacist_pcb_no or "PCB-RX-LINKED"
                sale.lines.filter(requires_rx_verification=True).update(rx_verified=True)
                for ln in sale.lines.filter(requires_rx_verification=True):
                    ln.rx_verified_base_units = ln.base_units_total
                    ln.save(update_fields=["rx_verified", "rx_verified_base_units", "updated_at"])
                sale.save(update_fields=["rx_verified_at", "rx_verified_by", "rx_gate_status", "pharmacist_pcb_no_snapshot", "updated_at"])
                return cls.evaluate_and_sync_sale_rx_gate(sale, actor_user=user, mutate=False)

            if is_offline_origin:
                raise _RxRejectionAbort(
                    sale=sale,
                    mark_offline_blocked=True,
                    audit_kwargs={
                        "action": "OFFLINE_RX_BLOCKED",
                        "from_gate_status": str(summary["rx_gate_status"]),
                        "to_gate_status": PosRxGateStatus.OFFLINE_BLOCKED,
                        "user": user,
                        "reason": "Unverified Rx/antibiotic/narcotic cannot finalize offline.",
                        "is_offline_attempt": True,
                        "was_rejected": True,
                        "rejection_code": "ERR_OFFLINE_RX_BLOCKED",
                    },
                    exc=ValidationError(
                        {
                            "error_code": "ERR_OFFLINE_RX_BLOCKED",
                            "detail": "Offline POS terminals cannot finalize unverified prescription, antibiotic, or narcotic sales.",
                            "message_en": "Offline limitation: reconnect for online registered Pharmacist approval before finalizing Rx medicines.",
                            "message_bn": "অফলাইন সীমাবদ্ধতা: প্রেসক্রিপশন/অ্যান্টিবায়োটিক বিক্রয় সম্পন্ন করতে অনলাইনে নিবন্ধিত ফার্মাসিস্টের অনুমোদন প্রয়োজন।",
                        }
                    ),
                )

            raise ValidationError(
                {
                    "error_code": "ERR_RX_VERIFICATION_REQUIRED",
                    "detail": "Prescription verification by a registered pharmacist is required before finalizing.",
                    "message_en": "Registered Pharmacist approval is required before finalizing Rx medicines.",
                    "message_bn": "প্রেসক্রিপশনযুক্ত ওষুধ চূড়ান্ত করতে নিবন্ধিত ফার্মাসিস্টের অনুমোদন আবশ্যক।",
                }
            )

        return summary

    @classmethod
    def evaluate_offline_preflight(
        cls,
        user: Any,
        sale: PosSaleOrder,
        data: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """
        Evaluates whether a counter cart is eligible for offline POS finalization under P227 rules.
        OTC baskets without safety recalls/bans pass cleanly; unverified Rx/antibiotic/narcotic carts
        are strictly blocked with HTTP 422 ERR_OFFLINE_RX_BLOCKED.
        """
        PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=sale.branch_id, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        summary = cls.evaluate_and_sync_sale_rx_gate(sale, actor_user=user, mutate=True)
        if summary["can_finalize_offline"]:
            return 200, {
                "offline_eligible": True,
                "policy_version": summary["rx_safety_policy_version"],
                "rx_gate_status": summary["rx_gate_status"],
                "message_en": "Cart contains eligible OTC/cleared items only and may finalize within offline quota.",
                "message_bn": "কার্টটি অফলাইন কোটার মধ্যে সম্পন্ন করার উপযোগী।",
                "summary": summary,
            }

        if summary["has_blocked_recall_or_ban"]:
            err_code = "ERR_REGULATED_PRODUCT_BLOCKED"
        elif summary["has_narcotic"]:
            err_code = "ERR_OFFLINE_NARCOTIC_FORBIDDEN"
        else:
            err_code = "ERR_OFFLINE_RX_BLOCKED"

        cls._record_rx_audit(
            sale=sale,
            action="OFFLINE_RX_BLOCKED",
            from_gate_status=summary["rx_gate_status"],
            to_gate_status=summary["rx_gate_status"],
            user=user,
            reason=summary["offline_block_reason_en"],
            is_offline_attempt=True,
            was_rejected=True,
            rejection_code=err_code,
        )
        return 422, {
            "offline_eligible": False,
            "error_code": err_code,
            "code": err_code,
            "policy_version": summary["rx_safety_policy_version"],
            "rx_gate_status": summary["rx_gate_status"],
            "message_en": summary["offline_block_reason_en"],
            "message_bn": summary["offline_block_reason_bn"],
            "summary": summary,
        }

    @classmethod
    @transaction.atomic
    def authorize_prescription_offline(
        cls,
        user: Any,
        sale: PosSaleOrder,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Architecture Decision: ADR-0248 (P248).
        Authorizes prescription/antibiotic items for offline POS dispensing by an on-duty
        licensed pharmacist with validated PCB credentials and evidence recording.
        Strictly forbids offline authorization for narcotics/Schedule-G or recalled/banned items.
        """
        data = data or {}
        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=sale.branch_id, require_write=True, allowed_roles=cls.PHARMACIST_ROLES
        )

        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in cls.PHARMACIST_ROLES:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_PHARMACIST_PCB_REQUIRED",
                    "message_en": "Only registered pharmacists or owners with valid credentials can authorize prescriptions offline.",
                    "message_bn": "শুধুমাত্র নিবন্ধিত ফার্মাসিস্ট বা মালিক অফলাইনে প্রেসক্রিপশন অনুমোদন করতে পারেন।",
                }
            )

        summary = cls.evaluate_and_sync_sale_rx_gate(sale, actor_user=user, mutate=False)

        # 1. Recalled or Banned check
        if summary["has_blocked_recall_or_ban"]:
            raise ValidationError(
                {
                    "error_code": "ERR_REGULATED_PRODUCT_BLOCKED",
                    "detail": "Cart contains recalled or banned items that cannot be authorized or dispensed.",
                }
            )

        # 2. Strict Narcotic / Schedule-G Prohibition
        if summary["has_narcotic"]:
            cls._record_rx_audit(
                sale=sale,
                action="OFFLINE_RX_BLOCKED",
                from_gate_status=summary["rx_gate_status"],
                to_gate_status=summary["rx_gate_status"],
                user=user,
                reason="Attempted offline authorization of Schedule-G narcotic was forbidden.",
                is_offline_attempt=True,
                was_rejected=True,
                rejection_code="ERR_OFFLINE_NARCOTIC_FORBIDDEN",
            )
            raise ValidationError(
                {
                    "error_code": "ERR_OFFLINE_NARCOTIC_FORBIDDEN",
                    "detail": "Narcotics and Schedule-G controlled substances are strictly prohibited from offline dispensing under DGDA regulations.",
                    "message_en": "Narcotics cannot be authorized or dispensed offline. Online real-time DGDA narcotic audit is mandatory.",
                    "message_bn": "মাদক ও সিডিউল-জি ওষুধ অফলাইনে অনুমোদন বা বিক্রয় করা সম্পূর্ণ নিষিদ্ধ।",
                }
            )

        # 3. Pharmacist PCB validation
        pcb_no = str(
            data.get("pharmacist_pcb_no")
            or getattr(user, "pcb_registration_no", "")
            or ""
        ).strip().upper()
        if len(pcb_no) < 4:
            raise ValidationError(
                {"pharmacist_pcb_no": "A valid Pharmacist PCB registration number (minimum 4 characters) is required for offline authorization."}
            )

        # 4. Doctor BMDC & Name validation
        doctor_bmdc = str(data.get("doctor_bmdc_no") or "").strip().upper()
        if len(doctor_bmdc) < 4:
            raise ValidationError(
                {"doctor_bmdc_no": "Doctor BMDC registration number (minimum 4 characters) is mandatory for offline prescription verification."}
            )

        doctor_name = str(data.get("doctor_name") or "").strip()
        if len(doctor_name) < 2:
            raise ValidationError(
                {"doctor_name": "Doctor name is mandatory for offline prescription verification."}
            )

        # 5. Offline justification / notes validation
        notes = str(data.get("review_notes") or data.get("offline_authorization_reason") or "").strip()
        if len(notes) < 5:
            raise ValidationError(
                {"review_notes": "Clinical justification / verification notes (minimum 5 characters) are mandatory."}
            )

        # Update PosSaleOrder
        from_gate = str(sale.rx_gate_status)
        sale.rx_gate_status = PosRxGateStatus.APPROVED
        sale.rx_verified_at = timezone.now()
        sale.rx_verified_by = user
        sale.pharmacist_pcb_no_snapshot = pcb_no
        sale.rx_reference_code = f"OFF-RX-{pcb_no[:8]}-{sale.sale_id[-6:].upper()}"
        sale.rx_decision_reason = f"OFFLINE_PHARMACIST_VERIFIED: {notes}"
        sale.rx_offline_blocked_reason = ""

        # Update prescription-requiring sale lines
        sale.lines.filter(requires_rx_verification=True).update(
            rx_verified=True,
        )
        for ln in sale.lines.filter(requires_rx_verification=True):
            ln.rx_verified_base_units = ln.base_units_total
            ln.save(update_fields=["rx_verified", "rx_verified_base_units", "updated_at"])

        sale.save(
            update_fields=[
                "rx_gate_status",
                "rx_verified_at",
                "rx_verified_by",
                "pharmacist_pcb_no_snapshot",
                "rx_reference_code",
                "rx_decision_reason",
                "rx_offline_blocked_reason",
                "updated_at",
            ]
        )

        cls._record_rx_audit(
            sale=sale,
            action="OFFLINE_RX_AUTHORIZED",
            from_gate_status=from_gate,
            to_gate_status=PosRxGateStatus.APPROVED,
            user=user,
            reason=f"Offline Rx verified by Pharmacist {pcb_no}: {notes}",
            is_offline_attempt=True,
            was_rejected=False,
        )

        return cls.evaluate_and_sync_sale_rx_gate(sale, actor_user=user, mutate=False)

    @classmethod
    def record_antibiotic_register_on_finalize(
        cls,
        user: Any,
        sale: PosSaleOrder,
        invoice: SaleInvoice,
    ) -> List[AntibioticDispenseRegisterEntry]:
        """
        Idempotently posts one AntibioticDispenseRegisterEntry per antibiotic line
        inside the finalize_sale atomic transaction (SRS FR-036).
        Retries never duplicate due to OneToOne(sale_line) + get_or_create.
        """
        entries: List[AntibioticDispenseRegisterEntry] = []
        rx = sale.prescription
        rx_items_by_pid: Dict[str, PrescriptionItem] = {}
        if rx:
            for pitem in rx.items.all():
                rx_items_by_pid[pitem.product_id] = pitem

        pcb_no = str(
            sale.pharmacist_pcb_no_snapshot
            or (rx.pharmacist_pcb_no if rx else "")
            or getattr(user, "pcb_registration_no", "")
            or ""
        )[:64]
        approving_user = sale.rx_verified_by or user
        patient_tok = rx.patient_token if rx else (f"PT-{sale.customer_id[-6:].upper()}" if sale.customer_id else f"PT-{sale.sale_id[-6:].upper()}")

        for line in sale.lines.select_related("product").order_by("line_number"):
            cinfo = cls.classify_product(line.product)
            if not cinfo["is_antibiotic"]:
                continue

            rx_item = rx_items_by_pid.get(line.product_id)
            dosage_instr = rx_item.dosage_instructions if rx_item else "Per verified prescription"
            duration_days = rx_item.duration_days if rx_item else None

            entry, created = AntibioticDispenseRegisterEntry.objects.get_or_create(
                sale_line=line,
                defaults={
                    "register_entry_id": f"abxreg-{uuid.uuid4().hex[:12]}",
                    "tenant": sale.tenant,
                    "branch": sale.branch,
                    "sale": sale,
                    "invoice": invoice,
                    "prescription": rx,
                    "product": line.product,
                    "brand_name": line.product.brand_name[:255],
                    "generic_name": line.product.generic_name[:255],
                    "classification_tier": cinfo["regulatory_classification"],
                    "dispensed_base_units": int(line.base_units_total),
                    "returned_base_units": 0,
                    "dosage_instructions": str(dosage_instr or "")[:255],
                    "duration_days": duration_days,
                    "rx_reference_code": str(sale.rx_reference_code or (rx.prescription_id if rx else ""))[:64],
                    "doctor_name": (rx.doctor_name if rx else "")[:255],
                    "doctor_bmdc_no": (rx.doctor_bmdc_no if rx else "")[:64],
                    "patient_token": patient_tok[:64],
                    "approving_pharmacist": approving_user if getattr(approving_user, "pk", None) else None,
                    "approving_pharmacist_pcb_no": pcb_no,
                    "status": AntibioticRegisterStatus.ACTIVE,
                    "dispensed_at": sale.finalized_at or timezone.now(),
                },
            )
            if created:
                cls._record_rx_audit(
                    sale=sale,
                    action="ANTIBIOTIC_REGISTER_RECORDED",
                    from_gate_status=str(sale.rx_gate_status),
                    to_gate_status=str(sale.rx_gate_status),
                    user=user,
                    prescription=rx,
                    reason=f"Antibiotic register entry {entry.register_entry_id} for {line.product.brand_name} ({line.base_units_total}u)",
                    pharmacist_pcb_no=pcb_no,
                    metadata={
                        "register_entry_id": entry.register_entry_id,
                        "product_id": line.product_id,
                        "dispensed_base_units": int(line.base_units_total),
                    },
                )
            entries.append(entry)
        return entries

    @classmethod
    def reverse_antibiotic_register_on_reversal(
        cls,
        user: Any,
        sale: PosSaleOrder,
        reason: str,
    ) -> int:
        """
        Transitions active AntibioticDispenseRegisterEntry rows to REVERSED when
        a finalized POS sale is reversed/returned (never hard-deleted).
        """
        now = timezone.now()
        entries = list(
            AntibioticDispenseRegisterEntry.objects.select_for_update().filter(
                sale=sale,
                status__in=[AntibioticRegisterStatus.ACTIVE, AntibioticRegisterStatus.PARTIALLY_RETURNED],
            )
        )
        updated_count = 0
        for entry in entries:
            entry.status = AntibioticRegisterStatus.REVERSED
            entry.returned_base_units = int(entry.dispensed_base_units)
            entry.reversed_at = now
            entry.reversal_reason = str(reason or "Finalized sale reversed")[:255]
            entry.save(update_fields=["status", "returned_base_units", "reversed_at", "reversal_reason", "updated_at"])
            updated_count += 1
        return updated_count

    @classmethod
    def build_printable_receipt(cls, user: Any, sale: PosSaleOrder) -> Dict[str, Any]:
        """
        Constructs printable thermal/A4 POS receipt payload including Decimal breakdown,
        split payments, customer due balance, sync status label, and Rx/Antibiotic Register compliance footer.
        Respects COST_EXEMPT_ROLES boundary cleanly.
        """
        PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=sale.branch_id, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        rx_summary = cls.evaluate_and_sync_sale_rx_gate(sale, actor_user=user, mutate=False)
        rx = sale.prescription
        abx_ids = list(
            AntibioticDispenseRegisterEntry.objects.filter(sale=sale)
            .order_by("register_entry_id")
            .values_list("register_entry_id", flat=True)
        )
        attempts = [
            {
                "attempt_id": att.attempt_id,
                "attempt_number": att.attempt_number,
                "tender_type": att.tender_type,
                "amount_bdt": f"{att.amount_bdt:.2f}",
                "change_given_bdt": f"{att.change_given_bdt:.2f}",
                "net_settled_amount_bdt": f"{(att.net_settled_amount_bdt if att.net_settled_amount_bdt > Decimal('0.00') else att.amount_bdt):.2f}",
                "status": att.status,
                "verification_status": att.verification_status,
                "verification_channel": att.verification_channel,
                "gateway_reference": att.gateway_reference or "",
                "card_last4": att.card_last4 or "",
            }
            for att in sale.payment_attempts.order_by("attempt_number")
        ]
        tender_summary: Dict[str, str] = {}
        for att in sale.payment_attempts.filter(status__in=["CAPTURED", "REFUNDED"]):
            net_v = att.net_settled_amount_bdt if att.net_settled_amount_bdt > Decimal("0.00") else att.amount_bdt
            curr = Decimal(tender_summary.get(att.tender_type, "0.00"))
            tender_summary[att.tender_type] = f"{(curr + net_v):.2f}"
        from apps.core.models import CustomerDueRecord

        due_rec = (
            CustomerDueRecord.objects.filter(sale=sale).order_by("-created_at").first()
            or (
                CustomerDueRecord.objects.filter(invoice_id=sale.linked_invoice_id).order_by("-created_at").first()
                if sale.linked_invoice_id
                else None
            )
        )
        cust = sale.customer
        customer_due_snapshot = {
            "customer_id": sale.customer_id or "",
            "customer_name": sale.customer_name or (cust.full_name if cust else "Walk-in Customer"),
            "credit_status": cust.credit_status if cust else "UNAPPROVED",
            "is_credit_approved": bool(cust.is_credit_approved) if cust else False,
            "credit_limit_bdt": f"{cust.credit_limit_bdt:.2f}" if cust else "0.00",
            "customer_total_due_bdt": f"{cust.current_due_bdt:.2f}" if cust else "0.00",
            "due_record_id": due_rec.due_id if due_rec else "",
            "due_status": due_rec.status if due_rec else ("NONE" if sale.due_amount_bdt == Decimal("0.00") else "PENDING"),
            "invoice_due_bdt": f"{sale.due_amount_bdt:.2f}",
            "paid_against_due_bdt": f"{due_rec.paid_amount_bdt:.2f}" if due_rec else "0.00",
            "remaining_due_bdt": f"{due_rec.remaining_due_bdt:.2f}" if due_rec else f"{sale.due_amount_bdt:.2f}",
            "due_date": due_rec.due_date.isoformat() if (due_rec and due_rec.due_date) else None,
        }
        show_cost = PosDomainService.can_view_cost(user)
        allocs_qs = (
            sale.allocations.select_related("batch", "product")
            .order_by("sale_line__line_number", "fefo_sequence", "allocation_id")
        )
        lot_allocations = []
        lots_by_line: Dict[str, List[Dict[str, Any]]] = {}
        for alloc in allocs_qs:
            lot_dict = {
                "allocation_id": alloc.allocation_id,
                "line_number": alloc.sale_line.line_number if alloc.sale_line else 1,
                "product_id": alloc.product_id,
                "brand_name": alloc.product.brand_name,
                "batch_id": alloc.batch_id,
                "batch_number": alloc.batch.batch_number,
                "expiry_date": (alloc.expiry_date_snapshot or alloc.batch.expiry_date).isoformat(),
                "allocated_base_units": int(alloc.allocated_base_units),
                "allocation_mode": alloc.allocation_mode,
                "status": alloc.status,
                "unit_cost_bdt": f"{alloc.unit_cost_bdt:.4f}" if show_cost else None,
                "total_cost_bdt": f"{alloc.total_cost_bdt:.4f}" if show_cost else None,
            }
            lot_allocations.append(lot_dict)
            lots_by_line.setdefault(alloc.sale_line_id, []).append(lot_dict)

        items = [
            {
                "line_number": ln.line_number,
                "product_id": ln.product_id,
                "brand_name": ln.product.brand_name,
                "generic_name": ln.product.generic_name,
                "packaging_unit": ln.packaging_unit,
                "quantity_units": int(ln.quantity_units),
                "base_units_total": int(ln.base_units_total),
                "unit_price_bdt": f"{ln.unit_price_bdt:.2f}",
                "discount_amount_bdt": f"{ln.discount_amount_bdt:.2f}",
                "line_tax_amount_bdt": f"{ln.line_tax_amount_bdt:.2f}",
                "line_total_bdt": f"{ln.line_total_bdt:.2f}",
                "regulatory_classification": ln.regulatory_classification,
                "rx_verified": bool(ln.rx_verified),
                "allocated_lots": lots_by_line.get(ln.line_id, []),
            }
            for ln in sale.lines.select_related("product").order_by("line_number")
        ]

        is_offline_pending = str(sale.sync_status) in ("OFFLINE_QUEUED", "OFFLINE_SYNCED_RECONCILING")
        return {
            "receipt_version": "POS-RCPT-v2026.1",
            "tenant_id": sale.tenant_id,
            "tenant_name": sale.tenant.name,
            "branch_id": sale.branch_id,
            "branch_name": sale.branch.name,
            "branch_license_number": sale.branch.license_number,
            "sale_id": sale.sale_id,
            "order_number": sale.order_number,
            "invoice_id": sale.linked_invoice_id,
            "invoice_number": sale.linked_invoice.invoice_number if sale.linked_invoice else "",
            "lifecycle_state": sale.lifecycle_state,
            "sync_status": sale.sync_status,
            "is_offline_pending_sync": is_offline_pending,
            "offline_notice_en": (
                "SAVED LOCALLY - PENDING CLOUD SYNC" if is_offline_pending else "CLOUD CONFIRMED TAX/SALES RECEIPT"
            ),
            "offline_notice_bn": (
                "লোকালে সংরক্ষিত — ক্লাউড সিঙ্ক অপেক্ষমান" if is_offline_pending else "ক্লাউড নিশ্চিতকৃত বিক্রয় রসিদ"
            ),
            "customer_name": sale.customer_name,
            "fefo_allocation_status": str(sale.fefo_allocation_status),
            "items": items,
            "lot_allocations": lot_allocations,
            "totals": {
                "subtotal_bdt": f"{sale.subtotal_bdt:.2f}",
                "discount_amount_bdt": f"{sale.discount_amount_bdt:.2f}",
                "net_before_tax_bdt": f"{sale.net_before_tax_bdt:.2f}",
                "tax_amount_bdt": f"{sale.tax_amount_bdt:.2f}",
                "rounding_adjustment_bdt": f"{sale.rounding_adjustment_bdt:.2f}",
                "delivery_fee_bdt": f"{sale.delivery_fee_bdt:.2f}",
                "grand_total_bdt": f"{sale.grand_total_bdt:.2f}",
                "paid_amount_bdt": f"{sale.paid_amount_bdt:.2f}",
                "due_amount_bdt": f"{sale.due_amount_bdt:.2f}",
                "change_returned_bdt": f"{sale.change_returned_bdt:.2f}",
                "refunded_amount_bdt": f"{sale.refunded_amount_bdt:.2f}",
            },
            "payments": attempts,
            "tender_summary": tender_summary,
            "customer_due_snapshot": customer_due_snapshot,
            "rx_compliance_footer": {
                "requires_rx_verification": rx_summary["requires_rx_verification"],
                "rx_gate_status": rx_summary["rx_gate_status"],
                "pharmacist_email": sale.rx_verified_by.email if sale.rx_verified_by else "",
                "pharmacist_pcb_no": str(sale.pharmacist_pcb_no_snapshot or ""),
                "rx_verified_at": sale.rx_verified_at.isoformat() if sale.rx_verified_at else None,
                "rx_reference_code": str(sale.rx_reference_code or ""),
                "doctor_bmdc_no": rx.doctor_bmdc_no if rx else "",
                "patient_token": rx.patient_token if rx else "",
                "antibiotic_register_entry_ids": abx_ids,
                "policy_version": rx_summary["rx_safety_policy_version"],
            },
            "finalized_at": sale.finalized_at.isoformat() if sale.finalized_at else None,
        }
