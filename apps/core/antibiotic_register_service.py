"""
backend/apps/core/antibiotic_register_service.py

Architecture Decision: ADR-0246 (P246).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Authoritative domain service for DGDA Antibiotic Dispense Register Ledger:
- Binds antibiotic sales directly to sale, medicine, quantity, pharmacist PCB, verified Rx, and branch.
- Immutable register entries with cryptographic SHA-256 tamper-evident integrity hashes.
- Audited partial return and reversal tracking.
- Privacy-safe DGDA compliance ledger exports with WHO AWaRe classification metrics.
- Server-side tenant isolation and pharmacist credential enforcement.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple
import uuid

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    AntibioticDispenseRegisterEntry,
    AntibioticRegisterStatus,
    Branch,
    MedicineProduct,
    PosRegulatoryClass,
    PosSaleOrder,
    PosSaleOrderLine,
    PrescriptionRecord,
    PrescriptionStatus,
    SaleInvoice,
    StaffMember,
    StaffRole,
    Tenant,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class AntibioticRegisterLedgerService:
    """
    Manages statutory antibiotic dispense ledger records under Bangladesh DGDA
    regulations and antimicrobial stewardship standards.
    """

    ALLOWED_REVERSAL_ROLES = {
        "STAFF_PHARMACIST",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "SUPERADMIN",
    }

    @classmethod
    def compute_sha256_hash(
        cls,
        *,
        tenant_id: str,
        branch_id: str,
        sale_id: str,
        product_id: str,
        base_units: int,
        pharmacist_pcb_no: str,
        doctor_bmdc_no: str,
        dispensed_at_iso: str,
    ) -> str:
        """
        Computes a deterministic cryptographic hash over statutory fields
        to guarantee ledger immutability and non-tampering.
        """
        payload = (
            f"T:{tenant_id}|B:{branch_id}|S:{sale_id}|P:{product_id}|"
            f"U:{base_units}|PCB:{pharmacist_pcb_no}|BMDC:{doctor_bmdc_no}|"
            f"TIME:{dispensed_at_iso}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    @transaction.atomic
    def record_dispense_entry(
        cls,
        user: Any,
        *,
        tenant_id: str,
        branch_id: str,
        sale: PosSaleOrder,
        sale_line: PosSaleOrderLine,
        product: MedicineProduct,
        dispensed_base_units: int,
        prescription: Optional[PrescriptionRecord] = None,
        invoice: Optional[SaleInvoice] = None,
        pharmacist_pcb_no: Optional[str] = None,
        dosage_instructions: str = "",
        duration_days: Optional[int] = None,
    ) -> AntibioticDispenseRegisterEntry:
        """
        Records a new immutable AntibioticDispenseRegisterEntry.
        Validates antibiotic classification, prescription validity, and pharmacist credentials.
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required to record antibiotic dispense.")

        # 1. Product antimicrobial validation
        if not getattr(product, "is_antibiotic", False):
            raise ValidationError(
                {"product": f"Product '{product.brand_name}' is not marked as an antibiotic."}
            )

        # 2. Units validation
        if dispensed_base_units <= 0:
            raise ValidationError({"dispensed_base_units": "Dispensed base units must be at least 1."})

        # 3. Pharmacist PCB validation
        if pharmacist_pcb_no is not None:
            clean_pcb = str(pharmacist_pcb_no).strip().upper()
        else:
            clean_pcb = str(
                (prescription.pharmacist_pcb_no if prescription else "")
                or getattr(user, "pcb_registration_no", "")
                or ""
            ).strip().upper()
        if not clean_pcb:
            raise ValidationError(
                {"pharmacist_pcb_no": "Pharmacist PCB registration number is mandatory for antibiotic dispensing."}
            )

        # 4. Prescription linkage validation
        rx = prescription or getattr(sale, "prescription", None)
        if not rx:
            raise ValidationError(
                {"prescription": "A verified clinical prescription is mandatory to dispense antibiotics under DGDA regulations."}
            )

        if rx.status in (PrescriptionStatus.REJECTED, PrescriptionStatus.CANCELLED, PrescriptionStatus.EXPIRED):
            raise ValidationError(
                {"prescription": f"Cannot dispense antibiotics against prescription in '{rx.status}' status."}
            )

        # 5. Doctor details & Patient token
        doctor_name = (rx.doctor_name or "")[:255]
        doctor_bmdc = (rx.doctor_bmdc_no or "")[:64]
        patient_token = (rx.patient_token or f"PT-{sale.sale_id[-6:].upper()}")[:64]

        tenant = Tenant.objects.get(tenant_id=tenant_id)
        branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)

        # Regulatory classification tier
        classification_tier = getattr(product, "regulatory_classification", PosRegulatoryClass.ANTIMICROBIAL_ACCESS)
        dispensed_time = timezone.now()

        sha256 = cls.compute_sha256_hash(
            tenant_id=str(tenant.tenant_id),
            branch_id=str(branch.branch_id),
            sale_id=str(sale.sale_id),
            product_id=str(product.product_id),
            base_units=dispensed_base_units,
            pharmacist_pcb_no=clean_pcb,
            doctor_bmdc_no=doctor_bmdc,
            dispensed_at_iso=dispensed_time.isoformat(),
        )

        entry, created = AntibioticDispenseRegisterEntry.objects.get_or_create(
            sale_line=sale_line,
            defaults={
                "register_entry_id": f"abxreg-{uuid.uuid4().hex[:12]}",
                "tenant": tenant,
                "branch": branch,
                "sale": sale,
                "invoice": invoice or getattr(sale, "invoice", None),
                "prescription": rx,
                "product": product,
                "brand_name": product.brand_name[:255],
                "generic_name": product.generic_name[:255],
                "classification_tier": classification_tier,
                "dispensed_base_units": dispensed_base_units,
                "returned_base_units": 0,
                "dosage_instructions": str(dosage_instructions or "")[:255],
                "duration_days": duration_days,
                "rx_reference_code": str(rx.prescription_number or rx.prescription_id)[:64],
                "doctor_name": doctor_name,
                "doctor_bmdc_no": doctor_bmdc,
                "patient_token": patient_token,
                "approving_pharmacist": user if getattr(user, "pk", None) else None,
                "approving_pharmacist_pcb_no": clean_pcb,
                "status": AntibioticRegisterStatus.ACTIVE,
                "sha256_hash": sha256,
                "dispensed_at": dispensed_time,
            },
        )
        return entry

    @classmethod
    @transaction.atomic
    def reverse_dispense_entry(
        cls,
        user: Any,
        *,
        tenant_id: str,
        register_entry_id: str,
        return_base_units: Optional[int] = None,
        reversal_reason: str = "",
    ) -> AntibioticDispenseRegisterEntry:
        """
        Audited reversal or partial return of an antibiotic register record.
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required to reverse antibiotic register entry.")

        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in cls.ALLOWED_REVERSAL_ROLES and not getattr(user, "is_superuser", False):
            raise PermissionDenied("Only registered pharmacists or managers can reverse antibiotic register records.")

        clean_reason = str(reversal_reason or "").strip()
        if not clean_reason:
            raise ValidationError({"reversal_reason": "Reversal reason is mandatory for antimicrobial audit."})

        entry = (
            AntibioticDispenseRegisterEntry.objects.select_for_update()
            .filter(register_entry_id=register_entry_id, tenant_id=tenant_id)
            .first()
        )
        if not entry:
            raise ValidationError(
                {"register_entry_id": f"Antibiotic register entry '{register_entry_id}' not found."}
            )

        if entry.status == AntibioticRegisterStatus.REVERSED:
            raise ValidationError({"status": "Register entry is already fully reversed."})

        available_units = entry.dispensed_base_units - entry.returned_base_units
        units_to_return = return_base_units if return_base_units is not None else available_units

        if units_to_return <= 0 or units_to_return > available_units:
            raise ValidationError(
                {"return_base_units": f"Invalid return quantity ({units_to_return} units, maximum available: {available_units})."}
            )

        entry.returned_base_units += units_to_return
        if entry.returned_base_units >= entry.dispensed_base_units:
            entry.status = AntibioticRegisterStatus.REVERSED
        else:
            entry.status = AntibioticRegisterStatus.PARTIALLY_RETURNED

        entry.reversed_at = timezone.now()
        entry.reversal_reason = f"{entry.reversal_reason}\n[{entry.reversed_at.isoformat()} Returned {units_to_return}u]: {clean_reason}".strip()
        entry.save(update_fields=["returned_base_units", "status", "reversed_at", "reversal_reason", "updated_at"])

        logger.info(
            "Antibiotic register %s reversed by %s (%d units returned, status=%s)",
            register_entry_id,
            user,
            units_to_return,
            entry.status,
        )
        return entry

    @classmethod
    def get_dgda_compliance_ledger(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        classification_tier: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Produces statutory DGDA antimicrobial audit records and AWaRe summary metrics.
        Guarantees privacy-safe exports (uses synthetic patient tokens, no raw patient PII).
        """
        qs = AntibioticDispenseRegisterEntry.objects.filter(tenant_id=tenant_id)

        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if start_date:
            qs = qs.filter(dispensed_at__date__gte=start_date)
        if end_date:
            qs = qs.filter(dispensed_at__date__lte=end_date)
        if classification_tier:
            qs = qs.filter(classification_tier=classification_tier)

        total_records = qs.count()
        total_dispensed = qs.aggregate(s=Sum("dispensed_base_units"))["s"] or 0
        total_returned = qs.aggregate(s=Sum("returned_base_units"))["s"] or 0
        net_active_units = max(0, total_dispensed - total_returned)

        # AWaRe Category counts
        tier_counts = {
            PosRegulatoryClass.ANTIMICROBIAL_ACCESS: qs.filter(classification_tier=PosRegulatoryClass.ANTIMICROBIAL_ACCESS).count(),
            PosRegulatoryClass.ANTIMICROBIAL_WATCH: qs.filter(classification_tier=PosRegulatoryClass.ANTIMICROBIAL_WATCH).count(),
            PosRegulatoryClass.ANTIMICROBIAL_RESERVE: qs.filter(classification_tier=PosRegulatoryClass.ANTIMICROBIAL_RESERVE).count(),
        }

        records_data = []
        for ent in qs.select_related("sale", "invoice", "prescription", "product")[:200]:
            records_data.append({
                "register_entry_id": ent.register_entry_id,
                "order_number": ent.sale.order_number if ent.sale else "",
                "invoice_number": ent.invoice.invoice_number if ent.invoice else "",
                "prescription_number": ent.rx_reference_code,
                "product_name": ent.brand_name,
                "generic_name": ent.generic_name,
                "classification_tier": ent.classification_tier,
                "dispensed_base_units": ent.dispensed_base_units,
                "returned_base_units": ent.returned_base_units,
                "net_units": ent.dispensed_base_units - ent.returned_base_units,
                "doctor_name": ent.doctor_name,
                "doctor_bmdc_no": ent.doctor_bmdc_no,
                "patient_token": ent.patient_token,
                "pharmacist_pcb_no": ent.approving_pharmacist_pcb_no,
                "status": ent.status,
                "sha256_hash": ent.sha256_hash,
                "dispensed_at": ent.dispensed_at.isoformat(),
            })

        return {
            "tenant_id": str(tenant_id),
            "branch_id": str(branch_id or "ALL"),
            "total_records": total_records,
            "total_dispensed_units": total_dispensed,
            "total_returned_units": total_returned,
            "net_active_units": net_active_units,
            "aware_tier_breakdown": tier_counts,
            "export_timestamp": timezone.now().isoformat(),
            "records": records_data,
        }
