"""
backend/apps/core/stock_quarantine_service.py
Architecture Decision: ADR-0206 (P206).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Domain Business Service for Expired Stock Quarantine:
- Automated expired batch quarantine sweeps.
- Immediate prohibition of sale, reservation, or dispensing for expired/quarantined batches.
- Approved disposal, vendor return, and supervised destruction workflows.
- Immutable audit trail and Stock Movement Ledger double-entry synchronization.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    ExpiredStockQuarantineReason,
    ExpiredStockQuarantineRecord,
    ExpiredStockQuarantineStatus,
    StockMovementType,
    Tenant,
)
from apps.core.stock_ledger_service import StockLedgerService

logger = logging.getLogger(__name__)


class ExpiredStockQuarantineService:
    """
    Authoritative domain service governing expired and defective batch quarantine,
    sale prohibition enforcement, and audited disposal/return lifecycles.
    """

    _ALLOWED_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "PLATFORM_SUPER_ADMIN",
        "STAFF_PHARMACIST",
        "PHARMACIST",
        "INVENTORY_CLERK",
        "TENANT_OWNER",
    }

    _ACTION_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "PLATFORM_SUPER_ADMIN",
        "TENANT_OWNER",
    }

    @classmethod
    def assert_quarantine_authority(cls, actor_user: Any) -> None:
        """Enforces that Cashiers are strictly barred from quarantine management."""
        if not actor_user:
            return
        role = getattr(actor_user, "role", None)
        role_str = str(role).upper() if role else ""
        if role_str == "CASHIER" or (role_str and role_str not in cls._ALLOWED_ROLES):
            raise PermissionDenied(
                "Cashiers are not authorized to quarantine inventory (ERR_STAFF_ROLE_DENIED)."
            )

    @classmethod
    def assert_action_authority(cls, actor_user: Any) -> None:
        """Enforces manager/owner privilege for disposal or return approvals."""
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied("Authentication required.")
        role = getattr(actor_user, "role", None)
        role_str = str(role).upper() if role else ""
        if role_str not in cls._ACTION_ROLES:
            raise PermissionDenied(
                f"Role '{role_str}' is not authorized to approve disposal or return (ERR_ACTION_ROLE_DENIED)."
            )

    @classmethod
    def assert_sale_allowed(cls, batch: BatchStock) -> None:
        """
        Guards point-of-sale and dispensing against selling expired or quarantined stock.
        """
        if getattr(batch, "is_quarantined", False) or getattr(batch, "status", "") == BatchStatus.QUARANTINED:
            raise ValidationError(
                {"batch": f"Sale prohibited: Batch '{batch.batch_number}' is quarantined (ERR_EXPIRED_STOCK_SALE_PROHIBITED)."}
            )
        if getattr(batch, "is_expired", False) or (batch.expiry_date and batch.expiry_date <= timezone.now().date()):
            raise ValidationError(
                {"batch": f"Sale prohibited: Batch '{batch.batch_number}' has expired on {batch.expiry_date} (ERR_EXPIRED_STOCK_SALE_PROHIBITED)."}
            )

    @classmethod
    @transaction.atomic
    def quarantine_batch(
        cls,
        tenant_id: str,
        branch_id: str,
        batch_id: str,
        reason: str = ExpiredStockQuarantineReason.BATCH_EXPIRED,
        notes: str = "",
        quarantine_location: str = "QUARANTINE-ZONE",
        actor_user: Any = None,
    ) -> ExpiredStockQuarantineRecord:
        """
        Quarantines a batch, moves it to the quarantine location, and prohibits sales.
        """
        cls.assert_quarantine_authority(actor_user)

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound(f"Tenant '{tenant_id}' not found.")

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
        if not branch:
            raise NotFound(f"Branch '{branch_id}' not found.")

        batch = (
            BatchStock.objects.select_for_update()
            .filter(tenant=tenant, branch=branch, batch_id=batch_id)
            .select_related("product")
            .first()
        )
        if not batch:
            raise NotFound(f"Batch '{batch_id}' not found.")

        if batch.is_quarantined and batch.status == BatchStatus.QUARANTINED:
            raise ValidationError(
                {"batch": f"Batch '{batch.batch_number}' is already in quarantine (ERR_ALREADY_QUARANTINED)."}
            )

        if reason not in ExpiredStockQuarantineReason.values:
            raise ValidationError({"reason": f"Invalid quarantine reason code '{reason}'."})

        # Update batch status
        batch.is_quarantined = True
        batch.status = BatchStatus.QUARANTINED
        batch.storage_location = quarantine_location.strip() or "QUARANTINE-ZONE"
        batch.save(update_fields=["is_quarantined", "status", "storage_location", "updated_at"])

        cost = batch.cost_per_unit or Decimal("0.00")
        total_impact = (Decimal(str(batch.quantity_base_units)) * cost).quantize(Decimal("0.0001"))

        record = ExpiredStockQuarantineRecord.objects.create(
            quarantine_id=f"quar-{uuid.uuid4().hex[:10]}",
            tenant=tenant,
            branch=branch,
            batch=batch,
            product=batch.product,
            batch_number=batch.batch_number,
            quarantined_quantity=batch.quantity_base_units,
            unit_cost_bdt=cost,
            total_cost_impact=total_impact,
            quarantine_reason=reason,
            status=ExpiredStockQuarantineStatus.QUARANTINED,
            quarantine_notes=notes.strip(),
            quarantine_location=batch.storage_location,
            quarantined_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            ledger_entry_id="",
        )

        logger.info(
            "Batch %s quarantined in branch %s under record %s",
            batch.batch_number,
            branch.branch_id,
            record.quarantine_id,
        )
        return record

    @classmethod
    @transaction.atomic
    def sweep_and_quarantine_expired_batches(
        cls,
        tenant_id: str,
        branch_id: str,
        actor_user: Any = None,
    ) -> List[ExpiredStockQuarantineRecord]:
        """
        Automated scan: detects all expired active batches in the branch and quarantines them.
        """
        cls.assert_quarantine_authority(actor_user)
        today = timezone.now().date()

        batches = (
            BatchStock.objects.select_for_update()
            .filter(
                tenant_id=tenant_id,
                branch_id=branch_id,
                expiry_date__lte=today,
                is_quarantined=False,
                quantity_base_units__gt=0,
            )
            .exclude(status=BatchStatus.QUARANTINED)
            .select_related("product")
        )

        results = []
        for batch in batches:
            record = cls.quarantine_batch(
                tenant_id=tenant_id,
                branch_id=branch_id,
                batch_id=batch.batch_id,
                reason=ExpiredStockQuarantineReason.BATCH_EXPIRED,
                notes="Automated expiry sweep quarantine",
                quarantine_location="QUARANTINE-ZONE",
                actor_user=actor_user,
            )
            results.append(record)
        return results

    @classmethod
    @transaction.atomic
    def approve_disposal_or_return(
        cls,
        quarantine_id: str,
        tenant_id: str,
        action_type: str,
        disposal_certificate_ref: str = "",
        action_notes: str = "",
        actor_user: Any = None,
    ) -> ExpiredStockQuarantineRecord:
        """
        Manager/Owner approves final disposal, vendor return, or destruction.
        Reduces physical inventory to 0 on batch and posts StockMovementType.STOCK_ADJUSTMENT_OUT.
        """
        cls.assert_action_authority(actor_user)

        valid_actions = {
            ExpiredStockQuarantineStatus.DISPOSED,
            ExpiredStockQuarantineStatus.RETURNED_TO_SUPPLIER,
            ExpiredStockQuarantineStatus.DESTROYED,
        }
        if action_type not in valid_actions:
            raise ValidationError({"action_type": f"Invalid disposal action '{action_type}'."})

        record = (
            ExpiredStockQuarantineRecord.objects.select_for_update()
            .filter(tenant_id=tenant_id, quarantine_id=quarantine_id)
            .select_related("batch", "product", "tenant", "branch")
            .first()
        )
        if not record:
            raise NotFound(f"Quarantine record '{quarantine_id}' not found.")

        if record.status != ExpiredStockQuarantineStatus.QUARANTINED:
            raise ValidationError(
                {"status": f"Cannot action quarantine record in status '{record.status}' (must be QUARANTINED)."}
            )

        batch = (
            BatchStock.objects.select_for_update()
            .filter(tenant_id=tenant_id, batch_id=record.batch_id)
            .first()
        )
        if not batch:
            raise NotFound(f"Batch '{record.batch_id}' not found.")

        qty_to_remove = record.quarantined_quantity
        if batch.quantity_base_units < qty_to_remove:
            qty_to_remove = batch.quantity_base_units

        # Post removal to Stock Movement Ledger
        if qty_to_remove > 0:
            ledger_entry = StockLedgerService.record_stock_movement(
                tenant=record.tenant,
                branch=record.branch,
                product=record.product,
                movement_type=StockMovementType.STOCK_ADJUSTMENT_OUT,
                quantity_delta=-qty_to_remove,
                source_entity_type="EXPIRED_STOCK_DISPOSAL",
                source_entity_id=record.quarantine_id,
                source_reference=disposal_certificate_ref.strip() or f"DISP-{record.quarantine_id}",
                batch=batch,
                batch_number=batch.batch_number,
                unit_cost=record.unit_cost_bdt,
                storage_location=batch.storage_location,
                notes=f"Disposal action: {action_type}. {action_notes}".strip(),
                posted_by=actor_user,
            )
            batch.quantity_base_units -= qty_to_remove
            record.ledger_entry_id = ledger_entry.entry_id

        batch.status = BatchStatus.DEPLETED if batch.quantity_base_units == 0 else BatchStatus.QUARANTINED
        batch.save(update_fields=["quantity_base_units", "status", "updated_at"])

        record.status = action_type
        record.actioned_by = actor_user
        record.actioned_at = timezone.now()
        record.action_notes = action_notes.strip()
        record.disposal_certificate_ref = disposal_certificate_ref.strip()
        record.save()

        logger.info(
            "Quarantine record %s actioned to %s by %s",
            record.quarantine_id,
            action_type,
            actor_user,
        )
        return record

    @classmethod
    @transaction.atomic
    def release_quarantine(
        cls,
        quarantine_id: str,
        tenant_id: str,
        release_reason: str,
        new_storage_location: str,
        actor_user: Any = None,
    ) -> ExpiredStockQuarantineRecord:
        """
        Releases non-expired batch back to active status (e.g. cleared false alarm or lab test passed).
        Expired batches CANNOT be released back into saleable inventory.
        """
        cls.assert_action_authority(actor_user)

        record = (
            ExpiredStockQuarantineRecord.objects.select_for_update()
            .filter(tenant_id=tenant_id, quarantine_id=quarantine_id)
            .select_related("batch", "product", "tenant", "branch")
            .first()
        )
        if not record:
            raise NotFound(f"Quarantine record '{quarantine_id}' not found.")

        if record.status != ExpiredStockQuarantineStatus.QUARANTINED:
            raise ValidationError({"status": f"Cannot release record in status '{record.status}'."})

        batch = record.batch
        if batch.expiry_date <= timezone.now().date():
            raise ValidationError(
                {"batch": "Cannot release expired batch to active inventory (ERR_EXPIRED_BATCH_CANNOT_BE_RELEASED)."}
            )

        clean_reason = release_reason.strip()
        if len(clean_reason) < 10:
            raise ValidationError(
                {"release_reason": "Release reason must be at least 10 characters documenting clearing justification."}
            )

        batch.is_quarantined = False
        batch.status = BatchStatus.ACTIVE
        batch.storage_location = new_storage_location.strip() or "SHELF-ACTIVE"
        batch.save(update_fields=["is_quarantined", "status", "storage_location", "updated_at"])

        record.status = ExpiredStockQuarantineStatus.RELEASED
        record.actioned_by = actor_user
        record.actioned_at = timezone.now()
        record.action_notes = clean_reason
        record.save()

        return record
