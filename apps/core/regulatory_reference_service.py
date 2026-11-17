"""
backend/apps/core/regulatory_reference_service.py
Architecture Decision: ADR-0259 (P259).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Domain service managing statutory regulatory references (DGDA, PCB, WHO, MOHFW)
with source URI, versioning, scheduled review dates, and tamper-evident document digests.
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    RegulatoryJurisdiction,
    RegulatoryReferenceRegister,
    RegulatoryReferenceStatus,
    Tenant,
)
from apps.core.pos_domain_service import PosDomainService


class RegulatoryReferenceService:
    """
    Manages reviewed regulatory frameworks, national PMS guidelines, and antimicrobial
    stewardship mandates with explicit audit trails and review date scheduling.
    """

    ALLOWED_READ_ROLES = ["SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST", "CASHIER"]
    ALLOWED_MANAGE_ROLES = ["SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST"]

    @classmethod
    def serialize_reference(cls, record: RegulatoryReferenceRegister, user: Any = None) -> Dict[str, Any]:
        """Serializes RegulatoryReferenceRegister with review schedule metrics."""
        expected_hash = record.compute_sha256_hash()
        is_tamper_free = record.sha256_document_hash == expected_hash

        return {
            "reference_id": record.reference_id,
            "tenant_id": str(record.tenant_id),
            "branch_id": str(record.branch_id) if record.branch_id else None,
            "branch_name": record.branch.name if record.branch else "Tenant-Wide",
            "title": record.title,
            "jurisdiction": record.jurisdiction,
            "jurisdiction_display": record.get_jurisdiction_display(),
            "authority_name": record.authority_name,
            "source_uri": record.source_uri,
            "version": record.version,
            "effective_date": record.effective_date.isoformat(),
            "next_review_date": record.next_review_date.isoformat(),
            "is_review_overdue": record.is_review_overdue(),
            "status": record.status,
            "status_display": record.get_status_display(),
            "reviewed_by_id": str(record.reviewed_by_id) if record.reviewed_by_id else None,
            "reviewed_by_name": getattr(record.reviewed_by, "full_name", None) or getattr(record.reviewed_by, "email", "Compliance Reviewer"),
            "reviewed_at": record.reviewed_at.isoformat() if record.reviewed_at else None,
            "sha256_document_hash": record.sha256_document_hash,
            "is_tamper_free": is_tamper_free,
            "summary_notes": record.summary_notes,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        }

    @classmethod
    def list_references(
        cls,
        user: Any,
        jurisdiction: Optional[str] = None,
        status_filter: Optional[str] = None,
        overdue_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """Lists regulatory references for tenant with overdue filter."""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )

        qs = RegulatoryReferenceRegister.objects.filter(tenant=tenant).select_related("branch", "reviewed_by")
        if jurisdiction:
            qs = qs.filter(jurisdiction=str(jurisdiction).strip().upper())
        if status_filter:
            qs = qs.filter(status=str(status_filter).strip().upper())
        if overdue_only:
            today = timezone.localdate() if hasattr(timezone, "localdate") else datetime.date.today()
            qs = qs.filter(next_review_date__lt=today, status=RegulatoryReferenceStatus.ACTIVE)

        return [cls.serialize_reference(r, user=user) for r in qs]

    @classmethod
    def get_reference(cls, user: Any, reference_id: str) -> Dict[str, Any]:
        """Retrieves single regulatory reference."""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )

        record = RegulatoryReferenceRegister.objects.filter(
            reference_id=str(reference_id).strip(),
            tenant=tenant,
        ).select_related("branch", "reviewed_by").first()

        if not record:
            raise NotFound({"reference_id": f"Regulatory reference '{reference_id}' not found."})

        return cls.serialize_reference(record, user=user)

    @classmethod
    @transaction.atomic
    def create_reference(cls, user: Any, data: Dict[str, Any]) -> Dict[str, Any]:
        """Registers a new statutory regulatory reference with scheduled review."""
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials required.")

        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in cls.ALLOWED_MANAGE_ROLES:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_REGULATORY_REGISTER_UNAUTHORIZED",
                    "message_en": "Only credentialed Pharmacists, Branch Managers, and Owners can register regulatory references.",
                    "message_bn": "শুধুমাত্র ফার্মাসিস্ট, ব্রাঞ্চ ম্যানেজার ও ফার্মেসি মালিক রেগুলেটরি রেফারেন্স নিবন্ধন করতে পারেন।",
                }
            )

        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=True, allowed_roles=cls.ALLOWED_MANAGE_ROLES
        )

        # 1. Title validation
        title = str(data.get("title") or "").strip()
        if len(title) < 3:
            raise ValidationError({"title": "title must be at least 3 characters."})

        # 2. Jurisdiction validation
        raw_jur = data.get("jurisdiction", RegulatoryJurisdiction.DGDA_BANGLADESH)
        jurisdiction = str(raw_jur).strip().upper()
        if jurisdiction not in RegulatoryJurisdiction.values:
            raise ValidationError({
                "jurisdiction": f"Invalid jurisdiction '{jurisdiction}'. Must be one of: {list(RegulatoryJurisdiction.values)}"
            })

        # 3. Source URI validation
        source_uri = str(data.get("source_uri") or "").strip()
        if not source_uri.startswith(("http://", "https://", "ftp://")):
            raise ValidationError({"source_uri": "source_uri must be a valid URL (starting with http:// or https://)."})

        # 4. Version validation
        version = str(data.get("version") or "").strip()
        if not version:
            raise ValidationError({"version": "version is required (e.g. '2024.1' or 'v2.0')."})

        # 5. Date boundaries
        eff_raw = data.get("effective_date")
        next_raw = data.get("next_review_date")
        if not eff_raw or not next_raw:
            raise ValidationError({"dates": "Both effective_date and next_review_date are required (YYYY-MM-DD)."})

        try:
            eff_date = datetime.date.fromisoformat(str(eff_raw).strip())
            next_date = datetime.date.fromisoformat(str(next_raw).strip())
        except ValueError:
            raise ValidationError({"dates": "Invalid date format. Expected YYYY-MM-DD."})

        if eff_date > next_date:
            raise ValidationError({"next_review_date": "next_review_date cannot be earlier than effective_date."})

        # Branch scoping (optional)
        branch_id = data.get("branch_id")
        branch = None
        if branch_id:
            branch = Branch.objects.filter(branch_id=str(branch_id).strip(), tenant=tenant).first()
            if not branch:
                raise ValidationError({"branch_id": f"Branch '{branch_id}' does not exist for this tenant."})

        authority_name = str(data.get("authority_name") or "Directorate General of Drug Administration").strip()
        summary_notes = str(data.get("summary_notes") or "").strip()

        ref_id = f"reg-{uuid.uuid4().hex[:12]}"
        record = RegulatoryReferenceRegister(
            reference_id=ref_id,
            tenant=tenant,
            branch=branch,
            title=title,
            jurisdiction=jurisdiction,
            authority_name=authority_name,
            source_uri=source_uri,
            version=version,
            effective_date=eff_date,
            next_review_date=next_date,
            status=RegulatoryReferenceStatus.ACTIVE,
            reviewed_by=user,
            reviewed_at=timezone.now(),
            summary_notes=summary_notes,
        )
        record.sha256_document_hash = record.compute_sha256_hash()
        record.save()

        return cls.serialize_reference(record, user=user)

    @classmethod
    @transaction.atomic
    def update_reference(cls, user: Any, reference_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Updates or transitions status of a regulatory reference."""
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials required.")

        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in cls.ALLOWED_MANAGE_ROLES:
            raise PermissionDenied("Only Pharmacists and Managers can update regulatory references.")

        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=True, allowed_roles=cls.ALLOWED_MANAGE_ROLES
        )

        record = RegulatoryReferenceRegister.objects.select_for_update().filter(
            reference_id=str(reference_id).strip(),
            tenant=tenant,
        ).first()

        if not record:
            raise NotFound({"reference_id": f"Regulatory reference '{reference_id}' not found."})

        if "title" in data and data["title"]:
            record.title = str(data["title"]).strip()
        if "source_uri" in data and data["source_uri"]:
            uri = str(data["source_uri"]).strip()
            if not uri.startswith(("http://", "https://", "ftp://")):
                raise ValidationError({"source_uri": "source_uri must be a valid URL."})
            record.source_uri = uri
        if "version" in data and data["version"]:
            record.version = str(data["version"]).strip()
        if "status" in data and data["status"]:
            status_val = str(data["status"]).strip().upper()
            if status_val not in RegulatoryReferenceStatus.values:
                raise ValidationError({"status": f"Invalid status '{status_val}'."})
            record.status = status_val
        if "next_review_date" in data and data["next_review_date"]:
            try:
                record.next_review_date = datetime.date.fromisoformat(str(data["next_review_date"]).strip())
            except ValueError:
                raise ValidationError({"next_review_date": "Invalid next_review_date format (YYYY-MM-DD)."})
        if "summary_notes" in data:
            record.summary_notes = str(data["summary_notes"]).strip()

        record.reviewed_by = user
        record.reviewed_at = timezone.now()
        record.sha256_document_hash = record.compute_sha256_hash()
        record.save()

        return cls.serialize_reference(record, user=user)

    @classmethod
    @transaction.atomic
    def archive_reference(cls, user: Any, reference_id: str, reason: str = "") -> Dict[str, Any]:
        """Archives a superseded or inactive regulatory reference."""
        return cls.update_reference(
            user,
            reference_id,
            {"status": RegulatoryReferenceStatus.ARCHIVED, "summary_notes": f"Archived: {reason}".strip()},
        )
