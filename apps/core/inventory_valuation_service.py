"""
backend/apps/core/inventory_valuation_service.py
Architecture Decision: ADR-0216 (P216).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Authoritative domain service generating comprehensive multi-category inventory valuation
reports:
- On-hand total inventory valuation at documented costing rules (FEFO lot cost, FIFO, Weighted Average, MRP).
- Categorical breakdown: Sellable, Quarantined, In-Transit Interbranch Transfers, and Expired.
- Mathematical precision (Decimal BDT 2-decimal places) and conservation reconciliation.
- Cryptographic SHA-256 tamper-evident integrity hashes.
- Multi-tenant isolation and strict RBAC enforcement (Cashier denied).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    InterBranchTransferItem,
    InterBranchTransferRequest,
    InterBranchTransferStatus,
    InventoryValuationCategory,
    InventoryValuationLineItem,
    InventoryValuationReport,
    MedicineProduct,
    StorageCondition,
    Tenant,
    ValuationCostingRule,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

ALLOWED_VALUATION_ROLES = {
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    UserRole.STAFF_PHARMACIST,
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "STAFF_PHARMACIST",
    "PROCUREMENT_OFFICER",
    "INVENTORY_CLERK",
    "AUDITOR",
    "CHIEF_PHARMACIST",
}

COLD_CHAIN_CONDITIONS = {
    StorageCondition.COLD_CHAIN,
    StorageCondition.FREEZER,
    StorageCondition.COOL,
}


class InventoryValuationService:
    """
    Authoritative computation and reporting engine for multi-category inventory valuation.
    """

    @classmethod
    def assert_valuation_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role == "CASHIER" or role == UserRole.CASHIER or role not in ALLOWED_VALUATION_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Cashiers are strictly forbidden from viewing or generating inventory valuation reports.",
                    "error_code": "ERR_VALUATION_PERMISSION_DENIED",
                }
            )

    @classmethod
    def compute_sha256_checksum(cls, line_items: List[Dict[str, Any]]) -> str:
        """
        Computes deterministic SHA-256 checksum over sorted valuation line items.
        """
        sorted_lines = sorted(
            line_items,
            key=lambda item: (item["product_id"], item["batch_number"], item["category"]),
        )
        hasher = hashlib.sha256()
        for item in sorted_lines:
            token = (
                f"{item['product_id']}|{item['batch_number']}|{item['category']}|"
                f"{item['quantity_base_units']}|{item['unit_cost']}|{item['total_value_bdt']}\n"
            )
            hasher.update(token.encode("utf-8"))
        return hasher.hexdigest()

    @classmethod
    def calculate_weighted_average_costs(
        cls,
        tenant: Tenant,
        branch: Branch,
    ) -> Dict[str, Decimal]:
        """
        Computes weighted average unit cost per product across all on-hand batches in the branch.
        """
        batches = BatchStock.objects.filter(
            tenant=tenant,
            branch=branch,
            quantity_base_units__gt=0,
        ).values("product_id", "quantity_base_units", "cost_per_unit")

        totals: Dict[str, Dict[str, Decimal]] = {}
        for b in batches:
            pid = b["product_id"]
            qty = Decimal(b["quantity_base_units"])
            cost = b["cost_per_unit"]
            if pid not in totals:
                totals[pid] = {"qty": Decimal("0"), "cost_sum": Decimal("0.00")}
            totals[pid]["qty"] += qty
            totals[pid]["cost_sum"] += (qty * cost)

        averages: Dict[str, Decimal] = {}
        for pid, data in totals.items():
            if data["qty"] > Decimal("0"):
                avg = (data["cost_sum"] / data["qty"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                averages[pid] = avg
            else:
                averages[pid] = Decimal("0.00")
        return averages

    @classmethod
    @transaction.atomic
    def generate_valuation_report(
        cls,
        tenant: Tenant,
        branch: Branch,
        costing_rule: str = ValuationCostingRule.FEFO_LOT_COST,
        user: Any = None,
        notes: str = "",
    ) -> InventoryValuationReport:
        """
        Generates and persists a fully reconciled, multi-category inventory valuation report.
        """
        if user:
            cls.assert_valuation_role(user)

        if branch.tenant_id != tenant.tenant_id:
            raise NotFound({"detail": "Branch does not belong to specified tenant.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        if costing_rule not in ValuationCostingRule.values:
            raise ValidationError(
                {
                    "detail": f"Invalid costing rule '{costing_rule}'. Allowed: {ValuationCostingRule.values}",
                    "error_code": "ERR_INVALID_COSTING_RULE",
                }
            )

        as_of_timestamp = timezone.now()
        today = timezone.localdate()

        # Weighted average cache if requested
        weighted_averages = (
            cls.calculate_weighted_average_costs(tenant, branch)
            if costing_rule == ValuationCostingRule.WEIGHTED_AVERAGE
            else {}
        )

        # 1. Evaluate Physical On-Hand Batches
        on_hand_batches = (
            BatchStock.objects.filter(
                tenant=tenant,
                branch=branch,
                quantity_base_units__gt=0,
            )
            .select_related("product")
            .order_by("product__brand_name", "batch_number")
        )

        total_on_hand_units = 0
        total_on_hand_value_bdt = Decimal("0.00")
        sellable_units = 0
        sellable_value_bdt = Decimal("0.00")
        quarantined_units = 0
        quarantined_value_bdt = Decimal("0.00")
        expired_units = 0
        expired_value_bdt = Decimal("0.00")
        cold_chain_units = 0
        cold_chain_value_bdt = Decimal("0.00")

        line_payloads: List[Dict[str, Any]] = []

        for batch in on_hand_batches:
            product = batch.product
            qty = batch.quantity_base_units

            # Determine unit cost
            if costing_rule == ValuationCostingRule.WEIGHTED_AVERAGE:
                unit_cost = weighted_averages.get(product.product_id, batch.cost_per_unit)
            elif costing_rule == ValuationCostingRule.MRP_RETAIL_VALUE:
                unit_cost = product.effective_mrp_unit or batch.cost_per_unit
            else:  # FEFO_LOT_COST / FIFO_LOT_COST
                unit_cost = batch.cost_per_unit

            unit_cost = unit_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            batch_valuation = (Decimal(qty) * unit_cost).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            is_cold_chain = (
                batch.storage_condition in COLD_CHAIN_CONDITIONS
                or getattr(product, "is_cold_chain", False)
            )

            # Categorize
            is_expired = batch.expiry_date <= today
            if is_expired:
                category = InventoryValuationCategory.EXPIRED
                status_reason = f"Expired on {batch.expiry_date.isoformat()}"
                expired_units += qty
                expired_value_bdt += batch_valuation
            elif batch.is_quarantined or batch.status == BatchStatus.QUARANTINED:
                category = InventoryValuationCategory.QUARANTINED
                status_reason = f"Quarantined: {batch.recall_reason or 'Quality / Cold-Chain hold'}"
                quarantined_units += qty
                quarantined_value_bdt += batch_valuation
            else:
                category = InventoryValuationCategory.SELLABLE
                status_reason = "Active sellable inventory"
                sellable_units += qty
                sellable_value_bdt += batch_valuation

            total_on_hand_units += qty
            total_on_hand_value_bdt += batch_valuation

            if is_cold_chain:
                cold_chain_units += qty
                cold_chain_value_bdt += batch_valuation

            line_payloads.append({
                "product": product,
                "product_id": product.product_id,
                "product_name": product.brand_name,
                "batch": batch,
                "batch_number": batch.batch_number,
                "category": category,
                "quantity_base_units": qty,
                "unit_cost": unit_cost,
                "total_value_bdt": batch_valuation,
                "is_cold_chain": is_cold_chain,
                "status_reason": status_reason,
            })

        # 2. Evaluate Interbranch In-Transit Transfers
        # Items in DISPATCHED status destined for this branch
        in_transit_transfers = (
            InterBranchTransferRequest.objects.filter(
                tenant=tenant,
                destination_branch=branch,
                status=InterBranchTransferStatus.DISPATCHED,
            )
            .prefetch_related("items__product", "items__source_batch")
        )

        in_transit_units = 0
        in_transit_value_bdt = Decimal("0.00")

        for transfer in in_transit_transfers:
            for item in transfer.items.all():
                in_transit_qty = max(0, item.quantity_dispatched - item.quantity_received)
                if in_transit_qty > 0:
                    unit_cost = (item.original_cost_per_unit or Decimal("0.00")).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    item_val = (Decimal(in_transit_qty) * unit_cost).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )

                    in_transit_units += in_transit_qty
                    in_transit_value_bdt += item_val

                    product = item.product
                    is_cold_chain = getattr(product, "is_cold_chain", False)

                    line_payloads.append({
                        "product": product,
                        "product_id": product.product_id,
                        "product_name": product.brand_name,
                        "batch": item.source_batch,
                        "batch_number": item.batch_number,
                        "category": InventoryValuationCategory.IN_TRANSIT,
                        "quantity_base_units": in_transit_qty,
                        "unit_cost": unit_cost,
                        "total_value_bdt": item_val,
                        "is_cold_chain": is_cold_chain,
                        "status_reason": f"In-transit from transfer {transfer.transfer_number}",
                    })

        # 3. Mathematical Reconciliation Check
        # On-hand physical units MUST equal sellable + quarantined + expired
        discrepancy_units = total_on_hand_units - (sellable_units + quarantined_units + expired_units)
        discrepancy_val = total_on_hand_value_bdt - (sellable_value_bdt + quarantined_value_bdt + expired_value_bdt)
        is_balanced = (discrepancy_units == 0 and discrepancy_val == Decimal("0.00"))

        # 4. Compute SHA-256 Checksum
        checksum_payload = [
            {
                "product_id": p["product_id"],
                "batch_number": p["batch_number"],
                "category": p["category"],
                "quantity_base_units": p["quantity_base_units"],
                "unit_cost": str(p["unit_cost"]),
                "total_value_bdt": str(p["total_value_bdt"]),
            }
            for p in line_payloads
        ]
        checksum = cls.compute_sha256_checksum(checksum_payload)

        # 5. Persist Report & Line Items
        report_id = f"valrep-{uuid.uuid4().hex[:12]}"
        report = InventoryValuationReport.objects.create(
            report_id=report_id,
            tenant=tenant,
            branch=branch,
            as_of_timestamp=as_of_timestamp,
            costing_rule=costing_rule,
            total_on_hand_units=total_on_hand_units,
            total_on_hand_value_bdt=total_on_hand_value_bdt,
            sellable_units=sellable_units,
            sellable_value_bdt=sellable_value_bdt,
            quarantined_units=quarantined_units,
            quarantined_value_bdt=quarantined_value_bdt,
            in_transit_units=in_transit_units,
            in_transit_value_bdt=in_transit_value_bdt,
            expired_units=expired_units,
            expired_value_bdt=expired_value_bdt,
            cold_chain_units=cold_chain_units,
            cold_chain_value_bdt=cold_chain_value_bdt,
            is_mathematically_balanced=is_balanced,
            discrepancy_units=discrepancy_units,
            discrepancy_value_bdt=discrepancy_val,
            notes=notes,
            sha256_checksum=checksum,
            generated_by=user if hasattr(user, "pk") and user.is_authenticated else None,
        )

        line_instances = [
            InventoryValuationLineItem(
                report=report,
                product=lp["product"],
                product_name=lp["product_name"],
                batch=lp["batch"],
                batch_number=lp["batch_number"],
                category=lp["category"],
                quantity_base_units=lp["quantity_base_units"],
                unit_cost=lp["unit_cost"],
                total_value_bdt=lp["total_value_bdt"],
                is_cold_chain=lp["is_cold_chain"],
                status_reason=lp["status_reason"],
            )
            for lp in line_payloads
        ]
        InventoryValuationLineItem.objects.bulk_create(line_instances)

        return report

    @classmethod
    def verify_report_integrity(cls, report: InventoryValuationReport) -> Tuple[bool, str]:
        """
        Verifies cryptographic SHA-256 integrity of the valuation report against persisted line items.
        """
        lines = report.line_items.all()
        checksum_payload = [
            {
                "product_id": line.product_id,
                "batch_number": line.batch_number,
                "category": line.category,
                "quantity_base_units": line.quantity_base_units,
                "unit_cost": str(line.unit_cost),
                "total_value_bdt": str(line.total_value_bdt),
            }
            for line in lines
        ]
        recomputed = cls.compute_sha256_checksum(checksum_payload)
        is_valid = recomputed == report.sha256_checksum
        return is_valid, recomputed
