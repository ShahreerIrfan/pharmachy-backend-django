"""
backend/apps/core/inventory_valuation_views.py
Architecture Decision: ADR-0216 (P216).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

DRF API ViewSet for Multi-Category Inventory Valuation Reports:
- On-hand, Sellable, Quarantined, In-Transit, and Expired inventory valuation.
- Costing rules: FEFO lot cost, FIFO, Weighted Average, and MRP.
- Mathematical precision and balance verification.
- Tamper-evident SHA-256 integrity audits.
- Multi-tenant isolation and strict RBAC enforcement (Cashier denied).
"""

from __future__ import annotations

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.inventory_valuation_service import InventoryValuationService
from apps.core.models import (
    Branch,
    InventoryValuationCategory,
    InventoryValuationLineItem,
    InventoryValuationReport,
    Tenant,
    ValuationCostingRule,
)


class InventoryValuationLineItemSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = InventoryValuationLineItem
        fields = [
            "id",
            "product_id",
            "product_name",
            "batch_number",
            "category",
            "category_display",
            "quantity_base_units",
            "unit_cost",
            "total_value_bdt",
            "is_cold_chain",
            "status_reason",
        ]


class InventoryValuationReportListSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    costing_rule_display = serializers.CharField(source="get_costing_rule_display", read_only=True)

    class Meta:
        model = InventoryValuationReport
        fields = [
            "report_id",
            "tenant_id",
            "branch_id",
            "branch_name",
            "as_of_timestamp",
            "costing_rule",
            "costing_rule_display",
            "total_on_hand_units",
            "total_on_hand_value_bdt",
            "sellable_units",
            "sellable_value_bdt",
            "quarantined_units",
            "quarantined_value_bdt",
            "in_transit_units",
            "in_transit_value_bdt",
            "expired_units",
            "expired_value_bdt",
            "cold_chain_units",
            "cold_chain_value_bdt",
            "is_mathematically_balanced",
            "discrepancy_units",
            "discrepancy_value_bdt",
            "sha256_checksum",
            "created_at",
        ]


class InventoryValuationReportDetailSerializer(InventoryValuationReportListSerializer):
    line_items = InventoryValuationLineItemSerializer(many=True, read_only=True)

    class Meta(InventoryValuationReportListSerializer.Meta):
        fields = InventoryValuationReportListSerializer.Meta.fields + ["notes", "line_items"]


class GenerateValuationReportRequestSerializer(serializers.Serializer):
    branch = serializers.CharField(required=True, help_text="Branch ID to evaluate")
    costing_rule = serializers.ChoiceField(
        choices=ValuationCostingRule.choices,
        default=ValuationCostingRule.FEFO_LOT_COST,
    )
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class InventoryValuationReportViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for Multi-Category Inventory Valuation Reports.
    """
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "report_id"

    def get_queryset(self):
        user = self.request.user
        InventoryValuationService.assert_valuation_role(user)

        tenant_id = getattr(user, "tenant_id", None) or getattr(user, "tenant", None)
        if not tenant_id:
            return InventoryValuationReport.objects.none()

        tenant = Tenant.objects.get(tenant_id=tenant_id) if not isinstance(tenant_id, Tenant) else tenant_id

        qs = (
            InventoryValuationReport.objects.filter(tenant=tenant)
            .select_related("branch", "generated_by")
            .order_by("-as_of_timestamp")
        )

        branch_id = self.request.query_params.get("branch")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        costing_rule = self.request.query_params.get("costing_rule")
        if costing_rule:
            qs = qs.filter(costing_rule=costing_rule)

        return qs

    def get_serializer_class(self):
        if self.action == "retrieve":
            return InventoryValuationReportDetailSerializer
        return InventoryValuationReportListSerializer

    @action(detail=False, methods=["post"], url_path="generate")
    def generate(self, request: Request) -> Response:
        """
        Generates and saves a new multi-category valuation report.
        POST /api/v2/inventory/valuation-reports/generate/
        """
        user = request.user
        InventoryValuationService.assert_valuation_role(user)

        branch_id = request.data.get("branch")
        if not branch_id:
            raise ValidationError({"branch": "Branch ID is required."})

        costing_rule = request.data.get("costing_rule", ValuationCostingRule.FEFO_LOT_COST)
        if costing_rule not in ValuationCostingRule.values:
            raise ValidationError({"costing_rule": f"Invalid costing rule '{costing_rule}'."})

        notes = request.data.get("notes", "")

        tenant_id = getattr(user, "tenant_id", None) or getattr(user, "tenant", None)
        if not tenant_id:
            raise ValidationError({"tenant": "User must have a tenant."})
        tenant = Tenant.objects.get(tenant_id=tenant_id) if not isinstance(tenant_id, Tenant) else tenant_id

        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise NotFound({"detail": f"Branch '{branch_id}' not found for tenant.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        report = InventoryValuationService.generate_valuation_report(
            tenant=tenant,
            branch=branch,
            costing_rule=costing_rule,
            user=user,
            notes=notes,
        )

        response_serializer = InventoryValuationReportDetailSerializer(report)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="verify-integrity")
    def verify_integrity(self, request: Request, report_id: str = None) -> Response:
        """
        Verifies SHA-256 cryptographic integrity of the valuation report.
        POST /api/v2/inventory/valuation-reports/{report_id}/verify-integrity/
        """
        report = self.get_object()
        is_valid, recomputed_checksum = InventoryValuationService.verify_report_integrity(report)

        return Response(
            {
                "report_id": report.report_id,
                "is_integrity_valid": is_valid,
                "persisted_checksum": report.sha256_checksum,
                "recomputed_checksum": recomputed_checksum,
                "is_mathematically_balanced": report.is_mathematically_balanced,
                "discrepancy_units": report.discrepancy_units,
                "discrepancy_value_bdt": str(report.discrepancy_value_bdt),
            },
            status=status.HTTP_200_OK,
        )

    def update(self, request, *args, **kwargs):
        raise MethodNotAllowed("PUT", detail="Valuation reports are immutable audit records.")

    def partial_update(self, request, *args, **kwargs):
        raise MethodNotAllowed("PATCH", detail="Valuation reports are immutable audit records.")

    def destroy(self, request, *args, **kwargs):
        raise MethodNotAllowed("DELETE", detail="Valuation reports are immutable audit records.")
