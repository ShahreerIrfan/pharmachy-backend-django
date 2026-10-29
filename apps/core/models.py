"""
apps/core/models.py
Architecture Decision: ADR-0004, ADR-0017 & ADR-0029
Phase 02: Repository, Toolchain & Engineering Automation

Normalized Core Domain Models for Bangladesh Pharmacy ERP & POS SaaS:
- Multi-Tenant & Branch partitioning (Tenant, Branch).
- Shared Medicine Catalog with Bangladesh DGDA parameters (MedicineProduct).
- FEFO Batch Inventory with Cold-Chain limits (BatchStock).
- Finalized & Audited Sales Transactions (SaleInvoice, SaleInvoiceItem).
- Strict Decimal precision for all monetary amounts (zero float).
"""

from __future__ import annotations

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models


class PlanTier(models.TextChoices):
    BASIC = "BASIC", "Basic"
    STANDARD = "STANDARD", "Standard"
    PRO = "PRO", "Pro"


class BatchStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    EXPIRED = "EXPIRED", "Expired"
    QUARANTINED = "QUARANTINED", "Quarantined"


class InvoiceStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    FINALIZED = "FINALIZED", "Finalized"
    REVERSED = "REVERSED", "Reversed"


class PaymentMethod(models.TextChoices):
    CASH = "CASH", "Cash"
    BKASH = "BKASH", "bKash"
    NAGAD = "NAGAD", "Nagad"
    CARD = "CARD", "Card"


class Tenant(models.Model):
    """Canonical multi-tenant account record representing a pharmacy chain or independent store."""

    tenant_id = models.CharField(max_length=64, primary_key=True)
    name = models.CharField(max_length=255)
    subdomain = models.CharField(max_length=64, unique=True, db_index=True)
    plan_tier = models.CharField(max_length=32, choices=PlanTier.choices, default=PlanTier.STANDARD)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Tenant Account"
        verbose_name_plural = "Tenant Accounts"

    def __str__(self) -> str:
        return f"{self.name} ({self.tenant_id})"


class Branch(models.Model):
    """Physical pharmacy store or dispensary branch belonging to a tenant."""

    branch_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="branches")
    name = models.CharField(max_length=255)
    license_number = models.CharField(max_length=128)  # DGDA Pharmacy License Number
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        unique_together = ("tenant", "name")
        verbose_name = "Pharmacy Branch"
        verbose_name_plural = "Pharmacy Branches"

    def __str__(self) -> str:
        return f"{self.name} [{self.license_number}] ({self.tenant_id})"


class MedicineProduct(models.Model):
    """Global or tenant-accessible pharmaceutical product catalog with DGDA classification."""

    product_id = models.CharField(max_length=64, primary_key=True)
    brand_name = models.CharField(max_length=255, db_index=True)
    generic_name = models.CharField(max_length=255, db_index=True)
    dosage_form = models.CharField(max_length=64)  # Tablet, Capsule, Syrup, Suspension, Injection
    strength = models.CharField(max_length=64)  # 500mg, 20mg, 100ml
    manufacturer = models.CharField(max_length=255, db_index=True)  # Square, Beximco, Incepta, etc.
    mrp_unit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Maximum Retail Price per base unit in BDT.",
    )
    cost_unit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Standard wholesale cost per base unit in BDT.",
    )
    requires_cold_chain = models.BooleanField(default=False)
    min_temp_celsius = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    max_temp_celsius = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["brand_name"]
        verbose_name = "Medicine Product"
        verbose_name_plural = "Medicine Products"

    def __str__(self) -> str:
        return f"{self.brand_name} {self.strength} ({self.generic_name}) - {self.manufacturer}"


