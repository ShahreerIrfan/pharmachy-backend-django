"""
backend/apps/core/catalogue_recall_views.py
Architecture Decision: ADR-0137 (P137).
REST API ViewSet for Discontinued & Recalled Catalogue Governance:
Safety Recalls, Market Withdrawals, POS Dispensing Blocks, and Lifecycle Audit Tracking.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response

from apps.core.catalogue_recall_service import CatalogueRecallService
from apps.core.models import (
    CatalogueLifecycleNotice,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.core.catalogue_recall.views")


class CatalogueLifecycleNoticeViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for Discontinued & Recalled Catalogue Governance.
    Protects patient safety by hard-blocking recalled or withdrawn stock at POS checkout,
    while maintaining permanent relational integrity with historical invoices and prescriptions.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _is_platform_operator(self, user) -> bool:
        if not user or not user.is_authenticated:
            return False
        return bool(
            getattr(user, "is_superuser", False)
            or str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]
        )

    def _require_authorized_role(self, request):
        """Cashiers cannot publish, update, or lift recall notices."""
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
                    "message_en": "Cashiers cannot publish, modify, or lift recall notices. Contact pharmacist or branch manager.",
                    "message_bn": "ক্যাশিয়াররা রিকল নোটিশ প্রকাশ, পরিবর্তন বা প্রত্যাহার করতে পারবেন না। ফার্মাসিস্ট বা ব্রাঞ্চ ম্যানেজারের সাথে যোগাযোগ করুন।",
                }
            )

    def _check_tenant_status(self, request):
        """Guard against suspended tenants mutating lifecycle notices."""
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
                        "message_en": "Tenant account is suspended. Catalogue recall modification access denied.",
                        "message_bn": "ফার্মেসি অ্যাকাউন্টটি স্থগিত থাকায় ক্যাটালগ রিকল নোটিশ পরিবর্তন করা বন্ধ রয়েছে।",
                    }
                )

    def create(self, request) -> Response:
        """
        POST /api/v2/catalog/recalls/
        Publishes a new statutory recall, withdrawal, or discontinuation notice.
        """
        self._require_authorized_role(request)
        self._check_tenant_status(request)

        user = request.user
        tenant_id = (
            request.data.get("tenant_id")
            if self._is_platform_operator(user) and request.data.get("tenant_id")
            else getattr(user, "tenant_id", "platform")
        )

        data = request.data or {}
        try:
            result = CatalogueRecallService.publish_notice(
                target_entity_type=data.get("target_entity_type", "MEDICINE"),
                lifecycle_status=data.get("lifecycle_status", "RECALLED"),
                regulatory_authority=data.get("regulatory_authority", ""),
                notice_number=data.get("notice_number", ""),
                reason=data.get("reason", ""),
                effective_date=data.get("effective_date"),
                product_id=data.get("product_id"),
                non_medicine_item_id=data.get("non_medicine_item_id"),
                recall_scope=data.get("recall_scope", "NATIONWIDE_ALL_BATCHES"),
                affected_batch_numbers=data.get("affected_batch_numbers"),
                action_required=data.get("action_required", ""),
                actor=user,
                tenant_id=tenant_id,
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except DjangoValidationError as exc:
            msg_dict = exc.message_dict if hasattr(exc, "message_dict") else {"detail": exc.messages}
            return Response(msg_dict, status=status.HTTP_400_BAD_REQUEST)

    def list(self, request) -> Response:
        """
        GET /api/v2/catalog/recalls/
        Lists recall and discontinuation notices. Accessible to all staff including cashiers.
        """
        user = request.user
        tenant_id = (
            request.query_params.get("tenant_id")
            if self._is_platform_operator(user) and request.query_params.get("tenant_id")
            else getattr(user, "tenant_id", None)
        )

        product_id = request.query_params.get("product_id")
        non_medicine_item_id = request.query_params.get("non_medicine_item_id")
        lifecycle_status = request.query_params.get("lifecycle_status")
        is_active_raw = request.query_params.get("is_active")
        is_active = is_active_raw.lower() in ("true", "1") if is_active_raw is not None else None
        search = request.query_params.get("search")

        results = CatalogueRecallService.list_notices(
            product_id=product_id,
            non_medicine_item_id=non_medicine_item_id,
            tenant_id=tenant_id,
            lifecycle_status=lifecycle_status,
            is_active=is_active,
            search=search,
        )
        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        """
        GET /api/v2/catalog/recalls/{id}/
        Retrieves a single lifecycle recall notice.
        """
        record = CatalogueLifecycleNotice.objects.filter(notice_id=str(pk).strip()).first()
        if not record:
            raise NotFound({"detail": f"Catalogue recall notice '{pk}' not found."})

        return Response(CatalogueRecallService.serialize_notice(record), status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="check")
    def check(self, request) -> Response:
        """
        GET /api/v2/catalog/recalls/check/?product_id=...&batch_number=...
        High-speed POS dispensing safety check.
        Returns whether dispensing is blocked and regulatory notice details.
        Accessible to all staff including cashiers.
        """
        product_id = request.query_params.get("product_id")
        non_medicine_item_id = request.query_params.get("non_medicine_item_id")
        batch_number = request.query_params.get("batch_number")
        user = request.user
        tenant_id = getattr(user, "tenant_id", None)

        result = CatalogueRecallService.check_recall_status(
            product_id=product_id,
            non_medicine_item_id=non_medicine_item_id,
            batch_number=batch_number,
            tenant_id=tenant_id,
        )
        if result.get("blocked"):
            result["error_code"] = "ERR_PRODUCT_RECALLED_DISPENSING_BLOCKED"
            result["message_en"] = "Dispensing blocked: Product or batch has an active statutory safety recall."
            result["message_bn"] = "বিক্রয় নিষিদ্ধ: পণ্য বা ব্যাচটিতে সক্রিয় সরকারি সেফটি রিকল রয়েছে।"

        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="validate-dispense")
    def validate_dispense(self, request) -> Response:
        """
        POST /api/v2/catalog/recalls/validate-dispense/
        Strict point-of-sale enforcement endpoint: returns HTTP 400 Bad Request
        if the requested item or batch is recalled or withdrawn.
        """
        data = request.data or {}
        product_id = data.get("product_id")
        non_medicine_item_id = data.get("non_medicine_item_id")
        batch_number = data.get("batch_number")
        user = request.user
        tenant_id = getattr(user, "tenant_id", None)

        result = CatalogueRecallService.check_recall_status(
            product_id=product_id,
            non_medicine_item_id=non_medicine_item_id,
            batch_number=batch_number,
            tenant_id=tenant_id,
        )
        if result.get("blocked"):
            return Response(
                {
                    "error_code": "ERR_PRODUCT_RECALLED_DISPENSING_BLOCKED",
                    "message_en": f"Dispensing blocked by notice {result.get('notice_number')}: {result.get('reason')}",
                    "message_bn": f"রিকল নোটিশ {result.get('notice_number')} এর কারণে ওষুধ প্রদান নিষিদ্ধ: {result.get('reason')}",
                    "details": result,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({"eligible_for_dispensing": True, "status": result.get("lifecycle_status")}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="lift")
    def lift(self, request, pk=None) -> Response:
        """
        POST /api/v2/catalog/recalls/{id}/lift/
        Officially lifts a recall or quarantine notice, clearing the dispensing block.
        """
        self._require_authorized_role(request)
        self._check_tenant_status(request)

        user = request.user
        tenant_id = getattr(user, "tenant_id", None) if not self._is_platform_operator(user) else None
        resolution_notes = request.data.get("resolution_notes", "")

        try:
            result = CatalogueRecallService.lift_notice(
                notice_id=pk,
                actor=user,
                tenant_id=tenant_id,
                resolution_notes=resolution_notes,
            )
            return Response(result, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            msg_dict = exc.message_dict if hasattr(exc, "message_dict") else {"detail": exc.messages}
            return Response(msg_dict, status=status.HTTP_400_BAD_REQUEST)
