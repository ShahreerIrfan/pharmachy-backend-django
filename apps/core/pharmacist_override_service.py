"""
backend/apps/core/pharmacist_override_service.py

Architecture Decision: ADR-0249 (P249).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Service for credentialed pharmacist clinical overrides for permitted exceptions
(emergency chronic care continuation, package splitting, manual generic equivalency).
Enforces mandatory PCB verification, statutory policy reference, patient consent evidence,
and cryptographic SHA-256 integrity hash.
Strictly prohibits overrides for narcotics/Schedule-G or recalled/banned medicines.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from rest_framework.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import (
    MedicineProduct,
    PatientConsentType,
    PharmacistClinicalOverride,
    PharmacistOverrideCategory,
    PosRegulatoryClass,
    PosRxGateStatus,
    PosSaleOrder,
    PosSaleOrderLine,
    StaffMember,
    Tenant,
    Branch,
)
from apps.core.pos_domain_service import PosDomainService
from apps.core.pos_rx_safety_service import PosRxSafetyService
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class PharmacistClinicalOverrideService:
    """
    Coordinates licensed pharmacist clinical overrides for permitted statutory exceptions.
    """

    ALLOWED_OVERRIDE_ROLES = {
        UserRole.STAFF_PHARMACIST,
        UserRole.PHARMACY_OWNER,
        UserRole.BRANCH_MANAGER,
        UserRole.SUPERADMIN,
        "STAFF_PHARMACIST",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "SUPERADMIN",
    }

    @classmethod
    def serialize_override(cls, override: PharmacistClinicalOverride) -> Dict[str, Any]:
        """Serializes override record to contract JSON dict."""
        return {
            "override_id": override.override_id,
            "sale_id": override.sale_id,
            "sale_line_id": override.sale_line_id,
            "product_id": override.product_id,
            "product_name": override.product.brand_name,
            "generic_name": override.product.generic_name,
            "pharmacist_user_id": str(override.pharmacist_user_id),
            "pharmacist_pcb_no": override.pharmacist_pcb_no,
            "override_category": override.override_category,
            "statutory_policy_reference": override.statutory_policy_reference,
            "consent_type": override.consent_type,
            "consent_evidence_notes": override.consent_evidence_notes,
            "clinical_justification": override.clinical_justification,
            "approved_base_units": override.approved_base_units,
            "sha256_hash": override.sha256_hash,
            "created_at": override.created_at.isoformat() if override.created_at else None,
        }

    @classmethod
    @transaction.atomic
    def record_clinical_override(
        cls,
        user: Any,
        sale: PosSaleOrder,
        product: MedicineProduct,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Records a licensed pharmacist clinical override on a sale order.
        Validates pharmacist credentials, statutory policy, patient consent evidence,
        and strictly blocks narcotics/Schedule-G or recalled drugs.
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required to record clinical override.")

        # 1. Role validation: Must be licensed pharmacist or pharmacy owner
        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in cls.ALLOWED_OVERRIDE_ROLES:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_PHARMACIST_OVERRIDE_UNAUTHORIZED",
                    "message_en": "Cashiers and non-credentialed staff cannot execute clinical overrides.",
                    "message_bn": "ক্যাশিয়ার বা অনুমোদনহীন কর্মী ক্লিনিক্যাল ওভাররাইড সম্পাদন করতে পারবেন না।",
                }
            )

        # 2. Multi-tenant and branch resolution
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=sale.branch_id, require_write=True, allowed_roles=cls.ALLOWED_OVERRIDE_ROLES
        )

        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        if sale.tenant_id != tenant.tenant_id:
            raise PermissionDenied("Cross-tenant override access denied.")

        # 3. Product safety classification checks
        classification_summary = PosRxSafetyService.classify_product(product)
        if classification_summary["is_blocked_recall_or_ban"]:
            raise ValidationError(
                {
                    "error_code": "ERR_REGULATED_PRODUCT_BLOCKED",
                    "product": f"Medicine '{product.brand_name}' is subject to statutory recall or ban and CANNOT be overridden.",
                    "message_en": "Recalled or banned drugs cannot be overridden under any condition.",
                    "message_bn": "প্রত্যাহৃত বা নিষিদ্ধ ওষুধ কোনো অবস্থাতেই ওভাররাইড করা যাবে না।",
                }
            )

        if classification_summary["is_narcotic"] or product.is_narcotic:
            raise ValidationError(
                {
                    "error_code": "ERR_NARCOTIC_OVERRIDE_FORBIDDEN",
                    "product": f"Narcotic / Schedule-G substance '{product.brand_name}' CANNOT be dispensed via pharmacist override without valid prescriber authorization.",
                    "message_en": "Narcotics cannot be dispensed via pharmacist override under DGDA narcotics control laws.",
                    "message_bn": "ডিজিডিএ মাদক নিয়ন্ত্রণ আইন অনুযায়ী মাদকদ্রব্য ফার্মাসিস্ট ওভাররাইডের মাধ্যমে সরবরাহ করা সম্পূর্ণ নিষিদ্ধ।",
                }
            )

        # 4. Mandatory PCB credentials validation
        clean_pcb = str(
            data.get("pharmacist_pcb_no")
            or getattr(user, "pcb_registration_no", "")
            or ""
        ).strip().upper()
        if len(clean_pcb) < 4:
            raise ValidationError(
                {"pharmacist_pcb_no": "A valid Pharmacist PCB registration number (minimum 4 characters) is required for clinical override."}
            )

        # 5. Override category validation
        cat = str(data.get("override_category") or "").strip().upper()
        if cat not in PharmacistOverrideCategory.values:
            raise ValidationError(
                {"override_category": f"Invalid override category '{cat}'. Must be one of {PharmacistOverrideCategory.values}."}
            )

        # 6. Statutory policy reference validation
        policy_ref = str(data.get("statutory_policy_reference") or "").strip()
        if len(policy_ref) < 5:
            raise ValidationError(
                {"statutory_policy_reference": "Statutory policy reference (minimum 5 characters) is mandatory."}
            )

        # 7. Patient consent evidence validation
        consent = str(data.get("consent_type") or PatientConsentType.INFORMED_VERBAL).strip().upper()
        if consent not in PatientConsentType.values:
            raise ValidationError(
                {"consent_type": f"Invalid consent type '{consent}'. Must be one of {PatientConsentType.values}."}
            )

        consent_notes = str(data.get("consent_evidence_notes") or "").strip()
        if len(consent_notes) < 5:
            raise ValidationError(
                {"consent_evidence_notes": "Patient informed consent evidence notes (minimum 5 characters) are mandatory."}
            )

        clinical_notes = str(data.get("clinical_justification") or "").strip()
        if len(clinical_notes) < 5:
            raise ValidationError(
                {"clinical_justification": "Clinical justification notes (minimum 5 characters) are mandatory."}
            )

        # 8. Approved base units validation
        try:
            approved_units = int(data.get("approved_base_units", 1))
            if approved_units < 1:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError({"approved_base_units": "Approved base units must be a positive integer >= 1."})

        # Match or identify sale line
        sale_line = sale.lines.filter(product=product).first()
        line_id = sale_line.line_id if sale_line else None

        # 9. Create immutable PharmacistClinicalOverride record
        override = PharmacistClinicalOverride.objects.create(
            override_id=f"pco-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            sale=sale,
            sale_line=sale_line,
            product=product,
            pharmacist_user=user,
            pharmacist_pcb_no=clean_pcb,
            override_category=cat,
            statutory_policy_reference=policy_ref,
            consent_type=consent,
            consent_evidence_notes=consent_notes,
            clinical_justification=clinical_notes,
            approved_base_units=approved_units,
        )

        # 10. Update POS Sale Order and Sale Line states
        if sale_line:
            sale_line.rx_verified = True
            sale_line.rx_verified_base_units = approved_units
            sale_line.save(update_fields=["rx_verified", "rx_verified_base_units", "updated_at"])

        from_gate = str(sale.rx_gate_status)
        sale.rx_gate_status = PosRxGateStatus.APPROVED
        sale.rx_verified_at = timezone.now()
        sale.rx_verified_by = user
        sale.pharmacist_pcb_no_snapshot = clean_pcb
        sale.rx_reference_code = f"OVR-{clean_pcb[:8]}-{sale.sale_id[-6:].upper()}"
        sale.rx_decision_reason = f"PHARMACIST_OVERRIDE [{cat}]: {clinical_notes[:100]}"
        sale.rx_offline_blocked_reason = ""
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

        # 11. Record audit log
        PosRxSafetyService._record_rx_audit(
            sale=sale,
            action="PHARMACIST_OVERRIDE_RECORDED",
            from_gate_status=from_gate,
            to_gate_status=PosRxGateStatus.APPROVED,
            user=user,
            reason=f"Clinical override by {clean_pcb} ({cat}): {clinical_notes}",
            metadata={
                "override_id": override.override_id,
                "category": cat,
                "policy_ref": policy_ref,
                "consent_type": consent,
                "approved_units": approved_units,
                "sha256": override.sha256_hash,
            },
        )

        return {
            "override": cls.serialize_override(override),
            "rx_gate": PosRxSafetyService.evaluate_and_sync_sale_rx_gate(sale, actor_user=user, mutate=False),
        }

    @classmethod
    def list_sale_overrides(cls, user: Any, sale: PosSaleOrder) -> List[Dict[str, Any]]:
        """Lists all clinical overrides recorded for a sale order."""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=sale.branch_id, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        overrides = (
            PharmacistClinicalOverride.objects.filter(tenant=tenant, sale=sale)
            .select_related("product", "pharmacist_user")
            .order_by("-created_at")
        )
        return [cls.serialize_override(o) for o in overrides]
