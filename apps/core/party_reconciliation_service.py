"""
backend/apps/core/party_reconciliation_service.py
Architecture Decision: ADR-0280 (P280).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service governing End-to-End Party Reconciliation:
- Reconciles Customer Accounts Receivable (AR) cycles:
  * Invoices, split payments, refunds (sales returns), due collections, and reversals.
- Reconciles Supplier Accounts Payable (AP) cycles:
  * GRN/bills, vendor payments, supplier credit notes, debit notes, and payment reversals.
- Double-entry balance parity verification:
  * Customer: sum(debits) - sum(credits) == PosCustomer.current_due_bdt (Variance == 0.00 BDT).
  * Supplier: sum(credits) - sum(debits) == Supplier.current_balance (Variance == 0.00 BDT).
- Concurrency control, fine-grained RBAC, and SHA-256 audit trails.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import hashlib
import json
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
    CustomerDueCollection,
    CustomerDueRecord,
    CustomerReceivableEntryType,
    CustomerReceivableLedgerEntry,
    PartyReconciliationRecord,
    PartyReconciliationStatus,
    PartyReconciliationType,
    PosCustomer,
    PosSaleOrder,
    PosSaleReturn,
    SaleInvoice,
    Supplier,
    SupplierLedgerEntry,
    SupplierPostingType,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")

RECON_VIEW_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
    UserRole.AUDITOR,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "FINANCE_OFFICER",
    "AUDITOR",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

RECON_ADJUST_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

STAFF_DENIED_ROLES = {
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    "CASHIER",
    "STAFF_PHARMACIST",
    "INVENTORY_CLERK",
    "DISPENSER",
}


def q2(val: Any) -> Decimal:
    """Strictly quantize monetary values to 2 decimal places."""
    if val is None:
        return DECIMAL_ZERO
    try:
        return Decimal(str(val)).quantize(Decimal("0.01"))
    except Exception:
        return DECIMAL_ZERO


def compute_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PartyReconciliationService:
    """
    Domain service for end-to-end customer and supplier party reconciliation.
    """

    @classmethod
    def assert_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_UNAUTHENTICATED",
                    "detail": "Authentication required for party reconciliation operations.",
                }
            )
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in RECON_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and staff pharmacist are not authorized to perform party reconciliation.",
                    "message_en": "Cashier and staff pharmacist are not authorized to perform party reconciliation.",
                    "message_bn": "ক্যাশিয়ার বা স্টাফ ফার্মাসিস্ট পার্টি রিকনসিলিয়েশন পরিচালনা করার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def assert_adjust_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in RECON_ADJUST_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_AUTO_ADJUST_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Finance Officer can authorize compensating reconciliation adjustments.",
                    "message_en": "Only Pharmacy Owner or Finance Officer can authorize compensating reconciliation adjustments.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা ফিন্যান্স অফিসার ক্ষতিপূরণমূলক রিকনসিলিয়েশন সমন্বয় অনুমোদন করতে পারেন।",
                }
            )

    @classmethod
    def assert_tenant_active(cls, tenant_id: str) -> Tenant:
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
                    "detail": "Tenant account subscription is suspended. Reconciliation operations blocked.",
                    "message_en": "Tenant account subscription is suspended. Reconciliation operations blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। রিকনসিলিয়েশন অপারেশন বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    # -------------------------------------------------------------------------
    # 1. CUSTOMER ACCOUNTS RECEIVABLE (AR) RECONCILIATION
    # -------------------------------------------------------------------------

    @classmethod
    @transaction.atomic
    def reconcile_customer_ar(
        cls,
        *,
        tenant_id: str,
        customer_id: str,
        branch_id: Optional[str] = None,
        actor_user: Any = None,
        auto_adjust: bool = False,
        adjustment_reason: str = "",
    ) -> Dict[str, Any]:
        """
        Reconciles Customer Receivable cycle across invoices, payments, refunds and reversals.
        Verifies double-entry: sum(debits) - sum(credits) == PosCustomer.current_due_bdt.
        """
        cls.assert_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        customer = PosCustomer.objects.select_for_update().filter(tenant=tenant, customer_id=customer_id).first()
        if not customer:
            foreign = PosCustomer.objects.filter(customer_id=customer_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_CUSTOMER_NOT_FOUND", "detail": f"Customer '{customer_id}' not found."})

        # Calculate totals from CustomerReceivableLedgerEntry
        entries = CustomerReceivableLedgerEntry.objects.filter(tenant=tenant, customer=customer)
        total_debits = entries.aggregate(tot=Sum("debit_amount_bdt"))["tot"] or DECIMAL_ZERO
        total_credits = entries.aggregate(tot=Sum("credit_amount_bdt"))["tot"] or DECIMAL_ZERO

        # Categorized totals
        source_invoices_bdt = entries.filter(
            entry_type__in=[CustomerReceivableEntryType.DUE_SALE, "DUE_SALE"]
        ).aggregate(tot=Sum("debit_amount_bdt"))["tot"] or DECIMAL_ZERO

        collections_bdt = entries.filter(
            entry_type__in=[CustomerReceivableEntryType.PAYMENT_COLLECTION, "PAYMENT_COLLECTION"]
        ).aggregate(tot=Sum("credit_amount_bdt"))["tot"] or DECIMAL_ZERO

        refunds_credits_bdt = entries.filter(
            entry_type__in=[CustomerReceivableEntryType.SALES_RETURN_CREDIT, "SALES_RETURN_CREDIT"]
        ).aggregate(tot=Sum("credit_amount_bdt"))["tot"] or DECIMAL_ZERO

        reversals_bdt = entries.filter(
            entry_type__in=[
                CustomerReceivableEntryType.REVERSAL,
                CustomerReceivableEntryType.ADJUSTMENT,
                CustomerReceivableEntryType.WRITE_OFF,
                "REVERSAL",
                "ADJUSTMENT",
                "WRITE_OFF",
            ]
        ).aggregate(tot=Sum("credit_amount_bdt"))["tot"] or DECIMAL_ZERO

        calculated_balance = q2(total_debits - total_credits)
        recorded_balance = q2(customer.current_due_bdt)
        variance = q2(calculated_balance - recorded_balance)

        discrepancies = []
        is_balanced = (variance == DECIMAL_ZERO)
        status_enum = PartyReconciliationStatus.BALANCED if is_balanced else PartyReconciliationStatus.DISCREPANCY_DETECTED

        if not is_balanced:
            discrepancies.append(
                f"Variance of {variance} BDT detected between AR ledger balance ({calculated_balance} BDT) and customer recorded balance ({recorded_balance} BDT)."
            )

        if auto_adjust and not is_balanced:
            cls.assert_adjust_authorized(actor_user)
            clean_adj_reason = str(adjustment_reason or "End-of-cycle AR ledger variance auto-adjustment").strip()

            # Record compensating entry in CustomerReceivableLedgerEntry
            # If calculated_balance > recorded_balance, we credit the difference; else debit.
            comp_credit = variance if variance > DECIMAL_ZERO else DECIMAL_ZERO
            comp_debit = (-variance) if variance < DECIMAL_ZERO else DECIMAL_ZERO

            CustomerReceivableLedgerEntry.objects.create(
                tenant=tenant,
                branch=customer.primary_branch,
                customer=customer,
                entry_type=CustomerReceivableEntryType.ADJUSTMENT,
                reference_number=f"ADJ-RECON-{uuid.uuid4().hex[:8].upper()}",
                debit_amount_bdt=comp_debit,
                credit_amount_bdt=comp_credit,
                balance_after_bdt=recorded_balance,
                narration=f"Compensating reconciliation adjustment: {clean_adj_reason}",
                posted_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            )

            status_enum = PartyReconciliationStatus.RECONCILED_WITH_ADJUSTMENT
            variance = DECIMAL_ZERO
            is_balanced = True
            discrepancies.append(f"Auto-adjustment applied: {clean_adj_reason}. New variance: 0.00 BDT.")

        now = timezone.now()
        recon_record = PartyReconciliationRecord.objects.create(
            tenant=tenant,
            branch=customer.primary_branch,
            reconciliation_type=PartyReconciliationType.CUSTOMER_RECEIVABLE,
            party_type="CUSTOMER",
            party_id=customer.customer_id,
            status=status_enum,
            total_source_invoices_bdt=q2(source_invoices_bdt),
            total_settlements_collections_bdt=q2(collections_bdt),
            total_refunds_credits_bdt=q2(refunds_credits_bdt),
            total_reversals_adjustments_bdt=q2(reversals_bdt),
            calculated_balance_bdt=calculated_balance,
            recorded_balance_bdt=recorded_balance,
            variance_bdt=variance,
            is_balanced=is_balanced,
            items_reconciled_count=entries.count(),
            discrepancy_details=discrepancies,
            reconciliation_notes=adjustment_reason or "Customer AR cycle reconciliation",
            reconciled_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            sha256_audit_signature=compute_sha256(f"{customer.customer_id}|{calculated_balance}|{variance}|{now.isoformat()}"),
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PARTY_RECONCILIATION_CUSTOMER_AR",
            resource_type="PartyReconciliationRecord",
            resource_id=recon_record.reconciliation_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "customer_id": customer.customer_id,
                "is_balanced": is_balanced,
                "variance_bdt": str(variance),
                "status": status_enum,
            },
        )

        return {
            "success": True,
            "reconciliation_id": recon_record.reconciliation_id,
            "party_type": "CUSTOMER",
            "party_id": customer.customer_id,
            "party_name": customer.full_name,
            "status": status_enum,
            "is_balanced": is_balanced,
            "total_source_invoices_bdt": str(q2(source_invoices_bdt)),
            "total_settlements_collections_bdt": str(q2(collections_bdt)),
            "total_refunds_credits_bdt": str(q2(refunds_credits_bdt)),
            "total_reversals_adjustments_bdt": str(q2(reversals_bdt)),
            "calculated_balance_bdt": str(calculated_balance),
            "recorded_balance_bdt": str(recorded_balance),
            "variance_bdt": str(variance),
            "items_reconciled_count": entries.count(),
            "discrepancies": discrepancies,
            "reconciled_at": now.isoformat(),
        }

    # -------------------------------------------------------------------------
    # 2. SUPPLIER ACCOUNTS PAYABLE (AP) RECONCILIATION
    # -------------------------------------------------------------------------

    @classmethod
    @transaction.atomic
    def reconcile_supplier_ap(
        cls,
        *,
        tenant_id: str,
        supplier_id: str,
        branch_id: Optional[str] = None,
        actor_user: Any = None,
        auto_adjust: bool = False,
        adjustment_reason: str = "",
    ) -> Dict[str, Any]:
        """
        Reconciles Supplier Payable cycle across GRN bills, vendor payments, credit notes and reversals.
        Verifies double-entry: sum(credits) - sum(debits) == Supplier.current_balance.
        """
        cls.assert_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        supplier = Supplier.objects.select_for_update().filter(tenant=tenant, supplier_id=supplier_id).first()
        if not supplier:
            foreign = Supplier.objects.filter(supplier_id=supplier_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})

        entries = SupplierLedgerEntry.objects.filter(tenant=tenant, supplier=supplier)
        total_credits = entries.aggregate(tot=Sum("credit_amount"))["tot"] or DECIMAL_ZERO
        total_debits = entries.aggregate(tot=Sum("debit_amount"))["tot"] or DECIMAL_ZERO

        # Categorized totals
        bills_invoices_bdt = entries.filter(
            posting_type__in=[
                SupplierPostingType.GRN_INVOICE,
                SupplierPostingType.INVOICE,
                "GRN_INVOICE",
                "INVOICE",
            ]
        ).aggregate(tot=Sum("credit_amount"))["tot"] or DECIMAL_ZERO

        payments_bdt = entries.filter(
            posting_type__in=[SupplierPostingType.PAYMENT, "PAYMENT"]
        ).aggregate(tot=Sum("debit_amount"))["tot"] or DECIMAL_ZERO

        credits_returns_bdt = entries.filter(
            posting_type__in=[
                SupplierPostingType.RETURN_DEBIT_NOTE,
                SupplierPostingType.SUPPLIER_CREDIT_NOTE,
                "RETURN_DEBIT_NOTE",
                "SUPPLIER_CREDIT_NOTE",
            ]
        ).aggregate(tot=Sum("debit_amount"))["tot"] or DECIMAL_ZERO

        reversals_bdt = entries.filter(
            posting_type__in=[
                SupplierPostingType.CREDIT_NOTE_REVERSAL,
                SupplierPostingType.PAYMENT_REVERSAL,
                SupplierPostingType.ADJUSTMENT,
                "CREDIT_NOTE_REVERSAL",
                "PAYMENT_REVERSAL",
                "ADJUSTMENT",
            ]
        ).aggregate(tot=Sum("credit_amount"))["tot"] or DECIMAL_ZERO

        calculated_balance = q2(total_credits - total_debits)
        recorded_balance = q2(supplier.current_balance)
        variance = q2(calculated_balance - recorded_balance)

        discrepancies = []
        is_balanced = (variance == DECIMAL_ZERO)
        status_enum = PartyReconciliationStatus.BALANCED if is_balanced else PartyReconciliationStatus.DISCREPANCY_DETECTED

        if not is_balanced:
            discrepancies.append(
                f"Variance of {variance} BDT detected between AP ledger balance ({calculated_balance} BDT) and supplier balance ({recorded_balance} BDT)."
            )

        if auto_adjust and not is_balanced:
            cls.assert_adjust_authorized(actor_user)
            clean_adj_reason = str(adjustment_reason or "Supplier AP ledger variance auto-adjustment").strip()

            comp_debit = variance if variance > DECIMAL_ZERO else DECIMAL_ZERO
            comp_credit = (-variance) if variance < DECIMAL_ZERO else DECIMAL_ZERO

            SupplierLedgerEntry.objects.create(
                tenant=tenant,
                supplier=supplier,
                posting_type=SupplierPostingType.ADJUSTMENT,
                reference_number=f"ADJ-AP-{uuid.uuid4().hex[:8].upper()}",
                debit_amount=comp_debit,
                credit_amount=comp_credit,
                balance_after=recorded_balance,
                notes=f"Compensating AP adjustment: {clean_adj_reason}",
                posted_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            )

            status_enum = PartyReconciliationStatus.RECONCILED_WITH_ADJUSTMENT
            variance = DECIMAL_ZERO
            is_balanced = True
            discrepancies.append(f"Auto-adjustment applied: {clean_adj_reason}. New variance: 0.00 BDT.")

        now = timezone.now()
        recon_record = PartyReconciliationRecord.objects.create(
            tenant=tenant,
            reconciliation_type=PartyReconciliationType.SUPPLIER_PAYABLE,
            party_type="SUPPLIER",
            party_id=supplier.supplier_id,
            status=status_enum,
            total_source_invoices_bdt=q2(bills_invoices_bdt),
            total_settlements_collections_bdt=q2(payments_bdt),
            total_refunds_credits_bdt=q2(credits_returns_bdt),
            total_reversals_adjustments_bdt=q2(reversals_bdt),
            calculated_balance_bdt=calculated_balance,
            recorded_balance_bdt=recorded_balance,
            variance_bdt=variance,
            is_balanced=is_balanced,
            items_reconciled_count=entries.count(),
            discrepancy_details=discrepancies,
            reconciliation_notes=adjustment_reason or "Supplier AP cycle reconciliation",
            reconciled_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            sha256_audit_signature=compute_sha256(f"{supplier.supplier_id}|{calculated_balance}|{variance}|{now.isoformat()}"),
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PARTY_RECONCILIATION_SUPPLIER_AP",
            resource_type="PartyReconciliationRecord",
            resource_id=recon_record.reconciliation_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "supplier_id": supplier.supplier_id,
                "is_balanced": is_balanced,
                "variance_bdt": str(variance),
                "status": status_enum,
            },
        )

        return {
            "success": True,
            "reconciliation_id": recon_record.reconciliation_id,
            "party_type": "SUPPLIER",
            "party_id": supplier.supplier_id,
            "party_name": supplier.name,
            "status": status_enum,
            "is_balanced": is_balanced,
            "total_source_invoices_bdt": str(q2(bills_invoices_bdt)),
            "total_settlements_collections_bdt": str(q2(payments_bdt)),
            "total_refunds_credits_bdt": str(q2(credits_returns_bdt)),
            "total_reversals_adjustments_bdt": str(q2(reversals_bdt)),
            "calculated_balance_bdt": str(calculated_balance),
            "recorded_balance_bdt": str(recorded_balance),
            "variance_bdt": str(variance),
            "items_reconciled_count": entries.count(),
            "discrepancies": discrepancies,
            "reconciled_at": now.isoformat(),
        }

    # -------------------------------------------------------------------------
    # 3. TENANT PORTFOLIO RECONCILIATION SUMMARY
    # -------------------------------------------------------------------------

    @classmethod
    def reconcile_tenant_portfolio(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Aggregates portfolio-wide reconciliation metrics across all customers and suppliers.
        """
        cls.assert_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        customer_qs = PosCustomer.objects.filter(tenant=tenant)
        supplier_qs = Supplier.objects.filter(tenant=tenant)

        total_customers = customer_qs.count()
        total_suppliers = supplier_qs.count()

        total_ar_due = customer_qs.aggregate(tot=Sum("current_due_bdt"))["tot"] or DECIMAL_ZERO
        total_ap_payable = supplier_qs.aggregate(tot=Sum("current_balance"))["tot"] or DECIMAL_ZERO

        recent_reconciliations = PartyReconciliationRecord.objects.filter(tenant=tenant)[:10]

        return {
            "tenant_id": str(tenant.tenant_id),
            "total_customers_count": total_customers,
            "total_suppliers_count": total_suppliers,
            "total_outstanding_ar_bdt": str(q2(total_ar_due)),
            "total_outstanding_ap_bdt": str(q2(total_ap_payable)),
            "recent_reconciliations": [
                {
                    "reconciliation_id": r.reconciliation_id,
                    "reconciliation_type": r.reconciliation_type,
                    "party_type": r.party_type,
                    "party_id": r.party_id,
                    "status": r.status,
                    "variance_bdt": str(r.variance_bdt),
                    "is_balanced": r.is_balanced,
                    "created_at": r.created_at.isoformat(),
                }
                for r in recent_reconciliations
            ],
            "audited_at": timezone.now().isoformat(),
        }

    @classmethod
    def get_reconciliation_statement(
        cls,
        *,
        tenant_id: str,
        reconciliation_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """Retrieves authoritative statement of a prior reconciliation record."""
        cls.assert_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        rec = PartyReconciliationRecord.objects.filter(tenant=tenant, reconciliation_id=reconciliation_id).first()
        if not rec:
            raise NotFound({"error_code": "ERR_RECONCILIATION_NOT_FOUND", "detail": f"Reconciliation '{reconciliation_id}' not found."})

        return {
            "reconciliation_id": rec.reconciliation_id,
            "party_type": rec.party_type,
            "party_id": rec.party_id,
            "reconciliation_type": rec.reconciliation_type,
            "status": rec.status,
            "is_balanced": rec.is_balanced,
            "total_source_invoices_bdt": str(rec.total_source_invoices_bdt),
            "total_settlements_collections_bdt": str(rec.total_settlements_collections_bdt),
            "total_refunds_credits_bdt": str(rec.total_refunds_credits_bdt),
            "total_reversals_adjustments_bdt": str(rec.total_reversals_adjustments_bdt),
            "calculated_balance_bdt": str(rec.calculated_balance_bdt),
            "recorded_balance_bdt": str(rec.recorded_balance_bdt),
            "variance_bdt": str(rec.variance_bdt),
            "items_reconciled_count": rec.items_reconciled_count,
            "discrepancies": rec.discrepancy_details,
            "reconciliation_notes": rec.reconciliation_notes,
            "sha256_audit_signature": rec.sha256_audit_signature,
            "created_at": rec.created_at.isoformat(),
        }
