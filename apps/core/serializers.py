"""
apps/core/serializers.py
Architecture Decision: ADR-0004, ADR-0025 & ADR-0031
DRF Serializers for Multi-Tenant Pharmacy SaaS Core Models.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.core.models import (
    AsyncJobRecord,
    BatchStock,
    Branch,
    ComplianceReminder,
    DailyBranchAnalytics,
    MedicineProduct,
    PaymentTransaction,
    PrescriptionItem,
    PrescriptionRecord,
    SaleInvoice,
    SaleInvoiceItem,
    StaffMember,
    SubscriptionRecord,
    Tenant,
)


class TenantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = ["tenant_id", "name", "subdomain", "plan_tier", "is_active", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]


class BranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = ["branch_id", "tenant", "name", "license_number", "address", "is_active", "created_at"]
        read_only_fields = ["created_at"]


class MedicineProductSerializer(serializers.ModelSerializer):
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
            "created_at",
        ]
        read_only_fields = ["created_at"]


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
    quantity = serializers.IntegerField(min_value=1)


class SaleCheckoutRequestSerializer(serializers.Serializer):
    tenant_id = serializers.CharField(max_length=64)
    branch_id = serializers.CharField(max_length=64)
    customer_name = serializers.CharField(max_length=255, required=False, default="Walk-in Customer")
    customer_phone = serializers.CharField(max_length=32, required=False, default="")
    payment_method = serializers.CharField(max_length=32, required=False, default="CASH")
    discount_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default="0.00")
    items = SaleCheckoutItemInputSerializer(many=True)


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
