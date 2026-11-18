"""
backend/apps/core/vendor_payment_allocation_service.py
Architecture Decision: ADR-0273 (P273).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service governing Vendor Payment Disbursements & Invoice Allocations:
- Allocation of single or multi-invoice disbursements among unpaid/partially paid supplier invoices.
- Strict anti-overpayment guards preventing allocation exceeding invoice remaining unpaid balance.
- Total allocations capped at total disbursement amount with unapplied balances tracked as advances.
- Fine-grained role-based access control (RBAC):
  * Allowed to disburse & allocate: PHARMACY_OWNER, BRANCH_MANAGER, FINANCE_OFFICER, SUPERADMIN, INVENTORY_CLERK.
  * Explicitly forbidden: CASHIER and STAFF_PHARMACIST (HTTP 403 Forbidden).
- Strict multi-tenant isolation (Cross-tenant supplier or invoice access raises HTTP 403 Forbidden).
- Idempotency replay protection for safe offline sync, mobile network drops, and POS client retries.
- Full ledger parity: posts accounts payable debit to SupplierLedgerEntry via SupplierPayableLedgerService.
- Immutable audit log tracking with AuditLogEntry.
"""

from __future__ import annotations

from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional
import uuid

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    Supplier,
    SupplierInvoice,
    SupplierPostingType,
    SupplierStatus,
    Tenant,
    TenantLifecycleStatus,
    VendorPayment,
    VendorPaymentInvoiceAllocation,
    VendorPaymentStatus,
)
from apps.core.supplier_payable_ledger_service import SupplierPayableLedgerService
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.0000")

VENDOR_PAYMENT_VIEW_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
    UserRole.INVENTORY_CLERK,
    UserRole.AUDITOR,
    UserRole.STAFF_PHARMACIST,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "FINANCE_OFFICER",
    "INVENTORY_CLERK",
    "AUDITOR",
    "STAFF_PHARMACIST",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
    "PROCUREMENT_OFFICER",
}

VENDOR_PAYMENT_MANAGE_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    UserRole.INVENTORY_CLERK,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "INVENTORY_CLERK",
    "OWNER_ADMIN",
    "ADMIN",
    "PROCUREMENT_OFFICER",
}


def to_d4(val: Any) -> Decimal:
    """Quantizes amounts strictly to 4 decimal places."""
    if val is None:
        return DECIMAL_ZERO
    try:
        return Decimal(str(val)).quantize(Decimal("0.0001"))
    except Exception:
        return DECIMAL_ZERO