class BatchStock(models.Model):
    """Individual manufactured batch of a medicine stored in a specific tenant branch with FEFO expiry."""

    batch_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="batches")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="batches")
    product = models.ForeignKey(MedicineProduct, on_delete=models.CASCADE, related_name="batches")
    batch_number = models.CharField(max_length=64)
    quantity_base_units = models.PositiveIntegerField(default=0)
    expiry_date = models.DateField(db_index=True)
    purchase_date = models.DateField()
    cost_per_unit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    status = models.CharField(max_length=32, choices=BatchStatus.choices, default=BatchStatus.ACTIVE)

    class Meta:
        ordering = ["expiry_date", "batch_number"]
        unique_together = ("tenant", "branch", "product", "batch_number")
        indexes = [
            models.Index(fields=["tenant", "branch", "expiry_date"]),
        ]
        verbose_name = "Batch Stock Lot"
        verbose_name_plural = "Batch Stock Lots"

    def __str__(self) -> str:
        return f"Batch {self.batch_number} - {self.product.brand_name} (Qty: {self.quantity_base_units}, Exp: {self.expiry_date})"

    @property
    def is_expired(self) -> bool:
        from django.utils import timezone

        return self.expiry_date < timezone.now().date()


class SaleInvoice(models.Model):
    """Immutable retail or wholesale sales transaction record emitted at cashier checkout."""

    invoice_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="invoices")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="invoices")
    invoice_number = models.CharField(max_length=64)
    customer_name = models.CharField(max_length=255, blank=True, default="Walk-in Customer")
    customer_phone = models.CharField(max_length=32, blank=True, default="")
    total_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    tax_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    discount_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    net_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    payment_method = models.CharField(max_length=32, choices=PaymentMethod.choices, default=PaymentMethod.CASH)
    status = models.CharField(max_length=32, choices=InvoiceStatus.choices, default=InvoiceStatus.FINALIZED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = ("tenant", "invoice_number")
        indexes = [
            models.Index(fields=["tenant", "branch", "-created_at"]),
        ]
        verbose_name = "Sale Invoice"
        verbose_name_plural = "Sale Invoices"

    def __str__(self) -> str:
        return f"Invoice {self.invoice_number} ({self.tenant_id}) - {self.net_amount} BDT"


class SaleInvoiceItem(models.Model):
    """Line item in a sale invoice referencing a batch and quantity."""

    invoice = models.ForeignKey(SaleInvoice, on_delete=models.CASCADE, related_name="items")
    batch = models.ForeignKey(BatchStock, on_delete=models.PROTECT, related_name="invoice_items")
    product = models.ForeignKey(MedicineProduct, on_delete=models.PROTECT, related_name="invoice_items")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    subtotal = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    class Meta:
        ordering = ["id"]
        verbose_name = "Sale Invoice Line Item"
        verbose_name_plural = "Sale Invoice Line Items"

    def __str__(self) -> str:
        return f"{self.product.brand_name} x {self.quantity} = {self.subtotal} BDT"


class JobCategory(models.TextChoices):
    NOTIFICATION = "NOTIFICATION", "Notification"
    REMINDER = "REMINDER", "Reminder"
    DERIVED_ANALYTICS = "DERIVED_ANALYTICS", "Derived Analytics"


class JobStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PROCESSING = "PROCESSING", "Processing"
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"
    RETRYING = "RETRYING", "Retrying"


class AsyncJobRecord(models.Model):
    """
    Audit log and lifecycle tracker for asynchronous background jobs executed by Celery and Redis.
    Architecture Decision: ADR-0031
    Restricted to NOTIFICATION, REMINDER, and DERIVED_ANALYTICS categories.
    """

    job_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="async_jobs")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, null=True, blank=True, related_name="async_jobs")
    category = models.CharField(max_length=32, choices=JobCategory.choices)
    task_name = models.CharField(max_length=255)
    status = models.CharField(max_length=32, choices=JobStatus.choices, default=JobStatus.PENDING, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    result = models.JSONField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    max_retries = models.PositiveIntegerField(default=3)
    scheduled_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-scheduled_at"]
        indexes = [
            models.Index(fields=["tenant", "category", "status"]),
            models.Index(fields=["status", "scheduled_at"]),
        ]
        verbose_name = "Async Background Job Record"
        verbose_name_plural = "Async Background Job Records"

    def __str__(self) -> str:
        return f"[{self.category}] {self.task_name} ({self.job_id}) - {self.status}"


class DailyBranchAnalytics(models.Model):
    """
    Derived daily revenue and order metrics aggregated asynchronously from finalized sales.
    Architecture Decision: ADR-0031 & ADR-0001 (Zero float, strict Decimal precision).
    """

    analytics_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="daily_analytics")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="daily_analytics")
    date = models.DateField(db_index=True)
    total_sales_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Gross daily sales aggregated from finalized invoices in BDT.",
    )
    total_orders_count = models.PositiveIntegerField(default=0)
    total_items_sold = models.PositiveIntegerField(default=0)
    last_aggregated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]
        unique_together = ("tenant", "branch", "date")
        indexes = [
            models.Index(fields=["tenant", "branch", "date"]),
        ]
        verbose_name = "Daily Branch Analytics Rollup"
        verbose_name_plural = "Daily Branch Analytics Rollups"

    def __str__(self) -> str:
        return f"{self.branch.name} Analytics ({self.date}): BDT {self.total_sales_amount} ({self.total_orders_count} orders)"


