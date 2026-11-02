"""
backend/apps/core/non_medicine_retail_service.py
Architecture Decision: ADR-0135 (P135).
Service Layer for Authorized Non-Medicine Retail Goods:
Separation of Dispensing Controls, Multi-Tenancy, Barcode Deduplication, and Audit History.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    NonMedicineCategory,
    NonMedicineRetailItem,
    Tenant,
)
from apps.identity.models import PharmacyUser

logger = logging.getLogger("pharmacy.core.non_medicine_retail")


class NonMedicineRetailService:
    """
    Business service governing non-medicine healthcare retail items,
    ensuring dispensing and regulatory controls remain strictly medicine-specific.
    """

    @classmethod
    def serialize_item(cls, item: NonMedicineRetailItem) -> Dict[str, Any]:
        """Converts NonMedicineRetailItem to JSON Schema compliant dictionary."""
        return {
            "item_id": item.item_id,
            "tenant_id": item.tenant_id_str,
            "name": item.name,
            "name_bn": item.name_bn or "",
            "category": item.category,
            "brand_name": item.brand_name or "",
            "manufacturer_or_distributor": item.manufacturer_or_distributor or "",
            "barcode": item.barcode or "",
            "sku": item.sku or "",
            "mrp": str(item.mrp),
            "cost_price": str(item.cost_price),
            "vat_percent": str(item.vat_percent),
            "unit_of_measure": item.unit_of_measure or "PIECE",
            "requires_cold_chain": item.requires_cold_chain,
            "storage_instructions": item.storage_instructions or "",
            "is_medicine": False,
            "is_prescription_only": False,
            "is_antibiotic": False,
            "is_narcotic": False,
            "is_active": item.is_active,
            "version_number": item.version_number,
            "immutable_hash": item.immutable_hash,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        }

    @classmethod
    def _validate_regulatory_restrictions(cls, data: Dict[str, Any]):
        """
        Enforces ADR-0135 rule: Non-medicine items cannot be marked with pharmaceutical regulatory flags.
        """
        if (
            bool(data.get("is_prescription_only"))
            or bool(data.get("is_antibiotic"))
            or bool(data.get("is_narcotic"))
            or bool(data.get("is_medicine"))
        ):
            raise ValidationError(
                {
                    "regulatory_controls": (
                        "ERR_NON_MEDICINE_REGULATORY_RESTRICTION: Non-medicine retail items cannot be marked as "
                        "prescription-only, antibiotic, narcotic, or medicine. Regulatory dispensing controls are strictly medicine-specific."
                    )
                }
            )

    @classmethod
    @transaction.atomic
    def create_item(
        cls,
        data: Dict[str, Any],
        actor: Optional[PharmacyUser] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Creates a new non-medicine retail item with regulatory boundary validation."""
        cls._validate_regulatory_restrictions(data)

        name = str(data.get("name", "")).strip()
        if len(name) < 2:
            raise ValidationError({"name": "Item name must be at least 2 characters long."})

        raw_category = str(data.get("category", "")).strip().upper()
        if raw_category not in NonMedicineCategory.values:
            raise ValidationError(
                {"category": f"Invalid category '{raw_category}'. Must be one of {NonMedicineCategory.values}."}
            )

        try:
            mrp = Decimal(str(data.get("mrp", "0.00")))
            if mrp <= Decimal("0.00"):
                raise ValueError()
        except Exception:
            raise ValidationError({"mrp": "MRP must be a positive decimal greater than 0.00."})

        try:
            cost_price = Decimal(str(data.get("cost_price", "0.00")))
            if cost_price < Decimal("0.00"):
                raise ValueError()
        except Exception:
            raise ValidationError({"cost_price": "cost_price must be a decimal greater than or equal to 0.00."})

        try:
            vat_percent = Decimal(str(data.get("vat_percent", "0.00")))
            if vat_percent < Decimal("0.00") or vat_percent > Decimal("100.00"):
                raise ValueError()
        except Exception:
            raise ValidationError({"vat_percent": "vat_percent must be a decimal between 0.00 and 100.00."})

        is_superuser = bool(getattr(actor, "is_superuser", False) or str(getattr(actor, "role", "")).upper() == "SUPERADMIN")
        effective_tenant = tenant_id or (getattr(actor, "tenant_id", None) if not is_superuser else "platform") or "platform"

        tenant_obj = None
        if effective_tenant != "platform":
            tenant_obj = Tenant.objects.filter(tenant_id=effective_tenant).first()

        barcode = str(data.get("barcode", "")).strip()
        if barcode:
            dup = NonMedicineRetailItem.objects.filter(tenant_id_str=effective_tenant, barcode=barcode).first()
            if dup:
                raise ValidationError({"barcode": f"Item with barcode '{barcode}' already exists for tenant '{effective_tenant}'."})

        item_id = f"nmi-{uuid.uuid4().hex[:12]}"

        item = NonMedicineRetailItem.objects.create(
            item_id=item_id,
            tenant=tenant_obj,
            tenant_id_str=effective_tenant,
            name=name,
            name_bn=str(data.get("name_bn", "")).strip(),
            category=raw_category,
            brand_name=str(data.get("brand_name", "")).strip(),
            manufacturer_or_distributor=str(data.get("manufacturer_or_distributor", "")).strip(),
            barcode=barcode,
            sku=str(data.get("sku", "")).strip(),
            mrp=mrp,
            cost_price=cost_price,
            vat_percent=vat_percent,
            unit_of_measure=str(data.get("unit_of_measure", "PIECE")).strip() or "PIECE",
            requires_cold_chain=bool(data.get("requires_cold_chain", False)),
            storage_instructions=str(data.get("storage_instructions", "")).strip(),
            is_active=True,
            version_number=1,
            created_by=actor,
        )

        AuditLogEntry.objects.create(
            action="NON_MEDICINE_RETAIL_ITEM_CREATED",
            resource_type="RETAIL_ITEM",
            resource_id=item.item_id,
            tenant_id=effective_tenant,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "name": item.name,
                "category": item.category,
                "mrp": str(item.mrp),
                "barcode": item.barcode,
            },
        )

        return cls.serialize_item(item)

    @classmethod
    @transaction.atomic
    def update_item(
        cls,
        item_id: str,
        data: Dict[str, Any],
        actor: Optional[PharmacyUser] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Updates a retail item, validating tenant boundaries and regulatory invariants."""
        cls._validate_regulatory_restrictions(data)

        item = NonMedicineRetailItem.objects.filter(item_id=str(item_id).strip()).first()
        if not item:
            raise ValidationError({"item_id": f"Non-medicine retail item '{item_id}' not found."})

        is_superuser = bool(getattr(actor, "is_superuser", False) or str(getattr(actor, "role", "")).upper() == "SUPERADMIN")
        effective_tenant = tenant_id or getattr(actor, "tenant_id", "platform")

        if not is_superuser and item.tenant_id_str != "platform" and item.tenant_id_str != effective_tenant:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied(
                detail={
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "message_en": f"Cannot mutate retail item belonging to foreign tenant '{item.tenant_id_str}'.",
                    "message_bn": "অন্য ফার্মেসির রিটেইল পণ্য পরিবর্তন করা নিষিদ্ধ।",
                }
            )

        if "name" in data:
            clean_name = str(data["name"]).strip()
            if len(clean_name) < 2:
                raise ValidationError({"name": "Item name must be at least 2 characters long."})
            item.name = clean_name

        if "name_bn" in data:
            item.name_bn = str(data["name_bn"]).strip()
        if "category" in data:
            c = str(data["category"]).strip().upper()
            if c not in NonMedicineCategory.values:
                raise ValidationError({"category": f"Invalid category '{c}'. Must be one of {NonMedicineCategory.values}."})
            item.category = c
        if "brand_name" in data:
            item.brand_name = str(data["brand_name"]).strip()
        if "manufacturer_or_distributor" in data:
            item.manufacturer_or_distributor = str(data["manufacturer_or_distributor"]).strip()
        if "barcode" in data:
            item.barcode = str(data["barcode"]).strip()
        if "sku" in data:
            item.sku = str(data["sku"]).strip()
        if "mrp" in data:
            item.mrp = Decimal(str(data["mrp"]))
        if "cost_price" in data:
            item.cost_price = Decimal(str(data["cost_price"]))
        if "vat_percent" in data:
            item.vat_percent = Decimal(str(data["vat_percent"]))
        if "unit_of_measure" in data:
            item.unit_of_measure = str(data["unit_of_measure"]).strip()
        if "requires_cold_chain" in data:
            item.requires_cold_chain = bool(data["requires_cold_chain"])
        if "storage_instructions" in data:
            item.storage_instructions = str(data["storage_instructions"]).strip()

        item.version_number += 1
        item.immutable_hash = item.compute_hash()
        item.save()

        AuditLogEntry.objects.create(
            action="NON_MEDICINE_RETAIL_ITEM_UPDATED",
            resource_type="RETAIL_ITEM",
            resource_id=item.item_id,
            tenant_id=item.tenant_id_str,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "new_version": item.version_number,
                "name": item.name,
                "mrp": str(item.mrp),
            },
        )

        return cls.serialize_item(item)

    @classmethod
    @transaction.atomic
    def archive_item(
        cls,
        item_id: str,
        actor: Optional[PharmacyUser] = None,
        tenant_id: Optional[str] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Soft-archives a retail item."""
        item = NonMedicineRetailItem.objects.filter(item_id=str(item_id).strip()).first()
        if not item:
            raise ValidationError({"item_id": f"Non-medicine retail item '{item_id}' not found."})

        is_superuser = bool(getattr(actor, "is_superuser", False) or str(getattr(actor, "role", "")).upper() == "SUPERADMIN")
        effective_tenant = tenant_id or getattr(actor, "tenant_id", "platform")

        if not is_superuser and item.tenant_id_str != "platform" and item.tenant_id_str != effective_tenant:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied(
                detail={
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "message_en": "Cannot archive retail item belonging to another tenant.",
                    "message_bn": "অন্য ফার্মেসির রিটেইল পণ্য আর্কাইভ করা নিষিদ্ধ।",
                }
            )

        item.is_active = False
        item.save(update_fields=["is_active"])

        AuditLogEntry.objects.create(
            action="NON_MEDICINE_RETAIL_ITEM_ARCHIVED",
            resource_type="RETAIL_ITEM",
            resource_id=item.item_id,
            tenant_id=item.tenant_id_str,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "reason": str(reason or "").strip(),
            },
        )

        return cls.serialize_item(item)

    @classmethod
    def list_items(
        cls,
        tenant_id: Optional[str] = None,
        category: Optional[str] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
        include_global: bool = True,
    ) -> List[Dict[str, Any]]:
        """Queries and lists non-medicine retail items with scoping and search."""
        qs = NonMedicineRetailItem.objects.all()

        if tenant_id:
            if include_global:
                qs = qs.filter(Q(tenant_id_str=str(tenant_id).strip()) | Q(tenant_id_str="platform"))
            else:
                qs = qs.filter(tenant_id_str=str(tenant_id).strip())

        if category:
            qs = qs.filter(category=str(category).strip().upper())
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if search:
            q = str(search).strip()
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(name_bn__icontains=q)
                | Q(barcode__icontains=q)
                | Q(brand_name__icontains=q)
                | Q(sku__icontains=q)
            )

        return [cls.serialize_item(i) for i in qs]
