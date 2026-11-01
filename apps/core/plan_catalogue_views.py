"""
apps/core/plan_catalogue_views.py
Architecture Decision: ADR-0015 & ADR-0081
Phase 05: Basic/Standard/Pro Subscription & Billing (P081)

Plan Catalogue & Tenant Entitlement ViewSets:
- Plan Catalogue exploration and Super Admin governance.
- Tenant Entitlement snapshot inspection and plan application.
- Cross-tenant security enforcement and role-based access control.
"""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.models import PlanCatalogueRecord
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.serializers import (
    ApplyPlanRequestSerializer,
    PlanCatalogueRecordSerializer,
    PlanQuoteRequestSerializer,
)

logger = logging.getLogger("pharmacy.billing.views")


def _is_platform_superadmin(user: Any) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    role = str(getattr(user, "role", "")).upper()
    return role in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_SUPER_ADMIN", "PLATFORM_OPERATOR"]


class PlanCatalogueViewSet(viewsets.ViewSet):
    """
    Public & Platform Admin Plan Catalogue ViewSet.
    Allows viewing all available Basic, Standard, and Pro offerings with quotas and pricing.
    """

    permission_classes = [permissions.AllowAny]

    def list(self, request):
        """Lists all active plans in the catalogue."""
        include_inactive = False
        if _is_platform_superadmin(request.user) and request.query_params.get("include_inactive") == "true":
            include_inactive = True

        plans = PlanCatalogueService.list_catalogue_plans(include_inactive=include_inactive)
        return Response(
            {
                "success": True,
                "count": len(plans),
                "data": plans,
            }
        )

    def retrieve(self, request, pk=None):
        """Retrieves details of a specific plan by its plan_code."""
        try:
            plan = PlanCatalogueService.get_plan_by_code(pk)
        except PlanCatalogueRecord.DoesNotExist:
            raise NotFound(detail=f"Plan with code '{pk}' not found.")

        serializer = PlanCatalogueRecordSerializer(plan)
        return Response(
            {
                "success": True,
                "data": serializer.data,
            }
        )

    def create(self, request):
        """Platform Super Admin creates or updates a plan catalogue specification."""
        if not _is_platform_superadmin(request.user):
            raise PermissionDenied(
                detail={
                    "error_code": "ROLE_PERMISSION_DENIED",
                    "message_en": "Only Platform Super Admin is authorized to create or modify plan records.",
                    "message_bn": "শুধুমাত্র প্ল্যাটফর্ম সুপার অ্যাডমিনের প্ল্যান তৈরি বা পরিবর্তনের অনুমতি আছে।",
                }
            )

        serializer = PlanCatalogueRecordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = serializer.save()

        return Response(
            {
                "success": True,
                "code": "PLAN_CREATED",
                "message_en": f"Plan '{plan.plan_code}' configured successfully.",
                "message_bn": f"প্ল্যান '{plan.plan_code}' সফলভাবে কনফিগার করা হয়েছে।",
                "data": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"], url_path="comparison", permission_classes=[permissions.AllowAny])
    def comparison(self, request):
        """
        Public comparison matrix for Basic, Standard, and Pro plans (ADR-0083).
        Includes annual savings breakdown, usage quotas, and guaranteed clinical safety features.
        """
        matrix = PlanCatalogueService.get_plan_comparison_matrix()
        return Response(
            {
                "success": True,
                "data": matrix,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="quote", permission_classes=[permissions.IsAuthenticated])
    def quote(self, request):
        """
        Calculates checkout quote, savings, and validates quota compliance for a tenant (ADR-0083).
        Enforces tenant owner RBAC and cross-tenant isolation.
        """
        serializer = PlanQuoteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        tenant_id = data["tenant_id"]

        # Enforce Tenant Access & Owner Role
        user = request.user
        if not _is_platform_superadmin(user):
            user_tenant_id = getattr(user, "tenant_id", None)
            if hasattr(user, "tenant") and user.tenant:
                user_tenant_id = getattr(user.tenant, "tenant_id", user_tenant_id)

            if not user_tenant_id or str(user_tenant_id) != str(tenant_id):
                raise CrossTenantViolationError(
                    message_en=f"Tenant '{user_tenant_id}' cannot generate quotes for tenant '{tenant_id}'.",
                    message_bn="অন্য ফার্মেসির জন্য প্ল্যান কোট তৈরি করার অনুমতি নেই।",
                )

            user_role = str(getattr(user, "role", "")).upper()
            if user_role not in ["TENANT_OWNER", "OWNER", "PHARMACY_OWNER"]:
                raise PermissionDenied(
                    detail={
                        "error_code": "ROLE_PERMISSION_DENIED",
                        "message_en": "Only Tenant Owner or Platform Super Admin can generate checkout quotes.",
                        "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা সুপার অ্যাডমিন চেকআউট কোট তৈরি করতে পারেন।",
                    }
                )

        try:
            quote_data = PlanCatalogueService.calculate_plan_quote(
                tenant_id=tenant_id,
                plan_code=data["plan_code"],
                billing_cycle=data.get("billing_cycle", "MONTHLY"),
            )
        except ValueError as err:
            raise ValidationError(detail={"error": [str(err)]})

        return Response(
            {
                "success": True,
                "data": quote_data,
            },
            status=status.HTTP_200_OK,
        )


class TenantEntitlementViewSet(viewsets.ViewSet):
    """
    Tenant Applied Entitlement ViewSet.
    Inspects locked-in immutable plan snapshots, quotas, and applies new plans.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _verify_tenant_access(self, request, tenant_id: str, require_owner: bool = False):
        user = request.user
        if _is_platform_superadmin(user):
            return

        user_tenant_id = getattr(user, "tenant_id", None)
        if hasattr(user, "tenant") and user.tenant:
            user_tenant_id = getattr(user.tenant, "tenant_id", user_tenant_id)

        if not user_tenant_id or str(user_tenant_id) != str(tenant_id):
            raise CrossTenantViolationError(
                message_en=f"Tenant '{user_tenant_id}' cannot access entitlements for tenant '{tenant_id}'.",
                message_bn="অন্য ফার্মেসির সাবস্ক্রিপশন ও এনটাইটেলমেন্ট দেখার অনুমতি নেই।",
            )

        if require_owner:
            user_role = str(getattr(user, "role", "")).upper()
            if user_role not in ["TENANT_OWNER", "OWNER", "PHARMACY_OWNER"]:
                raise PermissionDenied(
                    detail={
                        "error_code": "ROLE_PERMISSION_DENIED",
                        "message_en": "Only Tenant Owner or Platform Super Admin can apply plan changes.",
                        "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা সুপার অ্যাডমিন প্ল্যান পরিবর্তন করতে পারেন।",
                    }
                )

    def retrieve(self, request, pk=None):
        """Retrieves active applied entitlement for the given tenant_id."""
        tenant_id = str(pk)
        self._verify_tenant_access(request, tenant_id, require_owner=False)

        try:
            entitlement = PlanCatalogueService.get_active_tenant_entitlement(tenant_id)
        except ValueError as err:
            raise NotFound(detail=str(err))

        return Response(
            {
                "success": True,
                "data": entitlement,
            }
        )

    @action(detail=True, methods=["post"], url_path="apply")
    def apply_plan(self, request, pk=None):
        """Applies a plan from catalogue to the tenant."""
        tenant_id = str(pk)
        self._verify_tenant_access(request, tenant_id, require_owner=True)

        serializer = ApplyPlanRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        operator_id = str(getattr(request.user, "user_id", None) or getattr(request.user, "id", "SYSTEM"))

        try:
            result = PlanCatalogueService.apply_plan_to_tenant(
                tenant_id=tenant_id,
                plan_code=data["plan_code"],
                billing_cycle=data.get("billing_cycle", "MONTHLY"),
                operator_id=operator_id,
                notes=data.get("notes", ""),
            )
        except ValueError as err:
            raise ValidationError(detail={"error": [str(err)]})

        return Response(
            {
                "success": True,
                "code": "PLAN_APPLIED_SUCCESS",
                "message_en": f"Plan '{data['plan_code']}' applied to tenant '{tenant_id}' successfully.",
                "message_bn": f"টেন্যান্ট '{tenant_id}'-এ প্ল্যান '{data['plan_code']}' সফলভাবে প্রয়োগ করা হয়েছে।",
                "data": result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="verify-feature")
    def verify_feature(self, request, pk=None):
        """Checks if a tenant is entitled to a specific feature key."""
        tenant_id = str(pk)
        self._verify_tenant_access(request, tenant_id, require_owner=False)

        feature_key = request.query_params.get("feature", "").strip()
        if not feature_key:
            raise ValidationError(detail={"feature": ["Query parameter 'feature' is required."]})

        is_entitled = PlanCatalogueService.verify_feature_entitlement(tenant_id, feature_key)
        return Response(
            {
                "success": True,
                "tenant_id": tenant_id,
                "feature": feature_key,
                "is_entitled": is_entitled,
            }
        )


class TenantEffectiveEntitlementViewSet(viewsets.ViewSet):
    """
    Evaluates effective capabilities, limits, quotas, and offline lease for authenticated tenant/device.
    Route: /api/v2/billing/effective-entitlements/{tenant_id}/
    """

    permission_classes = [permissions.IsAuthenticated]

    def _verify_tenant_access(self, request, tenant_id: str):
        user = request.user
        if _is_platform_superadmin(user):
            return

        user_tenant_id = getattr(user, "tenant_id", None)
        if hasattr(user, "tenant") and user.tenant:
            user_tenant_id = getattr(user.tenant, "tenant_id", user_tenant_id)

        if not user_tenant_id or str(user_tenant_id) != str(tenant_id):
            raise CrossTenantViolationError(
                message_en=f"Tenant '{user_tenant_id}' cannot access effective entitlements for tenant '{tenant_id}'.",
                message_bn="অন্য ফার্মেসির কার্যকর এনটাইটেলমেন্ট ও কোটা দেখার অনুমতি নেই।",
            )

    def retrieve(self, request, pk=None):
        tenant_id = str(pk or "").strip()
        if not tenant_id:
            raise ValidationError(detail={"tenant_id": ["Tenant ID is required."]})

        self._verify_tenant_access(request, tenant_id)

        device_id = request.query_params.get("device_id") or request.headers.get("X-Device-Id")
        branch_id = request.query_params.get("branch_id") or request.headers.get("X-Branch-Id")

        if device_id:
            clean_dev = device_id.strip()
            if len(clean_dev) < 3 or len(clean_dev) > 64:
                raise ValidationError(detail={"device_id": ["Device ID must be between 3 and 64 characters."]})

        from apps.core.feature_entitlement_service import FeatureEntitlementEngine

        try:
            result = FeatureEntitlementEngine.evaluate_effective_entitlements(
                tenant_id=tenant_id,
                device_id=device_id,
                branch_id=branch_id,
            )
        except ValueError as err:
            raise NotFound(detail=str(err))

        return Response(
            {
                "success": True,
                "data": result,
            },
            status=status.HTTP_200_OK,
        )
