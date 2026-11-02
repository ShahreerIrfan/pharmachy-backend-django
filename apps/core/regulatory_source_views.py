"""
backend/apps/core/regulatory_source_views.py
Architecture Decision: ADR-0134 (P134).
REST API ViewSet for Regulatory Source Attachments:
Legal Citations, Storage Rule Temperature Validations, Review Deadlines, and Historical Immutability.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import (
    RegulatorySourceAttachment,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.regulatory_source_service import RegulatorySourceService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.core.regulatory_source.views")


class RegulatorySourceAttachmentViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for Regulatory Source Attachments, Storage Rules, and Periodic Review Cycles.
    Enforces RBAC, subscription entitlement checks, cold chain parameter validations,
    and due-review compliance audit filtering.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _is_platform_operator(self, user) -> bool:
        if not user or not user.is_authenticated:
            return False
        return bool(
            getattr(user, "is_superuser", False)
            or str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]
        )

    def _require_regulatory_role(self, request):
        """Cashiers and unauthorized roles are denied from managing regulatory sources."""
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
                    "message_en": "Cashiers cannot attach or mutate regulatory and storage rules. Contact store pharmacist.",
                    "message_bn": "ক্যাশিয়াররা রেগুলেটরি বা স্টোরেজ রুল পরিবর্তন করতে পারবেন না। ফার্মাসিস্টের সাথে যোগাযোগ করুন।",
                }
            )

    def _check_tenant_status(self, request):
        """Guard against suspended tenants mutating or viewing regulatory sources."""
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
                        "message_en": "Tenant account is suspended. Regulatory source management access denied.",
                        "message_bn": "ফার্মেসি অ্যাকাউন্টটি স্থগিত থাকায় রেগুলেটরি সোর্স সুবিধা বন্ধ রয়েছে।",
                    }
                )

    def create(self, request) -> Response:
        """
        POST /api/v2/catalog/regulatory-sources/
        Attaches a new legal/manufacturer source to a medicine product.
        """
        self._require_regulatory_role(request)
        self._check_tenant_status(request)

        data = request.data or {}
        try:
            result = RegulatorySourceService.attach_source(
                product_id=data.get("product_id"),
                source_type=data.get("source_type"),
                rule_target=data.get("rule_target"),
                document_reference_number=data.get("document_reference_number"),
                issuing_authority=data.get("issuing_authority"),
                effective_date=data.get("effective_date"),
                publication_date=data.get("publication_date"),
                review_cycle_months=data.get("review_cycle_months", 12),
                next_review_due_date=data.get("next_review_due_date"),
                source_url=data.get("source_url", ""),
                storage_rule_specs=data.get("storage_rule_specs"),
                classification_specs=data.get("classification_specs"),
                actor=request.user,
                review_notes=data.get("review_notes", ""),
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)

    def list(self, request) -> Response:
        """
        GET /api/v2/catalog/regulatory-sources/
        Lists regulatory source attachments with optional filters and full-text search.
        """
        self._require_regulatory_role(request)
        self._check_tenant_status(request)

        product_id = request.query_params.get("product_id")
        rule_target = request.query_params.get("rule_target")
        source_type = request.query_params.get("source_type")
        is_active_raw = request.query_params.get("is_active")
        is_active = is_active_raw.lower() in ("true", "1") if is_active_raw is not None else None
        search = request.query_params.get("search")

        results = RegulatorySourceService.list_sources(
            product_id=product_id,
            rule_target=rule_target,
            source_type=source_type,
            is_active=is_active,
            search=search,
        )
        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        """
        GET /api/v2/catalog/regulatory-sources/{id}/
        Retrieves a single regulatory source attachment record.
        """
        self._require_regulatory_role(request)
        self._check_tenant_status(request)

        attachment = (
            RegulatorySourceAttachment.objects.select_related("product", "reviewed_by")
            .filter(attachment_id=str(pk).strip())
            .first()
        )
        if not attachment:
            raise NotFound({"detail": f"Regulatory source attachment '{pk}' not found."})

        return Response(RegulatorySourceService.serialize_attachment(attachment), status=status.HTTP_200_OK)

    def partial_update(self, request, pk=None) -> Response:
        """
        PATCH /api/v2/catalog/regulatory-sources/{id}/
        Updates notes, URLs, review dates, or storage/classification parameters.
        """
        self._require_regulatory_role(request)
        self._check_tenant_status(request)

        try:
            result = RegulatorySourceService.update_source(
                attachment_id=pk,
                data=request.data or {},
                actor=request.user,
            )
            return Response(result, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)

    @action(detail=True, methods=["post"], url_path="archive")
    def archive(self, request, pk=None) -> Response:
        """
        POST /api/v2/catalog/regulatory-sources/{id}/archive/
        Soft-archives a superseded or rescinded regulatory source attachment.
        """
        self._require_regulatory_role(request)
        self._check_tenant_status(request)

        reason = request.data.get("reason", "")
        try:
            result = RegulatorySourceService.archive_source(
                attachment_id=pk,
                actor=request.user,
                reason=reason,
            )
            return Response(result, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)

    @action(detail=False, methods=["get"], url_path="due-reviews")
    def due_reviews(self, request) -> Response:
        """
        GET /api/v2/catalog/regulatory-sources/due-reviews/
        Queries attachments requiring periodic compliance re-review on or before target_date.
        """
        self._require_regulatory_role(request)
        self._check_tenant_status(request)

        target_date = request.query_params.get("target_date")
        try:
            results = RegulatorySourceService.get_due_reviews(target_date=target_date)
            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)
