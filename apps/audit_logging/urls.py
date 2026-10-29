"""
apps.audit_logging.urls
Architecture Decision: ADR-0035
URL patterns for Audit Logging and Observability API.
"""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.audit_logging.views import AuditLogEntryViewSet

router = DefaultRouter()
router.register("audit-logs", AuditLogEntryViewSet, basename="audit-log")

urlpatterns = router.urls
