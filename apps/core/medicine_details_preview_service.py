"""
backend/apps/core/medicine_details_preview_service.py
Architecture Decision: ADR-0138 (P138).
Service Layer for Unified Medicine Details Preview:
Aggregates Clinical Form, Strength Taxonomy, Administration Route, Packaging Hierarchy,
Rx / AWaRe Status, Storage Conditions, Active Variants, Tenant Overrides, and Recall Alerts.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

from django.core.exceptions import ValidationError
from django.db.models import Q

from apps.core.models import (
    BarcodeAlternateIdentifier,
    BarcodePackagingLevel,
    CatalogueLifecycleNotice,
    CatalogueLifecycleStatus,
    ClassificationType,
    MedicineClassificationSnapshot,
    MedicineProduct,
    MedicineVariant,
    TenantMedicineConfig,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.medicine_details_preview")


class MedicineDetailsPreviewService:
    """
    Business service generating a unified, rich medicine detail preview
    incorporating clinical form, strength, route, multi-tier packaging,
    Rx status, cold-chain storage rules, commercial variants, and recall alerts.
    """

    @classmethod
    def _is_management_or_pharmacist(cls, actor: Optional[PharmacyUser]) -> bool:
        if not actor or not actor.is_authenticated:
            return False
        if bool(getattr(actor, "is_superuser", False)):
            return True
        role = str(getattr(actor, "role", "")).upper().strip()
        return role in {
            UserRole.SUPERADMIN,
            "SUPER_ADMIN",
            "PLATFORM_OPERATOR",
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            "OWNER",
            "MANAGER",
            "PHARMACIST",
        }

    @classmethod
    def generate_preview(
        cls,
        product_id: str,
        tenant_id: Optional[str] = None,
        actor: Optional[PharmacyUser] = None,
    ) -> Dict[str, Any]:
        """
        Consolidates all medicine domain layers into a single high-fidelity preview payload.
        """
        clean_id = str(product_id or "").strip()
        if not clean_id:
            raise ValidationError({"product_id": "product_id is required."})

        product = (
            MedicineProduct.objects.select_related("curated_by", "reviewed_by")
            .filter(product_id=clean_id)
            .first()
        )
        if not product:
            raise ValidationError({"product_id": f"Medicine product '{clean_id}' not found in catalog."})

        effective_tenant = tenant_id or getattr(actor, "tenant_id", None)

        # 1. Tenant-Specific Overrides (ADR-0071 / P128)
        override = None
        if effective_tenant and effective_tenant != "platform":
            override = (
                TenantMedicineConfig.objects.filter(
                    product=product,
                    tenant_id=effective_tenant,
                )
                .first()
            )

        if override:
            has_tenant_override = True
            local_brand_name = override.local_brand_name or ""
            shelf_location = override.shelf_location or ""
            effective_mrp_unit = (
                str(override.custom_mrp_unit)
                if override.custom_mrp_unit is not None
                else str(product.mrp_unit)
            )
        else:
            has_tenant_override = False
            local_brand_name = ""
            shelf_location = ""
            effective_mrp_unit = str(product.mrp_unit)

        # 2. Rx, Narcotic, and Antimicrobial AWaRe Classification
        is_rx = bool(product.is_prescription_only)
        is_narcotic = bool(product.is_narcotic)
        is_antibiotic = bool(product.is_antibiotic)

        # Inspect current classification snapshot if present
        antimicrobial_class = "NONE"
        snapshot = (
            MedicineClassificationSnapshot.objects.filter(product=product, is_current=True)
            .order_by("-version_number")
            .first()
        )
        if snapshot:
            if snapshot.classification_type == ClassificationType.ANTIMICROBIAL_ACCESS:
                antimicrobial_class = "ACCESS"
            elif snapshot.classification_type == ClassificationType.ANTIMICROBIAL_WATCH:
                antimicrobial_class = "WATCH"
            elif snapshot.classification_type == ClassificationType.ANTIMICROBIAL_RESERVE:
                antimicrobial_class = "RESERVE"
        elif is_antibiotic:
            antimicrobial_class = "ACCESS"

        if is_narcotic:
            rx_label = "Controlled Narcotic (Schedule-G)"
            rx_label_bn = "নিয়ন্ত্রিত মাদকদ্রব্য (তফসিল-জি)"
        elif is_rx:
            rx_label = "Prescription-Only Medicine (POM)"
            rx_label_bn = "শুধুমাত্র রেজিস্টার্ড চিকিৎসকের প্রেসক্রিপশন সাপেক্ষে বিক্রয়যোগ্য (POM)"
        else:
            rx_label = "Over-The-Counter (OTC)"
            rx_label_bn = "ওভার-দ্য-কাউন্টার (OTC) - সাধারণ বিক্রয়যোগ্য"

        # 3. Storage and Cold Chain Conditions
        requires_cold_chain = bool(product.requires_cold_chain)
        if requires_cold_chain:
            min_temp = float(product.min_temp_celsius) if product.min_temp_celsius is not None else 2.0
            max_temp = float(product.max_temp_celsius) if product.max_temp_celsius is not None else 8.0
            storage_en = f"Store in refrigerator between {min_temp:.1f}°C and {max_temp:.1f}°C. Do not freeze."
            storage_bn = f"{min_temp:.1f}°সে থেকে {max_temp:.1f}°সে তাপমাত্রায় রেফ্রিজারেটরে সংরক্ষণ করুন। ডিপ ফ্রিজে রাখবেন না।"
        else:
            min_temp = float(product.min_temp_celsius) if product.min_temp_celsius is not None else 15.0
            max_temp = float(product.max_temp_celsius) if product.max_temp_celsius is not None else 30.0
            storage_en = "Store below 30°C in a dry place protected from light and moisture."
            storage_bn = "৩০°সে এর নিচে শুষ্ক স্থানে এবং আলো ও আর্দ্রতা থেকে দূরে সংরক্ষণ করুন।"

        # 4. Packaging Hierarchy Tiers (P136)
        barcodes_qs = BarcodeAlternateIdentifier.objects.filter(
            product=product,
            is_active=True,
        )
        if effective_tenant and effective_tenant != "platform":
            barcodes_qs = barcodes_qs.filter(
                Q(tenant_id_str=effective_tenant) | Q(tenant_id_str="platform")
            )

        barcodes_list = list(barcodes_qs.order_by("priority_order", "packaging_level"))
        packaging_hierarchy: List[Dict[str, Any]] = []

        if barcodes_list:
            for b in barcodes_list:
                packaging_hierarchy.append({
                    "packaging_level": b.packaging_level,
                    "multiplier": b.pack_quantity_multiplier,
                    "barcode_value": b.barcode_value,
                    "priority_order": b.priority_order,
                })
        else:
            # Standard default packaging tiers
            primary_code = product.barcode or ""
            packaging_hierarchy = [
                {
                    "packaging_level": BarcodePackagingLevel.UNIT,
                    "multiplier": 1,
                    "barcode_value": primary_code,
                    "priority_order": 1,
                },
                {
                    "packaging_level": BarcodePackagingLevel.STRIP,
                    "multiplier": 10,
                    "barcode_value": "",
                    "priority_order": 2,
                },
                {
                    "packaging_level": BarcodePackagingLevel.BOX,
                    "multiplier": 100,
                    "barcode_value": "",
                    "priority_order": 3,
                },
            ]

        # 5. Commercial Presentation Variants (P126)
        variants_qs = MedicineVariant.objects.filter(product=product).order_by("pack_size_numeric")
        variants_summary: List[Dict[str, Any]] = []
        for v in variants_qs:
            variants_summary.append({
                "variant_id": v.variant_id,
                "sku": v.sku,
                "variant_name": v.variant_name,
                "strength": v.strength,
                "pack_size": v.pack_size,
                "pack_size_numeric": v.pack_size_numeric,
                "container_type": v.container_type,
                "mrp_pack": str(v.mrp_pack),
                "cost_pack": str(v.cost_pack) if cls._is_management_or_pharmacist(actor) else "",
                "barcode": v.barcode or "",
            })

        # 6. Safety Recall & Discontinuation Status (P137)
        notices_qs = CatalogueLifecycleNotice.objects.filter(
            product=product,
            is_active=True,
        )
        if effective_tenant and effective_tenant != "platform":
            notices_qs = notices_qs.filter(
                Q(tenant_id_str=effective_tenant) | Q(tenant_id_str="platform")
            )

        active_notice = notices_qs.order_by("-effective_date").first()

        is_recalled = False
        is_discontinued = False
        is_dispensing_blocked = False
        active_recall_dict = None

        if active_notice:
            is_dispensing_blocked = active_notice.is_dispensing_blocked
            if active_notice.lifecycle_status in (
                CatalogueLifecycleStatus.RECALLED,
                CatalogueLifecycleStatus.WITHDRAWN,
                CatalogueLifecycleStatus.QUARANTINED,
            ):
                is_recalled = True
                active_recall_dict = {
                    "notice_number": active_notice.notice_number,
                    "regulatory_authority": active_notice.regulatory_authority,
                    "lifecycle_status": active_notice.lifecycle_status,
                    "reason": active_notice.reason,
                    "action_required": active_notice.action_required,
                }
            elif active_notice.lifecycle_status == CatalogueLifecycleStatus.DISCONTINUED:
                is_discontinued = True

        # 7. Wholesale Cost Price Masking for Cashier Privacy
        cost_unit_display: Optional[str] = (
            str(product.cost_unit) if cls._is_management_or_pharmacist(actor) else None
        )

        # 8. Cryptographic Hash of Consolidated Preview Record
        raw_hash_content = (
            f"{product.product_id}|{product.brand_name}|{product.dosage_form}|"
            f"{product.strength}|{product.route}|{effective_mrp_unit}|{is_rx}|"
            f"{requires_cold_chain}|{is_dispensing_blocked}"
        )
        preview_hash = hashlib.sha256(raw_hash_content.encode("utf-8")).hexdigest()

        return {
            "product_id": product.product_id,
            "brand_name": product.brand_name,
            "brand_name_bn": product.brand_name_bn or "",
            "generic_name": product.generic_name,
            "generic_name_bn": product.generic_name_bn or "",
            "manufacturer": product.manufacturer,
            "dgda_dar_number": product.dgda_dar_number or "",
            "curation_status": product.curation_status,
            "dosage_form": product.dosage_form,
            "strength": product.strength,
            "strength_numeric": float(product.strength_numeric) if product.strength_numeric is not None else None,
            "strength_unit": product.strength_unit or "mg",
            "strength_unit_family": product.strength_unit_family or "MASS",
            "route": product.route or "Oral",
            "primary_barcode": product.barcode or "",
            "mrp_unit": str(product.mrp_unit),
            "cost_unit": cost_unit_display,
            "effective_mrp_unit": effective_mrp_unit,
            "has_tenant_override": has_tenant_override,
            "local_brand_name": local_brand_name,
            "shelf_location": shelf_location,
            "is_prescription_only": is_rx,
            "is_narcotic": is_narcotic,
            "is_antibiotic": is_antibiotic,
            "antimicrobial_classification": antimicrobial_class,
            "rx_classification_label": rx_label,
            "rx_classification_label_bn": rx_label_bn,
            "requires_cold_chain": requires_cold_chain,
            "min_temp_celsius": min_temp,
            "max_temp_celsius": max_temp,
            "storage_instructions": storage_en,
            "storage_instructions_bn": storage_bn,
            "packaging_hierarchy": packaging_hierarchy,
            "variants": variants_summary,
            "is_recalled": is_recalled,
            "is_discontinued": is_discontinued,
            "is_dispensing_blocked": is_dispensing_blocked,
            "active_recall_notice": active_recall_dict,
            "immutable_preview_hash": preview_hash,
        }
