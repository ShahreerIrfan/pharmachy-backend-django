"""
backend/apps/core/pos_search_cart_views.py
Architecture Decision: ADR-0222 (P222).
Phase 12: POS, Sales Orders, Checkout & Returns.

DRF ViewSet for P222 Fast Search, Barcode Lookup & Validated SKU Snapshot Read Models:
- GET  /api/v2/pos/search/?branch=...&q=...              (ranked fast search + live sellable batches)
- GET  /api/v2/pos/search/barcode-lookup/?branch=...&q=  (exact scanner lookup + audit log)
- GET  /api/v2/pos/search/snapshot-cache/?branch=...     (offline validated SKU snapshot read model)
- POST /api/v2/pos/search/refresh-snapshot/              (Manager/Pharmacist/Clerk rebuild; Cashier denied)
- GET  /api/v2/pos/search/scan-events/                   (immutable PosCartScanEvent log)
"""

from __future__ import annotations

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.models import PosCartScanEvent, PosSkuInventoryReadModel
from apps.core.pos_cart_unit_service import PosCartUnitSelectionService
from apps.core.pos_domain_service import PosDomainService
from apps.core.pos_search_cart_service import PosSearchCartService


class PosCartScanEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source="cashier_user.email", read_only=True, default="")
    product_brand_name = serializers.CharField(source="matched_product.brand_name", read_only=True, default="")

    class Meta:
        model = PosCartScanEvent
        fields = [
            "scan_event_id",
            "tenant_id",
            "branch_id",
            "sale_id",
            "actor_email",
            "raw_query",
            "resolution_source",
            "matched_product_id",
            "product_brand_name",
            "matched_variant_id",
            "packaging_unit",
            "conversion_factor",
            "quantity_delta",
            "latency_ms",
            "used_snapshot_cache",
            "was_rejected",
            "rejection_code",
            "rejection_message",
            "sha256_hash",
            "created_at",
        ]
        read_only_fields = fields


class PosSkuInventoryReadModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = PosSkuInventoryReadModel
        fields = [
            "read_model_id",
            "tenant_id",
            "branch_id",
            "product_id",
            "variant_id",
            "source_snapshot_id",
            "sku_code",
            "barcode_tokens",
            "brand_name",
            "brand_name_bn",
            "generic_name",
            "generic_name_bn",
            "strength",
            "dosage_form",
            "unit_price_bdt",
            "box_conversion_factor",
            "strip_conversion_factor",
            "sellable_base_units",
            "reserved_base_units",
            "available_base_units",
            "quarantined_base_units",
            "expired_base_units",
            "recalled_base_units",
            "nearest_expiry_date",
            "shelf_location",
            "requires_rx_verification",
            "non_divisible_sealed_only",
            "sha256_checksum",
            "snapshot_as_of",
            "updated_at",
        ]
        read_only_fields = fields


