"""
Medicine Variant Structure & SKU Governance Service.
Architecture Decision: ADR-0126 (P126).
Phase 07: Milestone M04 - Shared Medicine Catalog & Dynamic Packaging Units.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db.models import Q
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import MedicineProduct, MedicineVariant
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.core.medicine_variant")


class MedicineVariantService:
    """
    Manages commercial presentations, SKUs, pack sizes, containers,
    and regulatory classification inheritance for medicine products.
    """

    @classmethod
    def serialize_variant(cls, variant: MedicineVariant) -> Dict[str, Any]:
        """Serializes variant record with parent clinical metadata."""
        product = variant.product
        return {
            "variant_id": variant.variant_id,
            "product_id": product.product_id,
            "sku": variant.sku,
            "variant_name": variant.variant_name,
            "brand_name": product.brand_name,
            "generic_name": product.generic_name,
            "dosage_form": product.dosage_form,
            "manufacturer": product.manufacturer,
            "strength": variant.strength,
            "strength_numeric": str(variant.strength_numeric) if variant.strength_numeric else None,
            "strength_unit": variant.strength_unit,
            "pack_size": variant.pack_size,
            "pack_size_numeric": variant.pack_size_numeric,
            "route": variant.route,
            "container_type": variant.container_type,
            "barcode": variant.barcode,
            "mrp_unit": str(variant.mrp_unit),
            "cost_unit": str(variant.cost_unit),
            "mrp_pack": str(variant.mrp_pack),
            "cost_pack": str(variant.cost_pack),
            "is_prescription_required": variant.is_prescription_required,
            "is_narcotic": variant.is_narcotic,
            "is_antibiotic": variant.is_antibiotic,
            "is_cold_chain": variant.is_cold_chain,
            "is_active": variant.is_active,
            "created_at": variant.created_at.isoformat(),
        }

    @classmethod
    def list_variants(
        cls,
        product_id: Optional[str] = None,
        search: Optional[str] = None,
        barcode: Optional[str] = None,
        sku: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Queries medicine variants with filtering and search."""
        qs = MedicineVariant.objects.select_related("product").all()

        if product_id:
            qs = qs.filter(product_id=product_id)
        if barcode:
            qs = qs.filter(barcode=barcode.strip())
        if sku:
            qs = qs.filter(sku=sku.strip())
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if search:
            query = search.strip()
            qs = qs.filter(
                Q(variant_name__icontains=query)
                | Q(sku__icontains=query)
                | Q(barcode__icontains=query)
                | Q(product__brand_name__icontains=query)
                | Q(product__generic_name__icontains=query)
            )

        return [cls.serialize_variant(v) for v in qs]

    @classmethod
    def get_variant(cls, variant_id: str) -> Dict[str, Any]:
        """Retrieves a single variant by its ID."""
        variant = MedicineVariant.objects.select_related("product").filter(variant_id=variant_id).first()
        if not variant:
            raise ValidationError(
                {
                    "error_code": "ERR_VARIANT_NOT_FOUND",
                    "message_en": f"Medicine variant with ID '{variant_id}' does not exist.",
                    "message_bn": f"অনুরোধকৃত মেডিসিন ভ্যারিয়েন্ট '{variant_id}' খুঁজে পাওয়া যায়নি।",
                }
            )
        return cls.serialize_variant(variant)

    @classmethod
    def create_variant(cls, actor_user, data: Dict[str, Any]) -> Dict[str, Any]:
        """Creates a commercial presentation variant SKU. Platform superadmin only."""
        if not getattr(actor_user, "is_superuser", False) and getattr(actor_user, "role", "") != UserRole.SUPERADMIN:
            raise PermissionDenied(
                {
                    "error_code": "PLATFORM_CATALOG_OWNERSHIP_PROTECTED",
                    "message_en": "Medicine variants and master catalog entries are centrally curated by platform administrators.",
                    "message_bn": "ঔষধ ভ্যারিয়েন্ট ও ক্যাটালগ এন্ট্রি কেন্দ্রীয়ভাবে প্ল্যাটফর্ম অ্যাডমিনিস্ট্রেটর দ্বারা পরিচালিত।",
                }
            )

        product_id = data.get("product_id")
        if not product_id:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_PRODUCT_ID",
                    "message_en": "product_id is required to attach a variant.",
                    "message_bn": "ভ্যারিয়েন্টের জন্য মূল পণ্যের আইডি (product_id) আবশ্যক।",
                }
            )

        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise ValidationError(
                {
                    "error_code": "ERR_PRODUCT_NOT_FOUND",
                    "message_en": f"Parent product '{product_id}' does not exist.",
                    "message_bn": f"মূল মেডিসিন পণ্য '{product_id}' পাওয়া যায়নি।",
                }
            )

        sku = data.get("sku", "").strip()
        if not sku:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_SKU",
                    "message_en": "sku is required for a commercial medicine variant.",
                    "message_bn": "মেডিসিন ভ্যারিয়েন্টের জন্য স্বতন্ত্র SKU কোড আবশ্যক।",
                }
            )

        if MedicineVariant.objects.filter(sku=sku).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_SKU",
                    "message_en": f"Medicine variant SKU '{sku}' already exists in catalog.",
                    "message_bn": f"ভ্যারিয়েন্ট SKU '{sku}' ইতিমধ্যেই নিবন্ধিত আছে।",
                }
            )

        barcode = data.get("barcode", "").strip()
        if barcode and MedicineVariant.objects.filter(barcode=barcode).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_BARCODE",
                    "message_en": f"Barcode '{barcode}' is already assigned to another variant.",
                    "message_bn": f"বারকোড '{barcode}' ইতিমধ্যেই অন্য ভ্যারিয়েন্টে বিদ্যমান।",
                }
            )

        pack_size_num = data.get("pack_size_numeric", 1)
        try:
            pack_size_num = int(pack_size_num)
            if pack_size_num <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_PACK_SIZE",
                    "message_en": "pack_size_numeric must be an integer > 0.",
                    "message_bn": "প্যাক সাইজ অবশ্যই ১ বা তার বেশি পূর্ণসংখ্যা হতে হবে।",
                }
            )

        mrp_unit = Decimal(str(data.get("mrp_unit", product.mrp_unit)))
        cost_unit = Decimal(str(data.get("cost_unit", product.cost_unit)))
        mrp_pack = Decimal(str(data.get("mrp_pack", mrp_unit * pack_size_num)))
        cost_pack = Decimal(str(data.get("cost_pack", cost_unit * pack_size_num)))

        variant_id = data.get("variant_id") or f"var-{uuid.uuid4().hex[:12]}"
        variant_name = data.get("variant_name") or f"{product.brand_name} {data.get('strength', product.strength)} ({data.get('pack_size', 'Standard Pack')})"

        # INHERIT REGULATORY CLASSIFICATIONS FROM PARENT MASTER
        variant = MedicineVariant.objects.create(
            variant_id=variant_id,
            product=product,
            sku=sku,
            variant_name=variant_name,
            strength=data.get("strength", product.strength),
            strength_numeric=Decimal(str(data.get("strength_numeric", product.strength_numeric or 500))),
            strength_unit=data.get("strength_unit", product.strength_unit or "mg"),
            pack_size=data.get("pack_size", f"Pack of {pack_size_num}"),
            pack_size_numeric=pack_size_num,
            route=data.get("route", product.route or "Oral"),
            container_type=data.get("container_type", "BLISTER_PACK"),
            barcode=barcode,
            mrp_unit=mrp_unit,
            cost_unit=cost_unit,
            mrp_pack=mrp_pack,
            cost_pack=cost_pack,
            is_prescription_required=product.is_prescription_only,
            is_narcotic=product.is_narcotic,
            is_antibiotic=product.is_antibiotic,
            is_cold_chain=product.requires_cold_chain,
            is_active=data.get("is_active", True),
        )

        return cls.serialize_variant(variant)
