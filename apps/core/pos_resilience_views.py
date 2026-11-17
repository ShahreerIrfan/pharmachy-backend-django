"""
backend/apps/core/pos_resilience_views.py

Architecture Decision: ADR-0239 (P239).
Phase 12: POS, Sales Orders, Checkout & Returns.

DRF ViewSets & Serializers for POS UI Error Resilience & Incident Recovery:
- /api/v2/pos/error-incidents/ (List & detail error incidents for active tenant/branch)
- /api/v2/pos/error-incidents/report/ (Audited incident submission from JavaFX/Flutter POS)
- /api/v2/pos/error-incidents/{id}/resolve/ (Execute recovery action: retry print, manual search, FEFO reallocate, offline outbox)
- /api/v2/pos/error-incidents/stock-alternatives/ (Discover sellable non-expired alternative batches upon stock conflict)
"""

from __future__ import annotations

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.models import (
    PosErrorIncident,
    PosIncidentStatus,
    PosIncidentType,
    PosRecoveryActionType,
)
from apps.core.pos_resilience_service import PosResilienceService


class PosErrorIncidentSerializer(serializers.ModelSerializer):
    cashier_email = serializers.CharField(source="cashier_user.email", read_only=True, default="")
    sale_order_number = serializers.CharField(source="sale.order_number", read_only=True, default="")

    class Meta:
        model = PosErrorIncident
        fields = [
            "incident_id",
            "incident_type",
            "error_code",
            "correlation_id",
            "device_id",
            "cashier_email",
            "sale_order_number",
            "message_en",
            "message_bn",
            "status",
            "recovery_action_taken",
            "recovery_payload",
            "is_retryable",
            "created_at",
            "resolved_at",
        ]
        read_only_fields = fields


class PosIncidentReportSerializer(serializers.Serializer):
    incident_type = serializers.ChoiceField(choices=PosIncidentType.choices)
    error_code = serializers.CharField(max_length=64, required=False, default="")
    device_id = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    sale_id = serializers.CharField(max_length=64, required=False, allow_null=True, default=None)
    message_en = serializers.CharField(required=False, allow_blank=True, default="")
    message_bn = serializers.CharField(required=False, allow_blank=True, default="")
    recovery_payload = serializers.DictField(required=False, default=dict)
    is_retryable = serializers.BooleanField(required=False, default=True)


class PosIncidentResolveSerializer(serializers.Serializer):
    recovery_action = serializers.ChoiceField(choices=PosRecoveryActionType.choices)
    recovery_payload = serializers.DictField(required=False, default=dict)


class PosErrorIncidentViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for POS error resilience auditing, report intake, and recovery execution.
    """
    serializer_class = PosErrorIncidentSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "incident_id"

    def get_queryset(self):
        user = self.request.user
        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        if not tenant_id:
            return PosErrorIncident.objects.none()

        qs = PosErrorIncident.objects.filter(tenant__tenant_id=tenant_id)
        branch_id = getattr(user, "branch_id", None)
        if branch_id and getattr(user, "role", "") not in ("SUPERADMIN", "PHARMACY_OWNER"):
            qs = qs.filter(branch__branch_id=branch_id)
        return qs.select_related("tenant", "branch", "cashier_user", "sale")

    @action(detail=False, methods=["post"], url_path="report")
    def report(self, request: Request) -> Response:
        """POST /api/v2/pos/error-incidents/report/ — Log a recoverable POS hardware/network/stock incident."""
        serializer = PosIncidentReportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        incident = PosResilienceService.report_incident(
            request.user,
            incident_type=data["incident_type"],
            error_code=data.get("error_code") or f"ERR_POS_{data['incident_type']}",
            device_id=data.get("device_id", ""),
            sale_id=data.get("sale_id"),
            message_en=data.get("message_en", ""),
            message_bn=data.get("message_bn", ""),
            recovery_payload=data.get("recovery_payload"),
            is_retryable=data.get("is_retryable", True),
        )
        return Response(PosErrorIncidentSerializer(incident).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="resolve")
    def resolve(self, request: Request, incident_id=None) -> Response:
        """POST /api/v2/pos/error-incidents/{incident_id}/resolve/ — Execute audited recovery action."""
        serializer = PosIncidentResolveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        incident = PosResilienceService.resolve_incident(
            request.user,
            incident_id=incident_id,
            recovery_action=data["recovery_action"],
            recovery_payload=data.get("recovery_payload"),
        )
        return Response(PosErrorIncidentSerializer(incident).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get", "post"], url_path="stock-alternatives")
    def stock_alternatives(self, request: Request) -> Response:
        """
        GET/POST /api/v2/pos/error-incidents/stock-alternatives/
        Query non-expired, sellable alternative batches for a product experiencing a stock conflict.
        """
        product_id = request.query_params.get("product_id") or request.data.get("product_id")
        if not product_id:
            return Response(
                {"detail": "product_id parameter is required.", "error_code": "ERR_PRODUCT_REQUIRED"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qty_raw = request.query_params.get("requested_quantity") or request.data.get("requested_quantity", 1)
        try:
            qty = int(qty_raw)
        except (ValueError, TypeError):
            qty = 1

        alternatives = PosResilienceService.get_stock_alternatives(
            request.user,
            product_id=str(product_id),
            requested_quantity=qty,
        )
        return Response({"product_id": product_id, "alternatives": alternatives}, status=status.HTTP_200_OK)

