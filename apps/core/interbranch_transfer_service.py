"""
backend/apps/core/interbranch_transfer_service.py
Architecture Decision: ADR-0208 (P208).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Domain Business Service for Multi-Branch Inventory Transfers:
- Tenant-scoped internal multi-branch transfer workflows.
- Sender stock reservation upon request/approval preventing overselling.
- Atomic dispatch (TRANSFER_OUT) and destination receipt (TRANSFER_IN) ledger posting.
- In-transit discrepancy tracking, release of reservations upon cancellation.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    InterBranchTransferItem,
    InterBranchTransferPriority,
    InterBranchTransferRequest,
    InterBranchTransferStatus,
    MedicineProduct,
    StockMovementType,
    Tenant,
)
from apps.core.stock_ledger_service import StockLedgerService

logger = logging.getLogger(__name__)


class InterBranchTransferService:
    """
    Authoritative service governing multi-branch transfer lifecycle:
    REQUESTED -> APPROVED -> DISPATCHED -> RECEIVED (or CANCELLED/REJECTED).
    """

    _STAFF_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "PLATFORM_SUPER_ADMIN",
        "STAFF_PHARMACIST",
        "PHARMACIST",
        "INVENTORY_CLERK",
        "TENANT_OWNER",
    }

    _APPROVAL_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "PLATFORM_SUPER_ADMIN",
        "STAFF_PHARMACIST",
        "PHARMACIST",
        "TENANT_OWNER",
    }

    @classmethod
    def assert_staff_authority(cls, actor_user: Any) -> None:
        """Enforces that Cashiers cannot request or dispatch transfers."""
        if not actor_user:
            return
        role = getattr(actor_user, "role", None)
        role_str = str(role).upper() if role else ""
        if role_str == "CASHIER" or (role_str and role_str not in cls._STAFF_ROLES):
            raise PermissionDenied(
                "Cashiers are not authorized to manage interbranch transfers (ERR_STAFF_ROLE_DENIED)."
            )

    @classmethod
    def assert_approval_authority(cls, actor_user: Any) -> None:
        """Enforces manager/pharmacist approval authority."""
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied("Authentication required.")
        role = getattr(actor_user, "role", None)
        role_str = str(role).upper() if role else ""
        if role_str not in cls._APPROVAL_ROLES:
            raise PermissionDenied(
                f"Role '{role_str}' lacks authority to approve transfers (ERR_APPROVAL_ROLE_DENIED)."
            )

    @classmethod
    @transaction.atomic
    def create_transfer_request(
        cls,
        tenant_id: str,
        source_branch_id: str,
        destination_branch_id: str,
        items_payload: List[Dict[str, Any]],
        priority: str = InterBranchTransferPriority.NORMAL,
        transfer_cost_policy: str = "AT_COST",
        markup_percentage: Decimal = Decimal("0.00"),
        logistics_handling_fee_bdt: Decimal = Decimal("0.0000"),
        notes: str = "",
        actor_user: Any = None,
    ) -> InterBranchTransferRequest:
        """
        Creates an inter-branch transfer request and reserves sender stock.
        Preserves original purchase lot cost, lot reference, and expiry trace (ADR-0210).
        """
        cls.assert_staff_authority(actor_user)

        if source_branch_id == destination_branch_id:
            raise ValidationError({"branches": "Source and destination branches cannot be identical (ERR_SAME_BRANCH_TRANSFER)."})

        if not items_payload:
            raise ValidationError({"items": "At least one item line must be provided for transfer."})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound(f"Tenant '{tenant_id}' not found.")

        source_branch = Branch.objects.filter(tenant=tenant, branch_id=source_branch_id).first()
        dest_branch = Branch.objects.filter(tenant=tenant, branch_id=destination_branch_id).first()

        if not source_branch or not dest_branch:
            raise NotFound("One or both branches do not exist or belong to a different tenant (ERR_CROSS_TENANT_TRANSFER).")

        transfer_id = f"trf-{uuid.uuid4().hex[:10]}"
        transfer_num = f"TRF-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

        transfer = InterBranchTransferRequest.objects.create(
            transfer_id=transfer_id,
            transfer_number=transfer_num,
            tenant=tenant,
            source_branch=source_branch,
            destination_branch=dest_branch,
            status=InterBranchTransferStatus.REQUESTED,
            priority=priority,
            transfer_cost_policy=transfer_cost_policy,
            markup_percentage=markup_percentage,
            logistics_handling_fee_bdt=logistics_handling_fee_bdt,
            notes=notes.strip(),
            requested_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        total_units = 0
        total_valuation = Decimal("0.0000")

        for line in items_payload:
            batch_id = line.get("batch_id")
            qty_req = int(line.get("quantity_requested", 0))

            if qty_req <= 0:
                raise ValidationError({"quantity": "Requested quantity must be greater than zero."})

            batch = (
                BatchStock.objects.select_for_update()
                .filter(tenant=tenant, branch=source_branch, batch_id=batch_id)
                .select_related("product")
                .first()
            )
            if not batch:
                raise NotFound(f"Batch '{batch_id}' not found in source branch inventory.")

            available = batch.quantity_base_units - batch.reserved_quantity_base_units
            if qty_req > available:
                raise ValidationError(
                    {"quantity": f"Cannot transfer {qty_req} units of batch {batch.batch_number}. Only {available} available."}
                )

            # Reserve stock at source branch so it cannot be sold concurrently
            batch.reserved_quantity_base_units += qty_req
            batch.save(update_fields=["reserved_quantity_base_units", "updated_at"])

            orig_cost = Decimal(str(batch.cost_per_unit or "0.0000"))
            
            # Apply Transfer Cost Policy (ADR-0210)
            if transfer_cost_policy == "LANDED_MARKUP" and markup_percentage > 0:
                markup = (orig_cost * (markup_percentage / Decimal("100.00"))).quantize(Decimal("0.0001"))
                transferred_cost = orig_cost + markup
            elif transfer_cost_policy == "FIXED_FEE" and logistics_handling_fee_bdt > 0:
                transferred_cost = (orig_cost + (logistics_handling_fee_bdt / Decimal(str(qty_req)))).quantize(Decimal("0.0001"))
            else:
                transferred_cost = orig_cost

            line_cost = (Decimal(str(qty_req)) * transferred_cost).quantize(Decimal("0.0001"))

            from apps.core.models import PurchaseCostLot
            src_lot = PurchaseCostLot.objects.filter(tenant=tenant, branch=source_branch, batch=batch).first()

            InterBranchTransferItem.objects.create(
                item_id=f"trfitm-{uuid.uuid4().hex[:10]}",
                transfer=transfer,
                product=batch.product,
                source_batch=batch,
                source_cost_lot=src_lot,
                batch_number=batch.batch_number,
                original_lot_reference=batch.lot_reference or batch.batch_number,
                original_cost_per_unit=orig_cost,
                transferred_cost_per_unit=transferred_cost,
                quantity_requested=qty_req,
                quantity_dispatched=0,
                quantity_received=0,
                unit_cost_bdt=transferred_cost,
                total_cost_bdt=line_cost,
                expiry_date=batch.expiry_date,
                source_storage_location=batch.storage_location,
                destination_storage_location=line.get("destination_storage_location", "SHELF-MAIN"),
                item_notes=line.get("notes", ""),
            )

            total_units += qty_req
            total_valuation += line_cost

        transfer.total_items_count = len(items_payload)
        transfer.total_units_requested = total_units
        transfer.total_valuation_bdt = total_valuation
        transfer.save(update_fields=["total_items_count", "total_units_requested", "total_valuation_bdt", "updated_at"])

        return transfer

    @classmethod
    @transaction.atomic
    def approve_transfer(
        cls,
        transfer_id: str,
        tenant_id: str,
        actor_user: Any,
    ) -> InterBranchTransferRequest:
        """Approves a requested transfer."""
        cls.assert_approval_authority(actor_user)

        transfer = (
            InterBranchTransferRequest.objects.select_for_update()
            .filter(tenant_id=tenant_id, transfer_id=transfer_id)
            .first()
        )
        if not transfer:
            raise NotFound(f"Transfer '{transfer_id}' not found.")

        if transfer.status != InterBranchTransferStatus.REQUESTED:
            raise ValidationError({"status": f"Transfer in status '{transfer.status}' cannot be approved (must be REQUESTED)."})

        transfer.status = InterBranchTransferStatus.APPROVED
        transfer.approved_by = actor_user
        transfer.approved_at = timezone.now()
        transfer.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        return transfer

    @classmethod
    @transaction.atomic
    def dispatch_transfer(
        cls,
        transfer_id: str,
        tenant_id: str,
        carrier_notes: str = "",
        actor_user: Any = None,
    ) -> InterBranchTransferRequest:
        """
        Dispatches transfer: physical stock leaves sender branch.
        Emits StockMovementType.TRANSFER_OUT on source branch ledger.
        """
        cls.assert_staff_authority(actor_user)

        transfer = (
            InterBranchTransferRequest.objects.select_for_update()
            .filter(tenant_id=tenant_id, transfer_id=transfer_id)
            .select_related("source_branch", "destination_branch", "tenant")
            .prefetch_related("items", "items__source_batch", "items__product")
            .first()
        )
        if not transfer:
            raise NotFound(f"Transfer '{transfer_id}' not found.")

        if transfer.status != InterBranchTransferStatus.APPROVED:
            raise ValidationError({"status": f"Transfer must be APPROVED before dispatch (current: '{transfer.status}')."})

        total_dispatched = 0

        for item in transfer.items.all():
            qty_to_dispatch = item.quantity_requested
            batch = (
                BatchStock.objects.select_for_update()
                .filter(pk=item.source_batch_id)
                .first()
            )
            if not batch:
                raise NotFound(f"Batch for item {item.item_id} not found.")

            # Release reserved units from source batch
            if batch.reserved_quantity_base_units >= qty_to_dispatch:
                batch.reserved_quantity_base_units -= qty_to_dispatch
            else:
                batch.reserved_quantity_base_units = 0
            batch.save(update_fields=["reserved_quantity_base_units", "updated_at"])

            # Record stock movement (TRANSFER_OUT)
            ledger_entry = StockLedgerService.record_stock_movement(
                tenant=transfer.tenant,
                branch=transfer.source_branch,
                product=item.product,
                movement_type=StockMovementType.TRANSFER_OUT,
                quantity_delta=-qty_to_dispatch,
                source_entity_type="INTERBRANCH_TRANSFER",
                source_entity_id=transfer.transfer_id,
                source_reference=transfer.transfer_number,
                batch=batch,
                batch_number=batch.batch_number,
                unit_cost=item.unit_cost_bdt,
                storage_location=batch.storage_location,
                notes=f"Dispatch to {transfer.destination_branch.name}. {carrier_notes}".strip(),
                posted_by=actor_user,
            )

            item.quantity_dispatched = qty_to_dispatch
            item.dispatch_ledger_entry_id = ledger_entry.entry_id
            item.save(update_fields=["quantity_dispatched", "dispatch_ledger_entry_id"])

            total_dispatched += qty_to_dispatch

        transfer.status = InterBranchTransferStatus.DISPATCHED
        transfer.dispatched_by = actor_user
        transfer.dispatched_at = timezone.now()
        transfer.dispatch_carrier_notes = carrier_notes.strip()
        transfer.total_units_dispatched = total_dispatched
        transfer.save(update_fields=["status", "dispatched_by", "dispatched_at", "dispatch_carrier_notes", "total_units_dispatched", "updated_at"])

        return transfer

    @classmethod
    @transaction.atomic
    def receive_transfer(
        cls,
        transfer_id: str,
        tenant_id: str,
        received_items_payload: Optional[List[Dict[str, Any]]] = None,
        receiving_notes: str = "",
        actor_user: Any = None,
    ) -> InterBranchTransferRequest:
        """
        Receives dispatched transfer at destination branch with full partial/damaged
        and transit loss reconciliation. Prevents phantom stock, lost in-transit items,
        and duplicate stock creation.
        """
        cls.assert_staff_authority(actor_user)

        transfer = (
            InterBranchTransferRequest.objects.select_for_update()
            .filter(tenant_id=tenant_id, transfer_id=transfer_id)
            .select_related("source_branch", "destination_branch", "tenant")
            .prefetch_related("items", "items__product")
            .first()
        )
        if not transfer:
            raise NotFound(f"Transfer '{transfer_id}' not found.")

        # Prevent double receiving / duplicate stock
        if transfer.status in [InterBranchTransferStatus.RECEIVED, InterBranchTransferStatus.RECONCILED]:
            raise ValidationError({"status": f"Transfer '{transfer_id}' has already been received/reconciled (ERR_TRANSFER_ALREADY_RECEIVED)."})

        if transfer.status != InterBranchTransferStatus.DISPATCHED:
            raise ValidationError({"status": f"Transfer must be DISPATCHED before receiving (current: '{transfer.status}')."})

        received_map = {}
        if received_items_payload:
            for r in received_items_payload:
                received_map[r.get("item_id")] = r

        total_received = 0
        total_good = 0
        total_damaged = 0
        total_lost = 0
        has_discrepancy = False

        for item in transfer.items.all():
            line_data = received_map.get(item.item_id, {})

            # Check good, damaged, lost
            if "quantity_received_good" in line_data or "quantity_received_damaged" in line_data or "quantity_lost" in line_data:
                qty_good = int(line_data.get("quantity_received_good", 0))
                qty_damaged = int(line_data.get("quantity_received_damaged", 0))
                qty_lost = int(line_data.get("quantity_lost", 0))
            elif "quantity_received" in line_data:
                qty_received_input = int(line_data.get("quantity_received", 0))
                qty_good = qty_received_input
                qty_damaged = 0
                qty_lost = max(0, item.quantity_dispatched - qty_good)
            else:
                # Default: 100% good receipt
                qty_good = item.quantity_dispatched
                qty_damaged = 0
                qty_lost = 0

            if qty_good < 0 or qty_damaged < 0 or qty_lost < 0:
                raise ValidationError({"quantity": "Received, damaged, and lost quantities must be non-negative."})

            # Invariant: Good + Damaged + Lost MUST equal Dispatched
            total_accounted = qty_good + qty_damaged + qty_lost
            if total_accounted != item.quantity_dispatched:
                raise ValidationError({
                    "reconciliation": (
                        f"Discrepancy conservation failure for item {item.item_id} ({item.product.brand_name}): "
                        f"Good ({qty_good}) + Damaged ({qty_damaged}) + Lost ({qty_lost}) = {total_accounted}, "
                        f"which does not match dispatched quantity ({item.quantity_dispatched})."
                    )
                })

            discrepancy_reason = line_data.get("discrepancy_reason", "")
            if (qty_damaged > 0 or qty_lost > 0) and not discrepancy_reason:
                discrepancy_reason = line_data.get("notes", "Discrepancy noted during receiving inspection.")

            if qty_damaged > 0 or qty_lost > 0:
                has_discrepancy = True

            dest_location = line_data.get("destination_storage_location") or item.destination_storage_location or "SHELF-MAIN"
            damage_location = line_data.get("damage_storage_location") or "QUARANTINE-DAMAGED"

            receipt_ledger_id = ""
            damage_ledger_id = ""

            # 1. Post Good Stock to Destination Batch
            if qty_good > 0:
                dest_batch = (
                    BatchStock.objects.select_for_update()
                    .filter(
                        tenant=transfer.tenant,
                        branch=transfer.destination_branch,
                        product=item.product,
                        batch_number=item.batch_number,
                        status=BatchStatus.ACTIVE,
                    )
                    .first()
                )

                transferred_unit_cost = item.transferred_cost_per_unit or item.unit_cost_bdt
                effective_expiry = item.expiry_date or (item.source_batch.expiry_date if item.source_batch else timezone.now().date() + timezone.timedelta(days=365))

                if not dest_batch:
                    dest_batch = BatchStock.objects.create(
                        batch_id=f"batch-{uuid.uuid4().hex[:8]}",
                        tenant=transfer.tenant,
                        branch=transfer.destination_branch,
                        product=item.product,
                        batch_number=item.batch_number,
                        purchase_date=item.source_batch.purchase_date if item.source_batch else timezone.now().date(),
                        expiry_date=effective_expiry,
                        quantity_base_units=0,
                        cost_per_unit=transferred_unit_cost,
                        storage_location=dest_location,
                        status=BatchStatus.ACTIVE,
                        origin_batch=item.source_batch,
                        origin_branch=transfer.source_branch,
                        transfer_cost_policy=transfer.transfer_cost_policy,
                        original_cost_per_unit=item.original_cost_per_unit,
                        transfer_cost_markup_bdt=transferred_unit_cost - item.original_cost_per_unit,
                        lot_reference=item.original_lot_reference,
                        manufacturing_date=item.source_batch.manufacturing_date if item.source_batch else None,
                        storage_condition=item.source_batch.storage_condition if item.source_batch else "ROOM_TEMPERATURE_15_25C",
                    )
                else:
                    # Enforce expiry date consistency: transfers cannot alter manufacturer expiry
                    if dest_batch.expiry_date != effective_expiry:
                        raise ValidationError({
                            "expiry_date": f"ERR_EXPIRY_DATE_ALTERATION_PROHIBITED: Transferred batch expiry ({effective_expiry}) does not match existing destination batch expiry ({dest_batch.expiry_date})."
                        })
                    # Weighted average cost update if existing stock exists
                    if dest_batch.quantity_base_units > 0:
                        existing_qty = Decimal(str(dest_batch.quantity_base_units))
                        existing_cost = Decimal(str(dest_batch.cost_per_unit or "0.0000"))
                        incoming_qty = Decimal(str(qty_good))
                        incoming_cost = Decimal(str(transferred_unit_cost))
                        new_cost = ((existing_qty * existing_cost) + (incoming_qty * incoming_cost)) / (existing_qty + incoming_qty)
                        dest_batch.cost_per_unit = new_cost.quantize(Decimal("0.01"))
                        dest_batch.save(update_fields=["cost_per_unit", "updated_at"])

                # Carry forward PurchaseCostLot continuity if source lot existed
                if item.source_cost_lot:
                    from apps.core.models import PurchaseCostLot, PurchaseCostLotStatus
                    dest_cost_lot = PurchaseCostLot.objects.create(
                        lot_id=f"lot-{uuid.uuid4().hex[:10]}",
                        tenant=transfer.tenant,
                        branch=transfer.destination_branch,
                        product=item.product,
                        batch=dest_batch,
                        supplier=item.source_cost_lot.supplier,
                        supplier_name=item.source_cost_lot.supplier_name,
                        purchase_order_number=item.source_cost_lot.purchase_order_number,
                        grn_number=transfer.transfer_number,
                        invoice_reference=item.source_cost_lot.invoice_reference,
                        purchase_date=item.source_cost_lot.purchase_date,
                        unit_cost=transferred_unit_cost,
                        initial_quantity=qty_good,
                        remaining_quantity=qty_good,
                        status=PurchaseCostLotStatus.ACTIVE,
                        notes=f"Lot continuity from {transfer.source_branch.name} via {transfer.transfer_number}",
                    )
                    item.destination_cost_lot = dest_cost_lot

                ledger_good = StockLedgerService.record_stock_movement(
                    tenant=transfer.tenant,
                    branch=transfer.destination_branch,
                    product=item.product,
                    movement_type=StockMovementType.TRANSFER_IN,
                    quantity_delta=qty_good,
                    source_entity_type="INTERBRANCH_TRANSFER",
                    source_entity_id=transfer.transfer_id,
                    source_reference=transfer.transfer_number,
                    batch=dest_batch,
                    batch_number=dest_batch.batch_number,
                    unit_cost=transferred_unit_cost,
                    storage_location=dest_batch.storage_location,
                    notes=f"Good stock received from {transfer.source_branch.name}. {receiving_notes}".strip(),
                    posted_by=actor_user,
                )
                receipt_ledger_id = ledger_good.entry_id

            # 2. Post Damaged Stock to Segregated Quarantine Batch (Barred from sellable inventory)
            if qty_damaged > 0:
                dest_damaged_batch = (
                    BatchStock.objects.select_for_update()
                    .filter(
                        tenant=transfer.tenant,
                        branch=transfer.destination_branch,
                        product=item.product,
                        batch_number=f"{item.batch_number}-DMG",
                        status=BatchStatus.QUARANTINED,
                    )
                    .first()
                )

                if not dest_damaged_batch:
                    dest_damaged_batch = BatchStock.objects.create(
                        batch_id=f"batch-dmg-{uuid.uuid4().hex[:8]}",
                        tenant=transfer.tenant,
                        branch=transfer.destination_branch,
                        product=item.product,
                        batch_number=f"{item.batch_number}-DMG",
                        purchase_date=timezone.now().date(),
                        expiry_date=item.expiry_date or (timezone.now().date() + timezone.timedelta(days=365)),
                        quantity_base_units=0,
                        cost_per_unit=item.unit_cost_bdt,
                        storage_location=damage_location,
                        status=BatchStatus.QUARANTINED,
                    )

                ledger_damaged = StockLedgerService.record_stock_movement(
                    tenant=transfer.tenant,
                    branch=transfer.destination_branch,
                    product=item.product,
                    movement_type=StockMovementType.TRANSFER_IN,
                    quantity_delta=qty_damaged,
                    source_entity_type="INTERBRANCH_TRANSFER_DAMAGED",
                    source_entity_id=transfer.transfer_id,
                    source_reference=transfer.transfer_number,
                    batch=dest_damaged_batch,
                    batch_number=dest_damaged_batch.batch_number,
                    unit_cost=item.unit_cost_bdt,
                    storage_location=damage_location,
                    notes=f"Damaged in transit from {transfer.source_branch.name}. {discrepancy_reason}".strip(),
                    posted_by=actor_user,
                )
                damage_ledger_id = ledger_damaged.entry_id

            # Update line item reconciliation details
            item.quantity_received = qty_good + qty_damaged
            item.quantity_received_good = qty_good
            item.quantity_received_damaged = qty_damaged
            item.quantity_lost = qty_lost
            item.discrepancy_reason = discrepancy_reason
            item.damage_storage_location = damage_location if qty_damaged > 0 else ""
            item.receipt_ledger_entry_id = receipt_ledger_id
            item.damage_ledger_entry_id = damage_ledger_id
            item.is_reconciled = True
            item.save(update_fields=[
                "quantity_received",
                "quantity_received_good",
                "quantity_received_damaged",
                "quantity_lost",
                "discrepancy_reason",
                "damage_storage_location",
                "receipt_ledger_entry_id",
                "damage_ledger_entry_id",
                "is_reconciled",
            ])

            total_received += (qty_good + qty_damaged)
            total_good += qty_good
            total_damaged += qty_damaged
            total_lost += qty_lost

        final_status = (
            InterBranchTransferStatus.RECONCILED
            if has_discrepancy
            else InterBranchTransferStatus.RECEIVED
        )

        transfer.status = final_status
        transfer.received_by = actor_user
        transfer.received_at = timezone.now()
        transfer.receiving_notes = receiving_notes.strip()
        transfer.reconciled_by = actor_user
        transfer.reconciled_at = timezone.now()
        transfer.reconciliation_notes = (
            f"Reconciled with discrepancy: {total_good} good, {total_damaged} damaged, {total_lost} lost in transit."
            if has_discrepancy
            else "Fully received without discrepancy."
        )
        transfer.total_units_received = total_received
        transfer.total_units_received_good = total_good
        transfer.total_units_received_damaged = total_damaged
        transfer.total_units_lost = total_lost
        transfer.save(update_fields=[
            "status",
            "received_by",
            "received_at",
            "receiving_notes",
            "reconciled_by",
            "reconciled_at",
            "reconciliation_notes",
            "total_units_received",
            "total_units_received_good",
            "total_units_received_damaged",
            "total_units_lost",
            "updated_at",
        ])

        return transfer

    @classmethod
    @transaction.atomic
    def cancel_transfer(
        cls,
        transfer_id: str,
        tenant_id: str,
        cancellation_reason: str,
        actor_user: Any,
    ) -> InterBranchTransferRequest:
        """
        Cancels transfer prior to dispatch and safely releases reserved stock.
        """
        cls.assert_staff_authority(actor_user)

        transfer = (
            InterBranchTransferRequest.objects.select_for_update()
            .filter(tenant_id=tenant_id, transfer_id=transfer_id)
            .prefetch_related("items", "items__source_batch")
            .first()
        )
        if not transfer:
            raise NotFound(f"Transfer '{transfer_id}' not found.")

        if transfer.status not in (InterBranchTransferStatus.REQUESTED, InterBranchTransferStatus.APPROVED):
            raise ValidationError({"status": f"Cannot cancel transfer in status '{transfer.status}'."})

        clean_reason = cancellation_reason.strip()
        if len(clean_reason) < 5:
            raise ValidationError({"cancellation_reason": "Cancellation reason required."})

        # Release all reserved units from source batches
        for item in transfer.items.all():
            batch = (
                BatchStock.objects.select_for_update()
                .filter(pk=item.source_batch_id)
                .first()
            )
            if batch:
                if batch.reserved_quantity_base_units >= item.quantity_requested:
                    batch.reserved_quantity_base_units -= item.quantity_requested
                else:
                    batch.reserved_quantity_base_units = 0
                batch.save(update_fields=["reserved_quantity_base_units", "updated_at"])

        transfer.status = InterBranchTransferStatus.CANCELLED
        transfer.rejection_reason = clean_reason
        transfer.save(update_fields=["status", "rejection_reason", "updated_at"])

        return transfer

