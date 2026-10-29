"""
apps.audit_logging.context
Architecture Decision: ADR-0035
Thread-safe, async-compatible request context holder using ContextVar.
"""

from __future__ import annotations

import contextvars
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class RequestCorrelationContext:
    request_id: str
    tenant_id: Optional[str] = None
    device_id: Optional[str] = None
    operation_id: Optional[str] = None
    user_id: Optional[str] = None
    client_ip: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_CURRENT_CONTEXT: contextvars.ContextVar[Optional[RequestCorrelationContext]] = contextvars.ContextVar(
    "current_request_context", default=None
)


class LogContextManager:
    """Manages thread-safe request context for structured logging and distributed tracing."""

    @classmethod
    def set_context(
        cls,
        request_id: str,
        tenant_id: Optional[str] = None,
        device_id: Optional[str] = None,
        operation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> RequestCorrelationContext:
        ctx = RequestCorrelationContext(
            request_id=request_id,
            tenant_id=tenant_id,
            device_id=device_id,
            operation_id=operation_id,
            user_id=user_id,
            client_ip=client_ip,
        )
        _CURRENT_CONTEXT.set(ctx)
        return ctx

    @classmethod
    def get_context(cls) -> Optional[RequestCorrelationContext]:
        return _CURRENT_CONTEXT.get()

    @classmethod
    def get_context_dict(cls) -> Dict[str, Any]:
        ctx = cls.get_context()
        if ctx:
            return ctx.to_dict()
        return {
            "request_id": "req-system-unbound",
            "tenant_id": None,
            "device_id": None,
            "operation_id": None,
            "user_id": None,
            "client_ip": None,
        }

    @classmethod
    def clear(cls) -> None:
        _CURRENT_CONTEXT.set(None)
