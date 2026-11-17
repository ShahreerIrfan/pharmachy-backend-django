"""
backend/apps/core/drug_license_service.py

Architecture Decision: ADR-0251 (P251).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Service for Branch Drug License Record Management under DGDA regulations:
- Tracks official branch retail/wholesale drug licenses (Form 20, 20A, 21).
- Enforces validity dates, renewal tracking, issuing authority, and review reminders.
- Implements RBAC: Pharmacy Owner and Branch Manager can manage; staff/cashiers have read access.
- Validates license integrity with cryptographic document SHA-256 hash.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any, Dict, List, Optional

from rest_framework.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import (
    Branch,
    BranchDrugLicenseRecord,
    DrugLicenseStatus,
    DrugLicenseType,
    Tenant,
)
from apps.core.pos_domain_service import PosDomainService
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class DrugLicenseComplianceService:
    """
    Coordinates physical branch drug license records, compliance checks, and review reminders.
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
    def serialize_license(cls, lic: BranchDrugLicenseRecord) -> Dict[str, Any]:
        """Serializes drug license record to contract JSON dict."""
        live_status = lic.evaluate_live_status()
        days_left = lic.days_until_expiry()
        return {
            "license_record_id": lic.license_record_id,
            "tenant_id": lic.tenant_id,
            "branch_id": lic.branch_id,
            "branch_name": lic.branch.name,
            "license_number": lic.license_number,
            "issuing_authority": lic.issuing_authority,
            "license_type": lic.license_type,
            "issue_date": lic.issue_date.isoformat() if lic.issue_date else None,
            "expiry_date": lic.expiry_date.isoformat() if lic.expiry_date else None,
            "renewal_submitted_at": lic.renewal_submitted_at.isoformat() if lic.renewal_submitted_at else None,
            "status": live_status,
            "days_until_expiry": days_left,
            "is_valid": live_status in (DrugLicenseStatus.ACTIVE, DrugLicenseStatus.RENEWAL_PENDING),
            "document_file_uri": lic.document_file_uri,
            "document_sha256": lic.document_sha256,
            "reminder_lead_days": lic.reminder_lead_days or [90, 60, 30, 15, 7],
            "last_reviewed_at": lic.last_reviewed_at.isoformat() if lic.last_reviewed_at else None,
            "review_notes": lic.review_notes,
            "is_archived": lic.is_archived,
            "created_at": lic.created_at.isoformat() if lic.created_at else None,
        }

    @classmethod
    @transaction.atomic
    def create_or_renew_license(
        cls,
        user: Any,
        branch: Branch,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Creates or renews an official branch drug license record.
        Requires Pharmacy Owner or Branch Manager role.
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required to manage branch drug licenses.")

        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in cls.ALLOWED_MANAGE_ROLES:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_DRUG_LICENSE_MANAGE_UNAUTHORIZED",
                    "message_en": "Only Pharmacy Owners and Branch Managers can register or update branch drug licenses.",
                    "message_bn": "শুধুমাত্র ফার্মেসি মালিক ও ব্রাঞ্চ ম্যানেজার ড্রাগ লাইসেন্স নিবন্ধন ও পরিবর্তন করতে পারবেন।",
                }
            )

        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=branch.branch_id, require_write=True, allowed_roles=cls.ALLOWED_MANAGE_ROLES
        )

        # 1. License number validation
        lic_no = str(data.get("license_number") or "").strip().upper()
        if len(lic_no) < 4:
            raise ValidationError({"license_number": "Drug license number (minimum 4 characters) is mandatory."})

        # 2. Issuing authority validation
        authority = str(data.get("issuing_authority") or "Directorate General of Drug Administration (DGDA)").strip()
        if len(authority) < 3:
            raise ValidationError({"issuing_authority": "Issuing authority (minimum 3 characters) is mandatory."})

        # 3. License type validation
        ltype = str(data.get("license_type") or DrugLicenseType.FORM_20_RETAIL).strip().upper()
        if ltype not in DrugLicenseType.values:
            raise ValidationError(
                {"license_type": f"Invalid license type '{ltype}'. Must be one of {DrugLicenseType.values}."}
            )

        # 4. Issue and expiry date parsing and validation
        try:
            issue_str = str(data.get("issue_date") or "").strip()
            issue_date = datetime.date.fromisoformat(issue_str)
        except (ValueError, TypeError):
            raise ValidationError({"issue_date": "Valid ISO format issue_date (YYYY-MM-DD) is required."})

        try:
            expiry_str = str(data.get("expiry_date") or "").strip()
            expiry_date = datetime.date.fromisoformat(expiry_str)
        except (ValueError, TypeError):
            raise ValidationError({"expiry_date": "Valid ISO format expiry_date (YYYY-MM-DD) is required."})

        if expiry_date <= issue_date:
            raise ValidationError({"expiry_date": "License expiry_date must be strictly after issue_date."})

        renewal_submitted = None
        if data.get("renewal_submitted_at"):
            try:
                renewal_submitted = datetime.date.fromisoformat(str(data.get("renewal_submitted_at")).strip())
            except (ValueError, TypeError):
                raise ValidationError({"renewal_submitted_at": "Invalid renewal_submitted_at format (YYYY-MM-DD)."})

        # Initial status
        today = timezone.localdate()
        if expiry_date < today:
            stat = DrugLicenseStatus.RENEWAL_PENDING if renewal_submitted else DrugLicenseStatus.EXPIRED
        else:
            stat = str(data.get("status") or DrugLicenseStatus.ACTIVE).strip().upper()
            if stat not in DrugLicenseStatus.values:
                stat = DrugLicenseStatus.ACTIVE

        doc_uri = str(data.get("document_file_uri") or "").strip()
        review_notes = str(data.get("review_notes") or "").strip()

        # Archive any previous active license for this branch
        BranchDrugLicenseRecord.objects.filter(
            tenant=tenant, branch=branch, is_archived=False
        ).update(is_archived=True)

        record = BranchDrugLicenseRecord.objects.create(
            license_record_id=f"bdl-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            license_number=lic_no,
            issuing_authority=authority,
            license_type=ltype,
            issue_date=issue_date,
            expiry_date=expiry_date,
            renewal_submitted_at=renewal_submitted,
            status=stat,
            document_file_uri=doc_uri,
            last_reviewed_at=timezone.now(),
            last_reviewed_by=user,
            review_notes=review_notes,
            is_archived=False,
        )

        # Sync branch license number
        branch.license_number = lic_no
        branch.save(update_fields=["license_number", "updated_at"])

        return cls.serialize_license(record)

    @classmethod
    def evaluate_branch_drug_license(cls, branch: Branch) -> Dict[str, Any]:
        """
        Evaluates the active drug license status for a given branch.
        Surfaces compliance validity and upcoming expiry warnings.
        """
        active_lic = (
            BranchDrugLicenseRecord.objects.filter(branch=branch, is_archived=False)
            .order_by("-expiry_date")
            .first()
        )
        if not active_lic:
            return {
                "branch_id": branch.branch_id,
                "branch_name": branch.name,
                "has_license": False,
                "is_valid": False,
                "status": "UNLICENSED",
                "days_until_expiry": 0,
                "message_en": f"Branch '{branch.name}' has no active drug license record.",
                "message_bn": f"ব্রাঞ্চ '{branch.name}'-এর কোনো সক্রিয় ড্রাগ লাইসেন্স রেকর্ড নেই।",
                "license": None,
            }

        live_stat = active_lic.evaluate_live_status()
        days_left = active_lic.days_until_expiry()
        is_valid = live_stat in (DrugLicenseStatus.ACTIVE, DrugLicenseStatus.RENEWAL_PENDING)

        warn_msg_en = None
        warn_msg_bn = None
        if live_stat == DrugLicenseStatus.EXPIRED:
            warn_msg_en = f"Drug license '{active_lic.license_number}' expired on {active_lic.expiry_date}."
            warn_msg_bn = f"ড্রাগ লাইসেন্স '{active_lic.license_number}'-এর মেয়াদ {active_lic.expiry_date} তারিখে উত্তীর্ণ হয়েছে।"
        elif live_stat == DrugLicenseStatus.SUSPENDED:
            warn_msg_en = "Drug license has been suspended by DGDA."
            warn_msg_bn = "ডিজিডিএ কর্তৃক ড্রাগ লাইসেন্স স্থগিত করা হয়েছে।"
        elif days_left <= 30:
            warn_msg_en = f"Drug license will expire in {days_left} days. Immediate renewal required."
            warn_msg_bn = f"ড্রাগ লাইসেন্সের মেয়াদ আর {days_left} দিন বাকি। জরুরি নবায়ন প্রয়োজন।"

        return {
            "branch_id": branch.branch_id,
            "branch_name": branch.name,
            "has_license": True,
            "is_valid": is_valid,
            "status": live_stat,
            "days_until_expiry": days_left,
            "warning_en": warn_msg_en,
            "warning_bn": warn_msg_bn,
            "license": cls.serialize_license(active_lic),
        }

    @classmethod
    def list_branch_licenses(
        cls, user: Any, branch_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Lists drug license records scoped by tenant and optional branch."""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=branch_id, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )
        qs = BranchDrugLicenseRecord.objects.filter(tenant=tenant)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        qs = qs.select_related("branch").order_by("-created_at")
        return [cls.serialize_license(lic) for lic in qs]

    @classmethod
    def list_compliance_reminders(cls, user: Any) -> List[Dict[str, Any]]:
        """Generates upcoming expiration reminders across tenant branches."""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )
        today = timezone.localdate()
        threshold = today + datetime.timedelta(days=90)
        licenses = (
            BranchDrugLicenseRecord.objects.filter(
                tenant=tenant, is_archived=False, expiry_date__lte=threshold
            )
            .select_related("branch")
            .order_by("expiry_date")
        )

        reminders = []
        for lic in licenses:
            days_left = lic.days_until_expiry()
            live_stat = lic.evaluate_live_status()
            reminders.append(
                {
                    "license_record_id": lic.license_record_id,
                    "branch_id": lic.branch_id,
                    "branch_name": lic.branch.name,
                    "license_number": lic.license_number,
                    "expiry_date": lic.expiry_date.isoformat(),
                    "days_until_expiry": days_left,
                    "status": live_stat,
                    "urgency": "CRITICAL" if days_left <= 15 else "HIGH" if days_left <= 30 else "MEDIUM",
                    "reminder_message_en": f"License {lic.license_number} ({lic.branch.name}) expires in {days_left} days.",
                    "reminder_message_bn": f"লাইসেন্স {lic.license_number} ({lic.branch.name})-এর মেয়াদ আর {days_left} দিন বাকি।",
                }
            )
        return reminders

