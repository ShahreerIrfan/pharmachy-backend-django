"""
backend/apps/core/medicine_form_template_views.py
Architecture Decision: ADR-0131 (P131).
REST API ViewSet for Dosage-Form Medicine Form Templates, Layout Rules,
Allowed Values, and Dynamic UI Configuration.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.medicine_form_template_service import MedicineFormTemplateService
from apps.core.models import Tenant, TenantLifecycleStatus

logger = logging.getLogger("pharmacy.core.medicine_form_template.views")


class MedicineFormTemplateViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for querying and managing Dosage-Form Medicine Form Templates.
    Enforces platform curation ownership, tenant status checks, and publishes
    responsive layout rules, widgets, and allowed clinical/packaging values.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _is_platform_operator(self, user) -> bool:
        if not user or not user.is_authenticated:
            return False
        return bool(
            getattr(user, "is_superuser", False)
            or str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]
        )

    def _check_tenant_status(self, request):
        """Guard against suspended tenants accessing catalog template services."""
        user = getattr(request, "user", None)
        if self._is_platform_operator(user):
            return

        tenant_id = getattr(user, "tenant_id", None) or getattr(request, "tenant_id", None)
        if tenant_id:
            tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
            if tenant and tenant.status == TenantLifecycleStatus.SUSPENDED:
                raise PermissionDenied(
                    detail={
                        "error_code": "ERR_TENANT_SUSPENDED",
                        "message_en": "Tenant subscription account is suspended. Access to catalog templates denied.",
                        "message_bn": "ফার্মেসির সাবস্ক্রিপশন অ্যাকাউন্ট স্থগিত থাকায় টেমপ্লেট অ্যাক্সেস বন্ধ রয়েছে।",
                    }
                )

    def _require_platform_operator(self, request):
        """Restricts template mutations exclusively to Platform Superadmins / Operators."""
        user = getattr(request, "user", None)
        if not self._is_platform_operator(user):
            raise PermissionDenied(
                detail={
                    "error_code": "PLATFORM_CATALOG_OWNERSHIP_PROTECTED",
                    "message_en": "Tenant users cannot create or mutate global dosage-form templates.",
                    "message_bn": "টেন্যান্ট ব্যবহারকারীরা গ্লোবাল ডোজ-ফর্ম টেমপ্লেট তৈরি বা পরিবর্তন করতে পারবেন না।",
                }
            )

    def list(self, request) -> Response:
        """
        GET /api/v2/catalog/templates/
        List published dosage-form templates with optional filters.
        """
        self._check_tenant_status(request)

        dosage_form = request.query_params.get("dosage_form")
        search = request.query_params.get("search")
        is_active_param = request.query_params.get("is_active")
        is_active = None
        if is_active_param is not None:
            is_active = is_active_param.lower() in ("true", "1")

        results = MedicineFormTemplateService.list_templates(
            dosage_form=dosage_form,
            is_active=is_active,
            search=search,
        )
        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        """
        GET /api/v2/catalog/templates/{id_or_code}/
        Retrieve single dosage-form template by template_id, template_code, or dosage_form_code.
        """
        self._check_tenant_status(request)

        template = MedicineFormTemplateService.get_template(pk)
        if not template:
            return Response(
                {
                    "error_code": "ERR_TEMPLATE_NOT_FOUND",
                    "message_en": f"No active dosage-form template found for '{pk}'.",
                    "message_bn": f"'{pk}'-এর জন্য কোনো সক্রিয় ডোজ-ফর্ম টেমপ্লেট পাওয়া যায়নি।",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(template, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        """
        POST /api/v2/catalog/templates/
        Create a new dosage-form template (Platform Superadmin only).
        """
        self._require_platform_operator(request)

        try:
            created = MedicineFormTemplateService.create_template(
                data=request.data,
                actor=request.user,
            )
            return Response(created, status=status.HTTP_201_CREATED)
        except DjangoValidationError as exc:
            if hasattr(exc, "message_dict"):
                raise ValidationError(exc.message_dict)
            raise ValidationError(str(exc))
        except Exception as exc:
            raise ValidationError(str(exc))

    @action(detail=False, methods=["get"], url_path="by-dosage-form")
    def by_dosage_form(self, request) -> Response:
        """
        GET /api/v2/catalog/templates/by-dosage-form/?dosage_form=TABLET
        Fast query returning the active default template for a dosage form.
        """
        self._check_tenant_status(request)

        dosage_form = request.query_params.get("dosage_form")
        if not dosage_form:
            raise ValidationError({"dosage_form": "dosage_form query parameter is required."})

        template = MedicineFormTemplateService.get_template(dosage_form)
        if not template:
            return Response(
                {
                    "error_code": "ERR_DOSAGE_FORM_TEMPLATE_NOT_FOUND",
                    "message_en": f"No default template configured for dosage form '{dosage_form}'.",
                    "message_bn": f"ডোজ-ফর্ম '{dosage_form}'-এর জন্য কোনো ডিফল্ট টেমপ্লেট পাওয়া যায়নি।",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(template, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="allowed-values")
    def allowed_values(self, request) -> Response:
        """
        GET /api/v2/catalog/templates/allowed-values/?dosage_form=TABLET
        Returns allowed values (units, routes, container types) for a dosage form.
        """
        self._check_tenant_status(request)

        dosage_form = request.query_params.get("dosage_form")
        if not dosage_form:
            raise ValidationError({"dosage_form": "dosage_form query parameter is required."})

        template = MedicineFormTemplateService.get_template(dosage_form)
        if not template:
            return Response(
                {
                    "error_code": "ERR_DOSAGE_FORM_TEMPLATE_NOT_FOUND",
                    "message_en": f"No template found for dosage form '{dosage_form}'.",
                    "message_bn": f"ডোজ-ফর্ম '{dosage_form}'-এর জন্য কোনো টেমপ্লেট পাওয়া যায়নি।",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "dosage_form": template["dosage_form_code"],
                "template_code": template["template_code"],
                "template_version": template["template_version"],
                "allowed_values": template["allowed_values"],
            },
            status=status.HTTP_200_OK,
        )
