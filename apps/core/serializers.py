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
    AsyncJobRecord,
    BatchStatus,
    BatchStock,
    Branch,
    ComplianceReminder,
    DailyBranchAnalytics,
    MedicineProduct,
    PaymentMethod,
    PaymentTransaction,
    PrescriptionItem,
    PrescriptionRecord,
    SaleInvoice,
    SaleInvoiceItem,
    StaffMember,
    SubscriptionRecord,
    Tenant,
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
