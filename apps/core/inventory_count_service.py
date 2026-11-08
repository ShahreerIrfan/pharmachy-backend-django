"""
backend/apps/core/inventory_count_service.py
Architecture Decision: ADR-0204 (P204).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Domain Business Service for Physical Count Sessions:
- Barcode/batch scanner collection.
- Discrepancy calculations (system vs physical count).
- Strict Maker-Checker separation of duties (Checker != Maker).
- Immutable Stock Movement Ledger posting (P201 integration).
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
    BatchStock,
    Branch,
    InventoryCountDiscrepancyType,
    InventoryCountItem,
    InventoryCountSession,
    InventoryCountSessionStatus,
    InventoryCountSessionType,
    MedicineProduct,
    StockMovementType,
    StorageLocation,
    Tenant,
    Warehouse,
)
from apps.core.stock_ledger_service import StockLedgerService

logger = logging.getLogger(__name__)


class InventoryCountService:
    """
    Business service executing inventory count sessions, scanner reconciliation,
    maker-checker review workflows, and double-entry stock ledger adjustment posting.
    """

    _MAKER_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "PLATFORM_SUPER_ADMIN",
        "STAFF_PHARMACIST",
        "PHARMACIST",
        "INVENTORY_CLERK",
        "TENANT_OWNER",
    }

    _CHECKER_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "PLATFORM_SUPER_ADMIN",
        "TENANT_OWNER",
    }

    @classmethod
    def assert_maker_role(cls, actor_user: Any) -> None:
        """Enforces that only qualified pharmacy inventory staff can conduct counts."""
        if not actor_user:
            return
        role = getattr(actor_user, "role", None)
        role_str = str(role).upper() if role else ""
        if role_str == "CASHIER" or (role_str and role_str not in cls._MAKER_ROLES):
            raise PermissionDenied(
                "Cashiers are not authorized to create or execute inventory count sessions (ERR_STAFF_ROLE_DENIED)."
            )

    @classmethod
    def assert_checker_role(cls, checker_user: Any) -> None:
        """Enforces that only managers, owners, or superadmins can approve counts."""
        if not checker_user or not getattr(checker_user, "is_authenticated", False):
            raise PermissionDenied("Checker must be an authenticated manager or owner.")
        role = getattr(checker_user, "role", None)
        role_str = str(role).upper() if role else ""
        if role_str not in cls._CHECKER_ROLES:
            raise PermissionDenied(
                f"Role '{role_str}' is not authorized to act as inventory checker. Only Managers or Owners permitted (ERR_CHECKER_ROLE_DENIED)."
            )

    @classmethod
    @transaction.atomic
    def create_session(
        cls,
        tenant_id: str,
        branch_id: str,
        session_type: str = InventoryCountSessionType.CYCLE_COUNT,
        warehouse_id: Optional[str] = None,
        location_id: Optional[str] = None,
        notes: str = "",
        actor_user: Any = None,
    ) -> InventoryCountSession:
        """Initializes a formal physical count session."""
        cls.assert_maker_role(actor_user)

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound(f"Tenant '{tenant_id}' not found.")

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
        if not branch:
            raise NotFound(f"Branch '{branch_id}' not found under tenant '{tenant_id}'.")

        wh = None
        if warehouse_id:
            wh = Warehouse.objects.filter(tenant=tenant, branch=branch, warehouse_id=warehouse_id).first()
            if not wh:
                raise NotFound(f"Warehouse '{warehouse_id}' not found in this branch.")

        loc = None
        if location_id:
            loc = StorageLocation.objects.filter(tenant=tenant, branch=branch, location_id=location_id).first()
            if not loc:
                raise NotFound(f"Storage location '{location_id}' not found in this branch.")

        now = timezone.now()
        prefix = f"CNT-{now.strftime('%Y%m')}"
        seq = InventoryCountSession.objects.filter(tenant=tenant, branch=branch, session_number__startswith=prefix).count() + 1
        session_number = f"{prefix}-{seq:04d}"

        session = InventoryCountSession.objects.create(
            session_id=f"cnt-{uuid.uuid4().hex[:10]}",
            tenant=tenant,
            branch=branch,
            session_number=session_number,
            session_type=session_type,
            status=InventoryCountSessionStatus.IN_PROGRESS,
            warehouse=wh,
            location=loc,
            notes=notes.strip(),
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            counted_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )
        return session

    @classmethod
    @transaction.atomic
    def record_scan_item(
        cls,
        session_id: str,
        tenant_id: str,
        branch_id: str,
        batch_number: str,
        counted_quantity: int,
        product_id: Optional[str] = None,
        scanner_barcode: str = "",
        storage_location: str = "",
        notes: str = "",
        actor_user: Any = None,
    ) -> InventoryCountItem:
        """
        Records physical counted units from scanner or manual entry and computes discrepancies.
        """
        cls.assert_maker_role(actor_user)

        if counted_quantity < 0:
            raise ValidationError({"counted_quantity": "Counted quantity cannot be negative."})

        session = (
            InventoryCountSession.objects.select_for_update()
            .filter(tenant_id=tenant_id, branch_id=branch_id, session_id=session_id)
            .first()
        )
        if not session:
            raise NotFound(f"Count session '{session_id}' not found in this branch.")

        if session.status not in [
            InventoryCountSessionStatus.DRAFT,
            InventoryCountSessionStatus.IN_PROGRESS,
        ]:
            raise ValidationError(
                {"session": f"Cannot add items to session in '{session.status}' status (must be DRAFT or IN_PROGRESS)."}
            )

        # Locate batch in branch
        batch_qs = BatchStock.objects.filter(
            tenant_id=tenant_id, branch_id=branch_id, batch_number__iexact=batch_number.strip()
        )
        if product_id:
            batch_qs = batch_qs.filter(product_id=product_id)

        batch = batch_qs.select_related("product").first()
        if not batch:
            # Check by barcode
            if scanner_barcode:
                prod = MedicineProduct.objects.filter(barcode=scanner_barcode.strip()).first()
                if prod:
                    batch = BatchStock.objects.filter(
                        tenant_id=tenant_id, branch_id=branch_id, product=prod
                    ).first()

        if not batch:
            raise NotFound(
                f"Batch '{batch_number}' not found in branch inventory. Unregistered batch cannot be adjusted directly."
            )

        product = batch.product
        system_quantity = batch.quantity_base_units
        unit_cost = batch.cost_per_unit
        loc_code = storage_location.strip() or batch.storage_location or "DEFAULT"

        # Update or create count item
        item, created = InventoryCountItem.objects.get_or_create(
            session=session,
            tenant_id=tenant_id,
            branch_id=branch_id,
            product=product,
            batch=batch,
            batch_number=batch.batch_number,
            storage_location=loc_code,
            defaults={
                "system_quantity": system_quantity,
                "counted_quantity": counted_quantity,
                "unit_cost": unit_cost,
                "scanner_barcode": scanner_barcode,
                "scanned_by": actor_user if getattr(actor_user, "is_authenticated", False) else None,
                "notes": notes,
            },
        )
        if not created:
            item.counted_quantity = counted_quantity
            item.system_quantity = system_quantity
            item.unit_cost = unit_cost
            item.notes = notes
            item.save()

        cls._recalculate_session_totals(session)
        return item

    @classmethod
    def _recalculate_session_totals(cls, session: InventoryCountSession) -> None:
        """Aggregates item discrepancies into session headers."""
        items = list(session.items.all())
        session.total_items_counted = len(items)
        session.total_discrepancy_units = sum(i.discrepancy_quantity for i in items)
        session.total_cost_discrepancy_bdt = sum(i.cost_discrepancy_bdt for i in items)
        session.surplus_count = sum(1 for i in items if i.discrepancy_type == InventoryCountDiscrepancyType.SURPLUS)
        session.shortage_count = sum(1 for i in items if i.discrepancy_type == InventoryCountDiscrepancyType.SHORTAGE)
        session.matched_count = sum(1 for i in items if i.discrepancy_type == InventoryCountDiscrepancyType.MATCH)
        session.save(
            update_fields=[
                "total_items_counted",
                "total_discrepancy_units",
                "total_cost_discrepancy_bdt",
                "surplus_count",
                "shortage_count",
                "matched_count",
                "updated_at",
            ]
        )

    @classmethod
    @transaction.atomic
    def submit_for_review(
        cls,
        session_id: str,
        tenant_id: str,
        actor_user: Any = None,
        notes: str = "",
    ) -> InventoryCountSession:
        """Maker submits completed count sheet to Checker for approval."""
        cls.assert_maker_role(actor_user)

        session = (
            InventoryCountSession.objects.select_for_update()
            .filter(tenant_id=tenant_id, session_id=session_id)
            .first()
        )
        if not session:
            raise NotFound(f"Count session '{session_id}' not found.")

        if session.status != InventoryCountSessionStatus.IN_PROGRESS:
            raise ValidationError(
                {"status": f"Cannot submit session in '{session.status}' status (must be IN_PROGRESS)."}
            )

        if session.items.count() == 0:
            raise ValidationError({"items": "Cannot submit empty count session with zero scanned items."})

        session.status = InventoryCountSessionStatus.SUBMITTED_FOR_REVIEW
        session.submitted_at = timezone.now()
        if actor_user and getattr(actor_user, "is_authenticated", False):
            session.counted_by = actor_user
        if notes:
            session.notes = f"{session.notes}; Submission: {notes}".strip("; ")
        session.save()
        return session

    @classmethod
    @transaction.atomic
    def review_session(
        cls,
        session_id: str,
        tenant_id: str,
        is_approved: bool,
        checker_user: Any,
        checker_notes: str = "",
    ) -> InventoryCountSession:
        """
        Checker approves or rejects count sheet.
        Strictly enforces Maker-Checker anti-self-approval rule.
        """
        cls.assert_checker_role(checker_user)

        session = (
            InventoryCountSession.objects.select_for_update()
            .filter(tenant_id=tenant_id, session_id=session_id)
            .first()
        )
        if not session:
            raise NotFound(f"Count session '{session_id}' not found.")

        if session.status != InventoryCountSessionStatus.SUBMITTED_FOR_REVIEW:
            raise ValidationError(
                {"status": f"Session cannot be reviewed from status '{session.status}' (must be SUBMITTED_FOR_REVIEW)."}
            )

        # Enforce Maker-Checker Separation of Duties
        checker_pk = getattr(checker_user, "pk", None)
        maker_pk = getattr(session.counted_by, "pk", None) or getattr(session.created_by, "pk", None)

        if checker_pk and maker_pk and checker_pk == maker_pk:
            raise ValidationError(
                {"checker": "Maker-checker separation violated: counter cannot approve their own inventory count session (ERR_MAKER_CHECKER_SELF_APPROVAL)."}
            )

        session.reviewed_by = checker_user
        session.reviewed_at = timezone.now()
        session.checker_notes = checker_notes.strip()

        if is_approved:
            session.status = InventoryCountSessionStatus.APPROVED
        else:
            session.status = InventoryCountSessionStatus.REJECTED

        session.save(update_fields=["status", "reviewed_by", "reviewed_at", "checker_notes", "updated_at"])
        return session

    @classmethod
    @transaction.atomic
    def post_adjustments(
        cls,
        session_id: str,
        tenant_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Posts audited stock adjustments for discrepancy items to StockLedgerEntry.
        Updates BatchStock and recalculates branch availability.
        """
        session = (
            InventoryCountSession.objects.select_for_update()
            .filter(tenant_id=tenant_id, session_id=session_id)
            .first()
        )
        if not session:
            raise NotFound(f"Count session '{session_id}' not found.")

        if session.status == InventoryCountSessionStatus.POSTED:
            raise ValidationError({"status": "Session has already been posted to inventory ledger (ERR_ALREADY_POSTED)."})

        if session.status != InventoryCountSessionStatus.APPROVED:
            raise ValidationError(
                {"status": f"Count session must be in APPROVED status before posting (current: '{session.status}')."}
            )

        items = list(session.items.select_related("product", "batch").all())
        adjustments_posted = 0

        for item in items:
            delta = item.discrepancy_quantity
            if delta == 0:
                continue

            movement_type = (
                StockMovementType.STOCK_ADJUSTMENT_IN
                if delta > 0
                else StockMovementType.STOCK_ADJUSTMENT_OUT
            )

            # Record in Stock Movement Ledger (ADR-0201)
            ledger_entry = StockLedgerService.record_stock_movement(
                tenant=session.tenant,
                branch=session.branch,
                product=item.product,
                movement_type=movement_type,
                quantity_delta=delta,
                source_entity_type="INVENTORY_COUNT_SESSION",
                source_entity_id=session.session_id,
                source_reference=session.session_number,
                batch=item.batch,
                batch_number=item.batch_number,
                unit_cost=item.unit_cost,
                storage_location=item.storage_location,
                notes=f"Count adjustment for {session.session_number}: delta {delta}",
                posted_by=actor_user,
            )

            item.adjustment_ledger_entry_id = ledger_entry.entry_id
            item.save(update_fields=["adjustment_ledger_entry_id"])
            adjustments_posted += 1

        session.status = InventoryCountSessionStatus.POSTED
        session.posted_at = timezone.now()
        session.save(update_fields=["status", "posted_at", "updated_at"])

        return {
            "session_id": session.session_id,
            "session_number": session.session_number,
            "status": session.status,
            "adjustments_posted_count": adjustments_posted,
            "total_discrepancy_units": session.total_discrepancy_units,
            "total_cost_discrepancy_bdt": str(session.total_cost_discrepancy_bdt),
        }

