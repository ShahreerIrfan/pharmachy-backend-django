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
from django.utils import timezone


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


class TenantLifecycleStatus(models.TextChoices):
    PROVISIONING = "PROVISIONING", "Provisioning & Bootstrap"
    TRIAL = "TRIAL", "Active Evaluation Trial"
    ACTIVE = "ACTIVE", "Active & In Good Standing"
    PAST_DUE = "PAST_DUE", "Past Due & In Grace Period"
    SUSPENDED = "SUSPENDED", "Temporarily Suspended"
    ARCHIVED = "ARCHIVED", "Decommissioned & Safely Archived"


def get_default_canonical_settings() -> dict:
    return {
        "currency": "BDT",
        "timezone": "Asia/Dhaka",
        "tax_percentage": 0.0,
        "allow_negative_stock": False,
        "enforce_prescription_antibiotics": True,
        "max_branches": 5,
        "max_pos_terminals": 10,
        "audit_retention_days": 365,
    }


class Tenant(models.Model):
    """Canonical multi-tenant account record representing a pharmacy chain or independent store."""

    tenant_id = models.CharField(max_length=64, primary_key=True)
    name = models.CharField(max_length=255)
    subdomain = models.CharField(max_length=64, unique=True, db_index=True)
    plan_tier = models.CharField(max_length=32, choices=PlanTier.choices, default=PlanTier.STANDARD)
    status = models.CharField(
        max_length=32,
        choices=TenantLifecycleStatus.choices,
        default=TenantLifecycleStatus.ACTIVE,
        db_index=True,
    )
    owner_user_id = models.CharField(max_length=128, blank=True, default="")
    billing_customer_id = models.CharField(max_length=128, blank=True, default="")
    billing_email = models.EmailField(blank=True, default="")
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    grace_period_ends_at = models.DateTimeField(null=True, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    suspension_reason = models.TextField(blank=True, default="")
    suspension_reason_code = models.CharField(max_length=64, blank=True, default="")
    restored_at = models.DateTimeField(null=True, blank=True)
    restoration_reason = models.TextField(blank=True, default="")
    canonical_settings = models.JSONField(default=get_default_canonical_settings, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Tenant Account"
        verbose_name_plural = "Tenant Accounts"

    def __str__(self) -> str:
        return f"{self.name} ({self.tenant_id}) [{self.status}]"

    def is_checkout_allowed(self) -> bool:
        """Returns True if the tenant is active and in good standing or active trial."""
        return self.is_active and self.status in [
            TenantLifecycleStatus.ACTIVE,
            TenantLifecycleStatus.TRIAL,
        ]

    def can_transition_to(self, target_status: str) -> bool:
        target = str(target_status).upper()
        if target not in TenantLifecycleStatus.values:
            return False
        if self.status == target:
            return True

        valid_transitions = {
            TenantLifecycleStatus.PROVISIONING: [TenantLifecycleStatus.TRIAL, TenantLifecycleStatus.ACTIVE],
            TenantLifecycleStatus.TRIAL: [
                TenantLifecycleStatus.ACTIVE,
                TenantLifecycleStatus.SUSPENDED,
                TenantLifecycleStatus.ARCHIVED,
            ],
            TenantLifecycleStatus.ACTIVE: [
                TenantLifecycleStatus.PAST_DUE,
                TenantLifecycleStatus.SUSPENDED,
                TenantLifecycleStatus.ARCHIVED,
            ],
            TenantLifecycleStatus.PAST_DUE: [
                TenantLifecycleStatus.ACTIVE,
                TenantLifecycleStatus.SUSPENDED,
                TenantLifecycleStatus.ARCHIVED,
            ],
            TenantLifecycleStatus.SUSPENDED: [TenantLifecycleStatus.ACTIVE, TenantLifecycleStatus.ARCHIVED],
            TenantLifecycleStatus.ARCHIVED: [TenantLifecycleStatus.ACTIVE],
        }
        return target in valid_transitions.get(self.status, [])

    def transition_status(self, target_status: str, operator_id: str, reason: str) -> None:
        from django.core.exceptions import ValidationError
        from django.utils import timezone

        from apps.audit_logging.models import AuditActionStatus, AuditLogEntry

        target_upper = str(target_status).upper()
        if not self.can_transition_to(target_upper):
            raise ValidationError(f"Invalid lifecycle transition from '{self.status}' to '{target_upper}'.")

        old_status = self.status
        self.status = target_upper

        if self.status == TenantLifecycleStatus.SUSPENDED:
            self.suspended_at = timezone.now()
            self.suspension_reason = reason
        elif self.status == TenantLifecycleStatus.ACTIVE:
            self.suspended_at = None
            self.suspension_reason = ""

        self.save(update_fields=["status", "suspended_at", "suspension_reason", "updated_at"])

        AuditLogEntry.objects.create(
            action="TENANT_LIFECYCLE_TRANSITION",
            resource_type="TENANT",
            resource_id=self.tenant_id,
            tenant_id=self.tenant_id,
            user_id=operator_id,
            status=AuditActionStatus.SUCCESS,
            before_state={"status": old_status},
            after_state={"status": self.status},
            metadata_json={
                "operator_id": operator_id,
                "reason": reason,
                "timestamp": timezone.now().isoformat(),
            },
        )


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


class MedicineCurationStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    UNDER_REVIEW = "UNDER_REVIEW", "Under Regulatory Review"
    APPROVED = "APPROVED", "Approved & Published"
    DEPRECATED = "DEPRECATED", "Deprecated"
    RECALLED = "RECALLED", "DGDA Recalled / Blacklisted"


class MedicineProduct(models.Model):
    """
    Architecture Decision: ADR-0004 & ADR-0070
    Global, centrally curated pharmaceutical master product catalog with DGDA regulatory metadata.
    Tenant staff have read-only access to published master records.
    Mutations and review lifecycle are strictly restricted to platform super administrators.
    """

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

    # ADR-0070: Centralized Curation, Review Lifecycle & Regulatory Provenance
    dgda_dar_number = models.CharField(max_length=64, blank=True, default="", db_index=True)
    regulatory_source = models.CharField(max_length=128, default="DGDA_BANGLADESH")
    curation_status = models.CharField(
        max_length=32,
        choices=MedicineCurationStatus.choices,
        default=MedicineCurationStatus.APPROVED,
        db_index=True,
    )
    version_number = models.PositiveIntegerField(default=1)
    immutable_version_hash = models.CharField(max_length=64, blank=True, default="")
    is_prescription_only = models.BooleanField(default=False, db_index=True)
    is_narcotic = models.BooleanField(default=False, db_index=True)
    is_antibiotic = models.BooleanField(default=False, db_index=True)
    curated_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="curated_medicine_products",
    )
    reviewed_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_medicine_products",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    curation_notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["brand_name"]
        verbose_name = "Medicine Product"
        verbose_name_plural = "Medicine Products"
        indexes = [
            models.Index(fields=["brand_name", "product_id"]),
            models.Index(fields=["-created_at", "product_id"]),
            models.Index(fields=["curation_status", "dgda_dar_number"]),
        ]

    def compute_version_hash(self) -> str:
        import hashlib

        raw = (
            f"{self.dgda_dar_number}|{self.brand_name}|{self.generic_name}|"
            f"{self.dosage_form}|{self.strength}|{self.manufacturer}|{self.version_number}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        if not self.immutable_version_hash or "update_fields" not in kwargs:
            self.immutable_version_hash = self.compute_version_hash()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        dar = f" [{self.dgda_dar_number}]" if self.dgda_dar_number else ""
        return f"{self.brand_name} {self.strength} ({self.generic_name}) - {self.manufacturer}{dar}"


class MedicineCurationHistory(models.Model):
    """
    Architecture Decision: ADR-0070
    Audited history of regulatory reviews, status changes, and version increments on global medicine master.
    """

    history_id = models.CharField(max_length=64, primary_key=True)
    product = models.ForeignKey(MedicineProduct, on_delete=models.CASCADE, related_name="curation_history")
    reviewer = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="medicine_curation_events",
    )
    action = models.CharField(max_length=64)
    old_status = models.CharField(max_length=32, blank=True, default="")
    new_status = models.CharField(max_length=32)
    version_number = models.PositiveIntegerField(default=1)
    notes = models.TextField(blank=True, default="")
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        verbose_name = "Medicine Curation History"
        verbose_name_plural = "Medicine Curation Histories"


class TenantMedicineConfig(models.Model):
    """
    Architecture Decision: ADR-0071
    Tenant-specific configuration and local overrides for a global master medicine.
    Enables tenant-scoped brand aliases, local SKUs, custom retail MRP, shelf locations,
    reorder thresholds, and visibility toggles without mutating the immutable global master.
    Strictly prevents relaxing global DGDA regulatory mandates (e.g. prescription requirements).
    """

    config_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="medicine_configs", db_index=True)
    product = models.ForeignKey(MedicineProduct, on_delete=models.CASCADE, related_name="tenant_configs", db_index=True)

    local_brand_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Tenant-customized brand alias, nickname, or colloquial Bengali name.",
    )
    local_sku = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="Internal barcode or SKU specific to this tenant store.",
    )
    custom_mrp_unit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Tenant custom retail price (BDT). If null, master mrp_unit is used.",
    )
    custom_cost_unit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Tenant default acquisition cost (BDT). If null, master cost_unit is used.",
    )
    is_visible = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Store visibility toggle. Set to False to hide this product from POS search and cashier view.",
    )
    shelf_location = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Physical shelf, rack, or bin location in store.",
    )
    low_stock_threshold = models.PositiveIntegerField(
        default=10,
        help_text="Branch low stock reorder alert threshold.",
    )
    requires_prescription_override = models.BooleanField(
        null=True,
        blank=True,
        help_text="Tenant can enforce stricter prescription rules. Strictly cannot relax master is_prescription_only=True!",
    )
    notes = models.TextField(blank=True, default="", help_text="Internal pharmacy notes for dispensing or sourcing.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Tenant Medicine Configuration"
        verbose_name_plural = "Tenant Medicine Configurations"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "product"], name="unique_tenant_medicine_config"),
            models.CheckConstraint(
                condition=models.Q(custom_mrp_unit__isnull=True) | models.Q(custom_mrp_unit__gte=Decimal("0.01")),
                name="check_positive_custom_mrp",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "is_visible"]),
            models.Index(fields=["tenant", "local_sku"]),
            models.Index(fields=["tenant", "product"]),
        ]

    def clean(self):
        super().clean()
        from django.core.exceptions import ValidationError

        # Invariant 1: Cannot relax global prescription requirement
        if self.product_id and hasattr(self, "product") and self.product:
            if self.product.is_prescription_only and self.requires_prescription_override is False:
                raise ValidationError(
                    {
                        "requires_prescription_override": (
                            "Cannot relax DGDA regulatory prescription requirement. "
                            "Global master product is prescription-only (POM)."
                        )
                    }
                )
            if (
                self.product.is_narcotic or self.product.is_antibiotic
            ) and self.requires_prescription_override is False:
                raise ValidationError(
                    {
                        "requires_prescription_override": (
                            "Cannot relax DGDA narcotic/antibiotic dispensing verification mandate."
                        )
                    }
                )
        # Invariant 2: Positive custom MRP
        if self.custom_mrp_unit is not None and self.custom_mrp_unit < Decimal("0.01"):
            raise ValidationError({"custom_mrp_unit": "Custom MRP must be at least 0.01 BDT."})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    @property
    def effective_brand_name(self) -> str:
        if self.local_brand_name and self.local_brand_name.strip():
            return self.local_brand_name.strip()
        return self.product.brand_name if self.product else ""

    @property
    def effective_mrp_unit(self) -> Decimal:
        if self.custom_mrp_unit is not None:
            return self.custom_mrp_unit
        return self.product.mrp_unit if self.product else Decimal("0.00")

    @property
    def effective_cost_unit(self) -> Decimal:
        if self.custom_cost_unit is not None:
            return self.custom_cost_unit
        return self.product.cost_unit if self.product else Decimal("0.00")

    @property
    def effective_requires_prescription(self) -> bool:
        if not self.product:
            return bool(self.requires_prescription_override)
        if self.product.is_prescription_only or self.product.is_narcotic or self.product.is_antibiotic:
            return True
        return bool(self.requires_prescription_override)

    def __str__(self) -> str:
        return f"{self.tenant_id} -> {self.effective_brand_name} (MRP: {self.effective_mrp_unit})"


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
            models.Index(fields=["tenant", "-created_at", "invoice_id"]),
            models.Index(fields=["tenant", "customer_phone"]),
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
    DEAD_LETTER = "DEAD_LETTER", "Dead Letter"


