"""
backend/apps/core/medicine_search_service.py
Architecture Decision: ADR-0129 (P129).
Multilingual Medicine Search Autocomplete Engine with Barcode Prioritization,
Phonetic Transliteration, Precedence-Aware Effective Attributes, and Latency Benchmarks.
"""

from __future__ import annotations

import time
import unicodedata
from typing import Any, Dict, List, Set

from django.core.exceptions import ValidationError
from django.db import models
from rest_framework.exceptions import PermissionDenied

from apps.core.models import (
    MedicineProduct,
    MedicineVariant,
    ModerationStatus,
    Tenant,
    TenantMedicineConfig,
)

# Common phonetic bilingual transliteration pairs for Bangladeshi pharmaceutical searches
PHONETIC_PAIRS = [
    ("napa", "নাপা"),
    ("seclo", "সেকলো"),
    ("ace", "এইস"),
    ("paracetamol", "প্যারাসিটামল"),
    ("omeprazole", "ওমেপ্রাজল"),
    ("esomeprazole", "এসোমিপ্রাজল"),
    ("sergel", "সারজেল"),
    ("monas", "মোনাস"),
    ("montelukast", "মন্টেলুকাস্ট"),
    ("beximco", "বেক্সিমকো"),
    ("square", "স্কয়ার"),
    ("incepta", "ইনসেপ্টা"),
    ("renata", "রেনাটা"),
]


