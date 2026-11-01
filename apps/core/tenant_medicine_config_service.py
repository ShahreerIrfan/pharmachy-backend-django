"""
backend/apps/core/tenant_medicine_config_service.py
Architecture Decision: ADR-0004, ADR-0005, ADR-0070 & ADR-0071
Tenant-Specific Medicine Configuration & Local Overrides Service.
Manages store-level brand aliases, internal SKUs, custom retail MRPs,
shelf locations, reorder thresholds, and visibility overrides while
strictly maintaining global master integrity and DGDA regulatory compliance.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.core.exceptions import ValidationError
from django.db import models, transaction

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import MedicineProduct, Tenant, TenantMedicineConfig
from apps.identity.models import PharmacyUser


class TenantMedicineConfigService:
    """
    Central business service for tenant medicine configuration & local overrides.
    """

    @classmethod
    def serialize_config(cls, config: TenantMedicineConfig) -> Dict[str, Any]:
        """Formats TenantMedicineConfig according to JSON Schema contract."""
        return {
            "config_id": config.config_id,
            "tenant_id": config.tenant_id,
            "product_id": config.product_id,
            "local_brand_name": config.local_brand_name or "",
            "local_sku": config.local_sku or "",
            "custom_mrp_unit": str(config.custom_mrp_unit) if config.custom_mrp_unit is not None else None,
            "custom_cost_unit": str(config.custom_cost_unit) if config.custom_cost_unit is not None else None,
            "is_visible": config.is_visible,
            "shelf_location": config.shelf_location or "",
            "low_stock_threshold": config.low_stock_threshold,
            "requires_prescription_override": config.requires_prescription_override,
            "notes": config.notes or "",
            "effective_brand_name": config.effective_brand_name,
            "effective_mrp_unit": str(config.effective_mrp_unit),
            "effective_cost_unit": str(config.effective_cost_unit),
            "effective_requires_prescription": config.effective_requires_prescription,
            "created_at": config.created_at.isoformat() if config.created_at else None,
            "updated_at": config.updated_at.isoformat() if config.updated_at else None,
        }

    @classmethod
    @transaction.atomic
    def upsert_config(
        cls,
        tenant_id: str,
        product_id: str,
        data: Dict[str, Any],
        actor: Optional[PharmacyUser] = None,
    ) -> Dict[str, Any]:
        """
        Creates or updates tenant-specific configuration for a master medicine.
        Preserves global master product immutability.
        Strictly prevents relaxing DGDA prescription requirements.
        """
        clean_tenant_id = str(tenant_id or "").strip()
        clean_product_id = str(product_id or "").strip()

        tenant = Tenant.objects.filter(tenant_id=clean_tenant_id).first()
        if not tenant:
            raise ValidationError({"tenant_id": f"Pharmacy tenant '{clean_tenant_id}' does not exist."})

        product = MedicineProduct.objects.filter(product_id=clean_product_id).first()
        if not product:
            raise ValidationError({"product_id": f"Master medicine product '{clean_product_id}' does not exist."})

        # Regulatory Invariant: Cannot relax DGDA prescription mandate!
        req_rx_override = data.get("requires_prescription_override")
        if req_rx_override is False:
            if product.is_prescription_only:
                raise ValidationError(
                    {
                        "requires_prescription_override": (
                            "Cannot relax DGDA regulatory prescription requirement. "
                            "Global master product is classified as Prescription-Only (POM)."
                        )
                    }
                )
            if product.is_narcotic or product.is_antibiotic:
                raise ValidationError(
                    {
                        "requires_prescription_override": (
                            "Cannot relax DGDA regulatory verification mandate for scheduled narcotics or antibiotics."
                        )
                    }
                )

        # Parse & validate monetary fields
        custom_mrp = data.get("custom_mrp_unit")
        parsed_mrp = None
        if custom_mrp is not None and str(custom_mrp).strip() != "":
            try:
                parsed_mrp = Decimal(str(custom_mrp))
                if parsed_mrp < Decimal("0.01"):
                    raise ValidationError({"custom_mrp_unit": "Custom MRP must be at least 0.01 BDT."})
            except Exception as e:
                if isinstance(e, ValidationError):
                    raise
                raise ValidationError({"custom_mrp_unit": "Invalid custom MRP unit decimal value."})

        custom_cost = data.get("custom_cost_unit")
        parsed_cost = None
        if custom_cost is not None and str(custom_cost).strip() != "":
            try:
                parsed_cost = Decimal(str(custom_cost))
                if parsed_cost < Decimal("0.00"):
                    raise ValidationError({"custom_cost_unit": "Custom cost must be non-negative."})
            except Exception as e:
                if isinstance(e, ValidationError):
                    raise
                raise ValidationError({"custom_cost_unit": "Invalid custom cost unit decimal value."})

        low_stock = data.get("low_stock_threshold", 10)
        try:
            low_stock_int = int(low_stock)
            if low_stock_int < 0:
                raise ValidationError({"low_stock_threshold": "Low stock threshold cannot be negative."})
        except (ValueError, TypeError):
            raise ValidationError({"low_stock_threshold": "Low stock threshold must be an integer."})

        config = TenantMedicineConfig.objects.filter(tenant=tenant, product=product).first()
        action_type = "TENANT_MEDICINE_CONFIG_UPDATED" if config else "TENANT_MEDICINE_CONFIG_CREATED"

        if not config:
            config = TenantMedicineConfig(
                config_id=f"tmc-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                product=product,
            )

        if "local_brand_name" in data:
            config.local_brand_name = str(data["local_brand_name"] or "").strip()
        if "local_sku" in data:
            config.local_sku = str(data["local_sku"] or "").strip()
        if "custom_mrp_unit" in data:
            config.custom_mrp_unit = parsed_mrp
        if "custom_cost_unit" in data:
            config.custom_cost_unit = parsed_cost
        if "is_visible" in data:
            config.is_visible = bool(data["is_visible"])
        if "shelf_location" in data:
            config.shelf_location = str(data["shelf_location"] or "").strip()
        if "low_stock_threshold" in data:
            config.low_stock_threshold = low_stock_int
        if "requires_prescription_override" in data:
            config.requires_prescription_override = req_rx_override
        if "notes" in data:
            config.notes = str(data["notes"] or "").strip()

        config.full_clean()
        config.save()

        actor_email = actor.email if actor else "system"
        AuditLogEntry.objects.create(
            action=action_type,
            resource_type="TENANT_MEDICINE_CONFIG",
            resource_id=config.config_id,
            tenant_id=tenant.tenant_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor_email,
                "product_id": product.product_id,
                "local_brand_name": config.local_brand_name,
                "local_sku": config.local_sku,
                "effective_mrp": str(config.effective_mrp_unit),
                "is_visible": config.is_visible,
            },
        )

        return cls.serialize_config(config)

    @classmethod
    @transaction.atomic
    def delete_config(
        cls,
        tenant_id: str,
        config_id_or_product_id: str,
        actor: Optional[PharmacyUser] = None,
    ) -> bool:
        """
        Deletes tenant medicine configuration, cleanly resetting effective values to global master defaults.
        """
        clean_tenant = str(tenant_id or "").strip()
        clean_key = str(config_id_or_product_id or "").strip()

        config = (
            TenantMedicineConfig.objects.filter(
                tenant_id=clean_tenant,
            )
            .filter(config_id=clean_key)
            .first()
        )

        if not config:
            config = TenantMedicineConfig.objects.filter(
                tenant_id=clean_tenant,
                product_id=clean_key,
            ).first()

        if not config:
            return False

        config_id = config.config_id
        product_id = config.product_id
        config.delete()

        actor_email = actor.email if actor else "system"
        AuditLogEntry.objects.create(
            action="TENANT_MEDICINE_CONFIG_DELETED",
            resource_type="TENANT_MEDICINE_CONFIG",
            resource_id=config_id,
            tenant_id=clean_tenant,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor_email,
                "product_id": product_id,
                "notes": "Reset to central master medicine defaults.",
            },
        )
        return True

    @classmethod
    def get_config(cls, tenant_id: str, product_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves tenant configuration for a specific master medicine."""
        config = TenantMedicineConfig.objects.filter(
            tenant_id=str(tenant_id).strip(),
            product_id=str(product_id).strip(),
        ).first()
        if not config:
            return None
        return cls.serialize_config(config)

    @classmethod
    def list_configs(
        cls,
        tenant_id: str,
        is_visible: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists configurations for a tenant with optional search and visibility filters."""
        qs = TenantMedicineConfig.objects.filter(tenant_id=str(tenant_id).strip()).select_related("product")
        if is_visible is not None:
            qs = qs.filter(is_visible=is_visible)
        if search:
            clean_search = str(search).strip()
            qs = qs.filter(
                models.Q(local_brand_name__icontains=clean_search)
                | models.Q(local_sku__icontains=clean_search)
                | models.Q(product__brand_name__icontains=clean_search)
                | models.Q(product__generic_name__icontains=clean_search)
            )
        return [cls.serialize_config(c) for c in qs]
