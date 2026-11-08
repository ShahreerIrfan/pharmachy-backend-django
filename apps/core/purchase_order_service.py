"""
backend/apps/core/purchase_order_service.py
Architecture Decision: ADR-0185 (P185).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Service layer governing purchase order creation, line items calculation,
DGDA MRP ceiling guard, supplier dispatch, goods receipt tracking,
and multi-tier guarded lifecycle state transitions.
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
    Branch,
    MedicineProduct,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    PurchaseRequisition,
    RequisitionStatus,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

ALLOWED_PROCUREMENT_ROLES = {
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    UserRole.STAFF_PHARMACIST,
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "STAFF_PHARMACIST",
    "OWNER_ADMIN",
    "ADMIN",
    "PROCUREMENT_OFFICER",
    "AUDITOR",
}

APPROVAL_ROLES = {
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
    "PROCUREMENT_OFFICER",
}


class PurchaseOrderService:
    """
    Authoritative service governing pharmaceutical purchase orders,
    commercial commitments, supplier shipments, and guarded state transitions.
    """

    @classmethod
    def _assert_procurement_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if role in [UserRole.CASHIER, "CASHIER"] or (
            role not in ALLOWED_PROCUREMENT_ROLES and not getattr(actor_user, "is_superuser", False)
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and unauthorized staff cannot manage purchase orders.",
                    "message_en": "Cashier and unauthorized staff cannot manage purchase orders.",
                    "message_bn": "ক্যাশিয়ার বা অননুমোদিত কর্মী পারচেজ অর্ডার ব্যবস্থাপনার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def _assert_approval_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if role not in APPROVAL_ROLES and not getattr(actor_user, "is_superuser", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_APPROVAL_ROLE_REQUIRED",
                    "detail": "Only Branch Managers or Pharmacy Owners can approve, dispatch, close, or cancel purchase orders.",
                    "message_en": "Only Branch Managers or Pharmacy Owners can approve, dispatch, close, or cancel purchase orders.",
                    "message_bn": "শুধুমাত্র ব্রাঞ্চ ম্যানেজার বা ফার্মেসি মালিক পারচেজ অর্ডার অনুমোদন বা ট্রানজিশন করতে পারেন।",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant_id: str) -> Tenant:
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' not found."})
        if (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant account subscription is suspended. Procurement module blocked.",
                    "message_en": "Tenant account subscription is suspended. Procurement module blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। ক্রয়াদেশ মডিউল বন্ধ।",
                }
            )
        return tenant

    @classmethod
    def serialize_item(cls, item: PurchaseOrderItem) -> Dict[str, Any]:
        return {
            "item_id": item.item_id,
            "product_id": str(item.product_id),
            "product_name": item.product.brand_name if item.product else "",
            "package_type": item.package_type,
            "package_size": item.package_size,
            "ordered_quantity": item.ordered_quantity,
            "received_quantity": item.received_quantity,
            "unit_cost_price": str(item.unit_cost_price),
            "discount_percentage": str(item.discount_percentage),
            "net_unit_cost": str(item.net_unit_cost),
            "line_total": str(item.line_total),
            "notes": item.notes or "",
        }

    @classmethod
    def serialize_po(cls, po: PurchaseOrder) -> Dict[str, Any]:
        return {
            "po_id": po.po_id,
            "po_number": po.po_number,
            "tenant_id": str(po.tenant_id),
            "branch_id": str(po.branch_id),
            "branch_name": po.branch.name if po.branch else "",
            "supplier_id": str(po.supplier_id),
            "supplier_name": po.supplier.name if po.supplier else "",
            "requisition_id": str(po.requisition_id) if po.requisition_id else None,
            "status": po.status,
            "payment_terms_days": po.payment_terms_days,
            "currency": po.currency,
            "subtotal_amount": str(po.subtotal_amount),
            "discount_amount": str(po.discount_amount),
            "tax_amount": str(po.tax_amount),
            "total_amount": str(po.total_amount),
            "expected_delivery_date": po.expected_delivery_date.isoformat() if po.expected_delivery_date else None,
            "ordered_at": po.ordered_at.isoformat() if po.ordered_at else None,
            "approved_by_id": str(po.approved_by_id) if po.approved_by_id else None,
            "approved_at": po.approved_at.isoformat() if po.approved_at else None,
            "approval_notes": po.approval_notes or "",
            "closed_at": po.closed_at.isoformat() if po.closed_at else None,
            "closing_notes": po.closing_notes or "",
            "cancelled_by_id": str(po.cancelled_by_id) if po.cancelled_by_id else None,
            "cancelled_at": po.cancelled_at.isoformat() if po.cancelled_at else None,
            "cancellation_reason": po.cancellation_reason or "",
            "notes": po.notes or "",
            "is_active": po.is_active,
            "items": [cls.serialize_item(item) for item in po.items.select_related("product").all()],
            "created_at": po.created_at.isoformat() if po.created_at else None,
            "updated_at": po.updated_at.isoformat() if po.updated_at else None,
        }

    @classmethod
    def get_purchase_order(cls, tenant_id: str, po_id: str) -> Dict[str, Any]:
        cls._assert_tenant_active(tenant_id)
        po = (
            PurchaseOrder.objects.select_related("branch", "supplier", "tenant")
            .prefetch_related("items__product")
            .filter(po_id=po_id)
            .first()
        )
        if not po:
            raise NotFound({"error_code": "ERR_PO_NOT_FOUND", "detail": f"Purchase order '{po_id}' not found."})
        if str(po.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant purchase order access forbidden."})
        return cls.serialize_po(po)

    @classmethod
    def list_purchase_orders(
        cls,
        tenant_id: str,
        branch_id: Optional[str] = None,
        supplier_id: Optional[str] = None,
        status: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        cls._assert_tenant_active(tenant_id)
        qs = (
            PurchaseOrder.objects.filter(tenant_id=tenant_id)
            .select_related("branch", "supplier")
            .prefetch_related("items__product")
        )
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if supplier_id:
            qs = qs.filter(supplier_id=supplier_id)
        if status:
            qs = qs.filter(status=status.upper())
        if is_active is not None:
            qs = qs.filter(is_active=is_active)

        return [cls.serialize_po(po) for po in qs.order_by("-created_at")]

    @classmethod
    def create_purchase_order(
        cls,
        tenant_id: str,
        branch_id: str,
        actor_user: Any,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        target_branch_id = data.get("branch_id") or branch_id
        if not target_branch_id:
            raise ValidationError({"error_code": "ERR_MISSING_BRANCH_ID", "detail": "branch_id is required."})

        branch = Branch.objects.filter(branch_id=target_branch_id).first()
        if not branch:
            raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{target_branch_id}' not found."})
        if str(branch.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant branch access forbidden."})

        supplier_id = data.get("supplier_id")
        if not supplier_id:
            raise ValidationError({"error_code": "ERR_MISSING_SUPPLIER_ID", "detail": "supplier_id is required."})

        supplier = Supplier.objects.filter(supplier_id=supplier_id).first()
        if not supplier:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})
        if str(supplier.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant supplier access forbidden."})

        # Link to upstream Requisition if present
        requisition = None
        req_id = data.get("requisition_id")
        if req_id:
            requisition = PurchaseRequisition.objects.filter(requisition_id=req_id).first()
            if requisition:
                if str(requisition.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant requisition linkage forbidden."})

        items_data = data.get("items")
        if not items_data or not isinstance(items_data, list):
            raise ValidationError({"error_code": "ERR_EMPTY_PO_ITEMS", "detail": "Purchase order must contain at least one item."})

        # Expected delivery date
        expected_delivery_date = None
        edd_raw = data.get("expected_delivery_date")
        if edd_raw:
            try:
                expected_delivery_date = edd_raw if isinstance(edd_raw, datetime.date) else datetime.date.fromisoformat(str(edd_raw))
            except ValueError:
                raise ValidationError({"error_code": "ERR_INVALID_DATE_FORMAT", "detail": "expected_delivery_date must be YYYY-MM-DD."})

        payment_terms_days = int(data.get("payment_terms_days", getattr(supplier, "payment_terms_days", 30)))
        if payment_terms_days < 0:
            payment_terms_days = 30

        # Validate line items & calculate subtotal
        validated_items = []
        subtotal = Decimal("0.0000")

        for idx, item in enumerate(items_data):
            prod_id = item.get("product_id")
            if not prod_id:
                raise ValidationError({"error_code": "ERR_MISSING_PRODUCT_ID", "detail": f"Item at index {idx} is missing product_id."})

            product = MedicineProduct.objects.filter(product_id=prod_id).first()
            if not product:
                raise NotFound({"error_code": "ERR_PRODUCT_NOT_FOUND", "detail": f"Product '{prod_id}' not found."})

            try:
                qty = int(item.get("ordered_quantity", 1))
                if qty < 1:
                    raise ValidationError({"error_code": "ERR_INVALID_QUANTITY", "detail": "Ordered quantity must be at least 1."})
            except (ValueError, TypeError):
                raise ValidationError({"error_code": "ERR_INVALID_QUANTITY", "detail": "Invalid quantity value."})

            try:
                unit_cost = Decimal(str(item.get("unit_cost_price", product.cost_unit or "1.0000")))
                if unit_cost <= Decimal("0.0000"):
                    raise ValidationError({"error_code": "ERR_INVALID_COST_PRICE", "detail": "Unit cost price must be positive."})
            except Exception as e:
                if isinstance(e, ValidationError):
                    raise
                raise ValidationError({"error_code": "ERR_INVALID_COST_PRICE", "detail": "Invalid unit cost price value."})

            # DGDA MRP ceiling check
            if product.mrp_unit and product.mrp_unit > Decimal("0.0000") and unit_cost > product.mrp_unit:
                raise ValidationError(
                    {
                        "error_code": "ERR_COST_EXCEEDS_MRP",
                        "detail": f"Quoted cost price ({unit_cost} BDT) cannot exceed product retail price ({product.mrp_unit} BDT).",
                    }
                )

            try:
                discount_pct = Decimal(str(item.get("discount_percentage", "0.00")))
                if discount_pct < Decimal("0.00") or discount_pct > Decimal("100.00"):
                    raise ValidationError({"error_code": "ERR_INVALID_DISCOUNT_PERCENTAGE", "detail": "Discount must be between 0% and 100%."})
            except Exception as e:
                if isinstance(e, ValidationError):
                    raise
                raise ValidationError({"error_code": "ERR_INVALID_DISCOUNT_PERCENTAGE", "detail": "Invalid discount percentage."})

            discount_factor = Decimal("1.0000") - (discount_pct / Decimal("100.00"))
            net_unit_cost = (unit_cost * discount_factor).quantize(Decimal("0.0001"))
            line_total = (net_unit_cost * Decimal(qty)).quantize(Decimal("0.0001"))
            subtotal += line_total

            pkg_size = max(1, int(item.get("package_size", 1)))
            validated_items.append(
                {
                    "item_id": item.get("item_id") or f"poi-{uuid.uuid4().hex[:12]}",
                    "product": product,
                    "package_type": item.get("package_type", "BOX").upper(),
                    "package_size": pkg_size,
                    "ordered_quantity": qty,
                    "unit_cost_price": unit_cost,
                    "discount_percentage": discount_pct,
                    "net_unit_cost": net_unit_cost,
                    "line_total": line_total,
                    "notes": str(item.get("notes", "")).strip(),
                }
            )

        order_discount = Decimal(str(data.get("discount_amount", "0.0000")))
        order_tax = Decimal(str(data.get("tax_amount", "0.0000")))
        total_amount = (subtotal - order_discount + order_tax).quantize(Decimal("0.0001"))
        if total_amount < Decimal("0.0000"):
            total_amount = Decimal("0.0000")

        po_id = data.get("po_id") or f"po-{uuid.uuid4().hex[:12]}"
        if not po_id.startswith("po-"):
            po_id = f"po-{po_id}"

        po_number = data.get("po_number")
        if not po_number:
            date_prefix = timezone.now().strftime("%Y%m")
            po_number = f"PO-{date_prefix}-{uuid.uuid4().hex[:6].upper()}"

        with transaction.atomic():
            po = PurchaseOrder.objects.create(
                po_id=po_id,
                po_number=po_number,
                tenant=tenant,
                branch=branch,
                supplier=supplier,
                requisition=requisition,
                status=PurchaseOrderStatus.DRAFT,
                payment_terms_days=payment_terms_days,
                currency=data.get("currency", "BDT"),
                subtotal_amount=subtotal,
                discount_amount=order_discount,
                tax_amount=order_tax,
                total_amount=total_amount,
                expected_delivery_date=expected_delivery_date,
                notes=str(data.get("notes", "")).strip(),
                created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            )

            for vi in validated_items:
                PurchaseOrderItem.objects.create(
                    item_id=vi["item_id"],
                    purchase_order=po,
                    product=vi["product"],
                    package_type=vi["package_type"],
                    package_size=vi["package_size"],
                    ordered_quantity=vi["ordered_quantity"],
                    received_quantity=0,
                    unit_cost_price=vi["unit_cost_price"],
                    discount_percentage=vi["discount_percentage"],
                    net_unit_cost=vi["net_unit_cost"],
                    line_total=vi["line_total"],
                    notes=vi["notes"],
                )

            # If generated from Requisition, mark requisition as converted
            if requisition and requisition.status != RequisitionStatus.CONVERTED_TO_PO:
                requisition.status = RequisitionStatus.CONVERTED_TO_PO
                requisition.save(update_fields=["status", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PURCHASE_ORDER_CREATED",
            resource_type="PurchaseOrder",
            resource_id=po.po_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "po_number": po.po_number,
                "supplier_id": supplier.supplier_id,
                "total_amount": str(po.total_amount),
                "items_count": len(validated_items),
            },
        )

        return cls.serialize_po(po)

    @classmethod
    def update_purchase_order(
        cls,
        tenant_id: str,
        actor_user: Any,
        po_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        po = PurchaseOrder.objects.filter(po_id=po_id).first()
        if not po:
            raise NotFound({"error_code": "ERR_PO_NOT_FOUND", "detail": f"Purchase order '{po_id}' not found."})
        if str(po.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant purchase order update forbidden."})

        if po.status != PurchaseOrderStatus.DRAFT:
            raise ValidationError(
                {
                    "error_code": "ERR_PO_LOCKED",
                    "detail": f"Purchase order in '{po.status}' status cannot be modified. Only DRAFT purchase orders can be updated.",
                }
            )

        if "notes" in data:
            po.notes = str(data["notes"]).strip()
        if "payment_terms_days" in data:
            po.payment_terms_days = max(0, int(data["payment_terms_days"]))
        if "expected_delivery_date" in data:
            edd = data["expected_delivery_date"]
            po.expected_delivery_date = edd if isinstance(edd, datetime.date) else datetime.date.fromisoformat(str(edd))

        po.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PURCHASE_ORDER_UPDATED",
            resource_type="PurchaseOrder",
            resource_id=po.po_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"updated_fields": list(data.keys())},
        )

        return cls.serialize_po(po)

    @classmethod
    def approve_purchase_order(
        cls,
        tenant_id: str,
        actor_user: Any,
        po_id: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Transitions DRAFT -> APPROVED. Commits financial liability."""
        cls._assert_approval_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        po = PurchaseOrder.objects.filter(po_id=po_id).first()
        if not po:
            raise NotFound({"error_code": "ERR_PO_NOT_FOUND", "detail": f"Purchase order '{po_id}' not found."})
        if str(po.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant purchase order approval forbidden."})

        if po.status != PurchaseOrderStatus.DRAFT:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_STATE_TRANSITION",
                    "detail": f"Cannot approve purchase order in '{po.status}' status. Must be in DRAFT.",
                }
            )

        if not po.items.exists():
            raise ValidationError({"error_code": "ERR_EMPTY_PO_ITEMS", "detail": "Cannot approve purchase order with no line items."})

        payload = data or {}
        po.status = PurchaseOrderStatus.APPROVED
        po.approved_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        po.approved_at = timezone.now()
        po.approval_notes = str(payload.get("approval_notes", "")).strip()
        po.save(update_fields=["status", "approved_by", "approved_at", "approval_notes", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PURCHASE_ORDER_APPROVED",
            resource_type="PurchaseOrder",
            resource_id=po.po_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"total_amount": str(po.total_amount), "po_number": po.po_number},
        )

        return cls.serialize_po(po)

    @classmethod
    def order_purchase_order(
        cls,
        tenant_id: str,
        actor_user: Any,
        po_id: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Transitions APPROVED -> ORDERED. Dispatches PO to supplier."""
        cls._assert_approval_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        po = PurchaseOrder.objects.filter(po_id=po_id).first()
        if not po:
            raise NotFound({"error_code": "ERR_PO_NOT_FOUND", "detail": f"Purchase order '{po_id}' not found."})
        if str(po.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant purchase order dispatch forbidden."})

        if po.status != PurchaseOrderStatus.APPROVED:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_STATE_TRANSITION",
                    "detail": f"Cannot dispatch purchase order in '{po.status}' status. PO must be APPROVED first.",
                }
            )

        po.status = PurchaseOrderStatus.ORDERED
        po.ordered_at = timezone.now()
        po.save(update_fields=["status", "ordered_at", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PURCHASE_ORDER_DISPATCHED",
            resource_type="PurchaseOrder",
            resource_id=po.po_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"po_number": po.po_number, "ordered_at": po.ordered_at.isoformat()},
        )

        return cls.serialize_po(po)

    @classmethod
    def receive_items(
        cls,
        tenant_id: str,
        actor_user: Any,
        po_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Records arriving stock against lines.
        Transitions ORDERED -> PARTIALLY_RECEIVED or CLOSED.
        """
        cls._assert_approval_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        po = PurchaseOrder.objects.filter(po_id=po_id).first()
        if not po:
            raise NotFound({"error_code": "ERR_PO_NOT_FOUND", "detail": f"Purchase order '{po_id}' not found."})
        if str(po.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant purchase order receipt forbidden."})

        if po.status not in [PurchaseOrderStatus.ORDERED, PurchaseOrderStatus.PARTIALLY_RECEIVED]:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_STATE_TRANSITION",
                    "detail": f"Cannot receive items on purchase order in '{po.status}' status. Must be ORDERED or PARTIALLY_RECEIVED.",
                }
            )

        receipts = data.get("receipts") or []
        if not receipts or not isinstance(receipts, list):
            raise ValidationError({"error_code": "ERR_EMPTY_RECEIPTS", "detail": "receipts list cannot be empty."})

        with transaction.atomic():
            for rec in receipts:
                item_id = rec.get("item_id")
                qty = int(rec.get("quantity", 0))
                if qty <= 0:
                    continue

                item = po.items.filter(item_id=item_id).first()
                if not item:
                    continue

                item.received_quantity += qty
                item.save(update_fields=["received_quantity"])

            # Check overall completion
            all_items = list(po.items.all())
            all_fulfilled = all(itm.received_quantity >= itm.ordered_quantity for itm in all_items)
            any_received = any(itm.received_quantity > 0 for itm in all_items)

            if all_fulfilled:
                po.status = PurchaseOrderStatus.CLOSED
                po.closed_at = timezone.now()
                po.closing_notes = "All line items fully received"
                po.save(update_fields=["status", "closed_at", "closing_notes", "updated_at"])
            elif any_received:
                po.status = PurchaseOrderStatus.PARTIALLY_RECEIVED
                po.save(update_fields=["status", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PURCHASE_ORDER_ITEMS_RECEIVED",
            resource_type="PurchaseOrder",
            resource_id=po.po_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"po_status": po.status, "receipts_count": len(receipts)},
        )

        return cls.serialize_po(po)

    @classmethod
    def close_purchase_order(
        cls,
        tenant_id: str,
        actor_user: Any,
        po_id: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Manually or force-closes an active purchase order."""
        cls._assert_approval_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        po = PurchaseOrder.objects.filter(po_id=po_id).first()
        if not po:
            raise NotFound({"error_code": "ERR_PO_NOT_FOUND", "detail": f"Purchase order '{po_id}' not found."})
        if str(po.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant purchase order close forbidden."})

        if po.status not in [PurchaseOrderStatus.ORDERED, PurchaseOrderStatus.PARTIALLY_RECEIVED]:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_STATE_TRANSITION",
                    "detail": f"Cannot close purchase order in '{po.status}' status. PO must be ORDERED or PARTIALLY_RECEIVED.",
                }
            )

        payload = data or {}
        po.status = PurchaseOrderStatus.CLOSED
        po.closed_at = timezone.now()
        po.closing_notes = str(payload.get("closing_notes", "Manually closed by procurement manager")).strip()
        po.save(update_fields=["status", "closed_at", "closing_notes", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PURCHASE_ORDER_CLOSED",
            resource_type="PurchaseOrder",
            resource_id=po.po_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"po_number": po.po_number, "closing_notes": po.closing_notes},
        )

        return cls.serialize_po(po)

    @classmethod
    def cancel_purchase_order(
        cls,
        tenant_id: str,
        actor_user: Any,
        po_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Cancels a purchase order. Barred if any items have already been received."""
        cls._assert_approval_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        po = PurchaseOrder.objects.filter(po_id=po_id).first()
        if not po:
            raise NotFound({"error_code": "ERR_PO_NOT_FOUND", "detail": f"Purchase order '{po_id}' not found."})
        if str(po.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant purchase order cancellation forbidden."})

        if po.status in [PurchaseOrderStatus.CLOSED, PurchaseOrderStatus.CANCELLED]:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_STATE_TRANSITION",
                    "detail": f"Cannot cancel purchase order in '{po.status}' status.",
                }
            )

        # Check if any items received
        total_received = sum(item.received_quantity for item in po.items.all())
        if total_received > 0:
            raise ValidationError(
                {
                    "error_code": "ERR_CANNOT_CANCEL_RECEIVED_PO",
                    "detail": f"Cannot cancel purchase order with received goods ({total_received} units received). Use return workflow.",
                }
            )

        reason = str(data.get("cancellation_reason", "")).strip()
        if not reason:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_CANCELLATION_REASON",
                    "detail": "cancellation_reason is required when cancelling a purchase order.",
                }
            )

        po.status = PurchaseOrderStatus.CANCELLED
        po.cancelled_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        po.cancelled_at = timezone.now()
        po.cancellation_reason = reason
        po.save(update_fields=["status", "cancelled_by", "cancelled_at", "cancellation_reason", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PURCHASE_ORDER_CANCELLED",
            resource_type="PurchaseOrder",
            resource_id=po.po_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"cancellation_reason": reason, "po_number": po.po_number},
        )

        return cls.serialize_po(po)