class MedicineSearchAutocompleteService:
    """
    High-speed autocomplete search engine for cashier checkout and catalog queries.
    """

    @classmethod
    def normalize_query(cls, text: str) -> str:
        """Normalizes Unicode text, removes diacritics/zero-width spaces, and lowercases."""
        if not text:
            return ""
        norm = unicodedata.normalize("NFKC", str(text))
        cleaned = norm.replace("\u200c", "").replace("\u200d", "").strip()
        return cleaned.lower()

    @classmethod
    def get_transliteration_variants(cls, query: str) -> Set[str]:
        """Returns transliteration variations between English romanization and Bengali text."""
        variants: Set[str] = set()
        clean_q = cls.normalize_query(query)
        if not clean_q:
            return variants

        variants.add(clean_q)

        # Match mapped word roots
        for en, bn in PHONETIC_PAIRS:
            if clean_q in en or en in clean_q:
                variants.add(bn)
            if clean_q in bn or bn in clean_q:
                variants.add(en)

        return variants

    @classmethod
    def autocomplete(
        cls,
        query: str,
        tenant_id: str,
        limit: int = 10,
        include_variants: bool = True,
    ) -> Dict[str, Any]:
        """
        Executes high-speed ranked search for a query string within a tenant's catalog scope.
        """
        start_time = time.perf_counter()
        clean_tenant = str(tenant_id or "").strip()
        raw_query = str(query or "").strip()

        if not raw_query:
            raise ValidationError({"q": "Search query parameter cannot be empty."})

        # Validate limit bounds (1 to 50)
        try:
            safe_limit = max(1, min(int(limit), 50))
        except (ValueError, TypeError):
            safe_limit = 10

        # Verify tenant standing
        tenant = Tenant.objects.filter(tenant_id=clean_tenant).first()
        if not tenant:
            raise ValidationError({"tenant_id": f"Pharmacy tenant '{clean_tenant}' does not exist."})

        if str(getattr(tenant, "status", "ACTIVE")).upper() == "SUSPENDED":
            raise PermissionDenied("ERR_TENANT_SUSPENDED: Suspended tenant cannot access catalog search.")

        norm_q = cls.normalize_query(raw_query)
        translit_variants = cls.get_transliteration_variants(norm_q)

        # 1. Fetch Tenant Overrides (for visibility and local customizations)
        tenant_configs = {
            cfg.product_id: cfg
            for cfg in TenantMedicineConfig.objects.filter(tenant_id=clean_tenant).select_related("product")
        }

        hidden_product_ids = {
            pid for pid, cfg in tenant_configs.items() if not cfg.is_visible
        }

        # 2. Build Query Filters across Product Fields
        product_filter = models.Q()

        # Check numeric barcode or SKU exact match
        is_numeric = raw_query.isdigit()
        if is_numeric:
            product_filter |= models.Q(barcode__iexact=raw_query)

        # Brand / Generic / Manufacturer matches
        product_filter |= (
            models.Q(brand_name__icontains=norm_q)
            | models.Q(brand_name_bn__icontains=norm_q)
            | models.Q(generic_name__icontains=norm_q)
            | models.Q(generic_name_bn__icontains=norm_q)
            | models.Q(manufacturer__icontains=norm_q)
            | models.Q(strength__icontains=norm_q)
            | models.Q(barcode__icontains=norm_q)
        )

        for variant_str in translit_variants:
            product_filter |= (
                models.Q(brand_name__icontains=variant_str)
                | models.Q(brand_name_bn__icontains=variant_str)
                | models.Q(generic_name__icontains=variant_str)
                | models.Q(generic_name_bn__icontains=variant_str)
            )

        # Also search in tenant store-specific brand aliases or local SKUs
        matching_config_pids = [
            cfg.product_id
            for cfg in tenant_configs.values()
            if cfg.is_visible
            and (
                norm_q in cls.normalize_query(cfg.local_brand_name)
                or (cfg.local_sku and norm_q in cls.normalize_query(cfg.local_sku))
            )
        ]
        if matching_config_pids:
            product_filter |= models.Q(product_id__in=matching_config_pids)

        products_qs = (
            MedicineProduct.objects.filter(product_filter)
            .exclude(product_id__in=hidden_product_ids)
            .distinct()[:50]
        )

        # 3. Match Commercial Variants (SKU / presentation level)
        variant_results: List[Dict[str, Any]] = []
        if include_variants:
            variant_filter = (
                models.Q(sku__iexact=raw_query)
                | models.Q(barcode__iexact=raw_query)
                | models.Q(variant_name__icontains=norm_q)
            )
            variants_qs = (
                MedicineVariant.objects.filter(variant_filter)
                .exclude(product_id__in=hidden_product_ids)
                .select_related("product")[:20]
            )
            for var in variants_qs:
                p = var.product
                cfg = tenant_configs.get(p.product_id)

                # Effective attributes
                effective_brand = (
                    cfg.local_brand_name
                    if cfg and cfg.local_brand_name and cfg.moderation_status == ModerationStatus.APPROVED
                    else p.brand_name
                )
                effective_mrp = (
                    str(cfg.custom_mrp_unit)
                    if cfg and cfg.custom_mrp_unit and cfg.moderation_status == ModerationStatus.APPROVED
                    else str(var.mrp_pack)
                )

                # Scoring
                if raw_query.lower() == var.sku.lower() or raw_query == var.barcode:
                    score = 100
                    m_type = "SKU_EXACT" if raw_query.lower() == var.sku.lower() else "BARCODE_EXACT"
                elif var.variant_name.lower().startswith(norm_q):
                    score = 85
                    m_type = "BRAND_PREFIX"
                else:
                    score = 60
                    m_type = "CONTAINS_SUBSTRING"

                variant_results.append(
                    {
                        "product_id": p.product_id,
                        "variant_id": var.variant_id,
                        "display_name": f"{effective_brand} ({var.pack_size})",
                        "brand_name_en": p.brand_name,
                        "brand_name_bn": p.brand_name_bn,
                        "generic_name": p.generic_name,
                        "generic_name_bn": p.generic_name_bn,
                        "dosage_form": p.dosage_form,
                        "strength": var.strength or p.strength,
                        "manufacturer": p.manufacturer,
                        "barcode": var.barcode or p.barcode,
                        "sku": var.sku,
                        "effective_mrp_unit": effective_mrp,
                        "effective_requires_prescription": p.is_prescription_only,
                        "shelf_location": cfg.shelf_location if cfg else "",
                        "is_overridden": cfg is not None,
                        "match_type": m_type,
                        "rank_score": score,
                    }
                )

        # 4. Rank and Score Master Product Results
        product_results: List[Dict[str, Any]] = []
        for p in products_qs:
            cfg = tenant_configs.get(p.product_id)

            # Effective brand & price resolution
            if cfg and cfg.local_brand_name and cfg.moderation_status == ModerationStatus.APPROVED:
                effective_brand = cfg.local_brand_name
            else:
                effective_brand = p.brand_name

            if cfg and cfg.custom_mrp_unit is not None and cfg.moderation_status == ModerationStatus.APPROVED:
                effective_mrp = str(cfg.custom_mrp_unit)
            else:
                effective_mrp = str(p.mrp_unit)

            shelf = cfg.shelf_location if cfg else ""
            local_sku = cfg.local_sku if cfg else ""

            # Determine rank score & match type
            score = 50
            match_type = "CONTAINS_SUBSTRING"

            # Check 1: Exact Barcode or Local SKU match
            if raw_query == p.barcode or (local_sku and raw_query.lower() == local_sku.lower()):
                score = 100
                match_type = "BARCODE_EXACT" if raw_query == p.barcode else "SKU_EXACT"
            # Check 2: Brand Prefix
            elif (
                p.brand_name.lower().startswith(norm_q)
                or (p.brand_name_bn and p.brand_name_bn.startswith(norm_q))
                or (cfg and cfg.local_brand_name and cfg.local_brand_name.lower().startswith(norm_q))
            ):
                score = 90
                match_type = "BRAND_PREFIX"
            # Check 3: Generic Prefix
            elif p.generic_name.lower().startswith(norm_q) or (p.generic_name_bn and p.generic_name_bn.startswith(norm_q)):
                score = 80
                match_type = "GENERIC_PREFIX"
            # Check 4: Transliteration Match
            elif any(
                v in p.brand_name.lower()
                or (p.brand_name_bn and v in p.brand_name_bn)
                or v in p.generic_name.lower()
                for v in translit_variants
            ):
                score = 75
                match_type = "TRANSLITERATION_MATCH"

            product_results.append(
                {
                    "product_id": p.product_id,
                    "variant_id": None,
                    "display_name": f"{effective_brand} {p.strength} {p.dosage_form}".strip(),
                    "brand_name_en": p.brand_name,
                    "brand_name_bn": p.brand_name_bn,
                    "generic_name": p.generic_name,
                    "generic_name_bn": p.generic_name_bn,
                    "dosage_form": p.dosage_form,
                    "strength": p.strength,
                    "manufacturer": p.manufacturer,
                    "barcode": p.barcode,
                    "sku": local_sku,
                    "effective_mrp_unit": effective_mrp,
                    "effective_requires_prescription": p.is_prescription_only,
                    "shelf_location": shelf,
                    "is_overridden": cfg is not None,
                    "match_type": match_type,
                    "rank_score": score,
                }
            )

        # 5. Merge, Deduplicate, Sort by Rank Score & Slice
        all_results = product_results + variant_results
        # Sort by rank_score DESC, then display_name ASC
        all_results.sort(key=lambda x: (-x["rank_score"], x["display_name"]))

        paginated = all_results[:safe_limit]
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

        return {
            "query": raw_query,
            "tenant_id": clean_tenant,
            "total_matches": len(all_results),
            "latency_ms": elapsed_ms,
            "results": paginated,
        }
