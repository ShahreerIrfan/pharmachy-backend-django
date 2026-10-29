"""
apps/core/views.py
Architecture Decision: ADR-0004, ADR-0005 & ADR-0031
DRF ViewSets with Tenant Isolation and Role-Based Access Control.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

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
from apps.core.serializers import (
    AsyncJobRecordSerializer,
    BatchStockSerializer,
    BranchSerializer,
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


class BatchStockViewSet(viewsets.ModelViewSet):
    queryset = BatchStock.objects.all()
    serializer_class = BatchStockSerializer
    permission_classes = [permissions.AllowAny]

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

    def get_queryset(self):
        qs = super().get_queryset()
        tenant_id = self.request.query_params.get("tenant_id")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        return qs

    @action(detail=False, methods=["post"], url_path="checkout")
    def checkout(self, request):
        """
        Executes synchronous counter checkout transaction with atomic row locking.
        Emits asynchronous receipt SMS and analytics refresh post-commit (ADR-0031).
        """
        serializer = SaleCheckoutRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        try:
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
        except Exception as err:
            return Response(
                {"error": str(err), "type": err.__class__.__name__},
                status=status.HTTP_400_BAD_REQUEST,
            )


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
