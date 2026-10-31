"""
apps.core.responses
Architecture Decision: ADR-0047
Standardized API Response Envelopes, Machine Codes, and ViewSet Mixins.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from apps.audit_logging.context import LogContextManager
from apps.core.errors import RecoveryAction


def get_current_request_id(request: Any = None) -> str:
    """Retrieves current request ID from context or request headers."""
    req_id = LogContextManager.get_request_id()
    if not req_id and request:
        req_id = (
            request.META.get("HTTP_X_REQUEST_ID")
            or request.META.get("HTTP_X_CORRELATION_ID")
            or getattr(request, "request_id", "")
        )
    return req_id or f"req-{uuid.uuid4()}"


def standard_success_response(
    data: Any = None,
    code: str = "OK",
    message_en: str = "Operation completed successfully.",
    message_bn: str = "অপারেশনটি সফলভাবে সম্পন্ন হয়েছে।",
    status_code: int = status.HTTP_200_OK,
    meta: dict[str, Any] | None = None,
    request: Any = None,
) -> Response:
    """Wraps a successful payload into the canonical ADR-0047 success envelope."""
    req_id = get_current_request_id(request)
    payload = {
        "success": True,
        "code": code,
        "request_id": req_id,
        "correlation_id": req_id,
        "timestamp": timezone.now().isoformat(),
        "data": data,
        "meta": meta or {},
        "message_en": message_en,
        "message_bn": message_bn,
    }
    return Response(payload, status=status_code)


def standard_error_response(
    error_code: str,
    message_en: str,
    message_bn: str,
    http_status: int = status.HTTP_400_BAD_REQUEST,
    recovery_action: str = RecoveryAction.RETRY_AFTER_BACKOFF.value,
    field_errors: dict[str, list[str]] | None = None,
    details: dict[str, Any] | None = None,
    is_retryable: bool = False,
    request: Any = None,
) -> Response:
    """Wraps an error into the canonical ADR-0014 / ADR-0047 error envelope."""
    req_id = get_current_request_id(request)
    payload = {
        "success": False,
        "error_code": error_code,
        "code": error_code,
        "request_id": req_id,
        "correlation_id": req_id,
        "http_status": http_status,
        "is_retryable": is_retryable,
        "retryable": is_retryable,
        "message": message_en,
        "message_en": message_en,
        "message_bn": message_bn,
        "recovery_action": recovery_action,
        "field_errors": field_errors or {},
        "details": details or field_errors or {},
        "timestamp": timezone.now().isoformat(),
    }
    return Response(payload, status=http_status)


class StandardResponseMixin:
    """
    ViewSet mixin providing standardized response wrapping helpers.
    """

    def success_response(
        self,
        data: Any = None,
        code: str = "OK",
        message_en: str = "Operation completed successfully.",
        message_bn: str = "অপারেশনটি সফলভাবে সম্পন্ন হয়েছে।",
        status_code: int = status.HTTP_200_OK,
        meta: dict[str, Any] | None = None,
    ) -> Response:
        return standard_success_response(
            data=data,
            code=code,
            message_en=message_en,
            message_bn=message_bn,
            status_code=status_code,
            meta=meta,
            request=getattr(self, "request", None),
        )

    def error_response(
        self,
        error_code: str,
        message_en: str,
        message_bn: str,
        http_status: int = status.HTTP_400_BAD_REQUEST,
        recovery_action: str = RecoveryAction.RETRY_AFTER_BACKOFF.value,
        field_errors: dict[str, list[str]] | None = None,
        details: dict[str, Any] | None = None,
        is_retryable: bool = False,
    ) -> Response:
        return standard_error_response(
            error_code=error_code,
            message_en=message_en,
            message_bn=message_bn,
            http_status=http_status,
            recovery_action=recovery_action,
            field_errors=field_errors,
            details=details,
            is_retryable=is_retryable,
            request=getattr(self, "request", None),
        )
