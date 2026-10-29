"""
apps.audit_logging.middleware
Architecture Decision: ADR-0035
Django Middleware for Distributed Tracing, Correlation Headers, and Structured Request Logging.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable

from django.http import HttpRequest, HttpResponse

from apps.audit_logging.context import LogContextManager

logger = logging.getLogger("apps.audit_logging.middleware")


class StructuredLoggingMiddleware:
    """Extracts distributed correlation headers, sets contextvars, measures latency, and logs structured JSON."""

    HEADER_REQUEST_ID = "HTTP_X_REQUEST_ID"
    HEADER_TENANT_ID = "HTTP_X_TENANT_ID"
    HEADER_DEVICE_ID = "HTTP_X_DEVICE_ID"
    HEADER_OPERATION_ID = "HTTP_X_OPERATION_ID"

    RESPONSE_HEADER_REQUEST_ID = "X-Request-ID"

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def _get_client_ip(self, request: HttpRequest) -> str:
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            return x_forwarded_for.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "127.0.0.1")

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # 1. Extract or generate request ID
        request_id = request.META.get(self.HEADER_REQUEST_ID)
        if not request_id:
            request_id = f"req-{uuid.uuid4()}"

        # 2. Extract correlation headers
        tenant_id = request.META.get(self.HEADER_TENANT_ID)
        device_id = request.META.get(self.HEADER_DEVICE_ID)
        operation_id = request.META.get(self.HEADER_OPERATION_ID)
        client_ip = self._get_client_ip(request)

        # 3. User identification if already authenticated
        user_id = None
        if hasattr(request, "user") and request.user and request.user.is_authenticated:
            user_id = str(request.user.id)
            if not tenant_id and hasattr(request.user, "tenant_id"):
                tenant_id = getattr(request.user, "tenant_id")

        # 4. Set thread-safe request context
        LogContextManager.set_context(
            request_id=request_id,
            tenant_id=tenant_id,
            device_id=device_id,
            operation_id=operation_id,
            user_id=user_id,
            client_ip=client_ip,
        )

        start_time = time.perf_counter()

        try:
            response = self.get_response(request)
        except Exception as exc:
            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.error(
                f"HTTP {request.method} {request.path} failed with unhandled exception: {exc}",
                exc_info=True,
                extra={
                    "http_metadata": {
                        "method": request.method,
                        "path": request.path,
                        "status_code": 500,
                        "duration_ms": duration_ms,
                    }
                },
            )
            LogContextManager.clear()
            raise

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        # 5. Inject X-Request-ID into response header for client correlation
        response[self.RESPONSE_HEADER_REQUEST_ID] = request_id

        # 6. Log structured request outcome
        status_code = response.status_code
        log_level = logging.INFO
        if 400 <= status_code < 500:
            log_level = logging.WARNING
        elif status_code >= 500:
            log_level = logging.ERROR

        record = logger.makeRecord(
            name=logger.name,
            level=log_level,
            fn="",
            lno=0,
            msg=f"HTTP {request.method} {request.path} {status_code} ({duration_ms}ms)",
            args=(),
            exc_info=None,
        )
        record.http_metadata = {  # type: ignore[attr-defined]
            "method": request.method,
            "path": request.path,
            "status_code": status_code,
            "duration_ms": duration_ms,
        }
        logger.handle(record)

        # 7. Clean up context on request completion
        LogContextManager.clear()

        return response
