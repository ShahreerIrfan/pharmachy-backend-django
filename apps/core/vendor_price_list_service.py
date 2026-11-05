"""
backend/apps/core/vendor_price_list_service.py
Architecture Decision: ADR-0183 (P183).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Service layer governing supplier product quotations, SKU-specific price lists,
effective date windows, minimum order quantities (MOQ), negotiated trade discounts,
and automated best-price procurement resolution.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    MedicineProduct,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
    VendorProductPriceList,
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


class VendorPriceListService:
    """
    Authoritative service governing supplier product quotations, trade discounts,
    regulatory margin compliance (cost <= MRP), and best-price PO resolution.
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
                    "detail": "Cashier and unauthorized staff cannot manage vendor price lists.",
                    "message_en": "Cashier and unauthorized staff cannot manage vendor price lists.",
                    "message_bn": "ক্যাশিয়ার বা অননুমোদিত কর্মী সরবরাহকারী মূল্য তালিকা ব্যবস্থাপনার অনুমতিপ্রাপ্ত নন।",
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
                    "detail": "Tenant account subscription is suspended. Price list operations blocked.",
                    "message_en": "Tenant account subscription is suspended. Price list operations blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। মূল্য তালিকা মডিউল বন্ধ।",
                }
            )
        return tenant

    @classmethod
    def serialize_price_entry(cls, entry: VendorProductPriceList) -> Dict[str, Any]:
        """Serializes a VendorProductPriceList conforming to JSON schema contract."""
        return {
            "price_entry_id": entry.price_entry_id,
            "tenant_id": str(entry.tenant_id),
            "supplier_id": str(entry.supplier_id),
            "supplier_name": entry.supplier.name if entry.supplier else "",
            "product_id": str(entry.product_id),
            "product_name": entry.product.brand_name if entry.product else "",
            "supplier_sku_code": entry.supplier_sku_code or "",
            "package_type": entry.package_type,
            "package_size": entry.package_size,
            "unit_cost_price": str(entry.unit_cost_price),
            "package_cost_price": str(entry.package_cost_price),
            "mrp_unit": str(entry.mrp_unit),
            "discount_percentage": str(entry.discount_percentage),
            "net_unit_cost": str(entry.net_unit_cost),
            "effective_from": entry.effective_from.isoformat() if entry.effective_from else None,
            "effective_to": entry.effective_to.isoformat() if entry.effective_to else None,
            "minimum_order_quantity": entry.minimum_order_quantity,
            "free_goods_threshold": entry.free_goods_threshold,
            "free_goods_bonus_quantity": entry.free_goods_bonus_quantity,
            "is_contracted": entry.is_contracted,
            "is_active": entry.is_active,
            "notes": entry.notes or "",
            "created_by_id": str(entry.created_by_id) if entry.created_by_id else None,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        }

    @classmethod
    def list_price_entries(
        cls,
        tenant_id: str,
        supplier_id: Optional[str] = None,
        product_id: Optional[str] = None,
        active_on_date: Optional[datetime.date] = None,
        is_active: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        cls._assert_tenant_active(tenant_id)
        qs = VendorProductPriceList.objects.filter(tenant_id=tenant_id)
        if supplier_id:
            qs = qs.filter(supplier_id=supplier_id)
        if product_id:
            qs = qs.filter(product_id=product_id)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if active_on_date:
            qs = qs.filter(
                effective_from__lte=active_on_date,
            ).filter(
                Q(effective_to__isnull=True) | Q(effective_to__gte=active_on_date),
            )

        return [cls.serialize_price_entry(e) for e in qs.select_related("supplier", "product").order_by("-effective_from")]

    @classmethod
    def get_price_entry(cls, tenant_id: str, price_entry_id: str) -> Dict[str, Any]:
        cls._assert_tenant_active(tenant_id)
        entry = VendorProductPriceList.objects.select_related("supplier", "product").filter(price_entry_id=price_entry_id).first()
        if not entry:
            raise NotFound({"error_code": "ERR_PRICE_ENTRY_NOT_FOUND", "detail": f"Price entry '{price_entry_id}' not found."})
        if str(entry.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant price entry access forbidden."})
        return cls.serialize_price_entry(entry)

    @classmethod
    def create_price_entry(
        cls,
        tenant_id: str,
        actor_user: Any,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        supplier_id = data.get("supplier_id")
        if not supplier_id:
            raise ValidationError({"error_code": "ERR_MISSING_SUPPLIER_ID", "detail": "supplier_id is required."})

        supplier = Supplier.objects.filter(supplier_id=supplier_id).first()
        if not supplier:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})
        if str(supplier.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant supplier pricing forbidden."})

        product_id = data.get("product_id")
        if not product_id:
            raise ValidationError({"error_code": "ERR_MISSING_PRODUCT_ID", "detail": "product_id is required."})

        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise NotFound({"error_code": "ERR_PRODUCT_NOT_FOUND", "detail": f"Product '{product_id}' not found."})

        # Unit cost validation
        try:
            unit_cost = Decimal(str(data.get("unit_cost_price", "0.0000")))
            if unit_cost <= Decimal("0.0000"):
                raise ValidationError({"error_code": "ERR_INVALID_COST_PRICE", "detail": "Unit cost price must be positive."})
        except Exception as e:
            if isinstance(e, ValidationError):
                raise
            raise ValidationError({"error_code": "ERR_INVALID_COST_PRICE", "detail": "Invalid unit cost price value."})

        # MRP comparison (Cost cannot exceed MRP)
        mrp_unit = Decimal(str(data.get("mrp_unit", product.mrp_unit or "0.0000")))
        if mrp_unit > Decimal("0.0000") and unit_cost > mrp_unit:
            raise ValidationError(
                {
                    "error_code": "ERR_COST_EXCEEDS_MRP",
                    "detail": f"Quoted unit purchase cost ({unit_cost} BDT) cannot exceed product retail price ({mrp_unit} BDT).",
                    "message_en": f"Quoted unit purchase cost ({unit_cost} BDT) cannot exceed product retail price ({mrp_unit} BDT).",
                    "message_bn": f"ক্রয়মূল্য ({unit_cost} টাকা) পণ্যটির নির্ধারিত এমআরপি ({mrp_unit} টাকা)-এর চেয়ে বেশি হতে পারবে না।",
                }
            )

        # Dates validation
        effective_from_raw = data.get("effective_from")
        if not effective_from_raw:
            effective_from = timezone.now().date()
        elif isinstance(effective_from_raw, datetime.date):
            effective_from = effective_from_raw
        else:
            effective_from = datetime.date.fromisoformat(str(effective_from_raw))

        effective_to_raw = data.get("effective_to")
        effective_to = None
        if effective_to_raw:
            if isinstance(effective_to_raw, datetime.date):
                effective_to = effective_to_raw
            else:
                effective_to = datetime.date.fromisoformat(str(effective_to_raw))
            if effective_to < effective_from:
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_DATE_RANGE",
                        "detail": "Effective to date cannot precede effective from date.",
                    }
                )

        # Discount validation
        try:
            discount = Decimal(str(data.get("discount_percentage", "0.00")))
            if discount < Decimal("0.00") or discount > Decimal("100.00"):
                raise ValidationError({"error_code": "ERR_INVALID_DISCOUNT_PERCENTAGE", "detail": "Discount must be between 0% and 100%."})
        except Exception as e:
            if isinstance(e, ValidationError):
                raise
            raise ValidationError({"error_code": "ERR_INVALID_DISCOUNT_PERCENTAGE", "detail": "Invalid discount value."})

        moq = int(data.get("minimum_order_quantity", 1))
        if moq < 1:
            raise ValidationError({"error_code": "ERR_INVALID_MOQ", "detail": "Minimum order quantity must be at least 1."})

        package_size = max(1, int(data.get("package_size", 1)))
        package_cost = (unit_cost * Decimal(package_size)).quantize(Decimal("0.0001"))

        price_entry_id = data.get("price_entry_id") or f"vpl-{uuid.uuid4().hex[:12]}"
        if not price_entry_id.startswith("vpl-"):
            price_entry_id = f"vpl-{price_entry_id}"

        entry = VendorProductPriceList.objects.create(
            price_entry_id=price_entry_id,
            tenant=tenant,
            supplier=supplier,
            product=product,
            supplier_sku_code=data.get("supplier_sku_code", "").strip(),
            package_type=data.get("package_type", "BOX").strip().upper(),
            package_size=package_size,
            unit_cost_price=unit_cost,
            package_cost_price=package_cost,
            mrp_unit=mrp_unit,
            discount_percentage=discount,
            effective_from=effective_from,
            effective_to=effective_to,
            minimum_order_quantity=moq,
            free_goods_threshold=max(0, int(data.get("free_goods_threshold", 0))),
            free_goods_bonus_quantity=max(0, int(data.get("free_goods_bonus_quantity", 0))),
            is_contracted=bool(data.get("is_contracted", False)),
            is_active=bool(data.get("is_active", True)),
            notes=data.get("notes", "").strip(),
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="VENDOR_PRICE_LIST_CREATED",
            resource_type="VendorProductPriceList",
            resource_id=entry.price_entry_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "supplier_id": supplier.supplier_id,
                "product_id": product.product_id,
                "unit_cost_price": str(entry.unit_cost_price),
                "discount_percentage": str(entry.discount_percentage),
            },
        )

        return cls.serialize_price_entry(entry)

    @classmethod
    def update_price_entry(
        cls,
        tenant_id: str,
        actor_user: Any,
        price_entry_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        entry = VendorProductPriceList.objects.filter(price_entry_id=price_entry_id).first()
        if not entry:
            raise NotFound({"error_code": "ERR_PRICE_ENTRY_NOT_FOUND", "detail": f"Price entry '{price_entry_id}' not found."})
        if str(entry.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant price entry update forbidden."})

        if "unit_cost_price" in data:
            new_cost = Decimal(str(data["unit_cost_price"]))
            if new_cost <= Decimal("0.0000"):
                raise ValidationError({"error_code": "ERR_INVALID_COST_PRICE", "detail": "Cost must be positive."})
            if entry.mrp_unit and entry.mrp_unit > Decimal("0.0000") and new_cost > entry.mrp_unit:
                raise ValidationError({"error_code": "ERR_COST_EXCEEDS_MRP", "detail": "Cost exceeds MRP."})
            entry.unit_cost_price = new_cost
            entry.package_cost_price = (new_cost * Decimal(entry.package_size)).quantize(Decimal("0.0001"))

        if "discount_percentage" in data:
            disc = Decimal(str(data["discount_percentage"]))
            if disc < Decimal("0.00") or disc > Decimal("100.00"):
                raise ValidationError({"error_code": "ERR_INVALID_DISCOUNT_PERCENTAGE", "detail": "Discount between 0 and 100."})
            entry.discount_percentage = disc

        if "minimum_order_quantity" in data:
            moq = int(data["minimum_order_quantity"])
            if moq < 1:
                raise ValidationError({"error_code": "ERR_INVALID_MOQ", "detail": "MOQ must be >= 1."})
            entry.minimum_order_quantity = moq

        if "effective_to" in data:
            eff_to = data["effective_to"]
            if eff_to:
                eff_to_date = eff_to if isinstance(eff_to, datetime.date) else datetime.date.fromisoformat(str(eff_to))
                if eff_to_date < entry.effective_from:
                    raise ValidationError({"error_code": "ERR_INVALID_DATE_RANGE", "detail": "Effective to precedes effective from."})
                entry.effective_to = eff_to_date
            else:
                entry.effective_to = None

        if "is_active" in data:
            entry.is_active = bool(data["is_active"])
        if "notes" in data:
            entry.notes = str(data["notes"]).strip()
        if "supplier_sku_code" in data:
            entry.supplier_sku_code = str(data["supplier_sku_code"]).strip()

        entry.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="VENDOR_PRICE_LIST_UPDATED",
            resource_type="VendorProductPriceList",
            resource_id=entry.price_entry_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"price_entry_id": entry.price_entry_id, "updated_fields": list(data.keys())},
        )

        return cls.serialize_price_entry(entry)

    @classmethod
    def archive_price_entry(cls, tenant_id: str, actor_user: Any, price_entry_id: str) -> Dict[str, Any]:
        """Soft-deletes / archives a vendor price entry."""
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        entry = VendorProductPriceList.objects.filter(price_entry_id=price_entry_id).first()
        if not entry:
            raise NotFound({"error_code": "ERR_PRICE_ENTRY_NOT_FOUND", "detail": f"Price entry '{price_entry_id}' not found."})
        if str(entry.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant price entry archival forbidden."})

        entry.is_active = False
        entry.save(update_fields=["is_active", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="VENDOR_PRICE_LIST_ARCHIVED",
            resource_type="VendorProductPriceList",
            resource_id=entry.price_entry_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"price_entry_id": entry.price_entry_id, "action": "ARCHIVED"},
        )

        return cls.serialize_price_entry(entry)

    @classmethod
    def resolve_best_price(
        cls,
        tenant_id: str,
        product_id: str,
        quantity: int,
        target_date: Optional[datetime.date] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Automated procurement engine: Resolves the best supplier offer for a given product
        meeting quantity thresholds, valid date window, and lowest net purchasing cost.
        """
        cls._assert_tenant_active(tenant_id)
        if target_date is None:
            target_date = timezone.now().date()

        candidate_entries = VendorProductPriceList.objects.select_related("supplier", "product").filter(
            tenant_id=tenant_id,
            product_id=product_id,
            is_active=True,
            effective_from__lte=target_date,
        ).filter(
            Q(effective_to__isnull=True) | Q(effective_to__gte=target_date),
        )

        # Filter by MOQ
        qualifying = [e for e in candidate_entries if quantity >= e.minimum_order_quantity]
        if not qualifying:
            return None

        # Sort by lowest net unit cost, then higher discount
        best_entry = min(qualifying, key=lambda e: (e.net_unit_cost, -e.discount_percentage))

        # Calculate bonus free goods
        bonus_units = 0
        if best_entry.free_goods_threshold > 0 and best_entry.free_goods_bonus_quantity > 0:
            tiers = quantity // best_entry.free_goods_threshold
            bonus_units = tiers * best_entry.free_goods_bonus_quantity

        total_net_cost = (best_entry.net_unit_cost * Decimal(quantity)).quantize(Decimal("0.0001"))

        return {
            "selected_price_entry_id": best_entry.price_entry_id,
            "supplier_id": str(best_entry.supplier_id),
            "supplier_name": best_entry.supplier.name,
            "product_id": str(best_entry.product_id),
            "product_name": best_entry.product.brand_name,
            "demanded_quantity": quantity,
            "unit_cost_price": str(best_entry.unit_cost_price),
            "discount_percentage": str(best_entry.discount_percentage),
            "net_unit_cost": str(best_entry.net_unit_cost),
            "total_net_cost": str(total_net_cost),
            "free_goods_bonus_units": bonus_units,
            "moq_satisfied": True,
            "resolved_at": timezone.now().isoformat(),
        }