class AsyncJobRecord(models.Model):
    """
    Audit log and lifecycle tracker for asynchronous background jobs executed by Celery and Redis.
    Architecture Decision: ADR-0031 & ADR-0057
    Restricted to NOTIFICATION, REMINDER, and DERIVED_ANALYTICS categories.
    Enforces idempotency keys, bounded retries, and dead-letter queue routing.
    """

    job_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="async_jobs")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, null=True, blank=True, related_name="async_jobs")
    category = models.CharField(max_length=32, choices=JobCategory.choices)
    task_name = models.CharField(max_length=255)
    status = models.CharField(max_length=32, choices=JobStatus.choices, default=JobStatus.PENDING, db_index=True)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    result = models.JSONField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    max_retries = models.PositiveIntegerField(default=3)
    scheduled_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    dead_letter_reason = models.TextField(blank=True)
    dead_lettered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-scheduled_at"]
        indexes = [
            models.Index(fields=["tenant", "category", "status"]),
            models.Index(fields=["status", "scheduled_at"]),
            models.Index(fields=["tenant", "idempotency_key"]),
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


class SupportAccessStatus(models.TextChoices):
    PENDING = "PENDING", "Pending Approval"
    APPROVED = "APPROVED", "Approved & Active"
    REJECTED = "REJECTED", "Rejected by Tenant"
    REVOKED = "REVOKED", "Revoked Prematurely"
    EXPIRED = "EXPIRED", "Expired"


class SupportAccessScope(models.TextChoices):
    DIAGNOSTIC_READ = "DIAGNOSTIC_READ", "Diagnostic & Configuration Read-Only"
    BILLING_READ = "BILLING_READ", "Subscription & Billing Read-Only"
    BREAK_GLASS_REPAIR = "BREAK_GLASS_REPAIR", "Emergency Break-Glass Repair"


class SupportAccessGrant(models.Model):
    """
    Architecture Decision: ADR-0068
    Secure support access without unsafe impersonation ("ghost login").
    Enforces tenant owner consent, narrow scopes, automatic expiration,
    break-glass audit governance, and non-dismissible client notice banners.
    """

    grant_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="support_grants")
    requested_by_operator = models.ForeignKey(
        "identity.PharmacyUser", on_delete=models.CASCADE, related_name="requested_support_grants"
    )
    approved_by_user = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_support_grants",
    )
    scope = models.CharField(
        max_length=32, choices=SupportAccessScope.choices, default=SupportAccessScope.DIAGNOSTIC_READ
    )
    status = models.CharField(
        max_length=20, choices=SupportAccessStatus.choices, default=SupportAccessStatus.PENDING, db_index=True
    )
    reason = models.TextField()
    ticket_reference = models.CharField(max_length=64, blank=True, default="")
    is_break_glass = models.BooleanField(default=False)
    break_glass_justification = models.TextField(blank=True, default="")
    duration_minutes = models.PositiveIntegerField(default=30)
    approved_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revocation_reason = models.TextField(blank=True, default="")
    revoked_by_user = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revoked_support_grants",
    )
    access_token = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Support Access Grant"
        verbose_name_plural = "Support Access Grants"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["expires_at", "status"]),
        ]

    def is_currently_active(self) -> bool:
        if self.status != SupportAccessStatus.APPROVED:
            return False
        if not self.expires_at:
            return False
        return timezone.now() < self.expires_at

    def expire_if_needed(self) -> bool:
        if self.status == SupportAccessStatus.APPROVED and self.expires_at and timezone.now() >= self.expires_at:
            self.status = SupportAccessStatus.EXPIRED
            self.save(update_fields=["status", "updated_at"])
            return True
        return False

    def __str__(self) -> str:
        bg = " [BREAK-GLASS]" if self.is_break_glass else ""
        return f"SupportGrant {self.grant_id} ({self.tenant_id}) [{self.scope}] {self.status}{bg}"


class TicketStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    WAITING_ON_TENANT = "WAITING_ON_TENANT", "Waiting on Tenant"
    RESOLVED = "RESOLVED", "Resolved"
    CLOSED = "CLOSED", "Closed"


class TicketPriority(models.TextChoices):
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"
    URGENT = "URGENT", "Urgent / Blocker"


class TicketCategory(models.TextChoices):
    BILLING = "BILLING", "Billing & Subscription"
    HARDWARE_PRINTER = "HARDWARE_PRINTER", "Thermal Printer & Peripherals"
    SYNC_OFFLINE = "SYNC_OFFLINE", "Offline POS & Cloud Sync"
    INVENTORY_STOCK = "INVENTORY_STOCK", "Batch Stock & Unit Conversions"
    REGULATORY_DGDA = "REGULATORY_DGDA", "DGDA Regulatory & Narcotics Logs"
    GENERAL_SUPPORT = "GENERAL_SUPPORT", "General Inquiry & Support"


class PlatformSupportTicket(models.Model):
    """
    Architecture Decision: ADR-0069
    Platform support ticket system with tenant ownership, status, assignment,
    internal notes, attachments, and audited action history.
    """

    ticket_id = models.CharField(max_length=64, primary_key=True)
    ticket_number = models.CharField(max_length=32, unique=True, db_index=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="support_tickets")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="support_tickets")
    created_by = models.ForeignKey(
        "identity.PharmacyUser", on_delete=models.CASCADE, related_name="created_support_tickets"
    )
    assigned_to = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_support_tickets",
    )
    title = models.CharField(max_length=200)
    description = models.TextField()
    category = models.CharField(
        max_length=32, choices=TicketCategory.choices, default=TicketCategory.GENERAL_SUPPORT, db_index=True
    )
    priority = models.CharField(
        max_length=16, choices=TicketPriority.choices, default=TicketPriority.MEDIUM, db_index=True
    )
    status = models.CharField(max_length=32, choices=TicketStatus.choices, default=TicketStatus.OPEN, db_index=True)
    resolution_summary = models.TextField(blank=True, default="")
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Platform Support Ticket"
        verbose_name_plural = "Platform Support Tickets"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["assigned_to", "status"]),
            models.Index(fields=["priority", "status"]),
        ]

    def __str__(self) -> str:
        return f"Ticket {self.ticket_number} ({self.tenant_id}) [{self.priority}] - {self.status}"


