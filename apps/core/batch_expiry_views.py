"""
backend/apps/core/batch_expiry_views.py
Architecture Decision: ADR-0172 (P172).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

REST API Endpoints for Batch Expiry Alert Windows,
POS Sale Cutoff Verification, and Automated Quarantine Sweeps.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.batch_expiry_service import BatchExpiryService
from apps.core.models import BatchExpiryPolicy, Branch, Tenant, TenantLifecycleStatus
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class BatchExpiryPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = BatchExpiryPolicy
        fields = [
            "policy_id",
            "tenant_id",
            "branch_id",
            "warning_lead_days",
            "critical_lead_days",
            "quarantine_lead_days",
            "sale_cutoff_lead_days",
            "auto_quarantine_on_cutoff",
            "allow_manager_override_near_expiry",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["policy_id", "tenant_id", "created_at", "updated_at"]


class BatchExpiryViewSet(viewsets.ViewSet):
    """
    ViewSet providing configurable batch expiry alert windows,
    sale cutoff verification, and automated quarantine sweeps.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _check_tenant_and_role(self, request: Any) -> None:
        user = request.user
        role = getattr(user, "role", None)
        if role not in {
            UserRole.CASHIER,
            UserRole.STAFF_PHARMACIST,
            UserRole.BRANCH_MANAGER,
            UserRole.PHARMACY_OWNER,
            UserRole.INVENTORY_CLERK,
            UserRole.AUDITOR,
            UserRole.SUPERADMIN,
            "CASHIER",
            "STAFF_PHARMACIST",
            "BRANCH_MANAGER",
            "PHARMACY_OWNER",
            "INVENTORY_CLERK",
            "AUDITOR",
            "SUPERADMIN",
            "OWNER_ADMIN",
            "ADMIN",
        }:
            raise PermissionDenied(
                {"detail": "Role is not authorized to access expiry operations.", "error_code": "ERR_ROLE_UNAUTHORIZED"}
            )

        tenant = getattr(request, "tenant", None) or getattr(user, "tenant", None)
        if tenant and (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {"detail": "Tenant subscription is suspended.", "error_code": "ERR_TENANT_SUSPENDED"}
            )

    @action(detail=False, methods=["get", "post"], url_path="policy")
    def policy(self, request):
        """
        GET /api/v2/inventory/expiry-windows/policy/?branch_id=...
        POST /api/v2/inventory/expiry-windows/policy/
        """
        self._check_tenant_and_role(request)
        user = request.user
        tenant_id = request.data.get("tenant_id") if request.method == "POST" else request.query_params.get("tenant_id")
        tenant_id = tenant_id or getattr(user, "tenant_id", None)

        if not tenant_id:
            raise ValidationError({"tenant_id": "tenant_id is required.", "error_code": "ERR_MISSING_TENANT"})

        if getattr(user, "tenant_id", None) and str(user.tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})

        if request.method == "GET":
            branch_id = request.query_params.get("branch_id")
            branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first() if branch_id else None
            effective_policy = BatchExpiryService.get_effective_policy(tenant, branch)
            return Response(BatchExpiryPolicySerializer(effective_policy).data, status=status.HTTP_200_OK)

        elif request.method == "POST":
            # Manager/Owner authorization required
            role = getattr(user, "role", None)
            if role not in {UserRole.BRANCH_MANAGER, UserRole.PHARMACY_OWNER, UserRole.SUPERADMIN, "BRANCH_MANAGER", "PHARMACY_OWNER", "SUPERADMIN"}:
                raise PermissionDenied({"detail": "Only managers or owners can configure expiry policy.", "error_code": "ERR_ROLE_UNAUTHORIZED"})

            branch_id = request.data.get("branch_id")
            branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first() if branch_id else None

            warning_days = int(request.data.get("warning_lead_days", 90))
            critical_days = int(request.data.get("critical_lead_days", 30))
            quarantine_days = int(request.data.get("quarantine_lead_days", 15))
            cutoff_days = int(request.data.get("sale_cutoff_lead_days", 7))
            auto_quar = bool(request.data.get("auto_quarantine_on_cutoff", True))
            allow_override = bool(request.data.get("allow_manager_override_near_expiry", False))

            policy_obj = BatchExpiryPolicy.objects.filter(tenant=tenant, branch=branch).first()
            if not policy_obj:
                policy_obj = BatchExpiryPolicy(
                    policy_id=f"expol-{uuid.uuid4().hex[:12]}",
                    tenant=tenant,
                    branch=branch,
                )
            policy_obj.warning_lead_days = warning_days
            policy_obj.critical_lead_days = critical_days
            policy_obj.quarantine_lead_days = quarantine_days
            policy_obj.sale_cutoff_lead_days = cutoff_days
            policy_obj.auto_quarantine_on_cutoff = auto_quar
            policy_obj.allow_manager_override_near_expiry = allow_override

            try:
                policy_obj.clean()
            except DjangoValidationError as val_err:
                raise ValidationError(val_err.message_dict if hasattr(val_err, "message_dict") else str(val_err))

            policy_obj.save()
            return Response(BatchExpiryPolicySerializer(policy_obj).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="verify-sale-cutoff")
    def verify_sale_cutoff(self, request):
        """
        POST /api/v2/inventory/expiry-windows/verify-sale-cutoff/
        Pre-tender validation ensuring batch has not expired or breached sale cutoff.
        """
        self._check_tenant_and_role(request)
        data = dict(request.data)
        if "tenant_id" not in data and getattr(request.user, "tenant_id", None):
            data["tenant_id"] = str(request.user.tenant_id)

        result = BatchExpiryService.verify_sale_cutoff(request.user, data)
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="auto-quarantine-scan")
    def auto_quarantine_scan(self, request):
        """
        POST /api/v2/inventory/expiry-windows/auto-quarantine-scan/
        Executes automated sweep over branch batches.
        """
        self._check_tenant_and_role(request)
        data = dict(request.data)
        if "tenant_id" not in data and getattr(request.user, "tenant_id", None):
            data["tenant_id"] = str(request.user.tenant_id)

        result = BatchExpiryService.scan_and_quarantine_expired_batches(request.user, data)
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="dashboard")
    def dashboard(self, request):
        """
        GET /api/v2/inventory/expiry-windows/dashboard/?branch_id=...
        """
        self._check_tenant_and_role(request)
        params = request.query_params.dict()
        if "tenant_id" not in params and getattr(request.user, "tenant_id", None):
            params["tenant_id"] = str(request.user.tenant_id)

        result = BatchExpiryService.get_expiry_dashboard(request.user, params)
        return Response(result, status=status.HTTP_200_OK)

