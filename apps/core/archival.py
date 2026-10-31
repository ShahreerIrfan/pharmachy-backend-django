"""
apps/core/archival.py

Regulatory Archival and Soft-Deletion Policy Engine for Bangladesh Pharmacy ERP SaaS.
Architecture Decision: ADR-0056
Enforces:
  - Strict prohibition of physical/destructive deletion on posted financial, dispensing, and audit records.
  - Soft-deletion lifecycle (is_archived, archived_at, archived_by, archival_reason) for mutable configurations.
  - Safe restoration workflows with non-repudiation audit trails.
  - Compliance with DGDA 2-year mandatory clinical/dispensing retention.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.db import models

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.errors import DestructiveDeleteForbiddenError

UTC_TZ = ZoneInfo("UTC")


class RetentionPolicyTier(models.TextChoices):
    PERMANENT_REGULATORY = "PERMANENT_REGULATORY", "Permanent Regulatory Retention (DGDA / Audit)"
    SEVEN_YEAR_FINANCIAL = "SEVEN_YEAR_FINANCIAL", "Seven-Year Tax & Financial Retention (NBR / BDT)"
    SOFT_ARCHIVE_RESTORABLE = "SOFT_ARCHIVE_RESTORABLE", "Restorable Soft-Deletion Catalog"


IMMUTABLE_RECORD_TYPES = {
    "SALE_INVOICE": {
        "tier": RetentionPolicyTier.SEVEN_YEAR_FINANCIAL,
        "destructive_delete_allowed": False,
        "reversal_mechanism": "CREDIT_NOTE_REVERSAL",
        "min_retention_days": 2555,  # 7 years
        "description_en": "Finalized cash/credit sale invoices with tax and VAT line items.",
        "description_bn": "ভ্যাট ও ট্যাক্সযুক্ত চূড়ান্ত বিক্রয় চালান বা ইনভয়েস।",
    },
    "PRESCRIPTION_RECORD": {
        "tier": RetentionPolicyTier.PERMANENT_REGULATORY,
        "destructive_delete_allowed": False,
        "reversal_mechanism": "VOID_TRANSACTION_AUDITED",
        "min_retention_days": 730,  # 2 years per DGDA
        "description_en": "Dispensing logs, doctor signatures, and prescription image uploads.",
        "description_bn": "ঔষধ প্রদান, চিকিৎসকের অনুমোদন ও প্রেসক্রিপশন রেকর্ড।",
    },
    "AUDIT_LOG_ENTRY": {
        "tier": RetentionPolicyTier.PERMANENT_REGULATORY,
        "destructive_delete_allowed": False,
        "reversal_mechanism": "NOT_APPLICABLE",
        "min_retention_days": 730,
        "description_en": "Regulatory audit trail tracking all high-value system operations.",
        "description_bn": "সকল উচ্চ-গুরুত্বপূর্ণ কর্মকাণ্ডের অপরিবর্তনীয় অডিট ট্রেইল।",
    },
    "PAYMENT_TRANSACTION": {
        "tier": RetentionPolicyTier.SEVEN_YEAR_FINANCIAL,
        "destructive_delete_allowed": False,
        "reversal_mechanism": "CREDIT_NOTE_REVERSAL",
        "min_retention_days": 2555,
        "description_en": "MFS (bKash/Nagad), Card, and Cash reconciliation transaction events.",
        "description_bn": "বিকাশ, নগদ, কার্ড ও নগদ অর্থ পরিশোধের লেনদেন রেকর্ড।",
    },
}

SOFT_ARCHIVABLE_TYPES = {
    "MEDICINE_CATALOG": {
        "tier": RetentionPolicyTier.SOFT_ARCHIVE_RESTORABLE,
        "destructive_delete_allowed": False,
        "reversal_mechanism": "NOT_APPLICABLE",
        "min_retention_days": 365,
        "description_en": "Medicine brand/generic master records.",
        "description_bn": "ওষুধের ব্র্যান্ড ও জেনেরিক ক্যাটালগ তথ্য।",
    },
    "STAFF_MEMBER": {
        "tier": RetentionPolicyTier.SOFT_ARCHIVE_RESTORABLE,
        "destructive_delete_allowed": False,
        "reversal_mechanism": "NOT_APPLICABLE",
        "min_retention_days": 730,
        "description_en": "Branch pharmacists, cashiers, and inventory managers.",
        "description_bn": "ফার্মাসিস্ট, ক্যাশিয়ার ও কর্মী তথ্য।",
    },
    "BRANCH_CONFIG": {
        "tier": RetentionPolicyTier.SOFT_ARCHIVE_RESTORABLE,
        "destructive_delete_allowed": False,
        "reversal_mechanism": "NOT_APPLICABLE",
        "min_retention_days": 1825,
        "description_en": "Dispensary branch operational profiles.",
        "description_bn": "ফার্মেসি শাখা পরিচালনা ও সেটআপ তথ্য।",
    },
}


class ArchivalEngine:
    """Centralized policy engine executing archival, restoration, and destructive deletion barrier."""

    @classmethod
    def get_policy_matrix(cls) -> list[dict[str, Any]]:
        """Returns the full regulatory archival and deletion rule matrix."""
        results = []
        for r_type, meta in {**IMMUTABLE_RECORD_TYPES, **SOFT_ARCHIVABLE_TYPES}.items():
            results.append(
                {
                    "resource_type": r_type,
                    "tier": meta["tier"].value if hasattr(meta["tier"], "value") else str(meta["tier"]),
                    "destructive_delete_allowed": meta["destructive_delete_allowed"],
                    "reversal_mechanism": meta["reversal_mechanism"],
                    "min_retention_days": meta["min_retention_days"],
                    "description_en": meta["description_en"],
                    "description_bn": meta["description_bn"],
                }
            )
        return results

    @classmethod
    def validate_resource_type(cls, resource_type: str) -> dict[str, Any]:
        """Validates resource type and returns policy metadata."""
        clean_type = str(resource_type).upper().strip()
        if clean_type in IMMUTABLE_RECORD_TYPES:
            return {"kind": "IMMUTABLE", "meta": IMMUTABLE_RECORD_TYPES[clean_type]}
        if clean_type in SOFT_ARCHIVABLE_TYPES:
            return {"kind": "SOFT_ARCHIVABLE", "meta": SOFT_ARCHIVABLE_TYPES[clean_type]}
        raise ValueError(
            f"Unknown or unsupported resource type '{resource_type}'. "
            f"Supported types: {list(IMMUTABLE_RECORD_TYPES.keys()) + list(SOFT_ARCHIVABLE_TYPES.keys())}."
        )

    @classmethod
    def enforce_no_destructive_delete(
        cls,
        resource_type: str,
        resource_id: str,
        tenant_id: str,
        user_id: str | None = None,
        reason: str = "",
    ) -> None:
        """
        Enforces regulatory barrier: raises DestructiveDeleteForbiddenError
        and emits an immediate security incident audit log.
        """
        policy_info = cls.validate_resource_type(resource_type)
        clean_type = str(resource_type).upper().strip()

        # Audit incident
        AuditLogEntry.objects.create(
            action="SECURITY_INCIDENT_DESTRUCTIVE_DELETE_ATTEMPT",
            resource_type="REGULATORY_RETENTION_BARRIER",
            resource_id=resource_id,
            tenant_id=tenant_id,
            user_id=user_id or "system",
            status=AuditActionStatus.DENIED,
            metadata_json={
                "event": "DESTRUCTIVE_DELETE_BLOCKED",
                "attempted_resource_type": clean_type,
                "attempted_resource_id": resource_id,
                "policy_kind": policy_info["kind"],
                "reason": reason,
            },
        )

        raise DestructiveDeleteForbiddenError(
            message_en=(
                f"Destructive physical deletion of '{clean_type}:{resource_id}' is prohibited under "
                f"ADR-0056 ({policy_info['meta']['tier']}). Use status transitions or audited reversals."
            ),
            message_bn=(
                f"'{clean_type}:{resource_id}' স্থায়ীভাবে মুছে ফেলা নিষিদ্ধ (নীতিমালা: ADR-0056)। "
                "অনুমোদিত বাতিলকরণ বা রিভার্সাল পদ্ধতি অনুসরণ করুন।"
            ),
        )

    @classmethod
    def archive_resource(
        cls,
        resource_type: str,
        resource_id: str,
        tenant_id: str,
        user_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """
        Soft-deletes a mutable entity with audited metadata.
        STRICT: Rejects immutable entities from soft-delete (must remain in active ledger).
        """
        policy_info = cls.validate_resource_type(resource_type)
        clean_type = str(resource_type).upper().strip()

        if policy_info["kind"] == "IMMUTABLE":
            cls.enforce_no_destructive_delete(
                resource_type=clean_type,
                resource_id=resource_id,
                tenant_id=tenant_id,
                user_id=user_id,
                reason=reason,
            )

        clean_reason = str(reason).strip()
        if len(clean_reason) < 5:
            raise ValueError("Archival reason is mandatory and must be at least 5 characters long.")

        now_utc = datetime.now(UTC_TZ)

        # Audit archival event
        AuditLogEntry.objects.create(
            action="RESOURCE_ARCHIVED",
            resource_type=clean_type,
            resource_id=resource_id,
            tenant_id=tenant_id,
            user_id=user_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "event": "SOFT_DELETED",
                "archived_at": now_utc.isoformat(),
                "archived_by": user_id,
                "archival_reason": clean_reason,
            },
        )

        return {
            "resource_type": clean_type,
            "resource_id": resource_id,
            "is_archived": True,
            "archived_at": now_utc.isoformat(),
            "archived_by": user_id,
            "archival_reason": clean_reason,
            "status": "ARCHIVED",
            "message_en": f"Resource '{clean_type}:{resource_id}' soft-deleted and archived successfully.",
            "message_bn": f"রিসোর্স '{clean_type}:{resource_id}' সফলভাবে আর্কাইভ করা হয়েছে।",
        }

    @classmethod
    def restore_resource(
        cls,
        resource_type: str,
        resource_id: str,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Restores a previously archived soft-deleted entity."""
        policy_info = cls.validate_resource_type(resource_type)
        clean_type = str(resource_type).upper().strip()

        if policy_info["kind"] == "IMMUTABLE":
            raise ValueError(f"Immutable resource '{clean_type}' does not support archival restoration.")

        now_utc = datetime.now(UTC_TZ)

        # Audit restoration event
        AuditLogEntry.objects.create(
            action="RESOURCE_RESTORED",
            resource_type=clean_type,
            resource_id=resource_id,
            tenant_id=tenant_id,
            user_id=user_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "event": "RESTORED_FROM_ARCHIVE",
                "restored_at": now_utc.isoformat(),
                "restored_by": user_id,
            },
        )

        return {
            "resource_type": clean_type,
            "resource_id": resource_id,
            "is_archived": False,
            "restored_at": now_utc.isoformat(),
            "restored_by": user_id,
            "status": "ACTIVE",
            "message_en": f"Resource '{clean_type}:{resource_id}' restored to active catalog.",
            "message_bn": f"রিসোর্স '{clean_type}:{resource_id}' সক্রিয় তালিকায় পুনঃস্থাপন করা হয়েছে।",
        }
