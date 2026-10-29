"""
apps.audit_logging.formatters
Architecture Decision: ADR-0035
Standardized Structured JSON Logging Formatter with Zero-PHI Redaction.
"""

from __future__ import annotations

import datetime
import json
import logging
import traceback
from typing import Any, Dict

from apps.audit_logging.context import LogContextManager
from apps.audit_logging.redaction import SensitiveDataRedactor


class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as structured, machine-parsable JSON lines with redacted payloads."""

    def __init__(
        self,
        service_name: str = "pharmacy-saas-cloud-api",
        environment: str = "development",
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.service_name = service_name
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        # 1. UTC Timestamp in ISO 8601 format
        log_time = datetime.datetime.fromtimestamp(record.created, tz=datetime.timezone.utc).isoformat()

        # 2. Extract correlation context from ContextVar or record attributes
        context_data = LogContextManager.get_context_dict()
        if hasattr(record, "request_id") and getattr(record, "request_id"):
            context_data["request_id"] = getattr(record, "request_id")
        if hasattr(record, "tenant_id") and getattr(record, "tenant_id"):
            context_data["tenant_id"] = getattr(record, "tenant_id")
        if hasattr(record, "device_id") and getattr(record, "device_id"):
            context_data["device_id"] = getattr(record, "device_id")
        if hasattr(record, "operation_id") and getattr(record, "operation_id"):
            context_data["operation_id"] = getattr(record, "operation_id")
        if hasattr(record, "user_id") and getattr(record, "user_id"):
            context_data["user_id"] = getattr(record, "user_id")

        # 3. Format message and redact
        raw_message = record.getMessage()
        redacted_message = SensitiveDataRedactor.redact_string(raw_message)

        # 4. Assemble root log payload
        payload: Dict[str, Any] = {
            "timestamp": log_time,
            "level": record.levelname,
            "logger": record.name,
            "message": redacted_message,
            "service": self.service_name,
            "environment": self.environment,
            "context": context_data,
        }

        # 5. Attach HTTP metadata if present
        if hasattr(record, "http_metadata"):
            http_meta = getattr(record, "http_metadata")
            payload["http"] = SensitiveDataRedactor.redact(http_meta)

        # 6. Attach extra metadata if present
        if hasattr(record, "extra_data"):
            extra = getattr(record, "extra_data")
            payload["extra"] = SensitiveDataRedactor.redact(extra)

        # 7. Attach exception traceback if present
        if record.exc_info:
            payload["error"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Exception",
                "traceback": SensitiveDataRedactor.redact_string(traceback.format_exc()),
            }

        return json.dumps(payload, ensure_ascii=False)
