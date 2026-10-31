"""
apps.core.pagination
Architecture Decision: ADR-0047, ADR-0053
Deterministic Cursor Pagination, Bounded Page Sizes, Validated Filters, and Bilingual Search Envelopes.
"""

from __future__ import annotations

import urllib.parse
import uuid
from typing import Any

from django.db.models import Q
from django.utils import timezone
from rest_framework import filters
from rest_framework.exceptions import NotFound
from rest_framework.pagination import BasePagination, CursorPagination, PageNumberPagination
from rest_framework.response import Response

from apps.audit_logging.context import LogContextManager
from apps.core.errors import FieldValidationError, InvalidCursorError
from apps.core.localization import normalize_search_query


class StandardPagination(PageNumberPagination):
    """
    Standard page-number pagination class complying with ADR-0047 conventions.
    Wraps paginated collections in the standard response envelope with metadata,
    page counts, bounded page size guardrails, request tracing, and bilingual descriptions.
    """

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100
    page_query_param = "page"

    def get_page_size(self, request: Any) -> int:
        """
        Extracts and strictly bounds the requested page size (1..100).
        Rejects non-integer or negative/zero page sizes with FieldValidationError (422).
        Clamps oversized values (>100) to max_page_size.
        """
        if self.page_size_query_param in request.query_params:
            raw_val = request.query_params[self.page_size_query_param]
            try:
                val = int(raw_val)
            except (ValueError, TypeError):
                raise FieldValidationError(
                    field_errors={"page_size": ["Page size must be a strictly positive integer between 1 and 100."]},
                    message_en="Page size must be a strictly positive integer between 1 and 100.",
                    message_bn="পৃষ্ঠার আকার অবশ্যই ১ থেকে ১০০ এর মধ্যে একটি ধনাত্মক পূর্ণসংখ্যা হতে হবে।",
                )
            if val <= 0:
                raise FieldValidationError(
                    field_errors={"page_size": ["Page size must be a strictly positive integer between 1 and 100."]},
                    message_en="Page size must be a strictly positive integer between 1 and 100.",
                    message_bn="পৃষ্ঠার আকার অবশ্যই ১ থেকে ১০০ এর মধ্যে একটি ধনাত্মক পূর্ণসংখ্যা হতে হবে।",
                )
            return min(val, self.max_page_size)
        return self.page_size

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
        filters_map: dict[str, Any] = {}
        for key, val in self.request.query_params.items():
            if key not in ignored_keys and val:
                filters_map[key] = val
        return filters_map

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
                    "type": "offset",
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


