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

from django.core.validators import MaxValueValidator, MinValueValidator
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
    is_over_quota_locked = models.BooleanField(default=False)
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
    PENDING = "PENDING", "Pending Payment"
    TRIAL = "TRIAL", "Active Evaluation Trial"
    ACTIVE = "ACTIVE", "Active & In Good Standing"
    PAST_DUE = "PAST_DUE", "Past Due Payment"
    GRACE_PERIOD = "GRACE_PERIOD", "Grace Period"
    SUSPENDED = "SUSPENDED", "Temporarily Suspended"
    CANCELLED = "CANCELLED", "Cancelled"
    EXPIRED = "EXPIRED", "Expired"


class SubscriptionRecord(models.Model):
    """
    SaaS Tenant Subscription entitlement, branch limits, and lifecycle record.
    Architecture Decision: ADR-0015, ADR-0033 & ADR-0084
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
    cancel_at_period_end = models.BooleanField(default=False)
    cancellation_reason = models.TextField(blank=True, default="")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-renewal_date"]
        verbose_name = "Tenant Subscription"
        verbose_name_plural = "Tenant Subscriptions"

    def __str__(self) -> str:
        return f"{self.tenant.name} [{self.plan_tier}] - Status: {self.status} (Exp: {self.renewal_date})"

    def can_transition_to(self, target_status: str) -> bool:
        """Evaluates whether the subscription can transition to the target status under ADR-0084."""
        target = str(target_status).upper().strip()
        if target not in SubscriptionStatus.values:
            return False
        if self.status == target:
            return True

        valid_transitions = {
            SubscriptionStatus.PENDING: [
                SubscriptionStatus.TRIAL,
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.CANCELLED,
            ],
            SubscriptionStatus.TRIAL: [
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.EXPIRED,
                SubscriptionStatus.CANCELLED,
            ],
            SubscriptionStatus.ACTIVE: [
                SubscriptionStatus.PAST_DUE,
                SubscriptionStatus.GRACE_PERIOD,
                SubscriptionStatus.SUSPENDED,
                SubscriptionStatus.CANCELLED,
            ],
            SubscriptionStatus.PAST_DUE: [
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.GRACE_PERIOD,
                SubscriptionStatus.SUSPENDED,
                SubscriptionStatus.CANCELLED,
            ],
            SubscriptionStatus.GRACE_PERIOD: [
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.SUSPENDED,
                SubscriptionStatus.EXPIRED,
                SubscriptionStatus.CANCELLED,
            ],
            SubscriptionStatus.SUSPENDED: [
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.CANCELLED,
                SubscriptionStatus.EXPIRED,
            ],
            SubscriptionStatus.CANCELLED: [
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.EXPIRED,
            ],
            SubscriptionStatus.EXPIRED: [
                SubscriptionStatus.ACTIVE,
            ],
        }
        return target in valid_transitions.get(self.status, [])

    def is_checkout_allowed(self) -> bool:
        """Returns True if the subscription status permits POS checkout dispensing."""
        return self.status in [
            SubscriptionStatus.TRIAL,
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.PAST_DUE,
            SubscriptionStatus.GRACE_PERIOD,
        ]

    def is_delinquent(self) -> bool:
        """Returns True if subscription payment is past due, in grace, or suspended."""
        return self.status in [
            SubscriptionStatus.PAST_DUE,
            SubscriptionStatus.GRACE_PERIOD,
            SubscriptionStatus.SUSPENDED,
            SubscriptionStatus.EXPIRED,
        ]


class RenewalInvoiceStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    ISSUED = "ISSUED", "Issued"
    PAID = "PAID", "Paid"
    PAST_DUE = "PAST_DUE", "Past Due"
    VOID = "VOID", "Void"
    REFUNDED = "REFUNDED", "Refunded"


class SubscriptionRenewalInvoice(models.Model):
    """
    Architecture Decision: ADR-0015, ADR-0033 & ADR-0087
    SaaS commercial subscription renewal invoice, tax computation,
    and append-only status transition history.
    """

    invoice_id = models.CharField(max_length=64, primary_key=True)
    invoice_number = models.CharField(max_length=64, unique=True, db_index=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="renewal_invoices")
    subscription = models.ForeignKey(
        SubscriptionRecord, on_delete=models.SET_NULL, null=True, blank=True, related_name="renewal_invoices"
    )
    plan_tier = models.CharField(max_length=32, choices=PlanTier.choices, default=PlanTier.STANDARD)
    billing_cycle = models.CharField(max_length=32, default="MONTHLY")
    period_start = models.DateField(db_index=True)
    period_end = models.DateField(db_index=True)

    subtotal_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Base tier plan charge + addon units in BDT.",
    )
    discount_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Promotional or annual prepay savings in BDT.",
    )
    tax_rate_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("5.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
        help_text="Statutory SaaS VAT rate percentage (e.g. 5.00%).",
    )
    tax_amount_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Calculated statutory VAT in BDT.",
    )
    total_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Net payable amount in BDT.",
    )

    status = models.CharField(
        max_length=32,
        choices=RenewalInvoiceStatus.choices,
        default=RenewalInvoiceStatus.ISSUED,
        db_index=True,
    )
    payment_method = models.CharField(max_length=64, blank=True, default="")
    payment_reference = models.CharField(max_length=128, blank=True, default="", db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    status_history_json = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Subscription Renewal Invoice"
        verbose_name_plural = "Subscription Renewal Invoices"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["period_start", "period_end"]),
        ]

    def __str__(self) -> str:
        return f"{self.invoice_number} [{self.tenant.tenant_id}] - {self.total_bdt} BDT ({self.status})"


class ManualPaymentClaimStatus(models.TextChoices):
    PENDING_VERIFICATION = "PENDING_VERIFICATION", "Pending Verification"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"


class ManualPaymentMethod(models.TextChoices):
    BKASH = "BKASH", "bKash"
    NAGAD = "NAGAD", "Nagad"
    ROCKET = "ROCKET", "Rocket"
    BANK_TRANSFER = "BANK_TRANSFER", "Bank Transfer"
    CASH_DEPOSIT = "CASH_DEPOSIT", "Cash Deposit"


class ManualPaymentClaim(models.Model):
    """
    Architecture Decision: ADR-0015, ADR-0033 & ADR-0088
    Manual subscription payment verification claim with maker-checker approvals
    and recorded activation outcomes.
    """

    claim_id = models.CharField(max_length=64, primary_key=True)
    claim_number = models.CharField(max_length=64, unique=True, db_index=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="manual_payment_claims")
    invoice = models.ForeignKey(SubscriptionRenewalInvoice, on_delete=models.CASCADE, related_name="manual_payment_claims")

    payment_method = models.CharField(
        max_length=32,
        choices=ManualPaymentMethod.choices,
        default=ManualPaymentMethod.BKASH,
    )
    transaction_reference = models.CharField(max_length=128, db_index=True)
    sender_account_number = models.CharField(max_length=64)
    claimed_amount_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Claimed payment amount in BDT.",
    )
    evidence_file_url = models.CharField(max_length=512)
    evidence_notes = models.TextField(blank=True, default="")

    status = models.CharField(
        max_length=32,
        choices=ManualPaymentClaimStatus.choices,
        default=ManualPaymentClaimStatus.PENDING_VERIFICATION,
        db_index=True,
    )

    maker_user_id = models.CharField(max_length=64, db_index=True)
    maker_user_email = models.EmailField(max_length=255)
    checker_user_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    checker_user_email = models.CharField(max_length=255, null=True, blank=True)
    checker_notes = models.TextField(blank=True, default="")
    rejection_reason = models.TextField(blank=True, default="")

    submitted_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    activation_outcomes_json = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Manual Payment Claim"
        verbose_name_plural = "Manual Payment Claims"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["invoice", "status"]),
            models.Index(fields=["transaction_reference"]),
        ]

    def __str__(self) -> str:
        return f"{self.claim_number} [{self.tenant.tenant_id}] - {self.claimed_amount_bdt} BDT ({self.status})"


class PaymentGatewayName(models.TextChoices):
    BKASH = "BKASH", "bKash"
    NAGAD = "NAGAD", "Nagad"
    SSLCOMMERZ = "SSLCOMMERZ", "SSLCommerz"
    MOCK_GATEWAY = "MOCK_GATEWAY", "Mock Gateway"


class GatewayWebhookStatus(models.TextChoices):
    RECEIVED = "RECEIVED", "Received"
    PROCESSED = "PROCESSED", "Processed"
    DUPLICATE = "DUPLICATE", "Duplicate"
    FAILED = "FAILED", "Failed"
    SIGNATURE_INVALID = "SIGNATURE_INVALID", "Signature Invalid"


class PaymentGatewayWebhookLog(models.Model):
    """
    Architecture Decision: ADR-0015, ADR-0033 & ADR-0089
    Immutable log of incoming payment gateway webhooks, signed verification,
    and idempotent deduplication. NO payment secrets are stored here.
    """

    event_id = models.CharField(max_length=64, primary_key=True)
    gateway_name = models.CharField(max_length=32, choices=PaymentGatewayName.choices, default=PaymentGatewayName.BKASH)
    event_type = models.CharField(max_length=64, default="PAYMENT_COMPLETED", db_index=True)
    idempotency_key = models.CharField(max_length=128, db_index=True)
    gateway_payment_id = models.CharField(max_length=128, db_index=True)
    invoice_number = models.CharField(max_length=64, db_index=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.SET_NULL, null=True, blank=True, related_name="gateway_webhook_logs")
    amount_bdt = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=8, default="BDT")
    payload_hash = models.CharField(max_length=64)
    payload_json = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=32, choices=GatewayWebhookStatus.choices, default=GatewayWebhookStatus.RECEIVED, db_index=True)
    signature_verified = models.BooleanField(default=False)
    error_message = models.TextField(blank=True, default="")
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Payment Gateway Webhook Log"
        verbose_name_plural = "Payment Gateway Webhook Logs"
        indexes = [
            models.Index(fields=["gateway_name", "idempotency_key"]),
            models.Index(fields=["gateway_payment_id", "status"]),
            models.Index(fields=["invoice_number", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.gateway_name}:{self.event_type} [{self.gateway_payment_id}] - {self.status}"


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


class PlatformFeatureFlag(models.Model):
    """
    Architecture Decision: ADR-0072
    Server-authoritative platform feature flag with gradual rollout,
    subscription plan tier gating, and instant emergency kill switch.
    """

    flag_key = models.CharField(max_length=64, primary_key=True)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    is_enabled_globally = models.BooleanField(default=False)
    default_enabled = models.BooleanField(default=False, help_text="Safe default value when no targeting matches.")
    rollout_percentage = models.PositiveSmallIntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Targeted rollout percentage (0-100).",
    )
    min_plan_tier = models.CharField(
        max_length=32,
        choices=PlanTier.choices,
        default=PlanTier.BASIC,
    )
    is_kill_switch_active = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Emergency kill switch. When True, forces flag to False platform-wide.",
    )
    kill_switch_reason = models.TextField(blank=True, default="")
    kill_switch_tripped_at = models.DateTimeField(null=True, blank=True)
    kill_switch_tripped_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tripped_kill_switches",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_feature_flags",
    )

    class Meta:
        ordering = ["flag_key"]
        verbose_name = "Platform Feature Flag"
        verbose_name_plural = "Platform Feature Flags"

    def __str__(self) -> str:
        status_str = (
            "KILL-SWITCH"
            if self.is_kill_switch_active
            else ("ENABLED" if self.is_enabled_globally else f"{self.rollout_percentage}%")
        )
        return f"Flag {self.flag_key} [{status_str}]"


class TenantFeatureFlagOverride(models.Model):
    """
    Architecture Decision: ADR-0072
    Tenant-specific targeting or exclusion override for a platform feature flag.
    """

    override_id = models.CharField(max_length=64, primary_key=True)
    flag = models.ForeignKey(PlatformFeatureFlag, on_delete=models.CASCADE, related_name="tenant_overrides")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="feature_flag_overrides")
    is_enabled = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_flag_overrides",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Tenant Feature Flag Override"
        verbose_name_plural = "Tenant Feature Flag Overrides"
        constraints = [
            models.UniqueConstraint(fields=["flag", "tenant"], name="unique_flag_tenant_override"),
        ]
        indexes = [
            models.Index(fields=["tenant", "flag"]),
        ]

    def __str__(self) -> str:
        state = "ENABLED" if self.is_enabled else "DISABLED"
        return f"{self.tenant_id} -> {self.flag_id}: {state}"


class TenantDataExportStatus(models.TextChoices):
    REQUESTED = "REQUESTED", "Requested"
    APPROVED = "APPROVED", "Approved"
    PROCESSING = "PROCESSING", "Processing"
    COMPLETED = "COMPLETED", "Completed"
    REJECTED = "REJECTED", "Rejected"
    EXPIRED = "EXPIRED", "Expired"
    FAILED = "FAILED", "Failed"


class TenantDataExportRequest(models.Model):
    """
    Architecture Decision: ADR-0073
    Governed tenant data export request, multi-tier approval workflow,
    cryptographic verification, and expiring download tokens.
    """

    export_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="data_exports")
    status = models.CharField(
        max_length=32,
        choices=TenantDataExportStatus.choices,
        default=TenantDataExportStatus.REQUESTED,
        db_index=True,
    )
    requested_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_tenant_exports",
    )
    approved_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_tenant_exports",
    )
    rejection_reason = models.TextField(blank=True, default="")
    include_patient_pii = models.BooleanField(
        default=False,
        help_text="When False, patient names and identifying contacts are redacted.",
    )
    include_financials = models.BooleanField(default=True)
    include_prescriptions = models.BooleanField(default=True)
    include_inventory = models.BooleanField(default=True)
    file_format = models.CharField(max_length=16, default="JSON")
    sha256_checksum = models.CharField(max_length=64, blank=True, default="")
    artifact_storage_path = models.CharField(max_length=255, blank=True, default="")
    file_size_bytes = models.BigIntegerField(default=0)
    download_token = models.CharField(max_length=128, blank=True, default="", db_index=True)
    download_count = models.PositiveIntegerField(default=0)
    max_downloads = models.PositiveIntegerField(default=5)
    expires_at = models.DateTimeField(null=True, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    audit_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        verbose_name = "Tenant Data Export Request"
        verbose_name_plural = "Tenant Data Export Requests"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["download_token"]),
        ]

    def __str__(self) -> str:
        return f"{self.export_id} [{self.tenant_id}] ({self.status})"


class TenantDeletionStatus(models.TextChoices):
    SUBMITTED = "SUBMITTED", "Submitted"
    UNDER_REVIEW = "UNDER_REVIEW", "Under Review"
    HOLD_ACTIVE = "HOLD_ACTIVE", "Hold Active"
    ELIGIBLE_FOR_PURGE = "ELIGIBLE_FOR_PURGE", "Eligible for Purge"
    CONFIRMED = "CONFIRMED", "Confirmed"
    PURGED = "PURGED", "Purged"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"


class TenantDeletionRequest(models.Model):
    """
    Architecture Decision: ADR-0074
    Governed tenant deletion request, statutory retention hold assessment,
    multi-step confirmation, and purge metadata.
    """

    request_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="deletion_requests")
    status = models.CharField(
        max_length=32,
        choices=TenantDeletionStatus.choices,
        default=TenantDeletionStatus.SUBMITTED,
        db_index=True,
    )
    reason = models.TextField(help_text="Operational or regulatory justification for tenant deletion")
    has_active_holds = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True if DGDA clinical or financial tax retention holds block physical deletion",
    )
    active_holds_summary = models.JSONField(default=dict, blank=True)
    hold_statutory_horizon_days = models.PositiveIntegerField(
        default=730,
        help_text="Statutory retention horizon in days (e.g. 730d Rx, 1825d controlled)",
    )
    confirmation_code = models.CharField(max_length=32, blank=True, default="")
    confirmation_text = models.CharField(max_length=128, blank=True, default="")
    rejection_reason = models.TextField(blank=True, default="")
    requested_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_tenant_deletions",
    )
    assessed_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assessed_tenant_deletions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    assessed_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    purged_at = models.DateTimeField(null=True, blank=True)
    audit_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Tenant Deletion Request"
        verbose_name_plural = "Tenant Deletion Requests"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"{self.request_id} [{self.tenant_id}] ({self.status})"


class CrossTenantMetricPeriod(models.TextChoices):
    DAILY = "DAILY", "Daily"
    WEEKLY = "WEEKLY", "Weekly"
    MONTHLY = "MONTHLY", "Monthly"
    QUARTERLY = "QUARTERLY", "Quarterly"
    ANNUAL = "ANNUAL", "Annual"


class CrossTenantMetricStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PUBLISHED = "PUBLISHED", "Published"
    SUPPRESSED = "SUPPRESSED", "Suppressed"
    ARCHIVED = "ARCHIVED", "Archived"


class CrossTenantMetricsSnapshot(models.Model):
    """
    Architecture Decision: ADR-0075
    Immutable, privacy-preserving cross-tenant aggregated telemetry snapshot.
    Enforces k-anonymity (min_cohort_threshold) and differential privacy.
    Strictly excludes all patient PHI/PII and tenant confidential raw microdata.
    """

    snapshot_id = models.CharField(max_length=64, primary_key=True)
    metric_period = models.CharField(
        max_length=16,
        choices=CrossTenantMetricPeriod.choices,
        default=CrossTenantMetricPeriod.MONTHLY,
    )
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=16,
        choices=CrossTenantMetricStatus.choices,
        default=CrossTenantMetricStatus.DRAFT,
    )
    total_active_tenants = models.PositiveIntegerField(default=0)
    contributing_tenants_count = models.PositiveIntegerField(default=0)
    min_cohort_threshold = models.PositiveIntegerField(default=3)
    privacy_filter_applied = models.BooleanField(default=True)
    clinical_metrics = models.JSONField(default=dict, blank=True)
    financial_metrics = models.JSONField(default=dict, blank=True)
    operational_metrics = models.JSONField(default=dict, blank=True)
    cohort_breakdowns = models.JSONField(default=list, blank=True)
    generated_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generated_metric_snapshots",
    )
    published_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    audit_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Cross-Tenant Metrics Snapshot"
        verbose_name_plural = "Cross-Tenant Metrics Snapshots"
        indexes = [
            models.Index(fields=["metric_period", "start_date", "end_date"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"{self.snapshot_id} [{self.metric_period}: {self.start_date} to {self.end_date}] ({self.status})"


class AnnouncementPriority(models.TextChoices):
    INFO = "INFO", "Informational"
    WARNING = "WARNING", "Warning"
    CRITICAL = "CRITICAL", "Critical / High Priority"
    MAINTENANCE = "MAINTENANCE", "Scheduled Maintenance"


class AnnouncementTargetType(models.TextChoices):
    ALL = "ALL", "All Tenants & Branches"
    TENANT_LIST = "TENANT_LIST", "Specific Tenants"
    ROLE_LIST = "ROLE_LIST", "Specific Roles"
    BRANCH_LIST = "BRANCH_LIST", "Specific Branches"


class AnnouncementStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PUBLISHED = "PUBLISHED", "Published"
    ARCHIVED = "ARCHIVED", "Archived"


class SystemAnnouncement(models.Model):
    """
    Architecture Decision: ADR-0076
    Audited platform broadcast bulletins with targeted tenant/role delivery.
    """

    announcement_id = models.CharField(max_length=64, primary_key=True)
    title = models.CharField(max_length=255)
    message = models.TextField()
    priority = models.CharField(
        max_length=16,
        choices=AnnouncementPriority.choices,
        default=AnnouncementPriority.INFO,
    )
    target_type = models.CharField(
        max_length=16,
        choices=AnnouncementTargetType.choices,
        default=AnnouncementTargetType.ALL,
    )
    target_tenants = models.JSONField(default=list, blank=True)
    target_roles = models.JSONField(default=list, blank=True)
    target_branches = models.JSONField(default=list, blank=True)
    starts_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=AnnouncementStatus.choices,
        default=AnnouncementStatus.DRAFT,
    )
    created_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_announcements",
    )
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "System Announcement"
        verbose_name_plural = "System Announcements"
        indexes = [
            models.Index(fields=["status", "-starts_at"]),
            models.Index(fields=["target_type", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.announcement_id}: {self.title} ({self.status})"


class AnnouncementReadReceipt(models.Model):
    """
    Architecture Decision: ADR-0076
    Immutable delivery and read acknowledgement record for system announcements.
    """

    receipt_id = models.CharField(max_length=64, primary_key=True)
    announcement = models.ForeignKey(
        SystemAnnouncement,
        on_delete=models.CASCADE,
        related_name="receipts",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="announcement_receipts",
    )
    user = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.CASCADE,
        related_name="announcement_receipts",
    )
    delivered_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    acknowledged = models.BooleanField(default=False)

    class Meta:
        unique_together = ("announcement", "user")
        ordering = ["-delivered_at"]
        indexes = [
            models.Index(fields=["announcement", "tenant"]),
            models.Index(fields=["user", "acknowledged"]),
        ]

    def __str__(self) -> str:
        return f"Receipt {self.receipt_id} for {self.user_id} on {self.announcement_id} (ack: {self.acknowledged})"


class PlatformApiAccessPolicy(models.Model):
    """
    Architecture Decision: ADR-0077
    Configurable access control policies protecting platform admin APIs,
    enforcing separate roles, mandatory MFA, step-up MFA for critical operations,
    and zero implicit tenant access.
    """

    policy_id = models.CharField(max_length=64, primary_key=True)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    endpoint_pattern = models.CharField(max_length=255)
    http_method = models.CharField(max_length=10, default="*")
    allowed_roles = models.JSONField(default=list, blank=True)
    require_mfa = models.BooleanField(default=True)
    require_step_up = models.BooleanField(default=False)
    enforce_zero_implicit_tenant_access = models.BooleanField(default=True)
    ip_allowlist = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_platform_policies",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Platform API Access Policy"
        verbose_name_plural = "Platform API Access Policies"
        indexes = [
            models.Index(fields=["is_active", "endpoint_pattern"]),
            models.Index(fields=["require_mfa", "require_step_up"]),
        ]

    def __str__(self) -> str:
        return f"{self.policy_id}: {self.name} ({self.http_method} {self.endpoint_pattern})"


class OnboardingWizardStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    COMPLETED = "COMPLETED", "Completed"
    ABANDONED = "ABANDONED", "Abandoned"


class InitialInventoryMode(models.TextChoices):
    EMPTY = "EMPTY_CATALOG", "Empty Catalog (Zero Stock)"
    SEED_BASELINE = "SEED_BASELINE", "Seed Essential BD Medicines Baseline"
    CUSTOM = "CUSTOM_OPENING_STOCK", "Custom Opening Stock"


class TenantOnboardingSession(models.Model):
    """
    Architecture Decision: ADR-0079
    Progressive and atomic Tenant Onboarding Wizard Session.
    Guides multi-step setup across:
      Step 1: Pharmacy Details (Name, Subdomain, Contact Info, Location)
      Step 2: Regulatory & Licensing (DGDA Drug License, Trade License, TIN, Pharmacist)
      Step 3: Branch & Staff Setup (Primary Branch, Initial Staff with PCB Numbers)
      Step 4: Initial Inventory (Seed Essential Catalog or Zero-Stock Baseline)
      Step 5: Subscription & Plan Purchase (Plan Tier, Billing Cycle, Payment Mode)
      Step 6: Atomic Finalization & Audit Trail
    """

    session_id = models.CharField(max_length=64, primary_key=True)
    pharmacy_name = models.CharField(max_length=255)
    trade_license_number = models.CharField(max_length=128, blank=True, default="")
    drug_license_number = models.CharField(max_length=128)  # Mandatory per DGDA
    tin_number = models.CharField(max_length=64, blank=True, default="")
    subdomain = models.CharField(max_length=64, unique=True)
    contact_email = models.EmailField()
    contact_phone = models.CharField(max_length=32)
    owner_full_name = models.CharField(max_length=255)

    # Branch configuration
    branch_name = models.CharField(max_length=255, default="Main Branch")
    branch_address = models.TextField(default="Dhaka, Bangladesh")

    # Step data payloads (JSON)
    initial_staff_data = models.JSONField(default=list, blank=True)
    initial_inventory_mode = models.CharField(
        max_length=32,
        choices=InitialInventoryMode.choices,
        default=InitialInventoryMode.SEED_BASELINE,
    )
    initial_inventory_data = models.JSONField(default=list, blank=True)

    # Subscription & Plan
    plan_tier = models.CharField(max_length=32, choices=PlanTier.choices, default=PlanTier.STANDARD)
    billing_cycle = models.CharField(max_length=32, default="MONTHLY")
    is_trial = models.BooleanField(default=False)
    payment_method = models.CharField(max_length=32, default="BKASH")

    # Wizard Lifecycle
    current_step = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(
        max_length=32,
        choices=OnboardingWizardStatus.choices,
        default=OnboardingWizardStatus.DRAFT,
    )

    # Provisioned Outcome
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="onboarding_sessions",
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_onboarding_sessions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Tenant Onboarding Session"
        verbose_name_plural = "Tenant Onboarding Sessions"
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["subdomain"]),
            models.Index(fields=["drug_license_number"]),
        ]

    def __str__(self) -> str:
        return f"OnboardingSession {self.session_id} - {self.pharmacy_name} ({self.status})"


class PlanCatalogueRecord(models.Model):
    """
    SaaS Plan Catalogue Record.
    Architecture Decision: ADR-0015 & ADR-0081
    Defines versioned commercial tiers (Basic, Standard, Pro), BDT pricing, billing cycles, quotas and feature matrix.
    """

    plan_code = models.CharField(max_length=32, primary_key=True)  # e.g. PLAN_BASIC, PLAN_STANDARD, PLAN_PRO
    tier = models.CharField(max_length=32, choices=PlanTier.choices, db_index=True)
    name = models.CharField(max_length=128)
    description_en = models.TextField(blank=True, default="")
    description_bn = models.TextField(blank=True, default="")
    version = models.PositiveIntegerField(default=1)
    monthly_price_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Standard monthly pricing in BDT.",
    )
    annual_price_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Discounted annual pricing in BDT.",
    )
    currency = models.CharField(max_length=8, default="BDT")
    supported_billing_cycles = models.JSONField(default=list)  # ["MONTHLY", "ANNUAL"]
    max_branches = models.PositiveIntegerField(default=1)
    max_devices_per_branch = models.PositiveIntegerField(default=1)
    max_total_devices = models.PositiveIntegerField(default=1)
    max_staff_users = models.PositiveIntegerField(default=3)
    features = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["monthly_price_bdt"]
        verbose_name = "Plan Catalogue Record"
        verbose_name_plural = "Plan Catalogue Records"
        indexes = [
            models.Index(fields=["tier", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.plan_code} v{self.version}) - {self.monthly_price_bdt} BDT/mo"


class TenantAppliedEntitlement(models.Model):
    """
    Immutable snapshot of applied plan entitlements, quotas, and terms locked into a tenant.
    Architecture Decision: ADR-0081
    Ensures existing subscriptions remain unaffected by future plan catalogue updates.
    """

    entitlement_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="applied_entitlements")
    plan_code = models.CharField(max_length=32, db_index=True)
    plan_tier = models.CharField(max_length=32, choices=PlanTier.choices, db_index=True)
    plan_version = models.PositiveIntegerField(default=1)
    billing_cycle = models.CharField(max_length=32, default="MONTHLY")
    unit_price_bdt = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=8, default="BDT")
    effective_from = models.DateTimeField(default=timezone.now)
    effective_until = models.DateTimeField(null=True, blank=True)
    max_branches = models.PositiveIntegerField(default=1)
    max_devices_per_branch = models.PositiveIntegerField(default=1)
    max_total_devices = models.PositiveIntegerField(default=1)
    max_staff_users = models.PositiveIntegerField(default=3)
    features_snapshot = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    applied_by = models.CharField(max_length=64, default="SYSTEM")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-effective_from"]
        verbose_name = "Tenant Applied Entitlement"
        verbose_name_plural = "Tenant Applied Entitlements"
        indexes = [
            models.Index(fields=["tenant", "is_active"]),
            models.Index(fields=["tenant", "-effective_from"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id} Entitlement [{self.plan_code} v{self.plan_version}] - Active: {self.is_active}"


class SubscriptionUpgradeStatus(models.TextChoices):
    QUOTED = "QUOTED", "Quoted (Pending Confirmation)"
    PENDING_PAYMENT = "PENDING_PAYMENT", "Pending Payment"
    COMPLETED = "COMPLETED", "Completed (Entitlements Applied)"
    CANCELLED = "CANCELLED", "Cancelled"


class SubscriptionUpgradeRecord(models.Model):
    """
    Immutable audit record for prorated subscription upgrades.
    Architecture Decision: ADR-0090
    Tracks proration mathematics, daily rates, unused credits, target fees,
    transition timestamp, and immediate entitlement application.
    """

    upgrade_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="subscription_upgrades")
    subscription = models.ForeignKey(SubscriptionRecord, on_delete=models.CASCADE, related_name="upgrades")
    invoice = models.ForeignKey(
        SubscriptionRenewalInvoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="upgrade_records",
    )

    from_plan_tier = models.CharField(max_length=32, choices=PlanTier.choices)
    from_plan_code = models.CharField(max_length=32)
    from_billing_cycle = models.CharField(max_length=32, default="MONTHLY")

    to_plan_tier = models.CharField(max_length=32, choices=PlanTier.choices)
    to_plan_code = models.CharField(max_length=32)
    to_billing_cycle = models.CharField(max_length=32, default="MONTHLY")

    transition_at = models.DateTimeField(default=timezone.now, db_index=True)

    total_cycle_days = models.PositiveIntegerField(default=30)
    used_days = models.PositiveIntegerField(default=0)
    remaining_days = models.PositiveIntegerField(default=30)

    current_plan_amount_bdt = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    current_plan_daily_rate_bdt = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0.0000"))
    unused_credit_bdt = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    target_plan_amount_bdt = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    target_plan_daily_rate_bdt = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0.0000"))
    prorated_charge_bdt = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    net_payable_bdt = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    status = models.CharField(
        max_length=32,
        choices=SubscriptionUpgradeStatus.choices,
        default=SubscriptionUpgradeStatus.QUOTED,
        db_index=True,
    )
    payment_method = models.CharField(max_length=32, blank=True, default="")
    payment_reference = models.CharField(max_length=128, blank=True, default="")

    applied_entitlement = models.ForeignKey(
        TenantAppliedEntitlement,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="originating_upgrade",
    )

    metadata_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    executed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Subscription Upgrade Record"
        verbose_name_plural = "Subscription Upgrade Records"
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["subscription", "-transition_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"Upgrade {self.upgrade_id}: {self.from_plan_tier} -> {self.to_plan_tier} ({self.status}) BDT {self.net_payable_bdt}"



class DowngradeEffectiveMode(models.TextChoices):
    IMMEDIATE = "IMMEDIATE", "Immediate"
    END_OF_CYCLE = "END_OF_CYCLE", "End of Billing Cycle"


class SubscriptionDowngradeStatus(models.TextChoices):
    SCHEDULED = "SCHEDULED", "Scheduled for Next Renewal"
    APPLIED = "APPLIED", "Applied"
    CANCELLED = "CANCELLED", "Cancelled"


class SubscriptionDowngradeRecord(models.Model):
    """
    Immutable audit record for subscription downgrades and over-quota handling.
    Architecture Decision: ADR-0091
    Tracks downgrade transitions, retained vs locked excess branches,
    and preserves 100% of historical data without deletion.
    """

    downgrade_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="subscription_downgrades")
    subscription = models.ForeignKey(SubscriptionRecord, on_delete=models.CASCADE, related_name="downgrades")

    from_plan_tier = models.CharField(max_length=32, choices=PlanTier.choices)
    from_plan_code = models.CharField(max_length=32)
    from_billing_cycle = models.CharField(max_length=32, default="MONTHLY")

    to_plan_tier = models.CharField(max_length=32, choices=PlanTier.choices)
    to_plan_code = models.CharField(max_length=32)
    to_billing_cycle = models.CharField(max_length=32, default="MONTHLY")

    effective_mode = models.CharField(
        max_length=32,
        choices=DowngradeEffectiveMode.choices,
        default=DowngradeEffectiveMode.IMMEDIATE,
    )
    scheduled_effective_date = models.DateField(null=True, blank=True)

    active_branches_at_downgrade = models.PositiveIntegerField(default=1)
    allowed_branches_quota = models.PositiveIntegerField(default=1)
    is_over_quota = models.BooleanField(default=False)

    retained_branch_ids = models.JSONField(default=list, blank=True)
    locked_branch_ids = models.JSONField(default=list, blank=True)

    status = models.CharField(
        max_length=32,
        choices=SubscriptionDowngradeStatus.choices,
        default=SubscriptionDowngradeStatus.APPLIED,
        db_index=True,
    )

    applied_entitlement = models.ForeignKey(
        TenantAppliedEntitlement,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="originating_downgrade",
    )

    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    executed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Subscription Downgrade Record"
        verbose_name_plural = "Subscription Downgrade Records"
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"Downgrade {self.downgrade_id}: {self.from_plan_tier} -> {self.to_plan_tier} ({self.status})"


class CancellationEffectiveMode(models.TextChoices):
    END_OF_BILLING_PERIOD = "END_OF_BILLING_PERIOD", "End of Current Billing Period"
    IMMEDIATE = "IMMEDIATE", "Immediate"


class SubscriptionCancellationRecord(models.Model):
    """
    Immutable audit record for subscription cancellations, grace periods, and reactivations.
    Architecture Decision: ADR-0092
    Guarantees zero data loss and preserves historical auditability.
    """

    cancellation_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="cancellations")
    subscription = models.ForeignKey(SubscriptionRecord, on_delete=models.CASCADE, related_name="cancellation_events")
    requested_by = models.CharField(max_length=128)
    reason = models.TextField()
    effective_mode = models.CharField(
        max_length=32,
        choices=CancellationEffectiveMode.choices,
        default=CancellationEffectiveMode.END_OF_BILLING_PERIOD,
    )
    effective_date = models.DateField()
    grace_period_end_date = models.DateField(null=True, blank=True)
    is_reactivated = models.BooleanField(default=False)
    reactivated_at = models.DateTimeField(null=True, blank=True)
    reactivated_by = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Subscription Cancellation Record"
        verbose_name_plural = "Subscription Cancellation Records"
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"Cancellation {self.cancellation_id} for {self.tenant_id} ({self.effective_mode})"


class OfflineLeaseStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    REVOKED = "REVOKED", "Revoked"
    EXPIRED = "EXPIRED", "Expired"
    SUPERSEDED = "SUPERSEDED", "Superseded"


class DeviceLeaseCertificate(models.Model):
    """
    Cryptographically signed, time-bound offline entitlement lease certificate.
    Architecture Decision: ADR-0082, ADR-0092 and ADR-0093
    Requirement Trace: FR-071 (SaaS Plans, Billing and Tenant Lifecycle)

    Binds tenant, branch, device_id, authenticated user, role snapshot, and policy version.
    Signed using HMAC-SHA256 for zero-trust offline POS terminal verification.
    """

    lease_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="offline_leases")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="offline_leases")
    device_id = models.CharField(max_length=128, db_index=True)
    device_name = models.CharField(max_length=255, blank=True, default="")
    user_id = models.CharField(max_length=128, blank=True, default="")
    user_email = models.EmailField(blank=True, default="")
    role_snapshot = models.CharField(max_length=64, default="CASHIER")
    plan_tier = models.CharField(max_length=32, choices=PlanTier.choices, default=PlanTier.BASIC)
    policy_version = models.CharField(max_length=32, default="v2.0")
    duration_hours = models.PositiveIntegerField(default=72)
    issued_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    capabilities_snapshot = models.JSONField(default=dict)
    signature_algorithm = models.CharField(max_length=32, default="HMAC-SHA256")
    signature = models.CharField(max_length=256)
    status = models.CharField(
        max_length=32,
        choices=OfflineLeaseStatus.choices,
        default=OfflineLeaseStatus.ACTIVE,
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revocation_reason = models.TextField(blank=True, default="")
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-issued_at"]
        verbose_name = "Device Lease Certificate"
        verbose_name_plural = "Device Lease Certificates"
        indexes = [
            models.Index(fields=["tenant", "device_id", "-issued_at"]),
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self) -> str:
        return f"Lease {self.lease_id} ({self.device_id} at branch {self.branch_id}) [{self.status}]"


class HoldQueueStatus(models.TextChoices):
    HELD_PENDING_RECONCILIATION = "HELD_PENDING_RECONCILIATION", "Held Pending Reconciliation"
    RECONCILED = "RECONCILED", "Reconciled"
    REJECTED = "REJECTED", "Rejected"


class LapsedSubscriptionHoldQueue(models.Model):
    """
    Queue for holding pending offline transactions pushed when a plan lapses or lease expires.
    Architecture Decision: ADR-0094
    Requirement Trace: FR-072 (Do not silently discard valid pending events if plan lapses)

    Preserves 100% of offline counter sales, prescriptions, and financial amounts for authorized reconciliation.
    Guarantees idempotency via unique(tenant, offline_event_id).
    """

    hold_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="lapsed_hold_events")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="lapsed_hold_events")
    device_id = models.CharField(max_length=128, db_index=True)
    offline_event_id = models.CharField(max_length=128, db_index=True)
    event_type = models.CharField(max_length=64, default="OFFLINE_SALE")
    occurred_at = models.DateTimeField(help_text="When transaction occurred offline at terminal")
    payload_json = models.JSONField(default=dict, help_text="Full transaction payload")
    financial_total_bdt = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    subscription_status_at_intake = models.CharField(max_length=32, default="EXPIRED")
    lease_status_at_intake = models.CharField(max_length=32, default="EXPIRED")
    hold_status = models.CharField(
        max_length=32,
        choices=HoldQueueStatus.choices,
        default=HoldQueueStatus.HELD_PENDING_RECONCILIATION,
    )
    reconciled_at = models.DateTimeField(null=True, blank=True)
    reconciled_by = models.CharField(max_length=128, blank=True, default="")
    reconciliation_target_id = models.CharField(max_length=64, blank=True, default="")
    resolution_notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-occurred_at"]
        verbose_name = "Lapsed Subscription Hold Queue"
        verbose_name_plural = "Lapsed Subscription Hold Queues"
        unique_together = ("tenant", "offline_event_id")
        indexes = [
            models.Index(fields=["tenant", "hold_status", "-occurred_at"]),
            models.Index(fields=["tenant", "device_id"]),
            models.Index(fields=["branch", "hold_status"]),
        ]

    def __str__(self) -> str:
        return f"HoldEvent {self.hold_id}: {self.event_type} ({self.financial_total_bdt} BDT) [{self.hold_status}]"


class QuotaResourceType(models.TextChoices):
    BRANCHES = "BRANCHES", "Active Pharmacy Branches"
    STAFF_SEATS = "STAFF_SEATS", "Staff User Seats"
    DEVICES = "DEVICES", "POS Devices / Terminals"
    PREMIUM_FEATURE = "PREMIUM_FEATURE", "Premium Feature Usage"


class TenantQuotaUsageCounter(models.Model):
    """
    Authoritative usage counter with row-level locking for concurrency race prevention.
    Architecture Decision: ADR-0095
    Requirement Trace: FR-070, FR-071, FR-072 (SaaS Plans, Billing and Tenant Lifecycle)

    Maintains row-locked transactional counters for branches, staff seats, and devices.
    Prevents race-induced over-consumption during simultaneous requests.
    """

    counter_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="quota_counters")
    resource_type = models.CharField(
        max_length=64,
        choices=QuotaResourceType.choices,
        default=QuotaResourceType.BRANCHES,
    )
    current_used = models.PositiveIntegerField(default=0)
    max_limit = models.PositiveIntegerField(default=1)
    reserved = models.PositiveIntegerField(default=0)
    version = models.PositiveIntegerField(default=1)
    last_calculated_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant", "resource_type"]
        verbose_name = "Tenant Quota Usage Counter"
        verbose_name_plural = "Tenant Quota Usage Counters"
        unique_together = ("tenant", "resource_type")
        indexes = [
            models.Index(fields=["tenant", "resource_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id} - {self.resource_type}: {self.current_used}/{self.max_limit} (v{self.version})"


class DeviceSeatStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active Enrolled Workstation"
    REVOKED = "REVOKED", "Revoked / Decommissioned"
    SUSPENDED = "SUSPENDED", "Suspended"
    DECOMMISSIONED = "DECOMMISSIONED", "Permanently Decommissioned"


class DeviceType(models.TextChoices):
    DESKTOP_POS = "DESKTOP_POS", "Desktop POS Workstation"
    TABLET_POS = "TABLET_POS", "Tablet POS Terminal"
    MOBILE_SCANNER = "MOBILE_SCANNER", "Mobile Barcode Scanner"
    COUNTER_TERMINAL = "COUNTER_TERMINAL", "Counter Billing Terminal"


class DeviceSeat(models.Model):
    """
    Enrolled POS Workstation / Terminal seat bound to tenant and branch.
    Architecture Decision: ADR-0096
    Requirement Trace: FR-004, FR-070, FR-071 (SaaS Plans, Device Seats & Billing Lifecycle)

    Represents an authorized device occupying a plan-allocated device seat.
    Guarantees strict zero-data-loss and automatic lease invalidation upon revocation.
    """

    device_id = models.CharField(max_length=128, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="device_seats")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="device_seats")
    device_name = models.CharField(max_length=255)
    device_type = models.CharField(
        max_length=64,
        choices=DeviceType.choices,
        default=DeviceType.DESKTOP_POS,
        db_index=True,
    )
    status = models.CharField(
        max_length=32,
        choices=DeviceSeatStatus.choices,
        default=DeviceSeatStatus.ACTIVE,
        db_index=True,
    )
    mac_address = models.CharField(max_length=64, blank=True, default="")
    ip_address = models.CharField(max_length=64, blank=True, default="")
    hardware_fingerprint = models.CharField(max_length=256, blank=True, default="")
    enrolled_at = models.DateTimeField(default=timezone.now)
    enrolled_by = models.CharField(max_length=128, blank=True, default="")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.CharField(max_length=128, blank=True, default="")
    revocation_reason = models.TextField(blank=True, default="")
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-enrolled_at"]
        verbose_name = "Device Seat"
        verbose_name_plural = "Device Seats"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["tenant", "branch"]),
        ]

    def __str__(self) -> str:
        return f"{self.device_name} [{self.device_id}] ({self.status}) - {self.branch.name}"


class BillingReportType(models.TextChoices):
    FULL_SUMMARY = "FULL_SUMMARY", "Full Platform Summary"
    MRR_CHURN = "MRR_CHURN", "MRR & Churn Report"
    COLLECTIONS = "COLLECTIONS", "Verified Collections Report"
    OUTSTANDING_INVOICES = "OUTSTANDING_INVOICES", "Outstanding Invoices & Aging Report"
    SUBSCRIPTION_AUDIT = "SUBSCRIPTION_AUDIT", "Subscription Audit Trail Report"


class BillingReportPeriod(models.TextChoices):
    CURRENT = "CURRENT", "Current Month / Period"
    MONTHLY = "MONTHLY", "Monthly"
    QUARTERLY = "QUARTERLY", "Quarterly"
    ANNUAL = "ANNUAL", "Annual"
    CUSTOM = "CUSTOM", "Custom Date Range"


class PlatformBillingReportSnapshot(models.Model):
    """
    Platform MRR, Churn, Collections, Outstanding Invoices, and Subscription Audit Snapshot.
    Architecture Decision: ADR-0098
    Requirement Trace: FR-044, FR-064, FR-066, FR-067, FR-074

    Persists audited platform-wide billing aggregates calculated from verified billing records.
    """

    report_id = models.CharField(max_length=64, primary_key=True)
    report_type = models.CharField(
        max_length=32,
        choices=BillingReportType.choices,
        default=BillingReportType.FULL_SUMMARY,
        db_index=True,
    )
    period = models.CharField(
        max_length=16,
        choices=BillingReportPeriod.choices,
        default=BillingReportPeriod.CURRENT,
        db_index=True,
    )
    start_date = models.DateField()
    end_date = models.DateField()
    currency = models.CharField(max_length=8, default="BDT")
    total_mrr_bdt = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    total_arr_bdt = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    arpu_bdt = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    total_collected_bdt = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    total_outstanding_bdt = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    churn_rate_pct = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0.00"))
    active_subscriptions_count = models.PositiveIntegerField(default=0)
    churned_subscriptions_count = models.PositiveIntegerField(default=0)
    outstanding_invoices_count = models.PositiveIntegerField(default=0)
    mrr_breakdown_json = models.JSONField(default=dict, blank=True)
    churn_details_json = models.JSONField(default=dict, blank=True)
    collections_breakdown_json = models.JSONField(default=dict, blank=True)
    aging_brackets_json = models.JSONField(default=dict, blank=True)
    audit_events_sample_json = models.JSONField(default=list, blank=True)
    generated_by_user_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Platform Billing Report Snapshot"
        verbose_name_plural = "Platform Billing Report Snapshots"
        indexes = [
            models.Index(fields=["report_type", "created_at"]),
            models.Index(fields=["period", "start_date", "end_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.report_id} - {self.report_type} ({self.period}: {self.start_date} to {self.end_date})"
