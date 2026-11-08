"""
backend/apps/core/stock_inquiry_views.py
Architecture Decision: ADR-0203 (P203).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

DRF ViewSets and Serializers for Tenant & Branch Stock Inquiry API.
"""

from __future__ import annotations

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import Branch, StockInquiryAuditLog
from apps.core.stock_inquiry_service import StockInquiryService


class StockInquiryAuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockInquiryAuditLog
        fields = [
            "inquiry_id",
            "branch",
            "search_query",
            "filter_expiry_status",
            "filter_location",
            "products_matched_count",
            "batches_matched_count",
            "total_available_units",
            "inquired_at",
        ]
        read_only_fields = fields


class StockInquiryViewSet(viewsets.ViewSet):
    """
    Authorized stock inquiry API for batches, base units, available, reserved,
    damaged, and expiry statuses across pharmacy branches.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request):
        """
        GET /api/v2/inventory/stock-inquiry/
        Execute an authorized stock inquiry with filters.
        """
        user = request.user
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            raise PermissionDenied("User does not have an active tenant affiliation.")

        branch_id = request.query_params.get("branch") or getattr(user, "branch_id", None)
        if not branch_id:
            first_branch = Branch.objects.filter(tenant_id=tenant_id).first()
            if not first_branch:
                raise ValidationError({"branch": "No active branch found for this pharmacy account."})
            branch_id = first_branch.branch_id

        search = request.query_params.get("search", "")
        product_id = request.query_params.get("product_id")
        batch_number = request.query_params.get("batch_number")
        expiry_status = request.query_params.get("expiry_status", "ALL")
        location_code = request.query_params.get("location_code")
        storage_condition = request.query_params.get("storage_condition")
        include_zero_stock = request.query_params.get("include_zero_stock", "false").lower() in ["true", "1"]

        res = StockInquiryService.query_stock(
            tenant_id=tenant_id,
            branch_id=branch_id,
            search=search,
            product_id=product_id,
            batch_number=batch_number,
            expiry_status=expiry_status,
            location_code=location_code,
            storage_condition=storage_condition,
            include_zero_stock=include_zero_stock,
            actor_user=user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="audit-logs")
    def audit_logs(self, request):
        """
        GET /api/v2/inventory/stock-inquiry/audit-logs/
        Retrieves recent inquiry audit logs for tenant compliance review.
        """
        user = request.user
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            return Response([])

        qs = StockInquiryAuditLog.objects.filter(tenant_id=tenant_id)
        branch_id = request.query_params.get("branch")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        serializer = StockInquiryAuditLogSerializer(qs[:50], many=True)
        return Response(serializer.data)