class VendorPaymentAllocationService:
    """
    Authoritative service governing supplier vendor payment disbursement and invoice allocation.
    """

    @classmethod
    def _assert_view_role(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_UNAUTHENTICATED",
                    "detail": "Authentication required to view vendor payments.",
                }
            )
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if role in [UserRole.CASHIER, "CASHIER"] or role not in VENDOR_PAYMENT_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and unauthorized staff are not permitted to view vendor payments.",
                    "message_en": "Cashier and unauthorized staff are not permitted to view vendor payments.",
                    "message_bn": "ক্যাশিয়ার বা অননুমোদিত কর্মী সরবরাহকারী পেমেন্ট বরাদ্দ দেখার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def _assert_manage_role(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_UNAUTHENTICATED",
                    "detail": "Authentication required to manage vendor payments and allocations.",
                }
            )
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if (
            role in [UserRole.CASHIER, "CASHIER", UserRole.STAFF_PHARMACIST, "STAFF_PHARMACIST"]
            or role not in VENDOR_PAYMENT_MANAGE_ROLES
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and staff pharmacist are not permitted to disburse or allocate vendor payments.",
                    "message_en": "Cashier and staff pharmacist are not permitted to disburse or allocate vendor payments.",
                    "message_bn": "ক্যাশিয়ার বা স্টাফ ফার্মাসিস্ট সরবরাহকারী পেমেন্ট বরাদ্দ বা পরিশোধ করার অনুমতিপ্রাপ্ত নন।",
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
                    "detail": "Tenant account subscription is suspended. Vendor payment operations blocked.",
                    "message_en": "Tenant account subscription is suspended. Vendor payment operations blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। সরবরাহকারী পেমেন্ট বরাদ্দ ব্লক করা হয়েছে।",
                }
            )
        return tenant

    @classmethod
    def calculate_invoice_unpaid_balance(cls, invoice: SupplierInvoice) -> Dict[str, Any]:
        """
        Dynamically calculates active allocations and exact remaining unpaid balance for an invoice.
        """
        total_amount = to_d4(invoice.total_amount)
        allocated_agg = (
            invoice.payment_allocations.filter(is_reversed=False).aggregate(total=Sum("allocated_amount"))
        )
        total_allocated = to_d4(allocated_agg.get("total") or DECIMAL_ZERO)
        unpaid_balance = max(DECIMAL_ZERO, total_amount - total_allocated)

        if total_allocated == DECIMAL_ZERO:
            settlement_status = "UNPAID"
        elif unpaid_balance == DECIMAL_ZERO:
            settlement_status = "SETTLED"
        else:
            settlement_status = "PARTIALLY_PAID"

        return {
            "invoice_id": invoice.invoice_id,
            "invoice_number": invoice.invoice_number,
            "total_amount": str(total_amount),
            "allocated_amount": str(total_allocated),
            "unpaid_balance": str(unpaid_balance),
            "settlement_status": settlement_status,
        }

    @classmethod
    def get_unpaid_invoices(
        cls,
        tenant_id: str,
        actor_user: Any,
        supplier_id: str,
        include_settled: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Retrieves all supplier invoices for the given supplier and tenant, annotated with unpaid balances.
        """
        cls._assert_view_role(actor_user)
        cls._assert_tenant_active(tenant_id)

        supplier = Supplier.objects.filter(supplier_id=supplier_id).first()
        if not supplier:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})
        if str(supplier.tenant_id) != str(tenant_id):
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "detail": "Cross-tenant supplier access is strictly forbidden.",
                }
            )

        qs = SupplierInvoice.objects.filter(tenant_id=tenant_id, supplier_id=supplier_id, is_active=True).order_by("invoice_date")
        results = []
        for inv in qs:
            balance_info = cls.calculate_invoice_unpaid_balance(inv)
            unpaid = Decimal(balance_info["unpaid_balance"])
            if include_settled or unpaid > DECIMAL_ZERO:
                balance_info["invoice_date"] = inv.invoice_date.isoformat() if inv.invoice_date else None
                balance_info["due_date"] = inv.due_date.isoformat() if inv.due_date else None
                balance_info["goods_receipt_number"] = inv.goods_receipt_number or ""
                results.append(balance_info)

        return results

    @classmethod
    def serialize_payment(cls, payment: VendorPayment) -> Dict[str, Any]:
        """Serializes VendorPayment and its itemized allocations."""
        allocations = []
        for alloc in payment.allocations.filter(is_reversed=False).select_related("invoice"):
            allocations.append(
                {
                    "allocation_id": alloc.allocation_id,
                    "invoice_id": alloc.invoice_id,
                    "invoice_number": alloc.invoice.invoice_number if alloc.invoice else "",
                    "allocated_amount": str(alloc.allocated_amount),
                    "previous_unpaid_amount": str(alloc.previous_unpaid_amount),
                    "remaining_unpaid_amount": str(alloc.remaining_unpaid_amount),
                    "is_reversed": alloc.is_reversed,
                    "notes": alloc.notes,
                    "created_at": alloc.created_at.isoformat() if alloc.created_at else None,
                }
            )

        return {
            "payment_id": payment.payment_id,
            "payment_number": payment.payment_number,
            "tenant_id": str(payment.tenant_id),
            "branch_id": str(payment.branch_id) if payment.branch_id else None,
            "supplier_id": str(payment.supplier_id),
            "supplier_name": payment.supplier.name if payment.supplier else "",
            "payment_date": payment.payment_date.isoformat() if payment.payment_date else None,
            "payment_method": payment.payment_method,
            "reference_number": payment.reference_number,
            "total_amount": str(payment.total_amount),
            "allocated_amount": str(payment.allocated_amount),
            "unallocated_amount": str(payment.unallocated_amount),
            "status": payment.status,
            "ledger_entry_id": str(payment.ledger_entry_id) if payment.ledger_entry_id else None,
            "notes": payment.notes,
            "idempotency_key": payment.idempotency_key,
            "created_by_id": str(payment.created_by_id) if payment.created_by_id else None,
            "created_at": payment.created_at.isoformat() if payment.created_at else None,
            "allocations": allocations,
        }

    @classmethod
    @transaction.atomic
    def create_and_allocate_payment(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        supplier_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Disburses a vendor payment and allocates amounts across specified supplier invoices.
        Enforces idempotency, RBAC, tenant isolation, anti-overpayment limits, and AP ledger posting.
        """
        cls._assert_manage_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        # Supplier check with cross-tenant isolation
        supplier = Supplier.objects.select_for_update().filter(supplier_id=supplier_id).first()
        if not supplier:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})
        if str(supplier.tenant_id) != str(tenant_id):
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "detail": "Cross-tenant supplier access is strictly forbidden.",
                }
            )
        if supplier.status == SupplierStatus.BLACKLISTED:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUPPLIER_BLACKLISTED",
                    "detail": "Transactions are barred for blacklisted suppliers.",
                }
            )

        # Idempotency check
        idempotency_key = str(data.get("idempotency_key", "")).strip()
        if idempotency_key:
            existing_payment = (
                VendorPayment.objects.filter(tenant=tenant, idempotency_key=idempotency_key)
                .select_related("supplier")
                .first()
            )
            if existing_payment:
                logger.info("Idempotent replay detected for vendor payment key '%s'", idempotency_key)
                res = cls.serialize_payment(existing_payment)
                res["is_idempotent_replay"] = True
                return res

        # Validate total disbursement amount
        total_amount = to_d4(data.get("total_amount", "0.0000"))
        if total_amount <= DECIMAL_ZERO:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_PAYMENT_AMOUNT",
                    "detail": "Total payment disbursement amount must be greater than zero.",
                    "message_en": "Total payment disbursement amount must be greater than zero.",
                    "message_bn": "মোট পেমেন্ট অংক অবশ্যই শূন্যের চেয়ে বেশি হতে হবে।",
                }
            )

        branch_id = data.get("branch_id")
        branch = None
        if branch_id:
            branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()

        payment_method = str(data.get("payment_method", "BANK_TRANSFER")).strip().upper()
        reference_number = str(data.get("reference_number", "")).strip()
        notes = str(data.get("notes", "")).strip()
        payment_date_val = data.get("payment_date")
        payment_date = timezone.now().date()
        if payment_date_val:
            try:
                import datetime
                if isinstance(payment_date_val, str):
                    payment_date = datetime.date.fromisoformat(payment_date_val)
                elif isinstance(payment_date_val, datetime.date):
                    payment_date = payment_date_val
            except Exception:
                pass

        allocations_data = data.get("allocations", [])
        if not isinstance(allocations_data, list):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_ALLOCATIONS",
                    "detail": "Allocations must be provided as a list of invoice allocation items.",
                }
            )

        # Validate allocations
        total_allocated = DECIMAL_ZERO
        validated_allocations = []

        for item in allocations_data:
            inv_id = item.get("invoice_id")
            if not inv_id:
                raise ValidationError(
                    {
                        "error_code": "ERR_MISSING_INVOICE_ID",
                        "detail": "Each allocation entry must specify a valid 'invoice_id'.",
                    }
                )

            inv_obj = SupplierInvoice.objects.select_for_update().filter(invoice_id=inv_id).first()
            if not inv_obj:
                raise NotFound(
                    {
                        "error_code": "ERR_INVOICE_NOT_FOUND",
                        "detail": f"Supplier invoice '{inv_id}' not found.",
                    }
                )

            if str(inv_obj.tenant_id) != str(tenant_id):
                raise PermissionDenied(
                    {
                        "error_code": "ERR_CROSS_TENANT_ACCESS",
                        "detail": "Cross-tenant invoice access is strictly forbidden.",
                    }
                )

            if str(inv_obj.supplier_id) != str(supplier_id):
                raise ValidationError(
                    {
                        "error_code": "ERR_INVOICE_SUPPLIER_MISMATCH",
                        "detail": f"Invoice '{inv_obj.invoice_number}' does not belong to supplier '{supplier.name}'.",
                    }
                )

            amt = to_d4(item.get("allocated_amount", "0.0000"))
            if amt <= DECIMAL_ZERO:
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_ALLOCATION_AMOUNT",
                        "detail": f"Allocation amount for invoice '{inv_obj.invoice_number}' must be greater than zero.",
                    }
                )

            bal_info = cls.calculate_invoice_unpaid_balance(inv_obj)
            current_unpaid = Decimal(bal_info["unpaid_balance"])

            if amt > current_unpaid:
                raise ValidationError(
                    {
                        "error_code": "ERR_ALLOCATION_EXCEEDS_INVOICE_BALANCE",
                        "detail": (
                            f"Allocated amount {amt} exceeds current unpaid balance {current_unpaid} "
                            f"for invoice '{inv_obj.invoice_number}'."
                        ),
                        "invoice_id": inv_id,
                        "unpaid_balance": str(current_unpaid),
                        "requested_amount": str(amt),
                    }
                )

            total_allocated += amt
            validated_allocations.append(
                {
                    "invoice": inv_obj,
                    "allocated_amount": amt,
                    "previous_unpaid": current_unpaid,
                    "remaining_unpaid": current_unpaid - amt,
                    "notes": str(item.get("notes", "")).strip(),
                }
            )

        # Check total allocated against payment total
        if total_allocated > total_amount:
            raise ValidationError(
                {
                    "error_code": "ERR_TOTAL_ALLOCATION_EXCEEDS_PAYMENT",
                    "detail": (
                        f"Sum of invoice allocations ({total_allocated}) cannot exceed "
                        f"total payment disbursement amount ({total_amount})."
                    ),
                    "total_allocated": str(total_allocated),
                    "total_payment": str(total_amount),
                }
            )

        unallocated_advance = total_amount - total_allocated

        # Create VendorPayment record
        payment_id = f"vpay-{uuid.uuid4().hex[:12]}"
        now_str = timezone.now().strftime("%Y%m%d")
        payment_number = f"VPAY-{now_str}-{uuid.uuid4().hex[:6].upper()}"

        status_val = VendorPaymentStatus.POSTED
        if total_allocated > DECIMAL_ZERO:
            if total_allocated == total_amount:
                status_val = VendorPaymentStatus.FULLY_ALLOCATED
            else:
                status_val = VendorPaymentStatus.PARTIALLY_ALLOCATED

        payment = VendorPayment(
            payment_id=payment_id,
            tenant=tenant,
            branch=branch,
            supplier=supplier,
            payment_number=payment_number,
            payment_date=payment_date,
            payment_method=payment_method,
            reference_number=reference_number,
            total_amount=total_amount,
            allocated_amount=total_allocated,
            unallocated_amount=unallocated_advance,
            status=status_val,
            notes=notes,
            idempotency_key=idempotency_key,
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )
        payment.save()

        # Create allocation records
        for valoc in validated_allocations:
            alloc_rec = VendorPaymentInvoiceAllocation(
                allocation_id=f"vpalloc-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                branch=branch,
                payment=payment,
                invoice=valoc["invoice"],
                allocated_amount=valoc["allocated_amount"],
                previous_unpaid_amount=valoc["previous_unpaid"],
                remaining_unpaid_amount=valoc["remaining_unpaid"],
                notes=valoc["notes"],
            )
            alloc_rec.save()

        # Post debit entry to Accounts Payable Ledger via SupplierPayableLedgerService
        ledger_res = SupplierPayableLedgerService.post_entry(
            tenant_id=tenant_id,
            actor_user=actor_user,
            supplier_id=supplier_id,
            data={
                "posting_type": SupplierPostingType.PAYMENT,
                "reference_number": payment.payment_number,
                "debit_amount": str(total_amount),
                "credit_amount": "0.0000",
                "notes": (
                    f"Disbursement voucher {payment.payment_number} settled via {payment_method}. "
                    f"Allocated: {total_allocated} BDT across {len(validated_allocations)} invoices. "
                    f"Unapplied balance: {unallocated_advance} BDT."
                ),
                "branch_id": branch.branch_id if branch else None,
                "idempotency_key": f"sup-ap-vpay-{payment.payment_id}",
            },
        )

        posting_id = ledger_res.get("posting_id")
        if posting_id:
            payment.ledger_entry_id = posting_id
            payment.save(update_fields=["ledger_entry"])

        # Audit log entry
        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="VENDOR_PAYMENT_ALLOCATED",
            resource_type="VendorPayment",
            resource_id=payment.payment_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "payment_number": payment.payment_number,
                "supplier_id": supplier.supplier_id,
                "supplier_name": supplier.name,
                "total_amount": str(total_amount),
                "allocated_amount": str(total_allocated),
                "unallocated_amount": str(unallocated_advance),
                "allocations_count": len(validated_allocations),
                "ledger_posting_id": posting_id,
            },
        )

        return cls.serialize_payment(payment)

    @classmethod
    @transaction.atomic
    def allocate_unapplied_balance(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        payment_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Allocates unapplied advance balance from an existing payment to pending invoices.
        Does not post an additional cash debit to AP ledger because the advance was already debited.
        """
        cls._assert_manage_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        payment = VendorPayment.objects.select_for_update().filter(payment_id=payment_id).first()
        if not payment:
            raise NotFound({"error_code": "ERR_PAYMENT_NOT_FOUND", "detail": f"Vendor payment '{payment_id}' not found."})
        if str(payment.tenant_id) != str(tenant_id):
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "detail": "Cross-tenant payment access is strictly forbidden.",
                }
            )

        if payment.status == VendorPaymentStatus.REVERSED:
            raise ValidationError(
                {
                    "error_code": "ERR_PAYMENT_REVERSED",
                    "detail": "Cannot allocate amounts from a reversed payment.",
                }
            )

        available_advance = payment.unallocated_amount
        if available_advance <= DECIMAL_ZERO:
            raise ValidationError(
                {
                    "error_code": "ERR_NO_UNAPPLIED_BALANCE",
                    "detail": "Payment has no remaining unapplied balance to allocate.",
                }
            )

        allocations_data = data.get("allocations", [])
        if not allocations_data or not isinstance(allocations_data, list):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_ALLOCATIONS",
                    "detail": "At least one allocation item is required.",
                }
            )

        total_new_allocated = DECIMAL_ZERO
        validated_allocations = []

        for item in allocations_data:
            inv_id = item.get("invoice_id")
            inv_obj = SupplierInvoice.objects.select_for_update().filter(invoice_id=inv_id).first()
            if not inv_obj:
                raise NotFound({"error_code": "ERR_INVOICE_NOT_FOUND", "detail": f"Invoice '{inv_id}' not found."})
            if str(inv_obj.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access denied."})
            if str(inv_obj.supplier_id) != str(payment.supplier_id):
                raise ValidationError(
                    {
                        "error_code": "ERR_INVOICE_SUPPLIER_MISMATCH",
                        "detail": f"Invoice does not belong to supplier of this payment voucher.",
                    }
                )

            amt = to_d4(item.get("allocated_amount", "0.0000"))
            if amt <= DECIMAL_ZERO:
                raise ValidationError({"error_code": "ERR_INVALID_ALLOCATION_AMOUNT", "detail": "Amount must be > 0."})

            bal_info = cls.calculate_invoice_unpaid_balance(inv_obj)
            current_unpaid = Decimal(bal_info["unpaid_balance"])
            if amt > current_unpaid:
                raise ValidationError(
                    {
                        "error_code": "ERR_ALLOCATION_EXCEEDS_INVOICE_BALANCE",
                        "detail": f"Allocation {amt} exceeds current unpaid balance {current_unpaid}.",
                    }
                )

            total_new_allocated += amt
            validated_allocations.append(
                {
                    "invoice": inv_obj,
                    "allocated_amount": amt,
                    "previous_unpaid": current_unpaid,
                    "remaining_unpaid": current_unpaid - amt,
                    "notes": str(item.get("notes", "")).strip(),
                }
            )

        if total_new_allocated > available_advance:
            raise ValidationError(
                {
                    "error_code": "ERR_ALLOCATION_EXCEEDS_UNAPPLIED_BALANCE",
                    "detail": f"Allocation {total_new_allocated} exceeds available unapplied balance {available_advance}.",
                }
            )

        # Update payment
        payment.allocated_amount += total_new_allocated
        payment.unallocated_amount = max(DECIMAL_ZERO, payment.total_amount - payment.allocated_amount)
        if payment.allocated_amount >= payment.total_amount:
            payment.status = VendorPaymentStatus.FULLY_ALLOCATED
        else:
            payment.status = VendorPaymentStatus.PARTIALLY_ALLOCATED
        payment.save()

        # Save allocation records
        for valoc in validated_allocations:
            alloc_rec = VendorPaymentInvoiceAllocation(
                allocation_id=f"vpalloc-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                branch=payment.branch,
                payment=payment,
                invoice=valoc["invoice"],
                allocated_amount=valoc["allocated_amount"],
                previous_unpaid_amount=valoc["previous_unpaid"],
                remaining_unpaid_amount=valoc["remaining_unpaid"],
                notes=valoc["notes"],
            )
            alloc_rec.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="VENDOR_ADVANCE_ALLOCATED",
            resource_type="VendorPayment",
            resource_id=payment.payment_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "payment_number": payment.payment_number,
                "newly_allocated": str(total_new_allocated),
                "remaining_unapplied": str(payment.unallocated_amount),
            },
        )

        return cls.serialize_payment(payment)

    @classmethod
    def list_vendor_payments(
        cls,
        tenant_id: str,
        actor_user: Any,
        supplier_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists vendor payments for the tenant with optional supplier and status filters."""
        cls._assert_view_role(actor_user)
        cls._assert_tenant_active(tenant_id)

        qs = VendorPayment.objects.filter(tenant_id=tenant_id).select_related("supplier", "branch").prefetch_related("allocations")
        if supplier_id:
            qs = qs.filter(supplier_id=supplier_id)
        if status:
            qs = qs.filter(status=status.upper())

        return [cls.serialize_payment(p) for p in qs.order_by("-created_at")]

    @classmethod
    def get_vendor_payment(
        cls,
        tenant_id: str,
        actor_user: Any,
        payment_id: str,
    ) -> Dict[str, Any]:
        """Retrieves single vendor payment details with allocations."""
        cls._assert_view_role(actor_user)
        cls._assert_tenant_active(tenant_id)

        payment = (
            VendorPayment.objects.filter(tenant_id=tenant_id, payment_id=payment_id)
            .select_related("supplier", "branch")
            .prefetch_related("allocations")
            .first()
        )
        if not payment:
            raise NotFound({"error_code": "ERR_PAYMENT_NOT_FOUND", "detail": f"Vendor payment '{payment_id}' not found."})

        return cls.serialize_payment(payment)