class TicketMessage(models.Model):
    """
    Threaded message / note for a support ticket.
    is_internal=True indicates private support operator notes, hidden from tenant users.
    """

    message_id = models.CharField(max_length=64, primary_key=True)
    ticket = models.ForeignKey(PlatformSupportTicket, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey("identity.PharmacyUser", on_delete=models.CASCADE, related_name="ticket_messages")
    message_text = models.TextField()
    is_internal = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Ticket Message"
        verbose_name_plural = "Ticket Messages"


class TicketAttachment(models.Model):
    """
    File attachment linked to a support ticket or specific message.
    """

    attachment_id = models.CharField(max_length=64, primary_key=True)
    ticket = models.ForeignKey(PlatformSupportTicket, on_delete=models.CASCADE, related_name="attachments")
    message = models.ForeignKey(
        TicketMessage, on_delete=models.CASCADE, null=True, blank=True, related_name="attachments"
    )
    file_name = models.CharField(max_length=255)
    file_url = models.CharField(max_length=512)
    file_size_bytes = models.PositiveIntegerField(default=0)
    mime_type = models.CharField(max_length=128, default="application/octet-stream")
    uploaded_by = models.ForeignKey("identity.PharmacyUser", on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Ticket Attachment"
        verbose_name_plural = "Ticket Attachments"


class TicketActionHistory(models.Model):
    """
    Audited history of transitions, assignments, and status updates on a support ticket.
    """

    history_id = models.CharField(max_length=64, primary_key=True)
    ticket = models.ForeignKey(PlatformSupportTicket, on_delete=models.CASCADE, related_name="action_history")
    actor = models.ForeignKey(
        "identity.PharmacyUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="ticket_actions"
    )
    action = models.CharField(max_length=64)
    old_value = models.CharField(max_length=255, blank=True, default="")
    new_value = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        verbose_name = "Ticket Action History"
        verbose_name_plural = "Ticket Action Histories"
