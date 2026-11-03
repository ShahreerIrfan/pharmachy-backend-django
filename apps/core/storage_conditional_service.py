"""
backend/apps/core/storage_conditional_service.py
Architecture Decision: ADR-0155 (P155).
Domain service for Conditional Cold-Chain Storage Fields, Temperature Ranges,
Refrigerator Placement, Excursion Tolerance, and Handling Alerts.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    ExcursionHandlingAction,
    MedicineProduct,
    StorageConditionalConfig,
    StoragePlacementZone,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.storage_conditional_service")


class StorageConditionalService:
    """
    Core domain service managing product-specific storage conditions.
    Enforces conditional cold-chain field exposure, safe temperature ranges,
    refrigerator placement integrity, and multi-tenant authorization.
    """

    ALLOWED_PLACEMENTS = [p.value for p in StoragePlacementZone]
    ALLOWED_ACTIONS = [a.value for a in ExcursionHandlingAction]

    @classmethod
    def _resolve_tenant(cls, user: PharmacyUser) -> Optional[Tenant]:
        tenant_id = getattr(user, "tenant_id", None)
        if tenant_id:
            return Tenant.objects.filter(tenant_id=tenant_id).first()
        return None

    @classmethod
    def check_permissions(cls, user: PharmacyUser, required_action: str = "read") -> None:
        """Enforces multi-tenant RBAC policies for storage configurations."""
        if user.is_superuser:
            return

        if required_action in ["write", "delete"]:
            if getattr(user, "role", None) in [UserRole.CASHIER]:
                raise PermissionDenied({
                    "error_code": "ERR_ROLE_UNAUTHORIZED",
                    "detail": "Cashier role has read-only access to storage conditions.",
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
    def get_storage_schema(cls) -> Dict[str, Any]:
        """Returns dynamic schema and regulatory constraints for cold-chain storage."""
        return {
            "schema_version": "1.0",
            "editor_type": "STORAGE_CONDITIONAL_FORM",
            "supported_placement_zones": [
                {
                    "code": p.value,
                    "label_en": p.label,
                    "label_bn": cls._translate_placement(p.value),
                }
                for p in StoragePlacementZone
            ],
            "supported_excursion_actions": [
                {
                    "code": a.value,
                    "label_en": a.label,
                    "label_bn": cls._translate_action(a.value),
                }
                for a in ExcursionHandlingAction
            ],
            "min_allowed_temp_celsius": -50.0,
            "max_allowed_temp_celsius": 50.0,
            "max_excursion_hours_limit": 72,
            "default_cold_chain_min": 2.0,
            "default_cold_chain_max": 8.0,
        }

    @classmethod
    def validate_storage_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates conditional cold-chain storage parameters.
        When is_cold_chain_required=False, clears/omits cold chain specifics.
        When is_cold_chain_required=True, strictly validates ranges and placement.
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

        is_cold_chain = bool(payload.get("is_cold_chain_required", False))

        if not is_cold_chain:
            # Ambient product - cold chain fields inactive
            return {
                "product_id": str(product.product_id),
                "product_brand_name": product.brand_name,
                "is_cold_chain_required": False,
                "min_temperature_celsius": 15.0,
                "max_temperature_celsius": 25.0,
                "refrigerator_placement": StoragePlacementZone.SHELF_MIDDLE,
                "max_excursion_hours": 24,
                "excursion_handling_action": ExcursionHandlingAction.QUARANTINE_IMMEDIATELY,
                "requires_ice_pack_transport": False,
                "protect_from_freezing": False,
                "handling_alert_notes": str(payload.get("handling_alert_notes", "")).strip(),
                "is_active": bool(payload.get("is_active", True)),
            }

        # Cold chain is mandatory - validate specifics
        try:
            min_temp = Decimal(str(payload.get("min_temperature_celsius", "2.00")))
            max_temp = Decimal(str(payload.get("max_temperature_celsius", "8.00")))
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError({
                "error_code": "ERR_INVALID_TEMPERATURE_FORMAT",
                "detail": "Temperatures must be valid decimal numbers.",
                "field": "min_temperature_celsius",
            })

        if min_temp >= max_temp:
            raise ValidationError({
                "error_code": "ERR_INVALID_TEMPERATURE_RANGE",
                "detail": f"min_temperature_celsius ({min_temp}°C) must be strictly less than max_temperature_celsius ({max_temp}°C).",
                "field": "min_temperature_celsius",
            })

        if min_temp < Decimal("-50.0") or max_temp > Decimal("50.0"):
            raise ValidationError({
                "error_code": "ERR_TEMPERATURE_OUT_OF_BOUNDS",
                "detail": "Temperatures must be within the range of -50.0°C to 50.0°C.",
                "field": "max_temperature_celsius",
            })

        placement = payload.get("refrigerator_placement", StoragePlacementZone.SHELF_MIDDLE)
        if placement not in cls.ALLOWED_PLACEMENTS:
            raise ValidationError({
                "error_code": "ERR_INVALID_PLACEMENT_ZONE",
                "detail": f"Invalid refrigerator_placement: '{placement}'. Allowed: {cls.ALLOWED_PLACEMENTS}",
                "field": "refrigerator_placement",
            })

        if placement == StoragePlacementZone.DOOR_NOT_PERMITTED:
            raise ValidationError({
                "error_code": "ERR_PROHIBITED_DOOR_PLACEMENT",
                "detail": "Medicines and biologics cannot be assigned to refrigerator door storage due to temperature instability.",
                "field": "refrigerator_placement",
            })

        try:
            excursion_hours = int(payload.get("max_excursion_hours", 24))
            if excursion_hours < 0 or excursion_hours > 72:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_EXCURSION_HOURS",
                "detail": "max_excursion_hours must be an integer between 0 and 72 hours.",
                "field": "max_excursion_hours",
            })

        action = payload.get("excursion_handling_action", ExcursionHandlingAction.QUARANTINE_IMMEDIATELY)
        if action not in cls.ALLOWED_ACTIONS:
            raise ValidationError({
                "error_code": "ERR_INVALID_EXCURSION_ACTION",
                "detail": f"Invalid excursion_handling_action: '{action}'. Allowed: {cls.ALLOWED_ACTIONS}",
                "field": "excursion_handling_action",
            })

        return {
            "product_id": str(product.product_id),
            "product_brand_name": product.brand_name,
            "is_cold_chain_required": True,
            "min_temperature_celsius": float(min_temp),
            "max_temperature_celsius": float(max_temp),
            "refrigerator_placement": placement,
            "max_excursion_hours": excursion_hours,
            "excursion_handling_action": action,
            "requires_ice_pack_transport": bool(payload.get("requires_ice_pack_transport", True)),
            "protect_from_freezing": bool(payload.get("protect_from_freezing", True)),
            "handling_alert_notes": str(payload.get("handling_alert_notes", "")).strip(),
            "is_active": bool(payload.get("is_active", True)),
        }

    @classmethod
    @transaction.atomic
    def create_or_update_storage_config(
        cls,
        payload: Dict[str, Any],
        user: PharmacyUser,
        target_config_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Creates or updates a product-specific storage condition configuration."""
        cls.check_permissions(user, required_action="write")
        tenant: Optional[Tenant] = cls._resolve_tenant(user)

        config: Optional[StorageConditionalConfig] = None
        if target_config_id:
            try:
                config = StorageConditionalConfig.objects.select_related("tenant", "product").get(pk=target_config_id)
            except StorageConditionalConfig.DoesNotExist:
                raise ValidationError({
                    "error_code": "ERR_CONFIG_NOT_FOUND",
                    "detail": f"StorageConditionalConfig '{target_config_id}' not found.",
                })
            user_tenant_id = getattr(user, "tenant_id", None)
            if config.tenant_id and user_tenant_id and str(config.tenant_id) != str(user_tenant_id) and not user.is_superuser:
                raise PermissionDenied({
                    "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                    "detail": "Cross-tenant storage modification prohibited.",
                })

        # Merge for partial update
        merged_payload = {}
        if config:
            merged_payload = {
                "product_id": str(config.product_id),
                "is_cold_chain_required": config.is_cold_chain_required,
                "min_temperature_celsius": float(config.min_temperature_celsius),
                "max_temperature_celsius": float(config.max_temperature_celsius),
                "refrigerator_placement": config.refrigerator_placement,
                "max_excursion_hours": config.max_excursion_hours,
                "excursion_handling_action": config.excursion_handling_action,
                "requires_ice_pack_transport": config.requires_ice_pack_transport,
                "protect_from_freezing": config.protect_from_freezing,
                "handling_alert_notes": config.handling_alert_notes,
                "is_active": config.is_active,
            }
        merged_payload.update(payload)
        validated = cls.validate_storage_payload(merged_payload)

        product = MedicineProduct.objects.get(pk=validated["product_id"])

        created = False
        if not config:
            q = Q(product=product)
            if tenant:
                q &= Q(tenant=tenant)
            else:
                q &= Q(tenant__isnull=True)
            config = StorageConditionalConfig.objects.filter(q).first()
            if not config:
                config_id = payload.get("config_id") or f"stg-cfg-{uuid.uuid4().hex[:12]}"
                config = StorageConditionalConfig(
                    config_id=config_id,
                    product=product,
                    tenant=tenant,
                )
                created = True

        config.is_cold_chain_required = validated["is_cold_chain_required"]
        config.min_temperature_celsius = Decimal(str(validated["min_temperature_celsius"]))
        config.max_temperature_celsius = Decimal(str(validated["max_temperature_celsius"]))
        config.refrigerator_placement = validated["refrigerator_placement"]
        config.max_excursion_hours = validated["max_excursion_hours"]
        config.excursion_handling_action = validated["excursion_handling_action"]
        config.requires_ice_pack_transport = validated["requires_ice_pack_transport"]
        config.protect_from_freezing = validated["protect_from_freezing"]
        config.handling_alert_notes = validated["handling_alert_notes"]
        config.is_active = validated["is_active"]

        config.full_clean()
        config.save()

        # Synchronize core MedicineProduct
        product.requires_cold_chain = config.is_cold_chain_required
        product.min_temp_celsius = config.min_temperature_celsius
        product.max_temp_celsius = config.max_temperature_celsius
        product.save(update_fields=["requires_cold_chain", "min_temp_celsius", "max_temp_celsius"])

        action = "STORAGE_CONDITIONS_CREATED" if created else "STORAGE_CONDITIONS_UPDATED"
        AuditLogEntry.objects.create(
            tenant_id=getattr(tenant, "tenant_id", "PLATFORM") if tenant else "PLATFORM",
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action=action,
            resource_type="StorageConditionalConfig",
            resource_id=str(config.config_id),
            metadata_json={
                "product_id": str(product.product_id),
                "is_cold_chain": config.is_cold_chain_required,
                "temp_range": f"{config.min_temperature_celsius}C - {config.max_temperature_celsius}C",
            },
            status=AuditActionStatus.SUCCESS,
        )

        return cls._serialize_config(config)

    @classmethod
    def get_storage_config(cls, config_id: str, user: PharmacyUser) -> Dict[str, Any]:
        """Retrieves details of a specific storage condition configuration."""
        cls.check_permissions(user, required_action="read")
        try:
            config = StorageConditionalConfig.objects.select_related("product", "tenant").get(pk=config_id)
        except StorageConditionalConfig.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_CONFIG_NOT_FOUND",
                "detail": f"StorageConditionalConfig '{config_id}' not found.",
            })

        user_tenant_id = getattr(user, "tenant_id", None)
        if config.tenant_id and user_tenant_id and str(config.tenant_id) != str(user_tenant_id) and not user.is_superuser:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                "detail": "Cross-tenant storage access prohibited.",
            })

        return cls._serialize_config(config)

    @classmethod
    def list_storage_configs(
        cls,
        user: PharmacyUser,
        product_id: Optional[str] = None,
        is_cold_chain_required: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Lists storage configurations scoped to user's tenant."""
        cls.check_permissions(user, required_action="read")
        qs = StorageConditionalConfig.objects.select_related("product", "tenant").filter(is_active=True)

        user_tenant: Optional[Tenant] = cls._resolve_tenant(user)
        if not user.is_superuser:
            if user_tenant:
                qs = qs.filter(Q(tenant=user_tenant) | Q(tenant__isnull=True))
            else:
                qs = qs.filter(tenant__isnull=True)

        if product_id:
            qs = qs.filter(product_id=product_id)

        if is_cold_chain_required is not None:
            qs = qs.filter(is_cold_chain_required=is_cold_chain_required)

        return [cls._serialize_config(c) for c in qs]

    @classmethod
    @transaction.atomic
    def archive_storage_config(cls, config_id: str, user: PharmacyUser, hard_delete: bool = False) -> Dict[str, Any]:
        """Archives or soft-deletes a storage configuration."""
        cls.check_permissions(user, required_action="delete")
        try:
            config = StorageConditionalConfig.objects.get(pk=config_id)
        except StorageConditionalConfig.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_CONFIG_NOT_FOUND",
                "detail": f"StorageConditionalConfig '{config_id}' not found.",
            })

        user_tenant_id = getattr(user, "tenant_id", None)
        if config.tenant_id and user_tenant_id and str(config.tenant_id) != str(user_tenant_id) and not user.is_superuser:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                "detail": "Cross-tenant storage deletion prohibited.",
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
            action="STORAGE_CONDITIONS_REMOVED",
            resource_type="StorageConditionalConfig",
            resource_id=config_id,
            metadata_json={"status": status_msg},
            status=AuditActionStatus.SUCCESS,
        )

        return {"status": status_msg, "config_id": config_id}

    @classmethod
    def _serialize_config(cls, config: StorageConditionalConfig) -> Dict[str, Any]:
        """Converts model instance to JSON dictionary."""
        return {
            "config_id": str(config.config_id),
            "product_id": str(config.product_id),
            "product_brand_name": config.product.brand_name if config.product else "",
            "tenant_id": str(config.tenant_id) if config.tenant_id else None,
            "is_cold_chain_required": config.is_cold_chain_required,
            "min_temperature_celsius": float(config.min_temperature_celsius),
            "max_temperature_celsius": float(config.max_temperature_celsius),
            "refrigerator_placement": config.refrigerator_placement,
            "refrigerator_placement_bn": cls._translate_placement(config.refrigerator_placement),
            "max_excursion_hours": config.max_excursion_hours,
            "excursion_handling_action": config.excursion_handling_action,
            "excursion_handling_action_bn": cls._translate_action(config.excursion_handling_action),
            "requires_ice_pack_transport": config.requires_ice_pack_transport,
            "protect_from_freezing": config.protect_from_freezing,
            "handling_alert_notes": config.handling_alert_notes,
            "is_active": config.is_active,
            "created_at": config.created_at.isoformat(),
            "updated_at": config.updated_at.isoformat(),
        }

    @staticmethod
    def _translate_placement(code: str) -> str:
        translations = {
            "SHELF_MIDDLE": "মাঝের তাক (স্থির তাপমাত্রা)",
            "SHELF_TOP": "উপরের তাক",
            "SHELF_BOTTOM": "নিচের তাক (ক্রিসপারের উপরে)",
            "CHILLER_TRAY": "চিলার ট্রে (২°সে - ৪°সে)",
            "DOOR_NOT_PERMITTED": "দরজায় রাখা কঠোরভাবে নিষিদ্ধ",
        }
        return translations.get(code, code)

    @staticmethod
    def _translate_action(code: str) -> str:
        translations = {
            "QUARANTINE_IMMEDIATELY": "তাত্ক্ষণিক কোয়ারেন্টাইন করুন",
            "RETURN_TO_SUPPLIER": "কোল্ড-চেইন সরবরাহকারীকে ফেরত পাঠান",
            "DISPOSE_BIOHAZARD": "নিরাপদ বায়োহ্যাজার্ড বিনষ্টকরণ",
            "NOTIFY_SUPERVISOR": "প্রধান ফার্মাসিস্টকে অবহিত করুন",
        }
        return translations.get(code, code)
