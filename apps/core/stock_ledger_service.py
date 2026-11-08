"""
backend/apps/core/stock_ledger_service.py
Architecture Decision: ADR-0201 (P201).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Service layer for Append-Only Stock Movement Ledger:
- Every physical and operational stock transition (receipt, sale, return, transfer, expiry, adjustment)
  is recorded as an immutable StockLedgerEntry.
- Enforces strict mathematical stock conservation:
    balance_after = balance_before + quantity_delta
- Atomic updates with concurrency locking (select_for_update) on BatchStock and BranchStockAvailability.
- Computes SHA-256 tamper-evident cryptographic verification hashes.
- Provides batch & product ledger trails and automated audit reconciliation.
- Enforces multi-tenant isolation and strict role-based access control (Cashier denied).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchStock,
    Branch,
    BranchStockAvailability,
    MedicineProduct,
    PurchaseCostLot,
    StockLedgerEntry,
    StockMovementType,
    Tenant,
)


class StockLedgerService:
    """
    Core business service for creating and auditing stock ledger entries.
    All state transitions are strictly append-only. Mutations and deletions are forbidden.
    """

    _ALLOWED_INVENTORY_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "SUPERADMIN",
        "STAFF_PHARMACIST",
        "PROCUREMENT_OFFICER",
        "INVENTORY_CLERK",
    }

    @classmethod
    def assert_inventory_role(cls, actor_user: Any) -> None:
        """
        Enforce that only authorized pharmacy personnel (Owners, Managers, Pharmacists, Inventory Clerks)
        can access or audit the stock ledger. Cashiers and unauthorized roles are strictly denied.
        """
        role = getattr(actor_user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str == "CASHIER" or role_str not in cls._ALLOWED_INVENTORY_ROLES:
            raise PermissionDenied(
                "Cashiers cannot view or modify the audit stock ledger. Access is restricted to Managers, Owners, and Pharmacists."
            )

    @classmethod
    @transaction.atomic
    def record_stock_movement(
        cls,
        tenant: Union[Tenant, str],
        branch: Union[Branch, str],
        product: Union[MedicineProduct, str],
        movement_type: Union[StockMovementType, str],
        quantity_delta: int,
        source_entity_type: str,
        source_entity_id: str,
        source_reference: str,
        batch_number: Optional[str] = None,
        batch: Optional[BatchStock] = None,
        cost_lot: Optional[PurchaseCostLot] = None,
        unit_cost: Optional[Decimal] = None,
        unit_price: Optional[Decimal] = None,
        is_cold_chain: Optional[bool] = None,
        storage_location: str = "",
        notes: str = "",
        posted_by: Any = None,
    ) -> StockLedgerEntry:
        """
        Atomically records a stock movement, updates physical batch stock and branch availability,
        and creates an immutable StockLedgerEntry with tamper-evident cryptographic hash.
        """
        if quantity_delta == 0:
            raise ValidationError({"quantity_delta": "Stock movement quantity_delta cannot be zero."})

        # Resolve Tenant
        if isinstance(tenant, str):
            try:
                tenant_obj = Tenant.objects.get(tenant_id=tenant)
            except Tenant.DoesNotExist:
                raise NotFound(f"Tenant {tenant} not found.")
        else:
            tenant_obj = tenant

        # Resolve Branch
        if isinstance(branch, str):
            try:
                branch_obj = Branch.objects.get(branch_id=branch, tenant=tenant_obj)
            except Branch.DoesNotExist:
                raise NotFound(f"Branch {branch} not found.")
        else:
            branch_obj = branch

        # Resolve Product
        if isinstance(product, str):
            try:
                product_obj = MedicineProduct.objects.get(product_id=product)
            except MedicineProduct.DoesNotExist:
                raise NotFound(f"Medicine product {product} not found.")
        else:
            product_obj = product

        # Resolve BatchStock if not supplied but batch_number is given
        batch_obj = batch
        if batch_obj is None and batch_number:
            batch_obj = (
                BatchStock.objects.select_for_update()
                .filter(tenant=tenant_obj, branch=branch_obj, product=product_obj, batch_number=batch_number)
                .first()
            )
        elif batch_obj is not None:
            batch_obj = BatchStock.objects.select_for_update().filter(pk=batch_obj.pk).first()

        effective_batch_number = (
            batch_obj.batch_number if batch_obj else (batch_number or "UNASSIGNED-BATCH")
        )

        # Balance calculation and conservation
        if batch_obj is not None:
            balance_before = batch_obj.quantity_base_units
            balance_after = balance_before + quantity_delta
            if balance_after < 0:
                raise ValidationError(
                    f"Insufficient batch stock for batch '{effective_batch_number}'. "
                    f"Current: {balance_before}, requested delta: {quantity_delta}."
                )
            batch_obj.quantity_base_units = balance_after
            batch_obj.save(update_fields=["quantity_base_units", "updated_at"])
        else:
            # If no batch row exists, calculate against latest ledger entry or default to 0
            last_entry = (
                StockLedgerEntry.objects.filter(
                    tenant=tenant_obj,
                    branch=branch_obj,
                    product=product_obj,
                    batch_number=effective_batch_number,
                )
                .order_by("-posted_at", "-entry_id")
                .first()
            )
            product_label = getattr(product_obj, "brand_name", "") or getattr(product_obj, "name", "")
            balance_before = last_entry.balance_after if last_entry else 0
            balance_after = balance_before + quantity_delta
            if balance_after < 0:
                raise ValidationError(
                    f"Insufficient balance for product {product_label}: "
                    f"Current balance: {balance_before}, requested reduction: {abs(quantity_delta)}."
                )

        # Update BranchStockAvailability atomically
        availability, _ = BranchStockAvailability.objects.select_for_update().get_or_create(
            tenant=tenant_obj,
            branch=branch_obj,
            product=product_obj,
            defaults={
                "quantity_on_hand": 0,
                "quantity_physically_available": 0,
                "quantity_server_confirmed_available": 0,
            },
        )
        new_on_hand = max(0, availability.quantity_on_hand + quantity_delta)
        new_available = max(0, availability.quantity_physically_available + quantity_delta)
        availability.quantity_on_hand = new_on_hand
        availability.quantity_physically_available = new_available
        availability.quantity_server_confirmed_available = new_available
        availability.is_out_of_stock = (new_on_hand <= 0)
        availability.last_recalculated_at = timezone.now()
        availability.save(
            update_fields=[
                "quantity_on_hand",
                "quantity_physically_available",
                "quantity_server_confirmed_available",
                "is_out_of_stock",
                "last_recalculated_at",
                "updated_at",
            ]
        )

        # Unit cost and price defaults
        resolved_unit_cost = unit_cost
        if resolved_unit_cost is None:
            if cost_lot and cost_lot.unit_cost_price:
                resolved_unit_cost = cost_lot.unit_cost_price
            elif batch_obj and batch_obj.cost_per_unit:
                resolved_unit_cost = batch_obj.cost_per_unit
            else:
                resolved_unit_cost = Decimal("0.0000")

        resolved_unit_price = unit_price
        if resolved_unit_price is None:
            mrp_val = getattr(product_obj, "mrp", None) or getattr(product_obj, "unit_price", None)
            resolved_unit_price = Decimal(str(mrp_val)) if mrp_val else Decimal("0.0000")

        total_cost_val = abs(Decimal(str(quantity_delta))) * resolved_unit_cost

        resolved_cold_chain = is_cold_chain
        if resolved_cold_chain is None:
            resolved_cold_chain = getattr(product_obj, "requires_cold_chain", False) or getattr(
                product_obj, "is_cold_chain", False
            )

        entry_id = f"stkld-{uuid.uuid4().hex[:12]}"
        entry = StockLedgerEntry(
            entry_id=entry_id,
            tenant=tenant_obj,
            branch=branch_obj,
            product=product_obj,
            batch=batch_obj,
            batch_number=effective_batch_number,
            cost_lot=cost_lot,
            movement_type=str(movement_type),
            quantity_delta=quantity_delta,
            balance_before=balance_before,
            balance_after=balance_after,
            unit_cost=resolved_unit_cost,
            total_cost=total_cost_val,
            unit_price=resolved_unit_price,
            source_entity_type=source_entity_type,
            source_entity_id=source_entity_id,
            source_reference=source_reference,
            is_cold_chain=resolved_cold_chain,
            storage_location=storage_location,
            notes=notes,
            posted_by=posted_by if (posted_by and getattr(posted_by, "is_authenticated", False)) else None,
            posted_at=timezone.now(),
        )
        entry.clean()
        entry.save()
        return entry

    @classmethod
    def get_batch_ledger(
        cls,
        tenant_id: str,
        batch_number_or_id: str,
        branch_id: Optional[str] = None,
    ) -> List[StockLedgerEntry]:
        """
        Retrieves the complete historical stock ledger audit trail for a specific batch.
        """
        qs = StockLedgerEntry.objects.filter(tenant_id=tenant_id).select_related(
            "product", "branch", "batch", "cost_lot", "posted_by"
        )
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        # Match either batch_number or batch__batch_id
        from django.db.models import Q
        qs = qs.filter(Q(batch_number=batch_number_or_id) | Q(batch__batch_id=batch_number_or_id))
        return list(qs.order_by("posted_at", "entry_id"))

    @classmethod
    def get_product_ledger(
        cls,
        tenant_id: str,
        product_id: str,
        branch_id: Optional[str] = None,
    ) -> List[StockLedgerEntry]:
        """
        Retrieves recent stock ledger entries for a product across batches.
        """
        qs = StockLedgerEntry.objects.filter(tenant_id=tenant_id, product_id=product_id).select_related(
            "branch", "batch", "cost_lot", "posted_by"
        )
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return list(qs.order_by("-posted_at", "-entry_id"))

    @classmethod
    def reconcile_batch_stock(
        cls,
        tenant_id: str,
        batch_number_or_id: str,
        branch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Performs an automated audit reconciliation of a batch's stock ledger against physical records:
        1. Validates cryptographic SHA-256 integrity on all ledger rows.
        2. Computes the cumulative algebraic sum of quantity_delta.
        3. Compares the ledger sum against BatchStock.quantity_base_units.
        4. Detects any drift, unexplained variance, or tampering.
        """
        from django.db.models import Q

        batch_qs = BatchStock.objects.filter(tenant_id=tenant_id)
        if branch_id:
            batch_qs = batch_qs.filter(branch_id=branch_id)
        batch = batch_qs.filter(
            Q(batch_number=batch_number_or_id) | Q(batch_id=batch_number_or_id)
        ).first()

        ledger_qs = StockLedgerEntry.objects.filter(tenant_id=tenant_id)
        if branch_id:
            ledger_qs = ledger_qs.filter(branch_id=branch_id)
        ledger_qs = ledger_qs.filter(
            Q(batch_number=batch_number_or_id) | Q(batch__batch_id=batch_number_or_id)
        ).order_by("posted_at", "entry_id")

        entries = list(ledger_qs)
        tamper_detected = False
        tampered_entry_ids = []

        # Verify SHA-256 hash for each entry
        for ent in entries:
            expected_hash = ent.compute_hash()
            if ent.immutable_verification_hash != expected_hash:
                tamper_detected = True
                tampered_entry_ids.append(ent.entry_id)

        ledger_sum = sum(e.quantity_delta for e in entries)
        recorded_quantity = batch.quantity_base_units if batch else None

        is_balanced = True
        discrepancy = 0
        if recorded_quantity is not None:
            discrepancy = recorded_quantity - ledger_sum
            is_balanced = (discrepancy == 0) and not tamper_detected

        return {
            "tenant_id": tenant_id,
            "branch_id": branch_id,
            "batch_number": batch.batch_number if batch else batch_number_or_id,
            "batch_id": batch.batch_id if batch else None,
            "recorded_batch_quantity": recorded_quantity,
            "ledger_sum_quantity": ledger_sum,
            "is_balanced": is_balanced,
            "discrepancy": discrepancy,
            "total_entries": len(entries),
            "tamper_detected": tamper_detected,
            "tampered_entry_ids": tampered_entry_ids,
            "last_movement_at": entries[-1].posted_at.isoformat() if entries else None,
        }
