"""
backend/apps/core/stock_segregation_service.py
Architecture Decision: ADR-0207 (P207).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Domain Business Service for Damaged, Suspicious, Recalled, and Returned Stock Segregation:
- Mandatory physical and logical separation from sellable stock.
- Instant deduction from available sellable inventory.
- Documented release rules with strict QA pharmacist inspection gates.
- Escalation to formal DGDA quarantine, vendor return, or disposal write-offs.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    SegregationCategory,
    SegregationStatus,
    StockMovementType,
    StockSegregationRecord,
    Tenant,
)
from apps.core.stock_ledger_service import StockLedgerService

logger = logging.getLogger(__name__)


class StockSegregationService:
    """
    Core domain service managing quarantine-adjacent segregation of damaged, suspicious,
    recalled, or unverified customer returns away from live point-of-sale inventory.
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

    _QA_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "PLATFORM_SUPER_ADMIN",
        "STAFF_PHARMACIST",
        "PHARMACIST",
        "TENANT_OWNER",
    }

    @classmethod
    def assert_staff_authority(cls, actor_user: Any) -> None:
        """Enforces that Cashiers cannot initiate or manage segregated stock."""
        if not actor_user:
            return
        role = getattr(actor_user, "role", None)
        role_str = str(role).upper() if role else ""
        if role_str == "CASHIER" or (role_str and role_str not in cls._STAFF_ROLES):
            raise PermissionDenied(
                "Cashiers are not authorized to segregate inventory (ERR_STAFF_ROLE_DENIED)."
            )

    @classmethod
    def assert_qa_authority(cls, actor_user: Any) -> None:
        """Enforces pharmacist or manager qualification for release sign-off."""
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied("Authentication required.")
        role = getattr(actor_user, "role", None)
        role_str = str(role).upper() if role else ""
        if role_str not in cls._QA_ROLES:
            raise PermissionDenied(
                f"Role '{role_str}' lacks QA authority to release segregated stock (ERR_QA_ROLE_REQUIRED)."
            )

    @classmethod
    @transaction.atomic
    def segregate_stock(
        cls,
        tenant_id: str,
        branch_id: str,
        batch_id: str,
        quantity: int,
        category: str,
        reason_notes: str,
        segregation_location: str = "SEGREGATION-HOLD",
        actor_user: Any = None,
    ) -> StockSegregationRecord:
        """
        Deducts units from live sellable availability and holds them in a designated physical area.
        """
        cls.assert_staff_authority(actor_user)

        if quantity <= 0:
            raise ValidationError({"quantity": "Segregated quantity must be greater than zero."})

        if category not in SegregationCategory.values:
            raise ValidationError({"category": f"Invalid segregation category '{category}'."})

        clean_notes = reason_notes.strip()
        if len(clean_notes) < 5:
            raise ValidationError({"reason_notes": "Documented reason must be provided."})

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

        # Ensure batch has sufficient unreserved units
        available_units = batch.quantity_base_units - batch.reserved_quantity_base_units
        if quantity > available_units:
            raise ValidationError(
                {"quantity": f"Cannot segregate {quantity} units. Only {available_units} unreserved sellable units available."}
            )

        # Deduct sellable stock by isolating into reserved pool
        batch.reserved_quantity_base_units += quantity
        batch.save(update_fields=["reserved_quantity_base_units", "updated_at"])

        cost = batch.cost_per_unit or Decimal("0.00")
        total_impact = (Decimal(str(quantity)) * cost).quantize(Decimal("0.0001"))

        record = StockSegregationRecord.objects.create(
            segregation_id=f"seg-{uuid.uuid4().hex[:10]}",
            tenant=tenant,
            branch=branch,
            batch=batch,
            product=batch.product,
            batch_number=batch.batch_number,
            segregated_quantity=quantity,
            unit_cost_bdt=cost,
            total_cost_impact=total_impact,
            category=category,
            status=SegregationStatus.SEGREGATED,
            segregation_location=segregation_location.strip() or "SEGREGATION-HOLD",
            reason_notes=clean_notes,
            segregated_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        logger.info(
            "Segregated %s units of %s under record %s in %s",
            quantity,
            batch.batch_number,
            record.segregation_id,
            branch.branch_id,
        )
        return record

    @classmethod
    @transaction.atomic
    def release_to_sellable(
        cls,
        segregation_id: str,
        tenant_id: str,
        justification: str,
        inspector_user: Any,
        new_storage_location: str = "SHELF-ACTIVE",
    ) -> StockSegregationRecord:
        """
        Documented release rule: Pharmacist QA clears stock back to sellable inventory.
        Strictly prohibits release of recalled, counterfeit, or expired items.
        """
        cls.assert_qa_authority(inspector_user)

        record = (
            StockSegregationRecord.objects.select_for_update()
            .filter(tenant_id=tenant_id, segregation_id=segregation_id)
            .select_related("batch", "product", "tenant", "branch")
            .first()
        )
        if not record:
            raise NotFound(f"Segregation record '{segregation_id}' not found.")

        if record.status != SegregationStatus.SEGREGATED:
            raise ValidationError(
                {"status": f"Cannot release record in status '{record.status}' (must be SEGREGATED)."}
            )

        # Prohibit release if recalled or suspected counterfeit
        if record.category in (SegregationCategory.RECALLED, SegregationCategory.SUSPICIOUS_COUNTERFEIT):
            raise ValidationError(
                {"category": f"Prohibited release: {record.category} items cannot be released to sellable inventory (ERR_PROHIBITED_RELEASE)."}
            )

        batch = record.batch
        if batch.expiry_date <= timezone.now().date():
            raise ValidationError(
                {"batch": "Cannot release expired stock to sellable inventory (ERR_EXPIRED_BATCH_CANNOT_BE_RELEASED)."}
            )

        clean_justification = justification.strip()
        if len(clean_justification) < 15:
            raise ValidationError(
                {"justification": "Documented release justification must be at least 15 characters confirming seal and quality integrity (ERR_RELEASE_JUSTIFICATION_REQUIRED)."}
            )

        # Restore sellable availability
        if batch.reserved_quantity_base_units >= record.segregated_quantity:
            batch.reserved_quantity_base_units -= record.segregated_quantity
        else:
            batch.reserved_quantity_base_units = 0
        batch.storage_location = new_storage_location.strip() or batch.storage_location
        batch.save(update_fields=["reserved_quantity_base_units", "storage_location", "updated_at"])

        record.status = SegregationStatus.RELEASED_TO_SELLABLE
        record.inspected_by = inspector_user
        record.inspected_at = timezone.now()
        record.release_justification = clean_justification
        record.actioned_by = inspector_user
        record.actioned_at = timezone.now()
        record.save()

        logger.info(
            "Released segregation %s back to sellable by %s",
            record.segregation_id,
            inspector_user,
        )
        return record

    @classmethod
    @transaction.atomic
    def dispose_or_return_stock(
        cls,
        segregation_id: str,
        tenant_id: str,
        action_type: str,
        disposition_reference: str,
        action_notes: str,
        actor_user: Any,
    ) -> StockSegregationRecord:
        """
        Final disposition: supplier return debit note or physical write-off and disposal.
        Emits StockMovementType.STOCK_ADJUSTMENT_OUT.
        """
        cls.assert_qa_authority(actor_user)

        valid_actions = {
            SegregationStatus.RETURNED_TO_SUPPLIER,
            SegregationStatus.DISPOSED_WRITTEN_OFF,
        }
        if action_type not in valid_actions:
            raise ValidationError({"action_type": f"Invalid disposition action '{action_type}'."})

        record = (
            StockSegregationRecord.objects.select_for_update()
            .filter(tenant_id=tenant_id, segregation_id=segregation_id)
            .select_related("batch", "product", "tenant", "branch")
            .first()
        )
        if not record:
            raise NotFound(f"Segregation record '{segregation_id}' not found.")

        if record.status != SegregationStatus.SEGREGATED:
            raise ValidationError(
                {"status": f"Cannot action record in status '{record.status}' (must be SEGREGATED)."}
            )

        batch = record.batch
        qty = record.segregated_quantity

        # Release reservation; StockLedgerService will atomically deduct physical units
        if batch.reserved_quantity_base_units >= record.segregated_quantity:
            batch.reserved_quantity_base_units -= record.segregated_quantity
        else:
            batch.reserved_quantity_base_units = 0
        batch.save(update_fields=["reserved_quantity_base_units", "updated_at"])

        # Post Stock Movement Ledger entry
        ledger_entry = StockLedgerService.record_stock_movement(
            tenant=record.tenant,
            branch=record.branch,
            product=record.product,
            movement_type=StockMovementType.STOCK_ADJUSTMENT_OUT,
            quantity_delta=-qty,
            source_entity_type="STOCK_SEGREGATION_DISPOSAL",
            source_entity_id=record.segregation_id,
            source_reference=disposition_reference.strip() or f"DISP-{record.segregation_id}",
            batch=batch,
            batch_number=batch.batch_number,
            unit_cost=record.unit_cost_bdt,
            storage_location=record.segregation_location,
            notes=f"Segregation disposition: {action_type}. {action_notes}".strip(),
            posted_by=actor_user,
        )

        record.status = action_type
        record.actioned_by = actor_user
        record.actioned_at = timezone.now()
        record.disposition_reference = disposition_reference.strip()
        record.inspection_notes = action_notes.strip()
        record.ledger_entry_id = ledger_entry.entry_id
        record.save()

        return record
