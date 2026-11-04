"""
backend/apps/core/sale_lot_allocation_service.py
Architecture Decision: ADR-0170 (P170).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Service for recording sale item lot allocations against exact manufacturer batches
and purchase cost lots, supporting statutory DGDA recall audit trails, exact returns,
and unblended GAAP-compliant gross profit derivations.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List

from django.db import transaction
from django.db.models import Sum
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    MedicineProduct,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    SaleInvoice,
    SaleInvoiceItem,
    SaleLotAllocation,
    SaleLotAllocationStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

ALLOWED_MANAGE_ROLES = {
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.INVENTORY_CLERK,
    UserRole.SUPERADMIN,
    "CASHIER",
    "STAFF_PHARMACIST",
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "INVENTORY_CLERK",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}


class SaleLotAllocationService:
    """
    Core engine managing exact lot-level sale allocations, DGDA recall patient traceability,
    controlled stock returns, and unblended gross profit analytics.
    """

    @classmethod
    def _assert_manage_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in ALLOWED_MANAGE_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Role is not authorized to manage sale lot allocations.",
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
                    "detail": "Tenant subscription is suspended. Operations are locked.",
                    "error_code": "ERR_TENANT_SUSPENDED",
                }
            )

    @classmethod
    def serialize_allocation(cls, alloc: SaleLotAllocation) -> Dict[str, Any]:
        """Serializes a SaleLotAllocation record matching schema."""
        return {
            "allocation_id": alloc.allocation_id,
            "invoice_id": str(alloc.invoice_id),
            "invoice_number": alloc.invoice.invoice_number if alloc.invoice else "",
            "customer_name": alloc.invoice.customer_name if alloc.invoice else "",
            "customer_phone": alloc.invoice.customer_phone if alloc.invoice else "",
            "product_id": str(alloc.product_id),
            "product_brand_name": alloc.product.brand_name if alloc.product else "",
            "batch_id": str(alloc.batch_id),
            "batch_number": alloc.batch.batch_number if alloc.batch else "",
            "cost_lot_id": str(alloc.cost_lot_id) if alloc.cost_lot_id else None,
            "quantity_base_units": alloc.quantity_base_units,
            "returned_quantity_base_units": alloc.returned_quantity_base_units,
            "unit_selling_price": str(alloc.unit_selling_price),
            "unit_cost": str(alloc.unit_cost),
            "total_selling_price": str(alloc.total_selling_price),
            "total_cost": str(alloc.total_cost),
            "gross_profit": str(alloc.gross_profit),
            "profit_margin_pct": str(alloc.profit_margin_pct),
            "status": alloc.status,
            "is_returned": alloc.is_returned,
            "created_at": alloc.created_at.isoformat() if alloc.created_at else None,
        }

    @classmethod
    @transaction.atomic
    def record_sale_lot_allocations(
        cls,
        user: Any,
        invoice_id: str,
        items_allocations: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Records exact lot allocations for lines within an invoice.
        Deducts purchase cost lot remaining quantities and calculates exact gross profits.
        """
        cls._assert_manage_role(user)

        invoice = SaleInvoice.objects.select_for_update().filter(invoice_id=invoice_id).first()
        if not invoice:
            raise NotFound({"detail": f"Invoice '{invoice_id}' not found.", "error_code": "ERR_INVOICE_NOT_FOUND"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(invoice.tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        cls._assert_tenant_active(invoice.tenant)

        created_allocations = []

        for item_data in items_allocations:
            batch_id = item_data.get("batch_id")
            if not batch_id:
                raise ValidationError({"batch_id": "batch_id is required.", "error_code": "ERR_MISSING_BATCH"})

            batch = BatchStock.objects.select_for_update().filter(batch_id=batch_id, tenant=invoice.tenant).first()
            if not batch:
                raise NotFound({"detail": f"Batch '{batch_id}' not found.", "error_code": "ERR_BATCH_NOT_FOUND"})

            product_id = item_data.get("product_id") or batch.product_id
            product = MedicineProduct.objects.filter(product_id=product_id).first()
            if not product:
                raise NotFound({"detail": "Product not found.", "error_code": "ERR_PRODUCT_NOT_FOUND"})

            qty_raw = item_data.get("quantity_base_units")
            if qty_raw is None:
                raise ValidationError({"quantity_base_units": "Quantity is required.", "error_code": "ERR_MISSING_QUANTITY"})
            try:
                quantity = int(qty_raw)
                if quantity <= 0:
                    raise ValueError()
            except Exception:
                raise ValidationError({"quantity_base_units": "Must be positive integer.", "error_code": "ERR_INVALID_QUANTITY"})

            price_raw = item_data.get("unit_selling_price", product.mrp_unit)
            unit_selling_price = Decimal(str(price_raw))

            # Resolve purchase cost lot
            cost_lot_id = item_data.get("cost_lot_id")
            cost_lot = None
            if cost_lot_id:
                cost_lot = PurchaseCostLot.objects.select_for_update().filter(
                    lot_id=cost_lot_id, tenant=invoice.tenant, batch=batch
                ).first()

            if not cost_lot:
                # Find active FIFO purchase cost lot for this batch
                cost_lot = (
                    PurchaseCostLot.objects.select_for_update()
                    .filter(
                        tenant=invoice.tenant,
                        branch=invoice.branch,
                        batch=batch,
                        status=PurchaseCostLotStatus.ACTIVE,
                        remaining_quantity__gt=0,
                    )
                    .order_by("purchase_date", "created_at")
                    .first()
                )

            # Determine unit cost
            if cost_lot:
                unit_cost = cost_lot.unit_cost
                # Deduct cost lot remaining units
                cost_lot.remaining_quantity = max(0, cost_lot.remaining_quantity - quantity)
                if cost_lot.remaining_quantity == 0:
                    cost_lot.status = PurchaseCostLotStatus.DEPLETED
                cost_lot.save(update_fields=["remaining_quantity", "status", "updated_at"])
            else:
                unit_cost = Decimal(str(batch.cost_per_unit))

            total_selling_price = unit_selling_price * Decimal(quantity)
            total_cost = unit_cost * Decimal(quantity)
            gross_profit = total_selling_price - total_cost
            margin_pct = (
                (gross_profit / total_selling_price * Decimal("100.00")).quantize(Decimal("0.01"))
                if total_selling_price > Decimal("0.00")
                else Decimal("0.00")
            )

            # Optional invoice_item resolution
            invoice_item_id = item_data.get("invoice_item_id")
            invoice_item = (
                SaleInvoiceItem.objects.filter(id=invoice_item_id, invoice=invoice).first()
                if invoice_item_id
                else None
            )

            alloc_id = item_data.get("allocation_id") or f"sall-{uuid.uuid4().hex[:12]}"
            allocation = SaleLotAllocation.objects.create(
                allocation_id=alloc_id,
                tenant=invoice.tenant,
                branch=invoice.branch,
                invoice=invoice,
                invoice_item=invoice_item,
                product=product,
                batch=batch,
                cost_lot=cost_lot,
                quantity_base_units=quantity,
                unit_selling_price=unit_selling_price,
                unit_cost=unit_cost,
                total_selling_price=total_selling_price,
                total_cost=total_cost,
                gross_profit=gross_profit,
                profit_margin_pct=margin_pct,
                status=SaleLotAllocationStatus.COMMITTED,
            )
            created_allocations.append(allocation)

        AuditLogEntry.objects.create(
            entry_id=uuid.uuid4(),
            tenant_id=invoice.tenant_id,
            branch_id=invoice.branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "pos@pharmacy"),
            actor_role=getattr(user, "role", "CASHIER"),
            action="SALE_LOT_ALLOCATIONS_RECORDED",
            resource_type="SaleInvoice",
            resource_id=invoice.invoice_id,
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            status=AuditActionStatus.SUCCESS,
            ip_address="127.0.0.1",
            after_state={
                "invoice_id": invoice.invoice_id,
                "allocations_count": len(created_allocations),
            },
        )

        return [cls.serialize_allocation(a) for a in created_allocations]

    @classmethod
    @transaction.atomic
    def process_lot_return(
        cls,
        user: Any,
        allocation_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Processes sales return against an exact lot allocation.
        Safely restocks batch and cost lot quantities if restock_inventory=True.
        """
        cls._assert_manage_role(user)

        alloc = SaleLotAllocation.objects.select_for_update().filter(allocation_id=allocation_id).first()
        if not alloc:
            raise NotFound({"detail": f"Allocation '{allocation_id}' not found.", "error_code": "ERR_ALLOCATION_NOT_FOUND"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(alloc.tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        cls._assert_tenant_active(alloc.tenant)

        qty_raw = data.get("return_quantity_base_units")
        if qty_raw is None:
            raise ValidationError({"return_quantity_base_units": "Return quantity is required.", "error_code": "ERR_MISSING_QUANTITY"})
        try:
            return_qty = int(qty_raw)
            if return_qty <= 0:
                raise ValueError()
        except Exception:
            raise ValidationError({"return_quantity_base_units": "Must be positive integer.", "error_code": "ERR_INVALID_QUANTITY"})

        max_returnable = alloc.quantity_base_units - alloc.returned_quantity_base_units
        if return_qty > max_returnable:
            raise ValidationError(
                {
                    "detail": f"Return quantity ({return_qty}) exceeds max returnable units ({max_returnable}).",
                    "error_code": "ERR_RETURN_EXCEEDS_DISPENSED",
                    "max_returnable_units": max_returnable,
                }
            )

        restock_inventory = bool(data.get("restock_inventory", True))
        reason = data.get("reason", "Customer Return")

        # Restock inventory
        if restock_inventory:
            batch = BatchStock.objects.select_for_update().get(batch_id=alloc.batch_id)
            batch.quantity_base_units += return_qty
            if batch.status == BatchStatus.DEPLETED:
                batch.status = BatchStatus.ACTIVE
            batch.save(update_fields=["quantity_base_units", "status", "updated_at"])

            if alloc.cost_lot_id:
                cost_lot = PurchaseCostLot.objects.select_for_update().filter(lot_id=alloc.cost_lot_id).first()
                if cost_lot:
                    cost_lot.remaining_quantity += return_qty
                    if cost_lot.status == PurchaseCostLotStatus.DEPLETED:
                        cost_lot.status = PurchaseCostLotStatus.ACTIVE
                    cost_lot.save(update_fields=["remaining_quantity", "status", "updated_at"])

        alloc.returned_quantity_base_units += return_qty
        alloc.is_returned = True
        if alloc.returned_quantity_base_units == alloc.quantity_base_units:
            alloc.status = SaleLotAllocationStatus.FULLY_RETURNED
        else:
            alloc.status = SaleLotAllocationStatus.PARTIALLY_RETURNED
        alloc.save(update_fields=["returned_quantity_base_units", "is_returned", "status", "updated_at"])

        refund_amount = alloc.unit_selling_price * Decimal(return_qty)
        refunded_cogs = alloc.unit_cost * Decimal(return_qty)

        AuditLogEntry.objects.create(
            entry_id=uuid.uuid4(),
            tenant_id=alloc.tenant_id,
            branch_id=alloc.branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "pos@pharmacy"),
            actor_role=getattr(user, "role", "STAFF_PHARMACIST"),
            action="SALE_LOT_RETURN_PROCESSED",
            resource_type="SaleLotAllocation",
            resource_id=alloc.allocation_id,
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            status=AuditActionStatus.SUCCESS,
            ip_address="127.0.0.1",
            after_state={
                "allocation_id": alloc.allocation_id,
                "return_quantity": return_qty,
                "restocked": restock_inventory,
                "refund_amount": str(refund_amount),
                "reason": reason,
            },
        )

        return {
            "allocation": cls.serialize_allocation(alloc),
            "returned_quantity_base_units": return_qty,
            "refund_amount": str(refund_amount),
            "refunded_cogs": str(refunded_cogs),
            "restocked_inventory": restock_inventory,
        }

    @classmethod
    def trace_recall_impact(cls, user: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Traces all customers and invoices that received units from a specific batch or cost lot.
        Essential for statutory DGDA recall notifications and quarantine enforcement.
        """
        cls._assert_manage_role(user)

        tenant_id = params.get("tenant_id") or getattr(user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant_id": "tenant_id is required.", "error_code": "ERR_MISSING_TENANT"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})
        cls._assert_tenant_active(tenant)

        batch_id = params.get("batch_id")
        batch_number = params.get("batch_number")
        cost_lot_id = params.get("cost_lot_id")

        if not batch_id and not batch_number and not cost_lot_id:
            raise ValidationError(
                {"detail": "At least one of batch_id, batch_number, or cost_lot_id is required.", "error_code": "ERR_MISSING_FILTER"}
            )

        qs = SaleLotAllocation.objects.select_related("invoice", "product", "batch", "cost_lot").filter(tenant=tenant)

        if batch_id:
            qs = qs.filter(batch_id=batch_id)
        if batch_number:
            qs = qs.filter(batch__batch_number=batch_number)
        if cost_lot_id:
            qs = qs.filter(cost_lot_id=cost_lot_id)

        allocations = list(qs)
        total_dispensed = sum(a.quantity_base_units for a in allocations)
        total_returned = sum(a.returned_quantity_base_units for a in allocations)
        net_exposure = total_dispensed - total_returned

        # Group customer contacts
        customers = []
        seen_invoices = set()
        for a in allocations:
            inv = a.invoice
            customers.append(
                {
                    "allocation_id": a.allocation_id,
                    "invoice_id": inv.invoice_id,
                    "invoice_number": inv.invoice_number,
                    "customer_name": inv.customer_name,
                    "customer_phone": inv.customer_phone,
                    "quantity_dispensed": a.quantity_base_units,
                    "quantity_returned": a.returned_quantity_base_units,
                    "net_quantity_with_customer": a.quantity_base_units - a.returned_quantity_base_units,
                    "sale_date": a.created_at.isoformat() if a.created_at else None,
                }
            )
            seen_invoices.add(inv.invoice_id)

        # Remaining stock in branch inventory
        remaining_inventory_stock = 0
        if batch_id:
            batch = BatchStock.objects.filter(batch_id=batch_id, tenant=tenant).first()
            if batch:
                remaining_inventory_stock = batch.quantity_base_units

        return {
            "query_filter": {"batch_id": batch_id, "batch_number": batch_number, "cost_lot_id": cost_lot_id},
            "total_dispensed_units": total_dispensed,
            "total_returned_units": total_returned,
            "net_patient_exposure_units": net_exposure,
            "affected_invoices_count": len(seen_invoices),
            "remaining_inventory_stock_units": remaining_inventory_stock,
            "affected_customers": customers,
        }

    @classmethod
    def calculate_profit_analytics(cls, user: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Calculates exact unblended gross profit, revenue, COGS, and margin percentage.
        """
        cls._assert_manage_role(user)

        tenant_id = params.get("tenant_id") or getattr(user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant_id": "tenant_id is required.", "error_code": "ERR_MISSING_TENANT"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})
        cls._assert_tenant_active(tenant)

        qs = SaleLotAllocation.objects.filter(tenant=tenant)

        branch_id = params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        product_id = params.get("product_id")
        if product_id:
            qs = qs.filter(product_id=product_id)

        batch_id = params.get("batch_id")
        if batch_id:
            qs = qs.filter(batch_id=batch_id)

        start_date = params.get("start_date")
        if start_date:
            qs = qs.filter(created_at__date__gte=datetime.date.fromisoformat(str(start_date)))

        end_date = params.get("end_date")
        if end_date:
            qs = qs.filter(created_at__date__lte=datetime.date.fromisoformat(str(end_date)))

        aggregates = qs.aggregate(
            total_rev=Sum("total_selling_price"),
            total_cogs=Sum("total_cost"),
            total_profit=Sum("gross_profit"),
            total_units=Sum("quantity_base_units"),
            total_returned_units=Sum("returned_quantity_base_units"),
        )

        total_rev = aggregates["total_rev"] or Decimal("0.0000")
        total_cogs = aggregates["total_cogs"] or Decimal("0.0000")
        total_profit = aggregates["total_profit"] or Decimal("0.0000")
        total_units = aggregates["total_units"] or 0
        total_returned_units = aggregates["total_returned_units"] or 0

        margin_pct = (
            (total_profit / total_rev * Decimal("100.00")).quantize(Decimal("0.01"))
            if total_rev > Decimal("0.0000")
            else Decimal("0.00")
        )

        return {
            "tenant_id": tenant.tenant_id,
            "branch_id": branch_id,
            "total_revenue": str(total_rev),
            "total_cogs": str(total_cogs),
            "gross_profit": str(total_profit),
            "profit_margin_pct": str(margin_pct),
            "total_units_sold": total_units,
            "total_units_returned": total_returned_units,
            "net_units_retained": total_units - total_returned_units,
            "allocations_count": qs.count(),
        }

