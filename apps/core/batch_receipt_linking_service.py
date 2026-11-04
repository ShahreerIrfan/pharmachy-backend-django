"""
Architecture Decision: ADR-0168 (P168)
Goods Receipt Line to Verified Batches & Cost Lots Binding Service.
"""

from __future__ import annotations

import datetime
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
    Branch,
    GoodsReceiptBatchLink,
    GoodsReceiptLine,
    GoodsReceiptNote,
    GoodsReceiptStatus,
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
    "SUPERADMIN",
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "STAFF_PHARMACIST",
    "INVENTORY_CLERK",
    "OWNER_ADMIN",
    "ADMIN",
}


class BatchReceiptLinkingService:
    """
    Manages GRN receipt lines, multi-batch binding allocations,
    and idempotent inventory posting without duplicate stock increments.
    """

    @classmethod
    def _assert_manage_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in ALLOWED_MANAGE_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Only Pharmacists, Branch Managers, or Inventory Clerks may manage goods receipts.",
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
                    "detail": "Tenant subscription is suspended. Goods receipt operations are locked.",
                    "error_code": "ERR_TENANT_SUSPENDED",
                }
            )

    @classmethod
    def serialize_link(cls, link: GoodsReceiptBatchLink) -> Dict[str, Any]:
        """Serializes a batch link record."""
        return {
            "link_id": link.link_id,
            "line_id": str(link.line_id),
            "batch_id": str(link.batch_id),
            "batch_number": link.batch.batch_number if link.batch else "",
            "cost_lot_id": str(link.cost_lot_id) if link.cost_lot_id else None,
            "quantity_base_units": link.quantity_base_units,
            "unit_cost": str(link.unit_cost),
            "is_posted": link.is_posted,
            "posted_at": link.posted_at.isoformat() if link.posted_at else None,
            "created_at": link.created_at.isoformat() if link.created_at else None,
        }

    @classmethod
    def serialize_line(cls, line: GoodsReceiptLine) -> Dict[str, Any]:
        """Serializes a GRN line with all child batch allocations."""
        allocations = [cls.serialize_link(link) for link in line.batch_allocations.all()]
        total_allocated = sum(link.quantity_base_units for link in line.batch_allocations.all())
        return {
            "line_id": line.line_id,
            "product_id": str(line.product_id),
            "product_brand_name": line.product.brand_name if line.product else "",
            "ordered_quantity_base_units": line.ordered_quantity_base_units,
            "received_quantity_base_units": line.received_quantity_base_units,
            "unit_cost": str(line.unit_cost),
            "allocated_quantity_base_units": total_allocated,
            "is_fully_allocated": line.is_fully_allocated,
            "batch_allocations": allocations,
            "created_at": line.created_at.isoformat() if line.created_at else None,
        }

    @classmethod
    def serialize_grn(cls, grn: GoodsReceiptNote) -> Dict[str, Any]:
        """Serializes a complete Goods Received Note."""
        lines = [cls.serialize_line(line) for line in grn.lines.select_related("product").prefetch_related("batch_allocations__batch").all()]
        return {
            "grn_id": grn.grn_id,
            "grn_number": grn.grn_number,
            "tenant_id": str(grn.tenant_id),
            "branch_id": str(grn.branch_id),
            "supplier_id": str(grn.supplier_id) if grn.supplier_id else None,
            "supplier_name": grn.supplier_name,
            "purchase_order_number": grn.purchase_order_number,
            "supplier_invoice_number": grn.supplier_invoice_number,
            "receipt_date": grn.receipt_date.isoformat() if grn.receipt_date else None,
            "status": grn.status,
            "total_received_base_units": grn.total_received_base_units,
            "total_invoice_amount": str(grn.total_invoice_amount),
            "notes": grn.notes,
            "is_active": grn.is_active,
            "lines": lines,
            "created_at": grn.created_at.isoformat() if grn.created_at else None,
            "updated_at": grn.updated_at.isoformat() if grn.updated_at else None,
        }

    @classmethod
    @transaction.atomic
    def create_grn(cls, user: Any, data: Dict[str, Any]) -> Dict[str, Any]:
        """Creates a Goods Received Note in DRAFT or RECEIVED status."""
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

        supplier = None
        supplier_name = data.get("supplier_name", "")
        supplier_id = data.get("supplier_id")
        if supplier_id:
            supplier = Supplier.objects.filter(supplier_id=supplier_id, tenant=tenant).first()
            if supplier:
                supplier_name = supplier.name

        receipt_date_raw = data.get("receipt_date")
        if receipt_date_raw:
            try:
                receipt_date = (
                    receipt_date_raw
                    if isinstance(receipt_date_raw, datetime.date)
                    else datetime.date.fromisoformat(str(receipt_date_raw))
                )
            except ValueError:
                raise ValidationError({"receipt_date": "Invalid date format.", "error_code": "ERR_INVALID_DATE"})
        else:
            receipt_date = timezone.now().date()

        grn_id = data.get("grn_id") or f"grn-{uuid.uuid4().hex[:12]}"
        grn_number = data.get("grn_number") or f"GRN-{uuid.uuid4().hex[:8].upper()}"

        grn = GoodsReceiptNote.objects.create(
            grn_id=grn_id,
            grn_number=grn_number,
            tenant=tenant,
            branch=branch,
            supplier=supplier,
            supplier_name=supplier_name,
            purchase_order_number=data.get("purchase_order_number", ""),
            supplier_invoice_number=data.get("supplier_invoice_number", ""),
            receipt_date=receipt_date,
            status=data.get("status", GoodsReceiptStatus.DRAFT),
            notes=data.get("notes", ""),
            received_by=user if hasattr(user, "user_id") else None,
            is_active=True,
        )

        # Process inline lines if supplied
        lines_data = data.get("lines", [])
        for line_item in lines_data:
            cls.add_grn_line(user, grn.grn_id, line_item)

        AuditLogEntry.objects.create(
            entry_id=uuid.uuid4(),
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "inventory@pharmacy"),
            actor_role=getattr(user, "role", "STAFF_PHARMACIST"),
            action="GRN_CREATED",
            resource_type="GoodsReceiptNote",
            resource_id=grn.grn_id,
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            status=AuditActionStatus.SUCCESS,
            ip_address="127.0.0.1",
            after_state={"grn_id": grn.grn_id, "grn_number": grn.grn_number, "status": grn.status},
        )

        grn.refresh_from_db()
        return cls.serialize_grn(grn)

    @classmethod
    @transaction.atomic
    def add_grn_line(cls, user: Any, grn_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Appends a received product line item to the Goods Received Note."""
        cls._assert_manage_role(user)

        grn = GoodsReceiptNote.objects.select_for_update().filter(grn_id=grn_id).first()
        if not grn:
            raise NotFound({"detail": f"GRN {grn_id} not found.", "error_code": "ERR_GRN_NOT_FOUND"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(grn.tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        cls._assert_tenant_active(grn.tenant)

        if grn.status in (GoodsReceiptStatus.POSTED, GoodsReceiptStatus.CANCELLED, GoodsReceiptStatus.REVERSED):
            raise ValidationError(
                {
                    "detail": f"Cannot modify GRN in {grn.status} status.",
                    "error_code": "ERR_GRN_CANNOT_MODIFY_POSTED",
                }
            )

        product_id = data.get("product_id")
        if not product_id:
            raise ValidationError({"product_id": "product_id is required.", "error_code": "ERR_MISSING_PRODUCT"})
        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise NotFound({"detail": "Medicine product not found.", "error_code": "ERR_PRODUCT_NOT_FOUND"})

        qty_raw = data.get("received_quantity_base_units")
        if qty_raw is None:
            raise ValidationError(
                {"received_quantity_base_units": "Quantity is required.", "error_code": "ERR_MISSING_QUANTITY"}
            )
        try:
            received_qty = int(qty_raw)
            if received_qty <= 0:
                raise ValueError()
        except Exception:
            raise ValidationError(
                {"received_quantity_base_units": "Must be a positive integer.", "error_code": "ERR_INVALID_QUANTITY"}
            )

        cost_raw = data.get("unit_cost", "0.0000")
        try:
            unit_cost = Decimal(str(cost_raw))
            if unit_cost < Decimal("0.0000"):
                raise ValueError()
        except Exception:
            raise ValidationError({"unit_cost": "Must be a non-negative Decimal.", "error_code": "ERR_INVALID_COST"})

        line_id = data.get("line_id") or f"grnl-{uuid.uuid4().hex[:12]}"
        line = GoodsReceiptLine.objects.create(
            line_id=line_id,
            grn=grn,
            tenant=grn.tenant,
            product=product,
            ordered_quantity_base_units=int(data.get("ordered_quantity_base_units", received_qty)),
            received_quantity_base_units=received_qty,
            unit_cost=unit_cost,
            is_fully_allocated=False,
        )

        # Process child allocations if provided
        batch_allocations = data.get("batch_allocations", [])
        for alloc in batch_allocations:
            cls.link_batch_to_line(user, grn.grn_id, line.line_id, alloc)

        # Recalculate totals
        total_units = sum(l.received_quantity_base_units for l in grn.lines.all())
        total_amount = sum(l.received_quantity_base_units * l.unit_cost for l in grn.lines.all())
        grn.total_received_base_units = total_units
        grn.total_invoice_amount = total_amount
        grn.save(update_fields=["total_received_base_units", "total_invoice_amount", "updated_at"])

        line.refresh_from_db()
        return cls.serialize_line(line)

    @classmethod
    @transaction.atomic
    def link_batch_to_line(cls, user: Any, grn_id: str, line_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Binds a verified manufacturer batch to a specific GRN receipt line.
        Guarantees allocated units do not exceed the line's received quantity.
        """
        cls._assert_manage_role(user)

        grn = GoodsReceiptNote.objects.select_for_update().filter(grn_id=grn_id).first()
        if not grn:
            raise NotFound({"detail": f"GRN {grn_id} not found.", "error_code": "ERR_GRN_NOT_FOUND"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(grn.tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        cls._assert_tenant_active(grn.tenant)

        if grn.status in (GoodsReceiptStatus.POSTED, GoodsReceiptStatus.CANCELLED, GoodsReceiptStatus.REVERSED):
            raise ValidationError(
                {
                    "detail": f"Cannot modify batch allocations for {grn.status} GRN.",
                    "error_code": "ERR_GRN_CANNOT_MODIFY_POSTED",
                }
            )

        line = GoodsReceiptLine.objects.select_for_update().filter(line_id=line_id, grn=grn).first()
        if not line:
            raise NotFound({"detail": f"GRN line {line_id} not found.", "error_code": "ERR_LINE_NOT_FOUND"})

        batch_id = data.get("batch_id")
        if not batch_id:
            raise ValidationError({"batch_id": "batch_id is required.", "error_code": "ERR_MISSING_BATCH"})

        batch = BatchStock.objects.filter(batch_id=batch_id, tenant=grn.tenant, branch=grn.branch).first()
        if not batch:
            raise NotFound({"detail": "Batch not found for tenant and branch.", "error_code": "ERR_BATCH_NOT_FOUND"})

        if batch.product_id != line.product_id:
            raise ValidationError(
                {
                    "detail": f"Batch product ({batch.product_id}) does not match line product ({line.product_id}).",
                    "error_code": "ERR_PRODUCT_BATCH_MISMATCH",
                }
            )

        # Batch status checks
        today = timezone.now().date()
        if batch.expiry_date <= today or batch.status == BatchStatus.EXPIRED:
            raise ValidationError(
                {
                    "detail": f"Cannot bind expired batch ({batch.expiry_date}) to GRN.",
                    "error_code": "ERR_CANNOT_RECEIVE_INTO_EXPIRED_BATCH",
                }
            )
        if batch.status == BatchStatus.RECALLED or batch.recall_notice_id:
            raise ValidationError(
                {
                    "detail": "Cannot bind recalled batch to GRN.",
                    "error_code": "ERR_CANNOT_RECEIVE_INTO_RECALLED_BATCH",
                }
            )
        if batch.status == BatchStatus.ARCHIVED or not batch.is_active:
            raise ValidationError(
                {
                    "detail": "Cannot bind archived batch to GRN.",
                    "error_code": "ERR_CANNOT_RECEIVE_INTO_ARCHIVED_BATCH",
                }
            )

        # Quantity check
        qty_raw = data.get("quantity_base_units")
        if qty_raw is None:
            raise ValidationError({"quantity_base_units": "Quantity is required.", "error_code": "ERR_MISSING_QUANTITY"})
        try:
            alloc_qty = int(qty_raw)
            if alloc_qty <= 0:
                raise ValueError()
        except Exception:
            raise ValidationError({"quantity_base_units": "Must be positive integer.", "error_code": "ERR_INVALID_QUANTITY"})

        # Check total allocations on this line
        other_links = line.batch_allocations.exclude(batch=batch)
        other_allocated = sum(l.quantity_base_units for l in other_links)
        if other_allocated + alloc_qty > line.received_quantity_base_units:
            raise ValidationError(
                {
                    "detail": f"Total allocations ({other_allocated + alloc_qty}) exceed line received quantity ({line.received_quantity_base_units}).",
                    "error_code": "ERR_ALLOCATION_EXCEEDS_LINE",
                }
            )

        cost_raw = data.get("unit_cost", line.unit_cost)
        unit_cost = Decimal(str(cost_raw))

        link_id = data.get("link_id") or f"grnbl-{uuid.uuid4().hex[:12]}"
        link, created = GoodsReceiptBatchLink.objects.update_or_create(
            line=line,
            batch=batch,
            defaults={
                "link_id": link_id if not GoodsReceiptBatchLink.objects.filter(line=line, batch=batch).exists() else None,
                "tenant": grn.tenant,
                "grn": grn,
                "quantity_base_units": alloc_qty,
                "unit_cost": unit_cost,
                "is_posted": False,
            },
        )

        # Update line fully allocated status
        all_allocated = sum(l.quantity_base_units for l in line.batch_allocations.all())
        line.is_fully_allocated = all_allocated == line.received_quantity_base_units
        line.save(update_fields=["is_fully_allocated", "updated_at"])

        return cls.serialize_link(link)

    @classmethod
    @transaction.atomic
    def post_grn(cls, user: Any, grn_id: str) -> Dict[str, Any]:
        """
        Idempotently posts a Goods Received Note to inventory.
        Creates distinct PurchaseCostLots and increments BatchStock exactly once.
        Re-posting an already posted GRN is strictly blocked with ERR_ALREADY_POSTED.
        """
        cls._assert_manage_role(user)

        grn = GoodsReceiptNote.objects.select_for_update().filter(grn_id=grn_id).first()
        if not grn:
            raise NotFound({"detail": f"GRN {grn_id} not found.", "error_code": "ERR_GRN_NOT_FOUND"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(grn.tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        cls._assert_tenant_active(grn.tenant)

        # Anti-duplicate post guardrail
        if grn.status == GoodsReceiptStatus.POSTED:
            raise ValidationError(
                {
                    "detail": "GRN is already posted. Duplicate inventory increments are strictly prohibited.",
                    "error_code": "ERR_ALREADY_POSTED",
                }
            )

        if grn.status in (GoodsReceiptStatus.CANCELLED, GoodsReceiptStatus.REVERSED):
            raise ValidationError(
                {
                    "detail": f"Cannot post {grn.status} GRN.",
                    "error_code": "ERR_GRN_INACTIVE",
                }
            )

        # Completeness validation
        lines = list(grn.lines.select_related("product").prefetch_related("batch_allocations__batch").all())
        if not lines:
            raise ValidationError({"detail": "GRN has no lines to post.", "error_code": "ERR_EMPTY_GRN"})

        for line in lines:
            allocated = sum(l.quantity_base_units for l in line.batch_allocations.all())
            if allocated != line.received_quantity_base_units:
                raise ValidationError(
                    {
                        "detail": f"Line {line.line_id} for {line.product.brand_name} is incomplete: "
                        f"allocated {allocated}/{line.received_quantity_base_units} base units.",
                        "error_code": "ERR_ALLOCATION_MISMATCH",
                    }
                )

        now = timezone.now()
        posted_allocations = []

        # Execute stock increments exactly once
        for line in lines:
            for link in line.batch_allocations.all():
                if link.is_posted:
                    continue  # Already posted link guard

                batch = BatchStock.objects.select_for_update().get(batch_id=link.batch_id)

                # Create distinct PurchaseCostLot
                lot_id = f"pcl-{uuid.uuid4().hex[:12]}"
                cost_lot = PurchaseCostLot.objects.create(
                    lot_id=lot_id,
                    tenant=grn.tenant,
                    branch=grn.branch,
                    product=line.product,
                    batch=batch,
                    supplier=grn.supplier,
                    supplier_name=grn.supplier_name,
                    purchase_order_number=grn.purchase_order_number,
                    grn_number=grn.grn_number,
                    invoice_reference=grn.supplier_invoice_number,
                    purchase_date=grn.receipt_date,
                    unit_cost=link.unit_cost,
                    initial_quantity=link.quantity_base_units,
                    remaining_quantity=link.quantity_base_units,
                    status=PurchaseCostLotStatus.ACTIVE,
                    notes=f"Created via GRN {grn.grn_number} line {line.line_id}",
                    is_active=True,
                )

                # Increment batch stock base units
                batch.quantity_base_units += link.quantity_base_units
                if batch.status == BatchStatus.DEPLETED:
                    batch.status = BatchStatus.ACTIVE
                batch.save(update_fields=["quantity_base_units", "status"])

                # Mark link posted
                link.cost_lot = cost_lot
                link.is_posted = True
                link.posted_at = now
                link.save(update_fields=["cost_lot", "is_posted", "posted_at"])

                posted_allocations.append(
                    {
                        "link_id": link.link_id,
                        "batch_id": batch.batch_id,
                        "batch_number": batch.batch_number,
                        "cost_lot_id": cost_lot.lot_id,
                        "quantity_base_units": link.quantity_base_units,
                        "unit_cost": str(link.unit_cost),
                    }
                )

        grn.status = GoodsReceiptStatus.POSTED
        grn.verified_by = user if hasattr(user, "user_id") else None
        grn.save(update_fields=["status", "verified_by", "updated_at"])

        AuditLogEntry.objects.create(
            entry_id=uuid.uuid4(),
            tenant_id=grn.tenant_id,
            branch_id=grn.branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "inventory@pharmacy"),
            actor_role=getattr(user, "role", "BRANCH_MANAGER"),
            action="GRN_POSTED",
            resource_type="GoodsReceiptNote",
            resource_id=grn.grn_id,
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            status=AuditActionStatus.SUCCESS,
            ip_address="127.0.0.1",
            after_state={
                "grn_id": grn.grn_id,
                "grn_number": grn.grn_number,
                "status": grn.status,
                "posted_allocations": posted_allocations,
            },
        )

        grn.refresh_from_db()
        return cls.serialize_grn(grn)

    @classmethod
    @transaction.atomic
    def reverse_grn(cls, user: Any, grn_id: str, reason: str = "Shipment Return / Cancel") -> Dict[str, Any]:
        """
        Reverses a posted Goods Received Note, decrementing batch stock
        and marking linked purchase cost lots as REVERSED.
        Blocks reversal if any portion of the stock was already dispensed.
        """
        cls._assert_manage_role(user)

        grn = GoodsReceiptNote.objects.select_for_update().filter(grn_id=grn_id).first()
        if not grn:
            raise NotFound({"detail": f"GRN {grn_id} not found.", "error_code": "ERR_GRN_NOT_FOUND"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(grn.tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        cls._assert_tenant_active(grn.tenant)

        if grn.status != GoodsReceiptStatus.POSTED:
            # If still DRAFT or RECEIVED, cancel without stock mutation
            grn.status = GoodsReceiptStatus.CANCELLED
            grn.save(update_fields=["status", "updated_at"])
            return cls.serialize_grn(grn)

        # Check whether stock was consumed
        posted_links = grn.batch_links.filter(is_posted=True).select_related("batch", "cost_lot")
        for link in posted_links:
            cost_lot = link.cost_lot
            if not cost_lot or cost_lot.remaining_quantity < link.quantity_base_units:
                consumed = link.quantity_base_units - (cost_lot.remaining_quantity if cost_lot else 0)
                raise ValidationError(
                    {
                        "detail": f"Cannot reverse GRN; {consumed} units of batch '{link.batch.batch_number}' have already been dispensed.",
                        "error_code": "ERR_CANNOT_REVERSE_CONSUMED_STOCK",
                    }
                )

        # Deduct stock and reverse cost lots
        for link in posted_links:
            cost_lot = link.cost_lot
            cost_lot.remaining_quantity -= link.quantity_base_units
            cost_lot.status = PurchaseCostLotStatus.REVERSED
            cost_lot.save(update_fields=["remaining_quantity", "status"])

            batch = BatchStock.objects.select_for_update().get(batch_id=link.batch_id)
            batch.quantity_base_units -= link.quantity_base_units
            if batch.quantity_base_units == 0:
                batch.status = BatchStatus.DEPLETED
            batch.save(update_fields=["quantity_base_units", "status"])

            link.is_posted = False
            link.save(update_fields=["is_posted"])

        grn.status = GoodsReceiptStatus.REVERSED
        grn.save(update_fields=["status", "updated_at"])

        AuditLogEntry.objects.create(
            entry_id=uuid.uuid4(),
            tenant_id=grn.tenant_id,
            branch_id=grn.branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "inventory@pharmacy"),
            actor_role=getattr(user, "role", "BRANCH_MANAGER"),
            action="GRN_REVERSED",
            resource_type="GoodsReceiptNote",
            resource_id=grn.grn_id,
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            status=AuditActionStatus.SUCCESS,
            ip_address="127.0.0.1",
            after_state={"grn_id": grn.grn_id, "status": grn.status, "reason": reason},
        )

        grn.refresh_from_db()
        return cls.serialize_grn(grn)

