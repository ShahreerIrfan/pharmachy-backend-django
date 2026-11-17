"""
backend/apps/core/pharmacist_queue_service.py

Architecture Decision: ADR-0243 (P243).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Authoritative domain service for Pharmacist-Only Review Queue:
- Restricts queue access and decisions to certified pharmacists with PCB credentials.
- Explicit lifecycle transitions: VERIFY, REJECT, REQUEST_CLARIFICATION, RESOLVE_CLARIFICATION.
- Priority queue ordering (antibiotics & high-risk medications prioritized).
- Immutable clinical decision audit trail.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple
import uuid

from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    PrescriptionRecord,
    PrescriptionReviewAction,
    PrescriptionStatus,
    PrescriptionVerificationAudit,
    StaffMember,
    Tenant,
)

logger = logging.getLogger(__name__)


class PharmacistQueueService:
    """
    Service managing the clinical pharmacist verification queue and decision actions.
    """

    ALLOWED_PHARMACIST_ROLES = {
        "STAFF_PHARMACIST",
        "SUPERADMIN",
    }

    ALLOWED_AUDIT_VIEW_ROLES = {
        "STAFF_PHARMACIST",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "AUDITOR",
        "SUPERADMIN",
    }

    @classmethod
    def assert_pharmacist_role(cls, user: Any, action_name: str = "review") -> None:
        user_role = getattr(user, "role", "")
        if user_role not in cls.ALLOWED_PHARMACIST_ROLES:
            raise PermissionDenied(
                {
                    "user": f"Role '{user_role}' is not authorized to {action_name} prescriptions in the clinical queue.",
                    "code": "UNAUTHORIZED_PHARMACIST_ACCESS",
                }
            )

    @classmethod
    def resolve_staff_member(cls, user: Any, tenant: Tenant) -> Optional[StaffMember]:
        if not hasattr(user, "pk"):
            return None
        uname = getattr(user, "username", "") or getattr(user, "email", "")
        return StaffMember.objects.filter(tenant=tenant, username=uname).first()

    @classmethod
    def get_review_queue(
        cls,
        user: Any,
        *,
        branch_id: Optional[str] = None,
        status_filter: Optional[str] = None,
    ) -> List[PrescriptionRecord]:
        """
        Retrieves active prescriptions awaiting pharmacist review.
        Enforces pharmacist role and multi-tenant scoping.
        Prioritizes prescriptions containing antibiotics or urgent regimens.
        """
        cls.assert_pharmacist_role(user, "access review queue of")

        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        if not tenant_id:
            return []

        allowed_statuses = [PrescriptionStatus.PENDING, PrescriptionStatus.CLARIFICATION_REQUESTED]
        if status_filter and status_filter.upper() in allowed_statuses:
            allowed_statuses = [status_filter.upper()]

        qs = PrescriptionRecord.objects.select_related(
            "tenant", "branch", "customer", "verified_by"
        ).prefetch_related(
            "items", "items__product", "attachments"
        ).filter(
            tenant__tenant_id=tenant_id,
            status__in=allowed_statuses,
        )

        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)

        # Prioritize prescriptions with antibiotics or urgent items first
        qs = qs.annotate(
            is_priority=Case(
                When(items__product__is_antibiotic=True, then=Value(1)),
                When(items__product__is_prescription_only=True, then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            )
        ).order_by("is_priority", "created_at").distinct()

        return list(qs)

    @classmethod
    @transaction.atomic
    def verify_prescription(
        cls,
        user: Any,
        *,
        prescription_id: str,
        pharmacist_pcb_no: str,
        notes: str = "",
    ) -> Tuple[PrescriptionRecord, PrescriptionVerificationAudit]:
        """
        Action: VERIFY — Approves prescription for dispensing.
        """
        cls.assert_pharmacist_role(user, "verify")

        clean_pcb = str(pharmacist_pcb_no or "").strip().upper()
        if not clean_pcb:
            raise ValidationError({"pharmacist_pcb_no": "Pharmacist PCB registration number is mandatory for verification."})

        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        rx = PrescriptionRecord.objects.select_for_update().filter(
            prescription_id=prescription_id,
            tenant__tenant_id=tenant_id,
        ).first()

        if not rx:
            raise ValidationError({"prescription_id": f"Prescription '{prescription_id}' not found."})

        if rx.status not in (PrescriptionStatus.PENDING, PrescriptionStatus.CLARIFICATION_REQUESTED):
            raise ValidationError(
                {"status": f"Cannot verify prescription with status '{rx.status}'. Only PENDING or CLARIFICATION_REQUESTED can be verified."}
            )

        if rx.is_expired:
            rx.status = PrescriptionStatus.EXPIRED
            rx.save(update_fields=["status", "updated_at"])
            raise ValidationError({"expiry_date": f"Cannot verify expired prescription (expired on {rx.expiry_date})."})

        staff = cls.resolve_staff_member(user, rx.tenant)
        from_status = rx.status
        rx.status = PrescriptionStatus.VERIFIED
        rx.verified_by = staff
        rx.pharmacist_pcb_no = clean_pcb
        if notes:
            rx.notes = (rx.notes + f"\nVerified: {notes}").strip()
        rx.verified_at = timezone.now()
        rx.save(update_fields=["status", "verified_by", "pharmacist_pcb_no", "notes", "verified_at", "updated_at"])

        audit = PrescriptionVerificationAudit.objects.create(
            audit_id=f"pva-{uuid.uuid4().hex[:12]}",
            tenant=rx.tenant,
            branch=rx.branch,
            prescription=rx,
            action=PrescriptionReviewAction.VERIFY,
            from_status=from_status,
            to_status=rx.status,
            pharmacist_user=user if hasattr(user, "pk") else None,
            pharmacist_staff=staff,
            pharmacist_pcb_no=clean_pcb,
            clinical_notes=str(notes or "").strip(),
        )

        logger.info("Rx %s VERIFIED by pharmacist %s (%s)", prescription_id, user, clean_pcb)
        return rx, audit

    @classmethod
    @transaction.atomic
    def reject_prescription(
        cls,
        user: Any,
        *,
        prescription_id: str,
        pharmacist_pcb_no: str,
        rejection_reason: str,
    ) -> Tuple[PrescriptionRecord, PrescriptionVerificationAudit]:
        """
        Action: REJECT — Clinically rejects prescription (illegible, unsafe dosage, contraindication).
        """
        cls.assert_pharmacist_role(user, "reject")

        clean_reason = str(rejection_reason or "").strip()
        if not clean_reason:
            raise ValidationError({"rejection_reason": "Rejection reason is mandatory."})

        clean_pcb = str(pharmacist_pcb_no or "").strip().upper()
        if not clean_pcb:
            raise ValidationError({"pharmacist_pcb_no": "Pharmacist PCB registration number is mandatory."})

        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        rx = PrescriptionRecord.objects.select_for_update().filter(
            prescription_id=prescription_id,
            tenant__tenant_id=tenant_id,
        ).first()

        if not rx:
            raise ValidationError({"prescription_id": f"Prescription '{prescription_id}' not found."})

        if rx.status in (PrescriptionStatus.DISPENSED, PrescriptionStatus.PARTIALLY_DISPENSED):
            raise ValidationError({"status": f"Cannot reject prescription with active dispenses (status: {rx.status})."})

        staff = cls.resolve_staff_member(user, rx.tenant)
        from_status = rx.status
        rx.status = PrescriptionStatus.REJECTED
        rx.verified_by = staff
        rx.pharmacist_pcb_no = clean_pcb
        rx.notes = (rx.notes + f"\nREJECTED: {clean_reason}").strip()
        rx.verified_at = timezone.now()
        rx.save(update_fields=["status", "verified_by", "pharmacist_pcb_no", "notes", "verified_at", "updated_at"])

        audit = PrescriptionVerificationAudit.objects.create(
            audit_id=f"pva-{uuid.uuid4().hex[:12]}",
            tenant=rx.tenant,
            branch=rx.branch,
            prescription=rx,
            action=PrescriptionReviewAction.REJECT,
            from_status=from_status,
            to_status=rx.status,
            pharmacist_user=user if hasattr(user, "pk") else None,
            pharmacist_staff=staff,
            pharmacist_pcb_no=clean_pcb,
            clinical_notes=clean_reason,
        )

        logger.info("Rx %s REJECTED by pharmacist %s: %s", prescription_id, clean_pcb, clean_reason)
        return rx, audit

    @classmethod
    @transaction.atomic
    def request_clarification(
        cls,
        user: Any,
        *,
        prescription_id: str,
        pharmacist_pcb_no: str,
        clarification_query: str,
    ) -> Tuple[PrescriptionRecord, PrescriptionVerificationAudit]:
        """
        Action: REQUEST_CLARIFICATION — Requests clarification from prescriber/patient before approval.
        """
        cls.assert_pharmacist_role(user, "request clarification on")

        clean_query = str(clarification_query or "").strip()
        if not clean_query:
            raise ValidationError({"clarification_query": "Clarification query is mandatory."})

        clean_pcb = str(pharmacist_pcb_no or "").strip().upper()
        if not clean_pcb:
            raise ValidationError({"pharmacist_pcb_no": "Pharmacist PCB registration number is mandatory."})

        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        rx = PrescriptionRecord.objects.select_for_update().filter(
            prescription_id=prescription_id,
            tenant__tenant_id=tenant_id,
        ).first()

        if not rx:
            raise ValidationError({"prescription_id": f"Prescription '{prescription_id}' not found."})

        if rx.status != PrescriptionStatus.PENDING:
            raise ValidationError(
                {"status": f"Cannot request clarification on prescription with status '{rx.status}'. Only PENDING prescriptions can be flagged."}
            )

        staff = cls.resolve_staff_member(user, rx.tenant)
        from_status = rx.status
        rx.status = PrescriptionStatus.CLARIFICATION_REQUESTED
        rx.notes = (rx.notes + f"\nClarification Query [{clean_pcb}]: {clean_query}").strip()
        rx.save(update_fields=["status", "notes", "updated_at"])

        audit = PrescriptionVerificationAudit.objects.create(
            audit_id=f"pva-{uuid.uuid4().hex[:12]}",
            tenant=rx.tenant,
            branch=rx.branch,
            prescription=rx,
            action=PrescriptionReviewAction.REQUEST_CLARIFICATION,
            from_status=from_status,
            to_status=rx.status,
            pharmacist_user=user if hasattr(user, "pk") else None,
            pharmacist_staff=staff,
            pharmacist_pcb_no=clean_pcb,
            clarification_query=clean_query,
        )

        logger.info("Rx %s CLARIFICATION_REQUESTED by %s: %s", prescription_id, clean_pcb, clean_query)
        return rx, audit

    @classmethod
    @transaction.atomic
    def resolve_clarification(
        cls,
        user: Any,
        *,
        prescription_id: str,
        clarification_response: str,
    ) -> Tuple[PrescriptionRecord, PrescriptionVerificationAudit]:
        """
        Action: RESOLVE_CLARIFICATION — Records response from doctor/patient, moving status back to PENDING.
        """
        clean_resp = str(clarification_response or "").strip()
        if not clean_resp:
            raise ValidationError({"clarification_response": "Clarification response is mandatory."})

        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        rx = PrescriptionRecord.objects.select_for_update().filter(
            prescription_id=prescription_id,
            tenant__tenant_id=tenant_id,
        ).first()

        if not rx:
            raise ValidationError({"prescription_id": f"Prescription '{prescription_id}' not found."})

        if rx.status != PrescriptionStatus.CLARIFICATION_REQUESTED:
            raise ValidationError(
                {"status": f"Prescription is not in CLARIFICATION_REQUESTED status (current: {rx.status})."}
            )

        staff = cls.resolve_staff_member(user, rx.tenant)
        from_status = rx.status
        rx.status = PrescriptionStatus.PENDING
        rx.notes = (rx.notes + f"\nClarification Resolved: {clean_resp}").strip()
        rx.save(update_fields=["status", "notes", "updated_at"])

        audit = PrescriptionVerificationAudit.objects.create(
            audit_id=f"pva-{uuid.uuid4().hex[:12]}",
            tenant=rx.tenant,
            branch=rx.branch,
            prescription=rx,
            action=PrescriptionReviewAction.RESOLVE_CLARIFICATION,
            from_status=from_status,
            to_status=rx.status,
            pharmacist_user=user if hasattr(user, "pk") else None,
            pharmacist_staff=staff,
            clarification_response=clean_resp,
        )

        logger.info("Rx %s clarification resolved: %s", prescription_id, clean_resp)
        return rx, audit

    @classmethod
    def get_prescription_audit_trail(
        cls,
        user: Any,
        *,
        prescription_id: str,
    ) -> List[PrescriptionVerificationAudit]:
        """
        Retrieves chronological audit trail for clinical verification actions.
        """
        user_role = getattr(user, "role", "")
        if user_role and user_role not in cls.ALLOWED_AUDIT_VIEW_ROLES:
            raise PermissionDenied({"user": f"Role '{user_role}' is not authorized to view clinical audit trails."})

        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        return list(
            PrescriptionVerificationAudit.objects.select_related(
                "pharmacist_staff", "pharmacist_user"
            ).filter(
                prescription_id=prescription_id,
                tenant__tenant_id=tenant_id,
            ).order_by("created_at")
        )

