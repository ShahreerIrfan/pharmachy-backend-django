"""
apps.core.throttling
Architecture Decision: ADR-0048
Multi-Tiered Endpoint and Identity-Based Rate Limiting Throttles.
"""

from __future__ import annotations

import hashlib
from typing import Any

from rest_framework.throttling import SimpleRateThrottle


class BaseIdentityRateThrottle(SimpleRateThrottle):
    """Base throttle providing safe client IP and identity resolution."""

    def get_client_ip(self, request: Any) -> str:
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            return x_forwarded_for.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "127.0.0.1")


class AuthLoginRateThrottle(BaseIdentityRateThrottle):
    """
    Throttles authentication attempts combining client IP with normalized credential.
    Protects against distributed credential stuffing and password spraying (ADR-0048).
    """

    scope = "auth_login"

    def get_cache_key(self, request: Any, view: Any) -> str:
        ip = self.get_client_ip(request)
        identifier = ""
        if hasattr(request, "data") and isinstance(request.data, dict):
            identifier = (
                str(
                    request.data.get("email")
                    or request.data.get("phone")
                    or request.data.get("phone_number")
                    or request.data.get("username", "")
                )
                .strip()
                .lower()
            )

        # Mask/hash identity to protect privacy in cache keys
        ident_hash = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:16] if identifier else "anon"
        return f"throttle_{self.scope}_{ip}_{ident_hash}"


class PasswordResetRateThrottle(BaseIdentityRateThrottle):
    """
    Throttles password-reset requests per target email address and client IP.
    Prevents email flooding and user enumeration (ADR-0048).
    """

    scope = "password_reset"

    def get_cache_key(self, request: Any, view: Any) -> str:
        ip = self.get_client_ip(request)
        email = ""
        if hasattr(request, "data") and isinstance(request.data, dict):
            email = str(request.data.get("email", "")).strip().lower()

        email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()[:16] if email else "none"
        return f"throttle_{self.scope}_{ip}_{email_hash}"


class SensitiveMutationRateThrottle(BaseIdentityRateThrottle):
    """
    Throttles high-value state mutations (e.g. POS checkouts, inventory stock reconciliations).
    Combines tenant identity with user ID or hardware device ID (ADR-0048).
    """

    scope = "sensitive_mutation"

    def get_cache_key(self, request: Any, view: Any) -> str:
        tenant_id = (
            request.META.get("HTTP_X_TENANT_ID")
            or getattr(request, "tenant_id", None)
            or (getattr(request.user, "tenant_id", None) if getattr(request, "user", None) else None)
            or (request.data.get("tenant_id") if hasattr(request, "data") and isinstance(request.data, dict) else None)
            or "public-tenant"
        )
        user_id = (
            str(getattr(request.user, "user_id", getattr(request.user, "id", "anon")))
            if getattr(request, "user", None) and request.user.is_authenticated
            else "anon"
        )
        device_id = request.META.get("HTTP_X_DEVICE_ID", "")

        return f"throttle_{self.scope}_{tenant_id}_{user_id}_{device_id}"


class DataExportRateThrottle(BaseIdentityRateThrottle):
    """
    Throttles resource-intensive bulk data exports (CSV, PDF, Audit Logs).
    Protects database connection pools and worker memory (ADR-0048).
    """

    scope = "data_export"

    def get_cache_key(self, request: Any, view: Any) -> str:
        tenant_id = (
            request.META.get("HTTP_X_TENANT_ID")
            or (getattr(request.user, "tenant_id", None) if getattr(request, "user", None) else None)
            or "public-tenant"
        )
        user_id = (
            str(getattr(request.user, "user_id", getattr(request.user, "id", "anon")))
            if getattr(request, "user", None) and request.user.is_authenticated
            else "anon"
        )
        return f"throttle_{self.scope}_{tenant_id}_{user_id}"


class PublicInvitationRateThrottle(BaseIdentityRateThrottle):
    """
    Throttles public staff invitation dispatch and acceptance routes.
    Protects against automated spam and dummy account generation (ADR-0048).
    """

    scope = "public_invitation"

    def get_cache_key(self, request: Any, view: Any) -> str:
        ip = self.get_client_ip(request)
        return f"throttle_{self.scope}_{ip}"


class TestStrictRateThrottle(BaseIdentityRateThrottle):
    """
    Dedicated strict throttle (3 requests / minute) for deterministic automated verification.
    Ensures rapid, predictable trigger of 429 response envelopes in test environments (ADR-0048).
    """

    scope = "test_throttle"

    def get_cache_key(self, request: Any, view: Any) -> str:
        ip = self.get_client_ip(request)
        action_name = getattr(view, "action", "test_action")
        user_id = (
            str(getattr(request.user, "user_id", getattr(request.user, "id", "anon")))
            if getattr(request, "user", None) and request.user.is_authenticated
            else "anon"
        )
        device_id = request.META.get("HTTP_X_DEVICE_ID", "default-dev")
        return f"throttle_{self.scope}_{action_name}_{ip}_{user_id}_{device_id}"


def get_active_rate_limit_policies() -> dict[str, Any]:
    """Returns metadata matrix describing all configured rate limit policies conforming to schema."""
    return {
        "standard_error_code": "ERR_RATE_LIMIT_EXCEEDED",
        "default_recovery_action": "RETRY_AFTER_BACKOFF",
        "policies": [
            {
                "scope": "anon",
                "rate": "100/min",
                "identity_type": "IP_ONLY",
                "description": "Baseline unauthenticated endpoint scraper defense.",
            },
            {
                "scope": "user",
                "rate": "1000/min",
                "identity_type": "TENANT_AND_USER",
                "description": "Standard authenticated API consumption quota.",
            },
            {
                "scope": "auth_login",
                "rate": "10/min",
                "identity_type": "IP_AND_CREDENTIAL",
                "description": "Anti-credential stuffing protection for password and phone login.",
            },
            {
                "scope": "password_reset",
                "rate": "5/min",
                "identity_type": "IP_AND_CREDENTIAL",
                "description": "Anti-spam defense for password reset token requests.",
            },
            {
                "scope": "sensitive_mutation",
                "rate": "30/min",
                "identity_type": "TENANT_AND_DEVICE",
                "description": "Guard against rapid checkout bursts, race conditions, and duplicate billing.",
            },
            {
                "scope": "data_export",
                "rate": "10/min",
                "identity_type": "TENANT_AND_USER",
                "description": "Protects worker resources during heavy database ledger CSV/PDF exports.",
            },
            {
                "scope": "public_invitation",
                "rate": "5/min",
                "identity_type": "IP_ONLY",
                "description": "Protects staff onboarding and invitation links from bot abuse.",
            },
            {
                "scope": "test_throttle",
                "rate": "3/min",
                "identity_type": "IP_AND_CREDENTIAL",
                "description": "Deterministic test throttle for verifying 429 response envelopes and backoff timers.",
            },
        ],
    }
