"""
backend/apps/core/override_precedence_service.py
Architecture Decision: ADR-0128 (P128).
Global and Tenant Override Precedence Engine, Moderation Rules & Regulatory Guardrails.
Resolves effective medicine presentation combining canonical Global Master defaults
with store-specific approved overrides, enforces statutory anti-gouging thresholds,
prohibits relaxing DGDA prescription mandates, and maintains immutable audit provenance.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    MedicineProduct,
    MedicineVariant,
    ModerationStatus,
    Tenant,
    TenantMedicineConfig,
)
from apps.identity.models import PharmacyUser


class GlobalTenantOverridePrecedenceService:
    """
    Central service for resolving global medicine values vs tenant overrides,
    moderating price changes, and enforcing pharmaceutical regulatory constraints.
    """

    @classmethod
    def resolve_medicine(cls, tenant_id: str, product_id: str) -> Dict[str, Any]:
        """
        Resolves effective attributes of a medicine for a specific tenant,
        generating transparent field-by-field provenance tracing.
        """
        clean_tenant = str(tenant_id or "").strip()
        clean_product = str(product_id or "").strip()

        product = MedicineProduct.objects.filter(product_id=clean_product).first()
        if not product:
            raise ValidationError({"product_id": f"Global master product '{clean_product}' does not exist."})

        config = (
            TenantMedicineConfig.objects.filter(
                tenant_id=clean_tenant,
                product_id=clean_product,
            )
            .select_related("tenant", "product")
            .first()
        )

        provenance: Dict[str, Any] = {}

        # 1. Brand Name Precedence
        if (
            config
            and config.local_brand_name
            and config.local_brand_name.strip()
            and config.moderation_status == ModerationStatus.APPROVED
        ):
            effective_brand = config.local_brand_name.strip()
            provenance["brand_name"] = {
                "value": effective_brand,
                "source": "TENANT_OVERRIDE",
                "rule": "ACTIVE_TENANT_OVERRIDE",
            }
        else:
            effective_brand = product.brand_name
            provenance["brand_name"] = {
                "value": effective_brand,
                "source": "GLOBAL_MASTER",
                "rule": "MASTER_DEFAULT",
            }

        # 2. MRP Precedence & Moderation Fallback
        if config and config.custom_mrp_unit is not None:
            if config.moderation_status == ModerationStatus.APPROVED:
                effective_mrp = str(config.custom_mrp_unit)
                provenance["mrp_unit"] = {
                    "value": effective_mrp,
                    "source": "TENANT_OVERRIDE",
                    "rule": "ACTIVE_TENANT_OVERRIDE",
                }
            elif config.moderation_status == ModerationStatus.PENDING_REVIEW:
                effective_mrp = str(product.mrp_unit)
                provenance["mrp_unit"] = {
                    "value": effective_mrp,
                    "source": "GLOBAL_MASTER",
                    "rule": "MODERATION_PENDING_FALLBACK",
                }
            else:  # REJECTED
                effective_mrp = str(product.mrp_unit)
                provenance["mrp_unit"] = {
                    "value": effective_mrp,
                    "source": "GLOBAL_MASTER",
                    "rule": "MODERATION_REJECTED_FALLBACK",
                }
        else:
            effective_mrp = str(product.mrp_unit)
            provenance["mrp_unit"] = {
                "value": effective_mrp,
                "source": "GLOBAL_MASTER",
                "rule": "MASTER_DEFAULT",
            }

        # 3. Cost Precedence
        if config and config.custom_cost_unit is not None:
            if config.moderation_status == ModerationStatus.APPROVED:
                effective_cost = str(config.custom_cost_unit)
                provenance["cost_unit"] = {
                    "value": effective_cost,
                    "source": "TENANT_OVERRIDE",
                    "rule": "ACTIVE_TENANT_OVERRIDE",
                }
            elif config.moderation_status == ModerationStatus.PENDING_REVIEW:
                effective_cost = str(product.cost_unit)
                provenance["cost_unit"] = {
                    "value": effective_cost,
                    "source": "GLOBAL_MASTER",
                    "rule": "MODERATION_PENDING_FALLBACK",
                }
            else:
                effective_cost = str(product.cost_unit)
                provenance["cost_unit"] = {
                    "value": effective_cost,
                    "source": "GLOBAL_MASTER",
                    "rule": "MODERATION_REJECTED_FALLBACK",
                }
        else:
            effective_cost = str(product.cost_unit)
            provenance["cost_unit"] = {
                "value": effective_cost,
                "source": "GLOBAL_MASTER",
                "rule": "MASTER_DEFAULT",
            }

        # 4. Prescription Safety Invariant
        if product.is_prescription_only:
            effective_rx = True
            provenance["is_prescription_required"] = {
                "value": True,
                "source": "REGULATORY_MANDATE",
                "rule": "DGDA_POM_MANDATE",
            }
        elif product.is_narcotic:
            effective_rx = True
            provenance["is_prescription_required"] = {
                "value": True,
                "source": "REGULATORY_MANDATE",
                "rule": "DGDA_NARCOTIC_MANDATE",
            }
        elif product.is_antibiotic:
            effective_rx = True
            provenance["is_prescription_required"] = {
                "value": True,
                "source": "REGULATORY_MANDATE",
                "rule": "DGDA_ANTIBIOTIC_MANDATE",
            }
        elif (
            config
            and config.requires_prescription_override is True
            and config.moderation_status == ModerationStatus.APPROVED
        ):
            effective_rx = True
            provenance["is_prescription_required"] = {
                "value": True,
                "source": "TENANT_OVERRIDE",
                "rule": "ACTIVE_TENANT_OVERRIDE",
            }
        else:
            effective_rx = False
            provenance["is_prescription_required"] = {
                "value": False,
                "source": "GLOBAL_MASTER",
                "rule": "MASTER_DEFAULT",
            }

        # 5. Visibility Precedence
        if config and not config.is_visible:
            effective_visible = False
            provenance["is_visible"] = {
                "value": False,
                "source": "TENANT_OVERRIDE",
                "rule": "ACTIVE_TENANT_OVERRIDE",
            }
        else:
            effective_visible = True
            provenance["is_visible"] = {
                "value": True,
                "source": "GLOBAL_MASTER",
                "rule": "MASTER_DEFAULT",
            }

        # 6. Physical Shelf Location
        if config and config.shelf_location:
            effective_shelf = config.shelf_location
            provenance["shelf_location"] = {
                "value": effective_shelf,
                "source": "TENANT_OVERRIDE",
                "rule": "ACTIVE_TENANT_OVERRIDE",
            }
        else:
            effective_shelf = ""
            provenance["shelf_location"] = {
                "value": "",
                "source": "GLOBAL_MASTER",
                "rule": "MASTER_DEFAULT",
            }

        # 7. Low Stock Reorder Threshold
        if config and config.low_stock_threshold is not None:
            effective_threshold = config.low_stock_threshold
            provenance["low_stock_threshold"] = {
                "value": effective_threshold,
                "source": "TENANT_OVERRIDE",
                "rule": "ACTIVE_TENANT_OVERRIDE",
            }
        else:
            effective_threshold = 10
            provenance["low_stock_threshold"] = {
                "value": 10,
                "source": "GLOBAL_MASTER",
                "rule": "MASTER_DEFAULT",
            }

        return {
            "product_id": product.product_id,
            "tenant_id": clean_tenant,
            "generic_name": product.generic_name,
            "dosage_form": product.dosage_form,
            "strength": product.strength,
            "manufacturer": product.manufacturer,
            "route": product.route,
            "barcode": product.barcode,
            "effective_brand_name": effective_brand,
            "effective_mrp_unit": effective_mrp,
            "effective_cost_unit": effective_cost,
            "effective_requires_prescription": effective_rx,
            "effective_is_visible": effective_visible,
            "effective_shelf_location": effective_shelf,
            "effective_low_stock_threshold": effective_threshold,
            "is_overridden": config is not None,
            "override_id": config.config_id if config else None,
            "moderation_status": config.moderation_status if config else "NOT_APPLICABLE",
            "moderation_reason": config.moderation_reason if config else "",
            "provenance": provenance,
        }

    @classmethod
    def resolve_variant(cls, tenant_id: str, variant_id: str) -> Dict[str, Any]:
        """
        Resolves effective attributes for a commercial variant presentation SKU.
        """
        clean_tenant = str(tenant_id or "").strip()
        clean_variant = str(variant_id or "").strip()

        variant = MedicineVariant.objects.filter(variant_id=clean_variant).select_related("product").first()
        if not variant:
            raise ValidationError({"variant_id": f"Commercial variant SKU '{clean_variant}' does not exist."})

        # Inherit parent product resolution
        resolved_parent = cls.resolve_medicine(tenant_id=clean_tenant, product_id=variant.product_id)

        # Variant-specific overrides
        config = (
            TenantMedicineConfig.objects.filter(
                tenant_id=clean_tenant,
                variant=variant,
            )
            .first()
        )

        return {
            "variant_id": variant.variant_id,
            "sku": variant.sku,
            "variant_name": variant.variant_name,
            "pack_size": variant.pack_size,
            "pack_size_numeric": variant.pack_size_numeric,
            "container_type": variant.container_type,
            "barcode": variant.barcode,
            "mrp_pack": str(variant.mrp_pack),
            "cost_pack": str(variant.cost_pack),
            "parent_product": resolved_parent,
            "is_variant_overridden": config is not None,
            "variant_moderation_status": config.moderation_status if config else "NOT_APPLICABLE",
        }

    @classmethod
    @transaction.atomic
    def upsert_override(
        cls,
        tenant_id: str,
        product_id: str,
        data: Dict[str, Any],
        actor: Optional[PharmacyUser] = None,
        variant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Creates or updates a tenant override configuration under explicit moderation
        and regulatory non-relaxation rules.
        """
        clean_tenant = str(tenant_id or "").strip()
        clean_product = str(product_id or "").strip()

        # 1. Tenant Verification & Subscription Entitlement Check
        tenant = Tenant.objects.filter(tenant_id=clean_tenant).first()
        if not tenant:
            raise ValidationError({"tenant_id": f"Pharmacy tenant '{clean_tenant}' does not exist."})

        tenant_status = str(getattr(tenant, "status", "ACTIVE")).upper()
        if tenant_status == "SUSPENDED":
            raise PermissionDenied("ERR_TENANT_SUSPENDED: Suspended tenant cannot modify catalog overrides.")
        if tenant_status in ("ARCHIVED", "EXPIRED", "CANCELLED", "DELINQUENT"):
            raise PermissionDenied(
                "ERR_SUBSCRIPTION_FEATURE_LOCKED: Custom medicine catalog overrides require an active subscription plan."
            )

        # 2. Master Product Verification
        product = MedicineProduct.objects.filter(product_id=clean_product).first()
        if not product:
            raise ValidationError({"product_id": f"Master product '{clean_product}' does not exist."})

        variant = None
        if variant_id:
            variant = MedicineVariant.objects.filter(variant_id=str(variant_id).strip()).first()

        # 3. Regulatory Non-Relaxation Check
        rx_override = data.get("requires_prescription_override")
        if rx_override is False:
            if product.is_prescription_only:
                raise ValidationError(
                    {
                        "requires_prescription_override": (
                            "ERR_REGULATORY_RELAXATION_FORBIDDEN: Cannot relax DGDA regulatory prescription requirement. "
                            "Global master product is classified as Prescription-Only (POM)."
                        )
                    }
                )
            if product.is_narcotic or product.is_antibiotic:
                raise ValidationError(
                    {
                        "requires_prescription_override": (
                            "ERR_REGULATORY_RELAXATION_FORBIDDEN: Cannot relax statutory dispensing verification "
                            "for scheduled narcotics or antibiotics."
                        )
                    }
                )

        # 4. Validate & Parse Pricing Fields
        parsed_mrp = None
        custom_mrp = data.get("custom_mrp_unit")
        if custom_mrp is not None and str(custom_mrp).strip() != "":
            try:
                parsed_mrp = Decimal(str(custom_mrp))
                if parsed_mrp < Decimal("0.01"):
                    raise ValidationError({"custom_mrp_unit": "Custom MRP must be at least 0.01 BDT."})
            except InvalidOperation:
                raise ValidationError({"custom_mrp_unit": "Invalid custom MRP unit decimal value."})

        parsed_cost = None
        custom_cost = data.get("custom_cost_unit")
        if custom_cost is not None and str(custom_cost).strip() != "":
            try:
                parsed_cost = Decimal(str(custom_cost))
                if parsed_cost < Decimal("0.00"):
                    raise ValidationError({"custom_cost_unit": "Custom cost must be non-negative."})
            except InvalidOperation:
                raise ValidationError({"custom_cost_unit": "Invalid custom cost unit decimal value."})

        # 5. Anti-Gouging Moderation Rule (+50% ceiling threshold)
        moderation_status = ModerationStatus.APPROVED
        moderation_reason = "Auto-approved within standard pricing and customization bounds."

        if parsed_mrp is not None:
            max_permitted_mrp = product.mrp_unit * Decimal("1.50")
            if parsed_mrp > max_permitted_mrp:
                moderation_status = ModerationStatus.PENDING_REVIEW
                moderation_reason = (
                    f"Custom retail price {parsed_mrp} BDT exceeds statutory/fair threshold "
                    f"(+50% of global MRP {product.mrp_unit} BDT). Placed in PENDING_REVIEW."
                )

        # 6. Retrieve or Instantiate Configuration
        config = (
            TenantMedicineConfig.objects.filter(
                tenant=tenant,
                product=product,
            )
            .first()
        )
        is_created = config is None

        if not config:
            config = TenantMedicineConfig(
                config_id=f"tmc-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                product=product,
                variant=variant,
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
            try:
                config.low_stock_threshold = max(0, int(data["low_stock_threshold"]))
            except (ValueError, TypeError):
                raise ValidationError({"low_stock_threshold": "Low stock threshold must be a valid integer."})
        if "requires_prescription_override" in data:
            config.requires_prescription_override = rx_override
        if "notes" in data:
            config.notes = str(data["notes"] or "").strip()

        config.moderation_status = moderation_status
        config.moderation_reason = moderation_reason
        config.full_clean()
        config.save()

        # 7. Audit Log Trail
        actor_email = actor.email if actor else "system"
        action_name = "TENANT_OVERRIDE_CREATED" if is_created else "TENANT_OVERRIDE_UPDATED"
        AuditLogEntry.objects.create(
            action=action_name,
            resource_type="TENANT_MEDICINE_CONFIG",
            resource_id=config.config_id,
            tenant_id=clean_tenant,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor_email,
                "product_id": clean_product,
                "local_brand_name": config.local_brand_name,
                "custom_mrp_unit": str(config.custom_mrp_unit) if config.custom_mrp_unit else None,
                "moderation_status": config.moderation_status,
                "moderation_reason": config.moderation_reason,
            },
        )

        return cls.resolve_medicine(tenant_id=clean_tenant, product_id=clean_product)

    @classmethod
    @transaction.atomic
    def moderate_override(
        cls,
        config_id: str,
        action: str,
        reason: str,
        actor: PharmacyUser,
    ) -> Dict[str, Any]:
        """
        Platform moderator reviews a pending or flagged tenant override.
        Allows approving or rejecting price adjustments.
        """
        clean_action = str(action or "").upper().strip()
        if clean_action not in ("APPROVE", "REJECT"):
            raise ValidationError({"action": "Moderation action must be either 'APPROVE' or 'REJECT'."})

        clean_reason = str(reason or "").strip()
        if len(clean_reason) < 3:
            raise ValidationError({"reason": "Moderation justification reason must be at least 3 characters."})

        config = TenantMedicineConfig.objects.filter(config_id=str(config_id).strip()).first()
        if not config:
            raise ValidationError({"config_id": f"Tenant medicine override '{config_id}' not found."})

        if clean_action == "APPROVE":
            config.moderation_status = ModerationStatus.APPROVED
            config.moderation_reason = f"Approved by platform moderator: {clean_reason}"
        else:
            config.moderation_status = ModerationStatus.REJECTED
            config.moderation_reason = f"Rejected by platform moderator: {clean_reason}"

        config.moderated_by = actor
        config.moderated_at = timezone.now()
        config.save()

        AuditLogEntry.objects.create(
            action=f"TENANT_OVERRIDE_MODERATED_{clean_action}",
            resource_type="TENANT_MEDICINE_CONFIG",
            resource_id=config.config_id,
            tenant_id=config.tenant_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "moderator": actor.email,
                "action": clean_action,
                "reason": clean_reason,
                "product_id": config.product_id,
                "effective_mrp": str(config.effective_mrp_unit),
            },
        )

        return cls.resolve_medicine(tenant_id=config.tenant_id, product_id=config.product_id)

    @classmethod
    @transaction.atomic
    def reset_override(
        cls,
        tenant_id: str,
        product_id: str,
        actor: Optional[PharmacyUser] = None,
    ) -> Dict[str, Any]:
        """
        Resets tenant override back to global master defaults.
        """
        clean_tenant = str(tenant_id or "").strip()
        clean_product = str(product_id or "").strip()

        config = TenantMedicineConfig.objects.filter(
            tenant_id=clean_tenant,
            product_id=clean_product,
        ).first()

        if config:
            config_id = config.config_id
            config.delete()
            AuditLogEntry.objects.create(
                action="TENANT_OVERRIDE_RESET_TO_MASTER",
                resource_type="TENANT_MEDICINE_CONFIG",
                resource_id=config_id,
                tenant_id=clean_tenant,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "actor": actor.email if actor else "system",
                    "product_id": clean_product,
                    "notes": "Reset to central global master values.",
                },
            )

        return cls.resolve_medicine(tenant_id=clean_tenant, product_id=clean_product)

    @classmethod
    def get_audit_history(cls, tenant_id: str, product_id: str) -> List[Dict[str, Any]]:
        """
        Retrieves immutable audit history for this medicine's override lifecycle.
        """
        clean_tenant = str(tenant_id or "").strip()
        clean_product = str(product_id or "").strip()

        logs = AuditLogEntry.objects.filter(
            tenant_id=clean_tenant,
            resource_type="TENANT_MEDICINE_CONFIG",
        ).order_by("-timestamp")[:20]

        results = []
        for log in logs:
            meta = log.metadata_json or {}
            if meta.get("product_id") == clean_product or not meta.get("product_id"):
                results.append(
                    {
                        "action": log.action,
                        "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                        "status": log.status,
                        "actor": meta.get("actor") or meta.get("moderator") or "system",
                        "metadata": meta,
                    }
                )
        return results
