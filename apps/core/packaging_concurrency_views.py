"""
backend/apps/core/packaging_concurrency_views.py
Architecture Decision: ADR-0180 (P180).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

DRF ViewSet providing multi-threaded concurrency stress testing,
simulated checkout collision analysis, and immutable audit record reporting.
"""

from __future__ import annotations

from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.models import PackagingConcurrencyAuditRecord
from apps.core.packaging_concurrency_service import PackagingConcurrencyService


class PackagingConcurrencyViewSet(viewsets.ViewSet):
    """
    Authoritative ViewSet for Packaging, FEFO, and Concurrency Stress Testing.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_tenant_id(self, request: Request) -> str:
        tenant_id = request.data.get("tenant_id") or request.query_params.get("tenant_id") or getattr(request.user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError(
                {"detail": "tenant_id is required.", "error_code": "ERR_TENANT_REQUIRED"}
            )
        return str(tenant_id)

    def list(self, request: Request) -> Response:
        """
        Lists past packaging concurrency audit runs for the tenant.
        GET /api/v2/inventory/concurrency-tests/
        """
        PackagingConcurrencyService._assert_manage_role(request.user)
        tenant_id = self._get_tenant_id(request)
        branch_id = request.query_params.get("branch_id")

        qs = PackagingConcurrencyAuditRecord.objects.select_related("product").filter(tenant_id=tenant_id)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        results = [PackagingConcurrencyService.serialize_audit_record(r) for r in qs[:50]]
        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

    def create(self, request: Request) -> Response:
        """
        Triggers a controlled multi-threaded concurrent checkout stress test.
        POST /api/v2/inventory/concurrency-tests/
        """
        tenant_id = self._get_tenant_id(request)
        branch_id = request.data.get("branch_id")
        if not branch_id:
            raise ValidationError({"detail": "branch_id is required.", "error_code": "ERR_BRANCH_REQUIRED"})

        product_id = request.data.get("product_id")
        if not product_id:
            raise ValidationError({"detail": "product_id is required.", "error_code": "ERR_PRODUCT_REQUIRED"})

        concurrency_level = int(request.data.get("concurrency_level", 10))
        requests_per_thread = int(request.data.get("requests_per_thread", 1))
        units_per_request = int(request.data.get("units_per_request", 5))
        notes = request.data.get("notes", "")

        result = PackagingConcurrencyService.run_stress_test(
            user=request.user,
            tenant_id=tenant_id,
            branch_id=branch_id,
            product_id=product_id,
            concurrency_level=concurrency_level,
            requests_per_thread=requests_per_thread,
            units_per_request=units_per_request,
            notes=notes,
        )
        return Response(result, status=status.HTTP_201_CREATED)

    def retrieve(self, request: Request, pk: str = None) -> Response:
        """
        Retrieves a specific concurrency audit record by ID.
        GET /api/v2/inventory/concurrency-tests/{id}/
        """
        PackagingConcurrencyService._assert_manage_role(request.user)
        record = PackagingConcurrencyAuditRecord.objects.select_related("product").filter(test_id=pk).first()
        if not record:
            raise NotFound({"detail": f"Concurrency test record '{pk}' not found.", "error_code": "ERR_TEST_RECORD_NOT_FOUND"})

        user_tenant = getattr(request.user, "tenant_id", None)
        if user_tenant and str(user_tenant) != str(record.tenant_id) and getattr(request.user, "role", None) != "SUPERADMIN":
            raise PermissionDenied({"detail": "Cross-tenant access prohibited.", "error_code": "ERR_CROSS_TENANT_ACCESS"})

        return Response(PackagingConcurrencyService.serialize_audit_record(record), status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="run")
    def run(self, request: Request) -> Response:
        """Explicit action alias: POST /api/v2/inventory/concurrency-tests/run/"""
        return self.create(request)

    def update(self, request: Request, pk: Any = None) -> Response:
        raise MethodNotAllowed("PUT", detail="Concurrency audit records are immutable financial quality records.")

    def partial_update(self, request: Request, pk: Any = None) -> Response:
        raise MethodNotAllowed("PATCH", detail="Concurrency audit records are immutable financial quality records.")

    def destroy(self, request: Request, pk: Any = None) -> Response:
        raise MethodNotAllowed("DELETE", detail="Concurrency audit records are immutable financial quality records.")

