"""
backend/apps/core/pos_sales_e2e_qa_views.py

Architecture Decision: ADR-0240 (P240).
Phase 12: POS, Sales Orders, Checkout & Returns Capstone.

DRF ViewSet for Sales End-to-End Acceptance, Concurrency & Reconciliation QA:
- /api/v2/pos/e2e-qa/ (List & detail audit verification runs)
- /api/v2/pos/e2e-qa/run-verification/ (Trigger automated Phase 12 POS E2E cycle)
"""

from __future__ import annotations

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.models import PosSalesVerificationRun
from apps.core.pos_sales_e2e_qa_service import PosSalesE2EQaService


class PosSalesVerificationRunSerializer(serializers.ModelSerializer):
    executed_by_email = serializers.CharField(source="executed_by.email", read_only=True, default="")
    branch_name = serializers.CharField(source="branch.name", read_only=True, default="")

    class Meta:
        model = PosSalesVerificationRun
        fields = [
            "run_id",
            "test_type",
            "status",
            "branch_name",
            "sale_order_number",
            "return_reference",
            "shift_reference",
            "daily_close_reference",
            "assertions_passed",
            "assertions_failed",
            "audit_notes",
            "metrics_json",
            "executed_by_email",
            "created_at",
        ]
        read_only_fields = fields


class PosSalesE2EQaViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for automated Phase 12 POS Sales E2E verification auditing.
    """
    serializer_class = PosSalesVerificationRunSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "run_id"

    def get_queryset(self):
        user = self.request.user
        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        if not tenant_id:
            return PosSalesVerificationRun.objects.none()

        qs = PosSalesVerificationRun.objects.filter(tenant__tenant_id=tenant_id)
        branch_id = getattr(user, "branch_id", None)
        if branch_id and getattr(user, "role", "") not in ("SUPERADMIN", "PHARMACY_OWNER"):
            qs = qs.filter(branch__branch_id=branch_id)
        return qs.select_related("tenant", "branch", "executed_by")

    @action(detail=False, methods=["post"], url_path="run-verification")
    def run_verification(self, request: Request) -> Response:
        """
        POST /api/v2/pos/e2e-qa/run-verification/
        Execute automated Phase 12 Sales E2E Acceptance cycle.
        """
        product_id = request.data.get("product_id")
        branch_id = request.data.get("branch_id")
        notes = str(request.data.get("notes") or "")

        run = PosSalesE2EQaService.execute_e2e_verification_cycle(
            request.user,
            product_id=product_id,
            branch_id=branch_id,
            notes=notes,
        )
        return Response(PosSalesVerificationRunSerializer(run).data, status=status.HTTP_201_CREATED)

