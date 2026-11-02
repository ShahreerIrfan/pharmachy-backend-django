"""
backend/apps/core/override_precedence_views.py
Architecture Decision: ADR-0128 (P128).
API ViewSet for Global and Tenant Override Precedence, Moderation & Audit History.
Provides endpoints to resolve effective medicine presentation, propose store-specific overrides,
perform platform compliance moderation, and trace full field-by-field provenance.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.override_precedence_service import GlobalTenantOverridePrecedenceService
from apps.identity.models import UserRole


class OverridePrecedenceViewSet(viewsets.ViewSet):
    """
    Catalog Override Precedence ViewSet.
    Handles resolution, tenant overrides, moderation lifecycle, and audit logs.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _is_platform_operator(self, user) -> bool:
        if not user or not user.is_authenticated:
            return False
        return bool(
            getattr(user, "is_superuser", False)
            or str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]
        )

    def _get_target_tenant_id(self, request) -> str:
        """
        Determines and strictly validates the tenant scope.
        Tenant staff cannot query or mutate foreign tenant overrides.
        """
        user = getattr(request, "user", None)
        user_tenant = getattr(user, "tenant_id", None) or getattr(request, "tenant_id", None)
        is_platform = self._is_platform_operator(user)

        query_tenant = request.query_params.get("tenant_id") or request.data.get("tenant_id")
        if is_platform:
            target = query_tenant or user_tenant or "default-tenant"
            return str(target).strip()

        if not user_tenant:
            raise PermissionDenied("User is not associated with an authorized pharmacy tenant.")

        if query_tenant and str(query_tenant).strip() != str(user_tenant).strip():
            raise CrossTenantViolationError(
                message_en=f"ERR_CROSS_TENANT_ACCESS: Cannot access or mutate overrides for foreign tenant '{query_tenant}'.",
                message_bn="ভিন্ন ফার্মেসির ওষুধ ওভাররাইড অ্যাক্সেস করা সম্পূর্ণ নিষিদ্ধ।",
            )

        return str(user_tenant).strip()

    def _require_mutation_permission(self, request):
        """
        Guards mutating actions: Cashiers cannot mutate tenant configurations.
        Allowed roles: SUPERADMIN, PLATFORM_OPERATOR, PHARMACY_OWNER, BRANCH_MANAGER, STAFF_PHARMACIST.
        """
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials were not provided.")

        if self._is_platform_operator(user):
            return

        role = str(getattr(user, "role", "")).upper().strip()
        allowed = {
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            "OWNER",
            "MANAGER",
            "PHARMACIST",
        }
        if role not in allowed:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": "Cashiers cannot mutate tenant medicine overrides. Contact your store owner or manager.",
                    "message_bn": "ক্যাশিয়ার বা সাধারণ কর্মীরা ফার্মেসির ওষুধ ওভাররাইড পরিবর্তন করতে পারবেন না।",
                }
            )

    @action(detail=False, methods=["get"], url_path="resolve")
    def resolve_medicine(self, request):
        """
        GET /api/v1/catalog/overrides/resolve/?product_id=<id>&tenant_id=<id>
        Resolves effective attributes with full field-by-field provenance trace.
        """
        product_id = request.query_params.get("product_id")
        if not product_id:
            raise ValidationError({"product_id": "Master product_id parameter is required."})

        target_tenant = self._get_target_tenant_id(request)

        try:
            resolved = GlobalTenantOverridePrecedenceService.resolve_medicine(
                tenant_id=target_tenant,
                product_id=product_id,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "MEDICINE_PRECEDENCE_RESOLVED",
                "message_en": f"Precedence resolved for medicine '{product_id}'.",
                "message_bn": f"ওষুধ '{product_id}'-এর প্রিসিডেন্স সফলভাবে নির্ণয় করা হয়েছে।",
                "data": resolved,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="resolve-variant")
    def resolve_variant(self, request):
        """
        GET /api/v1/catalog/overrides/resolve-variant/?variant_id=<id>&tenant_id=<id>
        Resolves commercial variant SKU presentation with parent master inheritance.
        """
        variant_id = request.query_params.get("variant_id")
        if not variant_id:
            raise ValidationError({"variant_id": "Commercial variant_id parameter is required."})

        target_tenant = self._get_target_tenant_id(request)

        try:
            resolved = GlobalTenantOverridePrecedenceService.resolve_variant(
                tenant_id=target_tenant,
                variant_id=variant_id,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "VARIANT_PRECEDENCE_RESOLVED",
                "message_en": f"Precedence resolved for variant '{variant_id}'.",
                "message_bn": f"ভ্যারিয়েন্ট '{variant_id}'-এর প্রিসিডেন্স সফলভাবে নির্ণয় করা হয়েছে।",
                "data": resolved,
            },
            status=status.HTTP_200_OK,
        )

    def create(self, request):
        """
        POST /api/v1/catalog/overrides/
        Creates or updates a tenant medicine override with anti-gouging moderation evaluation.
        """
        self._require_mutation_permission(request)
        target_tenant = self._get_target_tenant_id(request)
        data = request.data or {}

        product_id = data.get("product_id")
        if not product_id:
            raise ValidationError({"product_id": "product_id is required in payload."})

        try:
            resolved = GlobalTenantOverridePrecedenceService.upsert_override(
                tenant_id=target_tenant,
                product_id=product_id,
                data=data,
                actor=request.user,
                variant_id=data.get("variant_id"),
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "TENANT_OVERRIDE_SAVED",
                "message_en": f"Tenant override for '{product_id}' processed successfully.",
                "message_bn": f"ওষুধ '{product_id}'-এর ওভাররাইড সফলভাবে সংরক্ষিত হয়েছে।",
                "data": resolved,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path=r"moderate/(?P<config_id>[^/.]+)")
    def moderate(self, request, config_id=None):
        """
        POST /api/v1/catalog/overrides/moderate/<config_id>/
        Platform superadmin moderates a pending or flagged override (APPROVE/REJECT).
        """
        if not self._is_platform_operator(request.user):
            raise PermissionDenied("Only Platform Superadmins can moderate medicine overrides.")

        data = request.data or {}
        action_name = data.get("action")
        reason = data.get("reason", "")

        try:
            resolved = GlobalTenantOverridePrecedenceService.moderate_override(
                config_id=config_id,
                action=action_name,
                reason=reason,
                actor=request.user,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "OVERRIDE_MODERATED",
                "message_en": f"Override '{config_id}' moderated: {action_name}.",
                "message_bn": f"ওভাররাইড '{config_id}' মডারেশন সম্পন্ন: {action_name}।",
                "data": resolved,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="reset")
    def reset_to_master(self, request):
        """
        POST /api/v1/catalog/overrides/reset/
        Resets override for a product, cleanly reverting back to global master defaults.
        """
        self._require_mutation_permission(request)
        target_tenant = self._get_target_tenant_id(request)
        product_id = request.data.get("product_id") or request.query_params.get("product_id")
        if not product_id:
            raise ValidationError({"product_id": "product_id is required to reset."})

        resolved = GlobalTenantOverridePrecedenceService.reset_override(
            tenant_id=target_tenant,
            product_id=product_id,
            actor=request.user,
        )

        return Response(
            {
                "success": True,
                "code": "OVERRIDE_RESET_TO_MASTER",
                "message_en": f"Medicine '{product_id}' reset to global master defaults.",
                "message_bn": f"ওষুধ '{product_id}' গ্লোবাল মাস্টার ডিফল্টে রিসেট করা হয়েছে।",
                "data": resolved,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="audit-history")
    def audit_history(self, request):
        """
        GET /api/v1/catalog/overrides/audit-history/?product_id=<id>&tenant_id=<id>
        Returns immutable audit change timeline for this medicine's override.
        """
        product_id = request.query_params.get("product_id")
        if not product_id:
            raise ValidationError({"product_id": "product_id query parameter is required."})

        target_tenant = self._get_target_tenant_id(request)
        history = GlobalTenantOverridePrecedenceService.get_audit_history(
            tenant_id=target_tenant,
            product_id=product_id,
        )

        return Response(
            {
                "success": True,
                "code": "OVERRIDE_AUDIT_HISTORY_RETRIEVED",
                "message_en": f"Audit history for '{product_id}' retrieved.",
                "message_bn": f"ওষুধ '{product_id}'-এর অডিট হিস্টোরি প্রাপ্ত হয়েছে।",
                "data": {"product_id": product_id, "tenant_id": target_tenant, "history": history},
            },
            status=status.HTTP_200_OK,
        )
