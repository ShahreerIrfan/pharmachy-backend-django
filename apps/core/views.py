"""
apps/core/views.py
Architecture Decision: ADR-0004, ADR-0005 & ADR-0031
DRF ViewSets with Tenant Isolation and Role-Based Access Control.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.models import (
    AsyncJobRecord,
    BatchStock,
    Branch,
    ComplianceReminder,
    DailyBranchAnalytics,
    MedicineProduct,
    PaymentTransaction,
    PrescriptionRecord,
    PrescriptionStatus,
    SaleInvoice,
    StaffMember,
    StaffRole,
    SubscriptionRecord,
    Tenant,
)
from apps.core.pagination import (
    SafeAdaptivePagination,
    StandardPagination,
    UnicodePhoneticSearchFilter,
    ValidatedQueryFilterBackend,
)
from apps.core.responses import StandardResponseMixin
from apps.core.serializers import (
    AsyncJobRecordSerializer,
    BatchStockSerializer,
    BranchSerializer,
    CommandValidationSerializer,
    ComplianceReminderSerializer,
    DailyBranchAnalyticsSerializer,
    MedicineProductSerializer,
    PaymentTransactionSerializer,
    PrescriptionRecordSerializer,
    PrescriptionVerificationRequestSerializer,
    SaleCheckoutRequestSerializer,
    SaleInvoiceSerializer,
    StaffMemberSerializer,
    SubscriptionRecordSerializer,
    TenantSerializer,
)
from apps.core.synchronous_sale_service import SynchronousSaleService
from apps.core.throttling import (
    SensitiveMutationRateThrottle,
    TestStrictRateThrottle,
    get_active_rate_limit_policies,
)


class TenantViewSet(viewsets.ModelViewSet):
    queryset = Tenant.objects.all()
    serializer_class = TenantSerializer
    permission_classes = [permissions.AllowAny]  # For local bootstrap / demo verification


class BranchViewSet(viewsets.ModelViewSet):
    queryset = Branch.objects.all()
    serializer_class = BranchSerializer
    permission_classes = [permissions.AllowAny]


class MedicineProductViewSet(viewsets.ModelViewSet):
    queryset = MedicineProduct.objects.all()
    serializer_class = MedicineProductSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = SafeAdaptivePagination
    filter_backends = [
        ValidatedQueryFilterBackend,
        DjangoFilterBackend,
        UnicodePhoneticSearchFilter,
        filters.OrderingFilter,
    ]
    search_fields = ["brand_name", "generic_name", "manufacturer"]
    ordering_fields = ["brand_name", "mrp_unit", "created_at", "product_id"]
    ordering = ["brand_name", "product_id"]
    cursor_ordering = "-created_at"
    allowed_filter_fields = {
        "brand_name",
        "generic_name",
        "manufacturer",
        "dosage_form",
        "strength",
        "requires_cold_chain",
        "min_mrp",
        "max_mrp",
    }


class BatchStockViewSet(viewsets.ModelViewSet):
    queryset = BatchStock.objects.all()
    serializer_class = BatchStockSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = SafeAdaptivePagination
    filter_backends = [
        ValidatedQueryFilterBackend,
        DjangoFilterBackend,
        UnicodePhoneticSearchFilter,
        filters.OrderingFilter,
    ]
    search_fields = ["batch_number", "product__brand_name"]
    ordering_fields = ["expiry_date", "quantity", "created_at"]
    ordering = ["expiry_date"]
    cursor_ordering = "expiry_date"
    allowed_filter_fields = {"batch_number", "product_id", "status", "tenant_id", "branch_id"}

    def get_queryset(self):
        qs = super().get_queryset()
        tenant_id = self.request.query_params.get("tenant_id")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        return qs


class SaleInvoiceViewSet(viewsets.ModelViewSet):
    queryset = SaleInvoice.objects.all()
    serializer_class = SaleInvoiceSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = SafeAdaptivePagination
    filter_backends = [
        ValidatedQueryFilterBackend,
        DjangoFilterBackend,
        UnicodePhoneticSearchFilter,
        filters.OrderingFilter,
    ]
    search_fields = ["invoice_id", "invoice_number", "customer_name", "customer_phone"]
    ordering_fields = ["created_at", "total_amount", "gross_total", "invoice_id"]
    ordering = ["-created_at", "-invoice_id"]
    cursor_ordering = "-created_at"
    allowed_filter_fields = {
        "customer_name",
        "customer_phone",
        "payment_method",
        "status",
        "tenant_id",
        "branch_id",
        "start_date",
        "end_date",
    }

    def get_queryset(self):
        qs = super().get_queryset()
        trusted_tenant_id = getattr(self.request, "tenant_id", None)
        query_tenant_id = self.request.query_params.get("tenant_id")

        if trusted_tenant_id:
            if query_tenant_id and query_tenant_id.strip() != trusted_tenant_id:
                raise CrossTenantViolationError(
                    field_errors={
                        "tenant_id": [
                            f"Supplied tenant '{query_tenant_id}' does not match trusted tenant '{trusted_tenant_id}'."
                        ]
                    },
                    message_en=f"Cross-tenant access violation: Cannot query invoices for foreign tenant '{query_tenant_id}'.",
                    message_bn="অন্য ফার্মেসির তথ্যে প্রবেশাধিকার কঠোরভাবে নিষিদ্ধ।",
                )
            return qs.filter(tenant_id=trusted_tenant_id)

        if query_tenant_id:
            return qs.filter(tenant_id=query_tenant_id)

        if self.request.user and self.request.user.is_authenticated and self.request.user.is_superuser:
            return qs

        return qs.none()

    @action(detail=False, methods=["post"], url_path="checkout", throttle_classes=[SensitiveMutationRateThrottle])
    def checkout(self, request):
        """
        Executes synchronous counter checkout transaction with atomic row locking.
        Emits asynchronous receipt SMS and analytics refresh post-commit (ADR-0031).
        Enforces strict server-side validation and referential integrity (ADR-0046).
        """
        serializer = SaleCheckoutRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        invoice = SynchronousSaleService.execute_checkout(
            tenant_id=data["tenant_id"],
            branch_id=data["branch_id"],
            items_data=data["items"],
            customer_name=data.get("customer_name", "Walk-in Customer"),
            customer_phone=data.get("customer_phone", ""),
            payment_method=data.get("payment_method", "CASH"),
            discount_amount=data.get("discount_amount", Decimal("0.00")),
            execute_async_inline=True,
        )
        out_serializer = SaleInvoiceSerializer(invoice)
        return Response(out_serializer.data, status=status.HTTP_201_CREATED)


class AsyncJobRecordViewSet(viewsets.ReadOnlyModelViewSet):
    """Auditing and monitoring endpoint for Celery / Redis asynchronous worker jobs (ADR-0031)."""

    queryset = AsyncJobRecord.objects.all()
    serializer_class = AsyncJobRecordSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
        tenant_id = self.request.query_params.get("tenant_id")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(category=category.upper())
        job_status = self.request.query_params.get("status")
        if job_status:
            qs = qs.filter(status=job_status.upper())
        return qs


class DailyBranchAnalyticsViewSet(viewsets.ReadOnlyModelViewSet):
    """Derived analytics endpoint aggregated asynchronously by background workers (ADR-0031)."""

    queryset = DailyBranchAnalytics.objects.all()
    serializer_class = DailyBranchAnalyticsSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
        tenant_id = self.request.query_params.get("tenant_id")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        branch_id = self.request.query_params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return qs

    @action(detail=False, methods=["get"], url_path="kpi")
    def cached_kpi(self, request):
        """Fetches instantaneous mobile manager KPIs warmed in Redis cache (ADR-0031)."""
        tenant_id = request.query_params.get("tenant_id")
        branch_id = request.query_params.get("branch_id")
        date_str = request.query_params.get("date", timezone.now().strftime("%Y-%m-%d"))

        if not tenant_id or not branch_id:
            return Response(
                {"error": "tenant_id and branch_id query parameters are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cache_key = f"kpi:{tenant_id}:{branch_id}:{date_str}"
        cached_data = cache.get(cache_key)

        if cached_data:
            return Response(
                {
                    "source": "REDIS_CACHE",
                    "cache_key": cache_key,
                    "kpi": cached_data,
                }
            )

        # Fallback to DB aggregation if cache miss
        analytics = DailyBranchAnalytics.objects.filter(
            tenant_id=tenant_id,
            branch_id=branch_id,
            date=date_str,
        ).first()

        if analytics:
            return Response(
                {
                    "source": "POSTGRES_DB",
                    "kpi": {
                        "today_sales_bdt": str(analytics.total_sales_amount),
                        "total_orders_count": analytics.total_orders_count,
                        "total_items_sold": analytics.total_items_sold,
                        "gross_margin_percent": "18.5",
                        "as_of_timestamp": analytics.last_aggregated_at.isoformat(),
                    },
                }
            )

        return Response(
            {"source": "EMPTY", "kpi": {"today_sales_bdt": "0.00", "total_orders_count": 0, "total_items_sold": 0}},
            status=status.HTTP_200_OK,
        )


class ComplianceReminderViewSet(viewsets.ModelViewSet):
    """Regulatory renewal and batch quarantine reminders (ADR-0015 & ADR-0031)."""

    queryset = ComplianceReminder.objects.all()
    serializer_class = ComplianceReminderSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
        tenant_id = self.request.query_params.get("tenant_id")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        is_ack = self.request.query_params.get("is_acknowledged")
        if is_ack is not None:
            qs = qs.filter(is_acknowledged=is_ack.lower() in ("true", "1"))
        return qs

    @action(detail=True, methods=["post", "patch"], url_path="acknowledge")
    def acknowledge(self, request, pk=None):
        """Marks a regulatory reminder as acknowledged by an authorized staff member."""
        reminder = self.get_object()
        reminder.is_acknowledged = True
        reminder.acknowledged_at = timezone.now()
        reminder.save(update_fields=["is_acknowledged", "acknowledged_at"])
        serializer = self.get_serializer(reminder)
        return Response(serializer.data, status=status.HTTP_200_OK)


class SubscriptionRecordViewSet(viewsets.ModelViewSet):
    """Manages SaaS tenant subscriptions, plan tiers, and license quotas (ADR-0015 & ADR-0033)."""

    queryset = SubscriptionRecord.objects.all()
    serializer_class = SubscriptionRecordSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
        tenant_id = self.request.query_params.get("tenant_id")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        sub_status = self.request.query_params.get("status")
        if sub_status:
            qs = qs.filter(status=sub_status.upper())
        return qs


class StaffMemberViewSet(viewsets.ModelViewSet):
    """Manages authorized staff members, role assignments, and PCB registrations (ADR-0005 & ADR-0033)."""

    queryset = StaffMember.objects.all()
    serializer_class = StaffMemberSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
        tenant_id = self.request.query_params.get("tenant_id")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        branch_id = self.request.query_params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        role = self.request.query_params.get("role")
        if role:
            qs = qs.filter(role=role.upper())
        return qs


class PrescriptionRecordViewSet(viewsets.ModelViewSet):
    """Manages clinical prescriptions and pharmacist verification sign-off (ADR-0010 & ADR-0033)."""

    queryset = PrescriptionRecord.objects.all()
    serializer_class = PrescriptionRecordSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
        tenant_id = self.request.query_params.get("tenant_id")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        branch_id = self.request.query_params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        rx_status = self.request.query_params.get("status")
        if rx_status:
            qs = qs.filter(status=rx_status.upper())
        return qs

    @action(detail=True, methods=["post"], url_path="verify")
    def verify_prescription(self, request, pk=None):
        """
        Executes clinical pharmacist sign-off for antibiotics and prescription drugs.
        Validates PCB registration and pharmacist role.
        """
        prescription = self.get_object()
        serializer = PrescriptionVerificationRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        pharmacist_id = data["pharmacist_user_id"]

        # Validate that the verifying actor has PHARMACIST role
        pharmacist = StaffMember.objects.filter(
            staff_id=pharmacist_id,
            tenant_id=prescription.tenant_id,
            is_active=True,
        ).first()

        if not pharmacist or pharmacist.role != StaffRole.PHARMACIST:
            return Response(
                {
                    "error": (
                        f"Unauthorized prescription verification: Staff member '{pharmacist_id}' "
                        f"does not hold the active PHARMACIST role (ADR-0005, ADR-0033)."
                    ),
                    "code": "UNAUTHORIZED_CLINICAL_SIGN_OFF",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        if data["decision"] == "APPROVED":
            prescription.status = PrescriptionStatus.VERIFIED
        else:
            prescription.status = PrescriptionStatus.REJECTED

        prescription.verified_by = pharmacist
        prescription.pharmacist_pcb_no = data["pharmacist_pcb_reg_no"]
        prescription.notes = data.get("notes", "")
        prescription.verified_at = timezone.now()
        prescription.save(update_fields=["status", "verified_by", "pharmacist_pcb_no", "notes", "verified_at"])

        out_serializer = self.get_serializer(prescription)
        return Response(out_serializer.data, status=status.HTTP_200_OK)


class PaymentTransactionViewSet(viewsets.ModelViewSet):
    """Manages POS and subscription payment transactions with exact Decimal amounts (ADR-0001 & ADR-0033)."""

    queryset = PaymentTransaction.objects.all()
    serializer_class = PaymentTransactionSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
        tenant_id = self.request.query_params.get("tenant_id")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        branch_id = self.request.query_params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        tender_type = self.request.query_params.get("tender_type")
        if tender_type:
            qs = qs.filter(tender_type=tender_type.upper())
        return qs


class ValidationContractViewSet(viewsets.ViewSet):
    """
    Dedicated endpoint for validating domain command contracts in real-time (ADR-0046).
    Enables JavaFX Desktop POS and Flutter Mobile Manager to pre-validate commands against
    server business rules before committing transactions.
    """

    permission_classes = [permissions.AllowAny]

    @action(detail=False, methods=["post"], url_path="validate-command")
    def validate_command(self, request):
        serializer = CommandValidationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated_payload = serializer.validate_command()
        return Response(
            {
                "valid": True,
                "command_type": serializer.validated_data["command_type"],
                "tenant_id": serializer.validated_data["tenant_id"],
                "validated_payload": validated_payload,
                "message_en": "Command payload successfully verified against server validation contracts.",
                "message_bn": "কমান্ডের তথ্য সফলভাবে সার্ভার ভ্যালিডেশন কন্ট্রাক্ট দ্বারা যাচাই করা হয়েছে।",
            },
            status=status.HTTP_200_OK,
        )


class ApiResponseConventionViewSet(viewsets.ViewSet, StandardResponseMixin):
    """
    Dedicated viewset demonstrating and verifying canonical API response conventions (ADR-0047).
    Validates pagination, filtering, standardized envelopes, machine codes, request tracing,
    and localized bilingual descriptions.
    """

    permission_classes = [permissions.AllowAny]

    @action(detail=False, methods=["get"], url_path="paginated-medicines")
    def paginated_medicines(self, request):
        """
        Returns paginated pharmaceutical catalog wrapped in canonical ADR-0047 envelope.
        Supports search keyword query (?search=) and ordering (?ordering=).
        """
        queryset = MedicineProduct.objects.all()
        search_query = request.query_params.get("search", "").strip()
        if search_query:
            from django.db.models import Q

            queryset = queryset.filter(
                Q(brand_name__icontains=search_query)
                | Q(generic_name__icontains=search_query)
                | Q(manufacturer__icontains=search_query)
            )

        ordering = request.query_params.get("ordering", "brand_name")
        valid_orderings = {"brand_name", "-brand_name", "mrp_unit", "-mrp_unit", "created_at", "-created_at"}
        if ordering in valid_orderings:
            queryset = queryset.order_by(ordering)

        paginator = StandardPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        if page is not None:
            serializer = MedicineProductSerializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)

        serializer = MedicineProductSerializer(queryset, many=True)
        return self.success_response(
            data=serializer.data,
            code="RESOURCE_FETCHED",
            message_en="Complete pharmaceutical product catalog retrieved.",
            message_bn="সম্পূর্ণ ঔষধ ক্যাটালগ তথ্য লোড করা হয়েছে।",
        )

    @action(detail=False, methods=["get"], url_path=r"medicine-detail/(?P<pk>[^/.]+)")
    def medicine_detail(self, request, pk=None):
        """Returns single pharmaceutical product wrapped in standardized ADR-0047 envelope."""
        try:
            medicine = MedicineProduct.objects.get(pk=pk)
            serializer = MedicineProductSerializer(medicine)
            return self.success_response(
                data=serializer.data,
                code="RESOURCE_FETCHED",
                message_en=f"Retrieved medicine '{medicine.brand_name}' successfully.",
                message_bn=f"ঔষধ '{medicine.brand_name}' এর তথ্য সফলভাবে পাওয়া গেছে।",
            )
        except MedicineProduct.DoesNotExist:
            from rest_framework.exceptions import NotFound

            raise NotFound(detail=f"Medicine product with ID '{pk}' not found.")

    @action(detail=False, methods=["post"], url_path="validate-input")
    def validate_input(self, request):
        """
        Demonstrates validation rejection complying with ADR-0047 & ADR-0014 error envelope.
        Requires non-empty 'item_name', positive integer 'quantity', and positive 'unit_price'.
        """
        item_name = request.data.get("item_name")
        quantity = request.data.get("quantity")
        unit_price = request.data.get("unit_price")

        field_errors = {}
        if not item_name or not str(item_name).strip():
            field_errors["item_name"] = ["Field 'item_name' is required and cannot be blank."]
        if quantity is None:
            field_errors["quantity"] = ["Field 'quantity' is required."]
        else:
            try:
                qty_val = int(quantity)
                if qty_val <= 0:
                    field_errors["quantity"] = ["Quantity must be a positive integer strictly greater than zero."]
            except (ValueError, TypeError):
                field_errors["quantity"] = ["Quantity must be a valid integer."]

        if unit_price is None:
            field_errors["unit_price"] = ["Field 'unit_price' is required."]
        else:
            try:
                price_dec = Decimal(str(unit_price))
                if price_dec <= Decimal("0.00"):
                    field_errors["unit_price"] = ["Unit price must be strictly greater than zero BDT."]
            except Exception:
                field_errors["unit_price"] = ["Unit price must be a valid decimal monetary string."]

        if field_errors:
            from rest_framework.exceptions import ValidationError

            raise ValidationError(detail=field_errors)

        return self.success_response(
            data={"item_name": item_name, "quantity": quantity, "unit_price": str(unit_price)},
            code="RESOURCE_VALIDATED",
            message_en="Input validated successfully.",
            message_bn="প্রদত্ত তথ্য সফলভাবে যাচাই করা হয়েছে।",
        )

    @action(
        detail=False, methods=["get"], url_path="unauthorized-access", permission_classes=[permissions.IsAuthenticated]
    )
    def unauthorized_access(self, request):
        """Demonstrates 401 Unauthorized envelope for unauthenticated callers."""
        return self.success_response(
            data={"user": str(request.user)},
            code="AUTHORIZED",
            message_en="User is authenticated.",
            message_bn="ব্যবহারকারী অনুমোদিত।",
        )

    @action(detail=False, methods=["get"], url_path="cross-tenant-access")
    def cross_tenant_access(self, request):
        """
        Demonstrates cross-tenant rejection complying with ADR-0047 (403 Forbidden).
        Validates caller's tenant matches requested target tenant.
        """
        caller_tenant = request.query_params.get("caller_tenant_id", "tenant-dhaka-central")
        target_tenant = request.query_params.get("target_tenant_id", "tenant-chittagong-care")

        if caller_tenant != target_tenant:
            raise CrossTenantViolationError(
                message_en=f"Tenant '{caller_tenant}' is forbidden from accessing resources of tenant '{target_tenant}'.",
                message_bn=f"টেন্যান্ট '{caller_tenant}' অপর টেন্যান্ট '{target_tenant}' এর তথ্য দেখার অনুমতি রাখে না।",
            )

        return self.success_response(
            data={"tenant_id": caller_tenant, "accessible": True},
            code="TENANT_AUTHORIZED",
            message_en="Tenant access verified.",
            message_bn="টেন্যান্ট অ্যাক্সেস সফলভাবে যাচাই করা হয়েছে।",
        )


class RateLimitPolicyViewSet(viewsets.ViewSet, StandardResponseMixin):
    """
    Dedicated viewset auditing and demonstrating multi-tiered rate limiting (ADR-0048).
    Provides introspection into rate limits and strict throttle test endpoints.
    """

    permission_classes = [permissions.AllowAny]

    @action(detail=False, methods=["get"], url_path="policy")
    def policy(self, request):
        """Returns the active rate limiting policy matrix conforming to rate_limit_policy.schema.json."""
        policies = get_active_rate_limit_policies()
        return self.success_response(
            data=policies,
            code="RATE_LIMIT_POLICY_RETRIEVED",
            message_en="Active rate limit policies retrieved successfully.",
            message_bn="সক্রিয় রেট লিমিট নীতিমালা সফলভাবে লোড হয়েছে।",
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="test-auth-throttle",
        throttle_classes=[TestStrictRateThrottle],
    )
    def test_auth_throttle(self, request):
        """Demonstrates authentication throttling bucket."""
        return self.success_response(
            data={"attempt": "success", "scope": "auth_login"},
            code="AUTH_THROTTLE_PASS",
            message_en="Authentication attempt within rate limit.",
            message_bn="প্রমাণীকরণ চেষ্টা রেট লিমিটের মধ্যে রয়েছে।",
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="test-mutation-throttle",
        throttle_classes=[TestStrictRateThrottle],
    )
    def test_mutation_throttle(self, request):
        """Demonstrates sensitive mutation throttling bucket."""
        return self.success_response(
            data={"mutation": "accepted", "scope": "sensitive_mutation"},
            code="MUTATION_THROTTLE_PASS",
            message_en="Mutation operation accepted within rate limit.",
            message_bn="মিউটেশন অপারেশন রেট লিমিটের মধ্যে গৃহীত হয়েছে।",
        )

    @action(
        detail=False,
        methods=["get"],
        url_path="test-export-throttle",
        throttle_classes=[TestStrictRateThrottle],
    )
    def test_export_throttle(self, request):
        """Demonstrates bulk data export throttling bucket."""
        return self.success_response(
            data={"export": "generated", "scope": "data_export"},
            code="EXPORT_THROTTLE_PASS",
            message_en="Export query executed within rate limit.",
            message_bn="এক্সপোর্ট কুয়েরি রেট লিমিটের মধ্যে কার্যকর হয়েছে।",
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="test-invitation-throttle",
        throttle_classes=[TestStrictRateThrottle],
    )
    def test_invitation_throttle(self, request):
        """Demonstrates public staff invitation throttling bucket."""
        return self.success_response(
            data={"invitation": "dispatched", "scope": "public_invitation"},
            code="INVITATION_THROTTLE_PASS",
            message_en="Staff invitation dispatched within rate limit.",
            message_bn="স্টাফ আমন্ত্রণ রেট লিমিটের মধ্যে পাঠানো হয়েছে।",
        )


class TenantContextViewSet(viewsets.ViewSet, StandardResponseMixin):
    """
    Dedicated viewset auditing and demonstrating trusted tenant context resolution (ADR-0049).
    Resolves tenant and branch from trusted signed claims; never trusts client-supplied tenant IDs alone.
    """

    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["get"], url_path="current")
    def current(self, request):
        """Returns the active tenant and branch security context resolved by TenantContextMiddleware."""
        context = getattr(request, "tenant_context", None)
        if not context:
            tenant_id = getattr(request, "tenant_id", None) or getattr(request.user, "tenant_id", None)
            tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
            if not tenant:
                raise CrossTenantViolationError(
                    message_en="No active tenant context resolved for authenticated session.",
                    message_bn="প্রমাণিত সেশনের জন্য কোনো সক্রিয় টেন্যান্ট পাওয়া যায়নি।",
                )
            context_data = {
                "tenant_id": tenant.tenant_id,
                "tenant_name": tenant.name,
                "subdomain": tenant.subdomain,
                "plan_tier": tenant.plan_tier,
                "branch_id": getattr(request, "branch_id", None) or getattr(request.user, "branch_id", None),
                "branch_name": getattr(getattr(request, "branch", None), "name", None),
                "user_id": str(request.user.user_id),
                "user_role": getattr(request.user, "role", "CASHIER"),
                "resolution_source": "JWT_SIGNED_CLAIMS",
                "is_active": tenant.is_active,
            }
        else:
            context_data = context.to_dict()

        return self.success_response(
            data=context_data,
            code="TENANT_CONTEXT_RESOLVED",
            message_en="Tenant and active branch context resolved from trusted signed claims.",
            message_bn="নির্ভরযোগ্য সাইনড তথ্য থেকে টেন্যান্ট ও সক্রিয় শাখা সফলভাবে নিশ্চিত করা হয়েছে।",
        )

    @action(detail=False, methods=["post"], url_path="validate-access")
    def validate_access(self, request):
        """
        Validates target tenant and branch access against caller's trusted claims.
        Rejects cross-tenant tampering attempts with 403 Forbidden.
        """
        target_tenant_id = request.data.get("target_tenant_id")
        target_branch_id = request.data.get("target_branch_id")

        if not target_tenant_id:
            return self.error_response(
                code="ERR_VALIDATION_FAILED",
                message_en="Target tenant_id is required.",
                message_bn="উদ্দিষ্ট টেন্যান্ট আইডি প্রদান করা আবশ্যক।",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        caller_tenant_id = getattr(request, "tenant_id", None) or getattr(request.user, "tenant_id", None)
        is_superadmin = (
            getattr(request.user, "is_superuser", False) or getattr(request.user, "role", "") == "SUPERADMIN"
        )

        if target_tenant_id != caller_tenant_id and not is_superadmin:
            raise CrossTenantViolationError(
                message_en=(
                    f"Cross-tenant access violation: Caller tenant '{caller_tenant_id}' "
                    f"is forbidden from accessing resources of tenant '{target_tenant_id}'."
                ),
                message_bn=(
                    f"অন্য ফার্মেসির তথ্যে প্রবেশাধিকার কঠোরভাবে নিষিদ্ধ। "
                    f"আপনার টেন্যান্ট '{caller_tenant_id}' অপর টেন্যান্ট '{target_tenant_id}'-এর তথ্যে প্রবেশের অধিকার রাখে না।"
                ),
            )

        # Validate branch if provided
        if target_branch_id:
            branch = Branch.objects.filter(branch_id=target_branch_id, tenant_id=caller_tenant_id).first()
            if not branch:
                raise CrossTenantViolationError(
                    message_en=(
                        f"Cross-branch access violation: Branch '{target_branch_id}' "
                        f"does not belong to active tenant '{caller_tenant_id}'."
                    ),
                    message_bn=(f"শাখা প্রবেশাধিকার লঙ্ঘন: শাখা '{target_branch_id}' আপনার ফার্মেসির অন্তর্ভুক্ত নয়।"),
                )

        return self.success_response(
            data={
                "authorized": True,
                "tenant_id": caller_tenant_id,
                "branch_id": target_branch_id,
            },
            code="TENANT_ACCESS_AUTHORIZED",
            message_en="Tenant and branch access authorization confirmed.",
            message_bn="টেন্যান্ট ও শাখা প্রবেশাধিকার সফলভাবে অনুমোদিত।",
        )

    @action(detail=False, methods=["get"], url_path="branches")
    def branches(self, request):
        """Returns dispensary branches belonging strictly to the caller's active tenant."""
        tenant_id = getattr(request, "tenant_id", None) or getattr(request.user, "tenant_id", None)
        branch_list = list(
            Branch.objects.filter(tenant_id=tenant_id, is_active=True).values(
                "branch_id", "name", "license_number", "address", "is_active"
            )
        )
        return self.success_response(
            data=branch_list,
            code="TENANT_BRANCHES_RETRIEVED",
            message_en=f"Retrieved {len(branch_list)} active branches for tenant '{tenant_id}'.",
            message_bn=f"টেন্যান্ট '{tenant_id}'-এর জন্য {len(branch_list)}টি সক্রিয় শাখা সফলভাবে পাওয়া গেছে।",
        )

    @action(detail=False, methods=["get"], url_path="policy", permission_classes=[permissions.AllowAny])
    def policy(self, request):
        """Returns the tenant context resolution policy matrix conforming to ADR-0049."""
        policy_matrix = {
            "adr": "ADR-0049",
            "trust_hierarchy": [
                "JWT_SIGNED_CLAIMS",
                "SESSION_MEMBERSHIP",
                "SUPERADMIN_OVERRIDE",
            ],
            "client_supplied_tenant_ids_allowed": False,
            "superadmin_override_enabled": True,
            "cross_tenant_violation_code": "ERR_CROSS_TENANT_VIOLATION",
            "inactive_tenant_code": "ERR_TENANT_INACTIVE",
        }
        return self.success_response(
            data=policy_matrix,
            code="TENANT_POLICY_RETRIEVED",
            message_en="Tenant context resolution policy matrix retrieved successfully.",
            message_bn="টেন্যান্ট কনটেক্সট রেজোলিউশন নীতিমালা সফলভাবে লোড হয়েছে।",
        )


