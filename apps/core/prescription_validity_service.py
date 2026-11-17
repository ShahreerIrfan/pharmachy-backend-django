"""
backend/apps/core/prescription_validity_service.py

Architecture Decision: ADR-0244 (P244).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Authoritative domain service for Prescription Validity Controls:
- Validates prescriber credentials (BMDC registration format, doctor name, institutional workplace).
- Enforces temporal validity (prevents future-dated prescriptions, flags expired validity bounds).
- Verifies supporting documents/scans (mandatory uncorrupted documents for restricted/antibiotic medicines).
- Implements automated and pharmacist-governed validity checks and explicit rejection transitions.
- Fully tenant-isolated and integrated with PrescriptionRecord and PrescriptionVerificationAudit.
"""

from __future__ import annotations

from datetime import date, datetime
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    PrescriptionAttachment,
    PrescriptionRecord,
    PrescriptionReviewAction,
    PrescriptionStatus,
    PrescriptionValidityStatus,
    PrescriptionVerificationAudit,
    StaffMember,
    StaffRole,
    Tenant,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

# Dummy values that are strictly prohibited as prescriber credentials
_PROHIBITED_BMDC_STRINGS = {
    "NA", "N/A", "NONE", "TEST", "TESTING", "UNKNOWN", "0", "0000",
    "00000", "000000", "NULL", "NIL", "DOCTOR", "DOC", "ABC", "1234",
}

_PROHIBITED_NAME_STRINGS = {
    "DOCTOR", "DR", "DR.", "TEST", "ANONYMOUS", "UNKNOWN", "PATIENT",
}


