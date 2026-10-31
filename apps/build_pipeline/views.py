"""
apps.build_pipeline.views
Architecture Decision: ADR-0040
DRF ViewSet for Clean-Checkout Automated Build Smoke Pipeline.
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.build_pipeline.models import BuildSmokeExecutionRecord
from apps.build_pipeline.orchestrator import BuildSmokeOrchestrator
from apps.build_pipeline.serializers import (
    BuildSmokeExecutionRecordSerializer,
    BuildSmokeTriggerRequestSerializer,
)


def _resolve_tenant_id(request) -> str:
    return (
        request.headers.get("X-Tenant-ID")
        or request.query_params.get("tenant_id")
        or getattr(request.user, "tenant_id", "")
        or "tenant-default"
    )


class BuildSmokeViewSet(viewsets.ModelViewSet):
    """
    ViewSet for auditing and triggering automated build smoke pipelines.
    RBAC: Restricted to DevOps, Release Managers, Admins, and Owners.
    Cashiers receive HTTP 403 Forbidden.
    """

    queryset = BuildSmokeExecutionRecord.objects.all()
    serializer_class = BuildSmokeExecutionRecordSerializer
    permission_classes = [permissions.AllowAny]

    def _check_pipeline_rbac(self, request):
        role = request.headers.get("X-User-Role", "")
        if not role:
            role = getattr(request.user, "role", "")

        if role == "CASHIER":
            raise PermissionDenied("Cashiers are not authorized to trigger or audit build smoke pipelines.")

        allowed = {"PHARMACY_OWNER", "ADMIN", "SUPERADMIN", "DEVOPS", "RELEASE_MANAGER"}
        if role and role not in allowed and not getattr(request.user, "is_superuser", False):
            raise PermissionDenied(f"Role '{role}' is not authorized to manage build smoke pipelines.")

    def get_queryset(self):
        tenant_id = _resolve_tenant_id(self.request)
        return super().get_queryset().filter(tenant_id=tenant_id)

    def get_object(self):
        pk = self.kwargs.get("pk")
        obj = BuildSmokeExecutionRecord.objects.filter(pk=pk).first()
        if not obj:
            return super().get_object()

        tenant_id = _resolve_tenant_id(self.request)
        if obj.tenant_id != tenant_id:
            raise PermissionDenied(
                "Cross-tenant access forbidden: Cannot access another pharmacy's build smoke pipeline."
            )
        return obj

    def create(self, request, *args, **kwargs):
        """
        Trigger automated clean-checkout build smoke pipeline.
        """
        self._check_pipeline_rbac(request)
        tenant_id = _resolve_tenant_id(request)

        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        if "tenant_id" not in data or not data["tenant_id"]:
            data["tenant_id"] = tenant_id

        req_serializer = BuildSmokeTriggerRequestSerializer(data=data)
        if not req_serializer.is_valid():
            return Response(req_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        val_data = req_serializer.validated_data
        orchestrator = BuildSmokeOrchestrator()
        record = orchestrator.run_pipeline(
            tenant_id=val_data.get("tenant_id", tenant_id),
            triggered_by=val_data.get("triggered_by", "CLI_RUNNER"),
            fail_at_stage=val_data.get("fail_at_stage"),
            git_commit_hash=val_data.get("git_commit_hash"),
            git_branch=val_data.get("git_branch", "master"),
        )

        res_serializer = BuildSmokeExecutionRecordSerializer(record)
        return Response(res_serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"])
    def trigger(self, request):
        """
        Convenience endpoint to trigger a build smoke run.
        """
        return self.create(request)

    @action(detail=False, methods=["get"])
    def latest(self, request):
        """
        Retrieve the most recent smoke pipeline record for the tenant.
        """
        self._check_pipeline_rbac(request)
        tenant_id = _resolve_tenant_id(request)
        latest_record = BuildSmokeExecutionRecord.objects.filter(tenant_id=tenant_id).order_by("-executed_at").first()
        if not latest_record:
            return Response(
                {"detail": "No build smoke execution records found for tenant."},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = self.get_serializer(latest_record)
        return Response(serializer.data, status=status.HTTP_200_OK)
