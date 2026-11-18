"""
backend/apps/core/accounting_error_reversal_service.py
Architecture Decision: ADR-0299 (P299).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service governing Accounting Error Reversal & Reverse-and-Rebook:
- Corrects posted accounting mistakes strictly via approved reversing journals (and optional
  balanced replacement journals), NEVER destructive edits or raw GL balance overwrites.
- Enforces Maker-Checker governance: Finance Officer / Branch Manager submits reversal request;
  Pharmacy Owner / independent senior reviewer approves and atomically executes reversal.
- Enforces period locks (P292), balanced replacement entries, duplicate request guards,
  multi-tenant isolation, AuditLogEntry, and CSV export.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.double_entry_journal_service import DoubleEntryJournalService, q2
from apps.core.models import (
    AccountingErrorCategory,
    AccountingReversalRequest,
    AccountingReversalRequestStatus,
    JournalEntry,
    JournalEntryStatus,
    JournalSourceModule,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")

REVERSAL_REQUEST_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.FINANCE_OFFICER,
    UserRole.BRANCH_MANAGER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "FINANCE_OFFICER",
    "BRANCH_MANAGER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

REVERSAL_APPROVE_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

REVERSAL_VIEW_ROLES = REVERSAL_REQUEST_ROLES | {UserRole.AUDITOR, "AUDITOR"}


class AccountingErrorReversalService:
    """
    Governs formal reversal requests and atomic reverse-and-rebook execution on posted journals.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return
        if getattr(actor_user, "role", None) not in REVERSAL_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_REVERSAL_VIEW_DENIED",
                    "detail": "Counter staff are not authorized to access accounting reversal workflows.",
                    "message_en": "Counter staff are not authorized to access accounting reversal workflows.",
                    "message_bn": "কাউন্টার স্টাফ অ্যাকাউন্টিং রিভার্সাল অ্যাক্সেস করার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def assert_request_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return
        if getattr(actor_user, "role", None) not in REVERSAL_REQUEST_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_REVERSAL_REQUEST_DENIED",
                    "detail": "Not authorized to submit accounting error reversal requests.",
                }
            )

    @classmethod
    def assert_approve_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return
        if getattr(actor_user, "role", None) not in REVERSAL_APPROVE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_REVERSAL_APPROVE_DENIED",
                    "detail": "Only Pharmacy Owner or Finance Officer can approve and execute accounting reversals.",
                    "message_en": "Only Pharmacy Owner or Finance Officer can approve accounting reversals.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা ফিন্যান্স অফিসার অ্যাকাউন্টিং রিভার্সাল অনুমোদন করতে পারেন।",
                }
            )

    @classmethod
    def assert_tenant_active(cls, tenant_id: str) -> Tenant:
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' not found."})
        if (
            getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
            or getattr(tenant, "subscription_status", None) == "SUSPENDED"
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant subscription is suspended. Reversal operations blocked.",
                }
            )
        return tenant

    @classmethod
    def _serialize_request(cls, req: AccountingReversalRequest) -> Dict[str, Any]:
        return {
            "request_id": req.request_id,
            "tenant_id": str(req.tenant_id),
            "branch_id": str(req.branch_id) if req.branch_id else None,
            "original_journal_id": str(req.original_journal_id),
            "original_journal_number": req.original_journal.journal_number,
            "original_total_bdt": str(q2(req.original_journal.total_debit_bdt)),
            "error_category": req.error_category,
            "reason": req.reason,
            "has_replacement_entry": bool(req.replacement_lines_json),
            "replacement_lines_count": len(req.replacement_lines_json or []),
            "status": req.status,
            "approval_notes": req.approval_notes,
            "reversal_journal_id": str(req.reversal_journal_id) if req.reversal_journal_id else None,
            "replacement_journal_id": str(req.replacement_journal_id) if req.replacement_journal_id else None,
            "approved_at": req.approved_at.isoformat() if req.approved_at else None,
            "created_at": req.created_at.isoformat(),
        }

    @classmethod
    def _validate_replacement_lines(cls, lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not lines:
            return []
        if not isinstance(lines, list) or len(lines) < 2:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_REPLACEMENT_LINES",
                    "detail": "Replacement journal must include at least 2 balanced lines.",
                }
            )
        tot_dr = DECIMAL_ZERO
        tot_cr = DECIMAL_ZERO
        normalized: List[Dict[str, Any]] = []
        for idx, ln in enumerate(lines, start=1):
            code = str(ln.get("account_code", "")).strip()
            if not code:
                raise ValidationError({"error_code": "ERR_MISSING_ACCOUNT_CODE", "detail": f"Line {idx} missing account_code."})
            dr = q2(Decimal(str(ln.get("debit_amount_bdt", "0.00"))))
            cr = q2(Decimal(str(ln.get("credit_amount_bdt", "0.00"))))
            if dr < DECIMAL_ZERO or cr < DECIMAL_ZERO or (dr > DECIMAL_ZERO and cr > DECIMAL_ZERO) or (dr == DECIMAL_ZERO and cr == DECIMAL_ZERO):
                raise ValidationError({"error_code": "ERR_INVALID_LINE_AMOUNTS", "detail": f"Line {idx} must have strictly positive debit OR credit."})
            tot_dr = q2(tot_dr + dr)
            tot_cr = q2(tot_cr + cr)
            normalized.append(
                {
                    "account_code": code,
                    "debit_amount_bdt": str(dr),
                    "credit_amount_bdt": str(cr),
                    "description": str(ln.get("description", "")).strip(),
                }
            )
        if tot_dr != tot_cr or tot_dr <= DECIMAL_ZERO:
            raise ValidationError(
                {
                    "error_code": "ERR_UNBALANCED_REPLACEMENT_ENTRY",
                    "detail": f"Replacement journal unbalanced: Debits {tot_dr} != Credits {tot_cr}.",
                    "message_en": "Replacement journal debits and credits must be equal.",
                    "message_bn": "সংশোধনী জার্নালের ডেবিট এবং ক্রেডিট সমান হতে হবে।",
                }
            )
        return normalized

    @classmethod
    @transaction.atomic
    def create_reversal_request(
        cls,
        *,
        tenant_id: str,
        original_journal_id: str,
        error_category: str,
        reason: str,
        replacement_lines: Optional[List[Dict[str, Any]]] = None,
        replacement_posting_date: Optional[Any] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_request_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_REVERSAL_REASON",
                    "detail": "Audit reason is mandatory for accounting error reversal.",
                    "message_en": "Audit reason is mandatory for accounting error reversal.",
                    "message_bn": "অ্যাকাউন্টিং ভুল সংশোধনের জন্য কারণ উল্লেখ করা আবশ্যক।",
                }
            )

        err_cat = str(error_category or AccountingErrorCategory.WRONG_AMOUNT).upper().strip()
        if err_cat not in AccountingErrorCategory.values:
            raise ValidationError({"error_code": "ERR_INVALID_ERROR_CATEGORY", "detail": f"Invalid error_category '{err_cat}'."})

        orig = JournalEntry.objects.filter(tenant=tenant, entry_id=original_journal_id).first()
        if not orig:
            foreign = JournalEntry.objects.filter(entry_id=original_journal_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant journal access forbidden."})
            raise NotFound({"error_code": "ERR_JOURNAL_NOT_FOUND", "detail": f"Journal '{original_journal_id}' not found."})

        # Branch Manager restricted to own branch journals
        if getattr(actor_user, "role", None) in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            if orig.branch_id and str(orig.branch_id) != user_branch:
                raise PermissionDenied({"error_code": "ERR_BRANCH_SCOPE_DENIED", "detail": "Cannot request reversal for another branch's journal."})

        if orig.status != JournalEntryStatus.POSTED:
            raise ValidationError(
                {
                    "error_code": "ERR_JOURNAL_NOT_REVERSIBLE",
                    "detail": f"Journal '{orig.journal_number}' cannot be reversed (current status: {orig.status}).",
                }
            )

        if AccountingReversalRequest.objects.filter(
            tenant=tenant,
            original_journal=orig,
            status=AccountingReversalRequestStatus.PENDING_APPROVAL,
        ).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_REVERSAL_REQUEST",
                    "detail": f"A pending reversal request already exists for journal '{orig.journal_number}'.",
                }
            )

        norm_lines = cls._validate_replacement_lines(replacement_lines or [])
        rep_date = None
        if replacement_posting_date:
            rep_date = (
                datetime.date.fromisoformat(str(replacement_posting_date))
                if isinstance(replacement_posting_date, str)
                else replacement_posting_date
            )

        req = AccountingReversalRequest.objects.create(
            tenant=tenant,
            branch=orig.branch,
            original_journal=orig,
            error_category=err_cat,
            reason=clean_reason,
            replacement_lines_json=norm_lines,
            replacement_posting_date=rep_date,
            status=AccountingReversalRequestStatus.PENDING_APPROVAL,
            requested_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        res = cls._serialize_request(req)
        res["message_en"] = f"Reversal request '{req.request_id}' submitted for approval."
        res["message_bn"] = f"জার্নাল রিভার্সাল অনুরোধ '{req.request_id}' অনুমোদনের জন্য জমা দেওয়া হয়েছে।"
        return res

    @classmethod
    @transaction.atomic
    def approve_and_execute_reversal(
        cls,
        *,
        tenant_id: str,
        request_id: str,
        approval_notes: str = "",
        owner_self_approval_ack: bool = False,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_approve_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        req = AccountingReversalRequest.objects.select_for_update().select_related("original_journal").filter(
            tenant=tenant, request_id=request_id
        ).first()
        if not req:
            foreign = AccountingReversalRequest.objects.filter(request_id=request_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant approval forbidden."})
            raise NotFound({"error_code": "ERR_REVERSAL_REQUEST_NOT_FOUND", "detail": f"Request '{request_id}' not found."})

        if req.status != AccountingReversalRequestStatus.PENDING_APPROVAL:
            raise ValidationError(
                {
                    "error_code": "ERR_REVERSAL_REQUEST_NOT_PENDING",
                    "detail": f"Reversal request '{req.request_id}' is already in status '{req.status}'.",
                }
            )

        # Maker-Checker check: non-owner requester cannot self-approve; owner needs explicit ack if self-approving
        actor_id = getattr(actor_user, "id", None) or getattr(actor_user, "user_id", None)
        requester_id = getattr(req.requested_by, "id", None) or getattr(req.requested_by, "user_id", None)
        if actor_id and requester_id and str(actor_id) == str(requester_id):
            role = getattr(actor_user, "role", None)
            if role not in (UserRole.PHARMACY_OWNER, UserRole.SUPERADMIN, "PHARMACY_OWNER", "SUPERADMIN") or not owner_self_approval_ack:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_SELF_APPROVAL_FORBIDDEN",
                        "detail": "Maker-checker control: Requester cannot self-approve accounting reversals without independent reviewer (or explicit Owner solo-pharmacy ack).",
                        "message_en": "Requester cannot self-approve accounting reversals.",
                        "message_bn": "মেকার-চেকার নীতিমালা অনুযায়ী আবেদনকারী নিজে নিজের রিভার্সাল অনুমোদন করতে পারবেন না।",
                    }
                )

        # 1. Execute audited compensating reversal via DoubleEntryJournalService (NEVER destructive edit!)
        rev_res = DoubleEntryJournalService.reverse_journal_entry(
            tenant_id=str(tenant.tenant_id),
            entry_id=str(req.original_journal_id),
            reason=f"[{req.error_category}] {req.reason}",
            actor_user=actor_user,
        )
        rev_journal = JournalEntry.objects.get(entry_id=rev_res["reversal_journal_id"])

        # 2. Optional atomic balanced replacement journal (reverse-and-rebook)
        rep_journal = None
        if req.replacement_lines_json:
            rep_lines = [
                {
                    "account_code": l["account_code"],
                    "debit_amount_bdt": Decimal(str(l["debit_amount_bdt"])),
                    "credit_amount_bdt": Decimal(str(l["credit_amount_bdt"])),
                    "description": l.get("description", ""),
                }
                for l in req.replacement_lines_json
            ]
            rep_res = DoubleEntryJournalService.post_journal_entry(
                tenant_id=str(tenant.tenant_id),
                branch_id=str(req.branch_id) if req.branch_id else None,
                posting_date=req.replacement_posting_date or timezone.now().date(),
                source_module=JournalSourceModule.MANUAL,
                source_reference=f"REBOOK-{req.original_journal.journal_number}",
                narration=f"Corrected replacement for {req.original_journal.journal_number}: {req.reason}",
                lines=rep_lines,
                actor_user=actor_user,
            )
            rep_journal = JournalEntry.objects.get(entry_id=rep_res["entry_id"])

        req.status = AccountingReversalRequestStatus.APPROVED_EXECUTED
        req.approved_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        req.approved_at = timezone.now()
        req.approval_notes = str(approval_notes or "").strip()
        req.reversal_journal = rev_journal
        req.replacement_journal = rep_journal
        req.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="ACCOUNTING_REVERSAL_APPROVED_EXECUTED",
            resource_type="AccountingReversalRequest",
            resource_id=req.request_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "original_journal": req.original_journal.journal_number,
                "reversal_journal": rev_journal.journal_number,
                "replacement_journal": rep_journal.journal_number if rep_journal else None,
            },
        )

        res = cls._serialize_request(req)
        res["message_en"] = f"Reversal approved and compensating journal '{rev_journal.journal_number}' posted."
        res["message_bn"] = f"রিভার্সাল অনুমোদিত এবং সংশোধনী জার্নাল '{rev_journal.journal_number}' পোস্ট হয়েছে।"
        return res

    @classmethod
    @transaction.atomic
    def reject_reversal_request(
        cls, *, tenant_id: str, request_id: str, reason: str, actor_user: Any = None
    ) -> Dict[str, Any]:
        cls.assert_approve_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)
        if not str(reason or "").strip():
            raise ValidationError({"error_code": "ERR_MISSING_REJECTION_REASON", "detail": "Rejection reason is required."})

        req = AccountingReversalRequest.objects.select_for_update().filter(tenant=tenant, request_id=request_id).first()
        if not req:
            raise NotFound({"error_code": "ERR_REVERSAL_REQUEST_NOT_FOUND", "detail": f"Request '{request_id}' not found."})
        if req.status != AccountingReversalRequestStatus.PENDING_APPROVAL:
            raise ValidationError({"error_code": "ERR_REVERSAL_REQUEST_NOT_PENDING", "detail": "Only pending requests can be rejected."})

        req.status = AccountingReversalRequestStatus.REJECTED
        req.approval_notes = str(reason).strip()
        req.approved_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        req.approved_at = timezone.now()
        req.save()
        return cls._serialize_request(req)

    @classmethod
    @transaction.atomic
    def cancel_reversal_request(cls, *, tenant_id: str, request_id: str, actor_user: Any = None) -> Dict[str, Any]:
        cls.assert_request_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)
        req = AccountingReversalRequest.objects.select_for_update().filter(tenant=tenant, request_id=request_id).first()
        if not req:
            raise NotFound({"error_code": "ERR_REVERSAL_REQUEST_NOT_FOUND", "detail": f"Request '{request_id}' not found."})
        if req.status != AccountingReversalRequestStatus.PENDING_APPROVAL:
            raise ValidationError(
                {
                    "error_code": "ERR_CANNOT_CANCEL_FINALIZED",
                    "detail": "Executed or finalized reversal requests cannot be cancelled or deleted.",
                }
            )
        req.status = AccountingReversalRequestStatus.CANCELLED
        req.save()
        return {"success": True, "request_id": req.request_id, "status": req.status}

    @classmethod
    def list_reversal_requests(
        cls, *, tenant_id: str, status_filter: Optional[str] = None, actor_user: Any = None
    ) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)
        qs = AccountingReversalRequest.objects.filter(tenant=tenant).select_related("original_journal")
        if status_filter:
            qs = qs.filter(status=status_filter.upper().strip())
        if getattr(actor_user, "role", None) in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            if user_branch:
                qs = qs.filter(branch_id=user_branch)
        items = [cls._serialize_request(r) for r in qs[:100]]
        return {"tenant_id": str(tenant.tenant_id), "count": len(items), "requests": items}

    @classmethod
    def export_reversals_csv(cls, *, tenant_id: str, actor_user: Any = None) -> str:
        data = cls.list_reversal_requests(tenant_id=tenant_id, actor_user=actor_user)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "Request ID",
                "Original Journal",
                "Amount (BDT)",
                "Error Category",
                "Reason",
                "Status",
                "Reversal Journal ID",
                "Replacement Journal ID",
            ]
        )
        for r in data["requests"]:
            writer.writerow(
                [
                    r["request_id"],
                    r["original_journal_number"],
                    r["original_total_bdt"],
                    r["error_category"],
                    r["reason"],
                    r["status"],
                    r["reversal_journal_id"] or "",
                    r["replacement_journal_id"] or "",
                ]
            )
        return buf.getvalue()
