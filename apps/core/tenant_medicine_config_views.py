"""
backend/apps/core/tenant_medicine_config_views.py
Architecture Decision: ADR-0004, ADR-0005, ADR-0070 & ADR-0071
Tenant-Specific Medicine Configuration & Local Overrides ViewSet.
Provides RESTful CRUD operations with strict tenant isolation,
cashier privilege guards, and DGDA regulatory non-relaxation enforcement.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.models import TenantMedicineConfig
from apps.core.pagination import SafeAdaptivePagination
from apps.core.serializers import TenantMedicineConfigSerializer
from apps.core.tenant_medicine_config_service import TenantMedicineConfigService
from apps.identity.models import UserRole


class TenantMedicineConfigViewSet(viewsets.ModelViewSet):
    """
    Tenant-Scoped Medicine Configuration API ViewSet.
    Enforces strict tenant isolation and role-based access control.
    """

    queryset = TenantMedicineConfig.objects.all().select_related("product", "tenant")
    serializer_class = TenantMedicineConfigSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = SafeAdaptivePagination

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
        Tenant staff cannot access foreign tenants.
        """
        user = getattr(request, "user", None)
        user_tenant = getattr(user, "tenant_id", None) or getattr(request, "tenant_id", None)
        is_platform = self._is_platform_operator(user)

        query_tenant = request.query_params.get("tenant_id") or request.data.get("tenant_id")
        if is_platform:
            target = query_tenant or user_tenant or "default-tenant"
            return str(target).strip()

        # Regular tenant user:
        if not user_tenant:
            raise PermissionDenied("User is not associated with an authorized pharmacy tenant.")

        if query_tenant and str(query_tenant).strip() != str(user_tenant).strip():
            raise CrossTenantViolationError(
                message_en=(
                    f"Cross-tenant access violation: Cannot access medicine configurations "
                    f"for foreign tenant '{query_tenant}'."
                ),
                message_bn="ভিন্ন ফার্মেসির ওষুধ কনফিগারেশন অ্যাক্সেস করা সম্পূর্ণ নিষিদ্ধ।",
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
                    "error_code": "TENANT_CONFIG_MUTATION_RESTRICTED",
                    "message_en": (
                        "Cashiers and unauthorized store personnel cannot modify tenant medicine configurations. "
                        "Contact your pharmacy owner or registered pharmacist."
                    ),
                    "message_bn": "ক্যাশিয়ার বা সাধারণ কর্মীরা ফার্মেসির ওষুধ কনফিগারেশন পরিবর্তন করতে পারবেন না।",
                }
            )

    def get_queryset(self):
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            return TenantMedicineConfig.objects.none()

        target_tenant = self._get_target_tenant_id(self.request)
        qs = TenantMedicineConfig.objects.filter(tenant_id=target_tenant).select_related("product", "tenant")

        # Optional filters
        is_vis = self.request.query_params.get("is_visible")
        if is_vis is not None and is_vis.strip() != "":
            qs = qs.filter(is_visible=is_vis.lower() in ("true", "1"))

        search = self.request.query_params.get("search")
        if search:
            clean = search.strip()
            qs = qs.filter(
                models.Q(local_brand_name__icontains=clean)
                | models.Q(local_sku__icontains=clean)
                | models.Q(product__brand_name__icontains=clean)
                | models.Q(product__generic_name__icontains=clean)
            )

        return qs

    def create(self, request, *args, **kwargs):
        """Creates or upserts tenant medicine configuration."""
        self._require_mutation_permission(request)
        target_tenant = self._get_target_tenant_id(request)
        data = request.data or {}

        product_id = data.get("product_id") or data.get("product")
        if not product_id:
            raise ValidationError({"product_id": "Master product_id is required."})

        try:
            result = TenantMedicineConfigService.upsert_config(
                tenant_id=target_tenant,
                product_id=product_id,
                data=data,
                actor=request.user,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "TENANT_MEDICINE_CONFIG_SAVED",
                "message_en": f"Tenant medicine configuration for '{product_id}' saved successfully.",
                "message_bn": f"ওষুধ '{product_id}'-এর লোকাল কনফিগারেশন সফলভাবে সংরক্ষিত হয়েছে।",
                "data": result,
            },
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        """Updates an existing tenant medicine configuration."""
        self._require_mutation_permission(request)
        instance = self.get_object()
        data = request.data or {}

        try:
            result = TenantMedicineConfigService.upsert_config(
                tenant_id=instance.tenant_id,
                product_id=instance.product_id,
                data=data,
                actor=request.user,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "TENANT_MEDICINE_CONFIG_UPDATED",
                "message_en": f"Tenant medicine configuration '{instance.config_id}' updated.",
                "message_bn": f"লোকাল কনফিগারেশন '{instance.config_id}' আপডেট করা হয়েছে।",
                "data": result,
            },
            status=status.HTTP_200_OK,
        )

    def destroy(self, request, *args, **kwargs):
        """Deletes configuration, resetting medicine back to global master defaults."""
        self._require_mutation_permission(request)
        instance = self.get_object()
        target_tenant = instance.tenant_id
        config_id = instance.config_id

        TenantMedicineConfigService.delete_config(
            tenant_id=target_tenant,
            config_id_or_product_id=config_id,
            actor=request.user,
        )

        return Response(
            {
                "success": True,
                "code": "TENANT_MEDICINE_CONFIG_RESET",
                "message_en": f"Medicine configuration reset to master defaults for tenant '{target_tenant}'.",
                "message_bn": "ওষুধের কনফিগারেশন গ্লোবাল মাস্টার ডিফল্টে রিসেট করা হয়েছে।",
                "data": {"config_id": config_id, "status": "RESET_TO_MASTER_DEFAULTS"},
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path=r"by-product/(?P<product_id>[^/.]+)")
    def by_product(self, request, product_id=None):
        """Retrieves tenant configuration for a specific product."""
        target_tenant = self._get_target_tenant_id(request)
        config = TenantMedicineConfigService.get_config(tenant_id=target_tenant, product_id=product_id)

        if not config:
            return Response(
                {
                    "success": True,
                    "code": "TENANT_MEDICINE_CONFIG_DEFAULT",
                    "message_en": "No custom override found. Medicine uses global master defaults.",
                    "message_bn": "কোনো কাস্টম কনফিগারেশন নেই। ওষুধটি মাস্টার ডিফল্ট ব্যবহার করছে।",
                    "data": {
                        "tenant_id": target_tenant,
                        "product_id": product_id,
                        "is_overridden": False,
                    },
                },
                status=status.HTTP_200_OK,
            )

        return Response(
            {
                "success": True,
                "code": "TENANT_MEDICINE_CONFIG_RETRIEVED",
                "message_en": f"Tenant configuration for '{product_id}' retrieved.",
                "message_bn": f"ওষুধ '{product_id}'-এর কনফিগারেশন প্রাপ্ত হয়েছে।",
                "data": config,
            },
            status=status.HTTP_200_OK,
        )
