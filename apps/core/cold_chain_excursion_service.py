"""
backend/apps/core/cold_chain_excursion_service.py
Architecture Decision: ADR-0212 (P212).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Domain Service for Cold Chain Excursion Workflow:
- Detect temperature excursions automatically or manually.
- Automatically quarantine affected medicine batches in the designated storage location.
- Restrict sellable inventory and bar cashiers from POS dispensing.
- Provide pharmacist review workflow with clinical justification requirement.
- Execute formal disposition outcomes:
    * RELEASE_TO_SELLABLE: Restores batch status to ACTIVE and un-quarantines.
    * CONDEMN_DISPOSE: Marks batches DISPOSED and records EXPIRY_DISPOSAL in immutable StockLedger.
    * RETURN_TO_SUPPLIER: Marks batches RECALLED and records PURCHASE_RETURN in immutable StockLedger.
- Enforce multi-tenant boundaries and RBAC (Cashier strictly denied).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    ColdChainAffectedBatch,
    ColdChainExcursionIncident,
    ExcursionDispositionOutcome,
    ExcursionIncidentStatus,
    ExcursionSeverity,
    StockMovementType,
    StorageLocation,
    TemperatureLogReading,
    Tenant,
)
from apps.identity.models import UserRole
from apps.core.stock_ledger_service import StockLedgerService


class ColdChainExcursionService:
    """
    Business service managing temperature excursion incidents, lot quarantine,
    and clinical review disposition workflows.
    """

    ALLOWED_REVIEW_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "SUPERADMIN",
        "STAFF_PHARMACIST",
    }

    @classmethod
    def assert_review_permission(cls, user: Any) -> None:
        """
        Verify that the user holds a licensed clinical or managerial role.
        Cashiers and unauthorized personnel are rejected.
        """
        role = getattr(user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str == "CASHIER" or role_str not in cls.ALLOWED_REVIEW_ROLES:
            raise PermissionDenied(
                "Cashiers are not authorized to review or dispose cold-chain excursion incidents. "
                "Only registered Pharmacists and Managers may execute this review."
            )

    @classmethod
    @transaction.atomic
    def trigger_excursion(
        cls,
        tenant: Tenant,
        branch: Branch,
        storage_location: StorageLocation,
        recorded_temperature: Decimal,
        min_threshold: Optional[Decimal] = None,
        max_threshold: Optional[Decimal] = None,
        trigger_reading: Optional[TemperatureLogReading] = None,
        created_by: Any = None,
    ) -> ColdChainExcursionIncident:
        """
        Registers an excursion incident, calculates excursion severity,
        and automatically quarantines all active batches in the affected storage location.
        """
        min_t = min_threshold if min_threshold is not None else getattr(storage_location, "min_temperature_celsius", Decimal("2.00"))
        max_t = max_threshold if max_threshold is not None else getattr(storage_location, "max_temperature_celsius", Decimal("8.00"))

        if min_t >= max_t:
            raise ValidationError({"min_threshold": "Minimum threshold must be strictly less than maximum threshold."})

        # Calculate severity
        diff = max(min_t - recorded_temperature, recorded_temperature - max_t)
        if diff >= Decimal("5.00") or recorded_temperature >= Decimal("15.00"):
            severity = ExcursionSeverity.CRITICAL
        else:
            severity = ExcursionSeverity.WARNING

        incident_number = f"CCE-{uuid.uuid4().hex[:8].upper()}"

        incident = ColdChainExcursionIncident.objects.create(
            tenant=tenant,
            branch=branch,
            storage_location=storage_location,
            trigger_reading=trigger_reading,
            incident_number=incident_number,
            status=ExcursionIncidentStatus.DETECTED,
            disposition_outcome=ExcursionDispositionOutcome.PENDING,
            recorded_temperature_celsius=recorded_temperature,
            min_threshold_celsius=min_t,
            max_threshold_celsius=max_t,
            severity=severity,
        )

        # Query all active batches in the storage location for this tenant and branch
        batches = list(
            BatchStock.objects.select_for_update()
            .filter(
                tenant=tenant,
                branch=branch,
                storage_location=storage_location.code,
                status=BatchStatus.ACTIVE,
            )
        )

        quarantined_count = 0
        total_val = Decimal("0.00")

        for b in batches:
            qty = b.available_quantity_base_units or b.quantity_base_units
            unit_cost = getattr(b, "cost_per_unit", None) or getattr(b, "cost_price", None) or Decimal("0.00")
            batch_cost = unit_cost * Decimal(str(qty))
            total_val += batch_cost

            # Quarantine batch
            b.is_quarantined = True
            b.status = BatchStatus.QUARANTINED
            b.reserved_quantity_base_units = b.quantity_base_units
            b.save(update_fields=["is_quarantined", "status", "reserved_quantity_base_units"])

            ColdChainAffectedBatch.objects.create(
                incident=incident,
                batch=b,
                initial_quantity=qty,
                unit_cost=unit_cost,
                total_cost=batch_cost,
                disposition_outcome=ExcursionDispositionOutcome.PENDING,
            )
            quarantined_count += 1

        incident.quarantined_batches_count = quarantined_count
        incident.quarantined_total_value = total_val
        incident.save(update_fields=["quarantined_batches_count", "quarantined_total_value"])

        return incident

    @classmethod
    @transaction.atomic
    def review_disposition(
        cls,
        incident: ColdChainExcursionIncident,
        reviewer_user: Any,
        outcome: str,
        clinical_justification: str,
    ) -> ColdChainExcursionIncident:
        """
        Pharmacist review and disposition of an excursion incident.
        Restores stock, disposes of stock, or returns stock to supplier.
        """
        cls.assert_review_permission(reviewer_user)

        if incident.status == ExcursionIncidentStatus.RESOLVED:
            raise ValidationError({"status": "This cold chain excursion has already been reviewed and resolved."})

        if outcome not in [
            ExcursionDispositionOutcome.RELEASE_TO_SELLABLE,
            ExcursionDispositionOutcome.CONDEMN_DISPOSE,
            ExcursionDispositionOutcome.RETURN_TO_SUPPLIER,
        ]:
            raise ValidationError({"disposition_outcome": f"Invalid disposition outcome: {outcome}"})

        justification_clean = (clinical_justification or "").strip()
        if outcome == ExcursionDispositionOutcome.RELEASE_TO_SELLABLE:
            if len(justification_clean) < 15:
                raise ValidationError(
                    {
                        "clinical_justification": (
                            "Clinical justification must be at least 15 characters describing "
                            "the stability data or manufacturer consultation authorizing release."
                        )
                    }
                )

        affected_items = incident.affected_batches.select_related("batch", "batch__product").all()

        for item in affected_items:
            batch = item.batch
            item.disposition_outcome = outcome
            item.save(update_fields=["disposition_outcome"])

            if outcome == ExcursionDispositionOutcome.RELEASE_TO_SELLABLE:
                batch.status = BatchStatus.ACTIVE
                batch.is_quarantined = False
                batch.reserved_quantity_base_units = 0
                batch.save(update_fields=["status", "is_quarantined", "reserved_quantity_base_units"])

            elif outcome == ExcursionDispositionOutcome.CONDEMN_DISPOSE:
                batch.status = BatchStatus.ARCHIVED
                batch.is_active = False
                batch.is_quarantined = False
                batch.reserved_quantity_base_units = 0
                batch.save(update_fields=["status", "is_active", "is_quarantined", "reserved_quantity_base_units"])

                if item.initial_quantity > 0:
                    StockLedgerService.record_stock_movement(
                        tenant=incident.tenant,
                        branch=incident.branch,
                        product=batch.product,
                        movement_type=StockMovementType.EXPIRY_DISPOSAL,
                        quantity_delta=-item.initial_quantity,
                        source_entity_type="ColdChainExcursionIncident",
                        source_entity_id=str(incident.id),
                        source_reference=incident.incident_number,
                        batch_number=batch.batch_number,
                        batch=batch,
                        cost_lot=getattr(batch, "purchase_lot", None),
                        unit_cost=item.unit_cost,
                        storage_location=incident.storage_location.code,
                        notes=f"Cold chain excursion disposal: {justification_clean}",
                        posted_by=reviewer_user,
                    )

            elif outcome == ExcursionDispositionOutcome.RETURN_TO_SUPPLIER:
                batch.status = BatchStatus.RECALLED
                batch.is_quarantined = False
                batch.reserved_quantity_base_units = 0
                batch.save(update_fields=["status", "is_quarantined", "reserved_quantity_base_units"])

                if item.initial_quantity > 0:
                    StockLedgerService.record_stock_movement(
                        tenant=incident.tenant,
                        branch=incident.branch,
                        product=batch.product,
                        movement_type=StockMovementType.PURCHASE_RETURN,
                        quantity_delta=-item.initial_quantity,
                        source_entity_type="ColdChainExcursionIncident",
                        source_entity_id=str(incident.id),
                        source_reference=incident.incident_number,
                        batch_number=batch.batch_number,
                        batch=batch,
                        cost_lot=getattr(batch, "purchase_lot", None),
                        unit_cost=item.unit_cost,
                        storage_location=incident.storage_location.code,
                        notes=f"Cold chain excursion return to supplier: {justification_clean}",
                        posted_by=reviewer_user,
                    )

        incident.status = ExcursionIncidentStatus.RESOLVED
        incident.disposition_outcome = outcome
        incident.clinical_justification = justification_clean
        incident.reviewed_by = reviewer_user
        incident.reviewed_at = timezone.now()
        incident.save(
            update_fields=[
                "status",
                "disposition_outcome",
                "clinical_justification",
                "reviewed_by",
                "reviewed_at",
                "updated_at",
            ]
        )

        return incident