class ComplianceReminder(models.Model):
    """
    Asynchronously dispatched regulatory and operational reminders.
    Architecture Decision: ADR-0031 & ADR-0015 (DGDA license, PCB pharmacist renewals, batch quarantine).
    """

    reminder_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="compliance_reminders")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="compliance_reminders")
    reminder_type = models.CharField(max_length=64)  # e.g. DRUG_LICENSE_EXPIRY, BATCH_NEAR_EXPIRY
    target_date = models.DateField()
    message_en = models.CharField(max_length=255)
    message_bn = models.CharField(max_length=255)
    is_acknowledged = models.BooleanField(default=False, db_index=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Compliance Reminder"
        verbose_name_plural = "Compliance Reminders"

    def __str__(self) -> str:
        return f"[{self.reminder_type}] {self.branch.name} - Exp: {self.target_date}"


class SubscriptionStatus(models.TextChoices):
    TRIAL = "TRIAL", "Trial"
    ACTIVE = "ACTIVE", "Active"
    GRACE_PERIOD = "GRACE_PERIOD", "Grace Period"
    EXPIRED = "EXPIRED", "Expired"
    CANCELLED = "CANCELLED", "Cancelled"


class SubscriptionRecord(models.Model):
    """
    SaaS Tenant Subscription entitlement, branch limits, and lifecycle record.
    Architecture Decision: ADR-0015 & ADR-0033
    """

    subscription_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="subscriptions")
    plan_tier = models.CharField(max_length=32, choices=PlanTier.choices, default=PlanTier.STANDARD)
    status = models.CharField(
        max_length=32, choices=SubscriptionStatus.choices, default=SubscriptionStatus.ACTIVE, db_index=True
    )
    amount_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Recurring subscription billing amount in BDT.",
    )
    billing_cycle = models.CharField(max_length=32, default="MONTHLY")
    licensed_branches = models.PositiveIntegerField(default=1)
    licensed_devices = models.PositiveIntegerField(default=3)
    features_enabled = models.JSONField(default=dict, blank=True)
    start_date = models.DateField()
    renewal_date = models.DateField(db_index=True)
    grace_period_end_date = models.DateField(null=True, blank=True)
    is_auto_renew = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-renewal_date"]
        verbose_name = "Tenant Subscription"
        verbose_name_plural = "Tenant Subscriptions"

    def __str__(self) -> str:
        return f"{self.tenant.name} [{self.plan_tier}] - Status: {self.status} (Exp: {self.renewal_date})"


class StaffRole(models.TextChoices):
    PLATFORM_SUPER_ADMIN = "PLATFORM_SUPER_ADMIN", "Platform Super Admin"
    TENANT_OWNER = "TENANT_OWNER", "Tenant Owner"
    BRANCH_MANAGER = "BRANCH_MANAGER", "Branch Manager"
    PHARMACIST = "PHARMACIST", "Registered Pharmacist"
    CASHIER = "CASHIER", "Cashier"
    AUDITOR = "AUDITOR", "Auditor"


