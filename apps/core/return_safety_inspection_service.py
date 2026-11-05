"""
backend/apps/core/return_safety_inspection_service.py
Architecture Decision: ADR-0177 (P177).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Service governing sales returns to exact batch & cost lots with mandatory
pharmacist safety inspections, clinical quarantine holds, and GAAP-compliant COGS reconciliations.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    BranchStockAvailability,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    ReturnInspectionDisposition,
    ReturnSafetyInspection,
    SaleLotAllocation,
    SaleLotAllocationStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

# Mandatory pharmacist or managerial authority required for return safety inspection
INSPECTION_ALLOWED_ROLES = {
    UserRole.STAFF_PHARMACIST,
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    "STAFF_PHARMACIST",
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

REVERSAL_ALLOWED_ROLES = {
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}


class ReturnSafetyInspectionService:
    """
    Core engine enforcing mandatory clinical safety inspections on sales returns,
    governing restocking into exact batches and cost lots, and isolating uncertain stock in quarantine.
    """

    @classmethod
    def _assert_inspection_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in INSPECTION_ALLOWED_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Safety inspection and return disposition requires a registered pharmacist or manager.",
                    "error_code": "ERR_PERMISSION_DENIED",
                }
            )

    @classmethod
    def _assert_reversal_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in REVERSAL_ALLOWED_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Only branch managers or owners can reverse return safety inspections.",
                    "error_code": "ERR_PERMISSION_DENIED",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant: Tenant) -> None:
        if (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {
                    "detail": "Tenant subscription is suspended. Return operations are locked.",
                    "error_code": "ERR_TENANT_SUSPENDED",
                }
            )

    @classmethod
    def serialize_inspection(cls, insp: ReturnSafetyInspection) -> Dict[str, Any]:
        """Serializes a ReturnSafetyInspection instance matching JSON schema."""
        return {
            "inspection_id": insp.inspection_id,
            "tenant_id": str(insp.tenant_id),
            "branch_id": str(insp.branch_id),
            "allocation_id": str(insp.allocation_id),
            "invoice_id": str(insp.invoice_id),
            "invoice_number": insp.invoice.invoice_number if insp.invoice else "",
            "product_id": str(insp.product_id),
            "product_name": insp.product.brand_name if insp.product else "",
            "batch_id": str(insp.batch_id),
            "batch_number": insp.batch.batch_number if insp.batch else "",
            "cost_lot_id": str(insp.cost_lot_id) if insp.cost_lot_id else None,
            "returned_quantity_base_units": insp.returned_quantity_base_units,
            "disposition": insp.disposition,
            "inspector_id": str(insp.inspector_id) if insp.inspector_id else None,
            "inspector_name": (
                getattr(insp.inspector, "full_name", "") or getattr(insp.inspector, "email", "")
                if insp.inspector
                else ""
            ),
            "is_seal_intact": insp.is_seal_intact,
            "is_packaging_undamaged": insp.is_packaging_undamaged,
            "is_storage_condition_verified": insp.is_storage_condition_verified,
            "is_unexpired": insp.is_unexpired,
            "temperature_reading_celsius": (
                float(insp.temperature_reading_celsius) if insp.temperature_reading_celsius is not None else None
            ),
            "customer_reason": insp.customer_reason,
            "inspection_notes": insp.inspection_notes,
            "refund_amount": str(insp.refund_amount.quantize(Decimal("0.0001"))),
            "restocked_cogs": str(insp.restocked_cogs.quantize(Decimal("0.0001"))),
            "is_reversed": insp.is_reversed,
            "reversal_reason": insp.reversal_reason or None,
            "reversed_at": insp.reversed_at.isoformat() if insp.reversed_at else None,
            "created_at": insp.created_at.isoformat() if insp.created_at else None,
        }

    @classmethod
    @transaction.atomic
    def inspect_and_process_return(
        cls,
        user: Any,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Executes a formal safety inspection and processes sales return to the original lot/batch.
        """
        cls._assert_inspection_role(user)

        allocation_id = data.get("allocation_id")
        if not allocation_id:
            raise ValidationError(
                {"allocation_id": "Allocation ID is required.", "error_code": "ERR_MISSING_ALLOCATION_ID"}
            )

        alloc = (
            SaleLotAllocation.objects.select_for_update()
            .select_related("tenant", "branch", "product", "batch", "cost_lot", "invoice")
            .filter(allocation_id=allocation_id)
            .first()
        )
        if not alloc:
            raise NotFound(
                {
                    "detail": f"Sale lot allocation '{allocation_id}' not found.",
                    "error_code": "ERR_ALLOCATION_NOT_FOUND",
                }
            )

        # Cross-tenant check
        user_tenant_id = getattr(user, "tenant_id", None)
        if (
            user_tenant_id
            and str(user_tenant_id) != str(alloc.tenant_id)
            and getattr(user, "role", None) != UserRole.SUPERADMIN
        ):
            raise PermissionDenied(
                {
                    "detail": "Cross-tenant return operations are strictly barred.",
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                }
            )

        # Cross-branch check
        request_branch_id = data.get("branch_id")
        if request_branch_id and str(request_branch_id) != str(alloc.branch_id):
            raise ValidationError(
                {
                    "detail": f"Return must be processed in originating branch '{alloc.branch_id}'. Cross-branch returns to different batches are prohibited.",
                    "error_code": "ERR_CROSS_BRANCH_RETURN",
                }
            )

        cls._assert_tenant_active(alloc.tenant)

        # Validate return quantity
        qty_raw = data.get("returned_quantity_base_units")
        if qty_raw is None:
            raise ValidationError(
                {"returned_quantity_base_units": "Returned quantity is required.", "error_code": "ERR_MISSING_QUANTITY"}
            )
        try:
            return_qty = int(qty_raw)
            if return_qty <= 0:
                raise ValueError()
        except Exception:
            raise ValidationError(
                {
                    "returned_quantity_base_units": "Returned quantity must be a strictly positive integer.",
                    "error_code": "ERR_INVALID_QUANTITY",
                }
            )

        max_returnable = alloc.quantity_base_units - alloc.returned_quantity_base_units
        if return_qty > max_returnable:
            raise ValidationError(
                {
                    "detail": f"Return quantity ({return_qty}) exceeds max returnable dispensed units ({max_returnable}).",
                    "error_code": "ERR_RETURN_EXCEEDS_DISPENSED",
                    "max_returnable_units": max_returnable,
                }
            )

        # Safety Inspection Checklist
        is_seal_intact = bool(data.get("is_seal_intact", True))
        is_packaging_undamaged = bool(data.get("is_packaging_undamaged", True))
        is_storage_condition_verified = bool(data.get("is_storage_condition_verified", True))
        is_unexpired = bool(data.get("is_unexpired", True))

        temp_reading = data.get("temperature_reading_celsius")
        temp_decimal = Decimal(str(temp_reading)) if temp_reading is not None else None

        disposition = data.get("disposition")
        if disposition not in ReturnInspectionDisposition.values:
            raise ValidationError(
                {
                    "disposition": f"Invalid disposition '{disposition}'. Must be one of: {list(ReturnInspectionDisposition.values)}",
                    "error_code": "ERR_INVALID_DISPOSITION",
                }
            )

        today = timezone.now().date()
        batch = alloc.batch

        # Clinical Safety Rule: RESTOCK_APPROVED constraints
        if disposition == ReturnInspectionDisposition.RESTOCK_APPROVED:
            # 1. Inspection checklist must pass 100%
            if (
                not is_seal_intact
                or not is_packaging_undamaged
                or not is_storage_condition_verified
                or not is_unexpired
            ):
                raise ValidationError(
                    {
                        "detail": "Medicine failing safety inspection cannot be restocked to shelf inventory. Route to QUARANTINE_HOLD or DAMAGED_DESTROY.",
                        "error_code": "ERR_FAILED_SAFETY_INSPECTION_CANNOT_RESTOCK",
                    }
                )

            # 2. Expiry date guard: Expired batch cannot be restocked for sale
            if batch.expiry_date <= today:
                raise ValidationError(
                    {
                        "detail": f"Batch '{batch.batch_number}' expired on {batch.expiry_date}. Expired stock cannot be restocked for patient dispensing.",
                        "error_code": "ERR_BATCH_EXPIRED_NO_RESTOCK",
                    }
                )

            # 3. Quarantined or recalled batch guard
            if batch.is_quarantined or batch.status in [
                BatchStatus.QUARANTINED,
                BatchStatus.RECALLED,
                BatchStatus.EXPIRED,
            ]:
                raise ValidationError(
                    {
                        "detail": f"Batch '{batch.batch_number}' is currently quarantined or recalled. Cannot restock into active saleable inventory.",
                        "error_code": "ERR_BATCH_QUARANTINED_NO_RESTOCK",
                    }
                )

            # Execute stock restoration
            batch_to_update = BatchStock.objects.select_for_update().get(batch_id=alloc.batch_id)
            batch_to_update.quantity_base_units += return_qty
            if batch_to_update.status == BatchStatus.DEPLETED:
                batch_to_update.status = BatchStatus.ACTIVE
            batch_to_update.save(update_fields=["quantity_base_units", "status", "updated_at"])

            if alloc.cost_lot_id:
                cost_lot = PurchaseCostLot.objects.select_for_update().filter(lot_id=alloc.cost_lot_id).first()
                if cost_lot:
                    cost_lot.remaining_quantity += return_qty
                    if cost_lot.status == PurchaseCostLotStatus.DEPLETED:
                        cost_lot.status = PurchaseCostLotStatus.ACTIVE
                    cost_lot.save(update_fields=["remaining_quantity", "status", "updated_at"])

            # Update BranchStockAvailability (physically available & server confirmed increase)
            avail, _ = BranchStockAvailability.objects.select_for_update().get_or_create(
                tenant=alloc.tenant,
                branch=alloc.branch,
                product=alloc.product,
                defaults={
                    "availability_id": f"avail-{uuid.uuid4().hex[:12]}",
                    "quantity_on_hand": 0,
                    "quantity_reserved": 0,
                    "quantity_quarantined": 0,
                    "quantity_pending_inward": 0,
                    "quantity_physically_available": 0,
                    "quantity_server_confirmed_available": 0,
                },
            )
            avail.quantity_on_hand += return_qty
            avail.quantity_physically_available += return_qty
            avail.quantity_server_confirmed_available += return_qty
            avail.save(
                update_fields=[
                    "quantity_on_hand",
                    "quantity_physically_available",
                    "quantity_server_confirmed_available",
                    "updated_at",
                ]
            )

        elif disposition == ReturnInspectionDisposition.QUARANTINE_HOLD:
            # Uncertain quality: Do NOT increase dispensable stock!
            # Increase on-hand and quarantined in BranchStockAvailability so physical and server confirmed available remain unchanged!
            avail, _ = BranchStockAvailability.objects.select_for_update().get_or_create(
                tenant=alloc.tenant,
                branch=alloc.branch,
                product=alloc.product,
                defaults={
                    "availability_id": f"avail-{uuid.uuid4().hex[:12]}",
                    "quantity_on_hand": 0,
                    "quantity_reserved": 0,
                    "quantity_quarantined": 0,
                    "quantity_pending_inward": 0,
                    "quantity_physically_available": 0,
                    "quantity_server_confirmed_available": 0,
                },
            )
            avail.quantity_on_hand += return_qty
            avail.quantity_quarantined += return_qty
            # Physically available = on_hand - quarantined, which remains identically unchanged!
            avail.save(
                update_fields=[
                    "quantity_on_hand",
                    "quantity_quarantined",
                    "updated_at",
                ]
            )

        elif disposition == ReturnInspectionDisposition.DAMAGED_DESTROY:
            # Damaged / Spoiled: Customer is refunded/credited, but no stock is added anywhere
            pass

        # Update SaleLotAllocation
        alloc.returned_quantity_base_units += return_qty
        alloc.is_returned = True
        if alloc.returned_quantity_base_units == alloc.quantity_base_units:
            alloc.status = SaleLotAllocationStatus.FULLY_RETURNED
        else:
            alloc.status = SaleLotAllocationStatus.PARTIALLY_RETURNED
        alloc.save(update_fields=["returned_quantity_base_units", "is_returned", "status", "updated_at"])

        # Valuation calculations
        refund_amount = (alloc.unit_selling_price * Decimal(str(return_qty))).quantize(Decimal("0.0001"))
        restocked_cogs = (
            (alloc.unit_cost * Decimal(str(return_qty))).quantize(Decimal("0.0001"))
            if disposition == ReturnInspectionDisposition.RESTOCK_APPROVED
            else Decimal("0.0000")
        )

        inspection_id = f"retinsp-{uuid.uuid4().hex[:12]}"
        inspection = ReturnSafetyInspection.objects.create(
            inspection_id=inspection_id,
            tenant=alloc.tenant,
            branch=alloc.branch,
            allocation=alloc,
            invoice=alloc.invoice,
            product=alloc.product,
            batch=alloc.batch,
            cost_lot=alloc.cost_lot,
            returned_quantity_base_units=return_qty,
            disposition=disposition,
            is_seal_intact=is_seal_intact,
            is_packaging_undamaged=is_packaging_undamaged,
            is_storage_condition_verified=is_storage_condition_verified,
            is_unexpired=is_unexpired,
            temperature_reading_celsius=temp_decimal,
            inspector=user,
            customer_reason=(data.get("customer_reason") or "").strip(),
            inspection_notes=(data.get("inspection_notes") or "").strip(),
            refund_amount=refund_amount,
            restocked_cogs=restocked_cogs,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=alloc.tenant_id,
            branch_id=alloc.branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "pharmacist@pharmacy.com"),
            actor_role=str(getattr(user, "role", "STAFF_PHARMACIST")),
            action="RETURN_SAFETY_INSPECTED",
            resource_type="ReturnSafetyInspection",
            resource_id=inspection.inspection_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "disposition": disposition,
                "returned_quantity": return_qty,
                "refund_amount": str(refund_amount),
                "restocked_cogs": str(restocked_cogs),
                "batch_id": alloc.batch_id,
                "cost_lot_id": alloc.cost_lot_id,
            },
        )

        return cls.serialize_inspection(inspection)

    @classmethod
    @transaction.atomic
    def reverse_return_inspection(
        cls,
        user: Any,
        inspection_id: str,
        reason: str,
    ) -> Dict[str, Any]:
        """
        Reverses an existing return inspection, safely undoing stock restocking or quarantine holds.
        """
        cls._assert_reversal_role(user)

        cleaned_reason = (reason or "").strip()
        if len(cleaned_reason) < 10:
            raise ValidationError(
                {
                    "detail": "A reasoned justification (minimum 10 characters) is required for inspection reversal.",
                    "error_code": "ERR_AUDIT_REASON_REQUIRED",
                }
            )

        inspection = (
            ReturnSafetyInspection.objects.select_for_update()
            .select_related("tenant", "branch", "allocation", "batch", "cost_lot", "product")
            .filter(inspection_id=inspection_id)
            .first()
        )
        if not inspection:
            raise NotFound(
                {
                    "detail": f"Return inspection '{inspection_id}' not found.",
                    "error_code": "ERR_INSPECTION_NOT_FOUND",
                }
            )

        user_tenant_id = getattr(user, "tenant_id", None)
        if (
            user_tenant_id
            and str(user_tenant_id) != str(inspection.tenant_id)
            and getattr(user, "role", None) != UserRole.SUPERADMIN
        ):
            raise PermissionDenied(
                {
                    "detail": "Cross-tenant return reversal is strictly prohibited.",
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                }
            )

        cls._assert_tenant_active(inspection.tenant)

        if inspection.is_reversed:
            raise ValidationError(
                {
                    "detail": "This return inspection has already been reversed.",
                    "error_code": "ERR_ALREADY_REVERSED",
                }
            )

        return_qty = inspection.returned_quantity_base_units

        # Undo stock movements based on the original disposition
        if inspection.disposition == ReturnInspectionDisposition.RESTOCK_APPROVED:
            batch = BatchStock.objects.select_for_update().get(batch_id=inspection.batch_id)
            batch.quantity_base_units = max(0, batch.quantity_base_units - return_qty)
            if batch.quantity_base_units == 0:
                batch.status = BatchStatus.DEPLETED
            batch.save(update_fields=["quantity_base_units", "status", "updated_at"])

            if inspection.cost_lot_id:
                cost_lot = PurchaseCostLot.objects.select_for_update().filter(lot_id=inspection.cost_lot_id).first()
                if cost_lot:
                    cost_lot.remaining_quantity = max(0, cost_lot.remaining_quantity - return_qty)
                    if cost_lot.remaining_quantity == 0:
                        cost_lot.status = PurchaseCostLotStatus.DEPLETED
                    cost_lot.save(update_fields=["remaining_quantity", "status", "updated_at"])

            avail = BranchStockAvailability.objects.select_for_update().filter(
                tenant=inspection.tenant,
                branch=inspection.branch,
                product=inspection.product,
            ).first()
            if avail:
                avail.quantity_on_hand = max(0, avail.quantity_on_hand - return_qty)
                avail.quantity_physically_available = max(0, avail.quantity_physically_available - return_qty)
                avail.quantity_server_confirmed_available = max(0, avail.quantity_server_confirmed_available - return_qty)
                avail.save(
                    update_fields=[
                        "quantity_on_hand",
                        "quantity_physically_available",
                        "quantity_server_confirmed_available",
                        "updated_at",
                    ]
                )

        elif inspection.disposition == ReturnInspectionDisposition.QUARANTINE_HOLD:
            avail = BranchStockAvailability.objects.select_for_update().filter(
                tenant=inspection.tenant,
                branch=inspection.branch,
                product=inspection.product,
            ).first()
            if avail:
                avail.quantity_on_hand = max(0, avail.quantity_on_hand - return_qty)
                avail.quantity_quarantined = max(0, avail.quantity_quarantined - return_qty)
                avail.save(
                    update_fields=[
                        "quantity_on_hand",
                        "quantity_quarantined",
                        "updated_at",
                    ]
                )

        # Undo SaleLotAllocation return tracking
        alloc = SaleLotAllocation.objects.select_for_update().get(allocation_id=inspection.allocation_id)
        alloc.returned_quantity_base_units = max(0, alloc.returned_quantity_base_units - return_qty)
        if alloc.returned_quantity_base_units == 0:
            alloc.status = SaleLotAllocationStatus.COMMITTED
            alloc.is_returned = False
        else:
            alloc.status = SaleLotAllocationStatus.PARTIALLY_RETURNED
            alloc.is_returned = True
        alloc.save(update_fields=["returned_quantity_base_units", "is_returned", "status", "updated_at"])

        # Mark inspection reversed
        inspection.is_reversed = True
        inspection.reversal_reason = cleaned_reason
        inspection.reversed_by = user
        inspection.reversed_at = timezone.now()
        inspection.save(update_fields=["is_reversed", "reversal_reason", "reversed_by", "reversed_at", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=inspection.tenant_id,
            branch_id=inspection.branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "manager@pharmacy.com"),
            actor_role=str(getattr(user, "role", "BRANCH_MANAGER")),
            action="RETURN_INSPECTION_REVERSED",
            resource_type="ReturnSafetyInspection",
            resource_id=inspection.inspection_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "reversal_reason": cleaned_reason,
                "disposition": inspection.disposition,
                "returned_quantity": return_qty,
            },
        )

        return cls.serialize_inspection(inspection)