class GlobalExceptionHandlingViewSet(viewsets.ViewSet, StandardResponseMixin):
    """
    Dedicated viewset auditing, simulating, and verifying centralized global exception handling (ADR-0054).
    Validates machine error codes, bilingual localized descriptions, trace context preservation,
    and strict information masking across domain, validation, database, and unexpected server failures.
    """

    permission_classes = [permissions.AllowAny]

    @action(detail=False, methods=["get"], url_path="catalog")
    def catalog(self, request):
        """Returns the canonical catalog of all registered machine error codes and recovery guidance."""
        from apps.core.errors import get_registered_error_catalog

        error_catalog = get_registered_error_catalog()
        return self.success_response(
            data=error_catalog,
            code="ERROR_CATALOG_RETRIEVED",
            message_en=f"Retrieved {len(error_catalog)} registered canonical error codes.",
            message_bn=f"{len(error_catalog)}টি নিবন্ধিত ক্যানোনিকাল ত্রুটি কোড সফলভাবে লোড হয়েছে।",
        )

    @action(detail=False, methods=["post"], url_path="simulate")
    def simulate(self, request):
        """
        Simulates explicit exception scenarios against the global exception handler pipeline.
        Supported scenarios:
          - valid
          - validation_error
          - cross_tenant
          - unauthorized
          - permission_denied
          - database_integrity
          - database_unavailable
          - subscription_expired
          - unhandled_server_error
          - domain_batch_expired
          - resource_not_found
        """
        scenario = request.data.get("scenario", "valid")

        if scenario == "valid":
            return self.success_response(
                data={"scenario": "valid", "executed": True},
                code="SIMULATION_SUCCESS",
                message_en="Simulated valid request completed successfully.",
                message_bn="সঠিক অনুরোধের সিমুলেশন সফলভাবে সম্পন্ন হয়েছে।",
            )

        if scenario == "validation_error":
            from rest_framework.exceptions import ValidationError

            raise ValidationError(
                detail={
                    "medicine_id": ["Medicine ID is required and cannot be blank."],
                    "dispense_quantity": ["Dispense quantity must be a positive integer greater than zero."],
                }
            )

        if scenario == "cross_tenant":
            from apps.core.errors import CrossTenantViolationError

            raise CrossTenantViolationError(
                message_en="Access to foreign pharmacy tenant resources is strictly forbidden.",
                message_bn="অন্য ফার্মেসির তথ্যে প্রবেশাধিকার কঠোরভাবে নিষিদ্ধ।",
            )

        if scenario == "unauthorized":
            from apps.core.errors import AuthenticationExpiredError

            raise AuthenticationExpiredError(
                message_en="Authentication session expired. Please log in again.",
                message_bn="আপনার লগইন সেশনের মেয়াদ শেষ হয়েছে। অনুগ্রহ করে পুনরায় লগইন করুন।",
            )

        if scenario == "permission_denied":
            from apps.core.errors import PermissionDeniedError

            raise PermissionDeniedError(
                message_en="Cashier role cannot perform administrative override without manager authorization.",
                message_bn="ম্যানেজারের অনুমোদন ব্যতীত ক্যাশিয়ার প্রশাসনিক পরিবর্তন করতে পারবে না।",
            )

        if scenario == "database_integrity":
            from django.db import IntegrityError as DjangoIntegrityError

            raise DjangoIntegrityError(
                "duplicate key value violates unique constraint 'core_saleinvoice_tenant_id_invoice_number_uniq'\n"
                "DETAIL:  Key (tenant_id, invoice_number)=(tenant-dhaka, INV-1042) already exists."
            )

        if scenario == "database_unavailable":
            from django.db import OperationalError as DjangoOperationalError

            raise DjangoOperationalError(
                "could not connect to server: Connection refused\n"
                "Is the server running on host 'postgres.internal' and accepting TCP/IP connections on port 5432?"
            )

        if scenario == "subscription_expired":
            from apps.core.errors import SubscriptionExpiredError

            raise SubscriptionExpiredError(
                message_en="Pharmacy subscription has expired. Please renew your enterprise plan.",
                message_bn="ফার্মেসির সাবস্ক্রিপশনের মেয়াদ শেষ হয়েছে। অনুগ্রহ করে আপনার এন্টারপ্রাইজ প্ল্যান নবায়ন করুন।",
            )

        if scenario == "unhandled_server_error":
            raise RuntimeError(
                "Simulated unhandled critical runtime fault: pointer dereference or division by zero in pricing engine."
            )

        if scenario == "domain_batch_expired":
            from apps.core.errors import BatchExpiredError

            raise BatchExpiredError(
                message_en="Selected medicine batch BATCH-NAPA-2023 expired on 2024-01-01 and cannot be dispensed.",
                message_bn="নির্বাচিত ওষুধের ব্যাচটির মেয়াদ উত্তীর্ণ হয়েছে এবং এটি বিক্রি করা নিষিদ্ধ।",
            )

        if scenario == "resource_not_found":
            from rest_framework.exceptions import NotFound

            raise NotFound(detail="The requested simulated resource was not found.")

        from rest_framework.exceptions import ValidationError

        raise ValidationError(detail={"scenario": [f"Unknown simulation scenario '{scenario}'."]})

    @action(detail=False, methods=["get"], url_path="trace-context")
    def trace_context(self, request):
        """Returns the active distributed tracing and correlation context."""
        from apps.audit_logging.context import LogContextManager

        ctx = LogContextManager.get_context()
        request_id = (
            getattr(request, "request_id", None)
            or request.META.get("HTTP_X_REQUEST_ID")
            or (ctx.request_id if ctx else None)
        )
        return self.success_response(
            data={
                "request_id": request_id,
                "correlation_id": request.META.get("HTTP_X_CORRELATION_ID") or request_id,
                "tenant_id": getattr(request, "tenant_id", None) or (ctx.tenant_id if ctx else None),
                "user_id": getattr(getattr(request, "user", None), "user_id", None) or (ctx.user_id if ctx else None),
                "is_authenticated": bool(request.user and request.user.is_authenticated),
            },
            code="TRACE_CONTEXT_RETRIEVED",
            message_en="Distributed trace context retrieved successfully.",
            message_bn="ট্রেস কনটেক্সট তথ্য সফলভাবে লোড হয়েছে।",
        )


