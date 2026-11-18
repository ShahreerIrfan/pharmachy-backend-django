"""
backend/apps/core/party_privacy_views.py
Architecture Decision: ADR-0279 (P279).
Phase 14: Customers, Suppliers, Dues & Payments.

REST API ViewSet governing Party (Customer & Supplier) Data Privacy,
Statutory Retention Policies, Audited Corrections, Irreversible Anonymization,
and Restricted Subject Dossier Exports.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.party_data_privacy_service import PartyDataPrivacyService

logger = logging.getLogger(__name__)


def _get_request_tenant_id(request) -> str:
    user = getattr(request, "user", None)
    if user and getattr(user, "tenant_id", None):
        return str(user.tenant_id)
    tenant = getattr(request, "tenant", None)
    if tenant and getattr(tenant, "tenant_id", None):
        return str(tenant.tenant_id)
    header = request.META.get("HTTP_X_TENANT_ID")
    if header:
        return str(header)
    body_tenant = request.data.get("tenant_id")
    if body_tenant:
        return str(body_tenant)
    return ""


def _check_tenant_suspended(request, tenant_id: str):
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Party data privacy operations blocked.",
                "message_en": "Tenant account subscription is suspended. Party data privacy operations blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। ডাটা প্রাইভেসি অপারেশন বন্ধ করা হয়েছে।",
            }
        )


class PartyPrivacyViewSet(viewsets.ViewSet):
    """
    ViewSet governing party data privacy and statutory retention policies.
    Endpoints:
    - POST /api/v2/parties/privacy/assess-retention/
    - POST /api/v2/parties/privacy/correct/
    - POST /api/v2/parties/privacy/anonymize/
    - POST /api/v2/parties/privacy/restricted-export/
    """

    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["post"], url_path="assess-retention")
    def assess_retention(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        party_type = request.data.get("party_type", "CUSTOMER")
        party_id = request.data.get("party_id")
        if not party_id:
            raise ValidationError({"error_code": "ERR_MISSING_PARTY_ID", "detail": "party_id is required."})

        result = PartyDataPrivacyService.assess_retention_policy(
            actor_user=request.user,
            tenant_id=tenant_id,
            party_type=party_type,
            party_id=party_id,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="correct")
    def correct(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        party_type = request.data.get("party_type", "CUSTOMER")
        party_id = request.data.get("party_id")
        corrections = request.data.get("corrections") or {}
        reason = request.data.get("reason", "")

        if not party_id:
            raise ValidationError({"error_code": "ERR_MISSING_PARTY_ID", "detail": "party_id is required."})

        result = PartyDataPrivacyService.correct_party_data(
            actor_user=request.user,
            tenant_id=tenant_id,
            party_type=party_type,
            party_id=party_id,
            corrections=corrections,
            reason=reason,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="anonymize")
    def anonymize(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        party_type = request.data.get("party_type", "CUSTOMER")
        party_id = request.data.get("party_id")
        reason = request.data.get("reason", "")

        if not party_id:
            raise ValidationError({"error_code": "ERR_MISSING_PARTY_ID", "detail": "party_id is required."})

        result = PartyDataPrivacyService.anonymize_party_data(
            actor_user=request.user,
            tenant_id=tenant_id,
            party_type=party_type,
            party_id=party_id,
            reason=reason,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="restricted-export")
    def restricted_export(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        party_type = request.data.get("party_type", "CUSTOMER")
        party_id = request.data.get("party_id")
        classification = request.data.get("classification", "STANDARD")
        reason = request.data.get("reason", "")

        if not party_id:
            raise ValidationError({"error_code": "ERR_MISSING_PARTY_ID", "detail": "party_id is required."})

        result = PartyDataPrivacyService.export_restricted_dossier(
            actor_user=request.user,
            tenant_id=tenant_id,
            party_type=party_type,
            party_id=party_id,
            classification=classification,
            reason=reason,
        )
        return Response(result, status=status.HTTP_200_OK)
