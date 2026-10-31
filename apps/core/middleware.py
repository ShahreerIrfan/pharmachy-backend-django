"""
apps.core.middleware
Architecture Decision: ADR-0049
Multi-Tenant Context Resolution Middleware:
Resolves tenant and active branch from trusted signed claims/membership; never trusts client-supplied tenant IDs alone.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

from django.http import HttpRequest, HttpResponse, JsonResponse
from rest_framework import status
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import AccessToken

from apps.audit_logging.context import LogContextManager
from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import Branch, Tenant
from apps.identity.models import ActivationStatus, PharmacyUser, UserRole

logger = logging.getLogger("apps.core.middleware")


@dataclass(frozen=True)
class TenantSecurityContext:
    tenant_id: str
    tenant_name: str
    subdomain: str
    plan_tier: str
    branch_id: Optional[str]
    branch_name: Optional[str]
    user_id: Optional[str]
    user_role: Optional[str]
    resolution_source: str
    is_active: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TenantContextMiddleware:
    """
    Middleware that enforces multi-tenant boundary integrity.
    Resolves tenant and branch from trusted signed claims (JWT/Session).
    Never trusts client-supplied tenant IDs alone (ADR-0049).
    """

    PUBLIC_PATH_PREFIXES = (
        "/api/v2/auth/login",
        "/api/v2/auth/password-reset",
        "/api/v2/auth/token/refresh",
        "/api/v2/health",
        "/api/v2/schema",
        "/api/v2/docs",
        "/api/v2/rate-limits/policy",
        "/api/v2/rate-limits/test-",
        "/api/v2/conventions/",
    )

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def _build_error_response(
        self,
        error_code: str,
        http_status: int,
        message_en: str,
        message_bn: str,
        recovery_action: str,
        details: Optional[dict[str, Any]] = None,
        request_id: Optional[str] = None,
    ) -> JsonResponse:
        payload = {
            "success": False,
            "error_code": error_code,
            "error": error_code,
            "http_status": http_status,
            "is_retryable": False,
            "retryable": False,
            "message": message_en,
            "message_en": message_en,
            "message_bn": message_bn,
            "recovery_action": recovery_action,
            "field_errors": {},
            "details": details or {},
            "request_id": request_id or LogContextManager.get_request_id() or "req-unbound",
        }
        res = JsonResponse(payload, status=http_status)
        req_id = request_id or LogContextManager.get_request_id()
        if req_id:
            res["X-Request-ID"] = req_id
        return res

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Default empty context on request object
        request.tenant = None
        request.tenant_id = None
        request.branch = None
        request.branch_id = None
        request.tenant_context = None

        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        token_claims: Optional[dict[str, Any]] = None
        user: Optional[PharmacyUser] = None

        request_id = LogContextManager.get_request_id() or request.META.get("HTTP_X_REQUEST_ID", "req-unbound")

        # 1. Inspect Bearer JWT token if present
        if auth_header.startswith("Bearer "):
            raw_token = auth_header.split(" ", 1)[1].strip()
            try:
                token = AccessToken(raw_token)
                token_claims = token.payload
                user_id = token_claims.get("user_id")
                if user_id:
                    user = PharmacyUser.objects.filter(user_id=user_id, is_archived=False).first()
                    if user and user.activation_status == ActivationStatus.ACTIVE:
                        request.user = user
                    elif user:
                        return self._build_error_response(
                            error_code="ERR_USER_DEACTIVATED",
                            http_status=status.HTTP_403_FORBIDDEN,
                            message_en="Your user account is suspended or deactivated.",
                            message_bn="আপনার ব্যবহারকারী অ্যাকাউন্ট স্থগিত বা নিষ্ক্রিয় করা হয়েছে।",
                            recovery_action="CONTACT_SUPPORT",
                            details={"user_id": str(user_id)},
                            request_id=request_id,
                        )
            except (InvalidToken, TokenError) as exc:
                if not any(request.path.startswith(prefix) for prefix in self.PUBLIC_PATH_PREFIXES):
                    return self._build_error_response(
                        error_code="ERR_UNAUTHORIZED",
                        http_status=status.HTTP_401_UNAUTHORIZED,
                        message_en=f"Authentication token is invalid or expired: {exc}",
                        message_bn="প্রমাণীকরণ টোকেনটি অবৈধ বা মেয়াদোত্তীর্ণ।",
                        recovery_action="REAUTHENTICATE",
                        request_id=request_id,
                    )
            except Exception as exc:
                logger.warning(f"Unexpected token decoding error: {exc}")

        # 2. If session user exists and no JWT user set
        if not user and hasattr(request, "user") and request.user and request.user.is_authenticated:
            user = request.user

        client_tenant_id = request.META.get("HTTP_X_TENANT_ID") or request.GET.get("tenant_id")
        client_branch_id = request.META.get("HTTP_X_BRANCH_ID") or request.GET.get("branch_id")

        # 3. Authenticated Request: Enforce Trusted Signed Claims / Membership
        if user and user.is_authenticated:
            trusted_tenant_id = (token_claims.get("tenant_id") if token_claims else None) or user.tenant_id
            trusted_branch_id = (token_claims.get("branch_id") if token_claims else None) or user.branch_id
            user_role = (token_claims.get("role") if token_claims else None) or user.role
            is_superadmin = (user_role == UserRole.SUPERADMIN) or user.is_superuser

            resolution_source = "JWT_SIGNED_CLAIMS" if token_claims else "SESSION_MEMBERSHIP"
            resolved_tenant_id = trusted_tenant_id

            # Verify client-supplied tenant ID (Never trust client-supplied tenant IDs alone)
            if client_tenant_id and client_tenant_id.strip():
                client_tenant_id = client_tenant_id.strip()
                if client_tenant_id != trusted_tenant_id:
                    if is_superadmin:
                        # Superadmin override allowed, but tenant must exist and be active
                        target_tenant = Tenant.objects.filter(tenant_id=client_tenant_id).first()
                        if not target_tenant:
                            return self._build_error_response(
                                error_code="ERR_TENANT_NOT_FOUND",
                                http_status=status.HTTP_404_NOT_FOUND,
                                message_en=f"Target tenant '{client_tenant_id}' does not exist.",
                                message_bn=f"উদ্দিষ্ট টেন্যান্ট '{client_tenant_id}' খুঁজে পাওয়া যায়নি।",
                                recovery_action="CHECK_TENANT_IDENTIFIER",
                                details={"target_tenant_id": client_tenant_id},
                                request_id=request_id,
                            )
                        resolved_tenant_id = client_tenant_id
                        resolution_source = "SUPERADMIN_OVERRIDE"
                        try:
                            AuditLogEntry.objects.create(
                                tenant_id=client_tenant_id,
                                user_id=str(user.user_id),
                                request_id=request_id,
                                action="SUPERADMIN_TENANT_CONTEXT_SWITCH",
                                resource_type="TENANT",
                                resource_id=client_tenant_id,
                                status=AuditActionStatus.SUCCESS,
                                metadata_json={
                                    "original_tenant": trusted_tenant_id,
                                    "target_tenant": client_tenant_id,
                                    "user_email": user.email,
                                },
                            )
                        except Exception as log_err:
                            logger.error(f"Failed to record superadmin audit switch: {log_err}")
                    else:
                        # Cross-tenant violation: non-superadmin tried to access different tenant
                        try:
                            AuditLogEntry.objects.create(
                                tenant_id=trusted_tenant_id,
                                user_id=str(user.user_id),
                                request_id=request_id,
                                action="CROSS_TENANT_ACCESS_ATTEMPT",
                                resource_type="TENANT",
                                resource_id=client_tenant_id,
                                status=AuditActionStatus.DENIED,
                                metadata_json={
                                    "claimed_tenant": trusted_tenant_id,
                                    "attempted_tenant": client_tenant_id,
                                    "user_email": user.email,
                                },
                            )
                        except Exception as log_err:
                            logger.error(f"Failed to record cross-tenant violation audit: {log_err}")

                        return self._build_error_response(
                            error_code="ERR_CROSS_TENANT_VIOLATION",
                            http_status=status.HTTP_403_FORBIDDEN,
                            message_en=(
                                f"Cross-tenant access violation: Client-supplied tenant ID '{client_tenant_id}' "
                                f"does not match trusted signed claims '{trusted_tenant_id}'."
                            ),
                            message_bn=(
                                f"অন্য ফার্মেসির তথ্যে প্রবেশাধিকার কঠোরভাবে নিষিদ্ধ। "
                                f"ক্লায়েন্ট প্রদত্ত টেন্যান্ট '{client_tenant_id}' অনুমোদিত তথ্যের সাথে মেলেনি।"
                            ),
                            recovery_action="ABORT_SECURITY_INCIDENT",
                            details={
                                "trusted_tenant_id": trusted_tenant_id,
                                "supplied_tenant_id": client_tenant_id,
                            },
                            request_id=request_id,
                        )

            # Load active Tenant record from DB
            tenant = Tenant.objects.filter(tenant_id=resolved_tenant_id).first()
            if not tenant:
                return self._build_error_response(
                    error_code="ERR_TENANT_NOT_FOUND",
                    http_status=status.HTTP_403_FORBIDDEN,
                    message_en=f"Tenant '{resolved_tenant_id}' not found in active records.",
                    message_bn=f"টেন্যান্ট '{resolved_tenant_id}' সিস্টেমে পাওয়া যায়নি।",
                    recovery_action="CONTACT_SUPPORT",
                    details={"tenant_id": resolved_tenant_id},
                    request_id=request_id,
                )

            if not tenant.is_active:
                return self._build_error_response(
                    error_code="ERR_TENANT_INACTIVE",
                    http_status=status.HTTP_403_FORBIDDEN,
                    message_en=f"Tenant account '{tenant.name}' ({tenant.tenant_id}) is inactive or suspended.",
                    message_bn=f"ফার্মেসি টেন্যান্ট অ্যাকাউন্ট '{tenant.name}' নিষ্ক্রিয় বা স্থগিত করা হয়েছে।",
                    recovery_action="CONTACT_SUPPORT",
                    details={"tenant_id": tenant.tenant_id, "is_active": False},
                    request_id=request_id,
                )

            # Resolve Branch
            resolved_branch: Optional[Branch] = None
            target_branch_id = client_branch_id.strip() if client_branch_id else (trusted_branch_id or None)

            if target_branch_id:
                branch = Branch.objects.filter(branch_id=target_branch_id).first()
                if not branch or branch.tenant_id != tenant.tenant_id:
                    if client_branch_id:
                        return self._build_error_response(
                            error_code="ERR_CROSS_TENANT_VIOLATION",
                            http_status=status.HTTP_403_FORBIDDEN,
                            message_en=(
                                f"Cross-branch access violation: Branch '{target_branch_id}' "
                                f"does not belong to active tenant '{tenant.tenant_id}'."
                            ),
                            message_bn=(
                                f"শাখা প্রবেশাধিকার লঙ্ঘন: শাখা '{target_branch_id}' "
                                f"সক্রিয় ফার্মেসি টেন্যান্ট '{tenant.tenant_id}'-এর অন্তর্ভুক্ত নয়।"
                            ),
                            recovery_action="ABORT_SECURITY_INCIDENT",
                            details={"branch_id": target_branch_id, "tenant_id": tenant.tenant_id},
                            request_id=request_id,
                        )
                else:
                    if not branch.is_active:
                        if client_branch_id:
                            return self._build_error_response(
                                error_code="ERR_BRANCH_INACTIVE",
                                http_status=status.HTTP_403_FORBIDDEN,
                                message_en=f"Pharmacy branch '{branch.name}' is inactive.",
                                message_bn=f"ফার্মেসি শাখা '{branch.name}' নিষ্ক্রিয় অবস্থায় রয়েছে।",
                                recovery_action="SELECT_ACTIVE_BRANCH",
                                details={"branch_id": branch.branch_id},
                                request_id=request_id,
                            )
                    else:
                        resolved_branch = branch

            # Construct security context
            security_context = TenantSecurityContext(
                tenant_id=tenant.tenant_id,
                tenant_name=tenant.name,
                subdomain=tenant.subdomain,
                plan_tier=tenant.plan_tier,
                branch_id=resolved_branch.branch_id if resolved_branch else None,
                branch_name=resolved_branch.name if resolved_branch else None,
                user_id=str(user.user_id),
                user_role=user_role,
                resolution_source=resolution_source,
                is_active=tenant.is_active,
            )

            request.tenant = tenant
            request.tenant_id = tenant.tenant_id
            request.branch = resolved_branch
            request.branch_id = resolved_branch.branch_id if resolved_branch else None
            request.tenant_context = security_context

            # Propagate verified tenant context into thread-safe logging
            LogContextManager.set_context(
                request_id=request_id,
                tenant_id=tenant.tenant_id,
                device_id=request.META.get("HTTP_X_DEVICE_ID"),
                operation_id=request.META.get("HTTP_X_OPERATION_ID"),
                user_id=str(user.user_id),
                client_ip=request.META.get("REMOTE_ADDR", "127.0.0.1"),
            )

        # 4. Anonymous / Unauthenticated Request
        else:
            if client_tenant_id and client_tenant_id.strip():
                clean_tid = client_tenant_id.strip()
                public_tenant = Tenant.objects.filter(tenant_id=clean_tid).first()
                if public_tenant:
                    request.tenant = public_tenant
                    request.tenant_id = public_tenant.tenant_id
                    request.tenant_context = TenantSecurityContext(
                        tenant_id=public_tenant.tenant_id,
                        tenant_name=public_tenant.name,
                        subdomain=public_tenant.subdomain,
                        plan_tier=public_tenant.plan_tier,
                        branch_id=None,
                        branch_name=None,
                        user_id=None,
                        user_role=None,
                        resolution_source="ANONYMOUS_HINT",
                        is_active=public_tenant.is_active,
                    )
                else:
                    request.tenant_id = clean_tid

        response = self.get_response(request)
        return response
