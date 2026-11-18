"""
backend/apps/core/control_account_service.py
Architecture Decision: ADR-0285 (P285).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service governing General Ledger Control Accounts:
- Reconciles Accounts Receivable (1030) against detailed Customer Subledgers.
- Reconciles Accounts Payable (2010) against detailed Supplier Subledgers.
- Detects and flags subledger vs control account variances with party breakdown.
- Posts audited double-entry balancing adjustment journals for approved discrepancies.
- Multi-branch control account overview.
- Multi-tenant isolation and strict role-based access control.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import Sum, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.double_entry_journal_service import DoubleEntryJournalService, q2
from apps.core.models import (
    Branch,
    ChartOfAccount,
    ControlAccountReconciliationRun,
    ControlAccountReconciliationStatus,
    ControlAccountType,
    CustomerReceivableLedgerEntry,
    JournalEntry,
    JournalSourceModule,
    PosCustomer,
    Supplier,
    SupplierLedgerEntry,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")

CONTROL_ACCOUNT_VIEW_ROLES = {
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

CONTROL_ACCOUNT_ADJUST_ROLES = {
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


class ControlAccountService:
    """
    Authoritative domain service for General Ledger control account reconciliations.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in CONTROL_ACCOUNT_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and staff pharmacist are not authorized to view or reconcile control accounts.",
                    "message_en": "Cashier and staff pharmacist are not authorized to view or reconcile control accounts.",
                    "message_bn": "ক্যাশিয়ার বা স্টাফ ফার্মাসিস্ট কন্ট্রোল অ্যাকাউন্ট রিকনসিলিয়েশন দেখার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def assert_adjust_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in CONTROL_ACCOUNT_ADJUST_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CONTROL_ADJUST_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Finance Officer can apply control account balancing adjustments.",
                    "message_en": "Only Pharmacy Owner or Finance Officer can apply control account balancing adjustments.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা ফিন্যান্স অফিসার কন্ট্রোল অ্যাকাউন্ট ব্যালেন্সিং অ্যাডজাস্টমেন্ট করতে পারেন।",
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
                    "detail": "Tenant account subscription is suspended. Control account operations blocked.",
                    "message_en": "Tenant account subscription is suspended. Control account operations blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। কন্ট্রোল অ্যাকাউন্ট অপারেশন বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    @classmethod
    @transaction.atomic
    def reconcile_control_account(
        cls,
        *,
        tenant_id: str,
        control_account_type: str,
        branch_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Reconciles a General Ledger control account (1030 AR or 2010 AP) against its detailed subledger.
        Identifies exact party balances, subledger total, GL balance, variance, and creates an audited run.
        """
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_type = control_account_type.upper().strip()
        if clean_type not in (ControlAccountType.CUSTOMER_AR, ControlAccountType.SUPPLIER_AP, "CUSTOMER_AR", "SUPPLIER_AP"):
            raise ValidationError(
                {"error_code": "ERR_INVALID_CONTROL_ACCOUNT_TYPE", "detail": "Must be CUSTOMER_AR or SUPPLIER_AP."}
            )

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first() if branch_id else None

        account_code = "1030" if clean_type == ControlAccountType.CUSTOMER_AR else "2010"
        gl_account = ChartOfAccount.objects.filter(tenant=tenant, account_code=account_code).first()
        if not gl_account:
            raise NotFound(
                {
                    "error_code": "ERR_CONTROL_ACCOUNT_NOT_FOUND",
                    "detail": f"General Ledger control account '{account_code}' not found in tenant chart of accounts.",
                }
            )

        gl_control_balance = q2(gl_account.current_balance_bdt)
        subledger_total = DECIMAL_ZERO
        party_details: List[Dict[str, Any]] = []

        if clean_type == ControlAccountType.CUSTOMER_AR:
            # Reconcile Customers against AR (1030)
            customers = PosCustomer.objects.filter(tenant=tenant)
            for cust in customers:
                # Latest ledger entry or customer due balance
                latest_entry = CustomerReceivableLedgerEntry.objects.filter(
                    tenant=tenant, customer=cust
                ).order_by("-posted_at").first()

                if latest_entry:
                    c_bal = q2(latest_entry.balance_after_bdt)
                else:
                    c_bal = q2(getattr(cust, "current_due_bdt", getattr(cust, "current_due_balance_bdt", DECIMAL_ZERO)))

                if c_bal != DECIMAL_ZERO or latest_entry is not None:
                    subledger_total += c_bal
                    party_details.append({
                        "party_id": str(cust.customer_id),
                        "party_name": getattr(cust, "full_name", getattr(cust, "name", "Customer")),
                        "phone": getattr(cust, "phone_number", getattr(cust, "phone", "")),
                        "subledger_balance_bdt": str(c_bal),
                    })
        else:
            # Reconcile Suppliers against AP (2010)
            suppliers = Supplier.objects.filter(tenant=tenant)
            for sup in suppliers:
                latest_entry = SupplierLedgerEntry.objects.filter(
                    tenant=tenant, supplier=sup
                ).order_by("-posted_at").first()

                if latest_entry:
                    s_bal = q2(latest_entry.balance_after)
                else:
                    s_bal = q2(getattr(sup, "current_balance", getattr(sup, "current_payable_balance", DECIMAL_ZERO)))

                if s_bal != DECIMAL_ZERO or latest_entry is not None:
                    subledger_total += s_bal
                    party_details.append({
                        "party_id": str(sup.supplier_id),
                        "party_name": sup.name,
                        "code": sup.code,
                        "subledger_balance_bdt": str(s_bal),
                    })

        variance = q2(gl_control_balance - subledger_total)
        is_balanced = (abs(variance) < Decimal("0.005"))
        recon_status = ControlAccountReconciliationStatus.MATCHED if is_balanced else ControlAccountReconciliationStatus.DISCREPANCY_FLAGGED

        run = ControlAccountReconciliationRun.objects.create(
            tenant=tenant,
            branch=branch,
            control_account_type=clean_type,
            gl_account=gl_account,
            gl_control_balance_bdt=gl_control_balance,
            subledger_total_balance_bdt=subledger_total,
            variance_bdt=variance,
            is_balanced=is_balanced,
            party_count=len(party_details),
            status=recon_status,
            discrepancy_details_json={"parties": party_details[:100]},
            reconciled_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="CONTROL_ACCOUNT_RECONCILE",
            resource_type="ControlAccountReconciliationRun",
            resource_id=run.run_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "control_account_type": clean_type,
                "gl_balance": str(gl_control_balance),
                "subledger_total": str(subledger_total),
                "variance": str(variance),
                "is_balanced": is_balanced,
            },
        )

        return cls._serialize_run(run)

    @classmethod
    @transaction.atomic
    def apply_reconciliation_adjustment(
        cls,
        *,
        tenant_id: str,
        run_id: str,
        adjustment_reason: str,
        contra_account_code: str = "3020",  # Retained Earnings by default
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Applies an audited double-entry journal balancing adjustment to reconcile an existing
        flagged discrepancy between the GL control account and the subledger.
        """
        cls.assert_adjust_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_reason = str(adjustment_reason or "").strip()
        if not clean_reason:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_ADJUSTMENT_REASON",
                    "detail": "A mandatory explanatory reason is required to apply control account balancing adjustment.",
                    "message_en": "A mandatory explanatory reason is required to apply control account balancing adjustment.",
                    "message_bn": "কন্ট্রোল অ্যাকাউন্ট ব্যালেন্সিং অ্যাডজাস্টমেন্টের জন্য সুনির্দিষ্ট কারণ উল্লেখ করা বাধ্যতামূলক।",
                }
            )

        run = ControlAccountReconciliationRun.objects.select_for_update().filter(
            tenant=tenant, run_id=run_id
        ).first()
        if not run:
            foreign = ControlAccountReconciliationRun.objects.filter(run_id=run_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_RUN_NOT_FOUND", "detail": f"Reconciliation run '{run_id}' not found."})

        if run.is_balanced or run.status == ControlAccountReconciliationStatus.RECONCILED:
            raise ValidationError(
                {
                    "error_code": "ERR_NO_ADJUSTMENT_NEEDED",
                    "detail": "Reconciliation run is already balanced or has already been reconciled.",
                    "message_en": "Reconciliation run is already balanced or has already been reconciled.",
                    "message_bn": "এই রিকনসিলিয়েশনটি ইতিমধ্যেই ভারসাম্যপূর্ণ অথবা অ্যাডজাস্ট করা হয়েছে।",
                }
            )

        variance = q2(run.variance_bdt)
        abs_var = abs(variance)
        gl_code = run.gl_account.account_code

        # Double-entry lines to eliminate the variance on the GL control account
        # If variance > 0: GL is higher than subledger -> Credit GL control account, Debit contra account
        # If variance < 0: GL is lower than subledger -> Debit GL control account, Credit contra account
        lines = []
        if variance > DECIMAL_ZERO:
            lines = [
                {
                    "account_code": contra_account_code,
                    "debit_amount_bdt": abs_var,
                    "credit_amount_bdt": DECIMAL_ZERO,
                    "description": f"Variance offset adjustment for {run.control_account_type}: {clean_reason}",
                },
                {
                    "account_code": gl_code,
                    "debit_amount_bdt": DECIMAL_ZERO,
                    "credit_amount_bdt": abs_var,
                    "description": f"Control account balance correction to match subledger: {clean_reason}",
                },
            ]
        else:
            lines = [
                {
                    "account_code": gl_code,
                    "debit_amount_bdt": abs_var,
                    "credit_amount_bdt": DECIMAL_ZERO,
                    "description": f"Control account balance correction to match subledger: {clean_reason}",
                },
                {
                    "account_code": contra_account_code,
                    "debit_amount_bdt": DECIMAL_ZERO,
                    "credit_amount_bdt": abs_var,
                    "description": f"Variance offset adjustment for {run.control_account_type}: {clean_reason}",
                },
            ]

        journal_data = DoubleEntryJournalService.post_journal_entry(
            tenant_id=str(tenant.tenant_id),
            lines=lines,
            posting_date=timezone.now().date(),
            source_module=JournalSourceModule.CLOSING,
            source_reference=f"RECON-{run.run_id[:8]}",
            narration=f"Control Account Reconciliation Balancing: {run.control_account_type} [{clean_reason}]",
            branch_id=str(run.branch_id) if run.branch_id else None,
            actor_user=actor_user,
        )
        journal = JournalEntry.objects.get(entry_id=journal_data["entry_id"])

        # Update GL account balance from the freshly adjusted balance
        run.gl_account.refresh_from_db()
        new_gl_bal = q2(run.gl_account.current_balance_bdt)
        new_variance = q2(new_gl_bal - run.subledger_total_balance_bdt)

        run.adjustment_journal = journal
        run.gl_control_balance_bdt = new_gl_bal
        run.variance_bdt = new_variance
        run.is_balanced = (abs(new_variance) < Decimal("0.005"))
        run.status = ControlAccountReconciliationStatus.RECONCILED
        run.reconciliation_notes = clean_reason
        run.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "finance@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "FINANCE_OFFICER")),
            action="CONTROL_ACCOUNT_ADJUST",
            resource_type="ControlAccountReconciliationRun",
            resource_id=run.run_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "run_id": run.run_id,
                "adjustment_journal": journal.journal_number,
                "adjusted_amount": str(abs_var),
                "new_variance": str(new_variance),
            },
        )

        return cls._serialize_run(run)

    @classmethod
    def get_control_accounts_overview(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Returns live on-demand side-by-side reconciliation status of both
        AR (1030) and AP (2010) control accounts.
        """
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        # 1. Accounts Receivable (1030)
        ar_acc = ChartOfAccount.objects.filter(tenant=tenant, account_code="1030").first()
        ar_gl = q2(ar_acc.current_balance_bdt) if ar_acc else DECIMAL_ZERO

        ar_subledger = DECIMAL_ZERO
        customers = PosCustomer.objects.filter(tenant=tenant)
        for cust in customers:
            latest_entry = CustomerReceivableLedgerEntry.objects.filter(
                tenant=tenant, customer=cust
            ).order_by("-posted_at").first()
            if latest_entry:
                ar_subledger += q2(latest_entry.balance_after_bdt)
            else:
                ar_subledger += q2(getattr(cust, "current_due_bdt", getattr(cust, "current_due_balance_bdt", DECIMAL_ZERO)))

        ar_variance = q2(ar_gl - ar_subledger)
        ar_balanced = (abs(ar_variance) < Decimal("0.005"))

        # 2. Accounts Payable (2010)
        ap_acc = ChartOfAccount.objects.filter(tenant=tenant, account_code="2010").first()
        ap_gl = q2(ap_acc.current_balance_bdt) if ap_acc else DECIMAL_ZERO

        ap_subledger = DECIMAL_ZERO
        suppliers = Supplier.objects.filter(tenant=tenant)
        for sup in suppliers:
            latest_entry = SupplierLedgerEntry.objects.filter(
                tenant=tenant, supplier=sup
            ).order_by("-posted_at").first()
            if latest_entry:
                ap_subledger += q2(latest_entry.balance_after)
            else:
                ap_subledger += q2(getattr(sup, "current_balance", getattr(sup, "current_payable_balance", DECIMAL_ZERO)))

        ap_variance = q2(ap_gl - ap_subledger)
        ap_balanced = (abs(ap_variance) < Decimal("0.005"))

        all_balanced = (ar_balanced and ap_balanced)

        return {
            "tenant_id": str(tenant.tenant_id),
            "branch_id": str(branch_id) if branch_id else "ALL_BRANCHES",
            "overall_status": "BALANCED" if all_balanced else "DISCREPANCIES_DETECTED",
            "is_fully_reconciled": all_balanced,
            "accounts_receivable": {
                "account_code": "1030",
                "account_name": "Accounts Receivable (Customer Dues)",
                "gl_control_balance_bdt": str(ar_gl),
                "subledger_total_bdt": str(ar_subledger),
                "variance_bdt": str(ar_variance),
                "is_balanced": ar_balanced,
                "party_count": customers.count(),
            },
            "accounts_payable": {
                "account_code": "2010",
                "account_name": "Accounts Payable (Supplier Dues)",
                "gl_control_balance_bdt": str(ap_gl),
                "subledger_total_bdt": str(ap_subledger),
                "variance_bdt": str(ap_variance),
                "is_balanced": ap_balanced,
                "party_count": suppliers.count(),
            },
            "currency": "BDT",
        }

    @classmethod
    def list_reconciliation_runs(
        cls,
        *,
        tenant_id: str,
        control_account_type: Optional[str] = None,
        status_param: Optional[str] = None,
        branch_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> List[Dict[str, Any]]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = ControlAccountReconciliationRun.objects.filter(tenant=tenant).select_related(
            "branch", "gl_account", "adjustment_journal"
        )
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)
        if control_account_type:
            qs = qs.filter(control_account_type=control_account_type.upper())
        if status_param:
            qs = qs.filter(status=status_param.upper())

        return [cls._serialize_run(r) for r in qs[:200]]

    @classmethod
    def get_reconciliation_run(
        cls,
        *,
        tenant_id: str,
        run_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        run = ControlAccountReconciliationRun.objects.filter(
            tenant=tenant, run_id=run_id
        ).select_related("branch", "gl_account", "adjustment_journal").first()
        if not run:
            foreign = ControlAccountReconciliationRun.objects.filter(run_id=run_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_RUN_NOT_FOUND", "detail": f"Reconciliation run '{run_id}' not found."})

        return cls._serialize_run(run)

    @classmethod
    def _serialize_run(cls, r: ControlAccountReconciliationRun) -> Dict[str, Any]:
        return {
            "run_id": r.run_id,
            "tenant_id": str(r.tenant_id),
            "branch_id": str(r.branch_id) if r.branch_id else None,
            "control_account_type": r.control_account_type,
            "gl_account_code": r.gl_account.account_code,
            "gl_account_name": r.gl_account.account_name,
            "gl_control_balance_bdt": str(r.gl_control_balance_bdt),
            "subledger_total_balance_bdt": str(r.subledger_total_balance_bdt),
            "variance_bdt": str(r.variance_bdt),
            "is_balanced": r.is_balanced,
            "party_count": r.party_count,
            "status": r.status,
            "discrepancy_details": r.discrepancy_details_json,
            "adjustment_journal_id": str(r.adjustment_journal_id) if r.adjustment_journal_id else None,
            "adjustment_journal_number": r.adjustment_journal.journal_number if r.adjustment_journal else None,
            "reconciliation_notes": r.reconciliation_notes,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
