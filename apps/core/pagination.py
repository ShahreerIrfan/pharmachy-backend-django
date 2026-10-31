"""
apps.core.pagination
Architecture Decision: ADR-0047
Standardized DRF Pagination with Envelope Formatting and Query Filters Metadata.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.utils import timezone
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.audit_logging.context import LogContextManager


class StandardPagination(PageNumberPagination):
    """
    Standard pagination class complying with ADR-0047 conventions.
    Wraps paginated collections in the standard response envelope with metadata,
    page counts, request tracing, and bilingual localized descriptions.
    """

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100
    page_query_param = "page"

    def paginate_queryset(self, queryset: Any, request: Any, view: Any = None) -> list[Any] | None:
        """
        Determines whether to paginate the queryset.
        Maintains backward compatibility by allowing unpaginated responses
        unless pagination params (page, page_size, paginate=true) or view enforcement are present.
        """
        self.request = request
        should_paginate = (
            self.page_query_param in request.query_params
            or self.page_size_query_param in request.query_params
            or request.query_params.get("paginate", "").strip().lower() in ("true", "1")
            or getattr(view, "enforce_pagination", False)
        )

        if should_paginate:
            return super().paginate_queryset(queryset, request, view)
        return None

    def get_filter_meta(self) -> dict[str, Any]:
        """Extracts active query filters excluding pagination controls."""
        if not hasattr(self, "request") or not self.request:
            return {}
        ignored_keys = {self.page_query_param, self.page_size_query_param, "paginate", "format"}
        filters: dict[str, Any] = {}
        for key, val in self.request.query_params.items():
            if key not in ignored_keys and val:
                filters[key] = val
        return filters

    def get_paginated_response(self, data: Any) -> Response:
        """Constructs canonical ADR-0047 paginated response envelope."""
        request_id = LogContextManager.get_request_id()
        if not request_id and hasattr(self, "request") and self.request:
            request_id = (
                self.request.META.get("HTTP_X_REQUEST_ID")
                or self.request.META.get("HTTP_X_CORRELATION_ID")
                or getattr(self.request, "request_id", "")
            )
        if not request_id:
            request_id = f"req-{uuid.uuid4()}"

        page_number = self.page.number
        total_pages = self.page.paginator.num_pages
        total_count = self.page.paginator.count
        current_page_size = self.get_page_size(self.request) or len(data)

        payload = {
            "success": True,
            "code": "PAGINATED_RESULTS",
            "request_id": request_id,
            "correlation_id": request_id,
            "timestamp": timezone.now().isoformat(),
            "data": data,
            "meta": {
                "pagination": {
                    "page": page_number,
                    "page_size": current_page_size,
                    "total_count": total_count,
                    "total_pages": total_pages,
                    "has_next": self.page.has_next(),
                    "has_previous": self.page.has_previous(),
                    "next_page": self.page.next_page_number() if self.page.has_next() else None,
                    "previous_page": self.page.previous_page_number() if self.page.has_previous() else None,
                },
                "filters": self.get_filter_meta(),
            },
            "message_en": f"Retrieved page {page_number} of {total_pages} ({total_count} total records).",
            "message_bn": f"পৃষ্ঠা {page_number}/{total_pages} এর তথ্য সফলভাবে লোড হয়েছে (মোট {total_count} টি রেকর্ড)।",
        }
        return Response(payload)
