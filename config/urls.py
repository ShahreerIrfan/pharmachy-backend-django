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
from apps.core.auto_reorder_planner_views import AutoReorderPlannerViewSet
from apps.core.barcode_identifier_views import BarcodeAlternateIdentifierViewSet
from apps.core.barcode_inventory_views import BarcodeInventoryViewSet
from apps.core.batch_entity_views import BatchEntityViewSet
from apps.core.batch_expiry_views import BatchExpiryViewSet
from apps.core.batch_merge_views import BatchMergeViewSet
from apps.core.batch_receipt_linking_views import BatchReceiptLinkingViewSet
from apps.core.billing_report_views import (
    PlatformBillingReportViewSet,
    TenantBillingReportViewSet,
)
from apps.core.branch_batch_ownership_views import BranchBatchOwnershipViewSet
from apps.core.branch_lifecycle_views import BranchLifecycleViewSet
from apps.core.branch_membership_views import BranchStaffMembershipViewSet
from apps.core.branch_operating_settings_views import BranchOperatingSettingsViewSet
from apps.core.bulk_import_views import MedicineBulkImportViewSet
from apps.core.cancellation_grace_views import SubscriptionLifecycleViewSet
from apps.core.cashier_register_views import CashierRegisterAssignmentViewSet
from apps.core.catalogue_contract_views import CatalogueContractSuiteViewSet
from apps.core.catalogue_recall_views import CatalogueLifecycleNoticeViewSet
from apps.core.classification_snapshot_views import MedicineClassificationSnapshotViewSet
from apps.core.cold_chain_excursion_views import ColdChainExcursionViewSet
from apps.core.conditional_pricing_views import ConditionalPricingViewSet
from apps.core.cross_tenant_metrics_views import (
    PlatformCrossTenantMetricsViewSet,
    TenantPlatformBenchmarksViewSet,
)
from apps.core.custom_role_views import (
    CustomRoleViewSet,
    PermissionCatalogViewSet,
)
from apps.core.delivery_logistics_views import DeliveryLogisticsViewSet
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
from apps.core.fefo_allocation_views import FefoAllocationViewSet
from apps.core.field_visibility_views import FieldVisibilityViewSet
from apps.core.flexible_medicine_payload_views import FlexibleMedicinePayloadViewSet
from apps.core.form_template_migration_views import FormTemplateMigrationViewSet
from apps.core.gateway_views import PaymentGatewayViewSet
from apps.core.goods_receipt_views import GoodsReceiptViewSet
from apps.core.granular_permission_views import GranularPermissionViewSet
from apps.core.health import legacy_health_check, liveness_probe, readiness_probe
from apps.core.inhaler_views import InhalerEditorViewSet
from apps.core.injection_vial_views import InjectionVialEditorViewSet
from apps.core.insulin_views import InsulinEditorViewSet
from apps.core.interbranch_transfer_views import InterBranchTransferViewSet
from apps.core.inventory_base_unit_views import InventoryBaseUnitStrategyViewSet
from apps.core.inventory_count_views import InventoryCountSessionViewSet
from apps.core.inventory_fault_stress_views import InventoryFaultStressViewSet
from apps.core.inventory_snapshot_views import InventorySnapshotViewSet
from apps.core.inventory_valuation_views import InventoryValuationReportViewSet
from apps.core.landed_cost_views import LandedCostExpenseViewSet
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
from apps.core.multi_supplier_comparison_views import MultiSupplierComparisonViewSet
from apps.core.non_divisible_pack_views import NonDivisiblePackViewSet
from apps.core.non_medicine_retail_views import NonMedicineRetailItemViewSet
from apps.core.offline_entitlement_views import OfflineEntitlementViewSet
from apps.core.offline_lapse_views import LapsedPlanReconciliationViewSet
from apps.core.onboarding_views import PlatformTenantOnboardingWizardViewSet
from apps.core.override_precedence_views import OverridePrecedenceViewSet
from apps.core.owner_dashboard_views import OwnerDashboardViewSet
from apps.core.packaging_concurrency_views import PackagingConcurrencyViewSet
from apps.core.packaging_hierarchy_views import PackagingHierarchyViewSet
from apps.core.plan_catalogue_views import (
    PlanCatalogueViewSet,
    TenantEffectiveEntitlementViewSet,
    TenantEntitlementViewSet,
)
from apps.core.platform_access_views import PlatformAccessPolicyViewSet
from apps.core.pos_domain_views import (
    PosAntibioticRegisterViewSet,
    PosCreditEligibilityViewSet,
    PosCustomerDueViewSet,
    PosCustomerReceivableLedgerViewSet,
    PosCustomerStatementViewSet,
    PosCustomerViewSet,
    PosDeliveryOrderViewSet,
    PosInvoiceNumberPolicyViewSet,
    PosPartialCollectionViewSet,
    PosPaymentAttemptViewSet,
    PosCashierShiftViewSet,
    PosDailyCloseViewSet,
    PosSaleReturnViewSet,
    PosReceiptPrintLogViewSet,
    PosSaleAllocationViewSet,
    PosSaleOrderViewSet,
)
from apps.core.pos_resilience_views import PosErrorIncidentViewSet
from apps.core.pos_sales_e2e_qa_views import PosSalesE2EQaViewSet
from apps.core.pos_search_cart_views import PosFastSearchViewSet
from apps.core.procurement_e2e_qa_views import ProcurementE2EQaViewSet
from apps.core.product_metadata_review_views import ProductMetadataReviewViewSet
from apps.core.prorated_upgrade_views import ProratedUpgradeViewSet
from apps.core.purchase_cost_lot_views import PurchaseCostLotViewSet
from apps.core.purchase_document_views import PurchaseDocumentViewSet
from apps.core.purchase_order_views import PurchaseOrderViewSet
from apps.core.purchase_requisition_views import PurchaseRequisitionViewSet
from apps.core.purchase_return_views import PurchaseReturnViewSet
from apps.core.quota_counter_views import TenantQuotaUsageViewSet
from apps.core.regulated_stock_entry_views import RegulatedStockEntryViewSet
from apps.core.regulatory_source_views import RegulatorySourceAttachmentViewSet
from apps.core.renewal_invoice_views import RenewalInvoiceViewSet
from apps.core.return_safety_inspection_views import ReturnSafetyInspectionViewSet
from apps.core.role_migration_views import RoleMigrationGrantsViewSet
from apps.core.sachet_views import SachetEditorViewSet
from apps.core.sale_lot_allocation_views import SaleLotAllocationViewSet
from apps.core.shift_views import StaffShiftScheduleViewSet
from apps.core.staff_lifecycle_views import StaffLifecycleViewSet
from apps.core.staff_membership_views import (
    StaffInvitationViewSet,
    StaffMembershipViewSet,
)
from apps.core.staff_offline_auth_views import StaffOfflineAuthorizationViewSet
from apps.core.staff_performance_views import StaffPerformanceMetricsViewSet
from apps.core.stock_adjustment_views import StockAdjustmentViewSet
from apps.core.stock_availability_views import StockAvailabilityViewSet
from apps.core.stock_correction_views import StockCorrectionViewSet
from apps.core.stock_inquiry_views import StockInquiryViewSet
from apps.core.stock_ledger_views import StockLedgerViewSet
from apps.core.stock_movement_correction_views import StockMovementCorrectionViewSet
from apps.core.stock_permission_audit_views import StockPermissionAuditViewSet
from apps.core.stock_quarantine_views import ExpiredStockQuarantineViewSet
from apps.core.stock_segregation_views import StockSegregationViewSet
from apps.core.stock_valuation_views import StockValuationViewSet
from apps.core.storage_conditional_views import StorageConditionalViewSet
from apps.core.storage_policy_views import StoragePolicyViewSet
from apps.core.strength_taxonomy_views import StrengthTaxonomyViewSet
from apps.core.subscription_portal_views import SubscriptionCustomerPortalViewSet
from apps.core.subscription_support_views import SubscriptionSupportOverrideViewSet
from apps.core.subscription_views import SubscriptionViewSet
from apps.core.supplier_credit_note_views import SupplierCreditNoteViewSet
from apps.core.supplier_dashboard_views import SupplierDashboardViewSet
from apps.core.supplier_invoice_views import SupplierInvoiceViewSet
from apps.core.supplier_organization_views import SupplierOrganizationViewSet
from apps.core.vendor_payment_views import VendorPaymentViewSet
from apps.core.financial_account_views import FinancialAccountViewSet
from apps.core.payment_method_views import PaymentMethodViewSet
from apps.core.payment_status_views import PaymentStatusViewSet
from apps.core.mobile_customer_due_views import MobileCustomerDueCollectionViewSet
from apps.core.party_import_export_views import PartyImportExportViewSet
from apps.core.party_privacy_views import PartyPrivacyViewSet
from apps.core.party_reconciliation_views import PartyReconciliationViewSet
from apps.core.chart_of_accounts_views import ChartOfAccountsViewSet
from apps.core.double_entry_journal_views import DoubleEntryJournalViewSet
from apps.core.sales_accounting_views import SalesAccountingViewSet
from apps.core.purchase_accounting_views import PurchaseAccountingViewSet
from apps.core.control_account_views import ControlAccountViewSet
from apps.core.expense_category_views import ExpenseCategoryViewSet
from apps.core.operating_expense_views import OperatingExpenseViewSet
from apps.core.recurring_expense_views import RecurringExpenseViewSet
from apps.core.cash_transfer_reconciliation_views import (
    BankReconciliationViewSet,
    CashBankTransferViewSet,
)
from apps.core.payroll_views import PayrollRunViewSet
from apps.core.financial_period_views import FinancialPeriodViewSet
from apps.core.profit_loss_report_views import ProfitLossReportViewSet
from apps.core.cash_flow_overview_views import CashFlowOverviewViewSet
from apps.core.inventory_cost_valuation_views import InventoryCostValuationViewSet
from apps.core.expense_budget_views import ExpenseBudgetViewSet
from apps.core.tax_configuration_views import TaxConfigurationViewSet
from apps.core.accounting_reports_export_views import AccountingReportsExportViewSet
from apps.core.accounting_error_reversal_views import AccountingErrorReversalViewSet
from apps.core.finance_consistency_audit_views import FinanceConsistencyAuditViewSet
from apps.core.device_local_db_schema_views import DeviceLocalDbSchemaViewSet
from apps.core.device_encryption_key_views import DeviceEncryptionKeyViewSet
from apps.core.offline_cache_seeding_views import OfflineCacheSeedingViewSet
from apps.core.durable_outbox_views import DurableOutboxViewSet
from apps.core.cloud_push_protocol_views import CloudPushProtocolViewSet
from apps.core.canonical_ack_views import CanonicalAckViewSet
from apps.core.change_feed_views import ChangeFeedPullViewSet
from apps.core.sync_cursor_checkpoint_views import SyncCursorCheckpointViewSet
from apps.core.connectivity_detection_views import ConnectivityDetectionViewSet
from apps.core.offline_stock_allocation_views import OfflineStockAllocationPolicyViewSet
from apps.core.offline_prescription_policy_views import OfflinePrescriptionPolicyViewSet
from apps.core.offline_subscription_lease_views import OfflineSubscriptionLeaseViewSet
from apps.core.sync_conflict_classification_views import SyncConflictClassificationViewSet
from apps.core.provisional_identifier_views import ProvisionalIdentifierViewSet
from apps.core.supplier_payment_schedule_views import SupplierPaymentScheduleViewSet
from apps.core.supply_chain_alert_views import SupplyChainAlertViewSet
from apps.core.supply_chain_traceability_views import SupplyChainTraceabilityViewSet
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
from apps.core.temperature_monitoring_views import (
    TemperatureLogReadingViewSet,
    TemperatureMonitoringDeviceViewSet,
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
from apps.core.topical_views import TopicalEditorViewSet
from apps.core.trial_policy_views import TrialPolicyViewSet
from apps.core.vendor_price_list_views import VendorProductPriceListViewSet
from apps.core.pharmacist_queue_views import PharmacistReviewQueueViewSet
from apps.core.prescription_upload_views import PrescriptionAttachmentViewSet
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
from apps.core.compliance_views import (
    BatchRecallViewSet,
    BranchDrugLicenseViewSet,
    ComplianceAuditExportViewSet,
    InspectionEvidenceViewSet,
    PcbRegistrationViewSet,
    PharmacyIncidentViewSet,
    RegulatoryReferenceViewSet,
    TemperatureSafetyReviewViewSet,
)
from apps.core.warehouse_location_views import StorageLocationViewSet, WarehouseViewSet
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
router.register("procurement/vendor-payments", VendorPaymentViewSet, basename="procurement-vendor-payments")
router.register("finance/accounts", FinancialAccountViewSet, basename="finance-accounts")
router.register("finance/chart-of-accounts", ChartOfAccountsViewSet, basename="finance-chart-of-accounts")
router.register("finance/journals", DoubleEntryJournalViewSet, basename="finance-journals")
router.register("finance/sales-postings", SalesAccountingViewSet, basename="finance-sales-postings")
router.register("finance/purchase-postings", PurchaseAccountingViewSet, basename="finance-purchase-postings")
router.register("finance/control-accounts", ControlAccountViewSet, basename="finance-control-accounts")
router.register("finance/expense-categories", ExpenseCategoryViewSet, basename="finance-expense-categories")
router.register("finance/expenses", OperatingExpenseViewSet, basename="finance-expenses")
router.register("finance/recurring-expenses", RecurringExpenseViewSet, basename="finance-recurring-expenses")
router.register("finance/treasury/transfers", CashBankTransferViewSet, basename="finance-treasury-transfers")
router.register("finance/treasury/reconciliations", BankReconciliationViewSet, basename="finance-treasury-reconciliations")
router.register("finance/payroll/runs", PayrollRunViewSet, basename="finance-payroll-runs")
router.register("finance/periods", FinancialPeriodViewSet, basename="finance-periods")
router.register("finance/reports/profit-and-loss", ProfitLossReportViewSet, basename="finance-reports-profit-and-loss")
router.register("finance/reports/cash-flow", CashFlowOverviewViewSet, basename="finance-reports-cash-flow")
router.register("finance/inventory-valuation", InventoryCostValuationViewSet, basename="finance-inventory-valuation")
router.register("finance/expense-budgets", ExpenseBudgetViewSet, basename="finance-expense-budgets")
router.register("finance/tax-config", TaxConfigurationViewSet, basename="finance-tax-config")
router.register("finance/accounting-exports", AccountingReportsExportViewSet, basename="finance-accounting-exports")
router.register("finance/journal-reversals", AccountingErrorReversalViewSet, basename="finance-journal-reversals")
router.register("finance/consistency-audit", FinanceConsistencyAuditViewSet, basename="finance-consistency-audit")
router.register("offline-sync/device-schemas", DeviceLocalDbSchemaViewSet, basename="offline-sync-device-schemas")
router.register("offline-sync/device-keys", DeviceEncryptionKeyViewSet, basename="offline-sync-device-keys")
router.register("offline-sync/cache-seeds", OfflineCacheSeedingViewSet, basename="offline-sync-cache-seeds")
router.register("offline-sync/outbox", DurableOutboxViewSet, basename="offline-sync-outbox")
router.register("offline-sync/push", CloudPushProtocolViewSet, basename="offline-sync-push")
router.register("offline-sync/canonical-acks", CanonicalAckViewSet, basename="offline-sync-canonical-acks")
router.register("offline-sync/change-feed", ChangeFeedPullViewSet, basename="offline-sync-change-feed")
router.register("offline-sync/cursor-checkpoints", SyncCursorCheckpointViewSet, basename="offline-sync-cursor-checkpoints")
router.register("offline-sync/connectivity", ConnectivityDetectionViewSet, basename="offline-sync-connectivity")
router.register("offline-sync/stock-allocation-policies", OfflineStockAllocationPolicyViewSet, basename="offline-sync-stock-allocation-policies")
router.register("offline-sync/prescription-policies", OfflinePrescriptionPolicyViewSet, basename="offline-sync-prescription-policies")
router.register("offline-sync/subscription-leases", OfflineSubscriptionLeaseViewSet, basename="offline-sync-subscription-leases")
router.register("offline-sync/conflicts", SyncConflictClassificationViewSet, basename="offline-sync-conflicts")
router.register("offline-sync/provisional-identifiers", ProvisionalIdentifierViewSet, basename="offline-sync-provisional-identifiers")
router.register("finance/payment-methods", PaymentMethodViewSet, basename="finance-payment-methods")
router.register("finance/payment-status", PaymentStatusViewSet, basename="finance-payment-status")
router.register("mobile/due-collections", MobileCustomerDueCollectionViewSet, basename="mobile-due-collections")
router.register("parties/reconciliation", PartyReconciliationViewSet, basename="party-reconciliation")
router.register("parties/privacy", PartyPrivacyViewSet, basename="party-privacy")
router.register("parties", PartyImportExportViewSet, basename="parties")
router.register("procurement/price-lists", VendorProductPriceListViewSet, basename="procurement-price-lists")
router.register("procurement/requisitions", PurchaseRequisitionViewSet, basename="procurement-requisitions")
router.register("procurement/orders", PurchaseOrderViewSet, basename="procurement-orders")
router.register("procurement/goods-receipts", GoodsReceiptViewSet, basename="procurement-goods-receipts")
router.register("procurement/supplier-invoices", SupplierInvoiceViewSet, basename="procurement-supplier-invoices")
router.register("procurement/purchase-returns", PurchaseReturnViewSet, basename="procurement-purchase-returns")
router.register("procurement/supplier-credit-notes", SupplierCreditNoteViewSet, basename="procurement-supplier-credit-notes")
router.register("procurement/payment-schedules", SupplierPaymentScheduleViewSet, basename="procurement-payment-schedules")
router.register("procurement/reorder-plans", AutoReorderPlannerViewSet, basename="procurement-reorder-plans")
router.register("procurement/supplier-comparison", MultiSupplierComparisonViewSet, basename="procurement-supplier-comparison")
router.register("procurement/shipments", DeliveryLogisticsViewSet, basename="procurement-shipments")
router.register("procurement/traceability", SupplyChainTraceabilityViewSet, basename="procurement-traceability")
router.register("procurement/documents", PurchaseDocumentViewSet, basename="procurement-documents")
router.register("procurement/supplier-dashboard", SupplierDashboardViewSet, basename="procurement-supplier-dashboard")
router.register("inventory/stock-ledger", StockLedgerViewSet, basename="inventory-stock-ledger")
router.register("inventory/warehouses", WarehouseViewSet, basename="inventory-warehouses")
router.register("inventory/locations", StorageLocationViewSet, basename="inventory-locations")
router.register("inventory/stock-inquiry", StockInquiryViewSet, basename="inventory-stock-inquiry")
router.register("inventory/count-sessions", InventoryCountSessionViewSet, basename="inventory-count-sessions")
router.register("inventory/adjustments", StockAdjustmentViewSet, basename="inventory-adjustments")
router.register("inventory/quarantines", ExpiredStockQuarantineViewSet, basename="inventory-quarantines")
router.register("inventory/segregations", StockSegregationViewSet, basename="inventory-segregations")
router.register("inventory/transfers", InterBranchTransferViewSet, basename="inventory-transfers")
router.register("inventory/cold-chain/devices", TemperatureMonitoringDeviceViewSet, basename="cold-chain-devices")
router.register("inventory/cold-chain/temperature-logs", TemperatureLogReadingViewSet, basename="cold-chain-temperature-logs")
router.register("inventory/cold-chain/excursions", ColdChainExcursionViewSet, basename="cold-chain-excursions")
router.register("inventory/storage-policies", StoragePolicyViewSet, basename="inventory-storage-policies")
router.register("inventory/alerts", SupplyChainAlertViewSet, basename="inventory-alerts")
router.register("inventory/snapshots", InventorySnapshotViewSet, basename="inventory-snapshots")
router.register("inventory/valuation-reports", InventoryValuationReportViewSet, basename="inventory-valuation-reports")
router.register("inventory/movement-corrections", StockMovementCorrectionViewSet, basename="inventory-movement-corrections")
router.register("inventory/barcode", BarcodeInventoryViewSet, basename="inventory-barcode")
router.register("inventory/permissions", StockPermissionAuditViewSet, basename="inventory-permissions")
router.register("inventory/qa-stress", InventoryFaultStressViewSet, basename="inventory-qa-stress")
router.register("pos/search", PosFastSearchViewSet, basename="pos-search")
router.register("pos/customers", PosCustomerViewSet, basename="pos-customers")
router.register("pos/credit-eligibility", PosCreditEligibilityViewSet, basename="pos-credit-eligibility")
router.register("pos/receivable-ledger", PosCustomerReceivableLedgerViewSet, basename="pos-receivable-ledger")
router.register("pos/customer-dues", PosCustomerDueViewSet, basename="pos-customer-dues")
router.register("pos/partial-collections", PosPartialCollectionViewSet, basename="pos-partial-collections")
router.register("pos/statements", PosCustomerStatementViewSet, basename="pos-statements")
router.register("pos/invoice-policies", PosInvoiceNumberPolicyViewSet, basename="pos-invoice-policies")
router.register("pos/shifts", PosCashierShiftViewSet, basename="pos-shifts")
router.register("pos/daily-close", PosDailyCloseViewSet, basename="pos-daily-close")
router.register("pos/returns", PosSaleReturnViewSet, basename="pos-returns")
router.register("pos/receipt-logs", PosReceiptPrintLogViewSet, basename="pos-receipt-logs")
router.register("pos/error-incidents", PosErrorIncidentViewSet, basename="pos-error-incidents")
router.register("pos/e2e-qa", PosSalesE2EQaViewSet, basename="pos-e2e-qa")
router.register("pos/sales", PosSaleOrderViewSet, basename="pos-sales")
router.register("pos/deliveries", PosDeliveryOrderViewSet, basename="pos-deliveries")
router.register("pos/antibiotic-register", PosAntibioticRegisterViewSet, basename="pos-antibiotic-register")
router.register("pos/payment-attempts", PosPaymentAttemptViewSet, basename="pos-payment-attempts")
router.register("pos/allocations", PosSaleAllocationViewSet, basename="pos-allocations")
router.register("procurement/qa", ProcurementE2EQaViewSet, basename="procurement-qa")
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
router.register("prescriptions/review-queue", PharmacistReviewQueueViewSet, basename="pharmacist-review-queue")
router.register("prescriptions", PrescriptionRecordViewSet, basename="prescription")
router.register("prescription-attachments", PrescriptionAttachmentViewSet, basename="prescription-attachment")
router.register("compliance/drug-licenses", BranchDrugLicenseViewSet, basename="branch-drug-licenses")
router.register("compliance/pcb-registrations", PcbRegistrationViewSet, basename="pcb-registrations")
router.register("compliance/inspection-evidence", InspectionEvidenceViewSet, basename="inspection-evidence")
router.register("compliance/incidents", PharmacyIncidentViewSet, basename="pharmacy-incidents")
router.register("compliance/recalls", BatchRecallViewSet, basename="batch-recalls")
router.register("compliance/temperature-reviews", TemperatureSafetyReviewViewSet, basename="temperature-safety-reviews")
router.register("compliance/audit-exports", ComplianceAuditExportViewSet, basename="compliance-audit-exports")
router.register("compliance/regulatory-references", RegulatoryReferenceViewSet, basename="compliance-regulatory-references")
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
