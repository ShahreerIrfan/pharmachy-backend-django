"""
backend/apps/core/barcode_identifier_service.py
Architecture Decision: ADR-0136 (P136).
Service Layer for Barcode & Alternate Identifiers:
Primary, Secondary, and Alternate GTIN/EAN/Store Codes across Packaging Tiers,
Contextual Uniqueness, and Scan Prioritization Resolution.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BarcodeAlternateIdentifier,
    BarcodeIdentifierType,
    BarcodePackagingLevel,
    MedicineProduct,
    MedicineVariant,
    NonMedicineRetailItem,
    Tenant,
)
from apps.identity.models import PharmacyUser

logger = logging.getLogger("pharmacy.core.barcode_identifier")


class BarcodeIdentifierService:
    """
    Business service governing primary, secondary, and alternate barcodes across packaging levels
    (UNIT, STRIP, BOX, SHIPPER_CARTON), enforcing contextual uniqueness, and powering POS scan prioritization.
    """

    @classmethod
    def serialize_identifier(cls, record: BarcodeAlternateIdentifier) -> Dict[str, Any]:
        """Converts BarcodeAlternateIdentifier to JSON Schema compliant dictionary."""
        target_entity_type = "MEDICINE"
        target_name = ""
        resolved_mrp = "0.00"

        if record.product_id:
            target_entity_type = "MEDICINE"
            target_name = record.product.brand_name if record.product else ""
            resolved_mrp = str(record.product.mrp_unit) if record.product else "0.00"
        elif record.non_medicine_item_id:
            target_entity_type = "NON_MEDICINE"
            target_name = record.non_medicine_item.name if record.non_medicine_item else ""
            resolved_mrp = str(record.non_medicine_item.mrp) if record.non_medicine_item else "0.00"
        elif record.variant_id:
            target_entity_type = "VARIANT"
            target_name = record.variant.variant_name if record.variant else ""
            resolved_mrp = str(record.variant.mrp_unit) if record.variant else "0.00"

        return {
            "identifier_id": record.identifier_id,
            "tenant_id": record.tenant_id_str,
            "product_id": record.product_id,
            "non_medicine_item_id": record.non_medicine_item_id,
            "variant_id": record.variant_id,
            "target_entity_type": target_entity_type,
            "target_name": target_name,
            "resolved_mrp": resolved_mrp,
            "barcode_value": record.barcode_value,
            "identifier_type": record.identifier_type,
            "packaging_level": record.packaging_level,
            "priority_order": record.priority_order,
            "pack_quantity_multiplier": record.pack_quantity_multiplier,
            "description": record.description or "",
            "is_active": record.isActive if hasattr(record, "isActive") else record.is_active,
            "version_number": record.version_number,
            "immutable_hash": record.immutable_hash,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        }

    @classmethod
    @transaction.atomic
    def register_barcode(
        cls,
        barcode_value: str,
        identifier_type: str,
        packaging_level: str,
        product_id: Optional[str] = None,
        non_medicine_item_id: Optional[str] = None,
        variant_id: Optional[str] = None,
        priority_order: Optional[int] = None,
        pack_quantity_multiplier: Optional[int] = None,
        description: str = "",
        actor: Optional[PharmacyUser] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Registers a new barcode identifier with contextual uniqueness checks."""
        clean_code = str(barcode_value or "").strip()
        if len(clean_code) < 3:
            raise ValidationError({"barcode_value": "Barcode value must be at least 3 characters long."})

        clean_type = str(identifier_type or "").strip().upper()
        if clean_type not in BarcodeIdentifierType.values:
            raise ValidationError(
                {"identifier_type": f"Invalid identifier_type '{clean_type}'. Must be one of {BarcodeIdentifierType.values}."}
            )

        clean_level = str(packaging_level or "").strip().upper()
        if clean_level not in BarcodePackagingLevel.values:
            raise ValidationError(
                {"packaging_level": f"Invalid packaging_level '{clean_level}'. Must be one of {BarcodePackagingLevel.values}."}
            )

        # Resolve target
        product = None
        non_medicine_item = None
        variant = None

        if product_id:
            product = MedicineProduct.objects.filter(product_id=str(product_id).strip()).first()
            if not product:
                raise ValidationError({"product_id": f"Medicine product '{product_id}' not found."})
        elif non_medicine_item_id:
            non_medicine_item = NonMedicineRetailItem.objects.filter(item_id=str(non_medicine_item_id).strip()).first()
            if not non_medicine_item:
                raise ValidationError({"non_medicine_item_id": f"Retail item '{non_medicine_item_id}' not found."})
        elif variant_id:
            variant = MedicineVariant.objects.filter(variant_id=str(variant_id).strip()).first()
            if not variant:
                raise ValidationError({"variant_id": f"Medicine variant '{variant_id}' not found."})
        else:
            raise ValidationError(
                {"target": "Must specify at least one target: product_id, non_medicine_item_id, or variant_id."}
            )

        is_superuser = bool(getattr(actor, "is_superuser", False) or str(getattr(actor, "role", "")).upper() == "SUPERADMIN")
        effective_tenant = tenant_id or (getattr(actor, "tenant_id", None) if not is_superuser else "platform") or "platform"

        tenant_obj = None
        if effective_tenant != "platform":
            tenant_obj = Tenant.objects.filter(tenant_id=effective_tenant).first()

        # Contextual Uniqueness check across active barcodes for this tenant
        existing = (
            BarcodeAlternateIdentifier.objects.filter(
                Q(tenant_id_str=effective_tenant) | Q(tenant_id_str="platform"),
                barcode_value__iexact=clean_code,
                is_active=True,
            )
            .first()
        )
        if existing:
            # Check if assigned to a different product
            same_target = (
                (product and existing.product_id == product.product_id)
                or (non_medicine_item and existing.non_medicine_item_id == non_medicine_item.item_id)
                or (variant and existing.variant_id == variant.variant_id)
            )
            if not same_target:
                raise ValidationError(
                    {
                        "barcode_value": (
                            f"ERR_BARCODE_ALREADY_ASSIGNED: Barcode '{clean_code}' is already active and assigned to "
                            f"another item in tenant context '{effective_tenant}'."
                        )
                    }
                )

        # Default multiplier based on packaging level
        if pack_quantity_multiplier and int(pack_quantity_multiplier) >= 1:
            multiplier = int(pack_quantity_multiplier)
        else:
            if clean_level == BarcodePackagingLevel.UNIT:
                multiplier = 1
            elif clean_level == BarcodePackagingLevel.STRIP:
                multiplier = 10
            elif clean_level == BarcodePackagingLevel.BOX:
                multiplier = 100
            elif clean_level == BarcodePackagingLevel.SHIPPER_CARTON:
                multiplier = 1000
            else:
                multiplier = 1

        # Default priority order
        if priority_order and int(priority_order) >= 1:
            p_order = int(priority_order)
        else:
            if clean_type in (BarcodeIdentifierType.PRIMARY_EAN13_GTIN, BarcodeIdentifierType.INTERNAL_STORE_BARCODE):
                p_order = 1
            elif clean_level == BarcodePackagingLevel.STRIP:
                p_order = 2
            elif clean_level == BarcodePackagingLevel.BOX:
                p_order = 3
            else:
                p_order = 4

        identifier_id = f"bar-{uuid.uuid4().hex[:12]}"

        record = BarcodeAlternateIdentifier.objects.create(
            identifier_id=identifier_id,
            tenant=tenant_obj,
            tenant_id_str=effective_tenant,
            product=product,
            non_medicine_item=non_medicine_item,
            variant=variant,
            barcode_value=clean_code,
            identifier_type=clean_type,
            packaging_level=clean_level,
            priority_order=p_order,
            pack_quantity_multiplier=multiplier,
            description=str(description or "").strip(),
            is_active=True,
            version_number=1,
            created_by=actor,
        )

        # Update primary barcode on target if primary
        if clean_type == BarcodeIdentifierType.PRIMARY_EAN13_GTIN:
            if product and not product.barcode:
                product.barcode = clean_code
                product.save(update_fields=["barcode"])
            elif non_medicine_item and not non_medicine_item.barcode:
                non_medicine_item.barcode = clean_code
                non_medicine_item.save(update_fields=["barcode"])
            elif variant and not variant.barcode:
                variant.barcode = clean_code
                variant.save(update_fields=["barcode"])

        AuditLogEntry.objects.create(
            action="BARCODE_IDENTIFIER_REGISTERED",
            resource_type="BARCODE",
            resource_id=record.identifier_id,
            tenant_id=effective_tenant,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "barcode_value": clean_code,
                "identifier_type": clean_type,
                "packaging_level": clean_level,
                "priority_order": p_order,
                "pack_quantity_multiplier": multiplier,
            },
        )

        return cls.serialize_identifier(record)

    @classmethod
    def resolve_barcode(
        cls,
        barcode_value: str,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        High-speed resolution engine matching barcode input against active identifiers
        ordered strictly by priority_order ASC.
        """
        clean_code = str(barcode_value or "").strip()
        if not clean_code:
            return {"matched": False, "barcode_value": ""}

        qs = BarcodeAlternateIdentifier.objects.select_related(
            "product", "non_medicine_item", "variant"
        ).filter(
            barcode_value__iexact=clean_code,
            is_active=True,
        )

        if tenant_id:
            qs = qs.filter(Q(tenant_id_str=str(tenant_id).strip()) | Q(tenant_id_str="platform"))

        # Order by priority_order ASC (1 highest)
        record = qs.order_by("priority_order", "-version_number").first()

        if record:
            serialized = cls.serialize_identifier(record)
            return {
                "matched": True,
                "barcode_value": clean_code,
                "identifier_record": serialized,
                "resolved_mrp": serialized["resolved_mrp"],
                "resolved_packaging_level": record.packaging_level,
                "pack_quantity_multiplier": record.pack_quantity_multiplier,
            }

        # Fallback to direct legacy master product or retail item barcode
        product = MedicineProduct.objects.filter(barcode__iexact=clean_code).first()
        if product:
            return {
                "matched": True,
                "barcode_value": clean_code,
                "identifier_record": {
                    "identifier_id": f"legacy-{product.product_id}",
                    "tenant_id": "platform",
                    "product_id": product.product_id,
                    "target_entity_type": "MEDICINE",
                    "target_name": product.brand_name,
                    "resolved_mrp": str(product.mrp_unit),
                    "barcode_value": clean_code,
                    "identifier_type": "PRIMARY_EAN13_GTIN",
                    "packaging_level": "UNIT",
                    "priority_order": 1,
                    "pack_quantity_multiplier": 1,
                    "is_active": True,
                },
                "resolved_mrp": str(product.mrp_unit),
                "resolved_packaging_level": "UNIT",
                "pack_quantity_multiplier": 1,
            }

        retail_item = NonMedicineRetailItem.objects.filter(barcode__iexact=clean_code, is_active=True).first()
        if retail_item:
            return {
                "matched": True,
                "barcode_value": clean_code,
                "identifier_record": {
                    "identifier_id": f"legacy-{retail_item.item_id}",
                    "tenant_id": retail_item.tenant_id_str,
                    "non_medicine_item_id": retail_item.item_id,
                    "target_entity_type": "NON_MEDICINE",
                    "target_name": retail_item.name,
                    "resolved_mrp": str(retail_item.mrp),
                    "barcode_value": clean_code,
                    "identifier_type": "PRIMARY_EAN13_GTIN",
                    "packaging_level": "UNIT",
                    "priority_order": 1,
                    "pack_quantity_multiplier": 1,
                    "is_active": True,
                },
                "resolved_mrp": str(retail_item.mrp),
                "resolved_packaging_level": "UNIT",
                "pack_quantity_multiplier": 1,
            }

        return {"matched": False, "barcode_value": clean_code}

    @classmethod
    @transaction.atomic
    def update_priority(
        cls,
        identifier_id: str,
        new_priority: int,
        actor: Optional[PharmacyUser] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Amends priority order of a barcode identifier."""
        record = BarcodeAlternateIdentifier.objects.filter(identifier_id=str(identifier_id).strip()).first()
        if not record:
            raise ValidationError({"identifier_id": f"Barcode identifier '{identifier_id}' not found."})

        is_superuser = bool(getattr(actor, "is_superuser", False) or str(getattr(actor, "role", "")).upper() == "SUPERADMIN")
        effective_tenant = tenant_id or getattr(actor, "tenant_id", "platform")

        if not is_superuser and record.tenant_id_str != "platform" and record.tenant_id_str != effective_tenant:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied(
                detail={
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "message_en": "Cannot mutate barcode identifier belonging to another tenant.",
                    "message_bn": "অন্য ফার্মেসির বারকোড পরিবর্তন করা নিষিদ্ধ।",
                }
            )

        record.priority_order = max(1, int(new_priority))
        record.version_number += 1
        record.immutable_hash = record.compute_hash()
        record.save()

        AuditLogEntry.objects.create(
            action="BARCODE_PRIORITY_UPDATED",
            resource_type="BARCODE",
            resource_id=record.identifier_id,
            tenant_id=record.tenant_id_str,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "new_priority": record.priority_order,
                "version_number": record.version_number,
            },
        )

        return cls.serialize_identifier(record)

    @classmethod
    @transaction.atomic
    def archive_barcode(
        cls,
        identifier_id: str,
        actor: Optional[PharmacyUser] = None,
        tenant_id: Optional[str] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Soft-archives a barcode identifier."""
        record = BarcodeAlternateIdentifier.objects.filter(identifier_id=str(identifier_id).strip()).first()
        if not record:
            raise ValidationError({"identifier_id": f"Barcode identifier '{identifier_id}' not found."})

        is_superuser = bool(getattr(actor, "is_superuser", False) or str(getattr(actor, "role", "")).upper() == "SUPERADMIN")
        effective_tenant = tenant_id or getattr(actor, "tenant_id", "platform")

        if not is_superuser and record.tenant_id_str != "platform" and record.tenant_id_str != effective_tenant:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied(
                detail={
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "message_en": "Cannot archive barcode belonging to another tenant.",
                    "message_bn": "অন্য ফার্মেসির বারকোড আর্কাইভ করা নিষিদ্ধ।",
                }
            )

        record.is_active = False
        record.save(update_fields=["is_active"])

        AuditLogEntry.objects.create(
            action="BARCODE_IDENTIFIER_ARCHIVED",
            resource_type="BARCODE",
            resource_id=record.identifier_id,
            tenant_id=record.tenant_id_str,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "reason": str(reason or "").strip(),
            },
        )

        return cls.serialize_identifier(record)

    @classmethod
    def list_barcodes(
        cls,
        product_id: Optional[str] = None,
        non_medicine_item_id: Optional[str] = None,
        variant_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Queries and lists barcode identifiers with filtering."""
        qs = BarcodeAlternateIdentifier.objects.select_related(
            "product", "non_medicine_item", "variant"
        ).all()

        if product_id:
            qs = qs.filter(product_id=str(product_id).strip())
        if non_medicine_item_id:
            qs = qs.filter(non_medicine_item_id=str(non_medicine_item_id).strip())
        if variant_id:
            qs = qs.filter(variant_id=str(variant_id).strip())
        if tenant_id:
            qs = qs.filter(Q(tenant_id_str=str(tenant_id).strip()) | Q(tenant_id_str="platform"))
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if search:
            q = str(search).strip()
            qs = qs.filter(
                Q(barcode_value__icontains=q)
                | Q(description__icontains=q)
                | Q(product__brand_name__icontains=q)
            )

        return [cls.serialize_identifier(r) for r in qs.order_by("priority_order", "-version_number")]
