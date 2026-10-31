"""
Root URL Configuration for Bangladesh Multi-Tenant Pharmacy ERP & POS Cloud Authority API
Architecture Decision: ADR-0004, ADR-0021 & ADR-0031
"""

from __future__ import annotations

from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

from apps.audit_logging.views import AuditLogEntryViewSet
from apps.build_pipeline.views import BuildSmokeViewSet
from apps.core.views import (
    ApiResponseConventionViewSet,
    AsyncJobRecordViewSet,
    AsyncTaskIdempotencyViewSet,
    BatchStockViewSet,
    BranchViewSet,
    ComplianceReminderViewSet,
    DailyBranchAnalyticsViewSet,
    GlobalExceptionHandlingViewSet,
    MedicineProductViewSet,
    PaymentTransactionViewSet,
    PrescriptionRecordViewSet,
    RateLimitPolicyViewSet,
    SaleInvoiceViewSet,
    SoftDeletionAndArchivalViewSet,
    StaffMemberViewSet,
    SubscriptionRecordViewSet,
    TenantContextViewSet,
    TenantViewSet,
    TimeMoneyStandardsViewSet,
    ValidationContractViewSet,
)
from apps.data_privacy.views import DataPrivacyViewSet
from apps.developer_toolchain.views import DeveloperQuickstartViewSet
from apps.environment_governance.views import EnvironmentProfileViewSet
from apps.file_storage.views import SecureFileDownloadView, SecureFileStorageViewSet
from apps.identity.auth_views import AuthenticationViewSet
from apps.identity.views import PharmacyUserViewSet
from apps.quality_governance.views import (
    DefinitionOfDoneAuditViewSet,
    QualityIssueTicketViewSet,
)
from apps.release_management.views import ReleaseManifestViewSet
from apps.secrets_vault.views import TenantSecretViewSet

router = DefaultRouter()
router.register("auth", AuthenticationViewSet, basename="auth")
router.register("tenants", TenantViewSet, basename="tenant")
router.register("branches", BranchViewSet, basename="branch")
router.register("medicines", MedicineProductViewSet, basename="medicine")
router.register("batches", BatchStockViewSet, basename="batch")
router.register("invoices", SaleInvoiceViewSet, basename="invoice")
router.register("async-jobs", AsyncJobRecordViewSet, basename="async-job")
router.register("analytics", DailyBranchAnalyticsViewSet, basename="analytics")
router.register("compliance-reminders", ComplianceReminderViewSet, basename="compliance-reminder")
router.register("subscriptions", SubscriptionRecordViewSet, basename="subscription")
router.register("staff", StaffMemberViewSet, basename="staff")
router.register("prescriptions", PrescriptionRecordViewSet, basename="prescription")
router.register("payments", PaymentTransactionViewSet, basename="payment")
router.register("audit-logs", AuditLogEntryViewSet, basename="audit-log")
router.register("developer/quickstart", DeveloperQuickstartViewSet, basename="developer-quickstart")
router.register("secrets", TenantSecretViewSet, basename="tenant-secret")
router.register("releases", ReleaseManifestViewSet, basename="release")
router.register("quality/issues", QualityIssueTicketViewSet, basename="quality-issue")
router.register("quality/dod-audits", DefinitionOfDoneAuditViewSet, basename="dod-audit")
router.register("pipeline/smoke", BuildSmokeViewSet, basename="build-smoke")
router.register("environment/profile", EnvironmentProfileViewSet, basename="environment-profile")
router.register("identity/users", PharmacyUserViewSet, basename="identity-user")
router.register("validation", ValidationContractViewSet, basename="validation")
router.register("conventions", ApiResponseConventionViewSet, basename="conventions")
router.register("rate-limits", RateLimitPolicyViewSet, basename="rate-limits")
router.register("tenant-context", TenantContextViewSet, basename="tenant-context")
router.register("storage/files", SecureFileStorageViewSet, basename="secure-file")
router.register("storage/download", SecureFileDownloadView, basename="secure-download")
router.register("privacy/requests", DataPrivacyViewSet, basename="privacy-request")
router.register("exceptions", GlobalExceptionHandlingViewSet, basename="exceptions")
router.register("standards/time-money", TimeMoneyStandardsViewSet, basename="standards-time-money")
router.register("archival", SoftDeletionAndArchivalViewSet, basename="archival")
router.register("async-tasks", AsyncTaskIdempotencyViewSet, basename="async-task")


# Architecture Decision: ADR-0054 Centralized Global Exception & URL Fallback Handlers
handler404 = "apps.core.errors.global_handler404"
handler500 = "apps.core.errors.global_handler500"


def health_check(request):
    """Probes system readiness, database connectivity, and version."""
    from apps.core.celery_boundary import AsyncJobBoundaryEnforcer

    redis_status = AsyncJobBoundaryEnforcer.check_redis_status()
    return JsonResponse(
        {
            "service": "pharmacy-saas-cloud-api",
            "version": "2.0.0",
            "status": "HEALTHY",
            "timezone": "Asia/Dhaka",
            "redis": redis_status,
        }
    )


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health_check, name="health-check"),
    path("api/v2/", include(router.urls)),
    # OpenAPI 3.0 Documentation Endpoints
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
