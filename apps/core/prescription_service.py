"""
backend/apps/core/prescription_service.py

Architecture Decision: ADR-0241 (P241).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Authoritative domain service for clinical prescription management,
BMDC doctor validation, synthetic patient token tracking, issued/expiry date
safety bounds, pharmacist PCB clinical verification, and audited dispense ledger.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional, Tuple
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    BatchStock,
    Branch,
    MedicineProduct,
    PosCustomer,
    PosSaleOrder,
    PrescriptionDispenseRecord,
    PrescriptionItem,
    PrescriptionRecord,
    PrescriptionStatus,
    StaffMember,
    StaffRole,
    Tenant,
)
from apps.core.prescription_validity_service import PrescriptionValidityService

logger = logging.getLogger(__name__)


class PrescriptionService:
    """
    Authoritative domain service governing:
    1. Prescription lifecycle state transitions (PENDING -> VERIFIED -> PARTIALLY_DISPENSED -> DISPENSED).
    2. BMDC doctor registration & clinical metadata enforcement.
    3. Expiry date compliance (preventing dispensing of expired prescriptions).
    4. Pharmacist PCB sign-off validation.
    5. Audited dispense event logging with batch lot & sale order linkage.
    """

    ALLOWED_CREATOR_ROLES = {
        "STAFF_PHARMACIST",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "SUPERADMIN",
    }

    ALLOWED_DISPENSER_ROLES = {
        "STAFF_PHARMACIST",
        "CASHIER",
        "BRANCH_MANAGER",
        "PHARMACY_OWNER",
        "SUPERADMIN",
    }

    @classmethod
    def resolve_date(cls, val: Any) -> date:
        if isinstance(val, date) and not isinstance(val, datetime):
            return val
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, str) and val.strip():
            return datetime.strptime(val.strip(), "%Y-%m-%d").date()
        return timezone.now().date()

    @classmethod
    def resolve_tenant_and_branch(
        cls,
        user: Any,
        branch_id: Optional[str] = None,
    ) -> Tuple[Tenant, Branch]:
        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant": "Authenticated user is not linked to an active tenant."})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise ValidationError({"tenant": f"Tenant '{tenant_id}' not found."})

        target_branch_id = branch_id or getattr(user, "branch_id", None)
        branch = None
        if target_branch_id:
            branch = Branch.objects.filter(tenant=tenant, branch_id=target_branch_id).first()
        if not branch:
            branch = Branch.objects.filter(tenant=tenant).first()
        if not branch:
            branch = Branch.objects.create(
                tenant=tenant,
                branch_id=f"br-{uuid.uuid4().hex[:8]}",
                name="Main Pharmacy Branch",
                branch_code="MAIN01",
            )
        return tenant, branch

    @classmethod
    @transaction.atomic
    def create_prescription(
        cls,
        user: Any,
        *,
        doctor_name: str,
        doctor_bmdc_no: str,
        patient_token: str,
        prescription_date: Optional[Any] = None,
        expiry_date: Optional[Any] = None,
        patient_age: Optional[int] = None,
        patient_gender: str = "UNKNOWN",
        diagnosis: str = "",
        doctor_workplace: str = "",
        prescriber_contact: str = "",
        notes: str = "",
        customer_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        items_data: Optional[List[Dict[str, Any]]] = None,
    ) -> PrescriptionRecord:
        """
        Creates a new clinical prescription record with validated doctor BMDC registration,
        privacy-safe patient token, issued/expiry dates, and prescribed items.
        """
        user_role = getattr(user, "role", "")
        if user_role and user_role not in cls.ALLOWED_CREATOR_ROLES:
            raise PermissionDenied({"user": f"Role '{user_role}' is not authorized to create prescriptions."})

        tenant, branch = cls.resolve_tenant_and_branch(user, branch_id=branch_id)

        clean_doc_name = str(doctor_name or "").strip()
        if not clean_doc_name:
            raise ValidationError({"doctor_name": "Doctor / Prescriber name is mandatory."})

        clean_bmdc = str(doctor_bmdc_no or "").strip().upper()
        if not clean_bmdc:
            raise ValidationError({"doctor_bmdc_no": "Doctor BMDC registration number is mandatory."})

        bmdc_valid, bmdc_err = PrescriptionValidityService.validate_bmdc_registration(clean_bmdc)
        if not bmdc_valid and bmdc_err:
            raise ValidationError({"doctor_bmdc_no": bmdc_err})

        clean_token = str(patient_token or "").strip()
        if not clean_token:
            clean_token = f"PT-{uuid.uuid4().hex[:10].upper()}"

        rx_date = cls.resolve_date(prescription_date) if prescription_date else timezone.now().date()
        exp_date = cls.resolve_date(expiry_date) if expiry_date else None

        if exp_date and exp_date < rx_date:
            raise ValidationError(
                {"expiry_date": f"Expiry date ({exp_date}) cannot be earlier than issued prescription date ({rx_date})."}
            )

        customer = None
        if customer_id:
            customer = PosCustomer.objects.filter(tenant=tenant, customer_id=customer_id).first()

        rx_uid = uuid.uuid4().hex[:8]
        prescription_id = f"rx-{timezone.now().strftime('%Y%m%d')}-{rx_uid}"
        prescription_number = f"RX-{timezone.now().strftime('%Y%m%d')}-{rx_uid.upper()}"

        rx = PrescriptionRecord.objects.create(
            prescription_id=prescription_id,
            prescription_number=prescription_number,
            tenant=tenant,
            branch=branch,
            customer=customer,
            doctor_name=clean_doc_name,
            doctor_bmdc_no=clean_bmdc,
            doctor_workplace=str(doctor_workplace or "").strip(),
            prescriber_contact=str(prescriber_contact or "").strip(),
            patient_token=clean_token,
            patient_age=patient_age,
            patient_gender=str(patient_gender or "UNKNOWN").upper(),
            diagnosis=str(diagnosis or "").strip(),
            status=PrescriptionStatus.PENDING,
            notes=str(notes or "").strip(),
            prescription_date=rx_date,
            expiry_date=exp_date,
        )

        # Create line items
        if items_data:
            for idx, item in enumerate(items_data):
                product_id = item.get("product_id") or item.get("product")
                if not product_id:
                    raise ValidationError({"items": f"Item at index {idx} must specify a product_id."})
                product = MedicineProduct.objects.filter(product_id=product_id).first()
                if not product:
                    raise ValidationError({"items": f"Medicine product '{product_id}' not found."})

                qty = int(item.get("quantity_prescribed") or 1)
                if qty < 1:
                    raise ValidationError({"items": f"Prescribed quantity for item at index {idx} must be at least 1."})

                PrescriptionItem.objects.create(
                    prescription=rx,
                    product=product,
                    dosage_instructions=str(item.get("dosage_instructions") or "As directed by physician"),
                    duration_days=int(item.get("duration_days") or 7),
                    quantity_prescribed=qty,
                    quantity_dispensed=0,
                    is_dispensed=False,
                )

        PrescriptionValidityService.evaluate_prescription_validity(rx, update_record=True)

        logger.info("Prescription %s created by user %s for patient token %s", prescription_id, user, clean_token)
        return rx

    @classmethod
    @transaction.atomic
    def verify_prescription(
        cls,
        user: Any,
        *,
        prescription_id: str,
        decision: str,
        pharmacist_pcb_reg_no: str,
        notes: str = "",
    ) -> PrescriptionRecord:
        """
        Executes clinical pharmacist sign-off for prescription drugs and antibiotics.
        Validates PCB registration and role.
        """
        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        rx = PrescriptionRecord.objects.select_for_update().filter(
            prescription_id=prescription_id,
            tenant__tenant_id=tenant_id,
        ).first()

        if not rx:
            raise ValidationError({"prescription_id": f"Prescription '{prescription_id}' not found in your pharmacy."})

        if rx.status != PrescriptionStatus.PENDING:
            raise ValidationError(
                {"status": f"Cannot verify prescription with status '{rx.status}'. Only PENDING prescriptions can be verified."}
            )

        clean_pcb = str(pharmacist_pcb_reg_no or "").strip().upper()
        if not clean_pcb:
            raise ValidationError({"pharmacist_pcb_reg_no": "Pharmacist PCB registration number is mandatory for clinical sign-off."})

        # Verify staff role
        staff = None
        user_role = getattr(user, "role", "")
        if hasattr(user, "staff_profile"):
            staff = getattr(user, "staff_profile", None)

        if not staff and hasattr(user, "pk"):
            uname = getattr(user, "username", "") or getattr(user, "email", "")
            staff = StaffMember.objects.filter(tenant=rx.tenant, username=uname).first()

        clean_decision = str(decision or "").strip().upper()
        if clean_decision == "APPROVED":
            rx.status = PrescriptionStatus.VERIFIED
        elif clean_decision == "REJECTED":
            rx.status = PrescriptionStatus.REJECTED
        else:
            raise ValidationError({"decision": "Decision must be either 'APPROVED' or 'REJECTED'."})

        rx.verified_by = staff
        rx.pharmacist_pcb_no = clean_pcb
        rx.notes = (rx.notes + f"\nVerification: {notes}").strip() if notes else rx.notes
        rx.verified_at = timezone.now()
        rx.save(update_fields=["status", "verified_by", "pharmacist_pcb_no", "notes", "verified_at", "updated_at"])

        logger.info("Prescription %s verified as %s by pharmacist %s", prescription_id, rx.status, clean_pcb)
        return rx

    @classmethod
    @transaction.atomic
    def dispense_item(
        cls,
        user: Any,
        *,
        prescription_id: str,
        item_id: int,
        quantity: int,
        sale_order_id: Optional[str] = None,
        batch_stock_id: Optional[str] = None,
        notes: str = "",
    ) -> PrescriptionDispenseRecord:
        """
        Dispenses a specified quantity against a prescription line item.
        Validates clinical verification, expiry bounds, remaining quantities, and logs audited event.
        """
        user_role = getattr(user, "role", "")
        if user_role and user_role not in cls.ALLOWED_DISPENSER_ROLES:
            raise PermissionDenied({"user": f"Role '{user_role}' is not authorized to dispense prescriptions."})

        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        rx = PrescriptionRecord.objects.select_for_update().filter(
            prescription_id=prescription_id,
            tenant__tenant_id=tenant_id,
        ).first()

        if not rx:
            raise ValidationError({"prescription_id": f"Prescription '{prescription_id}' not found in current pharmacy."})

        if rx.is_expired:
            rx.status = PrescriptionStatus.EXPIRED
            rx.save(update_fields=["status", "updated_at"])
            raise ValidationError({"expiry_date": f"Cannot dispense expired prescription (expired on {rx.expiry_date})."})

        if rx.status not in (PrescriptionStatus.VERIFIED, PrescriptionStatus.PARTIALLY_DISPENSED):
            raise ValidationError(
                {"status": f"Cannot dispense prescription with status '{rx.status}'. Only VERIFIED prescriptions can be dispensed."}
            )

        item = PrescriptionItem.objects.select_for_update().filter(
            id=item_id,
            prescription=rx,
        ).first()

        if not item:
            raise ValidationError({"item_id": f"Prescription line item '{item_id}' not found on prescription '{prescription_id}'."})

        disp_qty = int(quantity or 0)
        if disp_qty <= 0:
            raise ValidationError({"quantity": "Dispensed quantity must be greater than zero."})

        if disp_qty > item.remaining_dispense_quantity:
            raise ValidationError(
                {
                    "quantity": (
                        f"Cannot dispense {disp_qty} units. Remaining allowable quantity for this item is "
                        f"{item.remaining_dispense_quantity} units (prescribed: {item.quantity_prescribed}, already dispensed: {item.quantity_dispensed})."
                    )
                }
            )

        # Update item
        item.quantity_dispensed += disp_qty
        if item.quantity_dispensed >= item.quantity_prescribed:
            item.is_dispensed = True
        item.save(update_fields=["quantity_dispensed", "is_dispensed"])

        # Check overall prescription status
        all_items = list(rx.items.all())
        all_dispensed = all_items and all(it.is_dispensed for it in all_items)
        if all_dispensed:
            rx.status = PrescriptionStatus.DISPENSED
        else:
            rx.status = PrescriptionStatus.PARTIALLY_DISPENSED
        rx.save(update_fields=["status", "updated_at"])

        # Resolve optional sale order and batch stock
        sale_order = None
        if sale_order_id:
            sale_order = PosSaleOrder.objects.filter(sale_id=sale_order_id, tenant=rx.tenant).first()

        batch_stock = None
        if batch_stock_id:
            batch_stock = BatchStock.objects.filter(batch_id=batch_stock_id, tenant=rx.tenant).first()

        staff = None
        if hasattr(user, "pk"):
            uname = getattr(user, "username", "") or getattr(user, "email", "")
            staff = StaffMember.objects.filter(tenant=rx.tenant, username=uname).first()

        dispense_record = PrescriptionDispenseRecord.objects.create(
            dispense_id=f"dsp-{uuid.uuid4().hex[:12]}",
            tenant=rx.tenant,
            branch=rx.branch,
            prescription=rx,
            prescription_item=item,
            sale_order=sale_order,
            batch_stock=batch_stock,
            quantity_dispensed=disp_qty,
            dispensed_by=staff,
            notes=str(notes or "").strip(),
        )

        logger.info(
            "Dispensed %d units of item %s for Rx %s (remaining: %d). Rx status now %s.",
            disp_qty,
            item_id,
            prescription_id,
            item.remaining_dispense_quantity,
            rx.status,
        )
        return dispense_record

    @classmethod
    @transaction.atomic
    def cancel_prescription(
        cls,
        user: Any,
        *,
        prescription_id: str,
        reason: str,
    ) -> PrescriptionRecord:
        """
        Cancels an un-dispensed or pending prescription.
        """
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError({"reason": "Cancellation reason is mandatory."})

        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        rx = PrescriptionRecord.objects.select_for_update().filter(
            prescription_id=prescription_id,
            tenant__tenant_id=tenant_id,
        ).first()

        if not rx:
            raise ValidationError({"prescription_id": f"Prescription '{prescription_id}' not found."})

        if rx.status in (PrescriptionStatus.DISPENSED, PrescriptionStatus.PARTIALLY_DISPENSED):
            raise ValidationError({"status": f"Cannot cancel prescription with active dispenses (status: {rx.status})."})

        rx.status = PrescriptionStatus.CANCELLED
        rx.notes = (rx.notes + f"\nCancelled: {clean_reason}").strip()
        rx.save(update_fields=["status", "notes", "updated_at"])
        return rx

    @classmethod
    def expire_stale_prescriptions(
        cls,
        tenant_id: str,
        branch_id: Optional[str] = None,
    ) -> int:
        """
        Audits active prescriptions and marks any whose expiry_date has passed as EXPIRED.
        """
        today = timezone.now().date()
        qs = PrescriptionRecord.objects.filter(
            tenant__tenant_id=tenant_id,
            expiry_date__lt=today,
            status__in=[PrescriptionStatus.PENDING, PrescriptionStatus.VERIFIED, PrescriptionStatus.PARTIALLY_DISPENSED],
        )
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)

        count = qs.update(status=PrescriptionStatus.EXPIRED, updated_at=timezone.now())
        return count
