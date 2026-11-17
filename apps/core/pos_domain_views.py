"""
backend/apps/core/pos_domain_views.py
Architecture Decision: ADR-0221 (P221).
Phase 12: POS, Sales Orders, Checkout & Returns.

DRF Serializers & ViewSets for the POS Domain Model:
- /api/v2/pos/customers/ (Tenant & branch-scoped customer ledger accounts + soft-archive)
- /api/v2/pos/sales/ (POS sale headers, lines, allocations, payment attempts, lifecycle actions)
- /api/v2/pos/payment-attempts/ (Read-only payment attempt audit view)
- /api/v2/pos/allocations/ (Read-only batch/cost-lot allocation view with cashier cost masking)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict

from django.db.models import Q
from django.utils import timezone
from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.antibiotic_register_service import AntibioticRegisterLedgerService
from apps.core.customer_purchase_history_service import CustomerPurchaseHistoryService
from apps.core.models import (
    AntibioticDispenseRegisterEntry,
    Branch,
    CustomerDueCollection,
    CustomerDueRecord,
    DueStatus,
    MedicineProduct,
    PaymentMethod,
    PosAllocationStatus,
    PosCartPricingAudit,
    PosCartUnitSelectionAudit,
    PosCashierCloseReconciliation,
    PosCheckoutAllocationAudit,
    PosCustomer,
    PosCustomerCreditStatus,
    PosDeliveryAudit,
    PosDeliveryFulfillmentState,
    PosDeliveryMethod,
    PosDeliveryOrder,
    PosDiscountType,
    PosHeldCartAudit,
    PosInvoiceNumberPolicy,
    PosInvoiceSequenceResetFrequency,
    PosOrderFulfillmentMode,
    PosPackagingUnit,
    PosCashierShift,
    PosDailyCloseStatus,
    PosDailySalesClose,
    PosShiftCashMovement,
    PosShiftMovementType,
    PosShiftStatus,
    PosPaymentAttempt,
    PosPaymentAttemptStatus,
    PosPaymentJournalEntry,
    PosPrinterStatus,
    PosPrintJobType,
    PosReceiptPrintFormat,
    PosReceiptPrintLog,
    PosRoundingMode,
    PosRxSafetyAudit,
    PosSaleAllocation,
    PosSaleReturn,
    PosSaleReturnLine,
    PosReturnStatus,
    PosRefundMethod,
    PosReturnSafetyDisposition,
    PosSaleLifecycleAudit,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleOrderLine,
    PosSaleSourceChannel,
    PosSyncState,
    PosTaxMode,
)
from apps.core.pos_cart_pricing_service import PosCartPricingService
from apps.core.pos_cart_unit_service import PosCartUnitSelectionService
from apps.core.pos_customer_due_service import PosCustomerDueService
from apps.core.pos_delivery_service import PosDeliveryService
from apps.core.pos_domain_service import PosDomainService, q2
from apps.core.pos_fefo_checkout_service import PosFefoCheckoutService
from apps.core.pos_held_cart_service import PosHeldCartService
from apps.core.pos_payment_service import PosPaymentSplitService
from apps.core.pos_rx_safety_service import PosRxSafetyService
from apps.core.pos_search_cart_service import PosSearchCartService
from apps.core.pos_search_cart_views import PosCartScanEventSerializer


class PosCustomerSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source="primary_branch.name", read_only=True, default="")
    credit_approved_by_email = serializers.CharField(source="credit_approved_by.email", read_only=True, default="")
    available_credit_bdt = serializers.SerializerMethodField()

    class Meta:
        model = PosCustomer
        fields = [
            "customer_id",
            "tenant_id",
            "primary_branch",
            "branch_name",
            "full_name",
            "phone",
            "email",
            "address",
            "loyalty_code",
            "credit_limit_bdt",
            "current_due_bdt",
            "available_credit_bdt",
            "is_credit_approved",
            "credit_status",
            "credit_terms_days",
            "credit_approved_by_email",
            "credit_approved_at",
            "credit_suspended_reason",
            "notes",
            "privacy_preferences",
            "is_active",
            "is_archived",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "customer_id",
            "tenant_id",
            "current_due_bdt",
            "available_credit_bdt",
            "is_credit_approved",
            "credit_approved_by_email",
            "credit_approved_at",
            "credit_suspended_reason",
            "is_archived",
            "created_at",
            "updated_at",
        ]

    def get_available_credit_bdt(self, obj: PosCustomer) -> str:
        return f"{PosCustomerDueService.compute_available_credit(obj):.2f}"


class CustomerDueCollectionSerializer(serializers.ModelSerializer):
    collected_by_email = serializers.CharField(source="collected_by.email", read_only=True, default="")
    reversed_by_email = serializers.CharField(source="reversed_by.email", read_only=True, default="")
    debit_account = serializers.CharField(source="journal_entry.debit_account", read_only=True, default="")
    credit_account = serializers.CharField(source="journal_entry.credit_account", read_only=True, default="AR_CUSTOMER_DUE")
    revenue_impact_bdt = serializers.SerializerMethodField()

    class Meta:
        model = CustomerDueCollection
        fields = [
            "collection_id",
            "tenant_id",
            "branch_id",
            "due_record_id",
            "customer_id",
            "sale_id",
            "register_assignment_id",
            "journal_entry_id",
            "debit_account",
            "credit_account",
            "revenue_impact_bdt",
            "tender_type",
            "amount_bdt",
            "cash_tendered_bdt",
            "change_given_bdt",
            "gateway_reference",
            "payer_reference",
            "card_last4",
            "verification_status",
            "verification_channel",
            "idempotency_key",
            "status",
            "notes",
            "collected_by_email",
            "collected_at",
            "reversed_by_email",
            "reversed_at",
            "reversal_reason",
            "sha256_hash",
        ]
        read_only_fields = fields

    def get_revenue_impact_bdt(self, obj: CustomerDueCollection) -> str:
        return "0.00"


class CustomerDueRecordSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source="branch.name", read_only=True, default="")
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True, default="")
    order_number = serializers.CharField(source="sale.order_number", read_only=True, default="")
    collections = CustomerDueCollectionSerializer(many=True, read_only=True)

    class Meta:
        model = CustomerDueRecord
        fields = [
            "due_id",
            "tenant_id",
            "branch_id",
            "branch_name",
            "customer_id",
            "sale_id",
            "order_number",
            "invoice_id",
            "invoice_number",
            "customer_name",
            "customer_phone",
            "total_due_bdt",
            "paid_amount_bdt",
            "remaining_due_bdt",
            "status",
            "due_date",
            "notes",
            "collections",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class PosSaleAllocationSerializer(serializers.ModelSerializer):
    product_brand_name = serializers.CharField(source="product.brand_name", read_only=True)
    batch_number = serializers.CharField(source="batch.batch_number", read_only=True)
    expiry_date = serializers.DateField(source="batch.expiry_date", read_only=True)
    override_approved_by_email = serializers.CharField(
        source="override_approved_by.email", read_only=True, default=""
    )
    unit_cost_bdt = serializers.SerializerMethodField()
    total_cost_bdt = serializers.SerializerMethodField()

    class Meta:
        model = PosSaleAllocation
        fields = [
            "allocation_id",
            "sale_id",
            "sale_line_id",
            "product_id",
            "product_brand_name",
            "batch_id",
            "batch_number",
            "expiry_date",
            "expiry_date_snapshot",
            "fefo_sequence",
            "allocation_mode",
            "override_reason",
            "override_approved_by_email",
            "cost_lot_id",
            "sale_lot_allocation_id",
            "allocated_base_units",
            "returned_base_units",
            "unit_cost_bdt",
            "total_cost_bdt",
            "status",
            "allocated_at",
            "released_at",
        ]
        read_only_fields = fields

    def _can_view_cost(self) -> bool:
        req = self.context.get("request")
        if not req or not getattr(req, "user", None):
            return False
        return PosDomainService.can_view_cost(req.user)

    def get_unit_cost_bdt(self, obj: PosSaleAllocation) -> str | None:
        if not self._can_view_cost():
            return None
        return f"{obj.unit_cost_bdt:.4f}"

    def get_total_cost_bdt(self, obj: PosSaleAllocation) -> str | None:
        if not self._can_view_cost():
            return None
        return f"{obj.total_cost_bdt:.4f}"


class PosCheckoutAllocationAuditSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source="actor_user.email", read_only=True, default="")

    class Meta:
        model = PosCheckoutAllocationAudit
        fields = [
            "alloc_audit_id",
            "tenant_id",
            "branch_id",
            "sale_id",
            "sale_line_id",
            "batch_id",
            "action",
            "allocation_mode",
            "requested_base_units",
            "allocated_base_units",
            "batch_number_snapshot",
            "expiry_date_snapshot",
            "was_rejected",
            "rejection_code",
            "reason",
            "actor_email",
            "actor_role",
            "is_offline_sync",
            "metadata_json",
            "sha256_hash",
            "created_at",
        ]
        read_only_fields = fields


class PosSaleOrderLineSerializer(serializers.ModelSerializer):
    product_brand_name = serializers.CharField(source="product.brand_name", read_only=True)
    product_generic_name = serializers.CharField(source="product.generic_name", read_only=True)
    variant_sku = serializers.CharField(source="variant.sku", read_only=True, default="")
    preferred_batch_number = serializers.CharField(source="preferred_batch.batch_number", read_only=True, default="")
    container_override_approved_by_email = serializers.CharField(
        source="container_override_approved_by.email", read_only=True, default=""
    )
    price_override_approved_by_email = serializers.CharField(
        source="price_override_approved_by.email", read_only=True, default=""
    )
    unit_cost_bdt = serializers.SerializerMethodField()
    allocations = PosSaleAllocationSerializer(many=True, read_only=True)

    class Meta:
        model = PosSaleOrderLine
        fields = [
            "line_id",
            "line_number",
            "product_id",
            "product_brand_name",
            "product_generic_name",
            "variant_id",
            "variant_sku",
            "scanned_barcode",
            "preferred_batch_id",
            "preferred_batch_number",
            "packaging_unit",
            "quantity_units",
            "conversion_factor",
            "base_units_total",
            "returned_base_units",
            "dosage_form_snapshot",
            "container_policy_applied",
            "unit_conversion_source",
            "container_override_approved_by_email",
            "container_override_reason",
            "unit_price_bdt",
            "price_source",
            "price_override_approved_by_email",
            "price_override_reason",
            "discount_type",
            "discount_rate_pct",
            "discount_amount_bdt",
            "tax_mode",
            "tax_rate_pct",
            "net_before_tax_bdt",
            "line_tax_amount_bdt",
            "line_subtotal_bdt",
            "line_total_bdt",
            "unit_cost_bdt",
            "requires_rx_verification",
            "rx_verified",
            "regulatory_classification",
            "classification_version_snapshot",
            "rx_verified_base_units",
            "linked_invoice_item_id",
            "notes",
            "allocations",
        ]
        read_only_fields = fields

    def get_unit_cost_bdt(self, obj: PosSaleOrderLine) -> str | None:
        req = self.context.get("request")
        if not req or not getattr(req, "user", None):
            return None
        if not PosDomainService.can_view_cost(req.user):
            return None
        return f"{obj.unit_cost_bdt:.4f}"


class PosCartUnitSelectionAuditSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source="actor_user.email", read_only=True, default="")
    override_approved_by_email = serializers.CharField(
        source="override_approved_by.email", read_only=True, default=""
    )
    product_brand_name = serializers.CharField(source="product.brand_name", read_only=True, default="")

    class Meta:
        model = PosCartUnitSelectionAudit
        fields = [
            "unit_audit_id",
            "tenant_id",
            "branch_id",
            "sale_id",
            "sale_line_id",
            "product_id",
            "product_brand_name",
            "variant_id",
            "from_unit",
            "to_unit",
            "from_conversion_factor",
            "to_conversion_factor",
            "quantity_units",
            "base_units_before",
            "base_units_after",
            "unit_price_before_bdt",
            "unit_price_after_bdt",
            "conversion_source",
            "container_policy_status",
            "was_rejected",
            "rejection_code",
            "rejection_message",
            "actor_email",
            "actor_role",
            "override_approved_by_email",
            "override_reason",
            "sha256_hash",
            "created_at",
        ]
        read_only_fields = fields


class PosPaymentAttemptSerializer(serializers.ModelSerializer):
    verified_by_email = serializers.CharField(source="verified_by.email", read_only=True, default="")
    voided_by_email = serializers.CharField(source="voided_by.email", read_only=True, default="")

    class Meta:
        model = PosPaymentAttempt
        fields = [
            "attempt_id",
            "sale_id",
            "tenant_id",
            "branch_id",
            "attempt_number",
            "tender_type",
            "amount_bdt",
            "change_given_bdt",
            "net_settled_amount_bdt",
            "status",
            "verification_status",
            "verification_channel",
            "card_last4",
            "split_batch_key",
            "idempotency_key",
            "gateway_reference",
            "payer_reference",
            "failure_reason_code",
            "failure_message",
            "linked_payment_tx_id",
            "verified_by_email",
            "verified_at",
            "voided_by_email",
            "voided_at",
            "void_reason",
            "attempted_at",
            "settled_at",
        ]
        read_only_fields = fields


class PosPaymentJournalEntrySerializer(serializers.ModelSerializer):
    posted_by_email = serializers.CharField(source="posted_by.email", read_only=True, default="")

    class Meta:
        model = PosPaymentJournalEntry
        fields = [
            "journal_id",
            "tenant_id",
            "branch_id",
            "sale_id",
            "invoice_id",
            "payment_attempt_id",
            "payment_tx_id",
            "register_assignment_id",
            "reversed_entry_id",
            "entry_type",
            "tender_type",
            "debit_account",
            "credit_account",
            "amount_bdt",
            "cash_register_impact_bdt",
            "gateway_reference",
            "verification_status",
            "notes",
            "posted_by_email",
            "is_offline_sync",
            "posted_at",
            "sha256_hash",
        ]
        read_only_fields = fields


class PosCashierCloseReconciliationSerializer(serializers.ModelSerializer):
    cashier_email = serializers.CharField(source="cashier_user.email", read_only=True, default="")
    closed_by_email = serializers.CharField(source="closed_by.email", read_only=True, default="")
    branch_name = serializers.CharField(source="branch.name", read_only=True, default="")

    class Meta:
        model = PosCashierCloseReconciliation
        fields = [
            "reconciliation_id",
            "tenant_id",
            "branch_id",
            "branch_name",
            "register_assignment_id",
            "cashier_email",
            "opening_float_bdt",
            "gross_cash_tendered_bdt",
            "cash_change_returned_bdt",
            "net_cash_collected_bdt",
            "cash_refunded_bdt",
            "expected_drawer_cash_bdt",
            "counted_cash_bdt",
            "variance_bdt",
            "digital_total_bdt",
            "due_issued_bdt",
            "due_collected_cash_bdt",
            "due_collected_digital_bdt",
            "tender_totals_json",
            "sales_count",
            "reversed_sales_count",
            "status",
            "notes",
            "closed_by_email",
            "closed_at",
            "sha256_hash",
        ]
        read_only_fields = fields


class PosSaleLifecycleAuditSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source="actor_user.email", read_only=True, default="")

    class Meta:
        model = PosSaleLifecycleAudit
        fields = [
            "audit_id",
            "sale_id",
            "from_state",
            "to_state",
            "action",
            "actor_email",
            "actor_role",
            "reason",
            "metadata_json",
            "sha256_hash",
            "created_at",
        ]
        read_only_fields = fields


class PosCartPricingAuditSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source="actor_user.email", read_only=True, default="")
    discount_approved_by_email = serializers.CharField(
        source="discount_approved_by.email", read_only=True, default=""
    )

    class Meta:
        model = PosCartPricingAudit
        fields = [
            "pricing_audit_id",
            "tenant_id",
            "branch_id",
            "sale_id",
            "action",
            "subtotal_bdt",
            "line_discount_total_bdt",
            "header_discount_amount_bdt",
            "discount_amount_bdt",
            "net_before_tax_bdt",
            "tax_mode",
            "tax_rate_pct",
            "tax_amount_bdt",
            "rounding_mode",
            "rounding_adjustment_bdt",
            "grand_total_bdt",
            "expected_grand_total_bdt",
            "pricing_confirmation_hash",
            "was_rejected",
            "rejection_code",
            "rejection_message",
            "actor_email",
            "actor_role",
            "discount_approved_by_email",
            "breakdown_snapshot",
            "sha256_hash",
            "created_at",
        ]
        read_only_fields = fields


class PosHeldCartAuditSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source="actor_user.email", read_only=True, default="")

    class Meta:
        model = PosHeldCartAudit
        fields = [
            "hold_audit_id",
            "tenant_id",
            "branch_id",
            "sale_id",
            "action",
            "hold_reference",
            "hold_note",
            "reserve_stock_requested",
            "released_reserved_units",
            "held_grand_total_bdt",
            "live_grand_total_bdt",
            "held_pricing_hash",
            "live_pricing_hash",
            "was_stale",
            "stale_reasons",
            "was_rejected",
            "rejection_code",
            "rejection_message",
            "actor_email",
            "actor_role",
            "metadata_json",
            "sha256_hash",
            "created_at",
        ]
        read_only_fields = fields


class PosSaleOrderSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    cashier_email = serializers.CharField(source="cashier_user.email", read_only=True)
    held_by_email = serializers.CharField(source="held_by.email", read_only=True, default="")
    resumed_by_email = serializers.CharField(source="resumed_by.email", read_only=True, default="")
    pricing_confirmed_by_email = serializers.CharField(
        source="pricing_confirmed_by.email", read_only=True, default=""
    )
    rx_verified_by_email = serializers.CharField(
        source="rx_verified_by.email", read_only=True, default=""
    )
    is_pricing_confirmed = serializers.SerializerMethodField()
    current_pricing_hash = serializers.SerializerMethodField()
    lines = PosSaleOrderLineSerializer(many=True, read_only=True)
    allocations = PosSaleAllocationSerializer(many=True, read_only=True)
    payment_attempts = PosPaymentAttemptSerializer(many=True, read_only=True)
    lifecycle_audits = PosSaleLifecycleAuditSerializer(many=True, read_only=True)

    class Meta:
        model = PosSaleOrder
        fields = [
            "sale_id",
            "tenant_id",
            "branch_id",
            "branch_name",
            "order_number",
            "idempotency_key",
            "offline_device_namespace",
            "offline_local_sequence",
            "device_id",
            "register_assignment_id",
            "cashier_user_id",
            "cashier_email",
            "cashier_staff_id",
            "customer_id",
            "customer_name",
            "customer_phone",
            "prescription_id",
            "linked_invoice_id",
            "source_channel",
            "sync_status",
            "lifecycle_state",
            "discount_type",
            "discount_rate_pct",
            "line_discount_total_bdt",
            "header_discount_amount_bdt",
            "discount_amount_bdt",
            "net_before_tax_bdt",
            "tax_mode",
            "tax_rate_pct",
            "tax_amount_bdt",
            "subtotal_bdt",
            "rounding_mode",
            "rounding_adjustment_bdt",
            "fulfillment_mode",
            "delivery_fee_bdt",
            "grand_total_bdt",
            "paid_amount_bdt",
            "due_amount_bdt",
            "change_returned_bdt",
            "refunded_amount_bdt",
            "currency",
            "requires_rx_verification",
            "rx_gate_status",
            "rx_decision_reason",
            "rx_reference_code",
            "pharmacist_pcb_no_snapshot",
            "rx_offline_blocked_reason",
            "rx_safety_policy_version",
            "fefo_allocation_status",
            "rx_verified_by_email",
            "rx_verified_at",
            "discount_reason",
            "pricing_confirmed_at",
            "pricing_confirmed_by_email",
            "pricing_confirmation_hash",
            "is_pricing_confirmed",
            "current_pricing_hash",
            "hold_reference",
            "hold_note",
            "held_at",
            "held_by_email",
            "hold_expires_at",
            "hold_stock_reserved",
            "hold_reservation_expires_at",
            "hold_snapshot_pricing_hash",
            "hold_snapshot_grand_total_bdt",
            "resumed_at",
            "resumed_by_email",
            "finalized_at",
            "voided_at",
            "void_reason",
            "reversed_by_sale_id",
            "version",
            "notes",
            "is_archived",
            "lines",
            "allocations",
            "payment_attempts",
            "lifecycle_audits",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_current_pricing_hash(self, obj: PosSaleOrder) -> str:
        lines = list(obj.lines.all())
        return PosCartPricingService.build_canonical_breakdown(obj, lines)["calculated_pricing_hash"]

    def get_is_pricing_confirmed(self, obj: PosSaleOrder) -> bool:
        if not obj.pricing_confirmed_at or not obj.pricing_confirmation_hash:
            return False
        return obj.pricing_confirmation_hash == self.get_current_pricing_hash(obj)


class PosCustomerViewSet(viewsets.ModelViewSet):
    """
    Authorized CRUD + Soft-Archive ViewSet for PosCustomer (/api/v2/pos/customers/).
    Enforces tenant/branch isolation, CASHIER credit-limit & archive denial, and CUSTOMER_LEDGER gating.
    """

    serializer_class = PosCustomerSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "customer_id"

    def get_queryset(self):
        user = self.request.user
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(user, require_write=False)
        include_archived = str(self.request.query_params.get("include_archived", "")).lower() in ("true", "1")
        qs = PosCustomer.objects.filter(tenant=tenant).select_related("primary_branch")
        if not include_archived:
            qs = qs.filter(is_archived=False)

        role = str(getattr(user, "role", "")).upper()
        actor_branch_id = getattr(user, "branch_id", None)
        if role not in PosDomainService.TENANT_WIDE_ROLES and actor_branch_id:
            qs = qs.filter(Q(primary_branch_id=actor_branch_id) | Q(primary_branch__isnull=True))

        branch_q = self.request.query_params.get("branch")
        if branch_q:
            PosDomainService.resolve_user_tenant_and_branch(user, branch_id=branch_q, require_write=False)
            qs = qs.filter(primary_branch_id=branch_q)

        phone_q = self.request.query_params.get("phone")
        if phone_q:
            qs = qs.filter(phone__icontains=phone_q)

        return qs.order_by("full_name", "-created_at")

    def create(self, request: Request, *args, **kwargs) -> Response:
        branch_id = request.data.get("primary_branch") or request.data.get("branch")
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            request.user,
            branch_id=branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES | PosDomainService.POS_CUSTOMER_ADMIN_ROLES,
        )
        credit_limit = q2(request.data.get("credit_limit_bdt", "0.00"))
        if credit_limit < Decimal("0.00"):
            raise ValidationError({"credit_limit_bdt": "Credit limit cannot be negative."})

        role = str(getattr(request.user, "role", "")).upper()
        req_status = str(request.data.get("credit_status") or "").upper().strip()
        req_approved = request.data.get("is_credit_approved")
        if credit_limit > Decimal("0.00"):
            if role not in PosDomainService.POS_CUSTOMER_ADMIN_ROLES:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Only Pharmacy Owner, Branch Manager, or Finance Officer may grant customer credit limits.",
                        "message_bn": "শুধুমাত্র মালিক, শাখা ব্যবস্থাপক বা ফাইন্যান্স অফিসার ক্রেডিট লিমিট নির্ধারণ করতে পারেন।",
                    }
                )
            PosDomainService.assert_feature_entitlement(tenant, "CUSTOMER_LEDGER", "customer credit limits")
            if req_status in (PosCustomerCreditStatus.PENDING_APPROVAL, PosCustomerCreditStatus.UNAPPROVED, PosCustomerCreditStatus.SUSPENDED) or req_approved is False:
                resolved_credit_status = req_status or PosCustomerCreditStatus.PENDING_APPROVAL
                resolved_approved = False
            else:
                resolved_credit_status = PosCustomerCreditStatus.APPROVED
                resolved_approved = True
        else:
            resolved_credit_status = PosCustomerCreditStatus.PENDING_APPROVAL if req_status == PosCustomerCreditStatus.PENDING_APPROVAL else PosCustomerCreditStatus.UNAPPROVED
            resolved_approved = False

        raw_terms = request.data.get("credit_terms_days")
        try:
            terms_days = 30 if raw_terms is None or raw_terms == "" else int(raw_terms)
        except (ValueError, TypeError):
            raise ValidationError({"credit_terms_days": "credit_terms_days must be an integer."})
        if terms_days < 1:
            raise ValidationError({"credit_terms_days": "credit_terms_days must be >= 1."})

        full_name = str(request.data.get("full_name") or "").strip()
        if not full_name:
            raise ValidationError({"full_name": "Customer full_name is required."})

        phone = str(request.data.get("phone") or "").strip()
        if phone and PosCustomer.objects.filter(tenant=tenant, phone=phone, is_archived=False).exists():
            raise ValidationError({"phone": f"An active customer with phone '{phone}' already exists."})

        privacy_prefs = request.data.get("privacy_preferences")
        if privacy_prefs is not None and not isinstance(privacy_prefs, dict):
            raise ValidationError({"privacy_preferences": "privacy_preferences must be a JSON object / dict."})
        resolved_prefs = privacy_prefs if isinstance(privacy_prefs, dict) else {
            "sms_receipts": True,
            "marketing_opt_in": False,
            "data_retention_consent": True,
        }

        customer = PosCustomer.objects.create(
            tenant=tenant,
            primary_branch=branch,
            full_name=full_name,
            phone=phone,
            email=str(request.data.get("email") or ""),
            address=str(request.data.get("address") or ""),
            loyalty_code=str(request.data.get("loyalty_code") or ""),
            credit_limit_bdt=credit_limit,
            is_credit_approved=resolved_approved,
            credit_status=resolved_credit_status,
            credit_terms_days=terms_days,
            credit_approved_by=request.user if resolved_approved else None,
            credit_approved_at=timezone.now() if resolved_approved else None,
            notes=str(request.data.get("notes") or ""),
            privacy_preferences=resolved_prefs,
            created_by=request.user,
            updated_by=request.user,
        )
        return Response(PosCustomerSerializer(customer).data, status=status.HTTP_201_CREATED)

    def update(self, request: Request, *args, **kwargs) -> Response:
        return self.partial_update(request, *args, **kwargs)

    def partial_update(self, request: Request, *args, **kwargs) -> Response:
        customer = self.get_object()
        branch_id = request.data.get("primary_branch") or customer.primary_branch_id
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            request.user,
            branch_id=branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES | PosDomainService.POS_CUSTOMER_ADMIN_ROLES,
        )
        role = str(getattr(request.user, "role", "")).upper()

        if "credit_limit_bdt" in request.data:
            new_limit = q2(request.data["credit_limit_bdt"])
            if new_limit < Decimal("0.00"):
                raise ValidationError({"credit_limit_bdt": "Credit limit cannot be negative."})
            if new_limit != customer.credit_limit_bdt:
                if role not in PosDomainService.POS_CUSTOMER_ADMIN_ROLES:
                    raise PermissionDenied(
                        {
                            "error_code": "ERR_PERMISSION_DENIED",
                            "message_en": "Only Owner, Branch Manager, or Finance Officer may modify customer credit limits.",
                        }
                    )
                if new_limit > Decimal("0.00"):
                    PosDomainService.assert_feature_entitlement(tenant, "CUSTOMER_LEDGER", "customer credit limits")
                customer.credit_limit_bdt = new_limit

        if "phone" in request.data:
            new_phone = str(request.data["phone"] or "").strip()
            if new_phone and PosCustomer.objects.filter(tenant=tenant, phone=new_phone, is_archived=False).exclude(customer_id=customer.customer_id).exists():
                raise ValidationError({"phone": f"An active customer with phone '{new_phone}' already exists."})
            customer.phone = new_phone

        if "full_name" in request.data:
            new_name = str(request.data["full_name"] or "").strip()
            if not new_name:
                raise ValidationError({"full_name": "Customer full_name cannot be empty."})
            customer.full_name = new_name

        if "privacy_preferences" in request.data:
            new_prefs = request.data["privacy_preferences"]
            if new_prefs is not None and not isinstance(new_prefs, dict):
                raise ValidationError({"privacy_preferences": "privacy_preferences must be a JSON object / dict."})
            customer.privacy_preferences = new_prefs or {}

        if "credit_terms_days" in request.data:
            try:
                new_terms = int(request.data["credit_terms_days"])
                if new_terms < 1:
                    raise ValueError
                customer.credit_terms_days = new_terms
            except (ValueError, TypeError):
                raise ValidationError({"credit_terms_days": "credit_terms_days must be >= 1."})

        for field in ("email", "address", "loyalty_code", "notes"):
            if field in request.data:
                setattr(customer, field, str(request.data[field] or "").strip())
        if "primary_branch" in request.data:
            customer.primary_branch = branch
        customer.updated_by = request.user
        customer.save()
        return Response(PosCustomerSerializer(customer).data, status=status.HTTP_200_OK)

    def destroy(self, request: Request, *args, **kwargs) -> Response:
        customer = self.get_object()
        PosDomainService.resolve_user_tenant_and_branch(
            request.user,
            branch_id=customer.primary_branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_CUSTOMER_ADMIN_ROLES,
        )
        if customer.current_due_bdt > Decimal("0.00"):
            raise ValidationError(
                {
                    "error_code": "ERR_CUSTOMER_HAS_OUTSTANDING_DUE",
                    "detail": f"Cannot archive customer with outstanding due balance ({customer.current_due_bdt} BDT).",
                }
            )
        customer.is_archived = True
        customer.is_active = False
        customer.updated_by = request.user
        customer.save(update_fields=["is_archived", "is_active", "updated_by", "updated_at"])
        return Response(
            {
                "customer_id": customer.customer_id,
                "is_archived": True,
                "message_en": "Customer archived safely.",
                "message_bn": "গ্রাহক প্রোফাইল নিরাপদে আর্কাইভ করা হয়েছে।",
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="approve-credit")
    def approve_credit(self, request: Request, customer_id=None) -> Response:
        """POST /api/v2/pos/customers/{customer_id}/approve-credit/ — Approve credit facility & limit."""
        customer = self.get_object()
        updated = PosCustomerDueService.approve_customer_credit(request.user, customer, request.data)
        return Response(PosCustomerSerializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="suspend-credit")
    def suspend_credit(self, request: Request, customer_id=None) -> Response:
        """POST /api/v2/pos/customers/{customer_id}/suspend-credit/ — Freeze credit facility with reason."""
        customer = self.get_object()
        reason = str(request.data.get("reason") or "")
        updated = PosCustomerDueService.suspend_customer_credit(request.user, customer, reason)
        return Response(PosCustomerSerializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="ledger")
    def ledger(self, request: Request, customer_id=None) -> Response:
        """GET /api/v2/pos/customers/{customer_id}/ledger/ — Customer dues, collections & zero double-revenue check."""
        customer = self.get_object()
        summary = PosCustomerDueService.build_customer_ledger(request.user, customer)
        dues = summary.pop("dues")
        collections = summary.pop("collections")
        summary["due_records"] = CustomerDueRecordSerializer(dues, many=True).data
        summary["collections"] = CustomerDueCollectionSerializer(collections, many=True).data
        return Response(summary, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="purchase-history")
    def purchase_history(self, request: Request, customer_id=None) -> Response:
        """GET /api/v2/pos/customers/{customer_id}/purchase-history/ — Unified timeline with pagination & privacy masking."""
        customer = self.get_object()
        branch_id = request.query_params.get("branch")
        event_type = request.query_params.get("event_type", "ALL")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        page = request.query_params.get("page", 1)
        page_size = request.query_params.get("page_size", 20)

        history_data = CustomerPurchaseHistoryService.get_customer_purchase_history(
            request.user,
            customer.customer_id,
            branch_id=branch_id,
            event_type=event_type,
            start_date=start_date,
            end_date=end_date,
            page=page,
            page_size=page_size,
        )
        return Response(history_data, status=status.HTTP_200_OK)


class PosSaleOrderViewSet(viewsets.ModelViewSet):
    """
    Canonical POS Sale Order ViewSet (/api/v2/pos/sales/).
    Supports authorized GET/POST/PATCH/SOFT-VOID plus audited lifecycle transitions:
    hold, resume, verify-rx, allocate, record-payment, finalize, reverse.
    """

    serializer_class = PosSaleOrderSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "sale_id"

    def get_queryset(self):
        user = self.request.user
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(user, require_write=False)
        qs = (
            PosSaleOrder.objects.filter(tenant=tenant)
            .select_related("branch", "cashier_user", "customer", "prescription", "linked_invoice")
            .prefetch_related(
                "lines__product",
                "lines__variant",
                "lines__container_override_approved_by",
                "lines__allocations__batch",
                "allocations__batch",
                "payment_attempts",
                "lifecycle_audits__actor_user",
            )
        )

        role = str(getattr(user, "role", "")).upper()
        actor_branch_id = getattr(user, "branch_id", None)
        if role not in PosDomainService.TENANT_WIDE_ROLES and actor_branch_id:
            qs = qs.filter(branch_id=actor_branch_id)

        branch_q = self.request.query_params.get("branch")
        if branch_q:
            PosDomainService.resolve_user_tenant_and_branch(user, branch_id=branch_q, require_write=False)
            qs = qs.filter(branch_id=branch_q)

        state_q = self.request.query_params.get("lifecycle_state")
        if state_q:
            qs = qs.filter(lifecycle_state=state_q.upper())

        sync_q = self.request.query_params.get("sync_status")
        if sync_q:
            qs = qs.filter(sync_status=sync_q.upper())

        return qs.order_by("-created_at", "-sale_id")

    def create(self, request: Request, *args, **kwargs) -> Response:
        sale, created = PosDomainService.create_sale_order(request.user, request.data)
        serializer = self.get_serializer(sale)
        return Response(serializer.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def update(self, request: Request, *args, **kwargs) -> Response:
        return self.partial_update(request, *args, **kwargs)

    def partial_update(self, request: Request, *args, **kwargs) -> Response:
        sale = self.get_object()
        updated = PosDomainService.update_mutable_sale(request.user, sale, request.data)
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    def destroy(self, request: Request, *args, **kwargs) -> Response:
        sale = self.get_object()
        reason = str(request.data.get("reason") or request.query_params.get("reason") or "Cancelled before finalization")
        voided = PosDomainService.void_pre_final_sale(request.user, sale, reason)
        return Response(self.get_serializer(voided).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="attach-customer")
    def attach_customer(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/attach-customer/ — Links a PosCustomer to a mutable sale."""
        sale = self.get_object()
        updated = PosCustomerDueService.attach_customer_to_sale(request.user, sale, request.data)
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="hold")
    def hold(self, request: Request, sale_id=None) -> Response:
        sale = self.get_object()
        try:
            updated = PosHeldCartService.hold_cart(request.user, sale, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return self._pricing_error_response(exc, "কার্ট হোল্ড করা সম্ভব হয়নি।")
        payload = dict(self.get_serializer(updated).data)
        payload["staleness"] = PosHeldCartService.inspect_held_cart_staleness(updated)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="resume")
    def resume(self, request: Request, sale_id=None) -> Response:
        sale = self.get_object()
        try:
            resumed, staleness = PosHeldCartService.resume_cart(request.user, sale, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return self._pricing_error_response(exc, "হোল্ড করা কার্ট রিজিউম করার আগে নিশ্চিতকরণ প্রয়োজন।")
        payload = dict(self.get_serializer(resumed).data)
        payload["staleness"] = staleness
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="held")
    def held(self, request: Request) -> Response:
        """GET /api/v2/pos/sales/held/ — List branch-scoped held baskets with live staleness diagnostics."""
        include_expired = str(request.query_params.get("include_expired", "true")).lower() in ("true", "1", "yes")
        branch_id = request.query_params.get("branch") or request.query_params.get("branch_id")
        held_pairs = PosHeldCartService.list_held_carts(
            request.user,
            branch_id=branch_id,
            include_expired=include_expired,
        )
        items = []
        stale_count = 0
        expired_count = 0
        for held_sale, staleness in held_pairs:
            if staleness["is_stale"]:
                stale_count += 1
            if staleness["is_hold_expired"] or staleness["is_reservation_expired"]:
                expired_count += 1
            items.append(
                {
                    "sale_id": held_sale.sale_id,
                    "order_number": held_sale.order_number,
                    "branch_id": held_sale.branch_id,
                    "branch_name": held_sale.branch.name,
                    "customer_name": held_sale.customer_name,
                    "customer_phone": held_sale.customer_phone,
                    "lifecycle_state": held_sale.lifecycle_state,
                    "hold_reference": held_sale.hold_reference,
                    "hold_note": held_sale.hold_note,
                    "held_by_email": held_sale.held_by.email if held_sale.held_by_id else "",
                    "held_at": held_sale.held_at.isoformat() if held_sale.held_at else None,
                    "hold_expires_at": held_sale.hold_expires_at.isoformat() if held_sale.hold_expires_at else None,
                    "hold_stock_reserved": bool(held_sale.hold_stock_reserved),
                    "hold_reservation_expires_at": (
                        held_sale.hold_reservation_expires_at.isoformat()
                        if held_sale.hold_reservation_expires_at
                        else None
                    ),
                    "line_count": held_sale.lines.count(),
                    "held_grand_total_bdt": staleness["held_grand_total_bdt"],
                    "live_grand_total_bdt": staleness["live_grand_total_bdt"],
                    "staleness": staleness,
                    "version": int(held_sale.version),
                    "sale": self.get_serializer(held_sale).data,
                }
            )
        return Response(
            {
                "count": len(items),
                "stale_count": stale_count,
                "expired_count": expired_count,
                "results": items,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="expire-stale-holds")
    def expire_stale_holds(self, request: Request) -> Response:
        """POST /api/v2/pos/sales/expire-stale-holds/ — Releases timed-out hold reservations back to FEFO stock."""
        try:
            summary = PosHeldCartService.expire_stale_holds(
                request.user,
                branch_id=request.data.get("branch") or request.data.get("branch_id"),
                void_expired_holds=bool(request.data.get("void_expired_holds", False)),
                reason=str(request.data.get("reason") or ""),
            )
        except (PermissionDenied, ValidationError) as exc:
            return self._pricing_error_response(exc, "মেয়াদোত্তীর্ণ হোল্ড কার্ট প্রসেস করা যায়নি।")
        return Response(summary, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="expire-hold")
    def expire_hold(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/expire-hold/ — Releases a held cart's stock reservation cleanly."""
        sale = self.get_object()
        try:
            updated, summary = PosHeldCartService.expire_single_hold(
                request.user,
                sale,
                void_hold=bool(request.data.get("void_hold", False)),
                reason=str(request.data.get("reason") or ""),
            )
        except (PermissionDenied, ValidationError) as exc:
            return self._pricing_error_response(exc, "হোল্ড রিজার্ভেশন মুক্ত করা যায়নি।")
        return Response(
            {
                **summary,
                "sale": self.get_serializer(updated).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="cancel-hold")
    def cancel_hold(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/cancel-hold/ — Releases reservations and voids a held cart cleanly."""
        sale = self.get_object()
        try:
            expected_version = PosHeldCartService.parse_strict_expected_version(request.data.get("expected_version"))
            cancelled = PosHeldCartService.cancel_hold(
                request.user,
                sale,
                reason=str(request.data.get("reason") or ""),
                expected_version=expected_version,
            )
        except (PermissionDenied, ValidationError) as exc:
            return self._pricing_error_response(exc, "হোল্ড কার্ট বাতিল করা যায়নি।")
        return Response(self.get_serializer(cancelled).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="hold-audits")
    def hold_audits(self, request: Request, sale_id=None) -> Response:
        """GET /api/v2/pos/sales/{sale_id}/hold-audits/ — Append-only held/resumed cart audit trail."""
        sale = self.get_object()
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            request.user,
            branch_id=sale.branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        qs = (
            PosHeldCartAudit.objects.filter(tenant=tenant, sale=sale)
            .select_related("actor_user")
            .order_by("-created_at", "-hold_audit_id")[:100]
        )
        return Response(PosHeldCartAuditSerializer(qs, many=True).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="rx-gate")
    def rx_gate(self, request: Request, sale_id=None) -> Response:
        """GET /api/v2/pos/sales/{sale_id}/rx-gate/ — P227 live Rx safety classification & gate summary."""
        sale = self.get_object()
        PosDomainService.resolve_user_tenant_and_branch(
            request.user,
            branch_id=sale.branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        summary = PosRxSafetyService.evaluate_and_sync_sale_rx_gate(sale, actor_user=request.user, mutate=False)
        return Response(summary, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="attach-prescription")
    def attach_prescription(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/attach-prescription/ — Links/records minimal Rx metadata without self-approving."""
        sale = self.get_object()
        try:
            updated = PosRxSafetyService.attach_prescription_to_sale(request.user, sale, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return self._pricing_error_response(exc, "প্রেসক্রিপশন সংযুক্ত করা যায়নি।")
        return Response(
            {
                "sale": self.get_serializer(updated).data,
                "rx_gate": PosRxSafetyService.evaluate_and_sync_sale_rx_gate(
                    updated, actor_user=request.user, mutate=False
                ),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="verify-rx")
    def verify_rx(self, request: Request, sale_id=None) -> Response:
        sale = self.get_object()
        updated = PosDomainService.verify_rx_sale(
            request.user,
            sale,
            notes=str(request.data.get("notes") or ""),
            data=request.data,
        )
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reject-rx")
    def reject_rx(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/reject-rx/ — Registered Pharmacist clinical rejection with documented reason."""
        sale = self.get_object()
        try:
            updated = PosRxSafetyService.reject_sale_rx(request.user, sale, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return self._pricing_error_response(exc, "প্রেসক্রিপশন প্রত্যাখ্যান করা যায়নি।")
        return Response(
            {
                "sale": self.get_serializer(updated).data,
                "rx_gate": PosRxSafetyService.evaluate_and_sync_sale_rx_gate(
                    updated, actor_user=request.user, mutate=False
                ),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="rx-gate/offline-preflight")
    def rx_offline_preflight(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/rx-gate/offline-preflight/ — Enforces SRS offline Rx limitation."""
        sale = self.get_object()
        http_code, payload = PosRxSafetyService.evaluate_offline_preflight(request.user, sale, request.data)
        return Response(payload, status=http_code)

    @action(detail=True, methods=["post"], url_path="rx-gate/offline-authorize")
    def rx_offline_authorize(self, request: Request, sale_id=None) -> Response:
        """
        POST /api/v2/pos/sales/{sale_id}/rx-gate/offline-authorize/
        Authorizes prescription items for offline dispensing under conservative on-duty pharmacist sign-off (ADR-0248).
        """
        sale = self.get_object()
        summary = PosRxSafetyService.authorize_prescription_offline(request.user, sale, request.data)
        return Response(summary, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="rx-gate/pharmacist-override")
    def pharmacist_override(self, request: Request, sale_id=None) -> Response:
        """
        POST /api/v2/pos/sales/{sale_id}/rx-gate/pharmacist-override/
        Records a credentialed pharmacist clinical override for permitted exceptions (ADR-0249).
        """
        from apps.core.pharmacist_override_service import PharmacistClinicalOverrideService

        sale = self.get_object()
        product_id = request.data.get("product_id")
        if not product_id:
            raise ValidationError({"product_id": "product_id is a required field for clinical override."})
        product = MedicineProduct.objects.filter(product_id=str(product_id).strip()).first()
        if not product:
            raise ValidationError({"product_id": f"Product '{product_id}' not found."})

        result = PharmacistClinicalOverrideService.record_clinical_override(
            request.user, sale, product, request.data
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="rx-gate/pharmacist-overrides")
    def list_pharmacist_overrides(self, request: Request, sale_id=None) -> Response:
        """
        GET /api/v2/pos/sales/{sale_id}/rx-gate/pharmacist-overrides/
        Lists all clinical overrides recorded for this sale order (ADR-0249).
        """
        from apps.core.pharmacist_override_service import PharmacistClinicalOverrideService

        sale = self.get_object()
        overrides = PharmacistClinicalOverrideService.list_sale_overrides(request.user, sale)
        return Response({"count": len(overrides), "results": overrides}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="rx-gate/substitute-item")
    def substitute_item(self, request: Request, sale_id=None) -> Response:
        """
        POST /api/v2/pos/sales/{sale_id}/rx-gate/substitute-item/
        Executes an audited human-pharmacist manual brand substitution (ADR-0250).
        Disallows autonomous or automated brand substitutions, AI dosage advice, or diagnosis.
        """
        from apps.core.clinical_guardrails_service import ClinicalSafetyGuardrailsService

        sale = self.get_object()
        orig_id = request.data.get("original_product_id")
        repl_id = request.data.get("replacement_product_id")
        if not orig_id:
            raise ValidationError({"original_product_id": "original_product_id is required."})
        if not repl_id:
            raise ValidationError({"replacement_product_id": "replacement_product_id is required."})

        orig_prod = MedicineProduct.objects.filter(product_id=str(orig_id).strip()).first()
        if not orig_prod:
            raise ValidationError({"original_product_id": f"Product '{orig_id}' not found."})
        repl_prod = MedicineProduct.objects.filter(product_id=str(repl_id).strip()).first()
        if not repl_prod:
            raise ValidationError({"replacement_product_id": f"Product '{repl_id}' not found."})

        result = ClinicalSafetyGuardrailsService.execute_manual_brand_substitution(
            request.user, sale, orig_prod, repl_prod, request.data
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="rx-gate/substitutions")
    def list_substitutions(self, request: Request, sale_id=None) -> Response:
        """
        GET /api/v2/pos/sales/{sale_id}/rx-gate/substitutions/
        Lists audited manual brand substitutions on this sale order (ADR-0250).
        """
        from apps.core.clinical_guardrails_service import ClinicalSafetyGuardrailsService

        sale = self.get_object()
        subs = ClinicalSafetyGuardrailsService.list_sale_substitutions(request.user, sale)
        return Response({"count": len(subs), "results": subs}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="clinical-safety-disclaimers")
    def clinical_safety_disclaimers(self, request: Request) -> Response:
        """
        GET /api/v2/pos/sales/clinical-safety-disclaimers/
        Returns statutory disclaimers prohibiting automated substitution, dosage advice, and diagnosis (ADR-0250).
        """
        from apps.core.clinical_guardrails_service import ClinicalSafetyGuardrailsService

        return Response(ClinicalSafetyGuardrailsService.get_statutory_disclaimers(), status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="rx-gate/offline-authorize")
    def rx_offline_authorize(self, request: Request, sale_id=None) -> Response:
        """
        POST /api/v2/pos/sales/{sale_id}/rx-gate/offline-authorize/
        Authorizes prescription items for offline dispensing under conservative on-duty pharmacist sign-off (ADR-0248).
        """
        sale = self.get_object()
        summary = PosRxSafetyService.authorize_prescription_offline(request.user, sale, request.data)
        return Response(summary, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="rx-gate/pharmacist-override")
    def pharmacist_override(self, request: Request, sale_id=None) -> Response:
        """
        POST /api/v2/pos/sales/{sale_id}/rx-gate/pharmacist-override/
        Records a credentialed pharmacist clinical override for permitted exceptions (ADR-0249).
        """
        from apps.core.pharmacist_override_service import PharmacistClinicalOverrideService

        sale = self.get_object()
        product_id = request.data.get("product_id")
        if not product_id:
            raise ValidationError({"product_id": "product_id is a required field for clinical override."})
        product = MedicineProduct.objects.filter(product_id=str(product_id).strip()).first()
        if not product:
            raise ValidationError({"product_id": f"Product '{product_id}' not found."})

        result = PharmacistClinicalOverrideService.record_clinical_override(
            request.user, sale, product, request.data
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="rx-gate/pharmacist-overrides")
    def list_pharmacist_overrides(self, request: Request, sale_id=None) -> Response:
        """
        GET /api/v2/pos/sales/{sale_id}/rx-gate/pharmacist-overrides/
        Lists all clinical overrides recorded for this sale order (ADR-0249).
        """
        from apps.core.pharmacist_override_service import PharmacistClinicalOverrideService

        sale = self.get_object()
        overrides = PharmacistClinicalOverrideService.list_sale_overrides(request.user, sale)
        return Response({"count": len(overrides), "results": overrides}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="rx-gate/substitute-item")
    def substitute_item(self, request: Request, sale_id=None) -> Response:
        """
        POST /api/v2/pos/sales/{sale_id}/rx-gate/substitute-item/
        Executes an audited human-pharmacist manual brand substitution (ADR-0250).
        Disallows autonomous or automated brand substitutions, AI dosage advice, or diagnosis.
        """
        from apps.core.clinical_guardrails_service import ClinicalSafetyGuardrailsService

        sale = self.get_object()
        orig_id = request.data.get("original_product_id")
        repl_id = request.data.get("replacement_product_id")
        if not orig_id:
            raise ValidationError({"original_product_id": "original_product_id is required."})
        if not repl_id:
            raise ValidationError({"replacement_product_id": "replacement_product_id is required."})

        orig_prod = MedicineProduct.objects.filter(product_id=str(orig_id).strip()).first()
        if not orig_prod:
            raise ValidationError({"original_product_id": f"Product '{orig_id}' not found."})
        repl_prod = MedicineProduct.objects.filter(product_id=str(repl_id).strip()).first()
        if not repl_prod:
            raise ValidationError({"replacement_product_id": f"Product '{repl_id}' not found."})

        result = ClinicalSafetyGuardrailsService.execute_manual_brand_substitution(
            request.user, sale, orig_prod, repl_prod, request.data
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="rx-gate/substitutions")
    def list_substitutions(self, request: Request, sale_id=None) -> Response:
        """
        GET /api/v2/pos/sales/{sale_id}/rx-gate/substitutions/
        Lists audited manual brand substitutions on this sale order (ADR-0250).
        """
        from apps.core.clinical_guardrails_service import ClinicalSafetyGuardrailsService

        sale = self.get_object()
        subs = ClinicalSafetyGuardrailsService.list_sale_substitutions(request.user, sale)
        return Response({"count": len(subs), "results": subs}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="clinical-safety-disclaimers")
    def clinical_safety_disclaimers(self, request: Request) -> Response:
        """
        GET /api/v2/pos/sales/clinical-safety-disclaimers/
        Returns statutory disclaimers prohibiting automated substitution, dosage advice, and diagnosis (ADR-0250).
        """
        from apps.core.clinical_guardrails_service import ClinicalSafetyGuardrailsService

        return Response(ClinicalSafetyGuardrailsService.get_statutory_disclaimers(), status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="rx-audits")
    def rx_audits(self, request: Request, sale_id=None) -> Response:
        """GET /api/v2/pos/sales/{sale_id}/rx-audits/ — Append-only Rx safety gate audit trail."""
        sale = self.get_object()
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            request.user,
            branch_id=sale.branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        qs = (
            PosRxSafetyAudit.objects.filter(tenant=tenant, sale=sale)
            .select_related("actor_user")
            .order_by("-created_at", "-rx_audit_id")[:100]
        )
        return Response(PosRxSafetyAuditSerializer(qs, many=True).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="receipt")
    def receipt(self, request: Request, sale_id=None) -> Response:
        """GET /api/v2/pos/sales/{sale_id}/receipt/ — Printable thermal/A4 receipt payload with Rx compliance footer."""
        sale = self.get_object()
        return Response(PosRxSafetyService.build_printable_receipt(request.user, sale), status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="fefo-plan")
    def fefo_plan(self, request: Request, sale_id=None) -> Response:
        """GET /api/v2/pos/sales/{sale_id}/fefo-plan/ — Preview deterministic FEFO & blocked batch plan."""
        sale = self.get_object()
        pref_batch = str(request.query_params.get("preferred_batch_id") or "").strip()
        line_id = str(request.query_params.get("line_id") or "").strip()
        overrides = {line_id: pref_batch} if (pref_batch and line_id) else None
        payload = PosFefoCheckoutService.build_fefo_plan(
            request.user,
            sale,
            batch_overrides=overrides,
            override_reason=str(request.query_params.get("override_reason") or ""),
        )
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="allocation-audits")
    def allocation_audits(self, request: Request, sale_id=None) -> Response:
        """GET /api/v2/pos/sales/{sale_id}/allocation-audits/ — Append-only FEFO checkout allocation audits."""
        sale = self.get_object()
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            request.user,
            branch_id=sale.branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        qs = (
            PosCheckoutAllocationAudit.objects.filter(tenant=tenant, sale=sale)
            .select_related("actor_user")
            .order_by("-created_at", "-alloc_audit_id")[:100]
        )
        return Response(PosCheckoutAllocationAuditSerializer(qs, many=True).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="allocate")
    def allocate(self, request: Request, sale_id=None) -> Response:
        sale = self.get_object()
        updated = PosDomainService.allocate_stock(
            request.user,
            sale,
            batch_overrides=request.data.get("batch_overrides"),
            override_reason=str(request.data.get("override_reason") or ""),
            expected_version=request.data.get("expected_version"),
        )
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="record-payment")
    def record_payment(self, request: Request, sale_id=None) -> Response:
        sale = self.get_object()
        attempt, created = PosDomainService.record_payment_attempt(request.user, sale, request.data)
        sale.refresh_from_db()
        return Response(
            {
                "idempotent_replay": not created,
                "payment_attempt": PosPaymentAttemptSerializer(attempt).data,
                "sale": self.get_serializer(sale).data,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="split-payments")
    def split_payments(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/split-payments/ — Atomic multi-tender split posting."""
        sale = self.get_object()
        updated, attempts, created = PosPaymentSplitService.record_split_batch(request.user, sale, request.data)
        updated.refresh_from_db()
        return Response(
            {
                "idempotent_replay": not created,
                "payment_attempts": PosPaymentAttemptSerializer(attempts, many=True).data,
                "sale": self.get_serializer(updated).data,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="verify-payment-attempt")
    def verify_payment_attempt(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/verify-payment-attempt/ — Verify pending digital tender."""
        sale = self.get_object()
        updated = PosPaymentSplitService.verify_payment_attempt(request.user, sale, request.data)
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="void-payment-attempt")
    def void_payment_attempt(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/void-payment-attempt/ — Audited pre-final attempt void."""
        sale = self.get_object()
        updated = PosPaymentSplitService.void_payment_attempt(request.user, sale, request.data)
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="refund-split")
    def refund_split(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/refund-split/ — Audited partial return split refund & journal."""
        sale = self.get_object()
        updated, entries = PosPaymentSplitService.post_partial_return_refund(request.user, sale, request.data)
        return Response(
            {
                "sale": self.get_serializer(updated).data,
                "refund_journal_entries": PosPaymentJournalEntrySerializer(entries, many=True).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="payment-journals")
    def payment_journals(self, request: Request, sale_id=None) -> Response:
        """GET /api/v2/pos/sales/{sale_id}/payment-journals/ — Immutable payment journal entries for sale."""
        sale = self.get_object()
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            request.user,
            branch_id=sale.branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        entries = PosPaymentJournalEntry.objects.filter(tenant=tenant, sale=sale).select_related("posted_by")
        return Response(PosPaymentJournalEntrySerializer(entries, many=True).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get", "post"], url_path="close-reconciliation")
    def close_reconciliation(self, request: Request) -> Response:
        """GET/POST /api/v2/pos/sales/close-reconciliation/ — Cashier drawer & multi-tender shift close."""
        if request.method == "GET":
            branch_id = str(
                request.query_params.get("branch")
                or request.query_params.get("branch_id")
                or getattr(request.user, "branch_id", "")
                or ""
            ).strip()
            if not branch_id:
                raise ValidationError({"branch": "branch query parameter is required."})
            reg_id = str(request.query_params.get("register_assignment") or "").strip()
            cashier_id = str(request.query_params.get("cashier_user_id") or "").strip()
            preview = PosPaymentSplitService.build_close_reconciliation_preview(
                request.user,
                branch_id=branch_id,
                register_assignment_id=reg_id,
                cashier_user_id=cashier_id,
            )
            recent = (
                PosCashierCloseReconciliation.objects.filter(tenant_id=preview["tenant_id"], branch_id=branch_id)
                .select_related("cashier_user", "closed_by", "branch")
                .order_by("-closed_at")[:20]
            )
            preview["recent_reconciliations"] = PosCashierCloseReconciliationSerializer(recent, many=True).data
            return Response(preview, status=status.HTTP_200_OK)

        rec = PosPaymentSplitService.submit_close_reconciliation(request.user, request.data)
        return Response(PosCashierCloseReconciliationSerializer(rec).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="finalize")
    def finalize(self, request: Request, sale_id=None) -> Response:
        sale = self.get_object()
        try:
            finalized = PosDomainService.finalize_sale(
                request.user,
                sale,
                expected_grand_total_bdt=request.data.get("expected_grand_total_bdt"),
                expected_pricing_hash=request.data.get("expected_pricing_hash"),
                is_offline_sync=bool(request.data.get("is_offline_sync", False)),
                batch_overrides=request.data.get("batch_overrides"),
                override_reason=str(request.data.get("override_reason") or ""),
            )
        except ValidationError as exc:
            det = exc.detail if isinstance(exc.detail, dict) else {}
            if str(det.get("error_code") or "") == "ERR_PRICE_CONFIRMATION_MISMATCH":
                return self._pricing_error_response(exc, "মূল্য নিশ্চিতকরণের তথ্যে গরমিল রয়েছে।")
            raise
        return Response(self.get_serializer(finalized).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse(self, request: Request, sale_id=None) -> Response:
        sale = self.get_object()
        reason = str(request.data.get("reason") or "")
        reversed_sale = PosDomainService.reverse_finalized_sale(request.user, sale, reason)
        return Response(self.get_serializer(reversed_sale).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="void")
    def void(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/void/ — Audited pre-final sale void command."""
        sale = self.get_object()
        reason = str(request.data.get("reason") or "").strip()
        voided = PosDomainService.void_pre_final_sale(request.user, sale, reason)
        return Response(self.get_serializer(voided).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="correct")
    def correct(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/correct/ — Audited cart or posted sale correction command."""
        sale = self.get_object()
        reason = str(request.data.get("reason") or "").strip()
        correction_type = str(request.data.get("correction_type") or "CART_CORRECTION")
        line_id = request.data.get("line_id")
        quantity_units = request.data.get("quantity_units")
        if quantity_units is not None:
            quantity_units = int(quantity_units)
        corrected = PosDomainService.correct_sale_order(
            user=request.user,
            sale=sale,
            reason=reason,
            correction_type=correction_type,
            line_id=line_id,
            quantity_units=quantity_units,
        )
        return Response(self.get_serializer(corrected).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="quick-cart")
    def quick_cart(self, request: Request) -> Response:
        """POST /api/v2/pos/sales/quick-cart/ — Gets or opens a keyboard-first DRAFT counter cart."""
        sale, created = PosSearchCartService.get_or_create_quick_cart(request.user, request.data)
        return Response(
            {
                "created": created,
                "sale": self.get_serializer(sale).data,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="scan-add")
    def scan_add(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/scan-add/ — Scans barcode/SKU and merges/adds cart line."""
        sale = self.get_object()
        updated, scan_event = PosSearchCartService.scan_add_to_cart(request.user, sale, request.data)
        return Response(
            {
                "scan_event": PosCartScanEventSerializer(scan_event).data,
                "sale": self.get_serializer(updated).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["patch"], url_path="cart-line")
    def cart_line(self, request: Request, sale_id=None) -> Response:
        """PATCH /api/v2/pos/sales/{sale_id}/cart-line/ — Keyboard qty +/-, unit toggle, or remove line."""
        sale = self.get_object()
        updated, scan_event = PosSearchCartService.mutate_cart_line(request.user, sale, request.data)
        return Response(
            {
                "scan_event": PosCartScanEventSerializer(scan_event).data,
                "sale": self.get_serializer(updated).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="unit-options")
    def unit_options(self, request: Request, sale_id=None) -> Response:
        """GET /api/v2/pos/sales/{sale_id}/unit-options/?line_id=...&product_id=..."""
        sale = self.get_object()
        payload = PosCartUnitSelectionService.get_unit_options_response(
            actor_user=request.user,
            sale=sale,
            line_id=request.query_params.get("line_id"),
            product_id=request.query_params.get("product_id"),
            variant_id=request.query_params.get("variant_id"),
        )
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="select-unit")
    def select_unit(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/select-unit/ — P223 atomic unit selection & base-unit conversion."""
        sale = self.get_object()
        try:
            updated, unit_audit = PosCartUnitSelectionService.select_cart_line_unit(
                request.user, sale, request.data
            )
        except PermissionDenied as pd:
            det = pd.detail if isinstance(pd.detail, dict) else {"detail": str(pd.detail)}
            err_code = str(det.get("error_code") or "ERR_PERMISSION_DENIED")
            msg_en = str(det.get("message_en") or det.get("detail") or "Permission denied.")
            msg_bn = str(det.get("message_bn") or "এই কাজের অনুমতি আপনার নেই।")
            return Response(
                {
                    "success": False,
                    "error_code": err_code,
                    "code": err_code,
                    "http_status": 403,
                    "message": msg_en,
                    "message_en": msg_en,
                    "message_bn": msg_bn,
                    "details": det,
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as ve:
            det = ve.detail if isinstance(ve.detail, dict) else {"detail": str(ve.detail)}
            err_code = str(det.get("error_code") or "ERR_VALIDATION_FAILED")
            first_msg = next(
                (v for k, v in det.items() if k not in ("error_code", "message_bn") and v),
                "Validation failed.",
            )
            msg_en = str(det.get("message_en") or (first_msg[0] if isinstance(first_msg, list) else first_msg))
            msg_bn = str(det.get("message_bn") or "প্যাকেজিং ইউনিট নির্বাচন সঠিক নয়।")
            return Response(
                {
                    "success": False,
                    "error_code": err_code,
                    "code": err_code,
                    "http_status": 400,
                    "message": msg_en,
                    "message_en": msg_en,
                    "message_bn": msg_bn,
                    "field_errors": det,
                    "details": det,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "unit_audit": PosCartUnitSelectionAuditSerializer(unit_audit).data,
                "sale": self.get_serializer(updated).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="unit-audits")
    def unit_audits(self, request: Request, sale_id=None) -> Response:
        """GET /api/v2/pos/sales/{sale_id}/unit-audits/ — Append-only cart unit selection audit trail."""
        sale = self.get_object()
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            request.user,
            branch_id=sale.branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        qs = (
            PosCartUnitSelectionAudit.objects.filter(tenant=tenant, sale=sale)
            .select_related("actor_user", "override_approved_by", "product")
            .order_by("-created_at", "-unit_audit_id")[:100]
        )
        return Response(PosCartUnitSelectionAuditSerializer(qs, many=True).data, status=status.HTTP_200_OK)

    @staticmethod
    def _pricing_error_response(exc: Exception, default_bn: str) -> Response:
        raw = getattr(exc, "raw_detail", None)
        if isinstance(raw, dict):
            det = raw
        else:
            det = exc.detail if isinstance(exc.detail, dict) else {"detail": str(exc.detail)}
        if isinstance(exc, PermissionDenied):
            err_code = str(det.get("error_code") or "ERR_PERMISSION_DENIED")
            msg_en = str(det.get("message_en") or det.get("detail") or "Permission denied.")
            msg_bn = str(det.get("message_bn") or "এই কাজের অনুমতি আপনার নেই।")
            return Response(
                {
                    "success": False,
                    "error_code": err_code,
                    "code": err_code,
                    "http_status": 403,
                    "message": msg_en,
                    "message_en": msg_en,
                    "message_bn": msg_bn,
                    "details": det,
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        err_code = str(det.get("error_code") or "ERR_VALIDATION_FAILED")
        first_msg = next(
            (v for k, v in det.items() if k not in ("error_code", "message_bn") and v),
            "Validation failed.",
        )
        msg_en = str(det.get("message_en") or (first_msg[0] if isinstance(first_msg, list) else first_msg))
        msg_bn = str(det.get("message_bn") or default_bn)
        return Response(
            {
                "success": False,
                "error_code": err_code,
                "code": err_code,
                "http_status": 400,
                "message": msg_en,
                "message_en": msg_en,
                "message_bn": msg_bn,
                "field_errors": det,
                "details": det,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    @action(detail=True, methods=["get", "post"], url_path="pricing-breakdown")
    def pricing_breakdown(self, request: Request, sale_id=None) -> Response:
        """GET/POST /api/v2/pos/sales/{sale_id}/pricing-breakdown/ — Reproducible itemized pricing breakdown."""
        sale = self.get_object()
        payload = PosCartPricingService.get_pricing_breakdown(request.user, sale)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="update-pricing")
    def update_pricing(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/update-pricing/ — Apply discounts, tax mode, rounding & price overrides."""
        sale = self.get_object()
        try:
            updated, breakdown, audit = PosCartPricingService.update_sale_pricing(request.user, sale, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return self._pricing_error_response(exc, "কার্টের মূল্য ও ছাড়ের তথ্য সঠিক নয়।")
        return Response(
            {
                "pricing_breakdown": breakdown,
                "pricing_audit": PosCartPricingAuditSerializer(audit).data,
                "sale": self.get_serializer(updated).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="confirm-pricing")
    def confirm_pricing(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/confirm-pricing/ — Server-side reproducible confirmation & SHA-256 hash."""
        sale = self.get_object()
        try:
            updated, breakdown, audit = PosCartPricingService.confirm_sale_pricing(request.user, sale, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return self._pricing_error_response(exc, "সার্ভারের মূল্য নিশ্চিতকরণে গরমিল পাওয়া গেছে।")
        return Response(
            {
                "pricing_confirmation": breakdown,
                "pricing_audit": PosCartPricingAuditSerializer(audit).data,
                "sale": self.get_serializer(updated).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="approve-discount")
    def approve_discount(self, request: Request, sale_id=None) -> Response:
        """POST /api/v2/pos/sales/{sale_id}/approve-discount/ — Manager approval for discounts & price overrides."""
        sale = self.get_object()
        try:
            updated, breakdown, audit = PosCartPricingService.approve_discount_or_override(
                request.user, sale, request.data
            )
        except (PermissionDenied, ValidationError) as exc:
            return self._pricing_error_response(exc, "ছাড় বা মূল্য অনুমোদনে ত্রুটি হয়েছে।")
        return Response(
            {
                "pricing_breakdown": breakdown,
                "pricing_audit": PosCartPricingAuditSerializer(audit).data,
                "sale": self.get_serializer(updated).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="pricing-audits")
    def pricing_audits(self, request: Request, sale_id=None) -> Response:
        """GET /api/v2/pos/sales/{sale_id}/pricing-audits/ — Append-only cart pricing audit trail."""
        sale = self.get_object()
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            request.user,
            branch_id=sale.branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        qs = (
            PosCartPricingAudit.objects.filter(tenant=tenant, sale=sale)
            .select_related("actor_user", "discount_approved_by")
            .order_by("-created_at", "-pricing_audit_id")[:100]
        )
        return Response(PosCartPricingAuditSerializer(qs, many=True).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="domain-metadata")
    def domain_metadata(self, request: Request) -> Response:
        """GET /api/v2/pos/sales/domain-metadata/"""
        PosDomainService.resolve_user_tenant_and_branch(request.user, require_write=False)
        payload: Dict[str, Any] = {
            "lifecycle_states": [{"code": c, "label": l} for c, l in PosSaleLifecycleState.choices],
            "allowed_transitions": {
                k: sorted(list(v)) for k, v in PosDomainService.ALLOWED_TRANSITIONS.items()
            },
            "payment_methods": [{"code": c, "label": l} for c, l in PaymentMethod.choices],
            "payment_attempt_statuses": [{"code": c, "label": l} for c, l in PosPaymentAttemptStatus.choices],
            "allocation_statuses": [{"code": c, "label": l} for c, l in PosAllocationStatus.choices],
            "discount_types": [{"code": c, "label": l} for c, l in PosDiscountType.choices],
            "tax_modes": [{"code": c, "label": l} for c, l in PosTaxMode.choices],
            "rounding_modes": [{"code": c, "label": l} for c, l in PosRoundingMode.choices],
            "packaging_units": [{"code": c, "label": l} for c, l in PosPackagingUnit.choices],
            "source_channels": [{"code": c, "label": l} for c, l in PosSaleSourceChannel.choices],
            "sync_states": [{"code": c, "label": l} for c, l in PosSyncState.choices],
            "fulfillment_modes": [{"code": c, "label": l} for c, l in PosOrderFulfillmentMode.choices],
            "delivery_methods": [{"code": c, "label": l} for c, l in PosDeliveryMethod.choices],
            "delivery_fulfillment_states": [
                {"code": c, "label": l} for c, l in PosDeliveryFulfillmentState.choices
            ],
            "cashier_discount_ceiling_pct": "5.00",
        }
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="print-receipt")
    def print_receipt(self, request: Request, sale_id=None) -> Response:
        sale = self.get_object()
        user = request.user
        PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=sale.branch_id, require_write=True, allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES
        )
        print_format = request.data.get("print_format") or PosReceiptPrintFormat.THERMAL_80MM
        printer_status = request.data.get("printer_status") or PosPrinterStatus.READY
        reprint_reason = str(request.data.get("reprint_reason") or "").strip()
        printer_name = str(request.data.get("printer_name") or "Default Thermal").strip()

        from apps.core.pos_receipt_service import PosReceiptService
        result = PosReceiptService.record_print_event(
            user=user,
            sale=sale,
            print_format=print_format,
            printer_status=printer_status,
            reprint_reason=reprint_reason,
            printer_name=printer_name,
        )
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="receipt-preview")
    def receipt_preview(self, request: Request, sale_id=None) -> Response:
        sale = self.get_object()
        user = request.user
        PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=sale.branch_id, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        print_format = request.query_params.get("print_format") or PosReceiptPrintFormat.THERMAL_80MM
        from apps.core.pos_receipt_service import PosReceiptService

        past_prints = PosReceiptPrintLog.objects.filter(tenant=sale.tenant, sale=sale).count()
        payload = PosReceiptService.build_receipt_payload(
            sale=sale,
            reprint_count=past_prints,
            print_format=print_format,
        )
        rendered_text = PosReceiptService.render_thermal_text(payload, print_format)
        return Response(
            {
                "sale_id": sale.sale_id,
                "invoice_number": payload["invoice_number"],
                "print_format": print_format,
                "reprint_count": past_prints,
                "is_duplicate": past_prints > 0,
                "watermark_text": payload["watermark_text"],
                "raw_receipt_text": rendered_text,
                "receipt_payload": payload,
            },
            status=status.HTTP_200_OK,
        )



class PosDeliveryAuditSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source="actor_user.email", read_only=True, default="")

    class Meta:
        model = PosDeliveryAudit
        fields = [
            "delivery_audit_id",
            "tenant_id",
            "branch_id",
            "delivery_id",
            "sale_id",
            "action",
            "from_state",
            "to_state",
            "reason",
            "was_rejected",
            "rejection_code",
            "rejection_message",
            "actor_email",
            "actor_role",
            "metadata_json",
            "sha256_hash",
            "created_at",
        ]
        read_only_fields = fields


class PosDeliveryOrderSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source="branch.name", read_only=True, default="")
    order_number = serializers.CharField(source="sale.order_number", read_only=True, default="")
    sale_lifecycle_state = serializers.CharField(source="sale.lifecycle_state", read_only=True, default="")
    sale_grand_total_bdt = serializers.DecimalField(
        source="sale.grand_total_bdt", max_digits=14, decimal_places=2, read_only=True
    )
    linked_invoice_id = serializers.CharField(source="sale.linked_invoice_id", read_only=True, default=None)
    requires_rx_verification = serializers.BooleanField(
        source="sale.requires_rx_verification", read_only=True, default=False
    )
    packed_by_email = serializers.CharField(source="packed_by.email", read_only=True, default="")
    dispatched_by_email = serializers.CharField(source="dispatched_by.email", read_only=True, default="")
    delivered_by_email = serializers.CharField(source="delivered_by.email", read_only=True, default="")

    class Meta:
        model = PosDeliveryOrder
        fields = [
            "delivery_id",
            "tenant_id",
            "branch_id",
            "branch_name",
            "sale_id",
            "order_number",
            "sale_lifecycle_state",
            "sale_grand_total_bdt",
            "linked_invoice_id",
            "requires_rx_verification",
            "delivery_number",
            "tracking_code",
            "idempotency_key",
            "fulfillment_mode",
            "delivery_method",
            "fulfillment_state",
            "scheduled_window_start",
            "scheduled_window_end",
            "stock_reserved",
            "reservation_expires_at",
            "recipient_name",
            "recipient_phone",
            "delivery_address_line",
            "delivery_area",
            "delivery_city",
            "delivery_landmark_notes",
            "privacy_consent_captured",
            "address_redacted_at",
            "delivery_fee_bdt",
            "cod_expected_bdt",
            "cod_collected_bdt",
            "courier_partner_name",
            "courier_reference",
            "rider_staff_id",
            "rider_name",
            "rider_phone",
            "packed_at",
            "packed_by_email",
            "dispatched_at",
            "dispatched_by_email",
            "delivered_at",
            "delivered_by_email",
            "proof_of_delivery_ref",
            "proof_recipient_relation",
            "failed_attempt_count",
            "last_failure_reason",
            "cancelled_at",
            "cancel_reason",
            "version",
            "is_archived",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def to_representation(self, instance: PosDeliveryOrder) -> Dict[str, Any]:
        rep = super().to_representation(instance)
        req = self.context.get("request")
        user = getattr(req, "user", None) if req else None
        privacy = PosDeliveryService.evaluate_privacy_payload(user, instance)
        rep.update(privacy)
        return rep


class PosDeliveryOrderViewSet(viewsets.ModelViewSet):
    """
    Canonical Preorder & Delivery Fulfillment ViewSet (/api/v2/pos/deliveries/).
    Supports authorized GET/POST/PATCH/SOFT-ARCHIVE plus explicit audited transitions:
    reserve-stock, pack, dispatch, out-for-delivery, deliver, fail-attempt,
    return-to-branch, cancel, reveal-address, redact-address, and audits.
    """

    serializer_class = PosDeliveryOrderSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "delivery_id"

    def get_queryset(self):
        user = self.request.user
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(user, require_write=False)
        include_archived = str(self.request.query_params.get("include_archived", "")).lower() in ("true", "1")
        qs = (
            PosDeliveryOrder.objects.filter(tenant=tenant)
            .select_related("branch", "sale", "rider_staff", "packed_by", "dispatched_by", "delivered_by")
        )
        if not include_archived:
            qs = qs.filter(is_archived=False)

        role = str(getattr(user, "role", "")).upper()
        actor_branch_id = getattr(user, "branch_id", None)
        if role not in PosDomainService.TENANT_WIDE_ROLES and actor_branch_id:
            qs = qs.filter(branch_id=actor_branch_id)

        branch_q = self.request.query_params.get("branch") or self.request.query_params.get("branch_id")
        if branch_q:
            PosDomainService.resolve_user_tenant_and_branch(user, branch_id=branch_q, require_write=False)
            qs = qs.filter(branch_id=branch_q)

        state_q = self.request.query_params.get("fulfillment_state")
        if state_q:
            qs = qs.filter(fulfillment_state=state_q.upper())

        mode_q = self.request.query_params.get("fulfillment_mode")
        if mode_q:
            qs = qs.filter(fulfillment_mode=mode_q.upper())

        return qs.order_by("-created_at", "-delivery_id")

    def list(self, request: Request, *args, **kwargs) -> Response:
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response({"count": len(serializer.data), "results": serializer.data})

    def create(self, request: Request, *args, **kwargs) -> Response:
        try:
            delivery, created = PosDeliveryService.create_delivery_order(request.user, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return PosSaleOrderViewSet._pricing_error_response(exc, "প্রি-অর্ডার বা ডেলিভারি তৈরি করা যায়নি।")
        return Response(
            self.get_serializer(delivery).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def update(self, request: Request, *args, **kwargs) -> Response:
        return self.partial_update(request, *args, **kwargs)

    def partial_update(self, request: Request, *args, **kwargs) -> Response:
        delivery = self.get_object()
        try:
            updated = PosDeliveryService.update_delivery_order(request.user, delivery, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return PosSaleOrderViewSet._pricing_error_response(exc, "ডেলিভারির তথ্য হালনাগাদ করা যায়নি।")
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    def destroy(self, request: Request, *args, **kwargs) -> Response:
        delivery = self.get_object()
        reason = str(request.data.get("reason") or request.query_params.get("reason") or "Archived delivery order")
        archived = PosDeliveryService.archive_delivery_order(request.user, delivery, reason=reason)
        data = dict(self.get_serializer(archived).data)
        data["message_en"] = "Delivery order archived safely."
        data["message_bn"] = "ডেলিভারি অর্ডার নিরাপদে আর্কাইভ করা হয়েছে।"
        return Response(data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reserve-stock")
    def reserve_stock(self, request: Request, delivery_id=None) -> Response:
        delivery = self.get_object()
        try:
            updated = PosDeliveryService.reserve_stock(request.user, delivery, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return PosSaleOrderViewSet._pricing_error_response(exc, "প্রি-অর্ডারের জন্য স্টক রিজার্ভ করা যায়নি।")
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="pack")
    def pack(self, request: Request, delivery_id=None) -> Response:
        delivery = self.get_object()
        try:
            updated = PosDeliveryService.pack_delivery(request.user, delivery, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return PosSaleOrderViewSet._pricing_error_response(exc, "অর্ডার প্যাক করা সম্ভব হয়নি।")
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="dispatch")
    def dispatch_order(self, request: Request, delivery_id=None) -> Response:
        delivery = self.get_object()
        try:
            updated = PosDeliveryService.dispatch_delivery(request.user, delivery, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return PosSaleOrderViewSet._pricing_error_response(exc, "ডেলিভারি ডিসপ্যাচ করা সম্ভব হয়নি।")
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="out-for-delivery")
    def out_for_delivery(self, request: Request, delivery_id=None) -> Response:
        delivery = self.get_object()
        try:
            updated = PosDeliveryService.mark_out_for_delivery(request.user, delivery, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return PosSaleOrderViewSet._pricing_error_response(exc, "আউট ফর ডেলিভারি আপডেট ব্যর্থ হয়েছে।")
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="deliver")
    def deliver(self, request: Request, delivery_id=None) -> Response:
        delivery = self.get_object()
        try:
            updated = PosDeliveryService.complete_delivery(request.user, delivery, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return PosSaleOrderViewSet._pricing_error_response(exc, "ডেলিভারি সম্পন্ন করা সম্ভব হয়নি।")
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="fail-attempt")
    def fail_attempt(self, request: Request, delivery_id=None) -> Response:
        delivery = self.get_object()
        try:
            updated = PosDeliveryService.fail_delivery_attempt(request.user, delivery, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return PosSaleOrderViewSet._pricing_error_response(exc, "ডেলিভারি ব্যর্থতার তথ্য সংরক্ষণ করা যায়নি।")
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="return-to-branch")
    def return_to_branch(self, request: Request, delivery_id=None) -> Response:
        delivery = self.get_object()
        try:
            updated = PosDeliveryService.return_to_branch(request.user, delivery, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return PosSaleOrderViewSet._pricing_error_response(exc, "শাখায় ফেরত প্রক্রিয়া সম্পন্ন করা যায়নি।")
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request: Request, delivery_id=None) -> Response:
        delivery = self.get_object()
        try:
            updated = PosDeliveryService.cancel_delivery(request.user, delivery, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return PosSaleOrderViewSet._pricing_error_response(exc, "ডেলিভারি অর্ডার বাতিল করা যায়নি।")
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reveal-address")
    def reveal_address(self, request: Request, delivery_id=None) -> Response:
        delivery = self.get_object()
        try:
            payload = PosDeliveryService.reveal_address(request.user, delivery, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return PosSaleOrderViewSet._pricing_error_response(exc, "গ্রাহকের সম্পূর্ণ ঠিকানা প্রদর্শন করা যায়নি।")
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="redact-address")
    def redact_address(self, request: Request, delivery_id=None) -> Response:
        delivery = self.get_object()
        try:
            updated = PosDeliveryService.redact_address(request.user, delivery, request.data)
        except (PermissionDenied, ValidationError) as exc:
            return PosSaleOrderViewSet._pricing_error_response(exc, "ঠিকানা গোপন করা সম্ভব হয়নি।")
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="audits")
    def audits(self, request: Request, delivery_id=None) -> Response:
        delivery = self.get_object()
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            request.user,
            branch_id=delivery.branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        qs = (
            PosDeliveryAudit.objects.filter(tenant=tenant, delivery=delivery)
            .select_related("actor_user")
            .order_by("-created_at", "-delivery_audit_id")[:100]
        )
        return Response(PosDeliveryAuditSerializer(qs, many=True).data, status=status.HTTP_200_OK)


class PosPaymentAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only audited ViewSet for POS payment attempts (/api/v2/pos/payment-attempts/)."""

    serializer_class = PosPaymentAttemptSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "attempt_id"

    def get_queryset(self):
        user = self.request.user
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(user, require_write=False)
        qs = PosPaymentAttempt.objects.filter(tenant=tenant)
        role = str(getattr(user, "role", "")).upper()
        actor_branch_id = getattr(user, "branch_id", None)
        if role not in PosDomainService.TENANT_WIDE_ROLES and actor_branch_id:
            qs = qs.filter(branch_id=actor_branch_id)
        sale_q = self.request.query_params.get("sale")
        if sale_q:
            qs = qs.filter(sale_id=sale_q)
        return qs.order_by("-attempted_at")


class PosSaleAllocationViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only ViewSet for POS batch allocations with cashier cost masking (/api/v2/pos/allocations/)."""

    serializer_class = PosSaleAllocationSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "allocation_id"

    def get_queryset(self):
        user = self.request.user
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(user, require_write=False)
        qs = PosSaleAllocation.objects.filter(tenant=tenant).select_related("product", "batch")
        role = str(getattr(user, "role", "")).upper()
        actor_branch_id = getattr(user, "branch_id", None)
        if role not in PosDomainService.TENANT_WIDE_ROLES and actor_branch_id:
            qs = qs.filter(branch_id=actor_branch_id)
        sale_q = self.request.query_params.get("sale")
        if sale_q:
            qs = qs.filter(sale_id=sale_q)
        return qs.order_by("-allocated_at")


class PosRxSafetyAuditSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source="actor_user.email", read_only=True, default="")

    class Meta:
        model = PosRxSafetyAudit
        fields = [
            "rx_audit_id",
            "tenant_id",
            "branch_id",
            "sale_id",
            "prescription_id",
            "action",
            "from_gate_status",
            "to_gate_status",
            "decision",
            "reason",
            "rx_reference_code",
            "pharmacist_pcb_no",
            "actor_email",
            "actor_role",
            "is_offline_attempt",
            "was_rejected",
            "rejection_code",
            "metadata_json",
            "sha256_hash",
            "created_at",
        ]
        read_only_fields = fields


class AntibioticDispenseRegisterSerializer(serializers.ModelSerializer):
    approving_pharmacist_email = serializers.CharField(
        source="approving_pharmacist.email", read_only=True, default=""
    )
    order_number = serializers.CharField(source="sale.order_number", read_only=True, default="")
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True, default="")

    class Meta:
        model = AntibioticDispenseRegisterEntry
        fields = [
            "register_entry_id",
            "tenant_id",
            "branch_id",
            "sale_id",
            "order_number",
            "sale_line_id",
            "invoice_id",
            "invoice_number",
            "prescription_id",
            "product_id",
            "brand_name",
            "generic_name",
            "classification_tier",
            "dispensed_base_units",
            "returned_base_units",
            "dosage_instructions",
            "duration_days",
            "rx_reference_code",
            "doctor_name",
            "doctor_bmdc_no",
            "patient_token",
            "approving_pharmacist_email",
            "approving_pharmacist_pcb_no",
            "status",
            "reversed_at",
            "reversal_reason",
            "sha256_hash",
            "dispensed_at",
            "created_at",
        ]
        read_only_fields = fields


class PosAntibioticRegisterViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only tenant/branch-scoped DGDA Antibiotic Dispensing Register (/api/v2/pos/antibiotic-register/).
    Generated idempotently on finalized antibiotic POS sales and reconciled upon reversals/returns.
    """

    serializer_class = AntibioticDispenseRegisterSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "register_entry_id"

    def get_queryset(self):
        user = self.request.user
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        qs = AntibioticDispenseRegisterEntry.objects.filter(tenant=tenant).select_related(
            "sale", "invoice", "approving_pharmacist", "product"
        )
        role = str(getattr(user, "role", "")).upper()
        actor_branch_id = getattr(user, "branch_id", None)
        if role not in PosDomainService.TENANT_WIDE_ROLES and actor_branch_id:
            qs = qs.filter(branch_id=actor_branch_id)
        branch_q = self.request.query_params.get("branch") or self.request.query_params.get("branch_id")
        if branch_q:
            PosDomainService.resolve_user_tenant_and_branch(
                user, branch_id=branch_q, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
            )
            qs = qs.filter(branch_id=branch_q)
        sale_q = self.request.query_params.get("sale")
        if sale_q:
            qs = qs.filter(sale_id=sale_q)
        status_q = self.request.query_params.get("status")
        if status_q:
            qs = qs.filter(status=status_q.upper())
        return qs.order_by("-dispensed_at", "-register_entry_id")

    def list(self, request: Request, *args, **kwargs) -> Response:
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response({"count": len(serializer.data), "results": serializer.data})

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse_entry(self, request: Request, register_entry_id=None) -> Response:
        """
        POST /api/v2/pos/antibiotic-register/{id}/reverse/
        Audited reversal or partial return of an antibiotic register record (ADR-0246).
        """
        entry = self.get_object()
        return_units = request.data.get("return_base_units")
        reason = request.data.get("reversal_reason") or request.data.get("reason", "")
        rev = AntibioticRegisterLedgerService.reverse_dispense_entry(
            request.user,
            tenant_id=str(entry.tenant_id),
            register_entry_id=entry.register_entry_id,
            return_base_units=int(return_units) if return_units is not None else None,
            reversal_reason=reason,
        )
        return Response(self.get_serializer(rev).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="dgda-export")
    def dgda_export(self, request: Request) -> Response:
        """
        GET /api/v2/pos/antibiotic-register/dgda-export/
        Produces statutory DGDA antimicrobial audit records and AWaRe summary metrics (ADR-0246).
        """
        user = request.user
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        branch_id = request.query_params.get("branch_id") or request.query_params.get("branch")
        tier = request.query_params.get("tier")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        data = AntibioticRegisterLedgerService.get_dgda_compliance_ledger(
            tenant_id=str(tenant.tenant_id),
            branch_id=branch_id,
            start_date=start_date,
            end_date=end_date,
            classification_tier=tier,
        )
        return Response(data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request: Request) -> Response:
        """
        GET /api/v2/pos/antibiotic-register/summary/
        Returns active antimicrobial dispense metrics and AWaRe tier distribution (ADR-0246).
        """
        user = request.user
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        data = AntibioticRegisterLedgerService.get_dgda_compliance_ledger(
            tenant_id=str(tenant.tenant_id),
            branch_id=request.query_params.get("branch_id"),
        )
        summary_data = {
            "total_records": data["total_records"],
            "total_dispensed_units": data["total_dispensed_units"],
            "total_returned_units": data["total_returned_units"],
            "net_active_units": data["net_active_units"],
            "aware_tier_breakdown": data["aware_tier_breakdown"],
        }
        return Response(summary_data, status=status.HTTP_200_OK)


class PosCustomerDueViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Canonical Customer Receivable & Due Collection ViewSet (/api/v2/pos/customer-dues/).
    Supports tenant/branch-scoped due tracking, repayment collection without double revenue,
    audited due write-off, and collection reversals.
    """

    serializer_class = CustomerDueRecordSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "due_id"

    def get_queryset(self):
        user = self.request.user
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        PosDomainService.assert_feature_entitlement(tenant, "CUSTOMER_LEDGER", "customer due receivables")
        qs = (
            CustomerDueRecord.objects.filter(tenant=tenant)
            .select_related("branch", "customer", "sale", "invoice")
            .prefetch_related("collections__collected_by", "collections__reversed_by", "collections__journal_entry")
        )
        role = str(getattr(user, "role", "")).upper()
        actor_branch_id = getattr(user, "branch_id", None)
        if role not in PosDomainService.TENANT_WIDE_ROLES and actor_branch_id:
            qs = qs.filter(branch_id=actor_branch_id)
        branch_q = self.request.query_params.get("branch") or self.request.query_params.get("branch_id")
        if branch_q:
            PosDomainService.resolve_user_tenant_and_branch(
                user, branch_id=branch_q, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
            )
            qs = qs.filter(branch_id=branch_q)
        cust_q = self.request.query_params.get("customer") or self.request.query_params.get("customer_id")
        if cust_q:
            qs = qs.filter(customer_id=cust_q)
        status_q = self.request.query_params.get("status")
        if status_q:
            qs = qs.filter(status=status_q.upper())
        if str(self.request.query_params.get("overdue") or "").lower() in ("true", "1"):
            qs = qs.filter(
                status__in=[DueStatus.OUTSTANDING, DueStatus.PARTIAL],
                due_date__lt=timezone.now().date(),
            )
        return qs.order_by("-created_at", "-due_id")

    def list(self, request: Request, *args, **kwargs) -> Response:
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response({"count": len(serializer.data), "results": serializer.data})

    @action(detail=True, methods=["post"], url_path="collect")
    def collect(self, request: Request, due_id=None) -> Response:
        due_rec = self.get_object()
        col, created = PosCustomerDueService.collect_customer_due(request.user, due_rec, request.data)
        due_rec.refresh_from_db()
        return Response(
            {
                "created": created,
                "idempotent_replay": not created,
                "collection": CustomerDueCollectionSerializer(col).data,
                "due_record": CustomerDueRecordSerializer(due_rec).data,
                "revenue_impact_bdt": "0.00",
                "message_en": (
                    f"Collected {col.amount_bdt:.2f} BDT against due {due_rec.due_id} (AR_CUSTOMER_DUE credited, zero double revenue)."
                    if created
                    else "Idempotent due collection replay returned existing receipt."
                ),
                "message_bn": (
                    f"বাকি {due_rec.due_id}-এ {col.amount_bdt:.2f} টাকা আদায় সম্পন্ন হয়েছে (ডাবল রেভিনিউ ছাড়া AR সমন্বিত)।"
                    if created
                    else "আইডেম্পোটেন্ট বাকি আদায়ের রসিদ প্রদান করা হয়েছে।"
                ),
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="write-off")
    def write_off(self, request: Request, due_id=None) -> Response:
        due_rec = self.get_object()
        reason = str(request.data.get("reason") or "")
        updated = PosCustomerDueService.write_off_customer_due(request.user, due_rec, reason)
        return Response(CustomerDueRecordSerializer(updated).data, status=status.HTTP_200_OK)

    @action(
        detail=False,
        methods=["post"],
        url_path=r"collections/(?P<collection_id>[^/.]+)/reverse",
    )
    def reverse_collection(self, request: Request, collection_id=None) -> Response:
        user = request.user
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        col = CustomerDueCollection.objects.filter(tenant=tenant, collection_id=collection_id).first()
        if not col:
            raise NotFound({"error_code": "ERR_COLLECTION_NOT_FOUND", "collection_id": f"Due collection '{collection_id}' not found."})
        reason = str(request.data.get("reason") or "")
        reversed_col = PosCustomerDueService.reverse_due_collection(user, col, reason)
        due_rec = CustomerDueRecord.objects.get(pk=reversed_col.due_record_id)
        return Response(
            {
                "collection": CustomerDueCollectionSerializer(reversed_col).data,
                "due_record": CustomerDueRecordSerializer(due_rec).data,
            },
            status=status.HTTP_200_OK,
        )


class PosInvoiceNumberPolicySerializer(serializers.ModelSerializer):
    next_preview = serializers.SerializerMethodField()

    class Meta:
        model = PosInvoiceNumberPolicy
        fields = [
            "policy_id",
            "tenant_id",
            "branch_id",
            "prefix",
            "pattern",
            "reset_frequency",
            "padding_digits",
            "offline_prefix",
            "allow_offline_preallocation",
            "is_active",
            "next_preview",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["policy_id", "tenant_id", "next_preview", "created_at", "updated_at"]

    def get_next_preview(self, obj: PosInvoiceNumberPolicy) -> str:
        from apps.core.pos_invoice_number_service import PosInvoiceNumberService
        branch = obj.branch
        if not branch:
            branch = Branch.objects.filter(tenant=obj.tenant).first()
        if not branch:
            return f"{obj.prefix}-PREVIEW"
        try:
            preview = PosInvoiceNumberService.preview_next_invoice_number(
                tenant=obj.tenant, branch=branch, device_namespace="ONLINE"
            )
            return preview.get("invoice_number", "")
        except Exception:
            return f"{obj.prefix}-PREVIEW"


class PosInvoiceNumberPolicyViewSet(viewsets.ModelViewSet):
    """
    Architecture Decision: ADR-0231 (P231).
    Tenant and branch-scoped collision-resistant invoice numbering policies and sequence previews.
    """

    serializer_class = PosInvoiceNumberPolicySerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "policy_id"

    def get_queryset(self):
        user = self.request.user
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        qs = PosInvoiceNumberPolicy.objects.filter(tenant=tenant)
        if branch:
            qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=True, allowed_roles=PosDomainService.POS_SUPERVISOR_ROLES
        )
        serializer.save(tenant=tenant, branch=branch)

    @action(detail=False, methods=["get"], url_path="preview-next")
    def preview_next(self, request: Request) -> Response:
        user = request.user
        tenant, default_branch = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        branch_id = request.query_params.get("branch_id")
        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first() if branch_id else default_branch
        if not branch:
            branch = Branch.objects.filter(tenant=tenant).first()
        if not branch:
            raise ValidationError({"branch": "Branch could not be resolved."})

        device_ns = request.query_params.get("device") or "ONLINE"
        from apps.core.pos_invoice_number_service import PosInvoiceNumberService
        res = PosInvoiceNumberService.preview_next_invoice_number(
            tenant=tenant, branch=branch, device_namespace=device_ns
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="reserve-next")
    def reserve_next(self, request: Request) -> Response:
        user = request.user
        tenant, default_branch = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=True, allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES
        )
        branch_id = request.data.get("branch_id")
        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first() if branch_id else default_branch
        if not branch:
            branch = Branch.objects.filter(tenant=tenant).first()
        if not branch:
            raise ValidationError({"branch": "Branch could not be resolved."})

        device_ns = request.data.get("device") or "ONLINE"
        from apps.core.pos_invoice_number_service import PosInvoiceNumberService
        res = PosInvoiceNumberService.generate_next_invoice_number_in_tx(
            tenant=tenant, branch=branch, device_namespace=device_ns, reserve=True
        )
        return Response(res, status=status.HTTP_201_CREATED)


class PosReceiptPrintLogSerializer(serializers.ModelSerializer):
    printed_by_email = serializers.CharField(source="printed_by.email", read_only=True, default="")

    class Meta:
        model = PosReceiptPrintLog
        fields = [
            "print_id",
            "tenant_id",
            "branch_id",
            "sale_id",
            "invoice_id",
            "print_format",
            "job_type",
            "reprint_count",
            "is_duplicate",
            "watermark_text",
            "printer_name",
            "printer_status",
            "reprint_reason",
            "raw_receipt_text",
            "receipt_payload_json",
            "printed_by_email",
            "printed_at",
        ]
        read_only_fields = fields


class PosReceiptPrintLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Architecture Decision: ADR-0232 (P232).
    Audited read-only view of POS receipt prints, reprints, watermarks and printer statuses.
    """

    serializer_class = PosReceiptPrintLogSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "print_id"

    def get_queryset(self):
        user = self.request.user
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        qs = PosReceiptPrintLog.objects.filter(tenant=tenant)
        if branch:
            qs = qs.filter(branch=branch)
        sale_id = self.request.query_params.get("sale_id")
        if sale_id:
            qs = qs.filter(sale_id=sale_id)
        return qs


# =============================================================================
# P233: Cashier Shift Serializers & ViewSet
# =============================================================================


class PosShiftCashMovementSerializer(serializers.ModelSerializer):
    created_by_email = serializers.CharField(source="created_by.email", read_only=True, default="")

    class Meta:
        model = PosShiftCashMovement
        fields = [
            "movement_id",
            "tenant_id",
            "branch_id",
            "shift_id",
            "movement_type",
            "amount_bdt",
            "reason",
            "recipient_or_source",
            "created_by_id",
            "created_by_email",
            "created_at",
        ]
        read_only_fields = ["movement_id", "tenant_id", "branch_id", "shift_id", "created_by_id", "created_at"]


class PosCashierShiftSerializer(serializers.ModelSerializer):
    cashier_email = serializers.CharField(source="cashier_user.email", read_only=True, default="")
    cashier_name = serializers.CharField(source="cashier_user.full_name", read_only=True, default="")
    approved_by_email = serializers.CharField(source="approved_by.email", read_only=True, default="")
    cash_movements = PosShiftCashMovementSerializer(many=True, read_only=True)

    class Meta:
        model = PosCashierShift
        fields = [
            "shift_id",
            "shift_number",
            "tenant_id",
            "branch_id",
            "cashier_user_id",
            "cashier_email",
            "cashier_name",
            "device_code",
            "status",
            "opening_float_bdt",
            "total_cash_sales_bdt",
            "total_card_sales_bdt",
            "total_digital_mfs_sales_bdt",
            "total_credit_sales_bdt",
            "total_cash_refunds_bdt",
            "total_payouts_bdt",
            "total_cash_in_bdt",
            "expected_drawer_cash_bdt",
            "closing_cash_counted_bdt",
            "variance_bdt",
            "variance_reason",
            "requires_manager_approval",
            "approved_by_id",
            "approved_by_email",
            "manager_approval_notes",
            "opened_at",
            "closed_at",
            "reconciled_at",
            "cash_movements",
        ]
        read_only_fields = [
            "shift_id",
            "shift_number",
            "tenant_id",
            "branch_id",
            "cashier_user_id",
            "status",
            "total_cash_sales_bdt",
            "total_card_sales_bdt",
            "total_digital_mfs_sales_bdt",
            "total_credit_sales_bdt",
            "total_cash_refunds_bdt",
            "total_payouts_bdt",
            "total_cash_in_bdt",
            "expected_drawer_cash_bdt",
            "variance_bdt",
            "requires_manager_approval",
            "approved_by_id",
            "opened_at",
            "closed_at",
            "reconciled_at",
        ]


class PosCashierShiftViewSet(viewsets.ModelViewSet):
    """
    Architecture Decision: ADR-0233 (P233).
    API ViewSet for opening/closing cashier shifts, recording drawer cash movements,
    and reconciling variances.
    """

    serializer_class = PosCashierShiftSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "shift_id"

    def get_queryset(self):
        user = self.request.user
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        qs = PosCashierShift.objects.filter(tenant=tenant).prefetch_related("cash_movements")
        if branch:
            qs = qs.filter(branch=branch)
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param.upper())
        return qs

    @action(detail=False, methods=["post"], url_path="open")
    def open_shift(self, request: Request) -> Response:
        user = request.user
        branch_id = request.data.get("branch_id")
        device_code = request.data.get("device_code", "DEFAULT")
        opening_float_bdt = request.data.get("opening_float_bdt", "0.00")

        from apps.core.pos_shift_service import PosShiftService
        shift = PosShiftService.open_shift(
            user=user,
            branch_id=branch_id,
            device_code=device_code,
            opening_float_bdt=opening_float_bdt,
        )
        serializer = self.get_serializer(shift)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"], url_path="active")
    def get_active_shift(self, request: Request) -> Response:
        user = request.user
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        shift = PosCashierShift.objects.filter(
            tenant=tenant,
            cashier_user=user,
            status=PosShiftStatus.OPEN,
        ).prefetch_related("cash_movements").first()

        if not shift:
            return Response(
                {"error_code": "ERR_NO_ACTIVE_SHIFT", "message": "No active shift open for this cashier."},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = self.get_serializer(shift)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="cash-movement")
    def record_cash_movement(self, request: Request, shift_id=None) -> Response:
        shift = self.get_object()
        user = request.user
        movement_type = request.data.get("movement_type")
        amount_bdt = request.data.get("amount_bdt")
        reason = request.data.get("reason")
        recipient = request.data.get("recipient_or_source", "")

        from apps.core.pos_shift_service import PosShiftService
        movement = PosShiftService.record_cash_movement(
            user=user,
            shift_id=shift.shift_id,
            movement_type=movement_type,
            amount_bdt=amount_bdt,
            reason=reason,
            recipient_or_source=recipient,
        )
        return Response(PosShiftCashMovementSerializer(movement).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="close")
    def close_shift(self, request: Request, shift_id=None) -> Response:
        shift = self.get_object()
        user = request.user
        closing_cash_counted_bdt = request.data.get("closing_cash_counted_bdt")
        variance_reason = request.data.get("variance_reason", "")

        from apps.core.pos_shift_service import PosShiftService
        closed = PosShiftService.close_shift(
            user=user,
            shift_id=shift.shift_id,
            closing_cash_counted_bdt=closing_cash_counted_bdt,
            variance_reason=variance_reason,
        )
        return Response(self.get_serializer(closed).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="approve-variance")
    def approve_variance(self, request: Request, shift_id=None) -> Response:
        shift = self.get_object()
        user = request.user
        manager_notes = request.data.get("manager_approval_notes", "")

        from apps.core.pos_shift_service import PosShiftService
        reconciled = PosShiftService.approve_variance(
            user=user,
            shift_id=shift.shift_id,
            manager_notes=manager_notes,
        )
        return Response(self.get_serializer(reconciled).data, status=status.HTTP_200_OK)


# =============================================================================
# P234: POS Sales Return & Refund Serializers & ViewSet
# =============================================================================


class PosSaleReturnLineSerializer(serializers.ModelSerializer):
    product_brand_name = serializers.CharField(source="product.brand_name", read_only=True, default="")
    generic_name = serializers.CharField(source="product.generic_name", read_only=True, default="")

    class Meta:
        model = PosSaleReturnLine
        fields = [
            "return_line_id",
            "sale_line_id",
            "product_id",
            "product_brand_name",
            "generic_name",
            "original_batch_id",
            "batch_number",
            "packaging_unit",
            "conversion_factor",
            "quantity_units",
            "base_units_returned",
            "unit_price_bdt",
            "refund_amount_bdt",
            "safety_disposition",
            "disposition_notes",
            "created_at",
        ]
        read_only_fields = [
            "return_line_id",
            "base_units_returned",
            "unit_price_bdt",
            "refund_amount_bdt",
            "created_at",
        ]


class PosSaleReturnSerializer(serializers.ModelSerializer):
    processed_by_email = serializers.CharField(source="processed_by.email", read_only=True, default="")
    processed_by_name = serializers.CharField(source="processed_by.full_name", read_only=True, default="")
    original_order_number = serializers.CharField(source="original_sale.order_number", read_only=True, default="")
    lines = PosSaleReturnLineSerializer(many=True, read_only=True)

    class Meta:
        model = PosSaleReturn
        fields = [
            "return_id",
            "return_number",
            "tenant_id",
            "branch_id",
            "original_sale_id",
            "original_order_number",
            "shift_id",
            "processed_by_id",
            "processed_by_email",
            "processed_by_name",
            "status",
            "refund_method",
            "total_refund_amount_bdt",
            "reason",
            "customer_name",
            "customer_phone",
            "created_at",
            "lines",
        ]
        read_only_fields = [
            "return_id",
            "return_number",
            "tenant_id",
            "branch_id",
            "shift_id",
            "processed_by_id",
            "status",
            "total_refund_amount_bdt",
            "created_at",
        ]


class PosSaleReturnViewSet(viewsets.ModelViewSet):
    """
    Architecture Decision: ADR-0234 (P234).
    Audited ViewSet for processing POS item returns, lot tracking, safety dispositions,
    and cash drawer refund reconciliations.
    """

    serializer_class = PosSaleReturnSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "return_id"

    def get_queryset(self):
        user = self.request.user
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        qs = PosSaleReturn.objects.filter(tenant=tenant).prefetch_related("lines__product", "lines__original_batch")
        if branch:
            qs = qs.filter(branch=branch)
        sale_id = self.request.query_params.get("sale_id")
        if sale_id:
            qs = qs.filter(original_sale_id=sale_id)
        return qs

    def create(self, request: Request, *args, **kwargs) -> Response:
        user = request.user
        sale_id = request.data.get("sale_id") or request.data.get("original_sale_id")
        lines_data = request.data.get("lines") or []
        refund_method = request.data.get("refund_method") or PosRefundMethod.CASH
        reason = request.data.get("reason") or ""
        customer_name = request.data.get("customer_name") or ""
        customer_phone = request.data.get("customer_phone") or ""

        from apps.core.pos_return_service import PosReturnService
        sale_return = PosReturnService.process_return(
            user=user,
            sale_id=sale_id,
            lines_data=lines_data,
            refund_method=refund_method,
            reason=reason,
            customer_name=customer_name,
            customer_phone=customer_phone,
        )
        serializer = self.get_serializer(sale_return)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class PosDailySalesCloseSerializer(serializers.ModelSerializer):
    """
    Architecture Decision: ADR-0237 (P237).
    Serializer for branch daily sales close totals, split tenders, returns,
    drawer cash reconciliation, and ledger balance verification.
    """

    class Meta:
        model = PosDailySalesClose
        fields = [
            "close_id",
            "close_number",
            "tenant_id",
            "branch_id",
            "closing_date",
            "status",
            "sales_count",
            "gross_sales_bdt",
            "discount_total_bdt",
            "tax_total_bdt",
            "net_sales_bdt",
            "cash_sales_bdt",
            "digital_sales_bdt",
            "due_sales_bdt",
            "returns_count",
            "returns_total_bdt",
            "cash_refunds_bdt",
            "digital_refunds_bdt",
            "due_collections_cash_bdt",
            "due_collections_digital_bdt",
            "shifts_closed_count",
            "unclosed_shifts_count",
            "opening_float_total_bdt",
            "cash_in_movements_bdt",
            "cash_payouts_bdt",
            "expected_drawer_cash_bdt",
            "actual_drawer_cash_bdt",
            "cash_variance_bdt",
            "ledger_entries_count",
            "ledger_debit_total_bdt",
            "ledger_credit_total_bdt",
            "is_ledger_balanced",
            "prepared_by_id",
            "approved_by_id",
            "notes",
            "created_at",
            "submitted_at",
            "approved_at",
            "sha256_hash",
        ]
        read_only_fields = [
            "close_id",
            "close_number",
            "tenant_id",
            "branch_id",
            "sha256_hash",
            "created_at",
            "submitted_at",
            "approved_at",
        ]


class PosDailyCloseViewSet(viewsets.ModelViewSet):
    """
    Architecture Decision: ADR-0237 (P237).
    Audited ViewSet for compiling, submitting, and approving branch daily sales closes.
    """

    serializer_class = PosDailySalesCloseSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "close_id"

    def get_queryset(self):
        user = self.request.user
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        qs = PosDailySalesClose.objects.filter(tenant=tenant)
        if branch:
            qs = qs.filter(branch=branch)
        c_date = self.request.query_params.get("date")
        if c_date:
            qs = qs.filter(closing_date=c_date)
        return qs

    @action(detail=False, methods=["get"], url_path="summary")
    def live_summary(self, request: Request) -> Response:
        """
        Compiles live draft aggregations for the specified branch and calendar date.
        """
        from apps.core.pos_daily_close_service import PosDailyCloseService
        user = request.user
        c_date = request.query_params.get("date")
        summary = PosDailyCloseService.compile_daily_summary(
            user=user,
            closing_date=c_date,
        )
        # Drop model instances before JSON serialize
        resp_data = {k: v for k, v in summary.items() if k not in ("tenant", "branch")}
        resp_data["tenant_id"] = summary["tenant"].tenant_id
        resp_data["branch_id"] = summary["branch"].branch_id
        resp_data["closing_date"] = summary["closing_date"].strftime("%Y-%m-%d")
        return Response(resp_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="submit")
    def submit_close(self, request: Request) -> Response:
        """
        Submits physical cash counted and records draft/submitted daily close.
        """
        from apps.core.pos_daily_close_service import PosDailyCloseService
        user = request.user
        closing_date = request.data.get("closing_date")
        actual_cash = request.data.get("actual_drawer_cash_bdt", "0.00")
        notes = request.data.get("notes", "")

        close_inst = PosDailyCloseService.submit_daily_close(
            user=user,
            closing_date=closing_date,
            actual_drawer_cash_bdt=actual_cash,
            notes=notes,
        )
        serializer = self.get_serializer(close_inst)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve_close(self, request: Request, close_id: str = None) -> Response:
        """
        Manager approval and sealing of daily close record.
        """
        from apps.core.pos_daily_close_service import PosDailyCloseService
        close_obj = self.get_object()
        user = request.user
        notes = request.data.get("manager_notes", "")
        close_inst = PosDailyCloseService.approve_daily_close(
            user=user,
            close_id=close_obj.close_id,
            manager_notes=notes,
        )
        serializer = self.get_serializer(close_inst)
        return Response(serializer.data, status=status.HTTP_200_OK)