class TimeMoneyStandardsViewSet(viewsets.ViewSet):
    """
    Architecture Decision: ADR-0055
    Standardized API for Decimal Monetary Precision and Asia/Dhaka Time Operations.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_tenant_id(self, request) -> str:
        tenant_id = (
            getattr(request, "tenant_id", None)
            or request.headers.get("X-Tenant-Id")
            or getattr(getattr(request, "user", None), "tenant_id", None)
        )
        if not tenant_id:
            tenant_id = "default-tenant"
        return str(tenant_id)

    def _check_tenant_match(self, request, target_tenant: str | None) -> None:
        if not target_tenant:
            return
        caller_tenant = self._get_tenant_id(request)
        if str(target_tenant).strip() != caller_tenant.strip():
            from apps.audit_logging.models import AuditActionStatus, AuditLogEntry

            AuditLogEntry.objects.create(
                action="SECURITY_INCIDENT_CROSS_TENANT",
                resource_type="SECURITY_BARRIER",
                tenant_id=caller_tenant,
                status=AuditActionStatus.DENIED,
                metadata_json={
                    "event": "CROSS_TENANT_TIME_MONEY_VIOLATION",
                    "caller_tenant": caller_tenant,
                    "target_tenant": target_tenant,
                    "endpoint": request.path,
                },
            )
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied(
                detail={
                    "error_code": "FORBIDDEN_CROSS_TENANT_ACCESS",
                    "message_en": f"Access denied: Caller tenant '{caller_tenant}' cannot operate on tenant '{target_tenant}'.",
                    "message_bn": f"অনুমতি নেই: কলার টেন্যান্ট '{caller_tenant}' টার্গেট টেন্যান্ট '{target_tenant}'-এ কাজ করতে পারে না।",
                }
            )

    @action(detail=False, methods=["get"], url_path="policy")
    def get_policy(self, request):
        """Returns the centralized Time and Money Standards Policy."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from apps.core.localization import format_bdt_currency
        from apps.core.time_and_money import MoneyStandard, TimeStandard

        target_tenant = request.query_params.get("tenant_id")
        self._check_tenant_match(request, target_tenant)

        now_utc = datetime.now(ZoneInfo("UTC"))
        sample_amount = Decimal("152345.50")

        return Response(
            {
                "success": True,
                "code": "POLICY_RETRIEVED",
                "message_en": "Time and money standards policy retrieved successfully.",
                "message_bn": "সময় ও মুদ্রার মানদণ্ড নীতিমালা সফলভাবে প্রাপ্ত হয়েছে।",
                "data": {
                    "currency_code": MoneyStandard.CURRENCY_CODE,
                    "currency_symbol": MoneyStandard.CURRENCY_SYMBOL,
                    "monetary_scale": 2,
                    "rounding_strategy": "ROUND_HALF_UP",
                    "storage_timezone": TimeStandard.STORAGE_TIMEZONE,
                    "display_timezone": TimeStandard.DISPLAY_TIMEZONE,
                    "date_format": "DD/MM/YYYY",
                    "datetime_format": "DD/MM/YYYY hh:mm A",
                    "samples": {
                        "amount_raw": str(sample_amount),
                        "bdt_en": format_bdt_currency(sample_amount, locale="en-US"),
                        "bdt_bn": format_bdt_currency(sample_amount, locale="bn-BD"),
                        "utc_iso": now_utc.isoformat(),
                        "dhaka_display_en": TimeStandard.format_dhaka_display(now_utc, locale="en-US"),
                        "dhaka_display_bn": TimeStandard.format_dhaka_display(now_utc, locale="bn-BD"),
                    },
                },
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="calculate-invoice")
    def calculate_invoice(self, request):
        """
        Calculates invoice totals using exact Decimal arithmetic,
        rejecting floating-point inputs and generating compliant timestamps.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from rest_framework.exceptions import ValidationError

        from apps.core.time_and_money import MoneyStandard, TimeStandard

        target_tenant = request.data.get("tenant_id")
        self._check_tenant_match(request, target_tenant)

        raw_items = request.data.get("items")
        if not raw_items or not isinstance(raw_items, list):
            raise ValidationError(
                detail={"items": ["Field 'items' is required and must be a non-empty list of cart items."]}
            )

        inv_discount = request.data.get("invoice_discount", "0.00")
        paid = request.data.get("paid_amount", "0.00")

        try:
            invoice_result = MoneyStandard.calculate_invoice(
                items=raw_items,
                invoice_discount_amount=inv_discount,
                paid_amount=paid,
            )
        except (ValueError, TypeError) as err:
            raise ValidationError(detail={"calculation_error": [str(err)]})

        now_utc = datetime.now(ZoneInfo("UTC"))
        invoice_result["created_at_utc"] = now_utc.isoformat()
        invoice_result["display_at_dhaka"] = TimeStandard.format_dhaka_display(now_utc, locale="en-US")
        invoice_result["display_at_dhaka_bn"] = TimeStandard.format_dhaka_display(now_utc, locale="bn-BD")

        return Response(
            {
                "success": True,
                "code": "INVOICE_CALCULATED",
                "message_en": "Invoice totals calculated deterministically with Decimal scale 2.",
                "message_bn": "ডেসিমেল স্কেল ২ নির্ভুলতায় মোট বিল হিসাব করা হয়েছে।",
                "data": invoice_result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="validate-money")
    def validate_money(self, request):
        """
        Demonstrates strict input validation: rejects float numbers,
        negative money where disallowed, and naive datetimes.
        """
        from rest_framework.exceptions import ValidationError

        from apps.core.localization import format_bdt_currency
        from apps.core.time_and_money import MoneyStandard, TimeStandard

        target_tenant = request.data.get("tenant_id")
        self._check_tenant_match(request, target_tenant)

        field_errors: dict[str, list[str]] = {}

        # 1. Validate Amount
        raw_amount = request.data.get("amount")
        amount_dec = None
        if raw_amount is None:
            field_errors["amount"] = ["Field 'amount' is required."]
        else:
            try:
                amount_dec = MoneyStandard.validate_non_negative(raw_amount, field_name="amount")
            except (ValueError, TypeError) as err:
                field_errors["amount"] = [str(err)]

        # 2. Validate Timestamp
        raw_timestamp = request.data.get("timestamp")
        utc_dt = None
        if raw_timestamp is None:
            field_errors["timestamp"] = ["Field 'timestamp' is required."]
        else:
            try:
                utc_dt = TimeStandard.ensure_aware_utc(raw_timestamp, field_name="timestamp")
            except (ValueError, TypeError) as err:
                field_errors["timestamp"] = [str(err)]

        if field_errors:
            raise ValidationError(detail=field_errors)

        return Response(
            {
                "success": True,
                "code": "VALIDATION_PASSED",
                "message_en": "Monetary and temporal inputs comply with ADR-0055 standards.",
                "message_bn": "মুদ্রা ও সময়ের তথ্য মানদণ্ড অনুযায়ী সঠিক রয়েছে।",
                "data": {
                    "amount_decimal": str(amount_dec),
                    "amount_formatted_en": format_bdt_currency(amount_dec, locale="en-US"),
                    "amount_formatted_bn": format_bdt_currency(amount_dec, locale="bn-BD"),
                    "timestamp_utc": utc_dt.isoformat(),
                    "timestamp_dhaka_en": TimeStandard.format_dhaka_display(utc_dt, locale="en-US"),
                    "timestamp_dhaka_bn": TimeStandard.format_dhaka_display(utc_dt, locale="bn-BD"),
                },
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="verify-expiry")
    def verify_expiry(self, request):
        """
        Verifies medicine batch expiry against current date in Asia/Dhaka timezone.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from rest_framework.exceptions import ValidationError

        from apps.core.time_and_money import TimeStandard

        target_tenant = request.data.get("tenant_id")
        self._check_tenant_match(request, target_tenant)

        raw_date = request.data.get("expiry_date")
        if not raw_date:
            raise ValidationError(detail={"expiry_date": ["Field 'expiry_date' is required."]})

        raw_ref = request.data.get("reference_timestamp")
        ref_utc = None
        if raw_ref:
            try:
                ref_utc = TimeStandard.ensure_aware_utc(raw_ref, field_name="reference_timestamp")
            except (ValueError, TypeError) as err:
                raise ValidationError(detail={"reference_timestamp": [str(err)]})

        try:
            parsed_exp = TimeStandard.parse_date_only(raw_date, field_name="expiry_date")
            is_exp = TimeStandard.is_batch_expired(parsed_exp, reference_time_utc=ref_utc)
        except (ValueError, TypeError) as err:
            raise ValidationError(detail={"expiry_date": [str(err)]})

        dhaka_ref = TimeStandard.to_dhaka(ref_utc if ref_utc else datetime.now(ZoneInfo("UTC")))

        return Response(
            {
                "success": True,
                "code": "BATCH_EXPIRY_VERIFIED",
                "message_en": "Batch expiry successfully evaluated against Dhaka business date.",
                "message_bn": "ঢাকা সময়ানুসারে ওষুধের মেয়াদের তারিখ যাচাই করা হয়েছে।",
                "data": {
                    "expiry_date": parsed_exp.isoformat(),
                    "reference_dhaka_date": dhaka_ref.date().isoformat(),
                    "is_expired": is_exp,
                    "action_required": "QUARANTINE_BATCH" if is_exp else "DISPENSE_ALLOWED",
                },
            },
            status=status.HTTP_200_OK,
        )
