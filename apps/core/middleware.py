"""
apps.core.middleware
Architecture Decision: ADR-0049
Multi-Tenant Context Resolution Middleware:
Resolves tenant and active branch from trusted signed claims/membership; never trusts client-supplied tenant IDs alone.
"""

from __future__ import annotations

import logging
import uuid
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
        elif not user and hasattr(request, "_force_auth_user") and request._force_auth_user:
            user = request._force_auth_user
            request.user = user

        client_tenant_id = request.META.get("HTTP_X_TENANT_ID") or request.GET.get("tenant_id")
        client_branch_id = request.META.get("HTTP_X_BRANCH_ID") or request.GET.get("branch_id")

        # 3. Authenticated Request: Enforce Trusted Signed Claims / Membership
        if user and user.is_authenticated:
            user_role = (token_claims.get("role") if token_claims else None) or getattr(user, "role", None)
            is_superadmin = (
                (user_role == UserRole.SUPERADMIN)
                or getattr(user, "is_superuser", False)
                or str(user_role).upper() in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_SUPER_ADMIN", "PLATFORM_OPERATOR"]
            )

            if is_superadmin:
                trusted_tenant_id = None
                trusted_branch_id = None
                resolved_tenant_id = client_tenant_id.strip() if client_tenant_id else None
                resolution_source = "SUPERADMIN_OVERRIDE" if resolved_tenant_id else "PLATFORM_GLOBAL"
            else:
                trusted_tenant_id = (token_claims.get("tenant_id") if token_claims else None) or getattr(user, "tenant_id", None)
                trusted_branch_id = (token_claims.get("branch_id") if token_claims else None) or getattr(user, "branch_id", None)
                resolution_source = "JWT_SIGNED_CLAIMS" if token_claims else "SESSION_MEMBERSHIP"
                resolved_tenant_id = trusted_tenant_id

            # Verify client-supplied tenant ID (Never trust client-supplied tenant IDs alone)
            if client_tenant_id and client_tenant_id.strip():
                client_tenant_id = client_tenant_id.strip()
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
                                "original_tenant": "PLATFORM_GLOBAL",
                                "target_tenant": client_tenant_id,
                                "user_email": user.email,
                            },
                        )
                    except Exception as log_err:
                        logger.error(f"Failed to record superadmin audit switch: {log_err}")
                elif client_tenant_id != trusted_tenant_id:
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
            tenant = Tenant.objects.filter(tenant_id=resolved_tenant_id).first() if resolved_tenant_id else None
            if not tenant:
                if is_superadmin and not resolved_tenant_id:
                    tenant = None
                else:
                    return self._build_error_response(
                        error_code="ERR_TENANT_NOT_FOUND",
                        http_status=status.HTTP_403_FORBIDDEN,
                        message_en=f"Tenant '{resolved_tenant_id}' not found in active records.",
                        message_bn=f"টেন্যান্ট '{resolved_tenant_id}' সিস্টেমে পাওয়া যায়নি।",
                        recovery_action="CONTACT_SUPPORT",
                        details={"tenant_id": resolved_tenant_id},
                        request_id=request_id,
                    )

            if tenant and not tenant.is_active:
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

            if tenant and target_branch_id:
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
            if tenant:
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
            else:
                request.tenant = None
                request.tenant_id = None
                request.branch = None
                request.branch_id = None
                request.tenant_context = None

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


class GlobalExceptionMiddleware:
    """
    Tier-2 Global Exception Middleware (ADR-0054).
    Intercepts any unhandled exception in the Django middleware pipeline or non-DRF views,
    guaranteeing that API endpoints always receive a standardized, localized, safe error envelope.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        try:
            return self.get_response(request)
        except Exception as exc:
            handled = self.process_exception(request, exc)
            if handled is not None:
                return handled
            raise

    def process_exception(self, request: HttpRequest, exc: Exception) -> Optional[HttpResponse]:
        # Handle only if request is an API request or expects JSON
        is_api = (
            request.path.startswith("/api/")
            or "application/json" in request.META.get("HTTP_ACCEPT", "")
        )
        if not is_api:
            # Let standard Django exception processing handle web / admin views
            return None

        # Build correlation ID
        ctx = LogContextManager.get_context()
        correlation_id = (
            getattr(request, "request_id", None)
            or request.META.get("HTTP_X_REQUEST_ID")
            or request.META.get("HTTP_X_CORRELATION_ID")
            or (ctx.request_id if ctx else None)
        )
        if not correlation_id:
            correlation_id = f"req-{uuid.uuid4()}"

        logger.error(
            f"GlobalExceptionMiddleware caught exception on {request.method} {request.path}: {exc}",
            exc_info=True,
            extra={
                "correlation_id": correlation_id,
                "path": request.path,
                "method": request.method,
            },
        )

        from apps.core.errors import format_error_response

        payload = format_error_response(exc, correlation_id=correlation_id)
        http_status = payload.get("http_status", 500)

        response = JsonResponse(payload, status=http_status)
        response["X-Request-ID"] = correlation_id
        response["X-Correlation-ID"] = correlation_id
        return response


class SubscriptionActivationGateMiddleware:
    """
    Middleware that rejects unentitled operational ERP writes on the server (ADR-0085).
    Allows authorized billing, read-only, and policy-approved reconciliation endpoints.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        from apps.core.subscription_activation_gate_service import SubscriptionActivationGateService

        user = getattr(request, "user", None)
        if (not user or not getattr(user, "is_authenticated", False)) and hasattr(request, "_force_auth_user") and request._force_auth_user:
            user = request._force_auth_user

        tenant_id = getattr(request, "tenant_id", None)
        if not tenant_id and hasattr(request, "tenant") and request.tenant:
            tenant_id = getattr(request.tenant, "tenant_id", None)
        if not tenant_id and user and hasattr(user, "tenant_id") and user.tenant_id:
            tenant_id = user.tenant_id

        is_permitted, code, reason, details = SubscriptionActivationGateService.is_operational_write_permitted(
            tenant_id=tenant_id,
            path=request.path,
            method=request.method,
            user=user,
        )

        if not is_permitted:
            request_id = LogContextManager.get_request_id() or request.META.get("HTTP_X_REQUEST_ID", "req-unbound")
            payload = {
                "success": False,
                "error_code": code,
                "error": code,
                "code": code,
                "http_status": status.HTTP_403_FORBIDDEN,
                "is_retryable": False,
                "retryable": False,
                "message": reason,
                "message_en": reason,
                "message_bn": "অপারেশনাল ডেটা পরিবর্তন প্রত্যাখ্যাত: ফার্মেসির বর্তমান সাবস্ক্রিপশন নতুন তথ্য যুক্ত বা পরিবর্তন করার অনুমতি দেয় না। অনুগ্রহ করে বিল পরিশোধ বা সাবস্ক্রিপশন সক্রিয় করুন।",
                "recovery_action": "RENEW_SUBSCRIPTION",
                "field_errors": {},
                "details": details,
                "request_id": request_id,
            }
            res = JsonResponse(payload, status=status.HTTP_403_FORBIDDEN)
            res["X-Request-ID"] = request_id
            return res

        return self.get_response(request)