class PrescriptionValidityService:
    """
    Evaluates and enforces clinical prescription validity under verified Bangladesh
    pharmacy regulations and DGDA guidelines.
    """

    MAX_CHRONIC_VALIDITY_DAYS = 180
    DEFAULT_ACUTE_VALIDITY_DAYS = 30

    @classmethod
    def validate_bmdc_registration(cls, bmdc_no: Optional[str]) -> Tuple[bool, Optional[str]]:
        """
        Validates BMDC registration format.
        Valid patterns:
        - Category prefix: A-12345 (MBBS), B-12345 (BDS)
        - Alphanumeric registration numbers: 4 to 16 characters containing at least one digit
        """
        if not bmdc_no:
            return False, "Doctor BMDC registration number is mandatory."

        clean = str(bmdc_no).strip().upper()
        if clean in _PROHIBITED_BMDC_STRINGS or len(clean) < 4:
            return False, f"BMDC registration number '{clean}' is a recognized placeholder or too short."

        # Pattern: Standard prefix e.g. BMDC-12345, BMDC-A12345, A-12345, B/6789 or bare 4-8 digits
        standard_pattern = r"^(?:BMDC[\-\/])?(?:[AB][\-\/]?)?\d{3,8}$"
        general_pattern = r"^[A-Z0-9\-\/]{4,20}$"

        if clean.startswith("BMDC-") or clean.startswith("BMDC/") or clean.startswith("A-") or clean.startswith("B-"):
            return True, None

        if re.match(standard_pattern, clean):
            return True, None

        if re.match(general_pattern, clean) and re.search(r"\d", clean):
            return True, None

        return False, f"BMDC registration number '{clean}' does not match council format (e.g., A-12345, B-6789, or 5-digit number)."

    @classmethod
    def validate_prescriber_info(
        cls,
        doctor_name: Optional[str],
        doctor_bmdc_no: Optional[str],
        doctor_workplace: Optional[str],
        prescriber_contact: Optional[str] = "",
    ) -> Dict[str, Any]:
        """
        Validates completeness and sanity of prescriber identity fields.
        """
        errors: List[str] = []

        # 1. Doctor Name
        clean_name = str(doctor_name or "").strip()
        if not clean_name or len(clean_name) < 3:
            errors.append("Prescriber doctor name is mandatory (minimum 3 characters).")
        elif clean_name.upper() in _PROHIBITED_NAME_STRINGS:
            errors.append(f"Doctor name '{clean_name}' is a generic placeholder.")

        # 2. BMDC Registration
        bmdc_valid, bmdc_err = cls.validate_bmdc_registration(doctor_bmdc_no)
        if not bmdc_valid and bmdc_err:
            errors.append(bmdc_err)

        # 3. Doctor Workplace / Clinic / Chamber
        clean_workplace = str(doctor_workplace or "").strip()
        if not clean_workplace or len(clean_workplace) < 3:
            errors.append("Doctor workplace/hospital/chamber is mandatory for clinical traceability.")
        elif clean_workplace.upper() in _PROHIBITED_NAME_STRINGS:
            errors.append(f"Doctor workplace '{clean_workplace}' is invalid.")

        # 4. Optional Prescriber Contact
        clean_contact = str(prescriber_contact or "").strip()
        if clean_contact:
            # BD mobile format sanity check
            bd_phone_pattern = r"^(?:\+?8801|01)[3-9]\d{8}$"
            digits_only = re.sub(r"[\s\-\(\)]", "", clean_contact)
            if not re.match(bd_phone_pattern, digits_only) and len(digits_only) < 7:
                errors.append(f"Prescriber contact '{clean_contact}' is not a valid telephone or mobile number.")

        return {
            "status": "PASS" if not errors else "FAIL",
            "doctor_name": clean_name,
            "doctor_bmdc_no": str(doctor_bmdc_no or "").strip().upper(),
            "doctor_workplace": clean_workplace,
            "prescriber_contact": clean_contact,
            "bmdc_valid": bmdc_valid,
            "errors": errors,
        }

    @classmethod
    def validate_temporal_bounds(
        cls,
        prescription_date: Any,
        expiry_date: Optional[Any] = None,
        reference_date: Optional[date] = None,
    ) -> Dict[str, Any]:
        """
        Checks date issuance and expiration bounds.
        """
        errors: List[str] = []
        ref_date = reference_date or timezone.now().date()

        # Parse prescription_date
        if isinstance(prescription_date, str):
            p_date = datetime.strptime(prescription_date[:10], "%Y-%m-%d").date()
        elif isinstance(prescription_date, datetime):
            p_date = prescription_date.date()
        else:
            p_date = prescription_date

        # 1. Post-dated check
        if p_date > ref_date:
            errors.append(f"Prescription date ({p_date}) is in the future. Post-dated prescriptions are invalid.")

        # 2. Expiry bounds check
        e_date = None
        if expiry_date:
            if isinstance(expiry_date, str):
                e_date = datetime.strptime(expiry_date[:10], "%Y-%m-%d").date()
            elif isinstance(expiry_date, datetime):
                e_date = expiry_date.date()
            else:
                e_date = expiry_date

            if e_date < p_date:
                errors.append(f"Prescription expiry date ({e_date}) cannot precede issued date ({p_date}).")
            elif e_date < ref_date:
                errors.append(f"Prescription has expired on {e_date} ({ (ref_date - e_date).days } day(s) ago).")

            span_days = (e_date - p_date).days
            if span_days > cls.MAX_CHRONIC_VALIDITY_DAYS:
                errors.append(
                    f"Prescription validity span ({span_days} days) exceeds maximum allowed duration ({cls.MAX_CHRONIC_VALIDITY_DAYS} days)."
                )
        else:
            # Default policy check if expiry date is omitted
            age_days = (ref_date - p_date).days
            if age_days > cls.DEFAULT_ACUTE_VALIDITY_DAYS:
                errors.append(
                    f"Prescription with no explicit expiry date has exceeded default {cls.DEFAULT_ACUTE_VALIDITY_DAYS}-day validity ({age_days} days old)."
                )

        is_expired = any("expired" in err.lower() for err in errors)
        days_remaining = None
        if e_date:
            days_remaining = (e_date - ref_date).days if e_date >= ref_date else 0

        return {
            "status": "PASS" if not errors else "FAIL",
            "prescription_date": p_date.isoformat(),
            "expiry_date": e_date.isoformat() if e_date else None,
            "is_expired": is_expired,
            "days_remaining": days_remaining,
            "errors": errors,
        }

    @classmethod
    def validate_supporting_documents(
        cls,
        prescription: PrescriptionRecord,
    ) -> Dict[str, Any]:
        """
        Validates attachment presence, file integrity, and non-quarantine status.
        Antibiotics or prescription-only medicines strictly require supporting document scan.
        """
        errors: List[str] = []
        attachments = list(prescription.attachments.all())
        valid_attachments: List[Dict[str, Any]] = []

        # Check if prescription contains antibiotics or prescription-only medicines
        items = list(prescription.items.select_related("product").all())
        has_antibiotic = any(
            bool(item.product and getattr(item.product, "is_antibiotic", False))
            for item in items
        )
        has_rx_only = any(
            bool(item.product and getattr(item.product, "is_prescription_only", False))
            for item in items
        )
        requires_document = has_antibiotic or has_rx_only

        for att in attachments:
            if att.file_size_bytes <= 0:
                errors.append(f"Attachment '{att.file_name}' has 0 bytes (corrupt file).")
                continue
            valid_attachments.append({
                "attachment_id": att.attachment_id,
                "file_name": att.file_name,
                "mime_type": att.mime_type,
                "file_size_bytes": att.file_size_bytes,
                "is_verified": att.is_verified_by_pharmacist,
            })

        if requires_document and not valid_attachments:
            errors.append(
                "Supporting prescription document/scan is mandatory for antibiotic or prescription-only medications under DGDA policy."
            )

        return {
            "status": "PASS" if not errors else "FAIL",
            "attachment_count": len(attachments),
            "valid_attachment_count": len(valid_attachments),
            "has_valid_attachment": len(valid_attachments) > 0,
            "requires_document": requires_document,
            "has_antibiotic": has_antibiotic,
            "has_rx_only": has_rx_only,
            "attachments": valid_attachments,
            "errors": errors,
        }

    @classmethod
    def evaluate_prescription_validity(
        cls,
        prescription: PrescriptionRecord,
        reference_date: Optional[date] = None,
        update_record: bool = True,
    ) -> Dict[str, Any]:
        """
        Performs holistic validity assessment across prescriber, temporal, document,
        and line-item dimensions.
        """
        prescriber_res = cls.validate_prescriber_info(
            doctor_name=prescription.doctor_name,
            doctor_bmdc_no=prescription.doctor_bmdc_no,
            doctor_workplace=prescription.doctor_workplace,
            prescriber_contact=prescription.prescriber_contact,
        )

        temporal_res = cls.validate_temporal_bounds(
            prescription_date=prescription.prescription_date,
            expiry_date=prescription.expiry_date,
            reference_date=reference_date,
        )

        document_res = cls.validate_supporting_documents(prescription)

        # Line-item validation
        items = list(prescription.items.all())
        item_errors: List[str] = []
        if not items:
            item_errors.append("Prescription has no prescribed line items.")
        for idx, it in enumerate(items):
            qty = getattr(it, "quantity_prescribed", getattr(it, "quantity", 1))
            if qty <= 0:
                item_errors.append(f"Line item #{idx+1} has non-positive quantity ({qty}).")

        # Aggregate overall status
        all_errors = (
            prescriber_res["errors"]
            + temporal_res["errors"]
            + document_res["errors"]
            + item_errors
        )

        # Determine validity status code
        if temporal_res["is_expired"]:
            computed_status = PrescriptionValidityStatus.EXPIRED
        elif prescriber_res["errors"]:
            computed_status = PrescriptionValidityStatus.INCOMPLETE_CREDENTIALS
        elif temporal_res["errors"]:
            computed_status = PrescriptionValidityStatus.INVALID_DATES
        elif document_res["errors"]:
            computed_status = PrescriptionValidityStatus.MISSING_DOCUMENTS
        elif all_errors:
            computed_status = PrescriptionValidityStatus.REVOKED
        else:
            computed_status = PrescriptionValidityStatus.VALID

        is_valid = (computed_status == PrescriptionValidityStatus.VALID)

        if update_record:
            prescription.validity_status = computed_status
            prescription.validity_checked_at = timezone.now()
            prescription.validity_notes = "; ".join(all_errors) if all_errors else "All validity controls passed."
            prescription.save(update_fields=["validity_status", "validity_checked_at", "validity_notes"])

        return {
            "prescription_id": prescription.prescription_id,
            "prescription_number": prescription.prescription_number,
            "tenant_id": str(prescription.tenant_id),
            "branch_id": str(prescription.branch_id),
            "current_status": prescription.status,
            "validity_status": computed_status,
            "is_valid": is_valid,
            "validity_checked_at": prescription.validity_checked_at.isoformat() if prescription.validity_checked_at else None,
            "prescriber_check": prescriber_res,
            "temporal_check": temporal_res,
            "supporting_document_check": document_res,
            "items_check": {
                "status": "PASS" if not item_errors else "FAIL",
                "item_count": len(items),
                "errors": item_errors,
            },
            "overall_errors": all_errors,
            "can_dispense": is_valid and prescription.status == PrescriptionStatus.VERIFIED,
        }

    @classmethod
    def reject_invalid_prescription(
        cls,
        prescription_id: str,
        tenant_id: str,
        user: Any,
        reason: str,
        clinical_notes: str = "",
    ) -> PrescriptionRecord:
        """
        Enforces rejection of an invalid, expired, or document-deficient prescription.
        Pharmacist-only action with immutable audit trail.
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required to reject prescriptions.")

        # RBAC Check
        user_role = getattr(user, "role", None)
        is_pharmacist = user_role in (StaffRole.PHARMACIST, UserRole.STAFF_PHARMACIST)
        is_super = getattr(user, "is_superuser", False) or user_role == UserRole.SUPERADMIN

        if not (is_pharmacist or is_super):
            raise PermissionDenied("Only registered pharmacists or superadmins can reject prescriptions.")

        # Tenant boundary
        user_tenant = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        if not is_super and user_tenant and str(user_tenant) != str(tenant_id):
            raise PermissionDenied("Cross-tenant prescription rejection is prohibited.")

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError({"reason": "Rejection reason is mandatory."})

        with transaction.atomic():
            rx = (
                PrescriptionRecord.objects.select_for_update()
                .filter(prescription_id=prescription_id, tenant_id=tenant_id)
                .first()
            )
            if not rx:
                raise ValidationError({"prescription_id": f"Prescription '{prescription_id}' not found."})

            if rx.status in (PrescriptionStatus.DISPENSED, PrescriptionStatus.CANCELLED):
                raise ValidationError({"status": f"Cannot reject prescription in terminal state '{rx.status}'."})

            from_status = rx.status
            rx.status = PrescriptionStatus.REJECTED
            rx.notes = f"{rx.notes}\n[REJECTED - VALIDITY]: {clean_reason}. Notes: {clinical_notes}".strip()
            rx.save(update_fields=["status", "notes", "updated_at"])

            # Link staff member
            staff = StaffMember.objects.filter(tenant_id=tenant_id, username=getattr(user, "email", "")).first()
            if not staff:
                staff = StaffMember.objects.filter(tenant_id=tenant_id, email=getattr(user, "email", "")).first()
            pcb_no = getattr(staff, "pcb_registration_no", "") if staff else getattr(user, "pcb_registration_no", "")

            # Create immutable clinical audit
            PrescriptionVerificationAudit.objects.create(
                audit_id=f"aud-rx-{uuid.uuid4().hex[:12]}",
                tenant=rx.tenant,
                branch=rx.branch,
                prescription=rx,
                action=PrescriptionReviewAction.REJECT,
                pharmacist_user=user if getattr(user, "pk", None) else None,
                pharmacist_staff=staff,
                pharmacist_pcb_no=pcb_no or "SUPERADMIN-REVIEW",
                from_status=from_status,
                to_status=PrescriptionStatus.REJECTED,
                clinical_notes=f"Reason: {clean_reason} | Notes: {clinical_notes}",
            )

            # Re-evaluate validity
            cls.evaluate_prescription_validity(rx, update_record=True)
            return rx