class SafeCursorPagination(CursorPagination):
    """
    Deterministic Cursor Pagination complying with ADR-0053.
    Guarantees stable sequential browsing across high-concurrency tables without offset drift.
    Enforces bounded page sizes (1..100) and transforms malformed cursor tokens into
    canonical InvalidCursorError (422) responses.
    """

    cursor_query_param = "cursor"
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100
    ordering = "-created_at"
    invalid_cursor_message = "The provided pagination cursor is invalid, expired, or malformed."

    def get_page_size(self, request: Any) -> int:
        """
        Validates that requested page size is a strictly positive integer between 1 and 100.
        Clamps values > 100 to max_page_size.
        """
        if self.page_size_query_param in request.query_params:
            raw_val = request.query_params[self.page_size_query_param]
            try:
                val = int(raw_val)
            except (ValueError, TypeError):
                raise FieldValidationError(
                    field_errors={"page_size": ["Page size must be a strictly positive integer between 1 and 100."]},
                    message_en="Page size must be a strictly positive integer between 1 and 100.",
                    message_bn="পৃষ্ঠার আকার অবশ্যই ১ থেকে ১০০ এর মধ্যে একটি ধনাত্মক পূর্ণসংখ্যা হতে হবে।",
                )
            if val <= 0:
                raise FieldValidationError(
                    field_errors={"page_size": ["Page size must be a strictly positive integer between 1 and 100."]},
                    message_en="Page size must be a strictly positive integer between 1 and 100.",
                    message_bn="পৃষ্ঠার আকার অবশ্যই ১ থেকে ১০০ এর মধ্যে একটি ধনাত্মক পূর্ণসংখ্যা হতে হবে।",
                )
            return min(val, self.max_page_size)
        return self.page_size

    def decode_cursor(self, request: Any):
        """
        Safely decodes base64 opaque cursor token.
        Raises InvalidCursorError (422) if token is corrupted or invalid.
        """
        try:
            return super().decode_cursor(request)
        except (NotFound, TypeError, ValueError, Exception) as exc:
            raise InvalidCursorError(
                message_en=f"The provided pagination cursor is invalid or corrupted: {exc}",
                message_bn="প্রদত্ত পেজিনেশন কার্সারটি অবৈধ বা বিকৃত।",
            )

    def get_filter_meta(self) -> dict[str, Any]:
        """Extracts active query filters excluding cursor controls."""
        if not hasattr(self, "request") or not self.request:
            return {}
        ignored_keys = {self.cursor_query_param, self.page_size_query_param, "pagination_type", "format"}
        filters_map: dict[str, Any] = {}
        for key, val in self.request.query_params.items():
            if key not in ignored_keys and val:
                filters_map[key] = val
        return filters_map

    def get_paginated_response(self, data: Any) -> Response:
        """Constructs canonical ADR-0053 cursor-paginated response envelope."""
        request_id = LogContextManager.get_request_id()
        if not request_id and hasattr(self, "request") and self.request:
            request_id = (
                self.request.META.get("HTTP_X_REQUEST_ID")
                or self.request.META.get("HTTP_X_CORRELATION_ID")
                or getattr(self.request, "request_id", "")
            )
        if not request_id:
            request_id = f"req-{uuid.uuid4()}"

        # Extract next and previous opaque cursor tokens
        next_cursor = None
        if self.has_next:
            next_link = self.get_next_link()
            if next_link:
                parsed = urllib.parse.urlparse(next_link)
                q_params = urllib.parse.parse_qs(parsed.query)
                next_cursor = q_params.get(self.cursor_query_param, [None])[0]

        previous_cursor = None
        if self.has_previous:
            prev_link = self.get_previous_link()
            if prev_link:
                parsed = urllib.parse.urlparse(prev_link)
                q_params = urllib.parse.parse_qs(parsed.query)
                previous_cursor = q_params.get(self.cursor_query_param, [None])[0]

        current_page_size = self.get_page_size(self.request) or len(data)

        payload = {
            "success": True,
            "code": "CURSOR_PAGINATED_RESULTS",
            "request_id": request_id,
            "correlation_id": request_id,
            "timestamp": timezone.now().isoformat(),
            "data": data,
            "meta": {
                "pagination": {
                    "type": "cursor",
                    "page_size": current_page_size,
                    "has_next": self.has_next,
                    "has_previous": self.has_previous,
                    "next_cursor": next_cursor,
                    "previous_cursor": previous_cursor,
                },
                "filters": self.get_filter_meta(),
            },
            "message_en": f"Retrieved {len(data)} records via deterministic cursor pagination.",
            "message_bn": f"নির্ধারিত কার্সার পেজিনেশনের মাধ্যমে {len(data)} টি রেকর্ড সফলভাবে লোড হয়েছে।",
        }
        return Response(payload)


class SafeAdaptivePagination(BasePagination):
    """
    Adaptive pagination router complying with ADR-0047 and ADR-0053.
    Activates SafeCursorPagination when ?cursor= or ?pagination_type=cursor is present;
    activates StandardPagination when ?page=, ?page_size=, or ?paginate=true is present;
    otherwise preserves backward compatibility by allowing unpaginated responses for fixtures.
    """

    def __init__(self) -> None:
        self.cursor_paginator = SafeCursorPagination()
        self.standard_paginator = StandardPagination()
        self._active_paginator: BasePagination | None = None

    def paginate_queryset(self, queryset: Any, request: Any, view: Any = None) -> list[Any] | None:
        self.request = request

        # 1. Deterministic Cursor Pagination requested
        if (
            self.cursor_paginator.cursor_query_param in request.query_params
            or request.query_params.get("pagination_type", "").strip().lower() == "cursor"
        ):
            # Propagate custom ordering if view declares cursor ordering
            if hasattr(view, "cursor_ordering") and view.cursor_ordering:
                self.cursor_paginator.ordering = view.cursor_ordering
            elif hasattr(view, "ordering") and view.ordering:
                self.cursor_paginator.ordering = view.ordering

            self._active_paginator = self.cursor_paginator
            return self.cursor_paginator.paginate_queryset(queryset, request, view)

        # 2. Standard Page Number Pagination requested
        should_paginate_standard = (
            self.standard_paginator.page_query_param in request.query_params
            or self.standard_paginator.page_size_query_param in request.query_params
            or request.query_params.get("paginate", "").strip().lower() in ("true", "1")
            or getattr(view, "enforce_pagination", False)
        )

        if should_paginate_standard:
            self._active_paginator = self.standard_paginator
            return self.standard_paginator.paginate_queryset(queryset, request, view)

        # 3. Default: unpaginated list for legacy fixtures and test helpers
        self._active_paginator = None
        return None

    def get_paginated_response(self, data: Any) -> Response:
        if self._active_paginator:
            return self._active_paginator.get_paginated_response(data)
        return Response(data)

    def to_html(self) -> str:
        if self._active_paginator and hasattr(self._active_paginator, "to_html"):
            return self._active_paginator.to_html()
        return ""


