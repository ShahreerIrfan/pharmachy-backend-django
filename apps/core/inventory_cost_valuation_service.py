"""
backend/apps/core/inventory_cost_valuation_service.py
Architecture Decision: ADR-0295 (P295).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service reconciling inventory cost valuation against:
1. Physical purchase lots and batch stocks (FEFO landed unit costs).
2. General Ledger account (GL 1030 Merchandise Inventory book balance).
3. Loss disposition and expiry/quarantine write-offs (GL 5020).
4. Stock movement reconciliation & variance detection.
5. Audited GL valuation adjustments (write-down / write-up).
6. Multi-branch breakdown and consolidated pharmacy valuation.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Sum, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.double_entry_journal_service import DoubleEntryJournalService, q2
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    ChartOfAccount,
    GLAccountCategory,
    InventoryValuationAdjustmentLog,
    InventoryValuationAdjustmentType,
    JournalEntry,
    JournalEntryLine,
    JournalEntryStatus,
    JournalSourceModule,
    MedicineProduct,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")
DECIMAL_TOLERANCE = Decimal("0.01")

VALUATION_VIEW_ROLES = {
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

VALUATION_ADJUST_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

VALUATION_DENIED_ROLES = {
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    "CASHIER",
    "STAFF_PHARMACIST",
    "INVENTORY_CLERK",
    "DISPENSER",
}


class InventoryCostValuationService:
    """
    Authoritative domain service reconciling physical batch lot valuations against
    the general ledger inventory control account (GL 1030).
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any, branch_id: Optional[str] = None) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in VALUATION_DENIED_ROLES or role not in VALUATION_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_INVENTORY_VALUATION_UNAUTHORIZED",
                    "detail": "Cashier and counter staff are not authorized to access inventory cost valuation.",
                    "message_en": "Cashier and counter staff are not authorized to access inventory cost valuation.",
                    "message_bn": "ক্যাশিয়ার বা কাউন্টার স্টাফদের ইনভেন্টরি কস্ট ভ্যালুয়েশন দেখার অনুমতি নেই।",
                }
            )

        if role in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
            user_branch_id = str(getattr(actor_user, "branch_id", "") or "")
            if branch_id and user_branch_id and str(branch_id) != user_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_BRANCH_ACCESS_DENIED",
                        "detail": "Branch Managers can only access their assigned branch inventory valuation.",
                        "message_en": "Branch Managers can only access their assigned branch inventory valuation.",
                        "message_bn": "শাখা ব্যবস্থাপক শুধুমাত্র নিজস্ব শাখার ইনভেন্টরি মূল্যায়ন দেখতে পারবেন।",
                    }
                )

    @classmethod
    def assert_adjust_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in VALUATION_ADJUST_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_VALUATION_ADJUST_FORBIDDEN",
                    "detail": "Only Pharmacy Owners and Finance Officers can post audited inventory valuation adjustments.",
                    "message_en": "Only Pharmacy Owners and Finance Officers can post audited inventory valuation adjustments.",
                    "message_bn": "শুধুমাত্র ফার্মেসি মালিক ও অর্থ কর্মকর্তা ইনভেন্টরি মূল্যায়ন সমন্বয় করতে পারবেন।",
                }
            )

    @classmethod
    def assert_tenant_active(cls, tenant_id: str) -> Tenant:
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' not found."})
        if (
            tenant.status == TenantLifecycleStatus.SUSPENDED
            or getattr(tenant, "subscription_status", None) == "SUSPENDED"
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant account subscription is suspended. Inventory valuation blocked.",
                    "message_en": "Tenant account subscription is suspended. Inventory valuation blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। ইনভেন্টরি মূল্যায়ন দেখা সম্ভব নয়।",
                }
            )
        return tenant

    # =========================================================================
    # 1. GENERATE INVENTORY COST VALUATION & RECONCILIATION
    # =========================================================================

    @classmethod
    def generate_inventory_valuation_report(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        as_of_date: Optional[Any] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Reconciles physical batch purchase lot valuation against GL 1030 Merchandise Inventory.
        """
        cls.assert_view_authorized(actor_user, branch_id=branch_id)
        tenant = cls.assert_tenant_active(tenant_id)

        target_branch = None
        if branch_id and branch_id not in ("ALL", "CONSOLIDATED", "ALL_BRANCHES"):
            target_branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
            if not target_branch:
                foreign = Branch.objects.filter(branch_id=branch_id).first()
                if foreign and str(foreign.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant branch access forbidden."})
                raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        p_date: Optional[datetime.date] = None
        if as_of_date:
            try:
                p_date = datetime.date.fromisoformat(str(as_of_date)) if isinstance(as_of_date, str) else as_of_date
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_DATE", "detail": "Invalid as_of_date format (YYYY-MM-DD)."})

        # 1. Query Physical Batches (Non-depleted on-hand inventory)
        batch_qs = BatchStock.objects.filter(
            tenant=tenant,
            quantity_base_units__gt=0,
        ).exclude(
            status__in=[BatchStatus.DEPLETED, BatchStatus.ARCHIVED],
        ).select_related("product", "branch")

        if target_branch:
            batch_qs = batch_qs.filter(branch=target_branch)

        sellable_units = 0
        sellable_value = DECIMAL_ZERO
        quarantined_units = 0
        quarantined_value = DECIMAL_ZERO
        expired_units = 0
        expired_value = DECIMAL_ZERO

        branch_map: Dict[str, Dict[str, Any]] = {}
        sample_batches: List[Dict[str, Any]] = []

        for b in batch_qs:
            qty = b.quantity_base_units
            cost = b.cost_per_unit or DECIMAL_ZERO
            val = q2(Decimal(qty) * cost)

            b_id = str(b.branch_id) if b.branch_id else "UNALLOCATED"
            b_name = b.branch.name if b.branch else "Head Office / Central"

            if b_id not in branch_map:
                branch_map[b_id] = {
                    "branch_id": b_id,
                    "branch_name": b_name,
                    "physical_units": 0,
                    "physical_valuation_bdt": DECIMAL_ZERO,
                    "sellable_value_bdt": DECIMAL_ZERO,
                    "quarantined_value_bdt": DECIMAL_ZERO,
                    "expired_value_bdt": DECIMAL_ZERO,
                    "gl_book_value_bdt": DECIMAL_ZERO,
                    "variance_bdt": DECIMAL_ZERO,
                    "reconciliation_status": "BALANCED",
                }

            if b.status == BatchStatus.EXPIRED:
                expired_units += qty
                expired_value += val
                branch_map[b_id]["expired_value_bdt"] += val
            elif b.status == BatchStatus.QUARANTINED or b.is_quarantined:
                quarantined_units += qty
                quarantined_value += val
                branch_map[b_id]["quarantined_value_bdt"] += val
                branch_map[b_id]["physical_units"] += qty
                branch_map[b_id]["physical_valuation_bdt"] += val
            else:
                sellable_units += qty
                sellable_value += val
                branch_map[b_id]["sellable_value_bdt"] += val
                branch_map[b_id]["physical_units"] += qty
                branch_map[b_id]["physical_valuation_bdt"] += val

            if len(sample_batches) < 20:
                sample_batches.append(
                    {
                        "batch_id": b.batch_id,
                        "batch_number": b.batch_number,
                        "product_name": b.product.brand_name,
                        "generic_name": getattr(b.product, "generic_name", ""),
                        "quantity_units": qty,
                        "cost_per_unit": str(cost),
                        "total_valuation_bdt": str(val),
                        "status": b.status,
                        "expiry_date": b.expiry_date.isoformat() if b.expiry_date else None,
                    }
                )

        total_physical_units = sellable_units + quarantined_units
        total_physical_valuation = q2(sellable_value + quarantined_value)
        total_loss_disposition_value = q2(expired_value)

        # 2. Query General Ledger Book Value (GL 1030 Merchandise Inventory)
        gl_inv_lines = JournalEntryLine.objects.filter(
            journal_entry__tenant=tenant,
            journal_entry__status=JournalEntryStatus.POSTED,
            account__account_code="1030",
        ).exclude(
            journal_entry__source_module=JournalSourceModule.CLOSING,
        ).select_related("journal_entry__branch")

        if target_branch:
            gl_inv_lines = gl_inv_lines.filter(journal_entry__branch=target_branch)

        if p_date:
            gl_inv_lines = gl_inv_lines.filter(journal_entry__posting_date__lte=p_date)

        total_gl_debits = DECIMAL_ZERO
        total_gl_credits = DECIMAL_ZERO

        for gl_line in gl_inv_lines:
            dr = gl_line.debit_amount_bdt
            cr = gl_line.credit_amount_bdt
            total_gl_debits += dr
            total_gl_credits += cr

            b_id = str(gl_line.journal_entry.branch_id) if gl_line.journal_entry.branch_id else "UNALLOCATED"
            b_name = gl_line.journal_entry.branch.name if gl_line.journal_entry.branch else "Head Office / Central"
            if b_id not in branch_map:
                branch_map[b_id] = {
                    "branch_id": b_id,
                    "branch_name": b_name,
                    "physical_units": 0,
                    "physical_valuation_bdt": DECIMAL_ZERO,
                    "sellable_value_bdt": DECIMAL_ZERO,
                    "quarantined_value_bdt": DECIMAL_ZERO,
                    "expired_value_bdt": DECIMAL_ZERO,
                    "gl_book_value_bdt": DECIMAL_ZERO,
                    "variance_bdt": DECIMAL_ZERO,
                    "reconciliation_status": "BALANCED",
                }
            branch_map[b_id]["gl_book_value_bdt"] += (dr - cr)

        gl_book_value = q2(total_gl_debits - total_gl_credits)

        # 3. Compute Valuation Variance & Status
        valuation_variance = q2(gl_book_value - total_physical_valuation)
        is_balanced = abs(valuation_variance) < DECIMAL_TOLERANCE
        reconciliation_status = "BALANCED" if is_balanced else "VARIANCE_DETECTED"

        # 4. Finalize Branch Summaries
        branches_summary = []
        for b_id, b_data in branch_map.items():
            b_phys = q2(b_data["physical_valuation_bdt"])
            b_gl = q2(b_data["gl_book_value_bdt"])
            b_diff = q2(b_gl - b_phys)
            b_bal = abs(b_diff) < DECIMAL_TOLERANCE
            branches_summary.append(
                {
                    "branch_id": b_id,
                    "branch_name": b_data["branch_name"],
                    "physical_units": b_data["physical_units"],
                    "physical_valuation_bdt": str(b_phys),
                    "sellable_value_bdt": str(q2(b_data["sellable_value_bdt"])),
                    "quarantined_value_bdt": str(q2(b_data["quarantined_value_bdt"])),
                    "expired_value_bdt": str(q2(b_data["expired_value_bdt"])),
                    "gl_book_value_bdt": str(b_gl),
                    "variance_bdt": str(b_diff),
                    "reconciliation_status": "BALANCED" if b_bal else "VARIANCE_DETECTED",
                }
            )

        return {
            "tenant_id": str(tenant.tenant_id),
            "tenant_name": tenant.name,
            "branch_id": str(target_branch.branch_id) if target_branch else "ALL_BRANCHES",
            "branch_name": target_branch.name if target_branch else "Consolidated Pharmacy (All Branches)",
            "as_of_date": p_date.isoformat() if p_date else timezone.now().date().isoformat(),
            "generated_at": timezone.now().isoformat(),
            # Sub-ledger physical valuation
            "physical_inventory": {
                "total_on_hand_units": total_physical_units,
                "sellable_units": sellable_units,
                "sellable_value_bdt": str(q2(sellable_value)),
                "quarantined_units": quarantined_units,
                "quarantined_value_bdt": str(q2(quarantined_value)),
                "total_physical_valuation_bdt": str(total_physical_valuation),
                "costing_method": "FEFO_LANDED_LOT_COST",
            },
            # Loss disposition
            "loss_disposition": {
                "expired_units": expired_units,
                "expired_loss_value_bdt": str(total_loss_disposition_value),
                "status_commentary": "Expired batches must be segregated and written off to GL 5020.",
            },
            # Accounting GL Book Value
            "accounting_gl": {
                "account_code": "1030",
                "account_name": "Merchandise Inventory",
                "account_name_bn": "ওষুধ মজুত",
                "total_debits_bdt": str(q2(total_gl_debits)),
                "total_credits_bdt": str(q2(total_gl_credits)),
                "gl_book_value_bdt": str(gl_book_value),
            },
            # Reconciliation
            "reconciliation": {
                "physical_valuation_bdt": str(total_physical_valuation),
                "gl_book_value_bdt": str(gl_book_value),
                "valuation_variance_bdt": str(valuation_variance),
                "is_balanced": is_balanced,
                "status": reconciliation_status,
                "message_en": (
                    "Inventory sub-ledger and GL control account are fully balanced."
                    if is_balanced
                    else f"Valuation discrepancy of BDT {valuation_variance} detected. Audited adjustment required."
                ),
                "message_bn": (
                    "ইনভেন্টরি সাব-লেজার এবং জিএল নিয়ন্ত্রণ হিসাব সম্পূর্ণভাবে ব্যালান্সড রয়েছে।"
                    if is_balanced
                    else f"ইনভেন্টরি মূল্যায়নে ৳{valuation_variance} গরমিল শনাক্ত হয়েছে। সমন্বয় প্রয়োজন।"
                ),
            },
            "sample_batches": sample_batches,
            "branches": branches_summary,
            "branch_breakdown": branches_summary,
            "is_consolidated": target_branch is None,
        }

    # =========================================================================
    # 2. AUDITED GENERAL LEDGER VALUATION ADJUSTMENT
    # =========================================================================

    @classmethod
    @transaction.atomic
    def post_valuation_adjustment(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        adjustment_type: str,
        amount_bdt: Any,
        reason: str,
        audit_notes: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Posts an audited GL valuation adjustment reconciling inventory book value.
        - WRITE_DOWN: Dr 5020 (Expiry/Wastage Loss), Cr 1030 (Inventory).
        - WRITE_UP: Dr 1030 (Inventory), Cr 5020 (Expiry/Wastage Loss reduction).
        """
        cls.assert_adjust_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_type = str(adjustment_type).strip().upper()
        if clean_type not in (InventoryValuationAdjustmentType.WRITE_DOWN, InventoryValuationAdjustmentType.WRITE_UP):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_ADJUSTMENT_TYPE",
                    "detail": "adjustment_type must be either WRITE_DOWN or WRITE_UP.",
                }
            )

        try:
            amt = q2(Decimal(str(amount_bdt)))
            if amt <= DECIMAL_ZERO:
                raise ValueError()
        except Exception:
            raise ValidationError({"error_code": "ERR_INVALID_AMOUNT", "detail": "amount_bdt must be a positive decimal."})

        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise ValidationError({"error_code": "ERR_MISSING_REASON", "detail": "Valuation adjustment reason is required."})

        target_branch = None
        if branch_id:
            target_branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
            if not target_branch:
                raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        acc_inv = ChartOfAccount.objects.filter(tenant=tenant, account_code="1030").first()
        acc_loss = ChartOfAccount.objects.filter(tenant=tenant, account_code="5020").first()

        if not acc_inv or not acc_loss:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_GL_ACCOUNTS",
                    "detail": "Required accounts 1030 (Merchandise Inventory) and 5020 (Expiry & Wastage) must exist.",
                }
            )

        # Construct balanced double-entry lines
        if clean_type == InventoryValuationAdjustmentType.WRITE_DOWN:
            # Shortage / Loss: Debit 5020, Credit 1030
            lines = [
                {"account_id": str(acc_loss.account_id), "debit_amount_bdt": amt, "credit_amount_bdt": DECIMAL_ZERO},
                {"account_id": str(acc_inv.account_id), "debit_amount_bdt": DECIMAL_ZERO, "credit_amount_bdt": amt},
            ]
            narration = f"Inventory Valuation Write-Down: {clean_reason}"
        else:
            # Overage / Surplus: Debit 1030, Credit 5020
            lines = [
                {"account_id": str(acc_inv.account_id), "debit_amount_bdt": amt, "credit_amount_bdt": DECIMAL_ZERO},
                {"account_id": str(acc_loss.account_id), "debit_amount_bdt": DECIMAL_ZERO, "credit_amount_bdt": amt},
            ]
            narration = f"Inventory Valuation Write-Up: {clean_reason}"

        # Post balanced double-entry journal
        journal_res = DoubleEntryJournalService.post_journal_entry(
            tenant_id=str(tenant.tenant_id),
            branch_id=str(target_branch.branch_id) if target_branch else None,
            posting_date=timezone.now().date(),
            source_module=JournalSourceModule.STOCK_ADJUSTMENT,
            source_reference=f"VAL-ADJ-{clean_type}",
            narration=narration,
            lines=lines,
            actor_user=actor_user,
        )

        jid = journal_res.get("entry_id") or journal_res.get("journal_id")
        journal_entry = JournalEntry.objects.get(entry_id=jid)

        adj_log = InventoryValuationAdjustmentLog.objects.create(
            tenant=tenant,
            branch=target_branch,
            adjustment_type=clean_type,
            amount_bdt=amt,
            reason=clean_reason,
            audit_notes=audit_notes or "",
            journal_entry=journal_entry,
            performed_by=actor_user,
        )

        return {
            "adjustment_id": adj_log.adjustment_id,
            "tenant_id": str(tenant.tenant_id),
            "branch_id": str(target_branch.branch_id) if target_branch else "ALL_BRANCHES",
            "adjustment_type": clean_type,
            "amount_bdt": str(amt),
            "reason": clean_reason,
            "audit_notes": adj_log.audit_notes,
            "journal_id": str(journal_entry.entry_id),
            "created_at": adj_log.created_at.isoformat(),
            "status": "ADJUSTMENT_POSTED",
            "message_en": f"Inventory valuation {clean_type} of BDT {amt} posted to GL 1030 successfully.",
            "message_bn": f"ইনভেন্টরি মূল্যায়ন সমন্বয় {clean_type} (৳{amt}) সফলভাবে জিএল ১০৩০ এ পোস্ট হয়েছে।",
        }

    # =========================================================================
    # 3. EXPORT CSV
    # =========================================================================

    @classmethod
    def export_inventory_valuation_csv(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        as_of_date: Optional[Any] = None,
        actor_user: Any = None,
    ) -> str:
        """
        Exports the Inventory Valuation & GL Reconciliation report to CSV.
        """
        rep = cls.generate_inventory_valuation_report(
            tenant_id=tenant_id,
            branch_id=branch_id,
            as_of_date=as_of_date,
            actor_user=actor_user,
        )

        lines = [
            f"# INVENTORY COST VALUATION & GL RECONCILIATION - {rep['tenant_name']}",
            f"# Scope: {rep['branch_name']}",
            f"# As of Date: {rep['as_of_date']}",
            f"# Generated At: {rep['generated_at']}",
            "Section,Metric / Parameter,Value",
            f"PHYSICAL_INVENTORY,Total On-Hand Units,{rep['physical_inventory']['total_on_hand_units']}",
            f"PHYSICAL_INVENTORY,Sellable Available Units,{rep['physical_inventory']['sellable_units']}",
            f"PHYSICAL_INVENTORY,Sellable Available Value BDT,{rep['physical_inventory']['sellable_value_bdt']}",
            f"PHYSICAL_INVENTORY,Quarantined Units,{rep['physical_inventory']['quarantined_units']}",
            f"PHYSICAL_INVENTORY,Quarantined Value BDT,{rep['physical_inventory']['quarantined_value_bdt']}",
            f"PHYSICAL_INVENTORY,Total Physical Valuation BDT,{rep['physical_inventory']['total_physical_valuation_bdt']}",
            f"LOSS_DISPOSITION,Expired Units Pending Write-Off,{rep['loss_disposition']['expired_units']}",
            f"LOSS_DISPOSITION,Expired Loss Value BDT,{rep['loss_disposition']['expired_loss_value_bdt']}",
            f"ACCOUNTING_GL,GL 1030 Merchandise Inventory Book Value BDT,{rep['accounting_gl']['gl_book_value_bdt']}",
            f"RECONCILIATION,Valuation Variance BDT,{rep['reconciliation']['valuation_variance_bdt']}",
            f"RECONCILIATION,Reconciliation Status,{rep['reconciliation']['status']}",
        ]

        if rep["branches"]:
            lines.append("")
            lines.append("# MULTI-BRANCH INVENTORY VALUATION BREAKDOWN")
            lines.append("Branch ID,Branch Name,Physical Units,Physical Value BDT,GL Book Value BDT,Variance BDT,Status")
            for b in rep["branches"]:
                lines.append(
                    f"{b['branch_id']},\"{b['branch_name']}\",{b['physical_units']},{b['physical_valuation_bdt']},"
                    f"{b['gl_book_value_bdt']},{b['variance_bdt']},{b['reconciliation_status']}"
                )

        return "\n".join(lines)