class StaffMember(models.Model):
    """
    Authorized pharmacy staff profile associated with a tenant, optional branch, and role.
    Architecture Decision: ADR-0005 & ADR-0033
    """

    staff_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="staff_members")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, null=True, blank=True, related_name="staff_members")
    username = models.CharField(max_length=128)
    full_name = models.CharField(max_length=255)
    role = models.CharField(max_length=32, choices=StaffRole.choices, default=StaffRole.CASHIER, db_index=True)
    mobile_number = models.CharField(max_length=32)
    pcb_registration_no = models.CharField(max_length=64, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["full_name"]
        unique_together = ("tenant", "username")
        verbose_name = "Staff Member"
        verbose_name_plural = "Staff Members"

    def __str__(self) -> str:
        return f"{self.full_name} ({self.role}) - {self.tenant.name}"


class PrescriptionStatus(models.TextChoices):
    PENDING = "PENDING", "Pending Verification"
    VERIFIED = "VERIFIED", "Verified by Pharmacist"
    REJECTED = "REJECTED", "Rejected"
    DISPENSED = "DISPENSED", "Dispensed"
    CANCELLED = "CANCELLED", "Cancelled"


class PrescriptionRecord(models.Model):
    """
    Clinical prescription order and pharmacist clinical verification record.
    Architecture Decision: ADR-0010, ADR-0015 & ADR-0033
    Strictly uses synthetic patient tokens, never real patient PHI.
    """

    prescription_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="prescriptions")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="prescriptions")
    doctor_name = models.CharField(max_length=255)
    doctor_bmdc_no = models.CharField(max_length=64)
    patient_token = models.CharField(max_length=64, db_index=True)
    patient_age = models.PositiveIntegerField(null=True, blank=True)
    patient_gender = models.CharField(max_length=16, blank=True, default="UNKNOWN")
    diagnosis = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=32, choices=PrescriptionStatus.choices, default=PrescriptionStatus.PENDING, db_index=True
    )
    verified_by = models.ForeignKey(
        StaffMember, on_delete=models.SET_NULL, null=True, blank=True, related_name="verified_prescriptions"
    )
    pharmacist_pcb_no = models.CharField(max_length=64, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    prescription_date = models.DateField()
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "branch", "status"]),
        ]
        verbose_name = "Prescription Record"
        verbose_name_plural = "Prescription Records"

    def __str__(self) -> str:
        return f"Rx {self.prescription_id} ({self.status}) - Dr. {self.doctor_name} [BMDC: {self.doctor_bmdc_no}]"


class PrescriptionItem(models.Model):
    """Individual pharmaceutical item prescribed in a clinical prescription."""

    prescription = models.ForeignKey(PrescriptionRecord, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(MedicineProduct, on_delete=models.PROTECT, related_name="prescription_items")
    dosage_instructions = models.CharField(max_length=255)
    duration_days = models.PositiveIntegerField(default=7)
    quantity_prescribed = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    is_dispensed = models.BooleanField(default=False)

    class Meta:
        ordering = ["id"]
        verbose_name = "Prescription Line Item"
        verbose_name_plural = "Prescription Line Items"

    def __str__(self) -> str:
        return f"{self.product.brand_name} x {self.quantity_prescribed} ({self.dosage_instructions})"


class PaymentTransaction(models.Model):
    """
    Point of Sale or Subscription tender settlement transaction.
    Architecture Decision: ADR-0001, ADR-0025 & ADR-0033
    Strict Decimal precision (zero floating-point).
    """

    transaction_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="payment_transactions")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="payment_transactions")
    invoice = models.ForeignKey(
        SaleInvoice, on_delete=models.CASCADE, null=True, blank=True, related_name="transactions"
    )
    tender_type = models.CharField(max_length=32, choices=PaymentMethod.choices, default=PaymentMethod.CASH)
    gateway_reference = models.CharField(max_length=128, blank=True, default="")
    amount_bdt = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    status = models.CharField(max_length=32, default="SUCCESS")
    initiated_by = models.ForeignKey(
        StaffMember, on_delete=models.SET_NULL, null=True, blank=True, related_name="processed_payments"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Payment Transaction"
        verbose_name_plural = "Payment Transactions"

    def __str__(self) -> str:
        return f"Payment {self.transaction_id} ({self.tender_type}) - BDT {self.amount_bdt} [{self.status}]"
