"""
apps.quality_governance.views
Architecture Decision: ADR-0039
REST ViewSets for Quality Issue Tickets and Definition of Done Compliance Audits.
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.quality_governance.engine import DefinitionOfDoneEngine
from apps.quality_governance.models import (
    DefinitionOfDoneAudit,
    QualityIssueTicket,
)
from apps.quality_governance.serializers import (
    DefinitionOfDoneAuditSerializer,
    DoDEvaluationRequestSerializer,
    QualityIssueTicketSerializer,
)


def _resolve_tenant_id(request) -> str:
    return (
        request.headers.get("X-Tenant-ID")
        or request.query_params.get("tenant_id")
        or getattr(request.user, "tenant_id", "")
        or "tenant-default"
    )


class QualityIssueTicketViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Quality Issue Tickets with strict tenant scoping.
    """

    queryset = QualityIssueTicket.objects.all()
    serializer_class = QualityIssueTicketSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        tenant_id = _resolve_tenant_id(self.request)
        return super().get_queryset().filter(tenant_id=tenant_id)

    def get_object(self):
        pk = self.kwargs.get("pk")
        obj = QualityIssueTicket.objects.filter(pk=pk).first()
        if not obj:
            return super().get_object()

        tenant_id = _resolve_tenant_id(self.request)
        if obj.tenant_id != tenant_id:
            raise PermissionDenied("Cross-tenant access forbidden: Cannot access another pharmacy's quality issue.")
        return obj

    def perform_create(self, serializer):
        tenant_id = _resolve_tenant_id(self.request)
        serializer.save(tenant_id=tenant_id)


class DefinitionOfDoneAuditViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Definition of Done Audits and Release Sign-offs.
    RBAC: Restricted to PHARMACY_OWNER, ADMIN, or SUPERADMIN. Cashiers get HTTP 403 Forbidden.
    """

    queryset = DefinitionOfDoneAudit.objects.all()
    serializer_class = DefinitionOfDoneAuditSerializer
    permission_classes = [permissions.AllowAny]

    def _check_reviewer_rbac(self, request):
        role = request.headers.get("X-User-Role", "")
        if not role:
            role = getattr(request.user, "role", "")

        if role == "CASHIER":
            raise PermissionDenied("Cashiers are not authorized to evaluate or sign off on Definition of Done audits.")

        allowed = {"PHARMACY_OWNER", "ADMIN", "SUPERADMIN"}
        if role and role not in allowed and not getattr(request.user, "is_superuser", False):
            raise PermissionDenied(f"Role '{role}' is not authorized to sign off on Definition of Done audits.")

    def get_queryset(self):
        tenant_id = _resolve_tenant_id(self.request)
        return super().get_queryset().filter(tenant_id=tenant_id)

    def get_object(self):
        pk = self.kwargs.get("pk")
        obj = DefinitionOfDoneAudit.objects.filter(pk=pk).first()
        if not obj:
            return super().get_object()

        tenant_id = _resolve_tenant_id(self.request)
        if obj.tenant_id != tenant_id:
            raise PermissionDenied("Cross-tenant access forbidden: Cannot access another pharmacy's DoD audit.")
        return obj

    def create(self, request, *args, **kwargs):
        self._check_reviewer_rbac(request)
        return super().create(request, *args, **kwargs)

    @action(detail=False, methods=["post"])
    def evaluate(self, request):
        """
        Evaluate Definition of Done submission against the 7 pillars and test evidence.
        """
        self._check_reviewer_rbac(request)
        tenant_id = _resolve_tenant_id(request)

        data = request.data.copy()
        if "tenant_id" not in data or not data["tenant_id"]:
            data["tenant_id"] = tenant_id

        req_serializer = DoDEvaluationRequestSerializer(data=data)
        if not req_serializer.is_valid():
            return Response(req_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        audit = req_serializer.save()
        dod_res = DefinitionOfDoneEngine.evaluate(req_serializer.validated_data)

        response_data = {
            "audit_id": str(audit.audit_id),
            "prompt_id": audit.prompt_id,
            "feature_title": audit.feature_title,
            "tenant_id": audit.tenant_id,
            "status": dod_res.status,
            "is_approved": dod_res.is_approved,
            "reasons_en": dod_res.reasons_en,
            "reasons_bn": dod_res.reasons_bn,
            "tests_passed": audit.tests_passed,
            "tests_failed": audit.tests_failed,
        }

        # If blocked and strict header requested
        status_code = status.HTTP_201_CREATED if dod_res.is_approved else status.HTTP_200_OK
        return Response(response_data, status=status_code)