class ValidatedQueryFilterBackend(filters.BaseFilterBackend):
    """
    Validates query parameters against an explicit whitelist allowed on the viewset (ADR-0053).
    Guards against ORM injection, attribute probing (__lookups), and parameter pollution.
    """

    SYSTEM_ALLOWED_PARAMS = {
        "page",
        "page_size",
        "paginate",
        "cursor",
        "pagination_type",
        "ordering",
        "search",
        "format",
    }

    PROHIBITED_KEYWORD_PROBES = {
        "password",
        "secret",
        "token",
        "hash",
        "salt",
        "is_superuser",
        "is_staff",
    }

    def filter_queryset(self, request: Any, queryset: Any, view: Any) -> Any:
        allowed_fields = set(getattr(view, "allowed_filter_fields", set()))
        allowed = allowed_fields.union(self.SYSTEM_ALLOWED_PARAMS)

        if hasattr(view, "search_fields") and view.search_fields:
            for s in view.search_fields:
                allowed.add(s.lstrip("^=@$"))
        if hasattr(view, "ordering_fields") and view.ordering_fields:
            allowed.update(view.ordering_fields)
        if hasattr(view, "filterset_fields") and view.filterset_fields:
            allowed.update(view.filterset_fields)

        field_errors: dict[str, list[str]] = {}
        for param in request.query_params:
            param_clean = param.strip()
            # 1. Reject double-underscore lookups (ORM probe protection)
            if "__" in param_clean:
                field_errors[param_clean] = [
                    f"Direct ORM lookup probe '{param_clean}' is strictly forbidden."
                ]
            # 2. Reject sensitive keywords
            elif any(probe in param_clean.lower() for probe in self.PROHIBITED_KEYWORD_PROBES):
                field_errors[param_clean] = [
                    f"Querying sensitive internal attribute '{param_clean}' is strictly forbidden."
                ]
            # 3. Reject unwhitelisted parameters if view has declared allowed_filter_fields
            elif allowed_fields and param_clean not in allowed:
                field_errors[param_clean] = [
                    f"Unrecognized or forbidden filter parameter '{param_clean}'. Allowed: {sorted(allowed)}."
                ]

        if field_errors:
            raise FieldValidationError(
                field_errors=field_errors,
                message_en="One or more query filter parameters are invalid or forbidden.",
                message_bn="এক বা একাধিক ফিল্টার প্যারামিটার অবৈধ বা নিষিদ্ধ।",
            )

        return queryset


class UnicodePhoneticSearchFilter(filters.SearchFilter):
    """
    Bilingual and Unicode-safe search filter complying with ADR-0010 and ADR-0053.
    Normalizes query strings using Unicode NFKC, cleans zero-width characters,
    and supports Bengali phonetic alias matching for medicine brand names and generics.
    """

    BANGLA_PHONETIC_EXPANSIONS: dict[str, str] = {
        "নাপা": "napa",
        "প্যারাসিটামল": "paracetamol",
        "সেকলো": "seclo",
        "এস": "ace",
        "সিপ্রোসিন": "ciprocin",
        "অ্যামোক্সিসিলিন": "amoxicillin",
        "এমোক্সিসিলিন": "amoxicillin",
        "ইনসুলিন": "insulin",
        "টফেন": "tofen",
        "রোসুভা": "rosuva",
        "অ্যাজিথ্রোমাইসিন": "azithromycin",
        "প্যানটোপ্রাজল": "pantoprazole",
        "ক্যাফেইন": "caffeine",
    }

    def filter_queryset(self, request: Any, queryset: Any, view: Any) -> Any:
        search_terms = self.get_search_terms(request)
        if not search_terms:
            return queryset

        normalized_terms = [normalize_search_query(term) for term in search_terms]
        expanded_terms = list(normalized_terms)

        for term in normalized_terms:
            for bn_alias, en_equiv in self.BANGLA_PHONETIC_EXPANSIONS.items():
                if bn_alias in term or term in bn_alias:
                    if en_equiv not in expanded_terms:
                        expanded_terms.append(en_equiv)

        search_fields = self.get_search_fields(view, request)
        if not search_fields:
            return queryset

        import operator
        from functools import reduce

        conditions = []
        for term in expanded_terms:
            term_q = Q()
            for field in search_fields:
                clean_field = field.lstrip("^=@$")
                lookup = f"{clean_field}__icontains"
                term_q |= Q(**{lookup: term})
            conditions.append(term_q)

        if conditions:
            queryset = queryset.filter(reduce(operator.or_, conditions))

        return queryset
