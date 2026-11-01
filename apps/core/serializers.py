"""
apps/core/serializers.py
Architecture Decision: ADR-0004, ADR-0025 & ADR-0031
DRF Serializers for Multi-Tenant Pharmacy SaaS Core Models.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from rest_framework import serializers

from apps.core.models import (
    AnnouncementReadReceipt,
    AsyncJobRecord,
    BatchStatus,
    BatchStock,
    Branch,
    ComplianceReminder,
    CrossTenantMetricsSnapshot,
    DailyBranchAnalytics,
    InitialInventoryMode,
    MedicineProduct,
    OnboardingWizardStatus,
    PaymentMethod,
    PaymentTransaction,
    PlanCatalogueRecord,
    PlatformApiAccessPolicy,
    PlatformFeatureFlag,
    PrescriptionItem,
    PrescriptionRecord,
    SaleInvoice,
    SaleInvoiceItem,
    StaffMember,
    SubscriptionRecord,
    SystemAnnouncement,
    Tenant,
    TenantAppliedEntitlement,
    TenantDataExportRequest,
    TenantDeletionRequest,
    TenantFeatureFlagOverride,
    TenantMedicineConfig,
    TenantOnboardingSession,
)
from apps.core.validators import (
    validate_bangladesh_phone_number,
    validate_cold_chain_temps,
    validate_date_not_future,
    validate_date_not_past,
    validate_discount_bounds,
    validate_enum_choice,
    validate_money_decimal,
    validate_mrp_above_cost,
    validate_positive_quantity,
)


class TenantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = [
            "tenant_id",
            "name",
            "subdomain",
            "plan_tier",
            "status",
            "owner_user_id",
            "billing_customer_id",
            "billing_email",
            "trial_ends_at",
            "grace_period_ends_at",
            "suspended_at",
            "suspension_reason",
            "canonical_settings",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at", "suspended_at"]

    def validate_canonical_settings(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("canonical_settings must be an object.")
        if value.get("allow_negative_stock") is True:
            raise serializers.ValidationError(
                "Clinical safety invariant violation: allow_negative_stock must be false."
            )
        return value

    def update(self, instance, validated_data):
        if "tenant_id" in validated_data and validated_data["tenant_id"] != instance.tenant_id:
            raise serializers.ValidationError({"tenant_id": ["tenant_id is strictly immutable."]})
        return super().update(instance, validated_data)


class BranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = ["branch_id", "tenant", "name", "license_number", "address", "is_active", "created_at"]
        read_only_fields = ["created_at"]


class MedicineProductSerializer(serializers.ModelSerializer):
    status_chip = serializers.SerializerMethodField()
    tenant_override = serializers.SerializerMethodField()

    class Meta:
        model = MedicineProduct
        fields = [
            "product_id",
            "brand_name",
            "generic_name",
            "dosage_form",
            "strength",
            "manufacturer",
            "mrp_unit",
            "cost_unit",
            "requires_cold_chain",
            "min_temp_celsius",
            "max_temp_celsius",
            "dgda_dar_number",
            "regulatory_source",
            "curation_status",
            "version_number",
            "immutable_version_hash",
            "is_prescription_only",
            "is_narcotic",
            "is_antibiotic",
            "status_chip",
            "tenant_override",
            "reviewed_at",
            "curation_notes",
            "created_at",
        ]
        read_only_fields = [
            "created_at",
            "version_number",
            "immutable_version_hash",
            "status_chip",
            "tenant_override",
            "reviewed_at",
        ]

    def get_status_chip(self, obj):
        from apps.core.global_medicine_service import GlobalMedicineCatalogService

        return GlobalMedicineCatalogService.resolve_status_chip(obj.curation_status)

    def get_tenant_override(self, obj):
        request = self.context.get("request")
        if not request:
            return None
        user = getattr(request, "user", None)
        tenant_id = (
            getattr(request, "tenant_id", None)
            or getattr(user, "tenant_id", None)
            or (
                request.query_params.get("tenant_id")
                if getattr(user, "is_superuser", False)
                or str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]
                else None
            )
        )
        if not tenant_id or str(tenant_id).strip() in ["", "platform-global"]:
            return None

        from apps.core.models import TenantMedicineConfig

        config = TenantMedicineConfig.objects.filter(tenant_id=str(tenant_id).strip(), product=obj).first()
        if not config:
            return None
        from apps.core.tenant_medicine_config_service import TenantMedicineConfigService

        return TenantMedicineConfigService.serialize_config(config)

    def validate_mrp_unit(self, value):
        return validate_money_decimal(value, "mrp_unit", min_value=Decimal("0.01"))

    def validate_cost_unit(self, value):
        return validate_money_decimal(value, "cost_unit", min_value=Decimal("0.00"))

    def validate(self, attrs):
        mrp = attrs.get("mrp_unit")
        cost = attrs.get("cost_unit")
        if mrp is not None and cost is not None:
            validate_mrp_above_cost(mrp, cost)

        requires_cold = attrs.get("requires_cold_chain", False)
        min_temp = attrs.get("min_temp_celsius")
        max_temp = attrs.get("max_temp_celsius")
        min_d, max_d = validate_cold_chain_temps(requires_cold, min_temp, max_temp)
        attrs["min_temp_celsius"] = min_d
        attrs["max_temp_celsius"] = max_d
        return attrs


class TenantMedicineConfigSerializer(serializers.ModelSerializer):
    effective_brand_name = serializers.CharField(read_only=True)
    effective_mrp_unit = serializers.CharField(read_only=True)
    effective_cost_unit = serializers.CharField(read_only=True)
    effective_requires_prescription = serializers.BooleanField(read_only=True)
    product_details = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = TenantMedicineConfig
        fields = [
            "config_id",
            "tenant",
            "product",
            "local_brand_name",
            "local_sku",
            "custom_mrp_unit",
            "custom_cost_unit",
            "is_visible",
            "shelf_location",
            "low_stock_threshold",
            "requires_prescription_override",
            "notes",
            "effective_brand_name",
            "effective_mrp_unit",
            "effective_cost_unit",
            "effective_requires_prescription",
            "product_details",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "config_id",
            "effective_brand_name",
            "effective_mrp_unit",
            "effective_cost_unit",
            "effective_requires_prescription",
            "product_details",
            "created_at",
            "updated_at",
        ]

    def get_product_details(self, obj):
        if not obj.product:
            return None
        return {
            "product_id": obj.product.product_id,
            "master_brand_name": obj.product.brand_name,
            "generic_name": obj.product.generic_name,
            "dosage_form": obj.product.dosage_form,
            "strength": obj.product.strength,
            "manufacturer": obj.product.manufacturer,
            "master_mrp_unit": str(obj.product.mrp_unit),
            "dgda_dar_number": obj.product.dgda_dar_number,
            "is_prescription_only": obj.product.is_prescription_only,
        }

    def validate(self, attrs):
        product = attrs.get("product") or (self.instance.product if self.instance else None)
        req_rx_override = attrs.get("requires_prescription_override")
        if req_rx_override is False and product:
            if product.is_prescription_only or product.is_narcotic or product.is_antibiotic:
                raise serializers.ValidationError(
                    {
                        "requires_prescription_override": (
                            "Cannot relax DGDA regulatory prescription requirement. "
                            "Global master product is prescription-only (POM)."
                        )
                    }
                )
        custom_mrp = attrs.get("custom_mrp_unit")
        if custom_mrp is not None and custom_mrp < Decimal("0.01"):
            raise serializers.ValidationError({"custom_mrp_unit": "Custom MRP must be at least 0.01 BDT."})
        return attrs


class BatchStockSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.brand_name", read_only=True)
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = BatchStock
        fields = [
            "batch_id",
            "tenant",
            "branch",
            "product",
            "product_name",
            "batch_number",
            "quantity_base_units",
            "expiry_date",
            "purchase_date",
            "cost_per_unit",
            "status",
            "is_expired",
        ]

    def validate_cost_per_unit(self, value):
        return validate_money_decimal(value, "cost_per_unit", min_value=Decimal("0.00"))

    def validate_quantity_base_units(self, value):
        if value < 0:
            raise serializers.ValidationError("Quantity cannot be negative.")
        return value

    def validate_expiry_date(self, value):
        return validate_date_not_past(value, "expiry_date")

    def validate_purchase_date(self, value):
        return validate_date_not_future(value, "purchase_date")

    def validate(self, attrs):
        tenant = attrs.get("tenant")
        branch = attrs.get("branch")
        if tenant and branch and branch.tenant_id != tenant.tenant_id:
            raise serializers.ValidationError(
                {"branch": f"Branch '{branch.branch_id}' does not belong to tenant '{tenant.tenant_id}'."}
            )

        purch = attrs.get("purchase_date")
        exp = attrs.get("expiry_date")
        if purch and exp and purch > exp:
            raise serializers.ValidationError(
                {"purchase_date": f"Purchase date '{purch}' cannot be later than expiry date '{exp}'."}
            )
        return attrs


class SaleInvoiceItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.brand_name", read_only=True)

    class Meta:
        model = SaleInvoiceItem
        fields = ["id", "invoice", "batch", "product", "product_name", "quantity", "unit_price", "subtotal"]
        read_only_fields = ["id"]


class SaleInvoiceSerializer(serializers.ModelSerializer):
    items = SaleInvoiceItemSerializer(many=True, read_only=True)

    class Meta:
        model = SaleInvoice
        fields = [
            "invoice_id",
            "tenant",
            "branch",
            "invoice_number",
            "customer_name",
            "customer_phone",
            "total_amount",
            "tax_amount",
            "discount_amount",
            "net_amount",
            "payment_method",
            "status",
            "created_at",
            "items",
        ]
        read_only_fields = ["created_at"]


class AsyncJobRecordSerializer(serializers.ModelSerializer):
    """Serializer for background worker task auditing (ADR-0031)."""

    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)

    class Meta:
        model = AsyncJobRecord
        fields = [
            "job_id",
            "tenant",
            "tenant_name",
            "branch",
            "branch_name",
            "category",
            "task_name",
            "status",
            "payload",
            "result",
            "error_message",
            "retry_count",
            "max_retries",
            "scheduled_at",
            "completed_at",
        ]
        read_only_fields = ["job_id", "scheduled_at", "completed_at"]


class DailyBranchAnalyticsSerializer(serializers.ModelSerializer):
    """Serializer for derived daily analytics and mobile manager dashboards."""

    branch_name = serializers.CharField(source="branch.name", read_only=True)

    class Meta:
        model = DailyBranchAnalytics
        fields = [
            "analytics_id",
            "tenant",
            "branch",
            "branch_name",
            "date",
            "total_sales_amount",
            "total_orders_count",
            "total_items_sold",
            "last_aggregated_at",
        ]
        read_only_fields = ["analytics_id", "last_aggregated_at"]


class ComplianceReminderSerializer(serializers.ModelSerializer):
    """Serializer for regulatory license and batch quarantine reminders."""

    branch_name = serializers.CharField(source="branch.name", read_only=True)

    class Meta:
        model = ComplianceReminder
        fields = [
            "reminder_id",
            "tenant",
            "branch",
            "branch_name",
            "reminder_type",
            "target_date",
            "message_en",
            "message_bn",
            "is_acknowledged",
            "acknowledged_at",
            "created_at",
        ]
        read_only_fields = ["reminder_id", "created_at"]


class SaleCheckoutItemInputSerializer(serializers.Serializer):
    batch_id = serializers.CharField(max_length=64)
    quantity = serializers.IntegerField()

    def validate_quantity(self, value):
        return validate_positive_quantity(value, "quantity")


class SaleCheckoutRequestSerializer(serializers.Serializer):
    tenant_id = serializers.CharField(max_length=64)
    branch_id = serializers.CharField(max_length=64)
    customer_name = serializers.CharField(max_length=255, required=False, default="Walk-in Customer")
    customer_phone = serializers.CharField(max_length=32, required=False, default="", allow_blank=True)
    payment_method = serializers.CharField(max_length=32, required=False, default="CASH")
    discount_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default="0.00")
    items = SaleCheckoutItemInputSerializer(many=True)

    def validate_customer_phone(self, value):
        if value and str(value).strip():
            return validate_bangladesh_phone_number(value)
        return ""

    def validate_payment_method(self, value):
        return validate_enum_choice(value, PaymentMethod, "payment_method")

    def validate_discount_amount(self, value):
        return validate_money_decimal(value, "discount_amount", min_value=Decimal("0.00"))

    def validate(self, attrs):
        if not attrs.get("items"):
            raise serializers.ValidationError({"items": ["Checkout cannot proceed with an empty cart."]})

        tenant_id = attrs["tenant_id"]
        branch_id = attrs["branch_id"]

        tenant = Tenant.objects.filter(tenant_id=tenant_id, is_active=True).first()
        if not tenant:
            raise serializers.ValidationError({"tenant_id": [f"Tenant '{tenant_id}' is inactive or does not exist."]})

        branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant, is_active=True).first()
        if not branch:
            raise serializers.ValidationError(
                {"branch_id": [f"Branch '{branch_id}' does not belong to tenant '{tenant_id}' or is inactive."]}
            )

        total_gross = Decimal("0.00")
        for item in attrs["items"]:
            b_id = item["batch_id"]
            qty = item["quantity"]
            batch = BatchStock.objects.filter(batch_id=b_id).first()
            if not batch:
                raise serializers.ValidationError({"items": [f"Batch '{b_id}' does not exist."]})

            if batch.tenant_id != tenant_id or batch.branch_id != branch_id:
                raise serializers.ValidationError(
                    {
                        "items": [
                            f"Cross-tenant violation: Batch '{b_id}' belongs to tenant '{batch.tenant_id}', not '{tenant_id}'."
                        ]
                    }
                )

            if batch.is_expired or batch.status == BatchStatus.EXPIRED:
                raise serializers.ValidationError(
                    {
                        "items": [
                            f"DGDA compliance violation: Batch '{batch.batch_number}' expired on {batch.expiry_date}."
                        ]
                    }
                )

            if batch.quantity_base_units < qty:
                raise serializers.ValidationError(
                    {
                        "items": [
                            f"Insufficient stock for batch '{batch.batch_number}'. Available: {batch.quantity_base_units}, Requested: {qty}."
                        ]
                    }
                )

            total_gross += batch.product.mrp_unit * Decimal(qty)

        disc = attrs.get("discount_amount", Decimal("0.00"))
        validate_discount_bounds(total_gross, disc)
        return attrs


class CommandValidationSerializer(serializers.Serializer):
    """
    Validates domain command payloads conforming to input_validation_command.schema.json (ADR-0046).
    """

    command_type = serializers.ChoiceField(
        choices=["CREATE_MEDICINE", "ADD_BATCH", "CHECKOUT_INVOICE", "DISPENSE_PRESCRIPTION"]
    )
    tenant_id = serializers.CharField(max_length=64)
    branch_id = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    payload = serializers.DictField()
    client_platform = serializers.ChoiceField(
        choices=["DESKTOP_POS", "MOBILE_MANAGER", "WEB_ADMIN"],
        required=False,
        default="DESKTOP_POS",
    )
    timestamp = serializers.DateTimeField(required=False)

    def validate_command(self) -> dict[str, Any]:
        """
        Executes deep command-specific validation against the payload.
        """
        cmd_type = self.validated_data["command_type"]
        raw_payload = dict(self.validated_data["payload"])
        tenant_id = self.validated_data["tenant_id"]

        if cmd_type == "CREATE_MEDICINE":
            med_ser = MedicineProductSerializer(data=raw_payload)
            med_ser.is_valid(raise_exception=True)
            return med_ser.validated_data
        elif cmd_type == "ADD_BATCH":
            raw_payload.setdefault("tenant", tenant_id)
            if self.validated_data.get("branch_id"):
                raw_payload.setdefault("branch", self.validated_data["branch_id"])
            batch_ser = BatchStockSerializer(data=raw_payload)
            batch_ser.is_valid(raise_exception=True)
            return batch_ser.validated_data
        elif cmd_type == "CHECKOUT_INVOICE":
            raw_payload.setdefault("tenant_id", tenant_id)
            if self.validated_data.get("branch_id"):
                raw_payload.setdefault("branch_id", self.validated_data["branch_id"])
            checkout_ser = SaleCheckoutRequestSerializer(data=raw_payload)
            checkout_ser.is_valid(raise_exception=True)
            return checkout_ser.validated_data

        return raw_payload


class SubscriptionRecordSerializer(serializers.ModelSerializer):
    """Serializer for tenant SaaS subscriptions and license entitlements (ADR-0015 & ADR-0033)."""

    tenant_name = serializers.CharField(source="tenant.name", read_only=True)

    class Meta:
        model = SubscriptionRecord
        fields = [
            "subscription_id",
            "tenant",
            "tenant_name",
            "plan_tier",
            "status",
            "amount_bdt",
            "billing_cycle",
            "licensed_branches",
            "licensed_devices",
            "features_enabled",
            "start_date",
            "renewal_date",
            "grace_period_end_date",
            "is_auto_renew",
            "created_at",
        ]
        read_only_fields = ["created_at"]


class StaffMemberSerializer(serializers.ModelSerializer):
    """Serializer for tenant staff members, cashier accounts, and pharmacist PCB profiles."""

    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True)

    class Meta:
        model = StaffMember
        fields = [
            "staff_id",
            "tenant",
            "tenant_name",
            "branch",
            "branch_name",
            "username",
            "full_name",
            "role",
            "mobile_number",
            "pcb_registration_no",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["created_at"]


class PrescriptionItemSerializer(serializers.ModelSerializer):
    """Serializer for individual prescribed drug line items."""

    product_brand = serializers.CharField(source="product.brand_name", read_only=True)
    product_generic = serializers.CharField(source="product.generic_name", read_only=True)

    class Meta:
        model = PrescriptionItem
        fields = [
            "id",
            "prescription",
            "product",
            "product_brand",
            "product_generic",
            "dosage_instructions",
            "duration_days",
            "quantity_prescribed",
            "is_dispensed",
        ]


class PrescriptionRecordSerializer(serializers.ModelSerializer):
    """Serializer for clinical prescriptions, BMDC doctor details, and pharmacist verification."""

    items = PrescriptionItemSerializer(many=True, read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    verified_by_name = serializers.CharField(source="verified_by.full_name", read_only=True)

    class Meta:
        model = PrescriptionRecord
        fields = [
            "prescription_id",
            "tenant",
            "branch",
            "branch_name",
            "doctor_name",
            "doctor_bmdc_no",
            "patient_token",
            "patient_age",
            "patient_gender",
            "diagnosis",
            "status",
            "verified_by",
            "verified_by_name",
            "pharmacist_pcb_no",
            "notes",
            "prescription_date",
            "verified_at",
            "created_at",
            "items",
        ]
        read_only_fields = ["created_at", "verified_at"]


class PrescriptionVerificationRequestSerializer(serializers.Serializer):
    """Payload for pharmacist clinical sign-off (ADR-0010, ADR-0033)."""

    pharmacist_user_id = serializers.CharField(max_length=64)
    pharmacist_pcb_reg_no = serializers.RegexField(
        regex=r"^PCB-[A-Z0-9]{4,10}$",
        error_messages={"invalid": "Invalid PCB registration format (Expected PCB-XXXXX)."},
    )
    decision = serializers.ChoiceField(choices=["APPROVED", "REJECTED"])
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class PaymentTransactionSerializer(serializers.ModelSerializer):
    """Serializer for POS tender settlements and gateway payment transactions."""

    branch_name = serializers.CharField(source="branch.name", read_only=True)
    initiated_by_name = serializers.CharField(source="initiated_by.full_name", read_only=True)

    class Meta:
        model = PaymentTransaction
        fields = [
            "transaction_id",
            "tenant",
            "branch",
            "branch_name",
            "invoice",
            "tender_type",
            "gateway_reference",
            "amount_bdt",
            "status",
            "initiated_by",
            "initiated_by_name",
            "created_at",
        ]
        read_only_fields = ["created_at"]


class PlatformFeatureFlagSerializer(serializers.ModelSerializer):
    overrides_count = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = PlatformFeatureFlag
        fields = [
            "flag_key",
            "name",
            "description",
            "is_enabled_globally",
            "default_enabled",
            "rollout_percentage",
            "min_plan_tier",
            "is_kill_switch_active",
            "kill_switch_reason",
            "kill_switch_tripped_at",
            "overrides_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at", "kill_switch_tripped_at", "overrides_count"]

    def get_overrides_count(self, obj):
        return obj.tenant_overrides.count()

    def validate_rollout_percentage(self, value):
        if value < 0 or value > 100:
            raise serializers.ValidationError("Rollout percentage must be an integer between 0 and 100.")
        return value


class TenantFeatureFlagOverrideSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenantFeatureFlagOverride
        fields = [
            "override_id",
            "flag",
            "tenant",
            "is_enabled",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["override_id", "created_at", "updated_at"]


class TenantDataExportRequestSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField(read_only=True)
    tenant_id = serializers.CharField(source="tenant.tenant_id", read_only=True)

    class Meta:
        model = TenantDataExportRequest
        fields = [
            "export_id",
            "tenant_id",
            "status",
            "requested_by",
            "approved_by",
            "rejection_reason",
            "include_patient_pii",
            "include_financials",
            "include_prescriptions",
            "include_inventory",
            "file_format",
            "sha256_checksum",
            "file_size_bytes",
            "download_url",
            "download_count",
            "max_downloads",
            "expires_at",
            "requested_at",
            "approved_at",
            "completed_at",
        ]
        read_only_fields = [
            "export_id",
            "tenant_id",
            "status",
            "requested_by",
            "approved_by",
            "rejection_reason",
            "sha256_checksum",
            "file_size_bytes",
            "download_url",
            "download_count",
            "max_downloads",
            "expires_at",
            "requested_at",
            "approved_at",
            "completed_at",
        ]

    def get_download_url(self, obj) -> str | None:
        if obj.status == "COMPLETED" and obj.download_token:
            return f"/api/v2/tenants/data-exports/{obj.export_id}/download/?token={obj.download_token}"
        return None


class TenantDeletionRequestSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source="tenant.tenant_id", read_only=True)

    class Meta:
        model = TenantDeletionRequest
        fields = [
            "request_id",
            "tenant_id",
            "status",
            "reason",
            "has_active_holds",
            "active_holds_summary",
            "hold_statutory_horizon_days",
            "confirmation_code",
            "confirmation_text",
            "rejection_reason",
            "requested_by",
            "assessed_by",
            "created_at",
            "assessed_at",
            "confirmed_at",
            "purged_at",
        ]
        read_only_fields = [
            "request_id",
            "tenant_id",
            "status",
            "has_active_holds",
            "active_holds_summary",
            "hold_statutory_horizon_days",
            "confirmation_code",
            "confirmation_text",
            "rejection_reason",
            "requested_by",
            "assessed_by",
            "created_at",
            "assessed_at",
            "confirmed_at",
            "purged_at",
        ]


class CrossTenantMetricsSnapshotSerializer(serializers.ModelSerializer):
    """
    Architecture Decision: ADR-0075
    Serializer for cross-tenant aggregated telemetry snapshots.
    """

    generated_by = serializers.CharField(source="generated_by.username", read_only=True)

    class Meta:
        model = CrossTenantMetricsSnapshot
        fields = [
            "snapshot_id",
            "metric_period",
            "start_date",
            "end_date",
            "status",
            "total_active_tenants",
            "contributing_tenants_count",
            "min_cohort_threshold",
            "privacy_filter_applied",
            "clinical_metrics",
            "financial_metrics",
            "operational_metrics",
            "cohort_breakdowns",
            "generated_by",
            "published_at",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "snapshot_id",
            "status",
            "total_active_tenants",
            "contributing_tenants_count",
            "min_cohort_threshold",
            "privacy_filter_applied",
            "clinical_metrics",
            "financial_metrics",
            "operational_metrics",
            "cohort_breakdowns",
            "generated_by",
            "published_at",
            "created_at",
            "updated_at",
        ]


class SystemAnnouncementSerializer(serializers.ModelSerializer):
    """
    Architecture Decision: ADR-0076
    Serializer for platform broadcast bulletins and targeting rules.
    """

    created_by = serializers.CharField(source="created_by.username", read_only=True)

    class Meta:
        model = SystemAnnouncement
        fields = [
            "announcement_id",
            "title",
            "message",
            "priority",
            "target_type",
            "target_tenants",
            "target_roles",
            "target_branches",
            "starts_at",
            "expires_at",
            "status",
            "created_by",
            "published_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "announcement_id",
            "status",
            "created_by",
            "published_at",
            "created_at",
            "updated_at",
        ]


class AnnouncementReadReceiptSerializer(serializers.ModelSerializer):
    """
    Architecture Decision: ADR-0076
    Serializer for announcement read receipts.
    """

    user_name = serializers.CharField(source="user.full_name", read_only=True)

    class Meta:
        model = AnnouncementReadReceipt
        fields = [
            "receipt_id",
            "announcement",
            "tenant",
            "user",
            "user_name",
            "delivered_at",
            "read_at",
            "acknowledged",
        ]
        read_only_fields = [
            "receipt_id",
            "announcement",
            "tenant",
            "user",
            "delivered_at",
            "read_at",
            "acknowledged",
        ]


class PlatformApiAccessPolicySerializer(serializers.ModelSerializer):
    """
    Architecture Decision: ADR-0077
    Serializer for Platform API Access Policies.
    """

    class Meta:
        model = PlatformApiAccessPolicy
        fields = [
            "policy_id",
            "name",
            "description",
            "endpoint_pattern",
            "http_method",
            "allowed_roles",
            "require_mfa",
            "require_step_up",
            "enforce_zero_implicit_tenant_access",
            "ip_allowlist",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["policy_id", "created_at", "updated_at"]

    def validate_endpoint_pattern(self, value):
        import re

        if not value or len(value.strip()) < 3:
            raise serializers.ValidationError("Endpoint pattern must be at least 3 characters.")
        try:
            re.compile(value)
        except re.error as e:
            raise serializers.ValidationError(f"Invalid regex pattern: {e}")
        return value.strip()

    def validate_name(self, value):
        if not value or len(value.strip()) < 3:
            raise serializers.ValidationError("Policy name must be at least 3 characters.")
        return value.strip()


class PlatformPolicyEvaluationRequestSerializer(serializers.Serializer):
    """
    Serializer for probing / evaluating access against policies.
    """

    path = serializers.CharField(max_length=255)
    method = serializers.CharField(max_length=10, default="GET")
    client_ip = serializers.CharField(max_length=45, required=False, allow_blank=True)


class StepUpVerificationRequestSerializer(serializers.Serializer):
    """
    Serializer for submitting a TOTP verification code for step-up challenge.
    """

    code = serializers.CharField(min_length=6, max_length=6)


class TenantOnboardingSessionSerializer(serializers.ModelSerializer):
    """
    Architecture Decision: ADR-0079
    Serializer for progressive and full Tenant Onboarding Wizard Session.
    """

    class Meta:
        model = TenantOnboardingSession
        fields = [
            "session_id",
            "pharmacy_name",
            "trade_license_number",
            "drug_license_number",
            "tin_number",
            "subdomain",
            "contact_email",
            "contact_phone",
            "owner_full_name",
            "branch_name",
            "branch_address",
            "initial_staff_data",
            "initial_inventory_mode",
            "initial_inventory_data",
            "plan_tier",
            "billing_cycle",
            "is_trial",
            "payment_method",
            "current_step",
            "status",
            "tenant",
            "completed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "session_id",
            "status",
            "tenant",
            "completed_at",
            "created_at",
            "updated_at",
        ]

    def validate_subdomain(self, value: str) -> str:
        from apps.core.provisioning import RESERVED_SUBDOMAINS, SUBDOMAIN_REGEX

        clean = str(value or "").strip().lower()
        if not clean:
            raise serializers.ValidationError("Subdomain is required.")
        if clean in RESERVED_SUBDOMAINS:
            raise serializers.ValidationError(f"Subdomain '{clean}' is a reserved platform name.")
        if not SUBDOMAIN_REGEX.match(clean):
            raise serializers.ValidationError(
                "Invalid subdomain format. Must be 3-64 characters, lowercase alphanumeric and hyphens."
            )
        if Tenant.objects.filter(subdomain=clean).exists():
            raise serializers.ValidationError(f"Subdomain '{clean}' is already registered to another pharmacy.")
        session_id = self.instance.session_id if self.instance else None
        existing = TenantOnboardingSession.objects.filter(
            subdomain=clean,
            status__in=[OnboardingWizardStatus.DRAFT, OnboardingWizardStatus.IN_PROGRESS],
        )
        if session_id:
            existing = existing.exclude(session_id=session_id)
        if existing.exists():
            raise serializers.ValidationError(
                f"Subdomain '{clean}' is already reserved in another active onboarding session."
            )
        return clean

    def validate_contact_phone(self, value: str) -> str:
        from django.core.exceptions import ValidationError as DjangoValidationError

        from apps.identity.models import normalize_bangladesh_phone

        try:
            return normalize_bangladesh_phone(value)
        except DjangoValidationError as e:
            raise serializers.ValidationError(list(e.messages))

    def validate_drug_license_number(self, value: str) -> str:
        clean = str(value or "").strip()
        if not clean or len(clean) < 3:
            raise serializers.ValidationError("Valid DGDA Drug License number is mandatory for pharmacy onboarding.")
        return clean


class TenantOnboardingStepUpdateSerializer(serializers.Serializer):
    """
    Serializer for updating individual steps in the onboarding wizard.
    """

    step = serializers.IntegerField(min_value=1, max_value=6)
    pharmacy_name = serializers.CharField(max_length=255, required=False)
    trade_license_number = serializers.CharField(max_length=128, required=False, allow_blank=True)
    drug_license_number = serializers.CharField(max_length=128, required=False)
    tin_number = serializers.CharField(max_length=64, required=False, allow_blank=True)
    subdomain = serializers.CharField(max_length=64, required=False)
    contact_email = serializers.EmailField(required=False)
    contact_phone = serializers.CharField(max_length=32, required=False)
    owner_full_name = serializers.CharField(max_length=255, required=False)
    branch_name = serializers.CharField(max_length=255, required=False)
    branch_address = serializers.CharField(required=False)
    initial_staff_data = serializers.ListField(child=serializers.DictField(), required=False)
    initial_inventory_mode = serializers.ChoiceField(choices=InitialInventoryMode.choices, required=False)
    initial_inventory_data = serializers.ListField(child=serializers.DictField(), required=False)
    plan_tier = serializers.CharField(max_length=32, required=False)
    billing_cycle = serializers.CharField(max_length=32, required=False)
    is_trial = serializers.BooleanField(required=False)
    payment_method = serializers.CharField(max_length=32, required=False)


class TenantOnboardingCompleteSerializer(serializers.Serializer):
    """
    Serializer for final atomic execution and completion of onboarding wizard.
    """

    owner_password = serializers.CharField(min_length=8, write_only=True)
    operator_notes = serializers.CharField(required=False, allow_blank=True, default="")
    mfa_code = serializers.CharField(required=False, allow_blank=True, default="")


class PlanCatalogueRecordSerializer(serializers.ModelSerializer):
    """Serializer for SaaS Plan Catalogue records (ADR-0015 and ADR-0081)."""

    class Meta:
        model = PlanCatalogueRecord
        fields = [
            "plan_code",
            "tier",
            "name",
            "description_en",
            "description_bn",
            "version",
            "monthly_price_bdt",
            "annual_price_bdt",
            "currency",
            "supported_billing_cycles",
            "max_branches",
            "max_devices_per_branch",
            "max_total_devices",
            "max_staff_users",
            "features",
            "is_active",
            "created_at",
            "updated_at",
        ]


class TenantAppliedEntitlementSerializer(serializers.ModelSerializer):
    """Serializer for immutable Tenant Applied Entitlement snapshots (ADR-0081)."""

    tenant_name = serializers.CharField(source="tenant.name", read_only=True)

    class Meta:
        model = TenantAppliedEntitlement
        fields = [
            "entitlement_id",
            "tenant",
            "tenant_name",
            "plan_code",
            "plan_tier",
            "plan_version",
            "billing_cycle",
            "unit_price_bdt",
            "currency",
            "effective_from",
            "effective_until",
            "max_branches",
            "max_devices_per_branch",
            "max_total_devices",
            "max_staff_users",
            "features_snapshot",
            "is_active",
            "applied_by",
            "notes",
            "created_at",
        ]


class ApplyPlanRequestSerializer(serializers.Serializer):
    """Input serializer for applying a catalogue plan to a tenant."""

    plan_code = serializers.CharField(max_length=32)
    billing_cycle = serializers.ChoiceField(choices=["MONTHLY", "ANNUAL"], default="MONTHLY")
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class PlanQuoteRequestSerializer(serializers.Serializer):
    """Input serializer for calculating a subscription plan quote before checkout (ADR-0083)."""

    tenant_id = serializers.CharField(max_length=64)
    plan_code = serializers.CharField(max_length=32)
    billing_cycle = serializers.ChoiceField(choices=["MONTHLY", "ANNUAL"], default="MONTHLY")
