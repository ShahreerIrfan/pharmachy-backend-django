"""
backend/apps/core/product_metadata_review_views.py
Architecture Decision: ADR-0133 (P133).
REST API ViewSet for Pharmacist/Editor Review of Medicine Metadata Changes:
Diff Inspection, Dual Control (Four-Eyes Principle), Publication Lifecycle, and Rejection Justifications.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import ProductMetadataReview, Tenant, TenantLifecycleStatus
from apps.core.product_metadata_review_service import ProductMetadataReviewService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.core.product_metadata_review.views")


class ProductMetadataReviewViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for Medicine Metadata Review Lifecycle.
    Enforces Four-Eyes Principle (Dual Control), Pharmacist/Admin RBAC,
    Pending Review Queue, and Change Diff Merging.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _is_platform_operator(self, user) -> bool:
        if not user or not user.is_authenticated:
            return False
        return bool(
            getattr(user, "is_superuser", False)
            or str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]
        )

    def _require_review_role(self, request):
        """Cashiers and unauthorized roles are denied from review operations."""
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
                    "message_en": "Cashiers cannot participate in medicine metadata review workflows. Contact store pharmacist.",
                    "message_bn": "ক্যাশিয়াররা মেডিসিন মেটাডাটা রিভিউ কার্যক্রমে অংশগ্রহণ করতে পারবেন না। ফার্মাসিস্টের সাথে যোগাযোগ করুন।",
                }
            )

    def _check_tenant_status(self, request):
        """Guard against suspended tenants mutating or reviewing catalog metadata."""
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
                        "message_en": "Tenant account is suspended. Medicine metadata review access denied.",
                        "message_bn": "ফার্মেসি অ্যাকাউন্টটি স্থগিত থাকায় মেটাডাটা রিভিউ সুবিধা বন্ধ রয়েছে।",
                    }
                )

    def create(self, request) -> Response:
        """
        POST /api/v2/catalog/reviews/
        Submit a new medicine metadata review proposal with diff tracking.
        """
        self._require_review_role(request)
        self._check_tenant_status(request)

        data = request.data or {}
        product_id = data.get("product_id")
        change_type = data.get("change_type", "METADATA_UPDATE")
        proposed_changes = data.get("proposed_changes", {})
        submission_notes = data.get("submission_notes", "")

        try:
            result = ProductMetadataReviewService.propose_review(
                product_id=product_id,
                change_type=change_type,
                proposed_changes=proposed_changes,
                actor=request.user,
                submission_notes=submission_notes,
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)

    def list(self, request) -> Response:
        """
        GET /api/v2/catalog/reviews/
        List reviews filtered optionally by product_id or status.
        """
        self._require_review_role(request)
        self._check_tenant_status(request)

        product_id = request.query_params.get("product_id")
        status_filter = request.query_params.get("status")

        reviews = ProductMetadataReviewService.list_reviews(
            product_id=product_id,
            status_filter=status_filter,
        )
        return Response({"count": len(reviews), "results": reviews}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        """
        GET /api/v2/catalog/reviews/{id}/
        Retrieve single metadata review record with full change diff.
        """
        self._require_review_role(request)
        self._check_tenant_status(request)

        review = (
            ProductMetadataReview.objects.select_related("product", "submitted_by", "reviewed_by")
            .filter(review_id=str(pk).strip())
            .first()
        )
        if not review:
            raise NotFound({"detail": f"Review record '{pk}' not found."})

        return Response(ProductMetadataReviewService.serialize_review(review), status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="pending")
    def pending(self, request) -> Response:
        """
        GET /api/v2/catalog/reviews/pending/
        Pending reviews queue awaiting pharmacist sign-off.
        """
        self._require_review_role(request)
        self._check_tenant_status(request)

        reviews = ProductMetadataReviewService.list_reviews(status_filter="PENDING_REVIEW")
        return Response({"count": len(reviews), "results": reviews}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None) -> Response:
        """
        POST /api/v2/catalog/reviews/{id}/approve/
        Pharmacist review approval. Merges diff into master product, increments version,
        and enforces Four-Eyes Principle.
        """
        self._require_review_role(request)
        self._check_tenant_status(request)

        notes = request.data.get("review_notes", "")
        try:
            result = ProductMetadataReviewService.decide_review(
                review_id=pk,
                decision="APPROVE",
                actor=request.user,
                review_notes=notes,
            )
            return Response(result, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None) -> Response:
        """
        POST /api/v2/catalog/reviews/{id}/reject/
        Rejects proposal with mandatory justification notes.
        """
        self._require_review_role(request)
        self._check_tenant_status(request)

        notes = request.data.get("review_notes", "")
        try:
            result = ProductMetadataReviewService.decide_review(
                review_id=pk,
                decision="REJECT",
                actor=request.user,
                review_notes=notes,
            )
            return Response(result, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)

    @action(detail=True, methods=["post"], url_path="request-changes")
    def request_changes(self, request, pk=None) -> Response:
        """
        POST /api/v2/catalog/reviews/{id}/request-changes/
        Requests revisions on proposed metadata changes.
        """
        self._require_review_role(request)
        self._check_tenant_status(request)

        notes = request.data.get("review_notes", "")
        try:
            result = ProductMetadataReviewService.decide_review(
                review_id=pk,
                decision="REQUEST_CHANGES",
                actor=request.user,
                review_notes=notes,
            )
            return Response(result, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)
