"""
backend/apps/core/pcb_registration_service.py

Architecture Decision: ADR-0252 (P252).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Coordinates Pharmacy Council of Bangladesh (PCB) personnel registration tracking,
grade verification (A/B/C), statutory validity checks, branch assignment,
and strict confidential evidence access restrictions under DGDA Good Pharmacy Practice Guidelines (2024).
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    PcbEvidenceAccessLevel,
    PcbGrade,
    PcbRegistrationStatus,
    PharmacyPersonnelPcbRegistration,
    StaffMember,
    Tenant,
)
from apps.core.pos_domain_service import PosDomainService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.compliance.pcb")


class PcbRegistrationService:
    """
    Business service enforcing PCB credential governance, validity checks,
    and role-based evidence access restrictions.
    """

    ALLOWED_MANAGE_ROLES = {
        UserRole.SUPERADMIN,
        UserRole.PHARMACY_OWNER,
        UserRole.BRANCH_MANAGER,
        "SUPERADMIN",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
    }

    ALLOWED_READ_ROLES = {
        UserRole.SUPERADMIN,
        UserRole.PHARMACY_OWNER,
        UserRole.BRANCH_MANAGER,
        UserRole.STAFF_PHARMACIST,
        UserRole.CASHIER,
        UserRole.AUDITOR,
        UserRole.FINANCE_OFFICER,
        "SUPERADMIN",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "STAFF_PHARMACIST",
        "CASHIER",
        "AUDITOR",
        "FINANCE_OFFICER",
    }

    @classmethod
    def serialize_registration(
        cls,
        reg: PharmacyPersonnelPcbRegistration,
        user: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Serializes PCB registration record to contract JSON dict.
        Enforces evidence redaction if user lacks authorized clearance.
        """
        live_status = reg.evaluate_live_status()
        days_left = reg.days_until_expiry()
        can_view_evidence = reg.can_view_evidence(user) if user else False

        evidence_uri = reg.evidence_document_uri if can_view_evidence else "[REDACTED_CONFIDENTIAL_EVIDENCE]"
        evidence_sha = reg.evidence_sha256 if can_view_evidence else "[REDACTED]"

        return {
            "registration_id": reg.registration_id,
            "tenant_id": reg.tenant_id,
            "branch_id": reg.branch_id,
            "branch_name": reg.branch.name if hasattr(reg, "branch") and reg.branch else "",
            "staff_member_id": reg.staff_member_id,
            "staff_member_name": reg.staff_member.full_name if hasattr(reg, "staff_member") and reg.staff_member else "",
            "registered_name": reg.registered_name,
            "registration_no": reg.registration_no,
            "grade": reg.grade,
            "grade_display": reg.get_grade_display(),
            "council_issue_date": reg.council_issue_date.isoformat() if reg.council_issue_date else None,
            "valid_until": reg.valid_until.isoformat() if reg.valid_until else None,
            "renewal_submitted_at": reg.renewal_submitted_at.isoformat() if reg.renewal_submitted_at else None,
            "status": live_status,
            "days_until_expiry": days_left,
            "is_valid_for_clinical": reg.is_active_for_clinical_practice(),
            "is_verified": reg.is_verified,
            "verification_notes": reg.verification_notes,
            "evidence_access_level": reg.evidence_access_level,
            "evidence_accessible": can_view_evidence,
            "evidence_document_uri": evidence_uri,
            "evidence_file_name": reg.evidence_file_name if can_view_evidence else None,
            "evidence_mime_type": reg.evidence_mime_type if can_view_evidence else None,
            "evidence_sha256": evidence_sha,
            "sha256_hash": reg.sha256_hash,
            "is_archived": reg.is_archived,
            "created_at": reg.created_at.isoformat() if reg.created_at else None,
            "updated_at": reg.updated_at.isoformat() if reg.updated_at else None,
        }

    @classmethod
    def list_registrations(
        cls,
        user: Any,
        branch_id: Optional[str] = None,
        grade: Optional[str] = None,
        status_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists PCB registrations within user's tenant scope with optional filtering."""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )

        qs = PharmacyPersonnelPcbRegistration.objects.filter(
            tenant=tenant, is_archived=False
        ).select_related("branch", "staff_member")

        user_role = str(getattr(user, "role", "")).upper()
        if user_role == "BRANCH_MANAGER":
            mgr_branch_id = getattr(user, "branch_id", None) or (
                user.branch.branch_id if getattr(user, "branch", None) else None
            )
            if mgr_branch_id:
                qs = qs.filter(branch_id=mgr_branch_id)

        if branch_id:
            qs = qs.filter(branch_id=str(branch_id).strip())
        if grade:
            qs = qs.filter(grade=str(grade).strip().upper())
        if status_filter:
            qs = qs.filter(status=str(status_filter).strip().upper())

        return [cls.serialize_registration(r, user=user) for r in qs]

    @classmethod
    def get_registration(cls, user: Any, registration_id: str) -> Dict[str, Any]:
        """Fetches single PCB registration by ID within tenant boundary."""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )
        reg = PharmacyPersonnelPcbRegistration.objects.filter(
            registration_id=str(registration_id).strip(),
            tenant=tenant,
        ).select_related("branch", "staff_member").first()

        if not reg:
            raise NotFound({"registration_id": f"PCB registration record '{registration_id}' not found."})

        return cls.serialize_registration(reg, user=user)

    @classmethod
    def get_registration_evidence(cls, user: Any, registration_id: str) -> Dict[str, Any]:
        """
        Retrieves unredacted PCB certificate evidence details.
        Strictly enforces evidence access restrictions (ADR-0252).
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )
        reg = PharmacyPersonnelPcbRegistration.objects.filter(
            registration_id=str(registration_id).strip(),
            tenant=tenant,
        ).select_related("branch", "staff_member").first()

        if not reg:
            raise NotFound({"registration_id": f"PCB registration record '{registration_id}' not found."})

        if not reg.can_view_evidence(user):
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_PCB_EVIDENCE_ACCESS_RESTRICTED",
                    "message_en": "Confidential evidence access is restricted to pharmacy owners, branch managers, and the credential holder.",
                    "message_bn": "গোপনীয় প্রমাণপত্রে প্রবেশাধিকার শুধুমাত্র মালিক, ব্রাঞ্চ ম্যানেজার এবং সংশ্লিষ্ট ফার্মাসিস্টের জন্য সংরক্ষিত।",
                }
            )

        return {
            "registration_id": reg.registration_id,
            "registration_no": reg.registration_no,
            "registered_name": reg.registered_name,
            "evidence_access_level": reg.evidence_access_level,
            "evidence_document_uri": reg.evidence_document_uri,
            "evidence_file_name": reg.evidence_file_name,
            "evidence_mime_type": reg.evidence_mime_type,
            "evidence_sha256": reg.evidence_sha256,
            "evidence_uploaded_at": reg.evidence_uploaded_at.isoformat() if reg.evidence_uploaded_at else None,
        }

    @classmethod
    @transaction.atomic
    def create_or_update_registration(
        cls,
        user: Any,
        data: Dict[str, Any],
        registration_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Registers or updates a PCB personnel record with statutory validations.
        Requires Pharmacy Owner or Branch Manager role.
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials required.")

        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in cls.ALLOWED_MANAGE_ROLES:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_PCB_MANAGE_UNAUTHORIZED",
                    "message_en": "Only Pharmacy Owners and Branch Managers can manage PCB registrations.",
                    "message_bn": "শুধুমাত্র ফার্মেসি মালিক ও ব্রাঞ্চ ম্যানেজার পিসিবি রেজিস্ট্রেশন পরিচালনা করতে পারবেন।",
                }
            )

        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=True, allowed_roles=cls.ALLOWED_MANAGE_ROLES
        )

        # 1. Registration Number Validation
        reg_no = str(data.get("registration_no") or "").strip().upper()
        if len(reg_no) < 3:
            raise ValidationError({"registration_no": "PCB Registration number (minimum 3 characters) is required."})

        # 2. Grade Validation
        grade = str(data.get("grade") or "").strip().upper()
        if grade not in PcbGrade.values:
            raise ValidationError({
                "grade": f"Invalid PCB qualification grade '{grade}'. Must be one of: {list(PcbGrade.values)}"
            })

        # 3. Staff Member Validation
        staff_id = data.get("staff_member_id")
        if not staff_id and not registration_id:
            raise ValidationError({"staff_member_id": "staff_member_id is required."})

        staff = None
        if staff_id:
            staff = StaffMember.objects.filter(staff_id=str(staff_id).strip(), tenant=tenant).first()
            if not staff:
                raise ValidationError({"staff_member_id": f"Staff member '{staff_id}' does not exist for this pharmacy tenant."})

        # 4. Branch Assignment Validation
        branch_id = data.get("branch_id")
        branch = None
        if branch_id:
            branch = Branch.objects.filter(branch_id=str(branch_id).strip(), tenant=tenant).first()
            if not branch:
                raise ValidationError({"branch_id": f"Branch '{branch_id}' does not exist for this pharmacy tenant."})
        elif staff and staff.branch:
            branch = staff.branch
        elif not registration_id:
            raise ValidationError({"branch_id": "branch_id is required for branch assignment."})

        # 5. Dates Validation
        issue_date_raw = data.get("council_issue_date")
        valid_until_raw = data.get("valid_until")

        if not issue_date_raw and not registration_id:
            raise ValidationError({"council_issue_date": "council_issue_date is required."})
        if not valid_until_raw and not registration_id:
            raise ValidationError({"valid_until": "valid_until date is required."})

        def parse_date(val: Any) -> Optional[datetime.date]:
            if not val:
                return None
            if isinstance(val, datetime.date):
                return val
            try:
                return datetime.date.fromisoformat(str(val).split("T")[0])
            except ValueError:
                raise ValidationError({"date": f"Invalid date format '{val}'. Expected YYYY-MM-DD."})

        issue_date = parse_date(issue_date_raw) if issue_date_raw else None
        valid_until = parse_date(valid_until_raw) if valid_until_raw else None
        renewal_date = parse_date(data.get("renewal_submitted_at")) if data.get("renewal_submitted_at") else None

        if issue_date and valid_until and valid_until < issue_date:
            raise ValidationError({
                "valid_until": "Validity date (valid_until) cannot be earlier than issuance date (council_issue_date)."
            })

        # 6. Retrieve or Create Instance
        reg = None
        if registration_id:
            reg = PharmacyPersonnelPcbRegistration.objects.filter(
                registration_id=str(registration_id).strip(),
                tenant=tenant,
            ).first()
            if not reg:
                raise NotFound({"registration_id": f"PCB registration '{registration_id}' not found."})
        else:
            # Check unique constraint on tenant + registration_no
            existing = PharmacyPersonnelPcbRegistration.objects.filter(
                tenant=tenant, registration_no=reg_no, is_archived=False
            ).first()
            if existing:
                raise ValidationError({
                    "registration_no": f"PCB Registration '{reg_no}' is already active for staff member '{existing.staff_member.full_name}'."
                })

            reg = PharmacyPersonnelPcbRegistration(
                registration_id=f"pcb-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                branch=branch,
                staff_member=staff,
            )

        # 7. Apply updates
        reg.registration_no = reg_no
        reg.grade = grade
        reg.registered_name = str(data.get("registered_name") or (staff.full_name if staff else reg.registered_name)).strip()
        if branch:
            reg.branch = branch
        if staff:
            reg.staff_member = staff
        if issue_date:
            reg.council_issue_date = issue_date
        if valid_until:
            reg.valid_until = valid_until
        if renewal_date is not None or "renewal_submitted_at" in data:
            reg.renewal_submitted_at = renewal_date

        if "status" in data:
            st = str(data["status"]).strip().upper()
            if st in PcbRegistrationStatus.values:
                reg.status = st

        if "is_verified" in data:
            reg.is_verified = bool(data["is_verified"])
            if reg.is_verified:
                reg.verified_by = user
                reg.verified_at = timezone.now()
        if "verification_notes" in data:
            reg.verification_notes = str(data["verification_notes"]).strip()

        # Evidence attributes
        if "evidence_document_uri" in data:
            reg.evidence_document_uri = str(data["evidence_document_uri"]).strip()
        if "evidence_file_name" in data:
            reg.evidence_file_name = str(data["evidence_file_name"]).strip()
        if "evidence_mime_type" in data:
            reg.evidence_mime_type = str(data["evidence_mime_type"]).strip()
        if "evidence_sha256" in data:
            reg.evidence_sha256 = str(data["evidence_sha256"]).strip()
        if reg.evidence_document_uri and not reg.evidence_uploaded_at:
            reg.evidence_uploaded_at = timezone.now()
        if "evidence_access_level" in data:
            al = str(data["evidence_access_level"]).strip().upper()
            if al in PcbEvidenceAccessLevel.values:
                reg.evidence_access_level = al

        # Synchronize staff member pcb_registration_no field
        if reg.staff_member and reg.registration_no != reg.staff_member.pcb_registration_no:
            reg.staff_member.pcb_registration_no = reg.registration_no
            reg.staff_member.save(update_fields=["pcb_registration_no"])

        reg.sha256_hash = reg.compute_sha256_hash()
        reg.save()

        return cls.serialize_registration(reg, user=user)

    @classmethod
    @transaction.atomic
    def archive_registration(cls, user: Any, registration_id: str) -> None:
        """Soft-deletes / revokes PCB registration."""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=True, allowed_roles=cls.ALLOWED_MANAGE_ROLES
        )
        reg = PharmacyPersonnelPcbRegistration.objects.filter(
            registration_id=str(registration_id).strip(),
            tenant=tenant,
        ).first()

        if not reg:
            raise NotFound({"registration_id": f"PCB registration '{registration_id}' not found."})

        reg.is_archived = True
        reg.status = PcbRegistrationStatus.REVOKED
        reg.save(update_fields=["is_archived", "status", "updated_at"])

    @classmethod
    def verify_credential_gate(
        cls,
        tenant_id: str,
        registration_no: str,
        required_grade: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Compliance gate verifying whether a PCB registration is valid and active
        for prescription verification, clinical overrides, or antibiotic dispensing.
        """
        reg_no = str(registration_no or "").strip().upper()
        reg = PharmacyPersonnelPcbRegistration.objects.filter(
            tenant_id=tenant_id,
            registration_no=reg_no,
            is_archived=False,
        ).select_related("branch", "staff_member").first()

        if not reg:
            return {
                "is_authorized": False,
                "error_code": "ERR_PCB_REGISTRATION_NOT_FOUND",
                "message_en": f"PCB Registration '{reg_no}' is not registered in this pharmacy system.",
                "message_bn": f"পিসিবি রেজিস্ট্রেশন নম্বর '{reg_no}' সিস্টেমে নিবন্ধিত নেই।",
            }

        live_status = reg.evaluate_live_status()
        if live_status not in (PcbRegistrationStatus.ACTIVE, PcbRegistrationStatus.PENDING_RENEWAL):
            return {
                "is_authorized": False,
                "error_code": "ERR_PCB_CREDENTIAL_EXPIRED",
                "status": live_status,
                "valid_until": reg.valid_until.isoformat() if reg.valid_until else None,
                "message_en": f"PCB Registration '{reg_no}' is expired or suspended [{live_status}].",
                "message_bn": f"পিসিবি রেজিস্ট্রেশন নম্বর '{reg_no}' এর মেয়াদ উত্তীর্ণ বা স্থগিত [{live_status}]।",
            }

        if required_grade:
            req_g = str(required_grade).strip().upper()
            if reg.grade != req_g and reg.grade != PcbGrade.GRADE_A:
                return {
                    "is_authorized": False,
                    "error_code": "ERR_PCB_INSUFFICIENT_GRADE",
                    "current_grade": reg.grade,
                    "required_grade": req_g,
                    "message_en": f"Clinical action requires PCB Grade '{req_g}', but registrant holds Grade '{reg.grade}'.",
                    "message_bn": f"এই কাজের জন্য পিসিবি গ্রেড '{req_g}' প্রয়োজন, কিন্তু ফার্মাসিস্টের গ্রেড '{reg.grade}'।",
                }

        return {
            "is_authorized": True,
            "registration_id": reg.registration_id,
            "registration_no": reg.registration_no,
            "registered_name": reg.registered_name,
            "grade": reg.grade,
            "status": live_status,
            "valid_until": reg.valid_until.isoformat() if reg.valid_until else None,
            "branch_id": reg.branch_id,
            "is_verified": reg.is_verified,
        }

    @classmethod
    def get_branch_audit_summary(cls, user: Any, branch_id: str) -> Dict[str, Any]:
        """Provides compliance summary counts for staff PCB credentials at a branch."""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )
        regs = PharmacyPersonnelPcbRegistration.objects.filter(
            tenant=tenant, branch_id=str(branch_id).strip(), is_archived=False
        )

        total = regs.count()
        active = 0
        expiring_soon = 0
        expired = 0
        pending_renewal = 0
        grades = {"GRADE_A": 0, "GRADE_B": 0, "GRADE_C": 0}

        for r in regs:
            st = r.evaluate_live_status()
            days = r.days_until_expiry()
            if st == PcbRegistrationStatus.ACTIVE:
                active += 1
                if 0 <= days <= 30:
                    expiring_soon += 1
            elif st == PcbRegistrationStatus.PENDING_RENEWAL:
                pending_renewal += 1
            elif st == PcbRegistrationStatus.EXPIRED:
                expired += 1

            if r.grade in grades:
                grades[r.grade] += 1

        return {
            "tenant_id": tenant.tenant_id if hasattr(tenant, "tenant_id") else str(tenant),
            "branch_id": str(branch_id).strip(),
            "total_registered_personnel": total,
            "active_count": active,
            "expiring_soon_30_days": expiring_soon,
            "expired_count": expired,
            "pending_renewal_count": pending_renewal,
            "grade_distribution": grades,
        }

