"""
Architecture Decision: ADR-0167 (P167)
Purchase Cost Lot Entity & COGS Valuation Separation Service.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

ALLOWED_MANAGE_ROLES = {
    UserRole.SUPERADMIN,
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.STAFF_PHARMACIST,
    UserRole.INVENTORY_CLERK,
    UserRole.FINANCE_OFFICER,
    "SUPERADMIN",
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "STAFF_PHARMACIST",
    "INVENTORY_CLERK",
    "FINANCE_OFFICER",
    "OWNER_ADMIN",
    "ADMIN",
}


class PurchaseCostLotService:
    """
    Manages purchase cost lot creation, COGS allocation across FEFO/FIFO tiers,
    reversal restoration, and inventory asset valuation.
    """

    @classmethod
    def _assert_manage_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in ALLOWED_MANAGE_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Only Pharmacists, Branch Managers, or Inventory/Finance Officers may manage purchase cost lots.",
                    "error_code": "ERR_ROLE_UNAUTHORIZED",
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
                    "detail": "Tenant subscription is suspended. Purchase lot management is locked.",
                    "error_code": "ERR_TENANT_SUSPENDED",
                }
            )

    @classmethod
    def serialize_lot(cls, lot: PurchaseCostLot) -> Dict[str, Any]:
        """Serializes purchase cost lot into schema format."""
        return {
            "lot_id": lot.lot_id,
            "tenant_id": str(lot.tenant_id),
            "branch_id": str(lot.branch_id),
            "product_id": str(lot.product_id),
            "product_brand_name": lot.product.brand_name if lot.product else "",
            "batch_id": str(lot.batch_id),
            "batch_number": lot.batch.batch_number if lot.batch else "",
            "supplier_id": str(lot.supplier_id) if lot.supplier_id else None,
            "supplier_name": lot.supplier_name or (lot.supplier.name if lot.supplier else ""),
            "purchase_order_number": lot.purchase_order_number,
            "grn_number": lot.grn_number,
            "invoice_reference": lot.invoice_reference,
            "purchase_date": lot.purchase_date.isoformat() if lot.purchase_date else None,
            "unit_cost": str(lot.unit_cost),
            "initial_quantity": lot.initial_quantity,
            "remaining_quantity": lot.remaining_quantity,
            "status": lot.status,
            "notes": lot.notes,
            "is_active": lot.is_active,
            "created_at": lot.created_at.isoformat() if lot.created_at else None,
            "updated_at": lot.updated_at.isoformat() if lot.updated_at else None,
        }

    @classmethod
    @transaction.atomic
    def create_purchase_lot(cls, user: Any, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Receives stock into a specific purchase cost lot and increments batch stock.
        """
        cls._assert_manage_role(user)

        tenant_id = data.get("tenant_id") or getattr(user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant_id": "tenant_id is required.", "error_code": "ERR_MISSING_TENANT"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})
        cls._assert_tenant_active(tenant)

        branch_id = data.get("branch_id")
        if not branch_id:
            raise ValidationError({"branch_id": "branch_id is required.", "error_code": "ERR_MISSING_BRANCH"})
        branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
        if not branch:
            raise NotFound({"detail": "Branch not found for tenant.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        product_id = data.get("product_id")
        if not product_id:
            raise ValidationError({"product_id": "product_id is required.", "error_code": "ERR_MISSING_PRODUCT"})
        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise NotFound({"detail": "Product not found.", "error_code": "ERR_PRODUCT_NOT_FOUND"})

        batch_id = data.get("batch_id")
        if not batch_id:
            raise ValidationError({"batch_id": "batch_id is required.", "error_code": "ERR_MISSING_BATCH"})

        # Lock the batch record
        batch = (
            BatchStock.objects.select_for_update()
            .filter(batch_id=batch_id, tenant=tenant, branch=branch, product=product)
            .first()
        )
        if not batch:
            raise NotFound(
                {
                    "detail": "Batch not found for tenant, branch, and product.",
                    "error_code": "ERR_BATCH_NOT_FOUND",
                }
            )

        # Batch status checks
        today = timezone.now().date()
        if batch.expiry_date <= today or batch.status == BatchStatus.EXPIRED:
            raise ValidationError(
                {
                    "detail": f"Cannot receive purchase stock into expired batch ({batch.expiry_date}).",
                    "error_code": "ERR_CANNOT_RECEIVE_INTO_EXPIRED_BATCH",
                }
            )
        if batch.status == BatchStatus.RECALLED or batch.recall_notice_id:
            raise ValidationError(
                {
                    "detail": "Cannot receive purchase stock into recalled batch.",
                    "error_code": "ERR_CANNOT_RECEIVE_INTO_RECALLED_BATCH",
                }
            )
        if batch.status == BatchStatus.ARCHIVED or not batch.is_active:
            raise ValidationError(
                {
                    "detail": "Cannot receive purchase stock into archived batch.",
                    "error_code": "ERR_CANNOT_RECEIVE_INTO_ARCHIVED_BATCH",
                }
            )

        # Quantity validation
        quantity_raw = data.get("initial_quantity")
        if quantity_raw is None:
            raise ValidationError({"initial_quantity": "initial_quantity is required.", "error_code": "ERR_MISSING_QUANTITY"})
        try:
            initial_quantity = int(quantity_raw)
            if initial_quantity <= 0:
                raise ValueError()
        except Exception:
            raise ValidationError(
                {"initial_quantity": "initial_quantity must be a positive integer.", "error_code": "ERR_INVALID_QUANTITY"}
            )

        # Cost validation
        cost_raw = data.get("unit_cost")
        if cost_raw is None:
            raise ValidationError({"unit_cost": "unit_cost is required.", "error_code": "ERR_MISSING_COST"})
        try:
            unit_cost = Decimal(str(cost_raw))
            if unit_cost < Decimal("0.0000"):
                raise ValueError()
        except Exception:
            raise ValidationError(
                {"unit_cost": "unit_cost must be a non-negative Decimal.", "error_code": "ERR_INVALID_COST"}
            )

        # Supplier resolution
        supplier = None
        supplier_name = data.get("supplier_name", "")
        supplier_id = data.get("supplier_id")
        if supplier_id:
            supplier = Supplier.objects.filter(supplier_id=supplier_id, tenant=tenant).first()
            if supplier:
                supplier_name = supplier.name

        purchase_date_raw = data.get("purchase_date")
        if purchase_date_raw:
            try:
                purchase_date = (
                    purchase_date_raw
                    if isinstance(purchase_date_raw, datetime.date)
                    else datetime.date.fromisoformat(str(purchase_date_raw))
                )
            except ValueError:
                raise ValidationError({"purchase_date": "Invalid date format.", "error_code": "ERR_INVALID_DATE"})
        else:
            purchase_date = today

        lot_id = data.get("lot_id") or f"pcl-{uuid.uuid4().hex[:12]}"
        lot = PurchaseCostLot.objects.create(
            lot_id=lot_id,
            tenant=tenant,
            branch=branch,
            product=product,
            batch=batch,
            supplier=supplier,
            supplier_name=supplier_name,
            purchase_order_number=data.get("purchase_order_number", ""),
            grn_number=data.get("grn_number", ""),
            invoice_reference=data.get("invoice_reference", ""),
            purchase_date=purchase_date,
            unit_cost=unit_cost,
            initial_quantity=initial_quantity,
            remaining_quantity=initial_quantity,
            status=PurchaseCostLotStatus.ACTIVE,
            notes=data.get("notes", ""),
            is_active=True,
        )

        # Atomically increment batch base unit quantity
        batch_before_units = batch.quantity_base_units
        batch.quantity_base_units += initial_quantity
        batch.save(update_fields=["quantity_base_units"])

        # Audit log entry
        AuditLogEntry.objects.create(
            entry_id=uuid.uuid4(),
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "inventory@pharmacy"),
            actor_role=getattr(user, "role", "STAFF_PHARMACIST"),
            action="PURCHASE_COST_LOT_CREATED",
            resource_type="PurchaseCostLot",
            resource_id=lot.lot_id,
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            status=AuditActionStatus.SUCCESS,
            ip_address="127.0.0.1",
            before_state={"batch_id": batch.batch_id, "quantity_base_units": batch_before_units},
            after_state=cls.serialize_lot(lot),
        )

        return cls.serialize_lot(lot)

    @classmethod
    def list_purchase_lots(
        cls,
        user: Any,
        tenant_id: str,
        branch_id: Optional[str] = None,
        product_id: Optional[str] = None,
        batch_id: Optional[str] = None,
        status_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists purchase cost lots for tenant ordered by FIFO purchase date."""
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        qs = PurchaseCostLot.objects.select_related("product", "batch", "supplier").filter(
            tenant=tenant,
            is_active=True,
        )

        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if product_id:
            qs = qs.filter(product_id=product_id)
        if batch_id:
            qs = qs.filter(batch_id=batch_id)
        if status_filter:
            qs = qs.filter(status=status_filter)

        qs = qs.order_by("purchase_date", "created_at")
        return [cls.serialize_lot(lot) for lot in qs]

    @classmethod
    def get_purchase_lot(cls, user: Any, tenant_id: str, lot_id: str) -> Dict[str, Any]:
        """Retrieves single purchase cost lot record."""
        lot = (
            PurchaseCostLot.objects.select_related("product", "batch", "supplier", "tenant", "branch")
            .filter(lot_id=lot_id, tenant_id=tenant_id)
            .first()
        )
        if not lot:
            raise NotFound({"detail": f"Purchase cost lot {lot_id} not found.", "error_code": "ERR_LOT_NOT_FOUND"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        return cls.serialize_lot(lot)

    @classmethod
    @transaction.atomic
    def calculate_and_allocate_cogs(
        cls,
        user: Any,
        tenant_id: str,
        branch_id: str,
        product_id: str,
        quantity: int,
        batch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Allocates requested quantity across active purchase cost lots in FEFO/FIFO order.
        Returns exact penny COGS calculation and depletes lots atomically.
        """
        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})
        cls._assert_tenant_active(tenant)

        if quantity <= 0:
            raise ValidationError(
                {"quantity": "Quantity must be greater than zero.", "error_code": "ERR_INVALID_QUANTITY"}
            )

        today = timezone.now().date()

        # Find eligible dispensable batches in FEFO order
        batch_qs = (
            BatchStock.objects.select_for_update()
            .filter(
                tenant=tenant,
                branch_id=branch_id,
                product_id=product_id,
                status=BatchStatus.ACTIVE,
                is_active=True,
                is_quarantined=False,
                recall_notice__isnull=True,
                expiry_date__gt=today,
                quantity_base_units__gt=0,
            )
            .order_by("expiry_date", "batch_number")
        )

        if batch_id:
            batch_qs = batch_qs.filter(batch_id=batch_id)

        batches = list(batch_qs)
        if not batches:
            raise ValidationError(
                {
                    "detail": "No active, dispensable batches available for allocation.",
                    "error_code": "ERR_NO_DISPENSABLE_BATCHES",
                }
            )

        remaining_needed = quantity
        allocations: List[Dict[str, Any]] = []
        total_cogs = Decimal("0.0000")

        for batch in batches:
            # Query active purchase cost lots for this batch in FIFO order
            cost_lots = list(
                PurchaseCostLot.objects.select_for_update()
                .filter(
                    batch=batch,
                    status=PurchaseCostLotStatus.ACTIVE,
                    is_active=True,
                    remaining_quantity__gt=0,
                )
                .order_by("purchase_date", "created_at")
            )

            for lot in cost_lots:
                take_units = min(lot.remaining_quantity, remaining_needed)
                lot_cogs = take_units * lot.unit_cost
                total_cogs += lot_cogs

                lot.remaining_quantity -= take_units
                if lot.remaining_quantity == 0:
                    lot.status = PurchaseCostLotStatus.DEPLETED
                lot.save(update_fields=["remaining_quantity", "status"])

                batch.quantity_base_units -= take_units
                remaining_needed -= take_units

                allocations.append(
                    {
                        "lot_id": lot.lot_id,
                        "batch_id": batch.batch_id,
                        "batch_number": batch.batch_number,
                        "units_allocated": take_units,
                        "unit_cost": str(lot.unit_cost),
                        "lot_cogs": str(lot_cogs.quantize(Decimal("0.0001"))),
                    }
                )

                if remaining_needed == 0:
                    break

            batch.save(update_fields=["quantity_base_units"])
            if remaining_needed == 0:
                break

        if remaining_needed > 0:
            raise ValidationError(
                {
                    "detail": f"Insufficient purchase cost lot stock. Short by {remaining_needed} units.",
                    "error_code": "ERR_INSUFFICIENT_COST_LOT_STOCK",
                }
            )

        effective_unit_cogs = (total_cogs / Decimal(quantity)).quantize(Decimal("0.0001"))

        # Audit log entry
        AuditLogEntry.objects.create(
            entry_id=uuid.uuid4(),
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "pos@checkout"),
            actor_role=getattr(user, "role", "CASHIER"),
            action="PURCHASE_COST_LOT_ALLOCATED",
            resource_type="PurchaseCostLot",
            resource_id=allocations[0]["lot_id"] if allocations else "",
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            status=AuditActionStatus.SUCCESS,
            ip_address="127.0.0.1",
            after_state={
                "requested_quantity": quantity,
                "total_cogs": str(total_cogs.quantize(Decimal("0.0001"))),
                "allocations": allocations,
            },
        )

        return {
            "total_units": quantity,
            "total_cogs": str(total_cogs.quantize(Decimal("0.0001"))),
            "effective_unit_cogs": str(effective_unit_cogs),
            "allocations": allocations,
        }

    @classmethod
    @transaction.atomic
    def reverse_purchase_lot_allocation(
        cls,
        user: Any,
        tenant_id: str,
        allocations: List[Dict[str, Any]],
        reason: str = "Sale Reversal / Customer Return",
    ) -> Dict[str, Any]:
        """
        Reverses previous lot allocations, restoring quantities back into
        originating cost lots and recalculating restored COGS.
        """
        cls._assert_manage_role(user)

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})
        cls._assert_tenant_active(tenant)

        if not allocations:
            raise ValidationError(
                {"allocations": "allocations list cannot be empty.", "error_code": "ERR_EMPTY_ALLOCATIONS"}
            )

        total_restored_units = 0
        total_restored_cogs = Decimal("0.0000")
        restored_records: List[Dict[str, Any]] = []

        for item in allocations:
            lot_id = item.get("lot_id")
            units_raw = item.get("units_allocated") or item.get("units")
            if not lot_id or units_raw is None:
                continue

            units = int(units_raw)
            if units <= 0:
                continue

            lot = (
                PurchaseCostLot.objects.select_for_update()
                .filter(lot_id=lot_id, tenant=tenant)
                .first()
            )
            if not lot:
                raise NotFound({"detail": f"Lot {lot_id} not found.", "error_code": "ERR_LOT_NOT_FOUND"})

            # Restore lot quantity
            lot.remaining_quantity += units
            if lot.status == PurchaseCostLotStatus.DEPLETED:
                lot.status = PurchaseCostLotStatus.ACTIVE
            lot.save(update_fields=["remaining_quantity", "status"])

            # Restore batch quantity
            batch = BatchStock.objects.select_for_update().get(batch_id=lot.batch_id)
            batch.quantity_base_units += units
            if batch.status == BatchStatus.DEPLETED:
                batch.status = BatchStatus.ACTIVE
            batch.save(update_fields=["quantity_base_units", "status"])

            item_cogs = units * lot.unit_cost
            total_restored_cogs += item_cogs
            total_restored_units += units

            restored_records.append(
                {
                    "lot_id": lot.lot_id,
                    "batch_id": batch.batch_id,
                    "units_restored": units,
                    "unit_cost": str(lot.unit_cost),
                    "cogs_reversed": str(item_cogs.quantize(Decimal("0.0001"))),
                }
            )

        # Audit log entry
        AuditLogEntry.objects.create(
            entry_id=uuid.uuid4(),
            tenant_id=tenant_id,
            branch_id=lot.branch_id if lot else None,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "inventory@pharmacy"),
            actor_role=getattr(user, "role", "BRANCH_MANAGER"),
            action="PURCHASE_COST_LOT_REVERSED",
            resource_type="PurchaseCostLot",
            resource_id=restored_records[0]["lot_id"] if restored_records else "",
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            status=AuditActionStatus.SUCCESS,
            ip_address="127.0.0.1",
            after_state={
                "reason": reason,
                "total_restored_units": total_restored_units,
                "total_restored_cogs": str(total_restored_cogs.quantize(Decimal("0.0001"))),
                "restored_records": restored_records,
            },
        )

        return {
            "total_restored_units": total_restored_units,
            "total_restored_cogs": str(total_restored_cogs.quantize(Decimal("0.0001"))),
            "reason": reason,
            "restored_records": restored_records,
        }

    @classmethod
    def get_inventory_valuation(
        cls,
        user: Any,
        tenant_id: str,
        branch_id: Optional[str] = None,
        product_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Computes exact aggregate inventory balance sheet valuation
        sum(remaining_quantity * unit_cost) across all active cost lots.
        """
        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})

        qs = PurchaseCostLot.objects.filter(
            tenant=tenant,
            status=PurchaseCostLotStatus.ACTIVE,
            is_active=True,
            remaining_quantity__gt=0,
        )

        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if product_id:
            qs = qs.filter(product_id=product_id)

        total_units = 0
        total_valuation = Decimal("0.0000")
        lot_count = 0

        for lot in qs:
            total_units += lot.remaining_quantity
            total_valuation += lot.remaining_quantity * lot.unit_cost
            lot_count += 1

        return {
            "tenant_id": tenant_id,
            "branch_id": branch_id,
            "product_id": product_id,
            "active_lot_count": lot_count,
            "total_quantity_base_units": total_units,
            "total_inventory_valuation_bdt": str(total_valuation.quantize(Decimal("0.0001"))),
        }
