"""
backend/apps/core/supplier_payable_ledger_service.py
Architecture Decision: ADR-0272 (P272).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service governing the Supplier Accounts Payable (AP) Ledger:
- Double-entry accounts payable liability tracking derived from:
  * Approved Goods Received Notes (GRN_INVOICE) -> Credit AP liability
  * Supplier Invoices (INVOICE) -> Credit AP liability
  * Payment Vouchers to Suppliers (PAYMENT) -> Debit AP liability
  * Purchase Returns / Debit Notes (RETURN_DEBIT_NOTE) -> Debit AP liability
  * Supplier Credit Notes (SUPPLIER_CREDIT_NOTE) -> Debit AP liability
  * Credit Note Reversals (CREDIT_NOTE_REVERSAL) -> Credit AP liability
  * Payment Reversals (PAYMENT_REVERSAL) -> Credit AP liability
  * Audited Adjustments (ADJUSTMENT) -> Debit / Credit with mandatory reason
- Atomic concurrency safety using select_for_update() on Supplier records.
- Idempotency replay protection for reliable offline sync and network retry recovery.
- Full reconciliation parity: opening_balance + total_credits - total_debits == closing_balance == supplier.current_balance.
- Strict multi-tenant isolation and fine-grained role-based access control.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    Supplier,
    SupplierLedgerEntry,
    SupplierPostingType,
    SupplierStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.0000")

SUPPLIER_LEDGER_VIEW_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.STAFF_PHARMACIST,
    UserRole.INVENTORY_CLERK,
    UserRole.FINANCE_OFFICER,
    UserRole.AUDITOR,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "STAFF_PHARMACIST",
    "INVENTORY_CLERK",
    "FINANCE_OFFICER",
    "AUDITOR",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
    "PROCUREMENT_OFFICER",
}

SUPPLIER_LEDGER_POST_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.INVENTORY_CLERK,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "INVENTORY_CLERK",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
    "PROCUREMENT_OFFICER",
}

SUPPLIER_LEDGER_ADMIN_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}


def to_d4(val: Any) -> Decimal:
    """Quantizes amounts strictly to 4 decimal places."""
    if val is None:
        return DECIMAL_ZERO
    try:
        return Decimal(str(val)).quantize(Decimal("0.0001"))
    except Exception:
        return DECIMAL_ZERO


class SupplierPayableLedgerService:
    """
    Service layer governing immutable double-entry accounts payable ledger entries,
    running balance updates, and complete source-document reconciliation.
    """

    @classmethod
    def _assert_view_role(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_UNAUTHENTICATED",
                    "detail": "Authentication required to view supplier ledger.",
                }
            )
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if role in [UserRole.CASHIER, "CASHIER"] or role not in SUPPLIER_LEDGER_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and unauthorized staff are not permitted to view supplier payable ledgers.",
                    "message_en": "Cashier and unauthorized staff are not permitted to view supplier payable ledgers.",
                    "message_bn": "ক্যাশিয়ার বা অননুমোদিত কর্মী সরবরাহকারী দেনা লেজার দেখার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def _assert_post_role(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_UNAUTHENTICATED",
                    "detail": "Authentication required to post to supplier ledger.",
                }
            )
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if (
            role in [UserRole.CASHIER, "CASHIER", UserRole.STAFF_PHARMACIST, "STAFF_PHARMACIST"]
            or role not in SUPPLIER_LEDGER_POST_ROLES
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and staff pharmacist are not permitted to post to supplier payable ledgers.",
                    "message_en": "Cashier and staff pharmacist are not permitted to post to supplier payable ledgers.",
                    "message_bn": "ক্যাশিয়ার বা স্টাফ ফার্মাসিস্ট সরবরাহকারী দেনা লেজারে এন্ট্রি দেওয়ার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def _assert_admin_role(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_UNAUTHENTICATED",
                    "detail": "Authentication required for administrative ledger operations.",
                }
            )
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if role not in SUPPLIER_LEDGER_ADMIN_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Only owners, managers, and finance officers are permitted to perform ledger reversals or adjustments.",
                    "message_en": "Only owners, managers, and finance officers are permitted to perform ledger reversals or adjustments.",
                    "message_bn": "কেবলমাত্র মালিক, ব্যবস্থাপক ও অর্থ কর্মকর্তা লেজার রিভার্সাল বা সমন্বয় করার অনুমতিপ্রাপ্ত।",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant_id: str) -> Tenant:
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' not found."})
        if (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant account subscription is suspended. Procurement ledger access blocked.",
                    "message_en": "Tenant account subscription is suspended. Procurement ledger access blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। সরবরাহকারী লেজার মডিউল বন্ধ।",
                }
            )
        return tenant

    @classmethod
    def serialize_entry(cls, entry: SupplierLedgerEntry) -> Dict[str, Any]:
        return {
            "posting_id": entry.posting_id,
            "tenant_id": str(entry.tenant_id),
            "supplier_id": str(entry.supplier_id),
            "supplier_name": entry.supplier.name if entry.supplier else "",
            "branch_id": str(entry.branch_id) if entry.branch_id else None,
            "posting_type": entry.posting_type,
            "reference_number": entry.reference_number,
            "debit_amount": str(entry.debit_amount),
            "credit_amount": str(entry.credit_amount),
            "balance_after": str(entry.balance_after),
            "currency": entry.currency,
            "notes": entry.notes or "",
            "posted_by_id": str(entry.posted_by_id) if entry.posted_by_id else None,
            "posted_at": entry.posted_at.isoformat() if entry.posted_at else None,
        }

    @classmethod
    @transaction.atomic
    def post_entry(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        supplier_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Posts an atomic, immutable accounts payable ledger entry with exact Decimal(14, 4) precision.
        Applies idempotency check, updates supplier running balance, and writes audit log.
        """
        cls._assert_post_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        posting_type = str(data.get("posting_type", SupplierPostingType.GRN_INVOICE)).strip().upper()
        if posting_type not in SupplierPostingType.values:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_POSTING_TYPE",
                    "detail": f"Invalid posting type '{posting_type}'. Allowed: {list(SupplierPostingType.values)}",
                }
            )

        # Administrative check for manual ADJUSTMENT
        if posting_type == SupplierPostingType.ADJUSTMENT:
            cls._assert_admin_role(actor_user)
            reason = str(data.get("notes") or data.get("reason", "")).strip()
            if not reason:
                raise ValidationError(
                    {
                        "error_code": "ERR_MISSING_ADJUSTMENT_REASON",
                        "detail": "A mandatory explanation reason is required for manual audit adjustments.",
                        "message_en": "A mandatory explanation reason is required for manual audit adjustments.",
                        "message_bn": "অডিট সমন্বয়ের জন্য সুনির্দিষ্ট কারণ উল্লেখ করা আবশ্যক।",
                    }
                )

        ref_number = str(data.get("reference_number", "")).strip()
        if not ref_number:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_REFERENCE_NUMBER",
                    "detail": "Reference number (PO/GRN/Invoice/Voucher) is required.",
                }
            )

        debit = to_d4(data.get("debit_amount", "0.0000"))
        credit = to_d4(data.get("credit_amount", "0.0000"))

        if debit < DECIMAL_ZERO or credit < DECIMAL_ZERO:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_AMOUNT",
                    "detail": "Debit and credit amounts cannot be negative.",
                }
            )

        if debit == DECIMAL_ZERO and credit == DECIMAL_ZERO:
            raise ValidationError(
                {
                    "error_code": "ERR_ZERO_AMOUNT",
                    "detail": "Posting must have a non-zero debit or credit amount.",
                }
            )

        branch_id = data.get("branch_id")
        branch = None
        if branch_id:
            branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()

        idempotency_key = str(data.get("idempotency_key", "")).strip()

        # Concurrency safety: lock supplier row
        supplier = Supplier.objects.select_for_update().filter(supplier_id=supplier_id).first()
        if not supplier:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})
        if str(supplier.tenant_id) != str(tenant_id):
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "detail": "Cross-tenant supplier posting is strictly forbidden.",
                }
            )
        if supplier.status == SupplierStatus.BLACKLISTED:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUPPLIER_BLACKLISTED",
                    "detail": "Transactions barred for blacklisted suppliers.",
                }
            )

        # Idempotency check: prevent duplicate posting on network retry
        if idempotency_key:
            existing = SupplierLedgerEntry.objects.filter(
                tenant=tenant,
                supplier=supplier,
                notes__contains=f"[IDEMPOTENCY:{idempotency_key}]",
            ).first()
            if existing:
                logger.info("Idempotent replay detected for supplier %s, key %s", supplier_id, idempotency_key)
                return cls.serialize_entry(existing)

        # Also check direct duplicate reference for same posting type and amount
        existing_dup = SupplierLedgerEntry.objects.filter(
            tenant=tenant,
            supplier=supplier,
            posting_type=posting_type,
            reference_number=ref_number,
            debit_amount=debit,
            credit_amount=credit,
        ).first()
        if existing_dup and data.get("check_duplicate", False):
            logger.info("Duplicate posting suppressed for ref %s", ref_number)
            return cls.serialize_entry(existing_dup)

        # Balance delta calculation:
        # Credit increases payable liability owed to supplier (GRN receipts, invoices).
        # Debit decreases payable liability (payments, return debit notes, supplier credit notes).
        balance_delta = credit - debit
        new_balance = supplier.current_balance + balance_delta

        posting_id = f"sup-post-{uuid.uuid4().hex[:12]}"
        notes_str = str(data.get("notes", "")).strip()
        if idempotency_key:
            notes_str = f"{notes_str} [IDEMPOTENCY:{idempotency_key}]".strip()

        entry = SupplierLedgerEntry.objects.create(
            posting_id=posting_id,
            tenant=tenant,
            supplier=supplier,
            branch=branch,
            posting_type=posting_type,
            reference_number=ref_number,
            debit_amount=debit,
            credit_amount=credit,
            balance_after=new_balance,
            currency="BDT",
            notes=notes_str,
            posted_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        supplier.current_balance = new_balance
        supplier.save(update_fields=["current_balance", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SUPPLIER_PAYABLE_LEDGER_POSTED",
            resource_type="SupplierLedgerEntry",
            resource_id=entry.posting_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "supplier_id": supplier.supplier_id,
                "posting_type": posting_type,
                "reference_number": ref_number,
                "debit": str(debit),
                "credit": str(credit),
                "balance_after": str(new_balance),
            },
        )

        return cls.serialize_entry(entry)

    @classmethod
    @transaction.atomic
    def reverse_entry(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        supplier_id: str,
        posting_id: str,
        reason: str,
    ) -> Dict[str, Any]:
        """
        Creates an audited, compensating reversal ledger entry.
        Never destructively alters historical postings.
        """
        cls._assert_admin_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        clean_reason = str(reason).strip()
        if not clean_reason:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_REVERSAL_REASON",
                    "detail": "A mandatory explanation reason is required to reverse a ledger entry.",
                    "message_en": "A mandatory explanation reason is required to reverse a ledger entry.",
                    "message_bn": "লেজার এন্ট্রি রিভার্স করার জন্য সুনির্দিষ্ট কারণ আবশ্যক।",
                }
            )

        original = SupplierLedgerEntry.objects.filter(posting_id=posting_id).first()
        if not original:
            raise NotFound({"error_code": "ERR_LEDGER_ENTRY_NOT_FOUND", "detail": f"Posting '{posting_id}' not found."})
        if str(original.tenant_id) != str(tenant_id):
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "detail": "Cross-tenant ledger reversal forbidden.",
                }
            )

        # Check if already reversed
        reversal_marker = f"[REVERSAL_OF:{posting_id}]"
        already_reversed = SupplierLedgerEntry.objects.filter(
            tenant=tenant,
            supplier_id=supplier_id,
            notes__contains=reversal_marker,
        ).exists()
        if already_reversed:
            raise ValidationError(
                {
                    "error_code": "ERR_ALREADY_REVERSED",
                    "detail": f"Posting '{posting_id}' has already been reversed.",
                    "message_en": f"Posting '{posting_id}' has already been reversed.",
                    "message_bn": f"এন্ট্রি '{posting_id}' ইতিমধ্যে রিভার্স করা হয়েছে।",
                }
            )

        supplier = Supplier.objects.select_for_update().filter(supplier_id=supplier_id, tenant=tenant).first()
        if not supplier:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": "Supplier not found."})

        # Compensating entry inversion:
        # If original was a credit (increased liability), reversal is a debit (decreases liability).
        # If original was a debit (decreased liability), reversal is a credit (increases liability).
        rev_debit = original.credit_amount
        rev_credit = original.debit_amount

        rev_type = SupplierPostingType.ADJUSTMENT
        if original.posting_type == SupplierPostingType.PAYMENT:
            rev_type = SupplierPostingType.PAYMENT_REVERSAL
        elif original.posting_type == SupplierPostingType.SUPPLIER_CREDIT_NOTE:
            rev_type = SupplierPostingType.CREDIT_NOTE_REVERSAL

        new_balance = supplier.current_balance + rev_credit - rev_debit
        rev_posting_id = f"sup-rev-{uuid.uuid4().hex[:12]}"
        rev_notes = f"Compensating reversal of {original.posting_id}. Reason: {clean_reason} {reversal_marker}".strip()

        reversal_entry = SupplierLedgerEntry.objects.create(
            posting_id=rev_posting_id,
            tenant=tenant,
            supplier=supplier,
            branch=original.branch,
            posting_type=rev_type,
            reference_number=f"REV-{original.reference_number}",
            debit_amount=rev_debit,
            credit_amount=rev_credit,
            balance_after=new_balance,
            currency="BDT",
            notes=rev_notes,
            posted_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        supplier.current_balance = new_balance
        supplier.save(update_fields=["current_balance", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "admin@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SUPPLIER_PAYABLE_LEDGER_REVERSED",
            resource_type="SupplierLedgerEntry",
            resource_id=reversal_entry.posting_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "original_posting_id": original.posting_id,
                "reversal_posting_id": reversal_entry.posting_id,
                "reason": clean_reason,
                "balance_after": str(new_balance),
            },
        )

        return cls.serialize_entry(reversal_entry)

    @classmethod
    def list_entries(
        cls,
        *,
        tenant_id: str,
        supplier_id: str,
        actor_user: Optional[Any] = None,
        posting_type: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists chronological AP ledger entries with optional filtering."""
        if actor_user is not None:
            cls._assert_view_role(actor_user)
        cls._assert_tenant_active(tenant_id)

        supplier = Supplier.objects.filter(supplier_id=supplier_id).first()
        if not supplier:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})
        if str(supplier.tenant_id) != str(tenant_id):
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "detail": "Cross-tenant supplier ledger access is strictly forbidden.",
                }
            )

        qs = SupplierLedgerEntry.objects.filter(tenant_id=tenant_id, supplier=supplier)

        if posting_type:
            qs = qs.filter(posting_type=posting_type.upper())
        if start_date:
            qs = qs.filter(posted_at__date__gte=start_date)
        if end_date:
            qs = qs.filter(posted_at__date__lte=end_date)
        if search:
            q = search.strip()
            qs = qs.filter(Q(reference_number__icontains=q) | Q(notes__icontains=q))

        entries = qs.order_by("-posted_at")
        return [cls.serialize_entry(e) for e in entries]

    @classmethod
    def reconcile_ledger(
        cls,
        *,
        tenant_id: str,
        supplier_id: str,
        actor_user: Optional[Any] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Reconciles accounts payable ledger for a supplier:
        opening_balance + total_credits - total_debits == closing_balance == supplier.current_balance.
        """
        if actor_user is not None:
            cls._assert_view_role(actor_user)
        cls._assert_tenant_active(tenant_id)

        supplier = Supplier.objects.filter(supplier_id=supplier_id).first()
        if not supplier:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})
        if str(supplier.tenant_id) != str(tenant_id):
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "detail": "Cross-tenant supplier ledger access is strictly forbidden.",
                }
            )

        qs = SupplierLedgerEntry.objects.filter(tenant_id=tenant_id, supplier=supplier).order_by("posted_at")
        if start_date:
            qs = qs.filter(posted_at__date__gte=start_date)
        if end_date:
            qs = qs.filter(posted_at__date__lte=end_date)

        entries_list = list(qs)
        total_debits = sum((e.debit_amount for e in entries_list), DECIMAL_ZERO)
        total_credits = sum((e.credit_amount for e in entries_list), DECIMAL_ZERO)

        # Opening balance before the first entry in range
        first_entry = entries_list[0] if entries_list else None
        if first_entry:
            # opening = first.balance_after - first.credit + first.debit
            opening_balance = first_entry.balance_after - first_entry.credit_amount + first_entry.debit_amount
            last_entry = entries_list[-1]
            closing_balance = last_entry.balance_after
        else:
            opening_balance = supplier.current_balance
            closing_balance = supplier.current_balance

        calculated_closing = opening_balance + total_credits - total_debits
        is_reconciled = (
            abs(calculated_closing - closing_balance) < Decimal("0.0001")
            and abs(closing_balance - supplier.current_balance) < Decimal("0.0001")
            if not (start_date or end_date)
            else abs(calculated_closing - closing_balance) < Decimal("0.0001")
        )

        return {
            "supplier_id": supplier.supplier_id,
            "supplier_name": supplier.name,
            "tenant_id": str(tenant_id),
            "currency": "BDT",
            "opening_balance": str(opening_balance),
            "total_credits": str(total_credits),
            "total_debits": str(total_debits),
            "calculated_closing_balance": str(calculated_closing),
            "actual_closing_balance": str(closing_balance),
            "supplier_current_balance": str(supplier.current_balance),
            "is_reconciled": is_reconciled,
            "total_postings": len(entries_list),
            "reconciled_at": timezone.now().isoformat(),
        }
