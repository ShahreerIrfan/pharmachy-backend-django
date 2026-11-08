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
from apps.audit_logging.staff_audit_timeline_views import StaffAuditTimelineViewSet
from apps.audit_logging.views import AuditLogEntryViewSet
from apps.build_pipeline.views import BuildSmokeViewSet
from apps.core.announcement_views import (
    PlatformAnnouncementViewSet,
    TenantAnnouncementViewSet,
)
from apps.core.approval_views import ApprovalRequestViewSet
from apps.core.barcode_identifier_views import BarcodeAlternateIdentifierViewSet
from apps.core.billing_report_views import (
    PlatformBillingReportViewSet,
    TenantBillingReportViewSet,
)
from apps.core.branch_lifecycle_views import BranchLifecycleViewSet
from apps.core.branch_membership_views import BranchStaffMembershipViewSet
from apps.core.branch_operating_settings_views import BranchOperatingSettingsViewSet
from apps.core.bulk_import_views import MedicineBulkImportViewSet
from apps.core.cancellation_grace_views import SubscriptionLifecycleViewSet
from apps.core.cashier_register_views import CashierRegisterAssignmentViewSet
from apps.core.catalogue_contract_views import CatalogueContractSuiteViewSet
from apps.core.catalogue_recall_views import CatalogueLifecycleNoticeViewSet
from apps.core.classification_snapshot_views import MedicineClassificationSnapshotViewSet
from apps.core.conditional_pricing_views import ConditionalPricingViewSet
from apps.core.cross_tenant_metrics_views import (
    PlatformCrossTenantMetricsViewSet,
    TenantPlatformBenchmarksViewSet,
)
from apps.core.custom_role_views import (
    CustomRoleViewSet,
    PermissionCatalogViewSet,
)
from apps.core.device_consumable_views import DeviceConsumableEditorViewSet
from apps.core.device_registration_views import LoginDeviceRegistrationViewSet
from apps.core.device_seat_views import DeviceSeatViewSet
from apps.core.dosage_form_qa_views import DosageFormQaViewSet
from apps.core.dosage_form_views import DosageFormViewSet
from apps.core.downgrade_views import SubscriptionDowngradeViewSet
from apps.core.drops_views import DropsEditorViewSet
from apps.core.dunning_views import DunningNotificationViewSet
from apps.core.dynamic_field_group_views import DynamicFieldGroupLayoutViewSet
from apps.core.feature_flag_views import (
    PlatformFeatureFlagViewSet,
    TenantFeatureFlagEvaluationViewSet,
)
from apps.core.field_visibility_views import FieldVisibilityViewSet
from apps.core.flexible_medicine_payload_views import FlexibleMedicinePayloadViewSet
from apps.core.form_template_migration_views import FormTemplateMigrationViewSet
from apps.core.gateway_views import PaymentGatewayViewSet
from apps.core.granular_permission_views import GranularPermissionViewSet
from apps.core.health import legacy_health_check, liveness_probe, readiness_probe
from apps.core.inhaler_views import InhalerEditorViewSet
from apps.core.injection_vial_views import InjectionVialEditorViewSet
from apps.core.insulin_views import InsulinEditorViewSet
from apps.core.inventory_base_unit_views import InventoryBaseUnitStrategyViewSet
from apps.core.local_inventory_views import LocalInventoryViewSet
from apps.core.loose_tablet_sales_views import LooseTabletSalesViewSet
from apps.core.manual_payment_views import ManualPaymentClaimViewSet
from apps.core.manufacturer_supplier_views import ManufacturerViewSet, SupplierViewSet
from apps.core.medicine_details_preview_views import MedicineDetailsPreviewViewSet
from apps.core.medicine_editor_schema_views import MedicineEditorSchemaViewSet
from apps.core.medicine_form_template_views import MedicineFormTemplateViewSet
from apps.core.medicine_search_views import MedicineSearchAutocompleteViewSet
from apps.core.medicine_variant_views import MedicineVariantViewSet
from apps.core.multi_level_unit_conversion_views import MultiLevelUnitConversionViewSet
from apps.core.batch_entity_views import BatchEntityViewSet
from apps.core.purchase_cost_lot_views import PurchaseCostLotViewSet
from apps.core.batch_receipt_linking_views import BatchReceiptLinkingViewSet
from apps.core.fefo_allocation_views import FefoAllocationViewSet
from apps.core.sale_lot_allocation_views import SaleLotAllocationViewSet
from apps.core.stock_availability_views import StockAvailabilityViewSet
from apps.core.batch_expiry_views import BatchExpiryViewSet
from apps.core.stock_correction_views import StockCorrectionViewSet
from apps.core.branch_batch_ownership_views import BranchBatchOwnershipViewSet
from apps.core.landed_cost_views import LandedCostExpenseViewSet
from apps.core.return_safety_inspection_views import ReturnSafetyInspectionViewSet
from apps.core.batch_merge_views import BatchMergeViewSet
from apps.core.stock_valuation_views import StockValuationViewSet
from apps.core.packaging_concurrency_views import PackagingConcurrencyViewSet
from apps.core.supplier_organization_views import SupplierOrganizationViewSet
from apps.core.vendor_price_list_views import VendorProductPriceListViewSet
from apps.core.purchase_requisition_views import PurchaseRequisitionViewSet
from apps.core.purchase_order_views import PurchaseOrderViewSet
from apps.core.goods_receipt_views import GoodsReceiptViewSet
from apps.core.non_divisible_pack_views import NonDivisiblePackViewSet
from apps.core.non_medicine_retail_views import NonMedicineRetailItemViewSet
from apps.core.offline_entitlement_views import OfflineEntitlementViewSet
from apps.core.offline_lapse_views import LapsedPlanReconciliationViewSet
from apps.core.onboarding_views import PlatformTenantOnboardingWizardViewSet
from apps.core.override_precedence_views import OverridePrecedenceViewSet
from apps.core.owner_dashboard_views import OwnerDashboardViewSet
from apps.core.packaging_hierarchy_views import PackagingHierarchyViewSet
from apps.core.plan_catalogue_views import (
    PlanCatalogueViewSet,
    TenantEffectiveEntitlementViewSet,
    TenantEntitlementViewSet,
)
from apps.core.platform_access_views import PlatformAccessPolicyViewSet
from apps.core.product_metadata_review_views import ProductMetadataReviewViewSet
from apps.core.prorated_upgrade_views import ProratedUpgradeViewSet
from apps.core.quota_counter_views import TenantQuotaUsageViewSet
from apps.core.regulated_stock_entry_views import RegulatedStockEntryViewSet
from apps.core.regulatory_source_views import RegulatorySourceAttachmentViewSet
from apps.core.renewal_invoice_views import RenewalInvoiceViewSet
from apps.core.role_migration_views import RoleMigrationGrantsViewSet
from apps.core.sachet_views import SachetEditorViewSet
from apps.core.shift_views import StaffShiftScheduleViewSet
from apps.core.staff_lifecycle_views import StaffLifecycleViewSet
from apps.core.staff_membership_views import (
    StaffInvitationViewSet,
    StaffMembershipViewSet,
)
from apps.core.staff_offline_auth_views import StaffOfflineAuthorizationViewSet
from apps.core.staff_performance_views import StaffPerformanceMetricsViewSet
from apps.core.storage_conditional_views import StorageConditionalViewSet
from apps.core.strength_taxonomy_views import StrengthTaxonomyViewSet
from apps.core.subscription_portal_views import SubscriptionCustomerPortalViewSet
from apps.core.subscription_support_views import SubscriptionSupportOverrideViewSet
from apps.core.subscription_views import SubscriptionViewSet
from apps.core.support_access_views import (
    PlatformSupportAccessViewSet,
    TenantSupportAccessViewSet,
)
from apps.core.support_ticket_views import (
    PlatformSupportTicketViewSet,
    TenantSupportTicketViewSet,
)
from apps.core.suppository_views import SuppositoryEditorViewSet
from apps.core.suspension_reconstitution_views import SuspensionReconstitutionEditorViewSet
from apps.core.syrup_liquid_views import SyrupLiquidEditorViewSet
from apps.core.tablet_capsule_views import TabletCapsuleEditorViewSet
from apps.core.tenant_deletion_views import (
    PlatformDeletionAssessmentViewSet,
    TenantDeletionRequestViewSet,
)
from apps.core.tenant_export_views import (
    PlatformDataExportAdminViewSet,
    TenantDataExportViewSet,
)
from apps.core.tenant_medicine_config_views import TenantMedicineConfigViewSet
from apps.core.topical_views import TopicalEditorViewSet
from apps.core.trial_policy_views import TrialPolicyViewSet
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
from apps.identity.active_session_views import ActiveSessionViewSet
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
router.register("branches/lifecycle", BranchLifecycleViewSet, basename="branch-lifecycle")
router.register("branches/operating-settings", BranchOperatingSettingsViewSet, basename="branch-operating-settings")
router.register("branches", BranchLifecycleViewSet, basename="branch")
router.register("approvals", ApprovalRequestViewSet, basename="approval-request")
router.register("shifts", StaffShiftScheduleViewSet, basename="shift-schedule")
router.register("field-visibility", FieldVisibilityViewSet, basename="field-visibility")
router.register("devices/registrations", LoginDeviceRegistrationViewSet, basename="device-registration")
router.register("registers/assignments", CashierRegisterAssignmentViewSet, basename="register-assignments")
router.register("sessions/active", ActiveSessionViewSet, basename="active-sessions")
router.register("staff/offline-authorizations", StaffOfflineAuthorizationViewSet, basename="staff-offline-authorizations")
router.register("dosage-forms", DosageFormViewSet, basename="dosage-forms")
router.register("catalog/manufacturers", ManufacturerViewSet, basename="manufacturers")
router.register("catalog/classifications", MedicineClassificationSnapshotViewSet, basename="catalog-classifications")
router.register("catalog/overrides", OverridePrecedenceViewSet, basename="catalog-overrides")
router.register("catalog/search", MedicineSearchAutocompleteViewSet, basename="catalog-search")
router.register("catalog/templates", MedicineFormTemplateViewSet, basename="catalog-templates")
router.register("catalog/bulk-import", MedicineBulkImportViewSet, basename="catalog-bulk-import")
router.register("catalog/reviews", ProductMetadataReviewViewSet, basename="catalog-reviews")
router.register("catalog/regulatory-sources", RegulatorySourceAttachmentViewSet, basename="catalog-regulatory-sources")
router.register("catalog/retail-items", NonMedicineRetailItemViewSet, basename="catalog-retail-items")
router.register("catalog/barcodes", BarcodeAlternateIdentifierViewSet, basename="catalog-barcodes")
router.register("catalog/recalls", CatalogueLifecycleNoticeViewSet, basename="catalog-recalls")
router.register("catalog/medicine-previews", MedicineDetailsPreviewViewSet, basename="catalog-medicine-previews")
router.register("catalog/template-migrations", FormTemplateMigrationViewSet, basename="catalog-template-migrations")
router.register("catalog/contracts", CatalogueContractSuiteViewSet, basename="catalog-contracts")
router.register("catalog/tablet-capsule", TabletCapsuleEditorViewSet, basename="catalog-tablet-capsule")
router.register("catalog/syrup-liquid", SyrupLiquidEditorViewSet, basename="catalog-syrup-liquid")
router.register("catalog/suspension-reconstitution", SuspensionReconstitutionEditorViewSet, basename="catalog-suspension-reconstitution")
router.register("catalog/injection-vials", InjectionVialEditorViewSet, basename="catalog-injection-vials")
router.register("catalog/insulin", InsulinEditorViewSet, basename="catalog-insulin")
router.register("catalog/drops", DropsEditorViewSet, basename="catalog-drops")
router.register("catalog/inhaler", InhalerEditorViewSet, basename="catalog-inhaler")
router.register("catalog/topical", TopicalEditorViewSet, basename="catalog-topical")
router.register("catalog/sachets", SachetEditorViewSet, basename="catalog-sachets")
router.register("catalog/suppositories", SuppositoryEditorViewSet, basename="catalog-suppositories")
router.register("catalog/devices", DeviceConsumableEditorViewSet, basename="catalog-devices")
router.register("catalog/layout/groups", DynamicFieldGroupLayoutViewSet, basename="catalog-layout-groups")
router.register("catalog/conditional-pricing", ConditionalPricingViewSet, basename="catalog-conditional-pricing")
router.register("catalog/stock-entry", RegulatedStockEntryViewSet, basename="catalog-stock-entry")
router.register("catalog/storage-conditions", StorageConditionalViewSet, basename="catalog-storage-conditions")
router.register("catalog/local-inventory", LocalInventoryViewSet, basename="catalog-local-inventory")
router.register("catalog/flexible-payloads", FlexibleMedicinePayloadViewSet, basename="catalog-flexible-payloads")
router.register("catalog/qa-fixtures", DosageFormQaViewSet, basename="catalog-qa-fixtures")
router.register("inventory/base-unit-strategies", InventoryBaseUnitStrategyViewSet, basename="inventory-base-unit-strategies")
router.register("inventory/packaging-hierarchy", PackagingHierarchyViewSet, basename="inventory-packaging-hierarchy")
router.register("inventory/unit-conversion", MultiLevelUnitConversionViewSet, basename="inventory-unit-conversion")
router.register("inventory/loose-tablet-sales", LooseTabletSalesViewSet, basename="inventory-loose-tablet-sales")
router.register("inventory/non-divisible-packs", NonDivisiblePackViewSet, basename="inventory-non-divisible-packs")
router.register("inventory/batch-entities", BatchEntityViewSet, basename="inventory-batch-entities")
router.register("inventory/purchase-cost-lots", PurchaseCostLotViewSet, basename="inventory-purchase-cost-lots")
router.register("inventory/goods-receipts", BatchReceiptLinkingViewSet, basename="inventory-goods-receipts")
router.register("inventory/fefo", FefoAllocationViewSet, basename="inventory-fefo")
router.register("inventory/sale-lot-allocations", SaleLotAllocationViewSet, basename="inventory-sale-lot-allocations")
router.register("inventory/availability", StockAvailabilityViewSet, basename="inventory-availability")
router.register("inventory/expiry-windows", BatchExpiryViewSet, basename="inventory-expiry-windows")
router.register("inventory/stock-corrections", StockCorrectionViewSet, basename="inventory-stock-corrections")
router.register("inventory/branch-batches", BranchBatchOwnershipViewSet, basename="inventory-branch-batches")
router.register("inventory/landed-costs", LandedCostExpenseViewSet, basename="inventory-landed-costs")
router.register("inventory/return-inspections", ReturnSafetyInspectionViewSet, basename="inventory-return-inspections")
router.register("inventory/batch-merges", BatchMergeViewSet, basename="inventory-batch-merges")
router.register("inventory/valuation", StockValuationViewSet, basename="inventory-valuation")
router.register("inventory/concurrency-tests", PackagingConcurrencyViewSet, basename="inventory-concurrency-tests")
router.register("procurement/suppliers", SupplierOrganizationViewSet, basename="procurement-suppliers")
router.register("procurement/price-lists", VendorProductPriceListViewSet, basename="procurement-price-lists")
router.register("procurement/requisitions", PurchaseRequisitionViewSet, basename="procurement-requisitions")
router.register("procurement/orders", PurchaseOrderViewSet, basename="procurement-orders")
router.register("procurement/goods-receipts", GoodsReceiptViewSet, basename="procurement-goods-receipts")
router.register("suppliers", SupplierViewSet, basename="suppliers")
router.register("medicines/editor-schema", MedicineEditorSchemaViewSet, basename="medicine-editor-schema")
router.register("medicines/strength-taxonomy", StrengthTaxonomyViewSet, basename="strength-taxonomy")
router.register("medicines/variants", MedicineVariantViewSet, basename="medicine-variants")
router.register("medicines", MedicineProductViewSet, basename="medicine")
router.register("products", MedicineProductViewSet, basename="product")
router.register("batches", BatchStockViewSet, basename="batch")
router.register("invoices", SaleInvoiceViewSet, basename="invoice")
router.register("async-jobs", AsyncJobRecordViewSet, basename="async-job")
router.register("analytics/platform-benchmarks", TenantPlatformBenchmarksViewSet, basename="tenant-platform-benchmarks")
router.register("analytics/staff-performance", StaffPerformanceMetricsViewSet, basename="staff-performance-metrics")
router.register("analytics", DailyBranchAnalyticsViewSet, basename="analytics")
router.register("compliance-reminders", ComplianceReminderViewSet, basename="compliance-reminder")
router.register("subscriptions", SubscriptionRecordViewSet, basename="subscription")
router.register("roles/catalog", PermissionCatalogViewSet, basename="roles-catalog")
router.register("roles/custom", CustomRoleViewSet, basename="roles-custom")
router.register("roles/migrations", RoleMigrationGrantsViewSet, basename="roles-migrations")
router.register("staff/invitations", StaffInvitationViewSet, basename="staff-invitations")
router.register("staff/memberships", StaffMembershipViewSet, basename="staff-memberships")
router.register("staff/branch-memberships", BranchStaffMembershipViewSet, basename="staff-branch-memberships")
router.register("staff/lifecycle", StaffLifecycleViewSet, basename="staff-lifecycle")
router.register("staff/audit-timeline", StaffAuditTimelineViewSet, basename="staff-audit-timeline")
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
router.register("permissions/granular", GranularPermissionViewSet, basename="permissions-granular")
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
router.register("billing/offline-reconciliation", LapsedPlanReconciliationViewSet, basename="billing-offline-reconciliation")
router.register("billing/quota-usage", TenantQuotaUsageViewSet, basename="billing-quota-usage")
router.register("billing/devices", DeviceSeatViewSet, basename="billing-devices")
router.register("billing/portal", SubscriptionCustomerPortalViewSet, basename="billing-portal")
router.register("billing/reports", TenantBillingReportViewSet, basename="billing-reports")
router.register("billing/dunning", DunningNotificationViewSet, basename="billing-dunning")
router.register("billing/support-overrides", SubscriptionSupportOverrideViewSet, basename="billing-support-overrides")
router.register("owner/dashboard", OwnerDashboardViewSet, basename="owner-dashboard")
router.register(
    "platform/billing-reports", PlatformBillingReportViewSet, basename="platform-billing-reports"
)


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
