"""
backend/apps/core/pos_search_cart_service.py
Architecture Decision: ADR-0222 (P222).
Phase 12: POS, Sales Orders, Checkout & Returns.

Domain Service for P222 Fast Search and Barcode Cart:
- High-speed barcode, variant SKU, local SKU, and bilingual EN/BN + phonetic brand/generic search
  enriched with validated SKU conversion factors and branch inventory snapshot read models.
- Strict FEFO eligible batch filtering (never exposes expired, quarantined, or recalled stock
  as sellable; surfaces safety diagnostics clearly and redacts cost prices from Cashiers).
- Keyboard-first server-side cart mutations (`quick-cart`, `scan-add`, `cart-line`) backed by
  PosSaleOrder/PosSaleOrderLine with select_for_update + optimistic `version` concurrency control,
  NonDivisiblePackPolicy enforcement, and append-only PosCartScanEvent audit logging.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from apps.core.medicine_search_service import PHONETIC_PAIRS
from apps.core.models import (
    BarcodeAlternateIdentifier,
    BatchStatus,
    BatchStock,
    Branch,
    CashierRegisterAssignment,
    InventorySnapshotCheckpoint,
    LoginDeviceRegistration,
    MedicineProduct,
    MedicineVariant,
    NonDivisiblePackPolicy,
    PackagingHierarchyNode,
    PosCartScanEvent,
    PosCartUnitSelectionAudit,
    PosCustomer,
    PosDiscountType,
    PosPackagingUnit,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleOrderLine,
    PosSaleSourceChannel,
    PosScanResolutionSource,
    PosSkuInventoryReadModel,
    PosSyncState,
    StaffMember,
    StockLedgerEntry,
    Tenant,
    TenantMedicineConfig,
)
from apps.core.pos_cart_unit_service import PosCartUnitSelectionService
from apps.core.pos_domain_service import PosDomainService, q2, q4
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.core.pos_search_cart")


class _ScanRejectionAbort(Exception):
    """Signals atomic cart rollback while preserving outside-transaction PosCartScanEvent logging."""

    def __init__(self, log_kwargs: Dict[str, Any], exc: APIException) -> None:
        super().__init__(str(exc))
        self.log_kwargs = log_kwargs
        self.exc = exc


class PosSearchCartService:
    """
    Unified fast search, barcode prioritization, validated SKU snapshot read-model sync,
    and keyboard-first POS cart mutation service.
    """

    SNAPSHOT_ADMIN_ROLES = {
        UserRole.SUPERADMIN,
        UserRole.PHARMACY_OWNER,
        UserRole.BRANCH_MANAGER,
        UserRole.STAFF_PHARMACIST,
        UserRole.INVENTORY_CLERK,
        "SUPERADMIN",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "STAFF_PHARMACIST",
        "INVENTORY_CLERK",
    }

    PACKAGING_LABELS: Dict[str, Tuple[str, str]] = {
        PosPackagingUnit.TABLET: ("Tablet / Unit", "ট্যাবলেট / পিস"),
        PosPackagingUnit.STRIP: ("Strip", "স্ট্রিপ"),
        PosPackagingUnit.BOX: ("Box", "বক্স"),
        PosPackagingUnit.BOTTLE: ("Bottle", "বোতল"),
        PosPackagingUnit.VIAL: ("Vial", "ভায়াল"),
        PosPackagingUnit.TUBE: ("Tube", "টিউব"),
        PosPackagingUnit.SACHET: ("Sachet", "স্যাশে"),
        PosPackagingUnit.UNIT: ("Unit", "একক"),
    }

    @classmethod
    def _normalize_text(cls, text: Any) -> str:
        if not text:
            return ""
        norm = unicodedata.normalize("NFKC", str(text))
        return norm.replace("\u200c", "").replace("\u200d", "").strip().lower()

    @classmethod
    def _transliteration_variants(cls, query: str) -> List[str]:
        norm = cls._normalize_text(query)
        if not norm:
            return []
        out = [norm]
        for en, bn in PHONETIC_PAIRS:
            if norm in en or en in norm:
                if bn not in out:
                    out.append(bn)
            if norm in bn or bn in norm:
                if en not in out:
                    out.append(en)
        return out

    @classmethod
    def _map_packaging_level(cls, level: str) -> str:
        clean = str(level or "").strip().upper()
        if clean in ("BOX", "CARTON", "SHIPPER_CARTON"):
            return PosPackagingUnit.BOX
        if clean in ("STRIP", "BLISTER"):
            return PosPackagingUnit.STRIP
        if clean in ("BOTTLE", "VIAL", "TUBE", "SACHET", "TABLET"):
            return clean
        return PosPackagingUnit.TABLET

    @classmethod
    def _resolve_packaging_factors(
        cls,
        tenant: Tenant,
        branch: Optional[Branch],
        product: MedicineProduct,
    ) -> Tuple[int, int]:
        """Returns (strip_factor, box_factor) strictly >= 1 from authoritative hierarchy policies."""
        ctx = PosCartUnitSelectionService.resolve_packaging_context(tenant, branch, product)
        return int(ctx["strip_factor"]), int(ctx["box_factor"])

    @classmethod
    def _get_non_divisible_policy(
        cls,
        tenant: Tenant,
        branch: Optional[Branch],
        product: MedicineProduct,
    ) -> Optional[NonDivisiblePackPolicy]:
        return (
            NonDivisiblePackPolicy.objects.filter(
                tenant=tenant,
                product=product,
                is_active=True,
            )
            .filter(Q(branch=branch) | Q(branch__isnull=True))
            .order_by("-branch_id", "-updated_at")
            .first()
        )

    @classmethod
    def _build_branch_batch_stats(
        cls,
        tenant: Tenant,
        branch: Branch,
        product_ids: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        today = timezone.now().date()
        qs = BatchStock.objects.filter(tenant=tenant, branch=branch).order_by("expiry_date", "batch_id")
        if product_ids:
            qs = qs.filter(product_id__in=product_ids)

        stats: Dict[str, Dict[str, Any]] = {}
        for batch in qs:
            entry = stats.setdefault(
                batch.product_id,
                {
                    "sellable_base_units": 0,
                    "reserved_base_units": 0,
                    "available_base_units": 0,
                    "quarantined_base_units": 0,
                    "expired_base_units": 0,
                    "recalled_base_units": 0,
                    "earliest_batch": None,
                    "shelf_location": "",
                },
            )
            qty = max(0, int(batch.quantity_base_units))
            res = max(0, int(batch.reserved_quantity_base_units))
            avail = max(0, qty - res)

            if batch.status == BatchStatus.RECALLED or batch.recall_notice_id is not None:
                entry["recalled_base_units"] += qty
            elif batch.is_quarantined or batch.status == BatchStatus.QUARANTINED:
                entry["quarantined_base_units"] += qty
            elif batch.expiry_date <= today or batch.status == BatchStatus.EXPIRED:
                entry["expired_base_units"] += qty
            elif batch.status == BatchStatus.ACTIVE and batch.is_active:
                entry["sellable_base_units"] += qty
                entry["reserved_base_units"] += res
                entry["available_base_units"] += avail
                if avail > 0 and entry["earliest_batch"] is None:
                    entry["earliest_batch"] = batch
                    if batch.storage_location and not entry["shelf_location"]:
                        entry["shelf_location"] = batch.storage_location
        return stats

    @classmethod
    @transaction.atomic
    def refresh_branch_sku_read_model(
        cls,
        actor_user: Any,
        branch_id: Optional[str] = None,
        *,
        product_ids: Optional[List[str]] = None,
        enforce_snapshot_role: bool = False,
    ) -> Dict[str, Any]:
        """
        Synchronizes PosSkuInventoryReadModel for a branch from canonical InventorySnapshotCheckpoint,
        StockLedgerEntry deltas, BatchStock balances, PackagingHierarchyNode, and TenantMedicineConfig.
        Cashier role is strictly blocked when enforce_snapshot_role=True (manual snapshot checkpoint refresh).
        """
        if enforce_snapshot_role:
            role = str(getattr(actor_user, "role", "")).upper()
            if role == "CASHIER" or role not in cls.SNAPSHOT_ADMIN_ROLES:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Cashiers cannot trigger manual inventory snapshot rebuilds.",
                        "message_bn": "ক্যাশিয়ার ম্যানুয়াল ইনভেন্টরি স্ন্যাপশট রিফ্রেশ করতে পারবেন না।",
                    }
                )

        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            actor_user,
            branch_id=branch_id,
            require_write=enforce_snapshot_role,
            allowed_roles=cls.SNAPSHOT_ADMIN_ROLES if enforce_snapshot_role else PosDomainService.POS_READ_ROLES,
        )
        if not branch:
            raise ValidationError({"branch": "Target branch is required to refresh POS SKU inventory snapshots."})

        latest_snapshot = (
            InventorySnapshotCheckpoint.objects.filter(tenant=tenant, branch=branch)
            .order_by("-as_of_timestamp", "-created_at")
            .first()
        )

        batch_stats = cls._build_branch_batch_stats(tenant, branch, product_ids=product_ids)
        ledger_qs = StockLedgerEntry.objects.filter(tenant=tenant, branch=branch)
        if product_ids:
            ledger_qs = ledger_qs.filter(product_id__in=product_ids)
        ledger_sums = {
            row["product_id"]: int(row["delta_sum"] or 0)
            for row in ledger_qs.values("product_id").annotate(delta_sum=Sum("quantity_delta"))
        }

        target_pids = set(batch_stats.keys())
        if product_ids:
            target_pids.update(product_ids)
        else:
            # Include any product with active variant/tenant config or barcode overrides
            cfg_pids = TenantMedicineConfig.objects.filter(tenant=tenant, is_visible=True).values_list(
                "product_id", flat=True
            )
            target_pids.update(cfg_pids)

        if not target_pids:
            return {
                "tenant_id": tenant.tenant_id,
                "branch_id": branch.branch_id,
                "source_snapshot_id": latest_snapshot.snapshot_id if latest_snapshot else None,
                "synced_records": 0,
                "synced_skus": 0,
            }

        products = list(MedicineProduct.objects.filter(product_id__in=target_pids))
        configs = {
            c.product_id: c
            for c in TenantMedicineConfig.objects.filter(tenant=tenant, product_id__in=target_pids)
        }
        variants_by_product: Dict[str, List[MedicineVariant]] = {}
        for var in MedicineVariant.objects.filter(product_id__in=target_pids, is_active=True):
            variants_by_product.setdefault(var.product_id, []).append(var)

        alt_barcodes_by_product: Dict[str, List[str]] = {}
        for alt in BarcodeAlternateIdentifier.objects.filter(
            Q(tenant_id_str=tenant.tenant_id) | Q(tenant_id_str="platform"),
            product_id__in=target_pids,
            is_active=True,
        ):
            alt_barcodes_by_product.setdefault(alt.product_id, []).append(alt.barcode_value)

        now_ts = timezone.now()
        synced_count = 0

        for prod in products:
            cfg = configs.get(prod.product_id)
            if cfg and not cfg.is_visible:
                PosSkuInventoryReadModel.objects.filter(
                    tenant=tenant, branch=branch, product=prod
                ).delete()
                continue

            st = batch_stats.get(
                prod.product_id,
                {
                    "sellable_base_units": 0,
                    "reserved_base_units": 0,
                    "available_base_units": 0,
                    "quarantined_base_units": 0,
                    "expired_base_units": 0,
                    "recalled_base_units": 0,
                    "earliest_batch": None,
                    "shelf_location": "",
                },
            )
            strip_factor, box_factor = cls._resolve_packaging_factors(tenant, branch, prod)
            nd_policy = cls._get_non_divisible_policy(tenant, branch, prod)
            non_divisible_sealed = bool(nd_policy and not nd_policy.is_splitting_allowed)

            effective_brand = (
                cfg.local_brand_name
                if cfg and cfg.local_brand_name and cfg.moderation_status == "APPROVED"
                else prod.brand_name
            )
            effective_price = q2(
                cfg.custom_mrp_unit
                if cfg and cfg.custom_mrp_unit is not None and cfg.moderation_status == "APPROVED"
                else prod.mrp_unit
            )
            shelf_loc = (cfg.shelf_location if cfg and cfg.shelf_location else st["shelf_location"]) or ""
            requires_rx = bool(prod.is_prescription_only or prod.is_narcotic or prod.is_antibiotic)
            earliest_batch = st["earliest_batch"]
            nearest_expiry = earliest_batch.expiry_date if earliest_batch else None
            ledger_sum = ledger_sums.get(prod.product_id, 0)

            var_list = variants_by_product.get(prod.product_id, [])
            targets: List[Optional[MedicineVariant]] = [None] + var_list

            for var in targets:
                if var is None:
                    sku_code = (cfg.local_sku if cfg and cfg.local_sku else "") or ""
                    tokens = {prod.barcode, sku_code, *alt_barcodes_by_product.get(prod.product_id, [])}
                    unit_price = effective_price
                    search_norm = cls._normalize_text(
                        f"{effective_brand} {prod.brand_name} {prod.brand_name_bn} "
                        f"{prod.generic_name} {prod.generic_name_bn} {prod.strength} {prod.dosage_form} {sku_code}"
                    )
                    rm = PosSkuInventoryReadModel.objects.filter(
                        tenant=tenant, branch=branch, product=prod, variant__isnull=True
                    ).first()
                else:
                    sku_code = var.sku
                    tokens = {var.barcode, var.sku, prod.barcode, *alt_barcodes_by_product.get(prod.product_id, [])}
                    unit_price = q2(var.mrp_unit)
                    search_norm = cls._normalize_text(
                        f"{effective_brand} {var.variant_name} {prod.brand_name_bn} "
                        f"{prod.generic_name} {prod.generic_name_bn} {var.strength or prod.strength} {var.sku}"
                    )
                    rm = PosSkuInventoryReadModel.objects.filter(
                        tenant=tenant, branch=branch, product=prod, variant=var
                    ).first()

                clean_tokens = " ".join(sorted(t for t in tokens if t))
                if rm is None:
                    rm = PosSkuInventoryReadModel(
                        tenant=tenant,
                        branch=branch,
                        product=prod,
                        variant=var,
                    )

                rm.source_snapshot = latest_snapshot
                rm.sku_code = sku_code
                rm.barcode_tokens = clean_tokens
                rm.search_text_normalized = search_norm
                rm.brand_name = effective_brand
                rm.brand_name_bn = prod.brand_name_bn or ""
                rm.generic_name = prod.generic_name or ""
                rm.generic_name_bn = prod.generic_name_bn or ""
                rm.strength = (var.strength if var and var.strength else prod.strength) or ""
                rm.dosage_form = prod.dosage_form or ""
                rm.unit_price_bdt = unit_price
                rm.strip_conversion_factor = var.pack_size_numeric if (var and var.pack_size_numeric > 1) else strip_factor
                rm.box_conversion_factor = box_factor
                rm.sellable_base_units = st["sellable_base_units"]
                rm.reserved_base_units = st["reserved_base_units"]
                rm.available_base_units = st["available_base_units"]
                rm.quarantined_base_units = st["quarantined_base_units"]
                rm.expired_base_units = st["expired_base_units"]
                rm.recalled_base_units = st["recalled_base_units"]
                rm.nearest_expiry_date = nearest_expiry
                rm.shelf_location = shelf_loc
                rm.requires_rx_verification = requires_rx
                rm.non_divisible_sealed_only = non_divisible_sealed
                rm.last_ledger_delta_sum = ledger_sum
                rm.snapshot_as_of = now_ts
                rm.save()
                synced_count += 1

        return {
            "tenant_id": tenant.tenant_id,
            "branch_id": branch.branch_id,
            "source_snapshot_id": latest_snapshot.snapshot_id if latest_snapshot else None,
            "synced_records": synced_count,
            "synced_skus": synced_count,
            "snapshot_as_of": now_ts.isoformat(),
        }

    @classmethod
    def _serialize_search_candidate(
        cls,
        *,
        product: MedicineProduct,
        variant: Optional[MedicineVariant],
        tenant: Tenant,
        branch: Branch,
        actor_user: Any,
        batch_stats: Dict[str, Any],
        read_model: Optional[PosSkuInventoryReadModel],
        cfg: Optional[TenantMedicineConfig],
        resolution_source: str,
        resolved_barcode: str,
        resolved_packaging_unit: str,
        resolved_conversion_factor: int,
        rank_score: int,
    ) -> Dict[str, Any]:
        today = timezone.now().date()
        pkg_ctx = PosCartUnitSelectionService.resolve_packaging_context(
            tenant, branch, product, variant=variant
        )
        from apps.core.pos_rx_safety_service import PosRxSafetyService

        rx_cinfo = PosRxSafetyService.classify_product(product)
        nd_policy = pkg_ctx.get("non_divisible_policy")
        non_divisible_sealed = bool(nd_policy and not nd_policy.is_splitting_allowed)

        effective_brand = (
            cfg.local_brand_name
            if cfg and cfg.local_brand_name and cfg.moderation_status == "APPROVED"
            else product.brand_name
        )
        packaging_options, blocked_units = PosCartUnitSelectionService.build_packaging_options(
            tenant=tenant,
            branch=branch,
            product=product,
            variant=variant,
            actor_user=actor_user,
            cfg=cfg,
            pkg_ctx=pkg_ctx,
        )
        base_unit_price = PosCartUnitSelectionService.compute_unit_price_bdt(
            tenant=tenant,
            product=product,
            variant=variant,
            packaging_unit=str(pkg_ctx.get("primary_base_unit") or PosPackagingUnit.TABLET),
            conversion_factor=1,
            pkg_ctx=pkg_ctx,
            cfg=cfg,
        )
        sku_code = (variant.sku if variant else "") or (cfg.local_sku if cfg and cfg.local_sku else "") or product.product_id

        avail = int(batch_stats.get("available_base_units", 0))
        sellable = int(batch_stats.get("sellable_base_units", 0))
        reserved = int(batch_stats.get("reserved_base_units", 0))
        quarantined = int(batch_stats.get("quarantined_base_units", 0))
        expired = int(batch_stats.get("expired_base_units", 0))
        recalled = int(batch_stats.get("recalled_base_units", 0))
        earliest: Optional[BatchStock] = batch_stats.get("earliest_batch")

        is_sellable = avail > 0 and earliest is not None
        block_reason = ""
        if not is_sellable:
            if recalled > 0:
                block_reason = "RECALLED_STOCK_BLOCKED"
            elif quarantined > 0:
                block_reason = "QUARANTINED_STOCK_BLOCKED"
            elif expired > 0:
                block_reason = "EXPIRED_STOCK_BLOCKED"
            else:
                block_reason = "OUT_OF_STOCK"

        near_expiry_warning = bool(earliest and (earliest.expiry_date - today).days <= 30)
        shelf = (cfg.shelf_location if cfg and cfg.shelf_location else batch_stats.get("shelf_location")) or ""

        # Choose safe default packaging option matching allowed policies
        eff_pkg = resolved_packaging_unit
        eff_factor = resolved_conversion_factor
        matching_opt = next((o for o in packaging_options if o["packaging_unit"] == eff_pkg and o["is_allowed"]), None)
        if matching_opt is None:
            first_allowed = next((o for o in packaging_options if o["is_allowed"]), None)
            if first_allowed is not None:
                eff_pkg = first_allowed["packaging_unit"]
                eff_factor = int(first_allowed["conversion_factor"])
                matching_opt = first_allowed

        if matching_opt is not None and matching_opt["conversion_factor"] == eff_factor:
            eff_price = q2(matching_opt["unit_price_bdt"])
        else:
            eff_price = (
                q2(variant.mrp_pack)
                if variant and variant.pack_size_numeric == eff_factor
                else q2(base_unit_price * Decimal(eff_factor))
            )

        can_view_cost = PosDomainService.can_view_cost(actor_user)
        unit_cost_out = None
        if can_view_cost and earliest is not None:
            unit_cost_out = f"{q4(earliest.cost_per_unit):.4f}"

        return {
            "product_id": product.product_id,
            "variant_id": variant.variant_id if variant else None,
            "sku_code": sku_code,
            "display_name": (
                f"{effective_brand} ({variant.pack_size})"
                if variant
                else f"{effective_brand} {product.strength} {product.dosage_form}".strip()
            ),
            "brand_name": effective_brand,
            "brand_name_bn": product.brand_name_bn or "",
            "generic_name": product.generic_name or "",
            "generic_name_bn": product.generic_name_bn or "",
            "strength": (variant.strength if variant and variant.strength else product.strength) or "",
            "dosage_form": product.dosage_form or "",
            "manufacturer": product.manufacturer or "",
            "barcode": resolved_barcode or (variant.barcode if variant and variant.barcode else product.barcode) or "",
            "shelf_location": shelf,
            "resolution_source": resolution_source,
            "resolved_packaging_unit": eff_pkg,
            "resolved_conversion_factor": eff_factor,
            "conversion_source": pkg_ctx["default_source"],
            "unit_price_bdt": f"{eff_price:.2f}",
            "base_unit_price_bdt": f"{base_unit_price:.2f}",
            "unit_cost_bdt": unit_cost_out,
            "packaging_options": packaging_options,
            "blocked_units": blocked_units,
            "sellable_base_units": sellable,
            "reserved_base_units": reserved,
            "available_base_units": avail,
            "quarantined_base_units": quarantined,
            "expired_base_units": expired,
            "recalled_base_units": recalled,
            "earliest_batch_id": earliest.batch_id if earliest else None,
            "earliest_batch_number": earliest.batch_number if earliest else "",
            "earliest_expiry_date": earliest.expiry_date.isoformat() if earliest else None,
            "near_expiry_warning": near_expiry_warning,
            "requires_rx_verification": bool(rx_cinfo["requires_rx_verification"]),
            "regulatory_classification": rx_cinfo["regulatory_classification"],
            "non_divisible_sealed_only": non_divisible_sealed,
            "is_sellable": is_sellable,
            "block_reason": block_reason,
            "snapshot_read_model_id": read_model.read_model_id if read_model else None,
            "snapshot_checksum": read_model.sha256_checksum if read_model else "",
            "snapshot_as_of": read_model.snapshot_as_of.isoformat() if read_model and read_model.snapshot_as_of else None,
            "rank_score": rank_score,
        }

    @classmethod
    def fast_search(
        cls,
        user: Any,
        query: str,
        *,
        branch_id: Optional[str] = None,
        limit: int = 15,
        include_zero_stock: bool = True,
        record_scan_event: bool = False,
        sale: Optional[PosSaleOrder] = None,
    ) -> Dict[str, Any]:
        """
        Sub-second POS barcode/SKU/brand/generic search enriched with branch FEFO sellable stock
        and PosSkuInventoryReadModel snapshot metadata.
        """
        started = time.perf_counter()
        raw_query = str(query or "").strip()
        if not raw_query:
            raise ValidationError(
                {
                    "q": "Search or scan query cannot be empty.",
                    "message_bn": "অনুসন্ধান বা বারকোড ইনপুট খালি রাখা যাবে না।",
                }
            )

        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        if not branch:
            raise ValidationError({"branch": "Target branch is required for POS inventory search."})

        try:
            safe_limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            safe_limit = 15

        norm_q = cls._normalize_text(raw_query)
        variants_q = cls._transliteration_variants(raw_query)

        configs = {
            c.product_id: c
            for c in TenantMedicineConfig.objects.filter(tenant=tenant)
        }
        hidden_pids = {pid for pid, cfg in configs.items() if not cfg.is_visible}

        # Collect ranked candidate tuples: (product, variant, resolution_source, barcode, pkg_unit, factor, score)
        candidates: List[Tuple[MedicineProduct, Optional[MedicineVariant], str, str, str, int, int]] = []
        seen_keys: set[Tuple[str, Optional[str]]] = set()

        def _add_candidate(
            prod: Optional[MedicineProduct],
            var: Optional[MedicineVariant],
            res_source: str,
            barcode_val: str,
            pkg_unit: str,
            factor: int,
            score: int,
        ) -> None:
            if prod is None or prod.product_id in hidden_pids:
                return
            key = (prod.product_id, var.variant_id if var else None)
            if key in seen_keys:
                return
            seen_keys.add(key)
            candidates.append((prod, var, res_source, barcode_val, pkg_unit, max(1, int(factor)), score))

        # 1. Exact BarcodeAlternateIdentifier (ordered by priority_order ASC)
        alt_hits = (
            BarcodeAlternateIdentifier.objects.select_related("product", "variant")
            .filter(
                Q(tenant_id_str=tenant.tenant_id) | Q(tenant_id_str="platform"),
                barcode_value__iexact=raw_query,
                is_active=True,
            )
            .order_by("priority_order", "-version_number")
        )
        for alt in alt_hits:
            prod = alt.product or (alt.variant.product if alt.variant else None)
            if prod:
                pkg = cls._map_packaging_level(alt.packaging_level)
                _add_candidate(
                    prod,
                    alt.variant,
                    PosScanResolutionSource.BARCODE_ALTERNATE,
                    alt.barcode_value,
                    pkg,
                    alt.pack_quantity_multiplier or 1,
                    120,
                )

        # 2. Exact PackagingHierarchyNode tier GTIN barcode
        today = timezone.now().date()
        tier_hits = (
            PackagingHierarchyNode.objects.select_related("product")
            .filter(
                tenant=tenant,
                is_active=True,
                tier_gtin_barcode__iexact=raw_query,
                effective_from__lte=today,
            )
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
            .filter(Q(branch=branch) | Q(branch__isnull=True))
            .order_by("-branch_id", "tier_level")
        )
        for node in tier_hits:
            pkg = cls._map_packaging_level(node.parent_unit)
            strip_f, box_f = cls._resolve_packaging_factors(tenant, branch, node.product)
            factor = box_f if pkg == PosPackagingUnit.BOX else node.conversion_factor
            _add_candidate(
                node.product,
                None,
                PosScanResolutionSource.PACKAGING_TIER_GTIN,
                node.tier_gtin_barcode,
                pkg,
                factor,
                115,
            )

        # 3. Exact or matching MedicineVariant SKU / Barcode
        var_filter = (
            Q(sku__iexact=raw_query)
            | Q(barcode__iexact=raw_query)
            | Q(variant_name__icontains=norm_q)
        )
        for var in (
            MedicineVariant.objects.select_related("product")
            .filter(var_filter, is_active=True)
            .exclude(product_id__in=hidden_pids)[:20]
        ):
            is_exact_sku = var.sku.lower() == raw_query.lower()
            is_exact_bc = bool(var.barcode and var.barcode.lower() == raw_query.lower())
            score = 110 if (is_exact_sku or is_exact_bc) else (85 if var.variant_name.lower().startswith(norm_q) else 65)
            source = (
                PosScanResolutionSource.VARIANT_SKU_EXACT
                if (is_exact_sku or is_exact_bc)
                else PosScanResolutionSource.BRAND_GENERIC_SEARCH
            )
            pkg = PosPackagingUnit.STRIP if var.pack_size_numeric > 1 else PosPackagingUnit.TABLET
            _add_candidate(var.product, var, source, var.barcode or var.sku, pkg, var.pack_size_numeric or 1, score)

        # 4. Product & TenantMedicineConfig Barcode / Local SKU / EN-BN Brand & Generic / Phonetic
        local_match_pids = [
            cfg.product_id
            for cfg in configs.values()
            if cfg.is_visible
            and (
                (cfg.local_sku and norm_q in cls._normalize_text(cfg.local_sku))
                or (cfg.local_brand_name and norm_q in cls._normalize_text(cfg.local_brand_name))
            )
        ]

        prod_q = (
            Q(barcode__iexact=raw_query)
            | Q(barcode__icontains=raw_query)
            | Q(brand_name__icontains=norm_q)
            | Q(brand_name_bn__icontains=norm_q)
            | Q(generic_name__icontains=norm_q)
            | Q(generic_name_bn__icontains=norm_q)
            | Q(strength__icontains=norm_q)
        )
        for v_term in variants_q:
            prod_q |= (
                Q(brand_name__icontains=v_term)
                | Q(brand_name_bn__icontains=v_term)
                | Q(generic_name__icontains=v_term)
                | Q(generic_name_bn__icontains=v_term)
            )
        if local_match_pids:
            prod_q |= Q(product_id__in=local_match_pids)

        for prod in (
            MedicineProduct.objects.filter(prod_q)
            .exclude(product_id__in=hidden_pids)
            .distinct()[:40]
        ):
            cfg = configs.get(prod.product_id)
            local_sku = cfg.local_sku if cfg else ""
            if prod.barcode and prod.barcode.lower() == raw_query.lower():
                res_source = PosScanResolutionSource.PRODUCT_BARCODE_EXACT
                score = 108
            elif local_sku and local_sku.lower() == raw_query.lower():
                res_source = PosScanResolutionSource.LOCAL_SKU_EXACT
                score = 105
            elif (
                prod.brand_name.lower().startswith(norm_q)
                or (prod.brand_name_bn and prod.brand_name_bn.startswith(norm_q))
                or (cfg and cfg.local_brand_name and cfg.local_brand_name.lower().startswith(norm_q))
            ):
                res_source = PosScanResolutionSource.BRAND_GENERIC_SEARCH
                score = 95
            elif prod.generic_name.lower().startswith(norm_q) or (
                prod.generic_name_bn and prod.generic_name_bn.startswith(norm_q)
            ):
                res_source = PosScanResolutionSource.BRAND_GENERIC_SEARCH
                score = 85
            elif any(
                v in prod.brand_name.lower()
                or (prod.brand_name_bn and v in prod.brand_name_bn)
                or v in prod.generic_name.lower()
                for v in variants_q
            ):
                res_source = PosScanResolutionSource.BRAND_GENERIC_SEARCH
                score = 75
            else:
                res_source = PosScanResolutionSource.BRAND_GENERIC_SEARCH
                score = 60

            _add_candidate(
                prod,
                None,
                res_source,
                prod.barcode or local_sku or "",
                PosPackagingUnit.TABLET,
                1,
                score,
            )

        matched_pids = list({c[0].product_id for c in candidates})
        batch_stats_map = cls._build_branch_batch_stats(tenant, branch, product_ids=matched_pids) if matched_pids else {}

        # Read or synchronize read-models lazily for matched products so snapshot metadata stays fresh
        read_models: Dict[Tuple[str, Optional[str]], PosSkuInventoryReadModel] = {}
        if matched_pids:
            existing_rms = list(
                PosSkuInventoryReadModel.objects.filter(
                    tenant=tenant,
                    branch=branch,
                    product_id__in=matched_pids,
                )
            )
            missing_pids = set(matched_pids) - {rm.product_id for rm in existing_rms}
            if missing_pids:
                cls.refresh_branch_sku_read_model(
                    user,
                    branch_id=branch.branch_id,
                    product_ids=list(missing_pids),
                    enforce_snapshot_role=False,
                )
                existing_rms = list(
                    PosSkuInventoryReadModel.objects.filter(
                        tenant=tenant,
                        branch=branch,
                        product_id__in=matched_pids,
                    )
                )
            for rm in existing_rms:
                read_models[(rm.product_id, rm.variant_id)] = rm

        serialized_results: List[Dict[str, Any]] = []
        for prod, var, res_src, bc_val, pkg_u, factor, score in candidates:
            st = batch_stats_map.get(prod.product_id, {})
            if not include_zero_stock and int(st.get("available_base_units", 0)) <= 0:
                continue
            rm = read_models.get((prod.product_id, var.variant_id if var else None)) or read_models.get((prod.product_id, None))
            serialized_results.append(
                cls._serialize_search_candidate(
                    product=prod,
                    variant=var,
                    tenant=tenant,
                    branch=branch,
                    actor_user=user,
                    batch_stats=st,
                    read_model=rm,
                    cfg=configs.get(prod.product_id),
                    resolution_source=res_src,
                    resolved_barcode=bc_val,
                    resolved_packaging_unit=pkg_u,
                    resolved_conversion_factor=factor,
                    rank_score=score,
                )
            )

        serialized_results.sort(
            key=lambda x: (-int(x["rank_score"]), 0 if x["is_sellable"] else 1, x["display_name"])
        )
        sliced = serialized_results[:safe_limit]
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

        if record_scan_event:
            top = sliced[0] if sliced else None
            PosCartScanEvent.objects.create(
                tenant=tenant,
                branch=branch,
                sale=sale,
                cashier_user=user,
                raw_query=raw_query[:128],
                resolution_source=top["resolution_source"] if top else PosScanResolutionSource.UNMATCHED,
                matched_product_id=top["product_id"] if top else None,
                matched_variant_id=top["variant_id"] if top else None,
                packaging_unit=top["resolved_packaging_unit"] if top else PosPackagingUnit.TABLET,
                conversion_factor=top["resolved_conversion_factor"] if top else 1,
                quantity_delta=0,
                latency_ms=Decimal(str(elapsed_ms)),
                used_snapshot_cache=bool(top and top.get("snapshot_read_model_id")),
                was_rejected=top is None,
                rejection_code="" if top else "ERR_BARCODE_NOT_FOUND",
                rejection_message="" if top else f"No matching medicine or SKU found for '{raw_query}'.",
            )

        return {
            "query": raw_query,
            "tenant_id": tenant.tenant_id,
            "branch_id": branch.branch_id,
            "total_matches": len(serialized_results),
            "latency_ms": elapsed_ms,
            "results": sliced,
        }

    @classmethod
    def _resolve_exact_item_for_scan(
        cls,
        *,
        tenant: Tenant,
        branch: Branch,
        raw_query: str,
        product_id: Optional[str],
        variant_id: Optional[str],
    ) -> Optional[Tuple[MedicineProduct, Optional[MedicineVariant], str, str, str, int]]:
        configs = {
            c.product_id: c
            for c in TenantMedicineConfig.objects.filter(tenant=tenant)
        }
        hidden_pids = {pid for pid, cfg in configs.items() if not cfg.is_visible}

        if product_id:
            prod = MedicineProduct.objects.filter(product_id=str(product_id).strip()).first()
            if not prod or prod.product_id in hidden_pids:
                return None
            var = None
            if variant_id:
                var = MedicineVariant.objects.filter(
                    variant_id=str(variant_id).strip(), product=prod, is_active=True
                ).first()
                if not var:
                    raise ValidationError({"variant": f"Variant '{variant_id}' does not belong to product '{product_id}'."})
            norm_form = PosCartUnitSelectionService.normalize_dosage_form(prod)
            base_pkg = PosCartUnitSelectionService.FORM_BASE_UNIT.get(norm_form, PosPackagingUnit.TABLET)
            is_solid = norm_form in ("TABLET", "CAPSULE", "")
            pkg = (
                (PosPackagingUnit.STRIP if is_solid else PosPackagingUnit.BOX)
                if (var and var.pack_size_numeric > 1)
                else base_pkg
            )
            factor = var.pack_size_numeric if (var and var.pack_size_numeric > 1) else 1
            return (
                prod,
                var,
                PosScanResolutionSource.VARIANT_SKU_EXACT if var else PosScanResolutionSource.PRODUCT_BARCODE_EXACT,
                raw_query or (var.barcode if var else prod.barcode) or "",
                pkg,
                factor,
            )

        if not raw_query:
            return None

        # Priority 1: BarcodeAlternateIdentifier
        alt = (
            BarcodeAlternateIdentifier.objects.select_related("product", "variant")
            .filter(
                Q(tenant_id_str=tenant.tenant_id) | Q(tenant_id_str="platform"),
                barcode_value__iexact=raw_query,
                is_active=True,
            )
            .order_by("priority_order", "-version_number")
            .first()
        )
        if alt:
            prod = alt.product or (alt.variant.product if alt.variant else None)
            if prod and prod.product_id not in hidden_pids:
                return (
                    prod,
                    alt.variant,
                    PosScanResolutionSource.BARCODE_ALTERNATE,
                    alt.barcode_value,
                    cls._map_packaging_level(alt.packaging_level),
                    int(alt.pack_quantity_multiplier or 1),
                )

        # Priority 2: PackagingHierarchyNode tier GTIN
        today = timezone.now().date()
        tier = (
            PackagingHierarchyNode.objects.select_related("product")
            .filter(
                tenant=tenant,
                is_active=True,
                tier_gtin_barcode__iexact=raw_query,
                effective_from__lte=today,
            )
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
            .filter(Q(branch=branch) | Q(branch__isnull=True))
            .order_by("-branch_id", "tier_level")
            .first()
        )
        if tier and tier.product_id not in hidden_pids:
            pkg = cls._map_packaging_level(tier.parent_unit)
            strip_f, box_f = cls._resolve_packaging_factors(tenant, branch, tier.product)
            factor = box_f if pkg == PosPackagingUnit.BOX else tier.conversion_factor
            return (
                tier.product,
                None,
                PosScanResolutionSource.PACKAGING_TIER_GTIN,
                tier.tier_gtin_barcode,
                pkg,
                int(factor),
            )

        # Priority 3: Variant SKU or barcode
        var = (
            MedicineVariant.objects.select_related("product")
            .filter(Q(sku__iexact=raw_query) | Q(barcode__iexact=raw_query), is_active=True)
            .exclude(product_id__in=hidden_pids)
            .first()
        )
        if var:
            pkg = PosPackagingUnit.STRIP if var.pack_size_numeric > 1 else PosPackagingUnit.TABLET
            return (
                var.product,
                var,
                PosScanResolutionSource.VARIANT_SKU_EXACT,
                var.barcode or var.sku,
                pkg,
                int(var.pack_size_numeric or 1),
            )

        # Priority 4: Master Product barcode or Tenant local_sku
        prod = MedicineProduct.objects.filter(barcode__iexact=raw_query).exclude(product_id__in=hidden_pids).first()
        if prod:
            norm_form = PosCartUnitSelectionService.normalize_dosage_form(prod)
            base_pkg = PosCartUnitSelectionService.FORM_BASE_UNIT.get(norm_form, PosPackagingUnit.TABLET)
            return (prod, None, PosScanResolutionSource.PRODUCT_BARCODE_EXACT, prod.barcode, base_pkg, 1)

        cfg = (
            TenantMedicineConfig.objects.select_related("product")
            .filter(tenant=tenant, is_visible=True, local_sku__iexact=raw_query)
            .first()
        )
        if cfg:
            norm_form = PosCartUnitSelectionService.normalize_dosage_form(cfg.product)
            base_pkg = PosCartUnitSelectionService.FORM_BASE_UNIT.get(norm_form, PosPackagingUnit.TABLET)
            return (cfg.product, None, PosScanResolutionSource.LOCAL_SKU_EXACT, cfg.local_sku, base_pkg, 1)

        return None

    @classmethod
    def _validate_sellable_stock_and_policy(
        cls,
        *,
        tenant: Tenant,
        branch: Branch,
        product: MedicineProduct,
        packaging_unit: str,
        conversion_factor: int,
        total_product_base_units_in_cart: int,
    ) -> Tuple[BatchStock, int]:
        """
        Locks eligible FEFO batches and enforces expired/quarantine/recall exclusion +
        NonDivisiblePackPolicy splitting rules.
        """
        nd_policy = cls._get_non_divisible_policy(tenant, branch, product)
        if nd_policy and not nd_policy.is_splitting_allowed and conversion_factor == 1 and packaging_unit in (
            PosPackagingUnit.TABLET,
            PosPackagingUnit.UNIT,
        ):
            raise ValidationError(
                {
                    "error_code": "ERR_NON_DIVISIBLE_PACK",
                    "packaging_unit": (
                        f"'{product.brand_name}' cannot be split into loose base units ({nd_policy.prohibition_reason}). "
                        f"Select sealed pack/box instead."
                    ),
                }
            )

        today = timezone.now().date()
        all_batches = list(
            BatchStock.objects.select_for_update()
            .filter(tenant=tenant, branch=branch, product=product)
            .order_by("expiry_date", "batch_id")
        )
        eligible: List[BatchStock] = []
        expired_qty = 0
        quarantined_qty = 0
        recalled_qty = 0

        for b in all_batches:
            if b.status == BatchStatus.RECALLED or b.recall_notice_id is not None:
                recalled_qty += b.quantity_base_units
            elif b.is_quarantined or b.status == BatchStatus.QUARANTINED:
                quarantined_qty += b.quantity_base_units
            elif b.expiry_date <= today or b.status == BatchStatus.EXPIRED:
                expired_qty += b.quantity_base_units
            elif b.status == BatchStatus.ACTIVE and b.is_active and b.available_quantity_base_units > 0:
                eligible.append(b)

        total_avail = sum(b.available_quantity_base_units for b in eligible)
        if total_avail <= 0:
            raise ValidationError(
                {
                    "error_code": "ERR_EXPIRED_OR_UNSELLABLE_STOCK",
                    "stock": (
                        f"No sellable FEFO stock available for '{product.brand_name}' in branch '{branch.name}' "
                        f"(expired={expired_qty}, quarantined={quarantined_qty}, recalled={recalled_qty})."
                    ),
                }
            )

        if total_product_base_units_in_cart > total_avail:
            raise ValidationError(
                {
                    "error_code": "ERR_INSUFFICIENT_SELLABLE_STOCK",
                    "quantity_units": (
                        f"Requested {total_product_base_units_in_cart} base units of '{product.brand_name}', "
                        f"but only {total_avail} eligible sellable units exist in branch '{branch.name}'."
                    ),
                }
            )

        return eligible[0], total_avail

    @classmethod
    def _record_scan_rejection(
        cls,
        *,
        tenant: Tenant,
        branch: Branch,
        sale: Optional[PosSaleOrder],
        user: Any,
        raw_query: str,
        resolution_source: str,
        product: Optional[MedicineProduct],
        variant: Optional[MedicineVariant],
        packaging_unit: str,
        factor: int,
        qty_delta: int,
        latency_ms: float,
        err_code: str,
        err_msg: str,
    ) -> None:
        PosCartScanEvent.objects.create(
            tenant=tenant,
            branch=branch,
            sale=sale,
            cashier_user=user,
            raw_query=str(raw_query or "")[:128],
            resolution_source=resolution_source,
            matched_product=product,
            matched_variant=variant,
            packaging_unit=packaging_unit,
            conversion_factor=max(1, factor),
            quantity_delta=qty_delta,
            latency_ms=Decimal(str(round(latency_ms, 2))),
            used_snapshot_cache=True,
            was_rejected=True,
            rejection_code=err_code[:64],
            rejection_message=err_msg[:255],
        )

    @classmethod
    @transaction.atomic
    def get_or_create_quick_cart(cls, user: Any, data: Dict[str, Any]) -> Tuple[PosSaleOrder, bool]:
        """
        Retrieves an existing open DRAFT keyboard cart for the user/branch or creates a new empty DRAFT cart.
        Empty carts cannot be allocated/finalized until at least one sellable item is scanned/added.
        """
        branch_id = data.get("branch") or data.get("branch_id")
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
        )
        if not branch:
            raise ValidationError({"branch": "Target branch is required to initialize a POS cart."})

        idem_key = str(data.get("idempotency_key") or "").strip()
        if idem_key:
            existing = (
                PosSaleOrder.objects.filter(tenant=tenant, idempotency_key=idem_key)
                .prefetch_related("lines__product", "lines__variant", "allocations", "payment_attempts")
                .first()
            )
            if existing:
                return existing, False

        off_ns = str(data.get("offline_device_namespace") or "").strip()
        if not bool(data.get("force_new", False)):
            open_qs = PosSaleOrder.objects.filter(
                tenant=tenant,
                branch=branch,
                cashier_user=user,
                lifecycle_state=PosSaleLifecycleState.DRAFT,
                is_archived=False,
            )
            if off_ns:
                open_qs = open_qs.filter(offline_device_namespace=off_ns)
            existing_open = open_qs.order_by("-updated_at").first()
            if existing_open:
                return existing_open, False

        customer = None
        customer_id = data.get("customer") or data.get("customer_id")
        if customer_id:
            customer = PosCustomer.objects.filter(customer_id=customer_id, tenant=tenant, is_archived=False).first()
            if not customer:
                raise ValidationError({"customer": f"Customer '{customer_id}' not found in tenant."})

        register_assignment = None
        reg_id = data.get("register_assignment") or data.get("register_assignment_id")
        if reg_id:
            register_assignment = CashierRegisterAssignment.objects.filter(
                assignment_id=reg_id, tenant=tenant, branch=branch
            ).first()

        device = None
        dev_id = data.get("device") or data.get("device_id")
        if dev_id:
            device = LoginDeviceRegistration.objects.filter(
                registration_id=dev_id, tenant=tenant, branch=branch
            ).first()

        cashier_staff = StaffMember.objects.filter(tenant=tenant, username=getattr(user, "email", "")).first()
        sale = PosSaleOrder(
            sale_id=f"sale-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            idempotency_key=idem_key,
            offline_device_namespace=off_ns,
            offline_local_sequence=data.get("offline_local_sequence"),
            device=device,
            register_assignment=register_assignment,
            cashier_user=user,
            cashier_staff=cashier_staff,
            customer=customer,
            customer_name=str(data.get("customer_name") or (customer.full_name if customer else "Walk-in Customer")),
            customer_phone=str(data.get("customer_phone") or (customer.phone if customer else "")),
            source_channel=str(data.get("source_channel") or PosSaleSourceChannel.DESKTOP_POS_ONLINE),
            sync_status=PosSyncState.CLOUD_CONFIRMED,
            lifecycle_state=PosSaleLifecycleState.DRAFT,
            notes=str(data.get("notes") or "Keyboard quick cart"),
            created_by=user,
            updated_by=user,
        )
        if bool(data.get("apply_branch_tax_defaults", False)):
            from apps.core.pos_cart_pricing_service import PosCartPricingService

            tax_defaults = PosCartPricingService.resolve_branch_tax_defaults(branch)
            sale.tax_mode = tax_defaults["tax_mode"]
            sale.tax_rate_pct = q2(tax_defaults["default_vat_rate_pct"])
        sale.save()

        PosDomainService._record_lifecycle_audit(
            sale=sale,
            from_state="",
            to_state=sale.lifecycle_state,
            action="QUICK_CART_OPENED",
            user=user,
            reason="Opened keyboard-first POS cart",
        )
        return sale, True

    @classmethod
    def scan_add_to_cart(
        cls,
        user: Any,
        sale: PosSaleOrder,
        data: Dict[str, Any],
    ) -> Tuple[PosSaleOrder, PosCartScanEvent]:
        """
        Resolves a scanned barcode, SKU, or selected product/variant and merges or adds
        it to a mutable PosSaleOrder cart with row locking, optimistic version verification,
        FEFO eligible stock checks, and NonDivisiblePackPolicy checks.
        """
        started = time.perf_counter()
        raw_query = str(data.get("barcode") or data.get("sku") or data.get("query") or "").strip()
        product_id = data.get("product") or data.get("product_id")
        variant_id = data.get("variant") or data.get("variant_id")

        try:
            with transaction.atomic():
                locked_sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
                tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
                    user,
                    branch_id=locked_sale.branch_id,
                    require_write=True,
                    allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
                )
                if not branch:
                    raise ValidationError({"branch": "Sale order branch is invalid."})

                if locked_sale.lifecycle_state not in PosDomainService.MUTABLE_SALE_STATES:
                    raise ValidationError(
                        {
                            "error_code": "ERR_IMMUTABLE_SALE_STATE",
                            "detail": f"Cannot scan items into sale '{locked_sale.order_number}' in state '{locked_sale.lifecycle_state}'.",
                        }
                    )

                expected_ver = data.get("expected_version")
                if expected_ver is not None and int(expected_ver) != int(locked_sale.version):
                    elapsed = round((time.perf_counter() - started) * 1000, 2)
                    raise _ScanRejectionAbort(
                        log_kwargs={
                            "tenant": tenant,
                            "branch": branch,
                            "sale": locked_sale,
                            "user": user,
                            "raw_query": raw_query or str(product_id or ""),
                            "resolution_source": PosScanResolutionSource.UNMATCHED,
                            "product": None,
                            "variant": None,
                            "packaging_unit": PosPackagingUnit.TABLET,
                            "factor": 1,
                            "qty_delta": 0,
                            "latency_ms": elapsed,
                            "err_code": "ERR_CART_VERSION_CONFLICT",
                            "err_msg": f"Stale cart version {expected_ver}; current version is {locked_sale.version}.",
                        },
                        exc=ValidationError(
                            {
                                "error_code": "ERR_CART_VERSION_CONFLICT",
                                "expected_version": expected_ver,
                                "current_version": locked_sale.version,
                                "detail": "Cart was updated concurrently; refresh cart before retrying.",
                            }
                        ),
                    )

                raw_qty = data.get("quantity_delta", data.get("quantity_units", 1))
                qty_delta = PosCartUnitSelectionService.parse_strict_positive_int(raw_qty, "quantity_delta")

                resolved = cls._resolve_exact_item_for_scan(
                    tenant=tenant,
                    branch=branch,
                    raw_query=raw_query,
                    product_id=product_id,
                    variant_id=variant_id,
                )
                if not resolved:
                    elapsed = round((time.perf_counter() - started) * 1000, 2)
                    raise _ScanRejectionAbort(
                        log_kwargs={
                            "tenant": tenant,
                            "branch": branch,
                            "sale": locked_sale,
                            "user": user,
                            "raw_query": raw_query or str(product_id or ""),
                            "resolution_source": PosScanResolutionSource.UNMATCHED,
                            "product": None,
                            "variant": None,
                            "packaging_unit": PosPackagingUnit.TABLET,
                            "factor": 1,
                            "qty_delta": qty_delta,
                            "latency_ms": elapsed,
                            "err_code": "ERR_BARCODE_NOT_FOUND",
                            "err_msg": f"Barcode or SKU '{raw_query}' did not match any active product.",
                        },
                        exc=ValidationError(
                            {
                                "error_code": "ERR_BARCODE_NOT_FOUND",
                                "barcode": f"Barcode or SKU '{raw_query}' not found.",
                                "message_bn": f"'{raw_query}' বারকোড বা SKU পাওয়া যায়নি।",
                            }
                        ),
                    )

                prod, var, res_source, matched_barcode, default_pkg, default_factor = resolved
                req_pkg = str(data.get("packaging_unit") or default_pkg).strip().upper()
                raw_conv_factor = data.get("conversion_factor")
                if raw_conv_factor is None and req_pkg == default_pkg and default_factor > 1:
                    raw_conv_factor = default_factor

                existing_lines = list(locked_sale.lines.select_related("product", "variant").order_by("line_number"))
                existing_same_unit_line = next(
                    (
                        l
                        for l in existing_lines
                        if l.product_id == prod.product_id
                        and l.variant_id == (var.variant_id if var else None)
                        and l.packaging_unit == req_pkg
                    ),
                    None,
                )
                effective_line_qty = (
                    int(existing_same_unit_line.quantity_units) + qty_delta
                    if existing_same_unit_line is not None
                    else qty_delta
                )

                override_reason = str(data.get("override_reason") or "").strip()[:255]
                try:
                    evaluated_unit = PosCartUnitSelectionService.evaluate_unit_selection(
                        tenant=tenant,
                        branch=branch,
                        product=prod,
                        variant=var,
                        packaging_unit=req_pkg,
                        requested_conversion_factor=raw_conv_factor,
                        quantity_units=effective_line_qty,
                        actor_user=user,
                        override_reason=override_reason,
                    )
                    conv_factor = int(evaluated_unit["conversion_factor"])
                    current_prod_base_units = sum(
                        l.base_units_total for l in existing_lines if l.product_id == prod.product_id
                    )
                    added_base_units = qty_delta * conv_factor
                    new_total_prod_base = current_prod_base_units + added_base_units

                    earliest_batch, _ = cls._validate_sellable_stock_and_policy(
                        tenant=tenant,
                        branch=branch,
                        product=prod,
                        packaging_unit=req_pkg,
                        conversion_factor=conv_factor,
                        total_product_base_units_in_cart=new_total_prod_base,
                    )
                except (ValidationError, PermissionDenied) as exc:
                    elapsed = round((time.perf_counter() - started) * 1000, 2)
                    detail_dict = exc.detail if isinstance(exc.detail, dict) else {"detail": str(exc.detail)}
                    err_code = str(detail_dict.get("error_code") or "ERR_STOCK_OR_POLICY_BLOCKED")
                    fallback_factor = default_factor if default_factor > 1 else 1
                    raise _ScanRejectionAbort(
                        log_kwargs={
                            "tenant": tenant,
                            "branch": branch,
                            "sale": locked_sale,
                            "user": user,
                            "raw_query": raw_query or matched_barcode,
                            "resolution_source": res_source,
                            "product": prod,
                            "variant": var,
                            "packaging_unit": req_pkg if req_pkg in PosPackagingUnit.values else PosPackagingUnit.TABLET,
                            "factor": fallback_factor,
                            "qty_delta": qty_delta,
                            "latency_ms": elapsed,
                            "err_code": err_code,
                            "err_msg": str(detail_dict),
                        },
                        exc=exc,
                    ) from exc

                canonical_unit_price = q2(evaluated_unit["unit_price_bdt"])
                line_price_source = "CANONICAL_MRP"
                line_override_by = None
                line_override_reason = ""
                if data.get("unit_price_bdt") is not None:
                    from apps.core.pos_cart_pricing_service import PosCartPricingService, parse_strict_decimal

                    req_price = parse_strict_decimal(data["unit_price_bdt"], "unit_price_bdt", min_val=Decimal("0.01"))
                    if req_price != canonical_unit_price:
                        actor_role = str(getattr(user, "role", "")).upper()
                        if actor_role not in PosCartPricingService.ELEVATED_PRICING_ROLES:
                            raise PermissionDenied(
                                {
                                    "error_code": "ERR_PRICE_OVERRIDE_REQUIRES_APPROVAL",
                                    "message_en": (
                                        "Cashiers cannot override canonical MRP price; Branch Manager, Owner, "
                                        "or Pharmacist approval is required."
                                    ),
                                    "message_bn": "ক্যাশিয়ার অনুমোদন ছাড়া নির্ধারিত মূল্য পরিবর্তন করতে পারবেন না।",
                                }
                            )
                        line_price_source = "MANAGER_OVERRIDE"
                        line_override_by = user
                        line_override_reason = str(data.get("override_reason") or "Manager price override")[:255]
                    resolved_unit_price = req_price
                else:
                    resolved_unit_price = canonical_unit_price

                unit_cost = q4(earliest_batch.cost_per_unit)

                matching_line = next(
                    (
                        l
                        for l in existing_lines
                        if l.product_id == prod.product_id
                        and l.variant_id == (var.variant_id if var else None)
                        and l.packaging_unit == evaluated_unit["packaging_unit"]
                        and int(l.conversion_factor) == conv_factor
                    ),
                    None,
                )

                PosDomainService._release_reserved_allocations(locked_sale)

                if matching_line:
                    matching_line.quantity_units += qty_delta
                    matching_line.base_units_total = matching_line.quantity_units * conv_factor
                    if matched_barcode and not matching_line.scanned_barcode:
                        matching_line.scanned_barcode = matched_barcode[:64]
                    matching_line.dosage_form_snapshot = evaluated_unit["dosage_form_snapshot"]
                    matching_line.container_policy_applied = evaluated_unit["container_policy_applied"]
                    matching_line.unit_conversion_source = evaluated_unit["conversion_source"]
                    if evaluated_unit["override_approved_by"] is not None:
                        matching_line.container_override_approved_by = evaluated_unit["override_approved_by"]
                        matching_line.container_override_reason = evaluated_unit["override_reason"]
                    matching_line.line_subtotal_bdt = q2(Decimal(matching_line.quantity_units) * matching_line.unit_price_bdt)
                    matching_line.line_total_bdt = matching_line.line_subtotal_bdt
                    matching_line.save()
                else:
                    next_line_no = max([l.line_number for l in existing_lines], default=0) + 1
                    PosSaleOrderLine.objects.create(
                        line_id=f"sline-{uuid.uuid4().hex[:12]}",
                        tenant=tenant,
                        branch=branch,
                        sale=locked_sale,
                        line_number=next_line_no,
                        product=prod,
                        variant=var,
                        scanned_barcode=(matched_barcode or raw_query)[:64],
                        preferred_batch=None,
                        packaging_unit=evaluated_unit["packaging_unit"],
                        quantity_units=qty_delta,
                        conversion_factor=conv_factor,
                        base_units_total=qty_delta * conv_factor,
                        unit_price_bdt=resolved_unit_price,
                        tax_mode=locked_sale.tax_mode,
                        tax_rate_pct=q2(data.get("tax_rate_pct", locked_sale.tax_rate_pct if locked_sale.tax_rate_pct else "0.00")),
                        line_subtotal_bdt=q2(Decimal(qty_delta) * resolved_unit_price),
                        line_total_bdt=q2(Decimal(qty_delta) * resolved_unit_price),
                        price_source=line_price_source,
                        price_override_approved_by=line_override_by,
                        price_override_reason=line_override_reason,
                        unit_cost_bdt=unit_cost,
                        requires_rx_verification=bool(
                            prod.is_prescription_only or prod.is_narcotic or prod.is_antibiotic
                        ),
                        dosage_form_snapshot=evaluated_unit["dosage_form_snapshot"],
                        container_policy_applied=evaluated_unit["container_policy_applied"],
                        unit_conversion_source=evaluated_unit["conversion_source"],
                        container_override_approved_by=evaluated_unit["override_approved_by"],
                        container_override_reason=evaluated_unit["override_reason"],
                    )

                PosDomainService.compute_line_and_header_totals(locked_sale, user, allow_empty=True)
                locked_sale.refresh_from_db()

                from apps.core.pos_rx_safety_service import PosRxSafetyService

                PosRxSafetyService.evaluate_and_sync_sale_rx_gate(
                    locked_sale, actor_user=user, mutate=True, reason="Cart line added/scanned"
                )
                locked_sale.refresh_from_db()

                locked_sale.version += 1
                locked_sale.updated_by = user
                locked_sale.save(update_fields=["version", "updated_by", "updated_at"])

                elapsed = round((time.perf_counter() - started) * 1000, 2)
                scan_event = PosCartScanEvent.objects.create(
                    tenant=tenant,
                    branch=branch,
                    sale=locked_sale,
                    cashier_user=user,
                    raw_query=(raw_query or prod.product_id)[:128],
                    resolution_source=res_source,
                    matched_product=prod,
                    matched_variant=var,
                    packaging_unit=req_pkg,
                    conversion_factor=conv_factor,
                    quantity_delta=qty_delta,
                    latency_ms=Decimal(str(elapsed)),
                    used_snapshot_cache=True,
                    was_rejected=False,
                )

                PosDomainService._record_lifecycle_audit(
                    sale=locked_sale,
                    from_state=locked_sale.lifecycle_state,
                    to_state=locked_sale.lifecycle_state,
                    action="SCAN_ITEM_ADDED",
                    user=user,
                    reason=f"Scanned/added {prod.brand_name} x{qty_delta} {req_pkg}",
                    metadata={"scan_event_id": scan_event.scan_event_id, "product_id": prod.product_id, "version": locked_sale.version},
                )

                # Refresh SKU read model non-destructively in case reservation state changed
                cls.refresh_branch_sku_read_model(user, branch_id=branch.branch_id, product_ids=[prod.product_id])

                refreshed_sale = (
                    PosSaleOrder.objects.filter(pk=locked_sale.pk)
                    .prefetch_related("lines__product", "lines__variant", "allocations", "payment_attempts", "lifecycle_audits")
                    .get()
                )
                return refreshed_sale, scan_event
        except _ScanRejectionAbort as abort:
            cls._record_scan_rejection(**abort.log_kwargs)
            raise abort.exc

    @classmethod
    def mutate_cart_line(
        cls,
        user: Any,
        sale: PosSaleOrder,
        data: Dict[str, Any],
    ) -> Tuple[PosSaleOrder, PosCartScanEvent]:
        """
        Keyboard-first cart line mutation: increment/decrement (+ / -), explicit quantity_units,
        unit conversion toggle (U: TABLET/STRIP/BOX), line discount, or remove line (Delete / qty <= 0).
        """
        started = time.perf_counter()
        with transaction.atomic():
            locked_sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
            tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
                user,
                branch_id=locked_sale.branch_id,
                require_write=True,
                allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
            )
            if not branch:
                raise ValidationError({"branch": "Sale order branch is invalid."})

            if locked_sale.lifecycle_state not in PosDomainService.MUTABLE_SALE_STATES:
                raise ValidationError(
                    {
                        "error_code": "ERR_IMMUTABLE_SALE_STATE",
                        "detail": f"Cannot edit cart lines for sale '{locked_sale.order_number}' in state '{locked_sale.lifecycle_state}'.",
                    }
                )

            expected_ver = data.get("expected_version")
            if expected_ver is not None and int(expected_ver) != int(locked_sale.version):
                raise ValidationError(
                    {
                        "error_code": "ERR_CART_VERSION_CONFLICT",
                        "expected_version": expected_ver,
                        "current_version": locked_sale.version,
                        "detail": "Cart was updated concurrently; refresh cart before mutating line.",
                    }
                )

            line_id = str(data.get("line_id") or "").strip()
            line_num = data.get("line_number")
            product_id_arg = str(data.get("product_id") or data.get("product") or "").strip()
            pkg_hint = str(data.get("packaging_unit") or "").strip().upper()
            lines = list(locked_sale.lines.select_related("product", "variant").order_by("line_number"))
            target_line: Optional[PosSaleOrderLine] = None
            if line_id:
                target_line = next((l for l in lines if l.line_id == line_id), None)
            elif line_num is not None:
                target_line = next((l for l in lines if l.line_number == int(line_num)), None)
            elif product_id_arg:
                matches = [l for l in lines if l.product_id == product_id_arg]
                if pkg_hint:
                    pkg_matches = [l for l in matches if str(l.packaging_unit).upper() == pkg_hint]
                    if len(pkg_matches) == 1:
                        target_line = pkg_matches[0]
                if not target_line and len(matches) == 1:
                    target_line = matches[0]

            if not target_line:
                raise ValidationError({"line_id": "Specified cart line was not found in this sale order."})

            prod = target_line.product
            var = target_line.variant
            remove_flag = bool(data.get("remove", False))

            new_qty = target_line.quantity_units
            if "quantity_units" in data and data["quantity_units"] is not None:
                if not remove_flag:
                    new_qty = PosCartUnitSelectionService.parse_strict_positive_int(
                        data["quantity_units"], "quantity_units"
                    )
                else:
                    new_qty = 0
            elif "delta" in data and data["delta"] is not None:
                raw_delta = data["delta"]
                if isinstance(raw_delta, bool) or isinstance(raw_delta, float):
                    raise ValidationError(
                        {
                            "error_code": "ERR_FRACTIONAL_UNIT_PROHIBITED",
                            "delta": "delta must be a whole integer; fractional quantities are prohibited.",
                        }
                    )
                try:
                    delta_int = int(str(raw_delta).strip()) if isinstance(raw_delta, str) and re.fullmatch(r"[+-]?\d+", str(raw_delta).strip()) else (int(raw_delta) if isinstance(raw_delta, int) else None)
                except (TypeError, ValueError):
                    delta_int = None
                if delta_int is None:
                    raise ValidationError(
                        {
                            "error_code": "ERR_FRACTIONAL_UNIT_PROHIBITED",
                            "delta": "delta must be a whole integer; fractional quantities are prohibited.",
                        }
                    )
                new_qty = target_line.quantity_units + delta_int

            qty_delta_applied = new_qty - target_line.quantity_units
            PosDomainService._release_reserved_allocations(locked_sale)

            if remove_flag or new_qty <= 0:
                removed_id = target_line.line_id
                target_line.delete()
                remaining_lines = list(locked_sale.lines.order_by("line_number", "created_at"))
                for idx, rem in enumerate(remaining_lines, start=1):
                    if rem.line_number != idx:
                        rem.line_number = idx
                        rem.save(update_fields=["line_number", "updated_at"])

                PosDomainService.compute_line_and_header_totals(locked_sale, user, allow_empty=True)
                locked_sale.refresh_from_db()
                from apps.core.pos_rx_safety_service import PosRxSafetyService

                PosRxSafetyService.evaluate_and_sync_sale_rx_gate(
                    locked_sale, actor_user=user, mutate=True, reason="Cart line removed"
                )
                locked_sale.refresh_from_db()
                locked_sale.version += 1
                locked_sale.updated_by = user
                locked_sale.save(update_fields=["version", "updated_by", "updated_at"])

                elapsed = round((time.perf_counter() - started) * 1000, 2)
                scan_event = PosCartScanEvent.objects.create(
                    tenant=tenant,
                    branch=branch,
                    sale=locked_sale,
                    cashier_user=user,
                    raw_query=removed_id,
                    resolution_source=PosScanResolutionSource.BRAND_GENERIC_SEARCH,
                    matched_product=prod,
                    matched_variant=var,
                    packaging_unit=target_line.packaging_unit,
                    conversion_factor=target_line.conversion_factor,
                    quantity_delta=-target_line.quantity_units,
                    latency_ms=Decimal(str(elapsed)),
                    used_snapshot_cache=True,
                    was_rejected=False,
                )
                PosDomainService._record_lifecycle_audit(
                    sale=locked_sale,
                    from_state=locked_sale.lifecycle_state,
                    to_state=locked_sale.lifecycle_state,
                    action="CART_LINE_REMOVED",
                    user=user,
                    reason=f"Removed cart line {prod.brand_name}",
                )
            else:
                from_unit = target_line.packaging_unit
                from_factor = int(target_line.conversion_factor)
                base_before = int(target_line.base_units_total)
                price_before = q2(target_line.unit_price_bdt)
                new_pkg = str(data.get("packaging_unit") or target_line.packaging_unit).strip().upper()
                raw_conv_factor = data.get("conversion_factor")
                override_reason = str(data.get("override_reason") or "").strip()[:255]

                evaluated_unit = PosCartUnitSelectionService.evaluate_unit_selection(
                    tenant=tenant,
                    branch=branch,
                    product=prod,
                    variant=var,
                    packaging_unit=new_pkg,
                    requested_conversion_factor=raw_conv_factor,
                    quantity_units=new_qty,
                    actor_user=user,
                    override_reason=override_reason,
                )
                new_pkg = evaluated_unit["packaging_unit"]
                new_factor = int(evaluated_unit["conversion_factor"])

                other_base = sum(
                    l.base_units_total
                    for l in lines
                    if l.product_id == prod.product_id and l.line_id != target_line.line_id
                )
                needed_total_base = other_base + (new_qty * new_factor)

                cls._validate_sellable_stock_and_policy(
                    tenant=tenant,
                    branch=branch,
                    product=prod,
                    packaging_unit=new_pkg,
                    conversion_factor=new_factor,
                    total_product_base_units_in_cart=needed_total_base,
                )

                if new_pkg != from_unit or new_factor != from_factor:
                    target_line.unit_price_bdt = q2(evaluated_unit["unit_price_bdt"])
                    target_line.price_source = "CANONICAL_MRP"

                if "unit_price_bdt" in data and data["unit_price_bdt"] is not None:
                    from apps.core.pos_cart_pricing_service import PosCartPricingService, parse_strict_decimal

                    req_price = parse_strict_decimal(data["unit_price_bdt"], "unit_price_bdt", min_val=Decimal("0.01"))
                    if req_price != q2(target_line.unit_price_bdt):
                        actor_role = str(getattr(user, "role", "")).upper()
                        if actor_role not in PosCartPricingService.ELEVATED_PRICING_ROLES:
                            raise PermissionDenied(
                                {
                                    "error_code": "ERR_PRICE_OVERRIDE_REQUIRES_APPROVAL",
                                    "message_en": (
                                        "Cashiers cannot override item unit price; Branch Manager, Owner, "
                                        "or Pharmacist approval is required."
                                    ),
                                    "message_bn": "ক্যাশিয়ার অনুমোদন ছাড়া একক মূল্য পরিবর্তন করতে পারবেন না।",
                                }
                            )
                        target_line.unit_price_bdt = req_price
                        target_line.price_source = "MANAGER_OVERRIDE"
                        target_line.price_override_approved_by = user
                        target_line.price_override_reason = override_reason or "Manager price override"

                target_line.packaging_unit = new_pkg
                target_line.conversion_factor = new_factor
                target_line.quantity_units = new_qty
                target_line.base_units_total = new_qty * new_factor
                target_line.dosage_form_snapshot = evaluated_unit["dosage_form_snapshot"]
                target_line.container_policy_applied = evaluated_unit["container_policy_applied"]
                target_line.unit_conversion_source = evaluated_unit["conversion_source"]
                if evaluated_unit["override_approved_by"] is not None:
                    target_line.container_override_approved_by = evaluated_unit["override_approved_by"]
                    target_line.container_override_reason = evaluated_unit["override_reason"]
                if "discount_type" in data and data["discount_type"] is not None:
                    target_line.discount_type = str(data["discount_type"]).strip().upper()
                if "discount_rate_pct" in data and data["discount_rate_pct"] is not None:
                    target_line.discount_rate_pct = q2(data["discount_rate_pct"])
                    if "discount_type" not in data:
                        target_line.discount_type = PosDiscountType.PERCENTAGE
                if "discount_amount_bdt" in data and data["discount_amount_bdt"] is not None:
                    target_line.discount_amount_bdt = q2(data["discount_amount_bdt"])
                    if "discount_type" not in data and "discount_rate_pct" not in data:
                        target_line.discount_type = PosDiscountType.FLAT_BDT
                if "tax_rate_pct" in data and data["tax_rate_pct"] is not None:
                    target_line.tax_rate_pct = q2(data["tax_rate_pct"])
                target_line.save()

                if new_pkg != from_unit or new_factor != from_factor:
                    PosCartUnitSelectionAudit.objects.create(
                        tenant=tenant,
                        branch=branch,
                        sale=locked_sale,
                        sale_line=target_line,
                        product=prod,
                        variant=var,
                        from_unit=from_unit,
                        to_unit=new_pkg,
                        from_conversion_factor=from_factor,
                        to_conversion_factor=new_factor,
                        quantity_units=new_qty,
                        base_units_before=base_before,
                        base_units_after=new_qty * new_factor,
                        unit_price_before_bdt=price_before,
                        unit_price_after_bdt=q2(target_line.unit_price_bdt),
                        conversion_source=evaluated_unit["conversion_source"],
                        container_policy_status=evaluated_unit["container_policy_applied"],
                        was_rejected=False,
                        actor_user=user,
                        actor_role=str(getattr(user, "role", "") or "").upper(),
                        override_approved_by=evaluated_unit["override_approved_by"],
                        override_reason=evaluated_unit["override_reason"],
                    )

                PosDomainService.compute_line_and_header_totals(locked_sale, user, allow_empty=True)
                locked_sale.refresh_from_db()
                from apps.core.pos_rx_safety_service import PosRxSafetyService

                PosRxSafetyService.evaluate_and_sync_sale_rx_gate(
                    locked_sale, actor_user=user, mutate=True, reason="Cart line mutated"
                )
                locked_sale.refresh_from_db()
                locked_sale.version += 1
                locked_sale.updated_by = user
                locked_sale.save(update_fields=["version", "updated_by", "updated_at"])

                elapsed = round((time.perf_counter() - started) * 1000, 2)
                scan_event = PosCartScanEvent.objects.create(
                    tenant=tenant,
                    branch=branch,
                    sale=locked_sale,
                    cashier_user=user,
                    raw_query=target_line.line_id,
                    resolution_source=PosScanResolutionSource.BRAND_GENERIC_SEARCH,
                    matched_product=prod,
                    matched_variant=var,
                    packaging_unit=new_pkg,
                    conversion_factor=new_factor,
                    quantity_delta=qty_delta_applied,
                    latency_ms=Decimal(str(elapsed)),
                    used_snapshot_cache=True,
                    was_rejected=False,
                )
                PosDomainService._record_lifecycle_audit(
                    sale=locked_sale,
                    from_state=locked_sale.lifecycle_state,
                    to_state=locked_sale.lifecycle_state,
                    action="CART_LINE_UPDATED",
                    user=user,
                    reason=f"Updated {prod.brand_name} to {new_qty} {new_pkg}",
                )

            refreshed_sale = (
                PosSaleOrder.objects.filter(pk=locked_sale.pk)
                .prefetch_related("lines__product", "lines__variant", "allocations", "payment_attempts", "lifecycle_audits")
                .get()
            )
            return refreshed_sale, scan_event