class PosFastSearchViewSet(viewsets.ViewSet):
    """
    Fast Search & Validated SKU Snapshot ViewSet (/api/v2/pos/search/).
    Strictly excludes unit acquisition costs from Cashiers and restricts manual snapshot rebuilds.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request: Request) -> Response:
        query = request.query_params.get("q") or request.query_params.get("query") or ""
        branch_id = request.query_params.get("branch") or request.query_params.get("branch_id")
        limit = request.query_params.get("limit", 15)
        include_zero = str(request.query_params.get("include_zero_stock", "true")).lower() in ("true", "1")
        data = PosSearchCartService.fast_search(
            request.user,
            query,
            branch_id=branch_id,
            limit=limit,
            include_zero_stock=include_zero,
            record_scan_event=False,
        )
        return Response(data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="barcode-lookup")
    def barcode_lookup(self, request: Request) -> Response:
        query = (
            request.query_params.get("barcode")
            or request.query_params.get("sku")
            or request.query_params.get("q")
            or ""
        )
        branch_id = request.query_params.get("branch") or request.query_params.get("branch_id")
        data = PosSearchCartService.fast_search(
            request.user,
            query,
            branch_id=branch_id,
            limit=5,
            include_zero_stock=True,
            record_scan_event=True,
        )
        top = data["results"][0] if data["results"] else None
        if not top:
            return Response(
                {
                    "matched": False,
                    "query": data["query"],
                    "branch_id": data["branch_id"],
                    "latency_ms": data["latency_ms"],
                    "message_en": f"No medicine or SKU matched '{data['query']}'.",
                    "message_bn": f"'{data['query']}' বারকোডের কোনো ওষুধ বা SKU পাওয়া যায়নি।",
                },
                status=status.HTTP_200_OK,
            )
        return Response(
            {
                "matched": True,
                "query": data["query"],
                "branch_id": data["branch_id"],
                "latency_ms": data["latency_ms"],
                "item": top,
                "matches": data["results"],
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="snapshot-cache")
    def snapshot_cache(self, request: Request) -> Response:
        branch_id = request.query_params.get("branch") or request.query_params.get("branch_id")
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            request.user,
            branch_id=branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        if not branch:
            raise ValidationError({"branch": "Target branch is required for SKU snapshot cache export."})

        qs = PosSkuInventoryReadModel.objects.filter(tenant=tenant, branch=branch).order_by("brand_name", "sku_code")
        items = list(qs[:500])
        if not items:
            PosSearchCartService.refresh_branch_sku_read_model(
                request.user, branch_id=branch.branch_id, enforce_snapshot_role=False
            )
            items = list(
                PosSkuInventoryReadModel.objects.filter(tenant=tenant, branch=branch).order_by("brand_name", "sku_code")[
                    :500
                ]
            )

        return Response(
            {
                "tenant_id": tenant.tenant_id,
                "branch_id": branch.branch_id,
                "count": len(items),
                "is_authoritative_online_snapshot": True,
                "offline_disclaimer_en": "Offline local snapshots are read-only estimates until re-validated online at checkout.",
                "offline_disclaimer_bn": "অফলাইন স্ন্যাপশট শুধুমাত্র সাময়িক অনুমান; অনলাইনে চেকআউটের সময় যাচাই বাধ্যতামূলক।",
                "items": PosSkuInventoryReadModelSerializer(items, many=True).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="refresh-snapshot")
    def refresh_snapshot(self, request: Request) -> Response:
        branch_id = request.data.get("branch") or request.data.get("branch_id")
        product_ids = request.data.get("product_ids")
        result = PosSearchCartService.refresh_branch_sku_read_model(
            request.user,
            branch_id=branch_id,
            product_ids=product_ids if isinstance(product_ids, list) else None,
            enforce_snapshot_role=True,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="scan-events")
    def scan_events(self, request: Request) -> Response:
        branch_id = request.query_params.get("branch") or request.query_params.get("branch_id")
        sale_id = request.query_params.get("sale")
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            request.user,
            branch_id=branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        qs = PosCartScanEvent.objects.filter(tenant=tenant).select_related("cashier_user", "matched_product")
        if branch:
            qs = qs.filter(branch=branch)
        if sale_id:
            qs = qs.filter(sale_id=sale_id)
        return Response(PosCartScanEventSerializer(qs[:100], many=True).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="unit-options")
    def unit_options(self, request: Request) -> Response:
        """GET /api/v2/pos/search/unit-options/?branch=...&product_id=...&variant_id=..."""
        branch_id = request.query_params.get("branch") or request.query_params.get("branch_id")
        product_id = request.query_params.get("product_id")
        variant_id = request.query_params.get("variant_id")
        data = PosCartUnitSelectionService.get_unit_options_response(
            actor_user=request.user,
            branch_id=branch_id,
            product_id=product_id,
            variant_id=variant_id,
        )
        return Response(data, status=status.HTTP_200_OK)

