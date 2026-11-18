"""
backend/apps/core/offline_prescription_policy_views.py
Architecture Decision: ADR-0311 (P311).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

REST API ViewSet governing Offline Prescription Policies & Minimal-PHI Auth Tokens:
- GET    /api/v2/offline-sync/prescription-policies/                 : List branch Rx policies
- POST   /api/v2/offline-sync/prescription-policies/                 : Create/update branch Rx policy
- PATCH  /api/v2/offline-sync/prescription-policies/<id>/            : Update branch Rx policy
- DELETE /api/v2/offline-sync/prescription-policies/<id>/            : Archive branch Rx policy
- POST   /api/v2/offline-sync/prescription-policies/issue-token/     : Issue minimal-PHI signed offline Rx token
- GET    /api/v2/offline-sync/prescription-policies/tokens/          : List offline Rx authorization tokens
- POST   /api/v2/offline-sync/prescription-policies/verify-dispense/ : Fail-closed offline Rx dispense verification
- POST   /api/v2/offline-sync/prescription-policies/revoke-token/    : Audited revocation + P307 tombstone emission
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.models import OfflinePrescriptionPolicy, TenantLifecycleStatus
from apps.core.offline_prescription_policy_service import OfflinePrescriptionPolicyService


def _get_request_tenant_id(request) -> str:
    qp_tenant = (
        getattr(request, "query_params", {}).get("tenant_id")
        if hasattr(request, "query_params")
        else request.GET.get("tenant_id")
    )
    if qp_tenant:
        return str(qp_tenant)
    user = getattr(request, "user", None)
    if user and getattr(user, "tenant_id", None):
        return str(user.tenant_id)
    tenant = getattr(request, "tenant", None)
    if tenant and getattr(tenant, "tenant_id", None):
        return str(tenant.tenant_id)
    if hasattr(request, "data") and isinstance(request.data, dict) and request.data.get("tenant_id"):
        return str(request.data.get("tenant_id"))
    return ""


def _check_tenant_suspended(request):
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {"error_code": "ERR_TENANT_SUSPENDED", "detail": "Tenant subscription is suspended."}
        )


class OfflinePrescriptionPolicyViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = OfflinePrescriptionPolicyService.list_policies(tenant_id=tenant_id, actor_user=request.user)
        return Response(res, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = OfflinePrescriptionPolicyService.upsert_policy(
            tenant_id=tenant_id,
            branch_id=payload.get("branch_id", ""),
            allow_offline_otc=bool(payload.get("allow_offline_otc", True)),
            allow_offline_standard_rx=bool(payload.get("allow_offline_standard_rx", True)),
            allow_offline_antimicrobial_rx=bool(payload.get("allow_offline_antimicrobial_rx", False)),
            allow_offline_controlled_narcotic=bool(payload.get("allow_offline_controlled_narcotic", False)),
            fail_closed_when_unverified=bool(payload.get("fail_closed_when_unverified", True)),
            max_token_ttl_hours=payload.get("max_token_ttl_hours", 24),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_201_CREATED if res["created"] else status.HTTP_200_OK)

    def partial_update(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        existing = OfflinePrescriptionPolicy.objects.filter(tenant_id=tenant_id, policy_id=str(pk)).first()
        payload = request.data or {}
        branch_id = payload.get("branch_id") or (str(existing.branch_id) if existing else "")
        res = OfflinePrescriptionPolicyService.upsert_policy(
            tenant_id=tenant_id,
            branch_id=branch_id,
            allow_offline_otc=bool(payload.get("allow_offline_otc", existing.allow_offline_otc if existing else True)),
            allow_offline_standard_rx=bool(
                payload.get("allow_offline_standard_rx", existing.allow_offline_standard_rx if existing else True)
            ),
            allow_offline_antimicrobial_rx=bool(
                payload.get(
                    "allow_offline_antimicrobial_rx",
                    existing.allow_offline_antimicrobial_rx if existing else False,
                )
            ),
            allow_offline_controlled_narcotic=bool(payload.get("allow_offline_controlled_narcotic", False)),
            fail_closed_when_unverified=bool(payload.get("fail_closed_when_unverified", True)),
            max_token_ttl_hours=payload.get("max_token_ttl_hours", existing.max_token_ttl_hours if existing else 24),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    def destroy(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = OfflinePrescriptionPolicyService.archive_policy(
            tenant_id=tenant_id,
            policy_id=str(pk),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="issue-token")
    def issue_token(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = OfflinePrescriptionPolicyService.issue_offline_auth_token(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            product_id=payload.get("product_id", ""),
            prescription_reference_id=payload.get("prescription_reference_id", ""),
            approved_max_units=payload.get("approved_max_units"),
            ttl_hours=payload.get("ttl_hours", 12),
            idempotency_key=payload.get("idempotency_key", ""),
            actor_user=request.user,
        )
        code = status.HTTP_200_OK if res.get("idempotent_replay") else status.HTTP_201_CREATED
        return Response(res, status=code)

    @action(detail=False, methods=["get"], url_path="tokens")
    def tokens(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = OfflinePrescriptionPolicyService.list_tokens(
            tenant_id=tenant_id,
            status_filter=request.query_params.get("status"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="verify-dispense")
    def verify_dispense(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = OfflinePrescriptionPolicyService.verify_and_record_dispense(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            device_fingerprint_sha256=payload.get("device_fingerprint_sha256", ""),
            token_id=payload.get("token_id", ""),
            token_signature_sha256=payload.get("token_signature_sha256", ""),
            dispensed_units=payload.get("dispensed_units"),
            local_invoice_ref=payload.get("local_invoice_ref", ""),
            idempotency_key=payload.get("idempotency_key", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="revoke-token")
    def revoke_token(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = OfflinePrescriptionPolicyService.revoke_token(
            tenant_id=tenant_id,
            token_id=payload.get("token_id", ""),
            reason=payload.get("reason", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)
