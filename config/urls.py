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

from apps.audit_logging.platform_audit_views import PlatformAuditSearchViewSet
from apps.audit_logging.views import AuditLogEntryViewSet
from apps.build_pipeline.views import BuildSmokeViewSet
from apps.core.announcement_views import (
    PlatformAnnouncementViewSet,
    TenantAnnouncementViewSet,
)
from apps.core.cross_tenant_metrics_views import (
    PlatformCrossTenantMetricsViewSet,
    TenantPlatformBenchmarksViewSet,
)
from apps.core.feature_flag_views import (
    PlatformFeatureFlagViewSet,
    TenantFeatureFlagEvaluationViewSet,
)
from apps.core.health import legacy_health_check, liveness_probe, readiness_probe
from apps.core.onboarding_views import PlatformTenantOnboardingWizardViewSet
from apps.core.plan_catalogue_views import (
    PlanCatalogueViewSet,
    TenantEffectiveEntitlementViewSet,
    TenantEntitlementViewSet,
)
from apps.core.platform_access_views import PlatformAccessPolicyViewSet
from apps.core.subscription_views import SubscriptionViewSet
from apps.core.trial_policy_views import TrialPolicyViewSet
from apps.core.renewal_invoice_views import RenewalInvoiceViewSet
from apps.core.manual_payment_views import ManualPaymentClaimViewSet
from apps.core.gateway_views import PaymentGatewayViewSet
from apps.core.prorated_upgrade_views import ProratedUpgradeViewSet
from apps.core.downgrade_views import SubscriptionDowngradeViewSet
from apps.core.cancellation_grace_views import SubscriptionLifecycleViewSet
from apps.core.offline_entitlement_views import OfflineEntitlementViewSet
from apps.core.support_access_views import (
    PlatformSupportAccessViewSet,
    TenantSupportAccessViewSet,
)
from apps.core.support_ticket_views import (
    PlatformSupportTicketViewSet,
    TenantSupportTicketViewSet,
)
from apps.core.tenant_deletion_views import (
    PlatformDeletionAssessmentViewSet,
    TenantDeletionRequestViewSet,
)
from apps.core.tenant_export_views import (
    PlatformDataExportAdminViewSet,
    TenantDataExportViewSet,
)
from apps.core.tenant_medicine_config_views import TenantMedicineConfigViewSet
from apps.core.views import (
    ApiResponseConventionViewSet,
    AsyncJobRecordViewSet,
    AsyncTaskIdempotencyViewSet,
    BatchStockViewSet,
    BranchViewSet,
    ComplianceReminderViewSet,
    DailyBranchAnalyticsViewSet,
    EndpointPermissionMatrixViewSet,
    GlobalExceptionHandlingViewSet,
    HealthDiagnosticsViewSet,
    MedicineProductViewSet,
    PaymentTransactionViewSet,
    PlatformConsoleViewSet,
    PrescriptionRecordViewSet,
    RateLimitPolicyViewSet,
    SaleInvoiceViewSet,
    SecurityBaselineViewSet,
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
router.register("tenants/medicine-configs", TenantMedicineConfigViewSet, basename="tenant-medicine-configs")
router.register("tenants/announcements", TenantAnnouncementViewSet, basename="tenant-announcements")
router.register("tenants/support-access", TenantSupportAccessViewSet, basename="tenant-support-access")
router.register("tenants/support/tickets", TenantSupportTicketViewSet, basename="tenant-support-tickets")
router.register("tenants/feature-flags", TenantFeatureFlagEvaluationViewSet, basename="tenant-feature-flags")
router.register("tenants/data-exports", TenantDataExportViewSet, basename="tenant-data-exports")
router.register("tenants/deletion-requests", TenantDeletionRequestViewSet, basename="tenant-deletion-requests")
router.register("tenants", TenantViewSet, basename="tenant")
router.register("branches", BranchViewSet, basename="branch")
router.register("medicines", MedicineProductViewSet, basename="medicine")
router.register("products", MedicineProductViewSet, basename="product")
router.register("batches", BatchStockViewSet, basename="batch")
router.register("invoices", SaleInvoiceViewSet, basename="invoice")
router.register("async-jobs", AsyncJobRecordViewSet, basename="async-job")
router.register("analytics/platform-benchmarks", TenantPlatformBenchmarksViewSet, basename="tenant-platform-benchmarks")
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
router.register("health/diagnostics", HealthDiagnosticsViewSet, basename="health-diagnostics")
router.register("permissions/matrix", EndpointPermissionMatrixViewSet, basename="permission-matrix")
router.register("security/baseline", SecurityBaselineViewSet, basename="security-baseline")
router.register("platform/announcements", PlatformAnnouncementViewSet, basename="platform-announcements")
router.register("platform/access-policies", PlatformAccessPolicyViewSet, basename="platform-access-policies")
router.register("platform/audit-search", PlatformAuditSearchViewSet, basename="platform-audit-search")
router.register(
    "platform/onboarding-wizard", PlatformTenantOnboardingWizardViewSet, basename="platform-onboarding-wizard"
)
router.register("platform/console", PlatformConsoleViewSet, basename="platform-console")
router.register("platform/support-access", PlatformSupportAccessViewSet, basename="platform-support-access")
router.register("platform/support/tickets", PlatformSupportTicketViewSet, basename="platform-support-tickets")
router.register("platform/feature-flags", PlatformFeatureFlagViewSet, basename="platform-feature-flags")
router.register("platform/data-exports", PlatformDataExportAdminViewSet, basename="platform-data-exports")
router.register(
    "platform/deletion-assessments", PlatformDeletionAssessmentViewSet, basename="platform-deletion-assessments"
)
router.register(
    "platform/cross-tenant-metrics", PlatformCrossTenantMetricsViewSet, basename="platform-cross-tenant-metrics"
)


router.register("billing/plans", PlanCatalogueViewSet, basename="billing-plans")
router.register("billing/entitlements", TenantEntitlementViewSet, basename="billing-entitlements")
router.register(
    "billing/effective-entitlements", TenantEffectiveEntitlementViewSet, basename="billing-effective-entitlements"
)
router.register("billing/subscriptions", SubscriptionViewSet, basename="billing-subscriptions")
router.register("billing/trial", TrialPolicyViewSet, basename="billing-trial")
router.register("billing/invoices", RenewalInvoiceViewSet, basename="billing-invoices")
router.register("billing/payment-claims", ManualPaymentClaimViewSet, basename="billing-payment-claims")
router.register("billing/gateway", PaymentGatewayViewSet, basename="billing-gateway")
router.register("billing/upgrade", ProratedUpgradeViewSet, basename="billing-upgrade")
router.register("billing/downgrade", SubscriptionDowngradeViewSet, basename="billing-downgrade")
router.register("billing/lifecycle", SubscriptionLifecycleViewSet, basename="billing-lifecycle")
router.register("billing/offline-leases", OfflineEntitlementViewSet, basename="billing-offline-leases")


# Architecture Decision: ADR-0054 Centralized Global Exception & URL Fallback Handlers
handler404 = "apps.core.errors.global_handler404"
handler500 = "apps.core.errors.global_handler500"


urlpatterns = [
    path("admin/", admin.site.urls),
    # Architecture Decision: ADR-0058 Liveness and Readiness Probes
    path("health/live/", liveness_probe, name="health-live"),
    path("health/ready/", readiness_probe, name="health-ready"),
    path("health/", legacy_health_check, name="health-check"),
    path("healthz", liveness_probe, name="healthz"),
    path("readyz", readiness_probe, name="readyz"),
    path("api/v2/", include(router.urls)),
    # OpenAPI 3.0 Documentation Endpoints
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
