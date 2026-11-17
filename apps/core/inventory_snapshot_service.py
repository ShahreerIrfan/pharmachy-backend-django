"""
backend/apps/core/inventory_snapshot_service.py
Architecture Decision: ADR-0215 (P215).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Domain Service for Inventory Snapshots & Read-Model Aggregation:
- Builds fast cacheable read models strictly derived from canonical StockLedgerEntry transitions.
- Reconciles physical batch balances with ledger movement delta sums.
- Generates SHA-256 cryptographic tamper-evident checksums over snapshot line items.
- Supports scheduled and on-demand checkpoints (EOD, Monthly Close, Cycle Count).
- Enforces strict multi-tenant isolation and role-based permissions (Cashier denied).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    InventorySnapshotCheckpoint,
    InventorySnapshotLineItem,
    MedicineProduct,
    SnapshotCheckpointType,
    StockLedgerEntry,
    Tenant,
)
from apps.identity.models import UserRole


class InventorySnapshotService:
    """
    Business service managing immutable inventory snapshots and reconciliation checkpoints.
    """

    ALLOWED_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "SUPERADMIN",
        "STAFF_PHARMACIST",
        "PROCUREMENT_OFFICER",
        "INVENTORY_CLERK",
    }

    @classmethod
    def assert_inventory_role(cls, user: Any) -> None:
        """
        Enforce that only authorized personnel can generate or inspect inventory snapshots.
        Cashiers are prohibited.
        """
        role = getattr(user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str == "CASHIER" or role_str not in cls.ALLOWED_ROLES:
            raise PermissionDenied(
                "Cashiers cannot generate or audit inventory snapshots. Access is restricted to Managers, Owners, and Pharmacists."
            )

    @classmethod
    def compute_checksum(cls, lines_data: List[Dict[str, Any]]) -> str:
        """
        Computes deterministic SHA-256 checksum over sorted line items.
        """
        sorted_lines = sorted(
            lines_data,
            key=lambda x: (x.get("product_id", ""), x.get("batch_number", "")),
        )
        hasher = hashlib.sha256()
        for l in sorted_lines:
            token = f"{l.get('product_id')}|{l.get('batch_number')}|{l.get('quantity_base_units')}|{l.get('unit_cost')}|{l.get('total_valuation_bdt')}\n"
            hasher.update(token.encode("utf-8"))
        return hasher.hexdigest()

    @classmethod
    @transaction.atomic
    def generate_snapshot(
        cls,
        tenant: Tenant,
        branch: Branch,
        checkpoint_type: str = SnapshotCheckpointType.ON_DEMAND,
        as_of_timestamp: Optional[Any] = None,
        actor_user: Any = None,
    ) -> InventorySnapshotCheckpoint:
        """
        Compiles a cacheable inventory snapshot checkpoint strictly derived
        from canonical StockLedgerEntry transactions up to as_of_timestamp.
        """
        if actor_user:
            cls.assert_inventory_role(actor_user)

        if tenant.tenant_id != branch.tenant_id:
            raise ValidationError({"branch": "Branch does not belong to specified tenant."})

        cutoff = as_of_timestamp or timezone.now()

        # Fetch all ledger entries up to cutoff
        ledger_qs = StockLedgerEntry.objects.filter(
            tenant=tenant,
            branch=branch,
            posted_at__lte=cutoff,
        ).order_by("posted_at")

        ledger_count = ledger_qs.count()
        last_entry = ledger_qs.last()

        # Compute sum of quantity_delta by (product_id, batch_number)
        ledger_aggregates = (
            ledger_qs.values("product_id", "batch_number")
            .annotate(delta_sum=Sum("quantity_delta"))
            .order_by("product_id", "batch_number")
        )

        ledger_deltas_map = {
            (item["product_id"], item["batch_number"]): item["delta_sum"] or 0
            for item in ledger_aggregates
        }

        # Query all active or historical batch stock in branch
        batches = list(
            BatchStock.objects.filter(
                tenant=tenant,
                branch=branch,
            ).select_related("product")
        )

        batches_map = {
            (b.product_id, b.batch_number): b
            for b in batches
        }

        all_keys = set(ledger_deltas_map.keys()) | set(batches_map.keys())

        products_seen = set()
        batches_seen = set()
        total_quantity = 0
        total_valuation = Decimal("0.00")
        is_all_reconciled = True

        line_items_data = []
        line_item_models = []

        snapshot_id = f"snap-{uuid.uuid4().hex[:12]}"

        # Pre-instantiate checkpoint container
        checkpoint = InventorySnapshotCheckpoint(
            snapshot_id=snapshot_id,
            tenant=tenant,
            branch=branch,
            checkpoint_type=checkpoint_type,
            as_of_timestamp=cutoff,
            ledger_entries_processed=ledger_count,
            last_included_ledger_entry=last_entry,
            created_by=actor_user,
        )

        for prod_id, b_num in all_keys:
            batch_obj = batches_map.get((prod_id, b_num))
            ledger_sum = ledger_deltas_map.get((prod_id, b_num), 0)

            if batch_obj:
                prod = batch_obj.product
                batch_qty = batch_obj.quantity_base_units
                unit_cost = getattr(batch_obj, "cost_per_unit", None) or Decimal("0.00")
                loc_code = batch_obj.storage_location or ""
            else:
                prod = MedicineProduct.objects.filter(product_id=prod_id).first()
                if not prod:
                    continue
                batch_qty = ledger_sum
                unit_cost = Decimal("0.00")
                loc_code = ""

            discrepancy = batch_qty - ledger_sum
            if discrepancy != 0:
                is_all_reconciled = False

            # We use ledger_sum if available, otherwise physical balance
            effective_qty = ledger_sum if ledger_sum != 0 else batch_qty
            line_val = Decimal(str(effective_qty)) * unit_cost
            if line_val < Decimal("0.00"):
                line_val = Decimal("0.00")

            products_seen.add(prod.product_id)
            batches_seen.add(b_num)
            total_quantity += effective_qty
            total_valuation += line_val

            line_data = {
                "product_id": str(prod.product_id),
                "batch_number": b_num,
                "quantity_base_units": effective_qty,
                "unit_cost": str(unit_cost),
                "total_valuation_bdt": str(line_val),
            }
            line_items_data.append(line_data)

            line_item_models.append(
                InventorySnapshotLineItem(
                    snapshot=checkpoint,
                    product=prod,
                    product_name=prod.brand_name,
                    batch=batch_obj,
                    batch_number=b_num,
                    storage_location_code=loc_code,
                    quantity_base_units=effective_qty,
                    unit_cost=unit_cost,
                    total_valuation_bdt=line_val,
                    is_cold_chain=getattr(prod, "requires_cold_chain", False),
                    ledger_delta_sum=ledger_sum,
                    reconciled_discrepancy=discrepancy,
                )
            )

        checksum = cls.compute_checksum(line_items_data)

        checkpoint.total_products_count = len(products_seen)
        checkpoint.total_batches_count = len(batches_seen)
        checkpoint.total_quantity_base_units = total_quantity
        checkpoint.total_valuation_bdt = total_valuation
        checkpoint.sha256_checksum = checksum
        checkpoint.is_canonical_reconciled = is_all_reconciled
        checkpoint.save()

        # Bulk create lines
        InventorySnapshotLineItem.objects.bulk_create(line_item_models)

        return checkpoint

    @classmethod
    def verify_snapshot_integrity(
        cls,
        snapshot: InventorySnapshotCheckpoint,
    ) -> Tuple[bool, str]:
        """
        Recomputes checksum over stored line items to ensure cryptographic integrity.
        """
        lines = snapshot.line_items.all()
        lines_data = [
            {
                "product_id": str(line.product_id),
                "batch_number": line.batch_number,
                "quantity_base_units": line.quantity_base_units,
                "unit_cost": str(line.unit_cost),
                "total_valuation_bdt": str(line.total_valuation_bdt),
            }
            for line in lines
        ]
        recomputed = cls.compute_checksum(lines_data)
        matches = recomputed == snapshot.sha256_checksum
        return matches, recomputed
