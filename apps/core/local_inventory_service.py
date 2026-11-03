"""
backend/apps/core/local_inventory_service.py
Architecture Decision: ADR-0156 (P156).
Domain service for Local Inventory-Specific Fields (Tenant/Branch SKU, Selling Price,
Reorder Levels, Shelf Coordinates, and Store Visibility).
"""

from __future__ import annotations

import logging
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    LocalInventoryConfig,
    MedicineProduct,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.local_inventory_service")


class LocalInventoryService:
    """
    Core domain service managing tenant- and branch-scoped inventory metadata.
    Enforces local barcode/SKU formatting, custom store selling prices,
    low-stock reorder thresholds, physical location tracking, and multi-tenant RBAC.
    """

    SKU_PATTERN = re.compile(r"^[A-Za-z0-9\-/_.]*$")

    @classmethod
    def _resolve_tenant(cls, user: PharmacyUser) -> Optional[Tenant]:
        tenant_id = getattr(user, "tenant_id", None)
        if tenant_id:
            return Tenant.objects.filter(tenant_id=tenant_id).first()
        return None

    @classmethod
    def _resolve_branch(cls, user: PharmacyUser, tenant: Optional[Tenant]) -> Optional[Branch]:
        branch_id = getattr(user, "branch_id", None)
        if branch_id and tenant:
            return Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
        return None

    @classmethod
    def check_permissions(cls, user: PharmacyUser, required_action: str = "read") -> None:
        """Enforces multi-tenant RBAC policies for local inventory configurations."""
        if user.is_superuser:
            return

        if required_action in ["write", "delete"]:
            if getattr(user, "role", None) in [UserRole.CASHIER]:
                raise PermissionDenied({
                    "error_code": "ERR_ROLE_UNAUTHORIZED",
                    "detail": "Cashier role has read-only access to local inventory configurations.",
                })

        tenant: Optional[Tenant] = cls._resolve_tenant(user)
        if tenant:
            if not tenant.is_active or tenant.status in [
                TenantLifecycleStatus.SUSPENDED,
                TenantLifecycleStatus.ARCHIVED,
            ]:
                raise PermissionDenied({
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": f"Tenant '{tenant.name}' subscription is suspended or terminated.",
                })

    @classmethod
    def get_inventory_schema(cls) -> Dict[str, Any]:
        """Returns dynamic schema and constraints for local inventory configuration."""
        return {
            "schema_version": "1.0",
            "editor_type": "LOCAL_INVENTORY_FORM",
            "sku_pattern": "^[A-Za-z0-9\\-_./]*$",
            "min_selling_price_bdt": 0.01,
            "default_reorder_level": 10,
            "default_min_order_quantity": 1,
            "max_shelf_location_length": 128,
        }

    @classmethod
    def validate_inventory_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates branch SKU, custom selling price, reorder level, and location.
        """
        product_id = payload.get("product_id")
        if not product_id or not str(product_id).strip():
            raise ValidationError({
                "error_code": "ERR_MISSING_PRODUCT_ID",
                "detail": "product_id is mandatory.",
                "field": "product_id",
            })

        try:
            product = MedicineProduct.objects.get(pk=str(product_id).strip())
        except MedicineProduct.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_PRODUCT_NOT_FOUND",
                "detail": f"Product '{product_id}' does not exist.",
                "field": "product_id",
            })

        local_sku = str(payload.get("local_sku", "")).strip()
        if local_sku:
            if len(local_sku) > 64:
                raise ValidationError({
                    "error_code": "ERR_INVALID_SKU_LENGTH",
                    "detail": "local_sku cannot exceed 64 characters.",
                    "field": "local_sku",
                })
            if not cls.SKU_PATTERN.match(local_sku):
                raise ValidationError({
                    "error_code": "ERR_INVALID_SKU_FORMAT",
                    "detail": "local_sku contains invalid characters. Allowed: alphanumeric, hyphens, underscores, slashes, periods.",
                    "field": "local_sku",
                })

        # Local selling price
        price_raw = payload.get("local_selling_price_bdt")
        local_price: Optional[float] = None
        if price_raw is not None and str(price_raw).strip() != "":
            try:
                dec_price = Decimal(str(price_raw))
                if dec_price < Decimal("0.01"):
                    raise ValueError()
                local_price = float(dec_price)
            except (InvalidOperation, ValueError, TypeError):
                raise ValidationError({
                    "error_code": "ERR_INVALID_SELLING_PRICE",
                    "detail": "local_selling_price_bdt must be at least 0.01 BDT.",
                    "field": "local_selling_price_bdt",
                })

        # Reorder level
        try:
            reorder_lvl = int(payload.get("reorder_level", 10))
            if reorder_lvl < 0 or reorder_lvl > 100000:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_REORDER_LEVEL",
                "detail": "reorder_level must be an integer between 0 and 100,000.",
                "field": "reorder_level",
            })

        # Min order quantity
        try:
            min_order = int(payload.get("min_order_quantity", 1))
            if min_order < 1 or min_order > 50000:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_MIN_ORDER_QTY",
                "detail": "min_order_quantity must be a positive integer (1 to 50,000).",
                "field": "min_order_quantity",
            })

        # Max capacity
        cap_raw = payload.get("max_capacity_units")
        max_cap: Optional[int] = None
        if cap_raw is not None and str(cap_raw).strip() != "":
            try:
                max_cap = int(cap_raw)
                if max_cap < min_order:
                    raise ValidationError({
                        "error_code": "ERR_CAPACITY_BELOW_MIN_ORDER",
                        "detail": f"max_capacity_units ({max_cap}) cannot be less than min_order_quantity ({min_order}).",
                        "field": "max_capacity_units",
                    })
            except (ValueError, TypeError):
                raise ValidationError({
                    "error_code": "ERR_INVALID_MAX_CAPACITY",
                    "detail": "max_capacity_units must be a valid positive integer.",
                    "field": "max_capacity_units",
                })

        shelf_loc = str(payload.get("shelf_location", "")).strip()
        if len(shelf_loc) > 128:
            raise ValidationError({
                "error_code": "ERR_LOCATION_TOO_LONG",
                "detail": "shelf_location cannot exceed 128 characters.",
                "field": "shelf_location",
            })

        return {
            "product_id": str(product.product_id),
            "product_brand_name": product.brand_name,
            "master_mrp_unit": float(product.mrp_unit) if product.mrp_unit else 0.0,
            "branch_id": payload.get("branch_id"),
            "local_sku": local_sku,
            "local_selling_price_bdt": local_price,
            "reorder_level": reorder_lvl,
            "shelf_location": shelf_loc,
            "is_visible": bool(payload.get("is_visible", True)),
            "min_order_quantity": min_order,
            "max_capacity_units": max_cap,
            "notes": str(payload.get("notes", "")).strip(),
            "is_active": bool(payload.get("is_active", True)),
        }

    @classmethod
    @transaction.atomic
    def create_or_update_inventory_config(
        cls,
        payload: Dict[str, Any],
        user: PharmacyUser,
        target_config_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Creates or updates a local inventory configuration."""
        cls.check_permissions(user, required_action="write")
        tenant: Optional[Tenant] = cls._resolve_tenant(user)
        if not tenant:
            raise ValidationError({
                "error_code": "ERR_TENANT_REQUIRED",
                "detail": "Active tenant context required for local inventory configuration.",
            })

        config: Optional[LocalInventoryConfig] = None
        if target_config_id:
            try:
                config = LocalInventoryConfig.objects.select_related("tenant", "branch", "product").get(pk=target_config_id)
            except LocalInventoryConfig.DoesNotExist:
                raise ValidationError({
                    "error_code": "ERR_CONFIG_NOT_FOUND",
                    "detail": f"LocalInventoryConfig '{target_config_id}' not found.",
                })
            user_tenant_id = getattr(user, "tenant_id", None)
            if config.tenant_id and user_tenant_id and str(config.tenant.tenant_id) != str(user_tenant_id) and not user.is_superuser:
                raise PermissionDenied({
                    "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                    "detail": "Cross-tenant inventory modification prohibited.",
                })

        merged_payload = {}
        if config:
            merged_payload = {
                "product_id": str(config.product_id),
                "branch_id": str(config.branch.branch_id) if config.branch else None,
                "local_sku": config.local_sku,
                "local_selling_price_bdt": float(config.local_selling_price_bdt) if config.local_selling_price_bdt else None,
                "reorder_level": config.reorder_level,
                "shelf_location": config.shelf_location,
                "is_visible": config.is_visible,
                "min_order_quantity": config.min_order_quantity,
                "max_capacity_units": config.max_capacity_units,
                "notes": config.notes,
                "is_active": config.is_active,
            }
        merged_payload.update(payload)
        validated = cls.validate_inventory_payload(merged_payload)

        product = MedicineProduct.objects.get(pk=validated["product_id"])

        # Resolve branch
        branch: Optional[Branch] = None
        branch_id = validated.get("branch_id")
        if branch_id:
            branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
            if not branch:
                raise ValidationError({
                    "error_code": "ERR_BRANCH_NOT_FOUND",
                    "detail": f"Branch '{branch_id}' not found for tenant '{tenant.name}'.",
                })
        else:
            branch = cls._resolve_branch(user, tenant)

        created = False
        if not config:
            q = Q(product=product, tenant=tenant)
            if branch:
                q &= Q(branch=branch)
            else:
                q &= Q(branch__isnull=True)
            config = LocalInventoryConfig.objects.filter(q).first()
            if not config:
                config_id = payload.get("config_id") or f"loc-inv-{uuid.uuid4().hex[:12]}"
                config = LocalInventoryConfig(
                    config_id=config_id,
                    tenant=tenant,
                    branch=branch,
                    product=product,
                )
                created = True

        config.local_sku = validated["local_sku"]
        if validated["local_selling_price_bdt"] is not None:
            config.local_selling_price_bdt = Decimal(str(validated["local_selling_price_bdt"]))
        else:
            config.local_selling_price_bdt = None

        config.reorder_level = validated["reorder_level"]
        config.shelf_location = validated["shelf_location"]
        config.is_visible = validated["is_visible"]
        config.min_order_quantity = validated["min_order_quantity"]
        config.max_capacity_units = validated["max_capacity_units"]
        config.notes = validated["notes"]
        config.is_active = validated["is_active"]

        config.full_clean()
        config.save()

        action = "LOCAL_INVENTORY_CREATED" if created else "LOCAL_INVENTORY_UPDATED"
        AuditLogEntry.objects.create(
            tenant_id=tenant.tenant_id,
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action=action,
            resource_type="LocalInventoryConfig",
            resource_id=str(config.config_id),
            metadata_json={
                "product_id": str(product.product_id),
                "local_sku": config.local_sku,
                "reorder_level": config.reorder_level,
                "shelf_location": config.shelf_location,
                "is_visible": config.is_visible,
            },
            status=AuditActionStatus.SUCCESS,
        )

        return cls._serialize_config(config)

    @classmethod
    def get_inventory_config(cls, config_id: str, user: PharmacyUser) -> Dict[str, Any]:
        """Retrieves details of a specific local inventory configuration."""
        cls.check_permissions(user, required_action="read")
        try:
            config = LocalInventoryConfig.objects.select_related("product", "tenant", "branch").get(pk=config_id)
        except LocalInventoryConfig.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_CONFIG_NOT_FOUND",
                "detail": f"LocalInventoryConfig '{config_id}' not found.",
            })

        user_tenant_id = getattr(user, "tenant_id", None)
        if config.tenant and user_tenant_id and str(config.tenant.tenant_id) != str(user_tenant_id) and not user.is_superuser:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                "detail": "Cross-tenant inventory access prohibited.",
            })

        return cls._serialize_config(config)

    @classmethod
    def list_inventory_configs(
        cls,
        user: PharmacyUser,
        product_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        is_visible: Optional[bool] = None,
        search_sku: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists local inventory configurations scoped to user's tenant."""
        cls.check_permissions(user, required_action="read")
        qs = LocalInventoryConfig.objects.select_related("product", "tenant", "branch").filter(is_active=True)

        user_tenant: Optional[Tenant] = cls._resolve_tenant(user)
        if not user.is_superuser:
            if user_tenant:
                qs = qs.filter(tenant=user_tenant)
            else:
                qs = qs.none()

        if product_id:
            qs = qs.filter(product_id=product_id)

        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)

        if is_visible is not None:
            qs = qs.filter(is_visible=is_visible)

        if search_sku:
            qs = qs.filter(local_sku__icontains=search_sku)

        return [cls._serialize_config(c) for c in qs]

    @classmethod
    @transaction.atomic
    def archive_inventory_config(cls, config_id: str, user: PharmacyUser, hard_delete: bool = False) -> Dict[str, Any]:
        """Archives or soft-deletes a local inventory configuration."""
        cls.check_permissions(user, required_action="delete")
        try:
            config = LocalInventoryConfig.objects.get(pk=config_id)
        except LocalInventoryConfig.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_CONFIG_NOT_FOUND",
                "detail": f"LocalInventoryConfig '{config_id}' not found.",
            })

        user_tenant_id = getattr(user, "tenant_id", None)
        if config.tenant and user_tenant_id and str(config.tenant.tenant_id) != str(user_tenant_id) and not user.is_superuser:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                "detail": "Cross-tenant inventory deletion prohibited.",
            })

        if hard_delete:
            config.delete()
            status_msg = "DELETED"
        else:
            config.is_active = False
            config.save(update_fields=["is_active", "updated_at"])
            status_msg = "ARCHIVED"

        AuditLogEntry.objects.create(
            tenant_id=config.tenant.tenant_id if config.tenant else "PLATFORM",
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="LOCAL_INVENTORY_REMOVED",
            resource_type="LocalInventoryConfig",
            resource_id=config_id,
            metadata_json={"status": status_msg},
            status=AuditActionStatus.SUCCESS,
        )

        return {"status": status_msg, "config_id": config_id}

    @classmethod
    def _serialize_config(cls, config: LocalInventoryConfig) -> Dict[str, Any]:
        """Converts model instance to JSON dictionary."""
        effective_price = (
            float(config.local_selling_price_bdt)
            if config.local_selling_price_bdt is not None
            else (float(config.product.mrp_unit) if config.product and config.product.mrp_unit else 0.0)
        )

        return {
            "config_id": str(config.config_id),
            "product_id": str(config.product_id),
            "product_brand_name": config.product.brand_name if config.product else "",
            "tenant_id": str(config.tenant.tenant_id) if config.tenant else None,
            "branch_id": str(config.branch.branch_id) if config.branch else None,
            "branch_name": config.branch.name if config.branch else "All Branches",
            "local_sku": config.local_sku,
            "local_selling_price_bdt": float(config.local_selling_price_bdt) if config.local_selling_price_bdt else None,
            "effective_selling_price_bdt": effective_price,
            "reorder_level": config.reorder_level,
            "shelf_location": config.shelf_location,
            "is_visible": config.is_visible,
            "min_order_quantity": config.min_order_quantity,
            "max_capacity_units": config.max_capacity_units,
            "notes": config.notes,
            "is_active": config.is_active,
            "created_at": config.created_at.isoformat(),
            "updated_at": config.updated_at.isoformat(),
        }
