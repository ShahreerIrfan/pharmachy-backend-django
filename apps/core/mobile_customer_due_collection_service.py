"""
backend/apps/core/mobile_customer_due_collection_service.py
Architecture Decision: ADR-0277 (P277).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service governing Mobile Customer Due Collection,
Owner Review, Digital Receipt Generation, Ledger Synchronization, and Immutable Audits.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    CustomerDueCollection,
    CustomerDueCollectionStatus,
    CustomerDueInvoiceAllocation,
    CustomerDueRecord,
    DueStatus,
    FinancialAccount,
    PaymentMethod,
    PosCustomer,
    SaleInvoice,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.pos_customer_due_service import PosCustomerDueService
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

MONEY_Q = Decimal("0.01")
DECIMAL_ZERO = Decimal("0.00")

# Roles authorized for mobile owner due review and collection
MOBILE_DUE_REVIEW_ROLES = {
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

# Strictly denied roles on mobile owner due endpoints
STAFF_DENIED_ROLES = {
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    "CASHIER",
    "STAFF_PHARMACIST",
    "INVENTORY_CLERK",
    "DISPENSER",
}


def to_money(val: Any) -> Decimal:
    """Quantizes decimal values strictly to 2 decimal places."""
    if val is None:
        return DECIMAL_ZERO
    try:
        return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return DECIMAL_ZERO


class MobileCustomerDueCollectionService:
    """
    Authoritative domain service for mobile owner customer due review,
    repayment collection, digital receipts, and audit verification.
    """

    @classmethod
    def assert_authorized_mobile_owner(cls, actor_user: Any) -> None:
        """Enforces that only authorized owner/manager personas can review and record mobile dues."""
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_UNAUTHENTICATED",
                    "detail": "Authentication required to access mobile due collections.",
                }
            )
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in MOBILE_DUE_REVIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and staff pharmacist are not authorized to access mobile owner due collections.",
                    "message_en": "Cashier and staff pharmacist are not authorized to access mobile owner due collections.",
                    "message_bn": "ক্যাশিয়ার বা স্টাফ ফার্মাসিস্ট মোবাইল ওনার ডিউ কালেকশন পরিচালনার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def assert_tenant_active(cls, tenant_id: str) -> Tenant:
        """Validates tenant existence and ensures active subscription."""
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
                    "detail": "Tenant account subscription is suspended. Mobile due collection blocked.",
                    "message_en": "Tenant account subscription is suspended. Mobile due collection blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। মোবাইল ডিউ কালেকশন বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    @classmethod
    def list_outstanding_dues(
        cls,
        *,
        actor_user: Any,
        tenant_id: str,
        branch_id: Optional[str] = None,
        search: Optional[str] = None,
        is_overdue: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """
        Lists open customer due receivables tailored for mobile owner view.
        """
        cls.assert_authorized_mobile_owner(actor_user)
        cls.assert_tenant_active(tenant_id)

        qs = CustomerDueRecord.objects.filter(
            tenant_id=tenant_id,
            status__in=[DueStatus.OUTSTANDING, DueStatus.PARTIAL],
            remaining_due_bdt__gt=DECIMAL_ZERO,
        ).select_related("branch", "customer", "invoice", "sale")

        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        today = timezone.now().date()
        if is_overdue is True:
            qs = qs.filter(due_date__lt=today)
        elif is_overdue is False:
            qs = qs.filter(Q(due_date__gte=today) | Q(due_date__isnull=True))

        if search:
            s = search.strip()
            qs = qs.filter(
                Q(customer_name__icontains=s)
                | Q(customer_phone__icontains=s)
                | Q(due_id__icontains=s)
                | Q(invoice__invoice_number__icontains=s)
            )

        results = []
        for due in qs.order_by("due_date", "-created_at"):
            overdue_days = 0
            is_od = False
            if due.due_date and due.due_date < today:
                overdue_days = (today - due.due_date).days
                is_od = True

            results.append(
                {
                    "due_id": due.due_id,
                    "tenant_id": str(due.tenant_id),
                    "branch_id": str(due.branch_id),
                    "branch_name": due.branch.name if due.branch else "",
                    "customer_id": str(due.customer_id) if due.customer_id else None,
                    "customer_name": due.customer_name,
                    "customer_phone": due.customer_phone,
                    "invoice_id": str(due.invoice_id) if due.invoice_id else None,
                    "invoice_number": due.invoice.invoice_number if due.invoice else (due.sale.order_number if due.sale else due.due_id),
                    "total_due_bdt": str(due.total_due_bdt),
                    "paid_amount_bdt": str(due.paid_amount_bdt),
                    "remaining_due_bdt": str(due.remaining_due_bdt),
                    "status": due.status,
                    "due_date": due.due_date.isoformat() if due.due_date else None,
                    "is_overdue": is_od,
                    "overdue_days": overdue_days,
                    "notes": due.notes,
                    "created_at": due.created_at.isoformat() if due.created_at else None,
                }
            )

        return results

    @classmethod
    def get_due_summary(
        cls,
        *,
        actor_user: Any,
        tenant_id: str,
        branch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Returns high-level KPI summary of customer dues for mobile dashboard.
        """
        cls.assert_authorized_mobile_owner(actor_user)
        cls.assert_tenant_active(tenant_id)

        qs = CustomerDueRecord.objects.filter(
            tenant_id=tenant_id,
            status__in=[DueStatus.OUTSTANDING, DueStatus.PARTIAL],
            remaining_due_bdt__gt=DECIMAL_ZERO,
        )
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        today = timezone.now().date()
        total_outstanding = qs.aggregate(tot=Sum("remaining_due_bdt"))["tot"] or DECIMAL_ZERO

        overdue_qs = qs.filter(due_date__lt=today)
        total_overdue = overdue_qs.aggregate(tot=Sum("remaining_due_bdt"))["tot"] or DECIMAL_ZERO
        overdue_count = overdue_qs.count()
        total_due_accounts = qs.count()

        # Today's collected dues
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_collections = CustomerDueCollection.objects.filter(
            tenant_id=tenant_id,
            status=CustomerDueCollectionStatus.CAPTURED,
            collected_at__gte=today_start,
        )
        if branch_id:
            today_collections = today_collections.filter(branch_id=branch_id)
        today_collected = today_collections.aggregate(tot=Sum("amount_bdt"))["tot"] or DECIMAL_ZERO

        return {
            "tenant_id": str(tenant_id),
            "branch_id": str(branch_id) if branch_id else None,
            "currency": "BDT",
            "total_outstanding_due_bdt": str(to_money(total_outstanding)),
            "total_overdue_due_bdt": str(to_money(total_overdue)),
            "total_due_accounts_count": total_due_accounts,
            "overdue_accounts_count": overdue_count,
            "today_collected_due_bdt": str(to_money(today_collected)),
            "as_of": timezone.now().isoformat(),
        }

    @classmethod
    @transaction.atomic
    def record_mobile_due_collection(
        cls,
        *,
        actor_user: Any,
        tenant_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Records full or partial customer due collection from mobile app.
        Enforces online-only connectivity, overpayment prevention, concurrency locking,
        ledger synchronization, and digital receipt generation.
        """
        cls.assert_authorized_mobile_owner(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        # 1. Offline protection
        if bool(data.get("is_offline", False)):
            raise ValidationError(
                {
                    "error_code": "ERR_OFFLINE_UNAVAILABLE",
                    "detail": "Mobile due collection requires online connectivity to verify live AR ledger state.",
                    "message_en": "Mobile due collection requires online connectivity to verify live AR ledger state.",
                    "message_bn": "লাইভ লেজার সমন্বয়ের জন্য মোবাইল ডিউ কালেকশনে অনলাইন ইন্টারনেট সংযোগ আবশ্যক।",
                }
            )

        # 2. Idempotency check
        idempotency_key = str(data.get("idempotency_key", "")).strip()
        if idempotency_key:
            existing = CustomerDueCollection.objects.filter(tenant=tenant, idempotency_key=idempotency_key).first()
            if existing:
                res = cls.build_digital_receipt(existing)
                res["is_idempotent_replay"] = True
                return res

        # 3. Resolve target CustomerDueRecord
        due_identifier = str(data.get("due_id") or data.get("invoice_id") or data.get("invoice_identifier") or "").strip()
        if not due_identifier:
            raise ValidationError(
                {
                    "error_code": "ERR_DUE_IDENTIFIER_REQUIRED",
                    "detail": "Due ID or Invoice ID is mandatory to record customer due collection.",
                }
            )

        due_rec = (
            CustomerDueRecord.objects.select_for_update()
            .filter(
                Q(tenant=tenant)
                & (
                    Q(due_id=due_identifier)
                    | Q(invoice__invoice_id=due_identifier)
                    | Q(invoice__invoice_number=due_identifier)
                    | Q(sale__sale_id=due_identifier)
                    | Q(sale__order_number=due_identifier)
                )
            )
            .first()
        )

        if not due_rec:
            # Check if this identifier belongs to a foreign tenant
            foreign_check = CustomerDueRecord.objects.filter(due_id=due_identifier).first()
            if foreign_check and str(foreign_check.tenant_id) != str(tenant_id):
                raise PermissionDenied(
                    {
                        "error_code": "ERR_CROSS_TENANT_ACCESS",
                        "detail": "Cross-tenant customer due collection is strictly forbidden.",
                    }
                )
            raise NotFound(
                {
                    "error_code": "ERR_DUE_RECORD_NOT_FOUND",
                    "detail": f"Customer due record '{due_identifier}' not found.",
                }
            )

        # 4. Duplicate settlement check
        if due_rec.status in (DueStatus.SETTLED, DueStatus.WRITTEN_OFF) or due_rec.remaining_due_bdt <= DECIMAL_ZERO:
            raise ValidationError(
                {
                    "error_code": "ERR_DUE_ALREADY_SETTLED",
                    "detail": f"Customer due '{due_rec.due_id}' is already {due_rec.status} (remaining 0.00 BDT).",
                    "message_en": f"Customer due is already settled.",
                    "message_bn": f"এই কাস্টমার বাকিটি ইতিমধ্যে সম্পূর্ণ পরিশোধিত।",
                }
            )

        # 5. Amount validation and overpayment check
        amount = to_money(data.get("amount_bdt"))
        if amount <= DECIMAL_ZERO:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_PAYMENT_AMOUNT",
                    "detail": "Collection amount must be strictly greater than 0.00 BDT.",
                }
            )

        if amount > due_rec.remaining_due_bdt:
            raise ValidationError(
                {
                    "error_code": "ERR_DUE_OVERPAYMENT",
                    "detail": (
                        f"Collection amount ({amount:.2f} BDT) exceeds remaining due ({due_rec.remaining_due_bdt:.2f} BDT). "
                        "Overpayments are strictly prohibited."
                    ),
                    "amount_bdt": str(amount),
                    "remaining_due_bdt": str(due_rec.remaining_due_bdt),
                    "message_en": "Collection amount exceeds remaining due. Overpayments are strictly prohibited.",
                    "message_bn": "আদায়ের পরিমাণ অবশিষ্ট বাকির চেয়ে বেশি। ওভারপেমেন্ট গ্রহণযোগ্য নয়।",
                }
            )

        # 6. Tender type validation
        tender_type = str(data.get("tender_type", PaymentMethod.CASH)).strip().upper()
        if tender_type == PaymentMethod.DUE:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_TENDER_TYPE",
                    "detail": "Cannot settle customer due using DUE tender.",
                }
            )
        if tender_type not in PaymentMethod.values:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_TENDER_TYPE",
                    "detail": f"Invalid tender type '{tender_type}'.",
                }
            )

        gateway_ref = str(data.get("gateway_reference", "")).strip()
        if tender_type in [PaymentMethod.BKASH, PaymentMethod.NAGAD, PaymentMethod.ROCKET, PaymentMethod.UPAY, PaymentMethod.CARD, PaymentMethod.BANK_TRANSFER]:
            if not gateway_ref or len(gateway_ref) < 4:
                raise ValidationError(
                    {
                        "error_code": "ERR_MISSING_GATEWAY_REFERENCE",
                        "detail": f"{tender_type} payment requires a valid gateway transaction reference (min 4 chars).",
                        "message_en": "Valid gateway transaction reference is required.",
                        "message_bn": "ডিজিটাল লেনদেনের জন্য সঠিক রেফারেন্স নম্বর আবশ্যক।",
                    }
                )

        # 7. Update CustomerDueRecord balances
        prev_remaining = due_rec.remaining_due_bdt
        due_rec.paid_amount_bdt = to_money(due_rec.paid_amount_bdt + amount)
        due_rec.remaining_due_bdt = to_money(due_rec.remaining_due_bdt - amount)
        if due_rec.remaining_due_bdt <= DECIMAL_ZERO:
            due_rec.remaining_due_bdt = DECIMAL_ZERO
            due_rec.status = DueStatus.SETTLED
        else:
            due_rec.status = DueStatus.PARTIAL
        due_rec.save()

        # 8. Update PosCustomer balance if linked
        if due_rec.customer:
            cust = PosCustomer.objects.select_for_update().filter(pk=due_rec.customer_id).first()
            if cust:
                cust.current_due_bdt = max(DECIMAL_ZERO, to_money(cust.current_due_bdt - amount))
                cust.save(update_fields=["current_due_bdt", "updated_at"])

        # 9. Create CustomerDueCollection record
        collection_id = f"MCOL-{uuid.uuid4().hex[:10].upper()}"
        col = CustomerDueCollection(
            collection_id=collection_id,
            tenant=tenant,
            branch=due_rec.branch,
            due_record=due_rec,
            customer=due_rec.customer,
            sale=due_rec.sale,
            invoice=due_rec.invoice,
            tender_type=tender_type,
            amount_bdt=amount,
            gateway_reference=gateway_ref,
            verification_channel="MOBILE_OWNER_APP",
            idempotency_key=idempotency_key,
            status=CustomerDueCollectionStatus.CAPTURED,
            notes=str(data.get("notes", "")).strip(),
            collected_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            collected_at=timezone.now(),
        )
        col.save()

        # 10. Create CustomerDueInvoiceAllocation for itemized penny-parity
        CustomerDueInvoiceAllocation.objects.create(
            allocation_id=f"MALLOC-{uuid.uuid4().hex[:8].upper()}",
            tenant=tenant,
            branch=due_rec.branch,
            collection=col,
            due_record=due_rec,
            invoice=due_rec.invoice,
            sale=due_rec.sale,
            allocated_amount_bdt=amount,
            previous_remaining_due_bdt=prev_remaining,
            new_remaining_due_bdt=due_rec.remaining_due_bdt,
        )

        # 11. Adjust FinancialAccount balance if clearing account linked
        fin_acc_id = data.get("financial_account_id")
        if fin_acc_id:
            fin_acc = FinancialAccount.objects.select_for_update().filter(account_id=fin_acc_id, tenant=tenant).first()
            if fin_acc:
                fin_acc.current_balance += amount
                fin_acc.save(update_fields=["current_balance", "updated_at"])

        # 12. Create immutable AuditLogEntry
        receipt_num = f"REC-DUE-{timezone.now().strftime('%Y%m%d')}-{collection_id[-6:]}"
        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="MOBILE_DUE_COLLECTION",
            resource_type="CustomerDueCollection",
            resource_id=col.collection_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "collection_id": col.collection_id,
                "due_id": due_rec.due_id,
                "amount_collected_bdt": str(amount),
                "remaining_due_bdt": str(due_rec.remaining_due_bdt),
                "receipt_number": receipt_num,
                "customer_name": due_rec.customer_name,
                "tender_type": tender_type,
            },
        )

        return cls.build_digital_receipt(col, receipt_num=receipt_num)

    @classmethod
    def build_digital_receipt(
        cls,
        col: CustomerDueCollection,
        receipt_num: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Builds formatted digital receipt payload for mobile display and audit."""
        due = col.due_record
        rec_number = receipt_num or f"REC-DUE-{col.collected_at.strftime('%Y%m%d')}-{col.collection_id[-6:]}"
        collector_name = getattr(col.collected_by, "full_name", None) or getattr(col.collected_by, "email", "Authorized Owner")

        return {
            "success": True,
            "collection_id": col.collection_id,
            "receipt_number": rec_number,
            "tenant_id": str(col.tenant_id),
            "branch_id": str(col.branch_id),
            "branch_name": col.branch.name if col.branch else "",
            "due_id": due.due_id if due else "",
            "customer_id": str(col.customer_id) if col.customer_id else None,
            "customer_name": due.customer_name if due else "",
            "customer_phone": due.customer_phone if due else "",
            "invoice_number": due.invoice.invoice_number if (due and due.invoice) else (due.sale.order_number if (due and due.sale) else ""),
            "tender_type": col.tender_type,
            "amount_collected_bdt": str(col.amount_bdt),
            "remaining_due_bdt": str(due.remaining_due_bdt) if due else "0.00",
            "due_status": due.status if due else "SETTLED",
            "gateway_reference": col.gateway_reference,
            "collected_by_name": collector_name,
            "collected_at": col.collected_at.isoformat() if col.collected_at else timezone.now().isoformat(),
            "notes": col.notes,
            "message_en": f"Successfully collected {col.amount_bdt:.2f} BDT. Digital receipt #{rec_number} generated.",
            "message_bn": f"{col.amount_bdt:.2f} টাকা সফলভাবে আদায় হয়েছে। ডিজিটাল রসিদ #{rec_number} তৈরি করা হয়েছে।",
        }

    @classmethod
    def get_digital_receipt(
        cls,
        *,
        actor_user: Any,
        tenant_id: str,
        collection_id: str,
    ) -> Dict[str, Any]:
        """Retrieves digital receipt for a specific collection."""
        cls.assert_authorized_mobile_owner(actor_user)
        cls.assert_tenant_active(tenant_id)

        col = CustomerDueCollection.objects.filter(tenant_id=tenant_id, collection_id=collection_id).select_related("due_record", "branch", "collected_by").first()
        if not col:
            raise NotFound({"error_code": "ERR_COLLECTION_NOT_FOUND", "detail": f"Collection '{collection_id}' not found."})

        return cls.build_digital_receipt(col)
