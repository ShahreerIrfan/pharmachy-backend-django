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

import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
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
    RECALLED = "RECALLED", "Recalled"
    DEPLETED = "DEPLETED", "Depleted"
    ARCHIVED = "ARCHIVED", "Archived"


class StorageCondition(models.TextChoices):
    ROOM_TEMPERATURE = "ROOM_TEMPERATURE_15_25C", "Room Temperature (15°C - 25°C)"
    COOL = "COOL_8_15C", "Cool Storage (8°C - 15°C)"
    COLD_CHAIN = "COLD_CHAIN_2_8C", "Cold Chain (2°C - 8°C)"
    FREEZER = "FREEZER_MINUS_20C", "Freezer (-20°C)"
    DO_NOT_FREEZE = "DO_NOT_FREEZE", "Do Not Freeze"
    PROTECT_FROM_LIGHT = "PROTECT_FROM_LIGHT", "Protect From Direct Light"


class InvoiceStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    FINALIZED = "FINALIZED", "Finalized"
    REVERSED = "REVERSED", "Reversed"


class PaymentMethod(models.TextChoices):
    CASH = "CASH", "Cash"
    BKASH = "BKASH", "bKash"
    NAGAD = "NAGAD", "Nagad"
    CARD = "CARD", "Card"
    DUE = "DUE", "Due / Credit"


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


class BranchLifecycleStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active Store"
    SUSPENDED = "SUSPENDED", "Temporarily Suspended"
    CLOSED = "CLOSED", "Permanently Closed"


class Branch(models.Model):
    """Physical pharmacy store or dispensary branch belonging to a tenant (ADR-0005, ADR-0091 & ADR-0107)."""

    branch_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="branches")
    name = models.CharField(max_length=255)
    branch_code = models.CharField(max_length=64, blank=True, default="", db_index=True)
    license_number = models.CharField(max_length=128)  # DGDA Pharmacy License Number
    address = models.TextField(blank=True)
    phone_number = models.CharField(max_length=32, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    manager_name = models.CharField(max_length=128, blank=True, default="")
    is_main_branch = models.BooleanField(default=False)
    status = models.CharField(
        max_length=32,
        choices=BranchLifecycleStatus.choices,
        default=BranchLifecycleStatus.ACTIVE,
        db_index=True,
    )
    is_active = models.BooleanField(default=True, db_index=True)
    is_over_quota_locked = models.BooleanField(default=False, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.CharField(max_length=128, blank=True, default="")
    closure_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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
    brand_name_bn = models.CharField(max_length=255, blank=True, default="", db_index=True, help_text="Bengali brand name transliteration")
    generic_name = models.CharField(max_length=255, db_index=True)
    generic_name_bn = models.CharField(max_length=255, blank=True, default="", db_index=True, help_text="Bengali generic molecule transliteration")
    dosage_form = models.CharField(max_length=64)  # Tablet, Capsule, Syrup, Suspension, Injection
    strength = models.CharField(max_length=64)  # 500mg, 20mg, 100ml
    strength_numeric = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, help_text="Normalized numeric strength value")
    strength_unit = models.CharField(max_length=32, blank=True, default="mg", help_text="Canonical unit: mg, mcg, g, mL, %, IU/mL, mg/5mL")
    strength_unit_family = models.CharField(max_length=32, blank=True, default="MASS", help_text="Taxonomy family: MASS, VOLUME, PERCENTAGE, CONCENTRATION, BIOLOGICAL")
    manufacturer = models.CharField(max_length=255, db_index=True)  # Square, Beximco, Incepta, etc.
    route = models.CharField(max_length=64, blank=True, default="Oral", help_text="Administration route (e.g. Oral, IV, Topical)")
    barcode = models.CharField(max_length=64, blank=True, default="", db_index=True, help_text="GTIN / EAN barcode")
    source_metadata = models.JSONField(default=dict, blank=True, help_text="Regulatory source metadata, DAR approval date, gazette info")
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
            f"{self.dosage_form}|{self.strength}|{self.manufacturer}|"
            f"{self.route}|{self.barcode}|{self.version_number}"
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


class ClassificationType(models.TextChoices):
    OTC = "OTC", "Over-The-Counter"
    PRESCRIPTION_ONLY = "PRESCRIPTION_ONLY", "Prescription-Only Medicine (POM)"
    NARCOTIC_SCHEDULED = "NARCOTIC_SCHEDULED", "Narcotic / Controlled Substance (Schedule-G)"
    ANTIMICROBIAL_ACCESS = "ANTIMICROBIAL_ACCESS", "Antimicrobial AWaRe - Access"
    ANTIMICROBIAL_WATCH = "ANTIMICROBIAL_WATCH", "Antimicrobial AWaRe - Watch"
    ANTIMICROBIAL_RESERVE = "ANTIMICROBIAL_RESERVE", "Antimicrobial AWaRe - Reserve"
    RECALLED = "RECALLED", "DGDA Statutory Safety Recall"
    BANNED = "BANNED", "Banned & Prohibited"


class MedicineClassificationSnapshot(models.Model):
    """
    Architecture Decision: ADR-0130 (P130).
    Versioned regulatory snapshot of a medicine's controlled classification,
    effective date range, approving authority, gazette source, and cryptographic hash.
    Enforces point-in-time dispensing compliance and recall hard-blocking.
    """

    snapshot_id = models.CharField(max_length=64, primary_key=True)
    product = models.ForeignKey(
        "core.MedicineProduct",
        on_delete=models.CASCADE,
        related_name="classification_snapshots",
        db_index=True,
    )
    version_number = models.PositiveIntegerField(default=1)
    classification_type = models.CharField(
        max_length=32,
        choices=ClassificationType.choices,
        default=ClassificationType.OTC,
        db_index=True,
    )
    regulatory_source = models.CharField(
        max_length=255,
        help_text="Official DGDA gazette, circular, or WHO AWaRe publication reference.",
    )
    effective_from = models.DateTimeField(db_index=True)
    effective_to = models.DateTimeField(null=True, blank=True, db_index=True)
    is_current = models.BooleanField(default=True, db_index=True)
    reviewed_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_classification_snapshots",
    )
    reviewed_at = models.DateTimeField(auto_now_add=True)
    review_notes = models.TextField(blank=True, default="")
    immutable_hash = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["product", "-version_number"]
        verbose_name = "Medicine Classification Snapshot"
        verbose_name_plural = "Medicine Classification Snapshots"
        indexes = [
            models.Index(fields=["product", "is_current"]),
            models.Index(fields=["product", "version_number"]),
            models.Index(fields=["classification_type", "is_current"]),
            models.Index(fields=["effective_from", "effective_to"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "version_number"],
                name="unique_product_classification_version",
            )
        ]

    def compute_hash(self) -> str:
        import hashlib

        eff_str = self.effective_from.isoformat() if self.effective_from else ""
        raw = f"{self.product_id}|{self.version_number}|{self.classification_type}|{self.regulatory_source}|{eff_str}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        if not self.immutable_hash:
            self.immutable_hash = self.compute_hash()
        super().save(*args, **kwargs)

    @property
    def is_dispensing_blocked(self) -> bool:
        return self.classification_type in (ClassificationType.RECALLED, ClassificationType.BANNED)

    def __str__(self) -> str:
        curr = " [CURRENT]" if self.is_current else ""
        return f"{self.product_id} v{self.version_number}: {self.classification_type}{curr}"


class ModerationStatus(models.TextChoices):
    APPROVED = "APPROVED", "Approved"
    PENDING_REVIEW = "PENDING_REVIEW", "Pending Review"
    REJECTED = "REJECTED", "Rejected"


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
    variant = models.ForeignKey(
        "core.MedicineVariant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tenant_configs",
        help_text="Optional commercial variant SKU linked to this tenant override.",
    )

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
    moderation_status = models.CharField(
        max_length=32,
        choices=ModerationStatus.choices,
        default=ModerationStatus.APPROVED,
        db_index=True,
        help_text="Platform moderation status for anti-gouging and regulatory verification.",
    )
    moderation_reason = models.TextField(
        blank=True,
        default="",
        help_text="Moderation rationale, compliance notes, or rejection reason.",
    )
    moderated_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="moderated_medicine_configs",
        help_text="Platform superadmin or compliance officer who moderated this override.",
    )
    moderated_at = models.DateTimeField(null=True, blank=True)
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
            models.Index(fields=["tenant", "moderation_status"]),
        ]

    def clean(self):
        super().clean()

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
        if self.local_brand_name and self.local_brand_name.strip() and self.moderation_status == ModerationStatus.APPROVED:
            return self.local_brand_name.strip()
        return self.product.brand_name if self.product else ""

    @property
    def effective_mrp_unit(self) -> Decimal:
        if self.custom_mrp_unit is not None and self.moderation_status == ModerationStatus.APPROVED:
            return self.custom_mrp_unit
        return self.product.mrp_unit if self.product else Decimal("0.00")

    @property
    def effective_cost_unit(self) -> Decimal:
        if self.custom_cost_unit is not None and self.moderation_status == ModerationStatus.APPROVED:
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
    reserved_quantity_base_units = models.PositiveIntegerField(
        default=0,
        help_text="Quantity currently reserved for active checkouts or pending carts",
    )
    expiry_date = models.DateField(db_index=True)
    purchase_date = models.DateField()
    cost_per_unit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    status = models.CharField(max_length=32, choices=BatchStatus.choices, default=BatchStatus.ACTIVE)
    manufacturing_date = models.DateField(null=True, blank=True, db_index=True)
    lot_reference = models.CharField(max_length=64, blank=True, default="", db_index=True)
    storage_condition = models.CharField(
        max_length=64,
        choices=StorageCondition.choices,
        default=StorageCondition.ROOM_TEMPERATURE,
    )
    quarantine_lead_days = models.PositiveIntegerField(default=30)
    is_quarantined = models.BooleanField(default=False)
    reconstitution_shelf_life_days = models.PositiveIntegerField(null=True, blank=True)
    recall_notice = models.ForeignKey(
        "CatalogueLifecycleNotice",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="linked_batches",
    )
    recall_reason = models.CharField(max_length=255, blank=True, default="")
    manufacturer_name = models.CharField(max_length=128, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ["expiry_date", "batch_number"]
        unique_together = ("tenant", "branch", "product", "batch_number")
        indexes = [
            models.Index(fields=["tenant", "branch", "expiry_date"]),
            models.Index(fields=["tenant", "batch_number"]),
            models.Index(fields=["status", "is_active"]),
        ]
        verbose_name = "Batch Stock Lot"
        verbose_name_plural = "Batch Stock Lots"

    def __str__(self) -> str:
        return f"Batch {self.batch_number} - {self.product.brand_name} (Qty: {self.quantity_base_units}, Exp: {self.expiry_date})"

    @property
    def available_quantity_base_units(self) -> int:
        return max(0, self.quantity_base_units - self.reserved_quantity_base_units)

    @property
    def is_expired(self) -> bool:
        from django.utils import timezone

        return self.expiry_date <= timezone.now().date()

    @property
    def is_recalled(self) -> bool:
        return self.status == BatchStatus.RECALLED or self.recall_notice_id is not None

    @property
    def is_dispensable(self) -> bool:
        if self.status != BatchStatus.ACTIVE or not self.is_active:
            return False
        if self.is_expired or self.is_quarantined or self.recall_notice_id is not None:
            return False
        return self.available_quantity_base_units > 0

    def clean(self):
        super().clean()
        if self.branch_id and self.tenant_id:
            if hasattr(self, 'branch') and self.branch and self.branch.tenant_id != self.tenant_id:
                raise ValidationError({'branch': 'Branch must belong to the same tenant as the batch.'})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class RegulatedStockEntry(models.Model):
    """
    Architecture Decision: ADR-0154 (P154).
    Regulated stock intake record capturing manufacturing date,
    expiration date, lot reference, and storage policy per DGDA rules.
    """

    entry_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="regulated_stock_entries")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="regulated_stock_entries")
    product = models.ForeignKey(MedicineProduct, on_delete=models.CASCADE, related_name="regulated_stock_entries")
    batch = models.ForeignKey(BatchStock, on_delete=models.SET_NULL, null=True, blank=True, related_name="regulated_entries")
    lot_reference = models.CharField(max_length=64, db_index=True)
    manufacturing_date = models.DateField(db_index=True)
    expiry_date = models.DateField(db_index=True)
    quantity_received = models.PositiveIntegerField(default=1)
    unit_cost_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0001"))],
    )
    storage_condition = models.CharField(
        max_length=64,
        choices=StorageCondition.choices,
        default=StorageCondition.ROOM_TEMPERATURE,
    )
    quarantine_lead_days = models.PositiveIntegerField(default=30)
    is_quarantined = models.BooleanField(default=False)
    reconstitution_shelf_life_days = models.PositiveIntegerField(null=True, blank=True)
    supplier_reference = models.CharField(max_length=128, blank=True, default="")
    entry_notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "branch", "expiry_date"]),
            models.Index(fields=["product", "lot_reference"]),
            models.Index(fields=["tenant", "is_quarantined"]),
        ]
        verbose_name = "Regulated Stock Entry"
        verbose_name_plural = "Regulated Stock Entries"

    def __str__(self) -> str:
        return f"StockEntry {self.entry_id} - Lot: {self.lot_reference} ({self.product.brand_name})"

    @property
    def shelf_life_days(self) -> int:
        if self.expiry_date and self.manufacturing_date:
            return (self.expiry_date - self.manufacturing_date).days
        return 0

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


class StaffLifecycleStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active Staff"
    DEACTIVATED = "DEACTIVATED", "Deactivated / Suspended"
    TERMINATED = "TERMINATED", "Terminated / Separated"


class StaffMember(models.Model):
    """
    Authorized pharmacy staff profile associated with a tenant, optional branch, and role.
    Architecture Decision: ADR-0005, ADR-0033 & ADR-0106
    """

    staff_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="staff_members")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, null=True, blank=True, related_name="staff_members")
    username = models.CharField(max_length=128)
    full_name = models.CharField(max_length=255)
    role = models.CharField(max_length=32, choices=StaffRole.choices, default=StaffRole.CASHIER, db_index=True)
    mobile_number = models.CharField(max_length=32)
    pcb_registration_no = models.CharField(max_length=64, blank=True, default="")
    lifecycle_status = models.CharField(
        max_length=32,
        choices=StaffLifecycleStatus.choices,
        default=StaffLifecycleStatus.ACTIVE,
        db_index=True,
    )
    is_active = models.BooleanField(default=True, db_index=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivated_by = models.CharField(max_length=128, blank=True, default="")
    deactivation_reason = models.TextField(blank=True, default="")
    terminated_at = models.DateTimeField(null=True, blank=True)
    terminated_by = models.CharField(max_length=128, blank=True, default="")
    termination_reason = models.TextField(blank=True, default="")
    last_credential_reset_at = models.DateTimeField(null=True, blank=True)
    last_mfa_reset_at = models.DateTimeField(null=True, blank=True)
    last_session_revocation_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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


class DunningStage(models.TextChoices):
    UPCOMING_RENEWAL = "UPCOMING_RENEWAL", "Upcoming Renewal Notice"
    PAYMENT_FAILED = "PAYMENT_FAILED", "Payment Failure Alert"
    GRACE_PERIOD_ACTIVE = "GRACE_PERIOD_ACTIVE", "Grace Period Warning"
    SUBSCRIPTION_EXPIRED = "SUBSCRIPTION_EXPIRED", "Subscription Expired"
    ACCOUNT_SUSPENDED = "ACCOUNT_SUSPENDED", "Account Suspended"
    PAYMENT_RECOVERED = "PAYMENT_RECOVERED", "Payment Recovered & Restored"


class DunningChannel(models.TextChoices):
    IN_APP = "IN_APP", "In-App Banner / Notification"
    SMS = "SMS", "SMS Message"
    EMAIL = "EMAIL", "Email Notification"


class DunningDeliveryStatus(models.TextChoices):
    PENDING = "PENDING", "Pending Delivery"
    DELIVERED = "DELIVERED", "Delivered / Sent"
    FAILED = "FAILED", "Delivery Failed"
    SUPPRESSED = "SUPPRESSED", "Suppressed (De-duplicated)"


class DunningNotification(models.Model):
    """
    Subscription Dunning Notification, De-duplication, and Recovery Record.
    Architecture Decision: ADR-0099
    Requirement Trace: FR-044, FR-066, FR-068, FR-070

    Tracks localized communication regarding renewals, payment failures,
    grace periods, and payment recoveries with deterministic de-duplication.
    """

    notification_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="dunning_notifications")
    subscription = models.ForeignKey(
        SubscriptionRecord,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dunning_notifications",
    )
    invoice = models.ForeignKey(
        SubscriptionRenewalInvoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dunning_notifications",
    )
    stage = models.CharField(
        max_length=32,
        choices=DunningStage.choices,
        default=DunningStage.UPCOMING_RENEWAL,
        db_index=True,
    )
    channel = models.CharField(
        max_length=16,
        choices=DunningChannel.choices,
        default=DunningChannel.IN_APP,
    )
    delivery_status = models.CharField(
        max_length=16,
        choices=DunningDeliveryStatus.choices,
        default=DunningDeliveryStatus.DELIVERED,
        db_index=True,
    )
    dedup_key = models.CharField(max_length=128, db_index=True)
    title_en = models.CharField(max_length=255)
    title_bn = models.CharField(max_length=255)
    body_en = models.TextField()
    body_bn = models.TextField()
    amount_due_bdt = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    recipient_email = models.EmailField(max_length=255, blank=True, default="")
    recipient_phone = models.CharField(max_length=64, blank=True, default="")
    is_acknowledged = models.BooleanField(default=False, db_index=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by_user_id = models.CharField(max_length=64, blank=True, default="")
    retry_count = models.PositiveIntegerField(default=0)
    max_retries = models.PositiveIntegerField(default=3)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    last_error_message = models.TextField(blank=True, default="")
    metadata_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Dunning Notification"
        verbose_name_plural = "Dunning Notifications"
        indexes = [
            models.Index(fields=["tenant", "is_acknowledged"]),
            models.Index(fields=["tenant", "stage"]),
            models.Index(fields=["dedup_key", "delivery_status"]),
        ]

    def __str__(self) -> str:
        return f"{self.notification_id} [{self.stage}] - {self.tenant_id} ({self.delivery_status})"


class SubscriptionOverrideType(models.TextChoices):
    GRACE_EXTENSION = "GRACE_EXTENSION", "Grace Extension"
    EMERGENCY_UNBLOCK = "EMERGENCY_UNBLOCK", "Emergency Unblock"
    QUOTA_OVERRIDE = "QUOTA_OVERRIDE", "Quota Override"
    REACTIVATION = "REACTIVATION", "Reactivation"


class SubscriptionOverrideStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    EXPIRED = "EXPIRED", "Expired"
    REVOKED = "REVOKED", "Revoked"


class SubscriptionSupportOverride(models.Model):
    """
    Subscription Support Override Model (P100 / ADR-0100).
    Requirement Trace: FR-044, FR-066, FR-070

    Audited platform operational exceptions granted by platform administrators:
    - Time-bound grace extensions during banking/clearance delays.
    - Emergency temporary unblocks for critical healthcare dispensing.
    - Temporary workstation / branch quota overrides.
    - Supervised emergency reactivations.
    """

    override_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="subscription_overrides")
    subscription = models.ForeignKey(
        SubscriptionRecord,
        on_delete=models.CASCADE,
        related_name="subscription_overrides",
    )
    override_type = models.CharField(
        max_length=32,
        choices=SubscriptionOverrideType.choices,
        default=SubscriptionOverrideType.GRACE_EXTENSION,
        db_index=True,
    )
    status = models.CharField(
        max_length=16,
        choices=SubscriptionOverrideStatus.choices,
        default=SubscriptionOverrideStatus.ACTIVE,
        db_index=True,
    )
    extension_days = models.PositiveIntegerField(null=True, blank=True, default=7)
    quota_bonus = models.PositiveIntegerField(null=True, blank=True, default=0)
    justification = models.TextField()
    ticket_reference = models.CharField(max_length=64, db_index=True)
    granted_by_user_id = models.CharField(max_length=64)
    granted_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by_user_id = models.CharField(max_length=64, blank=True, default="")
    revocation_reason = models.TextField(blank=True, default="")
    metadata_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-granted_at"]
        verbose_name = "Subscription Support Override"
        verbose_name_plural = "Subscription Support Overrides"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "override_type", "status"]),
            models.Index(fields=["expires_at", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.override_id} [{self.override_type}] - {self.tenant_id} ({self.status})"



class DueStatus(models.TextChoices):
    OUTSTANDING = "OUTSTANDING", "Outstanding"
    PARTIAL = "PARTIAL", "Partially Paid"
    SETTLED = "SETTLED", "Settled"
    WRITTEN_OFF = "WRITTEN_OFF", "Written Off"


class CustomerDueRecord(models.Model):
    """
    Tracks retail credit sales, outstanding balances, and customer repayments (P101 / ADR-0101).
    Requirement Trace: FR-031, FR-032, FR-033
    Strict Decimal precision (zero float).
    """

    due_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="customer_dues")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="customer_dues")
    invoice = models.ForeignKey(
        SaleInvoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="customer_dues",
    )
    customer_name = models.CharField(max_length=255)
    customer_phone = models.CharField(max_length=32, db_index=True)
    total_due_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    paid_amount_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    remaining_due_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    status = models.CharField(
        max_length=32,
        choices=DueStatus.choices,
        default=DueStatus.OUTSTANDING,
        db_index=True,
    )
    due_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Customer Due Record"
        verbose_name_plural = "Customer Due Records"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "customer_phone"]),
            models.Index(fields=["branch", "status"]),
        ]

    def __str__(self) -> str:
        return f"Due {self.due_id} - {self.customer_name} ({self.remaining_due_bdt} BDT) [{self.status}]"


class InvitationStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    ACCEPTED = "ACCEPTED", "Accepted"
    REVOKED = "REVOKED", "Revoked"
    EXPIRED = "EXPIRED", "Expired"


class StaffInvitation(models.Model):
    """
    Audited owner or branch manager invitation for prospective pharmacy personnel (P102 / ADR-0102).
    Supports branch scopes, custom role titles, secure tokens, expiration, resend and revocation.
    """

    invitation_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="staff_invitations")
    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staff_invitations",
    )
    email = models.EmailField(db_index=True)
    phone_number = models.CharField(max_length=32, blank=True, default="")
    full_name = models.CharField(max_length=255)
    role = models.CharField(
        max_length=32,
        choices=StaffRole.choices,
        default=StaffRole.CASHIER,
        db_index=True,
    )
    custom_role_title = models.CharField(max_length=128, blank=True, default="")
    pcb_registration_no = models.CharField(max_length=64, blank=True, default="")
    invite_token = models.CharField(max_length=128, unique=True, db_index=True)
    status = models.CharField(
        max_length=32,
        choices=InvitationStatus.choices,
        default=InvitationStatus.PENDING,
        db_index=True,
    )
    invited_by_username = models.CharField(max_length=128, blank=True, default="")
    expires_at = models.DateTimeField(db_index=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by_username = models.CharField(max_length=128, blank=True, default="")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by_username = models.CharField(max_length=128, blank=True, default="")
    revocation_reason = models.TextField(blank=True, default="")
    resend_count = models.PositiveIntegerField(default=0)
    last_resent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Staff Invitation"
        verbose_name_plural = "Staff Invitations"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "email"]),
            models.Index(fields=["branch", "status"]),
        ]

    def __str__(self) -> str:
        return f"Invitation {self.invitation_id} -> {self.email} ({self.role}) [{self.status}]"


class StaffMembership(models.Model):
    """
    Authoritative active or archived staff membership record within a tenant (P102 / ADR-0102).
    Links StaffMember to primary and secondary branch scopes, custom role titles, and provenance invitation.
    """

    membership_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="memberships")
    staff_member = models.OneToOneField(
        StaffMember,
        on_delete=models.CASCADE,
        related_name="membership",
    )
    primary_branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="primary_memberships",
    )
    assigned_branches = models.ManyToManyField(
        Branch,
        blank=True,
        related_name="scoped_memberships",
    )
    role = models.CharField(
        max_length=32,
        choices=StaffRole.choices,
        default=StaffRole.CASHIER,
        db_index=True,
    )
    custom_role_title = models.CharField(max_length=128, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    invitation = models.ForeignKey(
        StaffInvitation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_memberships",
    )
    joined_at = models.DateTimeField(auto_now_add=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivation_reason = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-joined_at"]
        verbose_name = "Staff Membership"
        verbose_name_plural = "Staff Memberships"
        indexes = [
            models.Index(fields=["tenant", "is_active"]),
            models.Index(fields=["primary_branch", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"Membership {self.membership_id} - {self.staff_member.full_name} ({self.role})"



class CustomRoleDefinition(models.Model):
    """
    Tenant-scoped custom role definition specifying granted atomic permission keys
    and safety minimums (P103 / ADR-0103).
    """

    role_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="custom_roles")
    role_code = models.CharField(max_length=64, db_index=True)
    display_name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    base_role = models.CharField(
        max_length=32,
        choices=StaffRole.choices,
        default=StaffRole.CASHIER,
        db_index=True,
    )
    permission_keys = models.JSONField(default=list)
    requires_pcb_registration = models.BooleanField(default=False)
    is_system_protected = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False, db_index=True)
    cloned_from_id = models.CharField(max_length=64, blank=True, default="")
    created_by_username = models.CharField(max_length=128, blank=True, default="")
    grant_version = models.PositiveIntegerField(default=1, help_text="Version counter for role grants and entitlements")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name"]
        verbose_name = "Custom Role Definition"
        verbose_name_plural = "Custom Role Definitions"
        unique_together = ("tenant", "role_code")
        indexes = [
            models.Index(fields=["tenant", "is_archived"]),
            models.Index(fields=["tenant", "role_code"]),
        ]

    def __str__(self) -> str:
        return f"Role {self.role_code} ({self.display_name}) - {self.tenant_id}"



class ExpenseStatus(models.TextChoices):
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending Approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class BranchExpenseRecord(models.Model):
    """
    Branch operational expense or petty cash voucher requiring granular approval (P104 / ADR-0104).
    Enforces expense_approval authority.
    """

    expense_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="branch_expenses")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="branch_expenses")
    category = models.CharField(max_length=64, default="MISCELLANEOUS")
    amount_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    description = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=32,
        choices=ExpenseStatus.choices,
        default=ExpenseStatus.PENDING_APPROVAL,
        db_index=True,
    )
    created_by_username = models.CharField(max_length=128, blank=True, default="")
    approved_by_username = models.CharField(max_length=128, blank=True, default="")
    rejection_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Branch Expense Record"
        verbose_name_plural = "Branch Expense Records"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["branch", "status"]),
        ]

    def __str__(self) -> str:
        return f"Expense {self.expense_id} - {self.category} ({self.amount_bdt} BDT) [{self.status}]"


class StockAdjustmentType(models.TextChoices):
    DAMAGE = "DAMAGE", "Damage / Breakage"
    EXPIRY_REMOVAL = "EXPIRY_REMOVAL", "Expired Stock Removal"
    COUNT_CORRECTION = "COUNT_CORRECTION", "Physical Count Correction"
    INTERNAL_USE = "INTERNAL_USE", "Internal Clinic / Store Use"
    THEFT_LOSS = "THEFT_LOSS", "Theft or Unaccounted Loss"
    FOUND_SURPLUS = "FOUND_SURPLUS", "Found Physical Surplus"


class StockCorrectionStatus(models.TextChoices):
    PENDING = "PENDING", "Pending Manager Approval"
    APPROVED = "APPROVED", "Approved & Posted"
    REJECTED = "REJECTED", "Rejected by Manager"
    REVERSED = "REVERSED", "Reversed / Offset"


class StockAdjustmentRecord(models.Model):
    """
    Audited inventory adjustment record modifying physical stock counts (P104 / P174 / ADR-0174).
    Enforces stock_change authority, two-person approval workflows, and reversible ledger entries.
    """

    adjustment_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="stock_adjustments")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="stock_adjustments")
    medicine = models.ForeignKey(MedicineProduct, on_delete=models.CASCADE, related_name="stock_adjustments")
    batch = models.ForeignKey(BatchStock, on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_adjustments")
    cost_lot = models.ForeignKey(
        "PurchaseCostLot",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_adjustments",
    )
    adjustment_type = models.CharField(
        max_length=32,
        choices=StockAdjustmentType.choices,
        default=StockAdjustmentType.DAMAGE,
        db_index=True,
    )
    quantity_delta = models.IntegerField(help_text="Positive for additions, negative for reductions.")
    unit_cost_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    total_cost_impact = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        help_text="Total GAAP valuation impact: abs(quantity_delta) * unit_cost_bdt",
    )
    reason = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=32,
        choices=StockCorrectionStatus.choices,
        default=StockCorrectionStatus.APPROVED,
        db_index=True,
    )
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="proposed_stock_adjustments",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_stock_adjustments",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    reversal_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reversals",
    )
    is_reversed = models.BooleanField(default=False, db_index=True)
    reversal_reason = models.TextField(blank=True, default="")
    supporting_document_ref = models.CharField(max_length=128, blank=True, default="")
    adjusted_by_username = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Stock Adjustment Record"
        verbose_name_plural = "Stock Adjustment Records"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["batch", "created_at"]),
            models.Index(fields=["tenant", "is_reversed"]),
        ]

    def __str__(self) -> str:
        return f"StockAdj {self.adjustment_id} - {self.medicine.brand_name} ({self.quantity_delta}) [{self.adjustment_type}] - {self.status}"

    def clean(self):
        super().clean()
        if not self.adjustment_id:
            self.adjustment_id = f"stkadj-{uuid.uuid4().hex[:12]}"
        elif not (str(self.adjustment_id).startswith("stkadj-") or str(self.adjustment_id).startswith("adj-")):
            self.adjustment_id = f"stkadj-{self.adjustment_id}"

        if self.quantity_delta == 0:
            raise ValidationError({"quantity_delta": "Quantity delta cannot be zero."})

        cost = self.unit_cost_bdt or Decimal("0.00")
        self.total_cost_impact = abs(self.quantity_delta) * cost

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class BranchStaffMembership(models.Model):
    """
    Authoritative branch-specific membership allowing a staff account to hold
    different roles in separate authorized branches (P105 / ADR-0105).
    Example: Staff member Kamal is BRANCH_MANAGER at Branch A, but CASHIER at Branch B.
    """

    membership_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="branch_staff_memberships")
    staff_member = models.ForeignKey(StaffMember, on_delete=models.CASCADE, related_name="branch_memberships")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="staff_memberships")
    role = models.CharField(
        max_length=64,
        default=StaffRole.CASHIER,
        db_index=True,
    )
    custom_role_code = models.CharField(max_length=64, blank=True, default="", db_index=True)
    custom_role_title = models.CharField(max_length=128, blank=True, default="")
    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True, db_index=True)
    assigned_at = models.DateTimeField(auto_now_add=True)
    assigned_by_username = models.CharField(max_length=128, blank=True, default="")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by_username = models.CharField(max_length=128, blank=True, default="")
    revocation_reason = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-assigned_at"]
        verbose_name = "Branch Staff Membership"
        verbose_name_plural = "Branch Staff Memberships"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "staff_member", "branch"],
                name="unique_tenant_staff_branch_membership",
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "branch", "is_active"]),
            models.Index(fields=["staff_member", "is_active"]),
            models.Index(fields=["tenant", "staff_member", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"BranchMembership {self.membership_id} - {self.staff_member.full_name} ({self.role}) @ {self.branch.name}"


class StaffLifecycleEventType(models.TextChoices):
    CREATED = "CREATED", "Staff Created"
    UPDATED = "UPDATED", "Staff Details Updated"
    DEACTIVATED = "DEACTIVATED", "Staff Deactivated"
    REACTIVATED = "REACTIVATED", "Staff Reactivated"
    TERMINATED = "TERMINATED", "Staff Terminated"
    CREDENTIALS_RESET = "CREDENTIALS_RESET", "Credentials Reset"
    MFA_RESET = "MFA_RESET", "MFA Reset / Recovered"
    SESSIONS_REVOKED = "SESSIONS_REVOKED", "Sessions Revoked"
    DEVICE_REVOKED = "DEVICE_REVOKED", "Device Credentials Revoked"


class StaffLifecycleEvent(models.Model):
    """
    Immutable audit trail recording state transitions and credential revocations across a staff member's lifecycle (ADR-0106 / P106).
    """

    event_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="staff_lifecycle_events")
    staff_member = models.ForeignKey(StaffMember, on_delete=models.CASCADE, related_name="lifecycle_events")
    event_type = models.CharField(max_length=32, choices=StaffLifecycleEventType.choices, db_index=True)
    performed_by_username = models.CharField(max_length=128, blank=True, default="")
    reason = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict)
    ip_address = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Staff Lifecycle Event"
        verbose_name_plural = "Staff Lifecycle Events"
        indexes = [
            models.Index(fields=["tenant", "event_type"]),
            models.Index(fields=["staff_member", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"Event {self.event_id} [{self.event_type}] for {self.staff_member.username}"


class BranchLifecycleEventType(models.TextChoices):
    CREATED = "CREATED", "Branch Created"
    UPDATED = "UPDATED", "Branch Details Updated"
    SUSPENDED = "SUSPENDED", "Branch Suspended"
    REACTIVATED = "REACTIVATED", "Branch Reactivated"
    CLOSED = "CLOSED", "Branch Closed / Decommissioned"
    QUOTA_LOCKED = "QUOTA_LOCKED", "Branch Over-Quota Locked"
    QUOTA_UNLOCKED = "QUOTA_UNLOCKED", "Branch Quota Unlocked"


class BranchLifecycleEvent(models.Model):
    """
    Immutable audit trail recording state transitions, closures, and quota events across a branch's lifecycle (ADR-0107 / P107).
    """

    event_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="branch_lifecycle_events")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="lifecycle_events")
    event_type = models.CharField(max_length=32, choices=BranchLifecycleEventType.choices, db_index=True)
    performed_by_username = models.CharField(max_length=128, blank=True, default="")
    reason = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict)
    ip_address = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Branch Lifecycle Event"
        verbose_name_plural = "Branch Lifecycle Events"
        indexes = [
            models.Index(fields=["tenant", "event_type"]),
            models.Index(fields=["branch", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"BranchEvent {self.event_id} [{self.event_type}] for {self.branch.name}"



class ApprovalRequestType(models.TextChoices):
    HIGH_DISCOUNT = "HIGH_DISCOUNT", "High Retail Discount"
    REFUND = "REFUND", "Sale Return / Refund"
    STOCK_WRITE_OFF = "STOCK_WRITE_OFF", "Stock Damage / Write-off"
    SENSITIVE_EXPENSE = "SENSITIVE_EXPENSE", "Sensitive Operating Expense"
    CLINICAL_ESCALATION = "CLINICAL_ESCALATION", "Clinical Escalation / Drug Safety"


class ApprovalStatus(models.TextChoices):
    PENDING = "PENDING", "Pending Review"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled by Maker"


class ApprovalUrgency(models.TextChoices):
    ROUTINE = "ROUTINE", "Routine"
    URGENT = "URGENT", "Urgent"
    CRITICAL = "CRITICAL", "Critical / High Priority"


class ApprovalRequest(models.Model):
    """
    Maker-Checker Dual-Control Approval Request (ADR-0108 / P108).
    Enforces that maker != checker across high discounts, refunds, stock write-offs,
    sensitive operational expenses, and clinical safety escalations.
    """

    request_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="approval_requests")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="approval_requests")
    approval_type = models.CharField(max_length=32, choices=ApprovalRequestType.choices, db_index=True)
    status = models.CharField(
        max_length=32,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.PENDING,
        db_index=True,
    )
    urgency = models.CharField(
        max_length=32,
        choices=ApprovalUrgency.choices,
        default=ApprovalUrgency.ROUTINE,
        db_index=True,
    )
    maker_user = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.CASCADE,
        related_name="initiated_approvals",
    )
    checker_user = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decided_approvals",
    )
    amount_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
    )
    title = models.CharField(max_length=255)
    reason = models.TextField()
    decision_notes = models.TextField(blank=True, default="")
    approval_token = models.CharField(max_length=128, blank=True, default="", db_index=True)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    decision_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Approval Request"
        verbose_name_plural = "Approval Requests"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["tenant", "approval_type"]),
            models.Index(fields=["maker_user", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"ApprovalRequest {self.request_id} [{self.approval_type}] - {self.status}"


class ApprovalAuditEvent(models.Model):
    """
    Immutable audit log recording every transition (SUBMITTED, APPROVED, REJECTED, CANCELLED)
    for regulatory DGDA and financial compliance (ADR-0108).
    """

    event_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="approval_audit_events")
    approval_request = models.ForeignKey(ApprovalRequest, on_delete=models.CASCADE, related_name="audit_trail")
    action = models.CharField(max_length=32, db_index=True)
    actor_username = models.CharField(max_length=128, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict)
    ip_address = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Approval Audit Event"
        verbose_name_plural = "Approval Audit Events"
        indexes = [
            models.Index(fields=["tenant", "action"]),
            models.Index(fields=["approval_request", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"ApprovalAudit {self.event_id} [{self.action}] on {self.approval_request_id}"



class ShiftType(models.TextChoices):
    MORNING = "MORNING", "Morning Shift (08:00 - 16:00)"
    EVENING = "EVENING", "Evening Shift (16:00 - 00:00)"
    NIGHT = "NIGHT", "Night Emergency Shift (00:00 - 08:00)"
    FULL_DAY = "FULL_DAY", "Full Day Shift"
    CUSTOM = "CUSTOM", "Custom Working Hours"


class ShiftScheduleStatus(models.TextChoices):
    SCHEDULED = "SCHEDULED", "Scheduled"
    CONFIRMED = "CONFIRMED", "Confirmed by Staff"
    IN_PROGRESS = "IN_PROGRESS", "Shift In Progress"
    COMPLETED = "COMPLETED", "Shift Completed"
    CANCELLED = "CANCELLED", "Shift Cancelled"
    NO_SHOW = "NO_SHOW", "No Show / Absent"


class AttendanceStatus(models.TextChoices):
    PENDING = "PENDING", "Pending Clock-In"
    ON_TIME = "ON_TIME", "Clocked In On-Time"
    LATE = "LATE", "Clocked In Late"
    EARLY_DEPARTURE = "EARLY_DEPARTURE", "Departed Early"
    ABSENT = "ABSENT", "Shift Absent"


class StaffShiftSchedule(models.Model):
    """
    Staff Shift Schedule & Cashier Roster (ADR-0109 / P109).
    Maintains scheduled working hours, attendance boundaries, grace periods,
    and branch-local display time (Asia/Dhaka). Enforces zero shift collision / overlap.
    """

    schedule_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="shift_schedules")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="shift_schedules")
    staff_member = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.CASCADE,
        related_name="assigned_shifts",
    )
    shift_name = models.CharField(max_length=128)
    shift_type = models.CharField(
        max_length=32,
        choices=ShiftType.choices,
        default=ShiftType.MORNING,
        db_index=True,
    )
    start_time = models.DateTimeField(db_index=True)
    end_time = models.DateTimeField(db_index=True)
    timezone_name = models.CharField(max_length=64, default="Asia/Dhaka")
    status = models.CharField(
        max_length=32,
        choices=ShiftScheduleStatus.choices,
        default=ShiftScheduleStatus.SCHEDULED,
        db_index=True,
    )
    grace_period_minutes = models.PositiveIntegerField(default=15)
    clock_in_at = models.DateTimeField(null=True, blank=True)
    clock_out_at = models.DateTimeField(null=True, blank=True)
    attendance_status = models.CharField(
        max_length=32,
        choices=AttendanceStatus.choices,
        default=AttendanceStatus.PENDING,
        db_index=True,
    )
    notes = models.TextField(blank=True, default="")
    created_by_username = models.CharField(max_length=128, blank=True, default="")
    updated_by_username = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_time"]
        verbose_name = "Staff Shift Schedule"
        verbose_name_plural = "Staff Shift Schedules"
        indexes = [
            models.Index(fields=["tenant", "branch", "start_time"]),
            models.Index(fields=["staff_member", "start_time"]),
            models.Index(fields=["tenant", "status"]),
        ]

    def __str__(self) -> str:
        return f"Shift {self.schedule_id} [{self.shift_name}] - {self.staff_member_id} ({self.status})"


class ShiftAuditEvent(models.Model):
    """
    Immutable audit trail for shift scheduling, roster changes, attendance clock-ins/outs,
    and cancellations (ADR-0109).
    """

    event_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="shift_audit_events")
    shift_schedule = models.ForeignKey(StaffShiftSchedule, on_delete=models.CASCADE, related_name="audit_trail")
    action = models.CharField(max_length=32, db_index=True)
    actor_username = models.CharField(max_length=128, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict)
    ip_address = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Shift Audit Event"
        verbose_name_plural = "Shift Audit Events"
        indexes = [
            models.Index(fields=["tenant", "action"]),
            models.Index(fields=["shift_schedule", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"ShiftAudit {self.event_id} [{self.action}] on {self.shift_schedule_id}"



class StaffSalaryRecord(models.Model):
    """
    Staff Compensation & Salary Ledger (ADR-0110 / P110).
    Maintains confidential payroll parameters: base salary, hourly rate, allowances,
    and bank details. Gated strictly by salary_visibility permission.
    """

    salary_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="staff_salaries")
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="staff_salaries",
        null=True,
        blank=True,
    )
    staff_member = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.CASCADE,
        related_name="salary_records",
    )
    base_salary_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    hourly_rate_bdt = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    allowances_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    bank_name = models.CharField(max_length=128, blank=True, default="")
    bank_account_no = models.CharField(max_length=64, blank=True, default="")
    effective_from = models.DateField(default=timezone.now)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by_username = models.CharField(max_length=128, blank=True, default="")
    updated_by_username = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Staff Salary Record"
        verbose_name_plural = "Staff Salary Records"
        indexes = [
            models.Index(fields=["tenant", "is_active"]),
            models.Index(fields=["staff_member", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"Salary {self.salary_id} [{self.staff_member_id}] - {self.base_salary_bdt} BDT"


class FieldVisibilityAuditEvent(models.Model):
    """
    Immutable audit log for sensitive field access, unmasked exports,
    and salary modifications (ADR-0110).
    """

    event_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="visibility_audit_events",
    )
    action = models.CharField(max_length=64, db_index=True)
    field_category = models.CharField(max_length=64, db_index=True)
    actor_username = models.CharField(max_length=128, blank=True, default="")
    actor_role = models.CharField(max_length=64, blank=True, default="")
    details = models.JSONField(default=dict)
    ip_address = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Field Visibility Audit Event"
        verbose_name_plural = "Field Visibility Audit Events"
        indexes = [
            models.Index(fields=["tenant", "field_category"]),
            models.Index(fields=["actor_username", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"VisibilityAudit {self.event_id} [{self.action}:{self.field_category}] by {self.actor_username}"



class DeviceRegistrationStatus(models.TextChoices):
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending Approval"
    APPROVED = "APPROVED", "Approved & Trusted"
    REJECTED = "REJECTED", "Registration Rejected"
    REVOKED = "REVOKED", "Cryptographic Trust Revoked"


class DeviceKeyAlgorithm(models.TextChoices):
    ED25519 = "ED25519", "Ed25519 Elliptic Curve"
    RSA_2048 = "RSA_2048", "RSA 2048-bit"
    ECDSA_P256 = "ECDSA_P256", "ECDSA P-256"


class LoginDeviceRegistration(models.Model):
    """
    Desktop POS Device Registration & Cryptographic Enrollment (ADR-0111 / P111).
    Maintains workstation cryptographic public key, key fingerprint, hardware binding,
    branch ownership, and maker-checker approval lifecycle.
    """

    registration_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="device_registrations")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="device_registrations")
    device_id = models.CharField(max_length=128, db_index=True)
    device_label = models.CharField(max_length=128)
    device_type = models.CharField(max_length=64, default="DESKTOP_POS")
    public_key_pem = models.TextField()
    key_algorithm = models.CharField(
        max_length=32,
        choices=DeviceKeyAlgorithm.choices,
        default=DeviceKeyAlgorithm.ED25519,
    )
    key_fingerprint = models.CharField(max_length=128, db_index=True)
    hardware_fingerprint = models.CharField(max_length=256, blank=True, default="")
    mac_address = models.CharField(max_length=64, blank=True, default="")
    ip_address = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(
        max_length=32,
        choices=DeviceRegistrationStatus.choices,
        default=DeviceRegistrationStatus.PENDING_APPROVAL,
        db_index=True,
    )
    requested_by_username = models.CharField(max_length=128, blank=True, default="")
    approved_by_username = models.CharField(max_length=128, blank=True, default="")
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_notes = models.TextField(blank=True, default="")
    rejected_by_username = models.CharField(max_length=128, blank=True, default="")
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, default="")
    revoked_by_username = models.CharField(max_length=128, blank=True, default="")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revocation_reason = models.TextField(blank=True, default="")
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Login Device Registration"
        verbose_name_plural = "Login Device Registrations"
        indexes = [
            models.Index(fields=["tenant", "branch", "status"]),
            models.Index(fields=["tenant", "device_id"]),
            models.Index(fields=["key_fingerprint"]),
        ]

    def __str__(self) -> str:
        return f"DeviceReg {self.registration_id} [{self.device_label}] - {self.device_id} ({self.status})"


class DeviceRegistrationAuditEvent(models.Model):
    """
    Immutable audit trail for desktop device registration, cryptographic trust approvals,
    rejections, label modifications, and key revocations (ADR-0111).
    """

    event_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="device_registration_audits",
    )
    device_registration = models.ForeignKey(
        LoginDeviceRegistration,
        on_delete=models.CASCADE,
        related_name="audit_trail",
    )
    action = models.CharField(max_length=64, db_index=True)
    actor_username = models.CharField(max_length=128, blank=True, default="")
    actor_role = models.CharField(max_length=64, blank=True, default="")
    details = models.JSONField(default=dict)
    ip_address = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Device Registration Audit Event"
        verbose_name_plural = "Device Registration Audit Events"
        indexes = [
            models.Index(fields=["tenant", "action"]),
            models.Index(fields=["device_registration", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"DeviceRegAudit {self.event_id} [{self.action}] on {self.device_registration_id} by {self.actor_username}"



class StaffOfflineAuthorization(models.Model):
    """
    Cryptographically signed, time-bound offline staff authorization lease with salted
    credential hashes and risk-limited action boundaries (ADR-0113 / P113).
    Never stores plaintext passwords.
    """

    authorization_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="staff_offline_authorizations",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="staff_offline_authorizations",
    )
    staff_member = models.ForeignKey(
        StaffMember,
        on_delete=models.CASCADE,
        related_name="offline_authorizations",
    )
    device_id = models.CharField(max_length=64, db_index=True)
    role_snapshot = models.CharField(max_length=64, db_index=True)
    credential_salt = models.CharField(max_length=64, help_text="Hex-encoded 16-byte random salt")
    credential_hash = models.CharField(max_length=128, help_text="PBKDF2-HMAC-SHA256 hash of staff PIN + salt")
    permitted_actions = models.JSONField(default=list, help_text="Explicit list of permitted offline actions")
    denied_actions = models.JSONField(default=list, help_text="Explicit list of denied high-risk actions")
    risk_limits = models.JSONField(default=dict, help_text="Financial, discount, and supervisor approval caps")
    issued_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    signature = models.CharField(max_length=128, help_text="HMAC-SHA256 signature of authorization payload")
    is_revoked = models.BooleanField(default=False, db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revocation_reason = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Staff Offline Authorization"
        verbose_name_plural = "Staff Offline Authorizations"
        indexes = [
            models.Index(fields=["tenant", "is_revoked", "expires_at"]),
            models.Index(fields=["staff_member", "device_id"]),
        ]

    def __str__(self) -> str:
        return f"OfflineAuth {self.authorization_id} for {self.staff_member.username} ({self.role_snapshot}) on {self.device_id}"

    def is_valid_offline(self) -> bool:
        return not self.is_revoked and timezone.now() < self.expires_at



class StaffPerformanceMetricSnapshot(models.Model):
    """
    Staff Performance Metrics Snapshot (ADR-0115 / P115).
    Tracks sales volume, refunds, cash reconciliation variance, and reporting freshness
    with tenant, branch, and staff-level scoping.
    """

    snapshot_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="staff_performance_snapshots",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="staff_performance_snapshots",
    )
    staff_member = models.ForeignKey(
        StaffMember,
        on_delete=models.CASCADE,
        related_name="performance_snapshots",
    )
    metric_date = models.DateField(db_index=True)
    total_sales_count = models.PositiveIntegerField(default=0)
    total_sales_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    total_refunds_count = models.PositiveIntegerField(default=0)
    total_refunds_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    net_revenue = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    reconciliation_variance_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    reconciliation_status = models.CharField(
        max_length=32,
        default="BALANCED",
        choices=[
            ("BALANCED", "Balanced"),
            ("OVERAGE", "Overage"),
            ("SHORTAGE", "Shortage"),
        ],
    )
    reporting_freshness_seconds = models.PositiveIntegerField(default=0)
    last_activity_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-metric_date", "-updated_at"]
        verbose_name = "Staff Performance Metric Snapshot"
        verbose_name_plural = "Staff Performance Metric Snapshots"
        unique_together = ("tenant", "staff_member", "metric_date")
        indexes = [
            models.Index(fields=["tenant", "branch", "-metric_date"]),
            models.Index(fields=["staff_member", "-metric_date"]),
        ]

    def __str__(self) -> str:
        return f"StaffMetrics {self.staff_member.username} on {self.metric_date} - Net: {self.net_revenue} BDT"



def get_default_receipt_format():
    return {
        "paper_width": "80mm",
        "header_text": "Pharmacy Care & Dispensary",
        "footer_text": "Prescription medicines cannot be returned after 7 days.",
        "show_tax_breakdown": True,
        "show_pharmacist_signature": True,
        "language": "BILINGUAL",
    }


def get_default_operating_hours():
    return {
        "is_24_7": False,
        "opening_time": "08:00",
        "closing_time": "23:00",
        "weekly_holidays": [],
    }


def get_default_storage_zones():
    return [
        {"zone_code": "DISPENSARY", "name": "Main Dispensary Shelf", "temperature_celsius": "15-25 C"},
        {"zone_code": "COLD_CHAIN", "name": "Biologicals Refrigerator", "temperature_celsius": "2-8 C"},
    ]


def get_default_low_stock_thresholds():
    return {
        "default_min_strips": 10,
        "default_critical_strips": 3,
        "auto_reorder_enabled": False,
    }


def get_default_tax_defaults():
    return {
        "default_vat_rate": "0.00",
        "is_vat_inclusive": True,
        "tax_id_number": "",
    }


def get_default_device_policies():
    return {
        "max_active_pos_terminals": 5,
        "allow_offline_pos_sales": True,
        "max_offline_hours": 72,
        "require_cash_drawer_kick_on_cash_sale": True,
    }


class BranchOperatingSettings(models.Model):
    """
    Branch Operating Settings & Governance (ADR-0116 / P116).
    Enforces receipt formats, operating hours, storage zones, low-stock thresholds,
    tax defaults, and device policies with clear precedence hierarchy:
    Tenant Defaults -> Branch Settings -> Local Device Policy.
    """

    settings_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="branch_operating_settings")
    branch = models.OneToOneField(Branch, on_delete=models.CASCADE, related_name="operating_settings")
    receipt_format = models.JSONField(default=get_default_receipt_format, blank=True)
    operating_hours = models.JSONField(default=get_default_operating_hours, blank=True)
    storage_zones = models.JSONField(default=get_default_storage_zones, blank=True)
    low_stock_thresholds = models.JSONField(default=get_default_low_stock_thresholds, blank=True)
    tax_defaults = models.JSONField(default=get_default_tax_defaults, blank=True)
    device_policies = models.JSONField(default=get_default_device_policies, blank=True)
    last_updated_by = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Branch Operating Settings"
        verbose_name_plural = "Branch Operating Settings"

    def __str__(self) -> str:
        return f"Settings for Branch {self.branch.name} ({self.tenant_id})"



class RegisterAssignmentStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active / Open Drawer"
    CLOSED = "CLOSED", "Closed / Drawer Handed Over"
    RECONCILED = "RECONCILED", "Audited & Reconciled"


class CashierRegisterAssignment(models.Model):
    """
    Cashier Register & Drawer Assignment (ADR-0117 / P117).
    Binds cashier identity and cash register terminal to explicit shift schedule.
    Enforces zero unauthorized concurrent drawer assignment per register and per cashier.
    """

    assignment_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="register_assignments")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="register_assignments")
    shift_schedule = models.ForeignKey(
        StaffShiftSchedule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="register_assignments",
    )
    cashier = models.ForeignKey(
        StaffMember,
        on_delete=models.CASCADE,
        related_name="register_assignments",
    )
    register_id = models.CharField(max_length=64, db_index=True)
    opening_float_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    closing_float_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=32,
        choices=RegisterAssignmentStatus.choices,
        default=RegisterAssignmentStatus.ACTIVE,
        db_index=True,
    )
    notes = models.TextField(blank=True, default="")
    assigned_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-assigned_at"]
        verbose_name = "Cashier Register Assignment"
        verbose_name_plural = "Cashier Register Assignments"
        indexes = [
            models.Index(fields=["tenant", "branch", "status"]),
            models.Index(fields=["branch", "register_id", "status"]),
            models.Index(fields=["cashier", "status"]),
        ]

    def __str__(self) -> str:
        return f"Assignment {self.assignment_id} - Reg {self.register_id} to {self.cashier.full_name} [{self.status}]"


class RoleGrantMigrationReason(models.TextChoices):
    PLAN_DOWNGRADE = "PLAN_DOWNGRADE", "Plan Downgrade"
    TEMPLATE_EVOLUTION = "TEMPLATE_EVOLUTION", "Template Evolution"
    COMPLIANCE_PRUNING = "COMPLIANCE_PRUNING", "Compliance Pruning"
    MANUAL_RECONCILIATION = "MANUAL_RECONCILIATION", "Manual Reconciliation"


class RoleGrantMigrationRecord(models.Model):
    """
    Audited, versioned snapshot tracking role grants migration, removal of invalid entitlements,
    and preservation of forbidden grant guards (ADR-0119 / P119).
    """

    migration_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="role_migrations")
    role = models.ForeignKey(CustomRoleDefinition, on_delete=models.CASCADE, related_name="migration_history")
    previous_version = models.PositiveIntegerField(default=1)
    new_version = models.PositiveIntegerField(default=2)
    reason = models.CharField(
        max_length=32,
        choices=RoleGrantMigrationReason.choices,
        default=RoleGrantMigrationReason.PLAN_DOWNGRADE,
    )
    previous_permissions = models.JSONField(default=list)
    retained_permissions = models.JSONField(default=list)
    removed_permissions = models.JSONField(default=list)
    forbidden_grants_blocked = models.JSONField(default=list)
    is_dry_run = models.BooleanField(default=False)
    applied_by = models.CharField(max_length=128, blank=True, default="SYSTEM")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Role Grant Migration Record"
        verbose_name_plural = "Role Grant Migration Records"
        indexes = [
            models.Index(fields=["tenant", "created_at"]),
            models.Index(fields=["role", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"Migration {self.migration_id} - Role {self.role.role_code} (v{self.previous_version}->v{self.new_version}) [{self.reason}]"


class DosageFormDefinition(models.Model):
    """
    Centralized Dosage Form Metadata Registry (ADR-0122 / P122).
    Defines dosage forms, administration routes, packaging conversion rules, and configurable field groups.
    """

    form_id = models.CharField(max_length=64, primary_key=True)
    form_code = models.CharField(max_length=64, unique=True, db_index=True)
    name_en = models.CharField(max_length=128)
    name_bn = models.CharField(max_length=128)
    category = models.CharField(max_length=64, default="SOLID", db_index=True)
    default_route = models.CharField(max_length=64, default="Oral")
    is_strip_applicable = models.BooleanField(default=True, help_text="Box to strip to unit conversion applicable")
    is_volume_applicable = models.BooleanField(default=False, help_text="Volume in ml applicable")
    is_cold_chain_applicable = models.BooleanField(default=False, help_text="Cold chain storage typically applicable")
    display_order = models.PositiveIntegerField(default=10, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    field_groups = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "name_en"]
        verbose_name = "Dosage Form Definition"
        verbose_name_plural = "Dosage Form Definitions"
        indexes = [
            models.Index(fields=["is_active", "display_order"]),
            models.Index(fields=["category", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.name_en} ({self.form_code}) - {self.category}"


class MedicineEditorSchema(models.Model):
    """
    Architecture Decision: ADR-0124 (P124).
    Server-driven medicine editor schema registry with conditional sections, validation rules,
    default values, and schema versioning.
    """

    schema_id = models.CharField(max_length=64, primary_key=True)
    schema_version = models.CharField(max_length=32, unique=True, db_index=True)
    title_en = models.CharField(max_length=128, default="Medicine Master Editor")
    title_bn = models.CharField(max_length=128, default="ঔষধ বিবরণী ও মাস্টার ফর্ম")
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    sections = models.JSONField(default=list, blank=True)
    conditional_rules = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-schema_version"]
        verbose_name = "Medicine Editor Schema"
        verbose_name_plural = "Medicine Editor Schemas"
        indexes = [
            models.Index(fields=["is_active", "schema_version"]),
        ]

    def __str__(self) -> str:
        return f"{self.title_en} ({self.schema_version}) - Active: {self.is_active}"


class MedicineFormTemplate(models.Model):
    """
    Architecture Decision: ADR-0131 (P131).
    Dosage-Form Form Template Registry.
    Publishes dosage-form specific form layouts, widget specifications,
    allowed values (units, routes, container types), and validation constraints.
    """

    template_id = models.CharField(max_length=64, primary_key=True)
    dosage_form = models.ForeignKey(
        "core.DosageFormDefinition",
        on_delete=models.CASCADE,
        related_name="form_templates",
        help_text="Canonical dosage form definition this template applies to.",
    )
    template_code = models.CharField(max_length=64, db_index=True)
    template_version = models.CharField(max_length=32, default="v1.0", db_index=True)
    title_en = models.CharField(max_length=128)
    title_bn = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    is_default = models.BooleanField(default=True, db_index=True)
    layout_rules = models.JSONField(
        default=dict,
        blank=True,
        help_text="Responsive layout rules, grid columns, sections, and field widget configurations.",
    )
    allowed_values = models.JSONField(
        default=dict,
        blank=True,
        help_text="Permitted strength units, routes, container types, and storage parameters.",
    )
    validation_rules = models.JSONField(
        default=list,
        blank=True,
        help_text="Cross-field validation rules and constraints for client and server evaluation.",
    )
    immutable_version_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="SHA-256 hash ensuring regulatory immutability and provenance tracking.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["dosage_form", "-template_version", "template_code"]
        verbose_name = "Medicine Form Template"
        verbose_name_plural = "Medicine Form Templates"
        constraints = [
            models.UniqueConstraint(
                fields=["dosage_form", "template_code", "template_version"],
                name="unique_dosage_form_template_version",
            )
        ]
        indexes = [
            models.Index(fields=["dosage_form", "is_active", "is_default"]),
            models.Index(fields=["template_code", "is_active"]),
        ]

    def compute_version_hash(self) -> str:
        """Computes SHA-256 hash across template specification fields."""
        import hashlib
        import json

        payload = (
            f"{self.dosage_form_id}|{self.template_code}|{self.template_version}|"
            f"{json.dumps(self.layout_rules, sort_keys=True)}|{json.dumps(self.allowed_values, sort_keys=True)}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        if not self.immutable_version_hash:
            self.immutable_version_hash = self.compute_version_hash()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.title_en} ({self.template_code} {self.template_version}) - {self.dosage_form_id}"


class ContainerType(models.TextChoices):
    BLISTER_PACK = "BLISTER_PACK", "Blister Pack"
    ALU_ALU = "ALU_ALU", "Alu-Alu Strip"
    STRIP = "STRIP", "Regular Foil Strip"
    GLASS_BOTTLE = "GLASS_BOTTLE", "Glass Bottle"
    PET_BOTTLE = "PET_BOTTLE", "PET Plastic Bottle"
    VIAL = "VIAL", "Vial (Liquid/Powder)"
    AMPOULE = "AMPOULE", "Ampoule"
    TUBE = "TUBE", "Ointment/Cream Tube"
    SACHET = "SACHET", "Oral Powder Sachet"
    INHALER_CANISTER = "INHALER_CANISTER", "Pressurized Inhaler Canister"
    DROPS_BOTTLE = "DROPS_BOTTLE", "Eye/Ear Dropper Bottle"
    OTHER = "OTHER", "Other Packaging Container"


class MedicineVariant(models.Model):
    """
    Architecture Decision: ADR-0126 (P126).
    Commercial strength/size/route/container presentation variants modeled as distinct SKUs
    tied to an immutable master medicine and DGDA regulated classification.
    """

    variant_id = models.CharField(max_length=64, primary_key=True)
    product = models.ForeignKey(
        "core.MedicineProduct",
        on_delete=models.CASCADE,
        related_name="variants",
        help_text="Parent master clinical medicine product.",
    )
    sku = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="Unique Stock Keeping Unit (e.g. SKU-BEX-NAPA-500-100B).",
    )
    variant_name = models.CharField(
        max_length=255,
        help_text="Full variant title (e.g. Napa 500mg Tablet - Box of 10x10).",
    )
    strength = models.CharField(max_length=64, help_text="e.g. 500 mg, 665 mg, 120mg/5mL")
    strength_numeric = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    strength_unit = models.CharField(max_length=32, blank=True, default="mg")
    pack_size = models.CharField(max_length=64, help_text="e.g. Box of 100, 100 mL Bottle")
    pack_size_numeric = models.PositiveIntegerField(default=1, help_text="Total base units contained in pack")
    route = models.CharField(max_length=64, blank=True, default="Oral")
    container_type = models.CharField(
        max_length=64,
        choices=ContainerType.choices,
        default=ContainerType.BLISTER_PACK,
    )
    barcode = models.CharField(max_length=64, blank=True, default="", db_index=True)
    mrp_unit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Retail price per single tablet / mL in BDT.",
    )
    cost_unit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Cost price per single tablet / mL in BDT.",
    )
    mrp_pack = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Retail price for full pack in BDT.",
    )
    cost_pack = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Cost price for full pack in BDT.",
    )
    is_prescription_required = models.BooleanField(default=False, db_index=True)
    is_narcotic = models.BooleanField(default=False, db_index=True)
    is_antibiotic = models.BooleanField(default=False, db_index=True)
    is_cold_chain = models.BooleanField(default=False, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product", "pack_size_numeric", "variant_name"]
        verbose_name = "Medicine Variant"
        verbose_name_plural = "Medicine Variants"
        indexes = [
            models.Index(fields=["product", "is_active"]),
            models.Index(fields=["sku"]),
            models.Index(fields=["barcode"]),
        ]

    def __str__(self) -> str:
        return f"{self.variant_name} ({self.sku})"


class Manufacturer(models.Model):
    """
    Architecture Decision: ADR-0127 (P127).
    Global master reference entity for DGDA licensed pharmaceutical manufacturers.
    Centrally curated by platform superadmin; read-only for tenant pharmacies.
    Supports merge policy for deduplication, archival, and alias tracking.
    """

    manufacturer_id = models.CharField(max_length=64, primary_key=True)
    name = models.CharField(max_length=255, unique=True, db_index=True)
    name_bn = models.CharField(max_length=255, blank=True, default="")
    code = models.CharField(max_length=64, unique=True, db_index=True)
    dgda_license_no = models.CharField(max_length=128, blank=True, default="", db_index=True)
    country = models.CharField(max_length=64, default="BD")
    website = models.URLField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    merged_into = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="merged_manufacturers",
        help_text="Canonical manufacturer if this entry was merged as a duplicate.",
    )
    merge_reason = models.TextField(blank=True, default="")
    merged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Manufacturer"
        verbose_name_plural = "Manufacturers"
        indexes = [
            models.Index(fields=["is_active", "name"]),
            models.Index(fields=["code"]),
            models.Index(fields=["dgda_license_no"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class SupplierStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    INACTIVE = "INACTIVE", "Inactive"
    SUSPENDED = "SUSPENDED", "Suspended"
    BLACKLISTED = "BLACKLISTED", "Blacklisted"


class SupplierRegion(models.TextChoices):
    DHAKA = "DHAKA", "Dhaka"
    CHITTAGONG = "CHITTAGONG", "Chittagong"
    RAJSHAHI = "RAJSHAHI", "Rajshahi"
    KHULNA = "KHULNA", "Khulna"
    BARISAL = "BARISAL", "Barisal"
    SYLHET = "SYLHET", "Sylhet"
    RANGPUR = "RANGPUR", "Rangpur"
    MYMENSINGH = "MYMENSINGH", "Mymensingh"
    OTHER = "OTHER", "Other"


class SupplierPostingType(models.TextChoices):
    GRN_INVOICE = "GRN_INVOICE", "Goods Received Invoice"
    PAYMENT = "PAYMENT", "Payment to Supplier"
    RETURN_DEBIT_NOTE = "RETURN_DEBIT_NOTE", "Purchase Return / Debit Note"
    OPENING_BALANCE = "OPENING_BALANCE", "Opening Balance"
    ADJUSTMENT = "ADJUSTMENT", "Audit Adjustment"


class Supplier(models.Model):
    """
    Architecture Decision: ADR-0127 (P127).
    Tenant-scoped pharmaceutical supplier, distributor, or regional depot directory.
    Supports merge, archival, deduplication, and DGDA drug license traceability.
    """

    supplier_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="suppliers",
        help_text="Owning pharmacy tenant account.",
    )
    name = models.CharField(max_length=255, db_index=True)
    name_bn = models.CharField(max_length=255, blank=True, default="")
    code = models.CharField(max_length=64, blank=True, default="", db_index=True)
    contact_person = models.CharField(max_length=128, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="", db_index=True)
    email = models.EmailField(blank=True, default="")
    address = models.TextField(blank=True, default="")
    dl_number = models.CharField(max_length=128, blank=True, default="", help_text="Wholesale drug license number")
    legal_name = models.CharField(max_length=255, blank=True, default="", help_text="Registered company legal name")
    region = models.CharField(max_length=32, choices=SupplierRegion.choices, default=SupplierRegion.DHAKA, db_index=True)
    district = models.CharField(max_length=64, blank=True, default="")
    tax_id = models.CharField(max_length=64, blank=True, default="", help_text="Legacy tax reference")
    tin_number = models.CharField(max_length=64, blank=True, default="", help_text="Taxpayer Identification Number (e-TIN)")
    bin_number = models.CharField(max_length=64, blank=True, default="", help_text="9 or 13-digit Business Identification Number (VAT)")
    trade_license_number = models.CharField(max_length=128, blank=True, default="", help_text="City Corporation/Municipal Trade License")
    status = models.CharField(max_length=32, choices=SupplierStatus.choices, default=SupplierStatus.ACTIVE, db_index=True)
    payment_terms_days = models.PositiveIntegerField(default=30, help_text="Standard credit terms window in days")
    credit_limit = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Approved credit limit in BDT",
    )
    current_balance = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        help_text="Current accounts payable balance owed to supplier (BDT)",
    )
    bank_name = models.CharField(max_length=128, blank=True, default="")
    bank_account_name = models.CharField(max_length=128, blank=True, default="")
    bank_account_no = models.CharField(max_length=64, blank=True, default="")
    bank_branch = models.CharField(max_length=128, blank=True, default="")
    bank_routing_number = models.CharField(max_length=32, blank=True, default="")
    lead_time_days = models.PositiveIntegerField(default=2, help_text="Typical delivery lead time in days")
    proof_of_origin_verified = models.BooleanField(default=False, help_text="Verified manufacturer/DGDA distribution proof")
    proof_of_origin_notes = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    merged_into = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="merged_suppliers",
        help_text="Canonical supplier if merged as duplicate.",
    )
    merge_reason = models.TextField(blank=True, default="")
    merged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant", "name"]
        verbose_name = "Supplier"
        verbose_name_plural = "Suppliers"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"],
                condition=models.Q(is_active=True, merged_into__isnull=True),
                name="unique_active_supplier_per_tenant",
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "is_active"]),
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "phone"]),
            models.Index(fields=["tenant", "region"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.tenant_id})"

    def clean(self):
        super().clean()
        if not self.supplier_id:
            self.supplier_id = f"sup-{uuid.uuid4().hex[:12]}"
        elif not str(self.supplier_id).startswith("sup-"):
            self.supplier_id = f"sup-{self.supplier_id}"
        if self.status != SupplierStatus.ACTIVE:
            self.is_active = False
        else:
            self.is_active = True

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class SupplierLedgerEntry(models.Model):
    """
    Architecture Decision: ADR-0181 (P181).
    Immutable double-entry accounts payable ledger tracking supplier invoice credits,
    payment debits, return debit notes, and running balance.
    """

    posting_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="supplier_ledger_entries",
        db_index=True,
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.CASCADE,
        related_name="ledger_entries",
        db_index=True,
    )
    branch = models.ForeignKey(
        "core.Branch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supplier_ledger_entries",
    )
    posting_type = models.CharField(
        max_length=32,
        choices=SupplierPostingType.choices,
        default=SupplierPostingType.GRN_INVOICE,
        db_index=True,
    )
    reference_number = models.CharField(max_length=128, db_index=True, help_text="GRN number, PO number, or payment voucher ID")
    debit_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Decreases AP payable liability (payments, purchase return debit notes)",
    )
    credit_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Increases AP payable liability (GRN invoice receipts)",
    )
    balance_after = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        help_text="Running AP payable balance owed after this posting",
    )
    currency = models.CharField(max_length=8, default="BDT")
    notes = models.TextField(blank=True, default="")
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supplier_postings",
    )
    posted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-posted_at"]
        verbose_name = "Supplier Ledger Entry"
        verbose_name_plural = "Supplier Ledger Entries"
        indexes = [
            models.Index(fields=["tenant", "supplier", "-posted_at"]),
            models.Index(fields=["tenant", "reference_number"]),
        ]

    def __str__(self) -> str:
        return f"SupplierLedger {self.posting_id}: {self.supplier.name} ({self.posting_type} Ref: {self.reference_number})"

    def clean(self):
        super().clean()
        if not self.posting_id:
            self.posting_id = f"sup-post-{uuid.uuid4().hex[:12]}"
        elif not str(self.posting_id).startswith("sup-post-"):
            self.posting_id = f"sup-post-{self.posting_id}"

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


# Semantic alias for Phase 10
SupplierOrganization = Supplier


class SupplierContactPersonRole(models.TextChoices):
    DEPOT_MANAGER = "DEPOT_MANAGER", "Depot Manager / In-Charge"
    SALES_REPRESENTATIVE = "SALES_REPRESENTATIVE", "Sales Representative / MIO"
    ORDER_BOOKER = "ORDER_BOOKER", "Order Booker / Commercial Officer"
    ACCOUNTS_EXECUTIVE = "ACCOUNTS_EXECUTIVE", "Accounts / Billing Executive"
    DISPATCH_LOGISTICS = "DISPATCH_LOGISTICS", "Dispatch / Delivery Logistics"
    ESCALATION_MANAGER = "ESCALATION_MANAGER", "Escalation / Emergency Contact"
    OTHER = "OTHER", "Other Representative"


class PreferredCommunicationMethod(models.TextChoices):
    PHONE = "PHONE", "Phone Call"
    WHATSAPP = "WHATSAPP", "WhatsApp"
    EMAIL = "EMAIL", "Email"
    SMS = "SMS", "SMS"


class SupplierContactPerson(models.Model):
    """
    Architecture Decision: ADR-0182 (P182).
    Tenant-scoped supplier contact person capturing multiple functional roles
    (depot manager, sales representative, order booker, billing, logistics),
    preferred communication channels, and primary contact designation.
    """

    contact_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="supplier_contacts",
        db_index=True,
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.CASCADE,
        related_name="contacts",
        db_index=True,
    )
    name = models.CharField(max_length=128, db_index=True)
    name_bn = models.CharField(max_length=128, blank=True, default="")
    designation = models.CharField(max_length=128, blank=True, default="")
    role = models.CharField(
        max_length=32,
        choices=SupplierContactPersonRole.choices,
        default=SupplierContactPersonRole.SALES_REPRESENTATIVE,
        db_index=True,
    )
    phone = models.CharField(max_length=32, db_index=True)
    alternate_phone = models.CharField(max_length=32, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    preferred_communication_method = models.CharField(
        max_length=16,
        choices=PreferredCommunicationMethod.choices,
        default=PreferredCommunicationMethod.PHONE,
        db_index=True,
    )
    is_primary = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Designated default representative for purchase orders and billing alerts.",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    notes = models.TextField(blank=True, default="", help_text="Availability hours or territory details")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["supplier", "-is_primary", "name"]
        verbose_name = "Supplier Contact Person"
        verbose_name_plural = "Supplier Contact Persons"
        indexes = [
            models.Index(fields=["tenant", "supplier", "is_primary"]),
            models.Index(fields=["tenant", "supplier", "role"]),
            models.Index(fields=["tenant", "phone"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_role_display()}) - {self.supplier.name}"

    def clean(self):
        super().clean()
        if not self.contact_id:
            self.contact_id = f"sup-cnt-{uuid.uuid4().hex[:12]}"
        elif not str(self.contact_id).startswith("sup-cnt-"):
            self.contact_id = f"sup-cnt-{self.contact_id}"
        if self.supplier_id and self.tenant_id:
            if hasattr(self, "supplier") and self.supplier and self.supplier.tenant_id != self.tenant_id:
                raise ValidationError({"supplier": "Supplier must belong to the same tenant as the contact person."})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class VendorProductPriceList(models.Model):
    """
    Architecture Decision: ADR-0183 (P183).
    Tenant-scoped supplier product price quotation governing unit purchasing costs,
    packaging sizes, effective date windows, minimum order quantities (MOQ),
    and negotiated trade discounts.
    """

    price_entry_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="vendor_price_lists",
        db_index=True,
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.CASCADE,
        related_name="product_price_lists",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="supplier_price_lists",
        db_index=True,
    )
    supplier_sku_code = models.CharField(max_length=64, blank=True, default="", db_index=True)
    package_type = models.CharField(max_length=32, default="BOX")
    package_size = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Number of base units in package (e.g. 100 tablets per box)",
    )
    unit_cost_price = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0001"))],
        help_text="Quoted purchase cost per individual base unit (BDT)",
    )
    package_cost_price = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Calculated total cost per package",
    )
    mrp_unit = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Maximum Retail Price benchmark for regulatory margin validation",
    )
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
        help_text="Negotiated trade or promotional discount percentage",
    )
    effective_from = models.DateField(db_index=True)
    effective_to = models.DateField(null=True, blank=True, db_index=True)
    minimum_order_quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Minimum order threshold in base units required to claim this quotation",
    )
    free_goods_threshold = models.PositiveIntegerField(
        default=0,
        help_text="Buy X units bonus threshold (e.g. 10)",
    )
    free_goods_bonus_quantity = models.PositiveIntegerField(
        default=0,
        help_text="Get Y bonus units free (e.g. 1)",
    )
    is_contracted = models.BooleanField(
        default=False,
        help_text="Formal negotiated price agreement vs ad-hoc promotional quotation",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_price_lists",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-effective_from", "product__brand_name"]
        verbose_name = "Vendor Product Price List"
        verbose_name_plural = "Vendor Product Price Lists"
        indexes = [
            models.Index(fields=["tenant", "supplier", "product", "is_active"]),
            models.Index(fields=["tenant", "effective_from", "effective_to"]),
            models.Index(fields=["tenant", "product", "unit_cost_price"]),
        ]

    def __str__(self) -> str:
        return f"PriceList {self.price_entry_id}: {self.product.brand_name} by {self.supplier.name} @ {self.unit_cost_price} BDT"

    @property
    def net_unit_cost(self) -> Decimal:
        """Returns net unit cost after applying trade discount percentage."""
        discount_factor = Decimal("1.0000") - (self.discount_percentage / Decimal("100.00"))
        return (self.unit_cost_price * discount_factor).quantize(Decimal("0.0001"))

    def clean(self):
        super().clean()
        if not self.price_entry_id:
            self.price_entry_id = f"vpl-{uuid.uuid4().hex[:12]}"
        elif not str(self.price_entry_id).startswith("vpl-"):
            self.price_entry_id = f"vpl-{self.price_entry_id}"

        if self.effective_to and self.effective_to < self.effective_from:
            raise ValidationError({"effective_to": "Effective to date cannot precede effective from date."})

        if self.mrp_unit and self.mrp_unit > Decimal("0.0000") and self.unit_cost_price > self.mrp_unit:
            raise ValidationError({"unit_cost_price": "Quoted purchase unit cost cannot exceed Maximum Retail Price (MRP)."})

        if self.package_cost_price == Decimal("0.0000") and self.unit_cost_price and self.package_size:
            self.package_cost_price = (self.unit_cost_price * Decimal(self.package_size)).quantize(Decimal("0.0001"))

        if self.supplier_id and self.tenant_id:
            if hasattr(self, "supplier") and self.supplier and self.supplier.tenant_id != self.tenant_id:
                raise ValidationError({"supplier": "Supplier must belong to the same tenant as the price list entry."})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class RequisitionDepartment(models.TextChoices):
    DISPENSARY = "DISPENSARY", "Dispensary / Outpatient"
    CENTRAL_STORE = "CENTRAL_STORE", "Central Store"
    OT_PHARMACY = "OT_PHARMACY", "Operation Theater Pharmacy"
    INPATIENT = "INPATIENT", "Inpatient / Ward Pharmacy"
    GENERAL = "GENERAL", "General Requisition"


class RequisitionPriority(models.TextChoices):
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"
    EMERGENCY = "EMERGENCY", "Emergency / Urgent"


class RequisitionStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted for Approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"
    CONVERTED_TO_PO = "CONVERTED_TO_PO", "Converted to Purchase Order"


class PurchaseRequisition(models.Model):
    """
    Architecture Decision: ADR-0184 (P184).
    Tenant and branch scoped purchase requisition with department, priority,
    budget tracking, and approval lifecycle state machine.
    """

    requisition_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="purchase_requisitions",
        db_index=True,
    )
    branch = models.ForeignKey(
        "core.Branch",
        on_delete=models.CASCADE,
        related_name="purchase_requisitions",
        db_index=True,
    )
    department = models.CharField(
        max_length=32,
        choices=RequisitionDepartment.choices,
        default=RequisitionDepartment.DISPENSARY,
        db_index=True,
    )
    priority = models.CharField(
        max_length=16,
        choices=RequisitionPriority.choices,
        default=RequisitionPriority.MEDIUM,
        db_index=True,
    )
    status = models.CharField(
        max_length=24,
        choices=RequisitionStatus.choices,
        default=RequisitionStatus.DRAFT,
        db_index=True,
    )
    total_estimated_cost = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Calculated total estimated cost of all line items (BDT)",
    )
    allocated_budget = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Allocated budget cap for this requisition (BDT)",
    )
    justification = models.TextField(blank=True, default="", help_text="Clinical or business justification")
    target_delivery_date = models.DateField(null=True, blank=True, db_index=True)

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_requisitions",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_requisitions",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_notes = models.TextField(blank=True, default="")

    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rejected_requisitions",
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, default="")

    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_requisitions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Purchase Requisition"
        verbose_name_plural = "Purchase Requisitions"
        indexes = [
            models.Index(fields=["tenant", "branch", "status"]),
            models.Index(fields=["tenant", "department", "status"]),
            models.Index(fields=["tenant", "priority", "status"]),
            models.Index(fields=["tenant", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"Requisition {self.requisition_id} [{self.department}] ({self.status})"

    def clean(self):
        super().clean()
        if not self.requisition_id:
            self.requisition_id = f"req-{uuid.uuid4().hex[:12]}"
        elif not (str(self.requisition_id).startswith("req-") or str(self.requisition_id).startswith("pr-")):
            self.requisition_id = f"req-{self.requisition_id}"

        if self.branch_id and self.tenant_id:
            if hasattr(self, "branch") and self.branch and self.branch.tenant_id != self.tenant_id:
                raise ValidationError({"branch": "Branch must belong to the requisition tenant."})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class PurchaseRequisitionItem(models.Model):
    """
    Architecture Decision: ADR-0184 (P184).
    Line item for a purchase requisition specifying medicine product,
    packaging, requested quantity, and estimated procurement cost.
    """

    item_id = models.CharField(max_length=64, primary_key=True)
    requisition = models.ForeignKey(
        PurchaseRequisition,
        on_delete=models.CASCADE,
        related_name="items",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="requisition_items",
        db_index=True,
    )
    preferred_supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="preferred_requisition_items",
        db_index=True,
    )
    package_type = models.CharField(max_length=32, default="BOX")
    package_size = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Units per package (e.g. 100 tablets per box)",
    )
    requested_quantity = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Total base units requested",
    )
    estimated_unit_cost = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0001"))],
        help_text="Estimated purchase cost per base unit (BDT)",
    )
    estimated_total_cost = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Calculated line total (BDT)",
    )
    current_stock_quantity = models.IntegerField(
        default=0,
        help_text="Snapshot of on-hand branch inventory at requisition creation",
    )
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["item_id"]
        verbose_name = "Purchase Requisition Item"
        verbose_name_plural = "Purchase Requisition Items"
        indexes = [
            models.Index(fields=["requisition", "product"]),
        ]

    def __str__(self) -> str:
        return f"Item {self.item_id}: {self.product.brand_name} x {self.requested_quantity}"

    def clean(self):
        super().clean()
        if not self.item_id:
            self.item_id = f"pri-{uuid.uuid4().hex[:12]}"
        elif not str(self.item_id).startswith("pri-"):
            self.item_id = f"pri-{self.item_id}"

        if self.estimated_unit_cost and self.requested_quantity:
            self.estimated_total_cost = (self.estimated_unit_cost * Decimal(self.requested_quantity)).quantize(Decimal("0.0001"))

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class PurchaseOrderStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    APPROVED = "APPROVED", "Approved"
    ORDERED = "ORDERED", "Ordered / Sent to Supplier"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED", "Partially Received"
    CLOSED = "CLOSED", "Closed / Completed"
    CANCELLED = "CANCELLED", "Cancelled"


class PurchaseOrder(models.Model):
    """
    Architecture Decision: ADR-0185 (P185).
    Tenant and branch scoped Purchase Order (PO) representing a formal commercial commitment
    to a pharmaceutical distributor or manufacturer depot, with guarded lifecycle transitions.
    """

    po_id = models.CharField(max_length=64, primary_key=True)
    po_number = models.CharField(max_length=64, db_index=True)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="purchase_orders",
        db_index=True,
    )
    branch = models.ForeignKey(
        "core.Branch",
        on_delete=models.CASCADE,
        related_name="purchase_orders",
        db_index=True,
    )
    supplier = models.ForeignKey(
        "core.Supplier",
        on_delete=models.CASCADE,
        related_name="purchase_orders",
        db_index=True,
    )
    requisition = models.ForeignKey(
        "core.PurchaseRequisition",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchase_orders",
        db_index=True,
    )
    status = models.CharField(
        max_length=32,
        choices=PurchaseOrderStatus.choices,
        default=PurchaseOrderStatus.DRAFT,
        db_index=True,
    )
    payment_terms_days = models.PositiveIntegerField(
        default=30,
        help_text="Agreed credit payment window in days",
    )
    currency = models.CharField(max_length=8, default="BDT")
    subtotal_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Sum of gross line amounts before order-level discounts/taxes",
    )
    discount_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Negotiated overall order discount in BDT",
    )
    tax_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Applicable VAT/tax in BDT",
    )
    total_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Final net payable commitment in BDT",
    )
    expected_delivery_date = models.DateField(null=True, blank=True, db_index=True)
    ordered_at = models.DateTimeField(null=True, blank=True)

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_purchase_orders",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_notes = models.TextField(blank=True, default="")

    closed_at = models.DateTimeField(null=True, blank=True)
    closing_notes = models.TextField(blank=True, default="")

    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cancelled_purchase_orders",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True, default="")

    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_purchase_orders",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Purchase Order"
        verbose_name_plural = "Purchase Orders"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "po_number"],
                name="unique_tenant_po_number",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "branch", "status"]),
            models.Index(fields=["tenant", "supplier", "status"]),
            models.Index(fields=["tenant", "status", "created_at"]),
            models.Index(fields=["tenant", "po_number"]),
        ]

    def __str__(self) -> str:
        return f"PO {self.po_number} [{self.supplier.name}] ({self.status})"

    def clean(self):
        super().clean()
        if not self.po_id:
            self.po_id = f"po-{uuid.uuid4().hex[:12]}"
        elif not str(self.po_id).startswith("po-"):
            self.po_id = f"po-{self.po_id}"

        if not self.po_number:
            date_prefix = timezone.now().strftime("%Y%m")
            self.po_number = f"PO-{date_prefix}-{uuid.uuid4().hex[:6].upper()}"

        if self.branch_id and self.tenant_id:
            if hasattr(self, "branch") and self.branch and self.branch.tenant_id != self.tenant_id:
                raise ValidationError({"branch": "Branch must belong to the same tenant as the purchase order."})

        if self.supplier_id and self.tenant_id:
            if hasattr(self, "supplier") and self.supplier and self.supplier.tenant_id != self.tenant_id:
                raise ValidationError({"supplier": "Supplier must belong to the same tenant as the purchase order."})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class DeliveryStatus(models.TextChoices):
    PENDING = "PENDING", "Pending Delivery"
    PARTIALLY_DELIVERED = "PARTIALLY_DELIVERED", "Partially Delivered"
    FULLY_DELIVERED = "FULLY_DELIVERED", "Fully Delivered"
    BACKORDERED = "BACKORDERED", "Backordered"
    CANCELLED = "CANCELLED", "Cancelled"


class PurchaseOrderItem(models.Model):
    """
    Architecture Decision: ADR-0185 (P185) & ADR-0186 (P186).
    Line item for a Purchase Order specifying ordered product, packaging,
    pack units, package size, unit/pack costs, line discounts, VAT/tax rates,
    and SKU-specific expected delivery schedules.
    """

    item_id = models.CharField(max_length=64, primary_key=True)
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="items",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="purchase_order_items",
        db_index=True,
    )
    pack_unit = models.CharField(max_length=32, default="BOX")
    package_type = models.CharField(max_length=32, default="BOX")
    package_size = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Units per package (e.g. 100 tablets per box)",
    )
    ordered_packages = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Quantity ordered in commercial packaging units",
    )
    ordered_quantity = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Total base units ordered",
    )
    received_quantity = models.PositiveIntegerField(
        default=0,
        help_text="Cumulative base units received so far",
    )
    cancelled_quantity = models.PositiveIntegerField(
        default=0,
        help_text="Quantity cancelled or closed as unfulfillable backorder",
    )
    delivery_status = models.CharField(
        max_length=32,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
        db_index=True,
    )
    unit_cost_price = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0001"))],
        help_text="Quoted purchase cost per individual base unit (BDT)",
    )
    package_cost_price = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Calculated purchase cost per pack (BDT)",
    )
    mrp_unit = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Maximum Retail Price ceiling benchmark (BDT)",
    )
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
        help_text="Line trade or promotional discount percentage",
    )
    net_unit_cost = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Net unit cost after discount (BDT)",
    )
    tax_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
        help_text="Line-level VAT/Tax percentage",
    )
    tax_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Calculated line VAT/Tax amount in BDT",
    )
    line_subtotal = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Line net total before tax (BDT)",
    )
    line_total = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Calculated line total liability including tax (BDT)",
    )
    expected_delivery_date = models.DateField(null=True, blank=True, db_index=True)
    free_goods_bonus_quantity = models.PositiveIntegerField(
        default=0,
        help_text="Bonus promotional free units provided with this line",
    )
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["item_id"]
        verbose_name = "Purchase Order Item"
        verbose_name_plural = "Purchase Order Items"
        indexes = [
            models.Index(fields=["purchase_order", "product"]),
        ]

    def __str__(self) -> str:
        return f"POItem {self.item_id}: {self.product.brand_name} (Ordered: {self.ordered_quantity}, Recv: {self.received_quantity})"

    @property
    def backordered_quantity(self) -> int:
        return max(0, self.ordered_quantity - self.received_quantity - self.cancelled_quantity)

    @property
    def is_backordered(self) -> bool:
        return self.backordered_quantity > 0 and self.received_quantity > 0

    def clean(self):
        super().clean()
        if not self.item_id:
            self.item_id = f"poi-{uuid.uuid4().hex[:12]}"
        elif not str(self.item_id).startswith("poi-"):
            self.item_id = f"poi-{self.item_id}"

        if not self.pack_unit and self.package_type:
            self.pack_unit = self.package_type
        elif self.pack_unit and not self.package_type:
            self.package_type = self.pack_unit

        self.package_size = max(1, self.package_size)
        if self.ordered_packages and self.package_size:
            calculated_qty = self.ordered_packages * self.package_size
            if not self.ordered_quantity or self.ordered_quantity < calculated_qty:
                self.ordered_quantity = calculated_qty

        if self.unit_cost_price and self.package_size:
            self.package_cost_price = (self.unit_cost_price * Decimal(self.package_size)).quantize(Decimal("0.0001"))

        discount_factor = Decimal("1.0000") - (self.discount_percentage / Decimal("100.00"))
        self.net_unit_cost = (self.unit_cost_price * discount_factor).quantize(Decimal("0.0001"))
        self.line_subtotal = (self.net_unit_cost * Decimal(self.ordered_quantity)).quantize(Decimal("0.0001"))
        self.tax_amount = (self.line_subtotal * (self.tax_percentage / Decimal("100.00"))).quantize(Decimal("0.0001"))
        self.line_total = (self.line_subtotal + self.tax_amount).quantize(Decimal("0.0001"))

        mrp = self.mrp_unit
        if (not mrp or mrp == Decimal("0.0000")) and self.product_id and hasattr(self, "product") and self.product:
            mrp = self.product.mrp_unit
            self.mrp_unit = mrp or Decimal("0.0000")

        if mrp and mrp > Decimal("0.0000") and self.unit_cost_price > mrp:
            raise ValidationError({"unit_cost_price": "Quoted purchase unit cost cannot exceed Maximum Retail Price (MRP)."})

        # Synchronize delivery status
        if self.received_quantity >= self.ordered_quantity:
            self.delivery_status = DeliveryStatus.FULLY_DELIVERED
        elif self.received_quantity > 0:
            if (self.received_quantity + self.cancelled_quantity) >= self.ordered_quantity:
                self.delivery_status = DeliveryStatus.FULLY_DELIVERED
            else:
                self.delivery_status = DeliveryStatus.PARTIALLY_DELIVERED
        elif self.cancelled_quantity >= self.ordered_quantity:
            self.delivery_status = DeliveryStatus.CANCELLED
        else:
            self.delivery_status = DeliveryStatus.PENDING

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class ImportDuplicatePolicy(models.TextChoices):
    SKIP = "SKIP", "Skip duplicates"
    UPDATE_OVERWRITE = "UPDATE_OVERWRITE", "Update / Overwrite existing"
    FAIL_ON_DUPLICATE = "FAIL_ON_DUPLICATE", "Fail entire batch on duplicate"


class ImportSessionStatus(models.TextChoices):
    UPLOADED = "UPLOADED", "Uploaded & Parsed"
    MAPPED = "MAPPED", "Columns Mapped"
    VALIDATING = "VALIDATING", "Validating"
    VALIDATED = "VALIDATED", "Validated Pre-Flight"
    COMMITTING = "COMMITTING", "Committing"
    COMMITTED = "COMMITTED", "Committed Successfully"
    ROLLED_BACK = "ROLLED_BACK", "Rolled Back"
    FAILED = "FAILED", "Failed"


class MedicineImportSession(models.Model):
    """
    Architecture Decision: ADR-0132 (P132).
    Multi-stage bulk medicine import session governing CSV parsing, field mapping,
    pre-flight validation, duplicate detection, and rollback-safe commit.
    """

    session_id = models.CharField(max_length=64, primary_key=True)
    tenant_id = models.CharField(max_length=64, default="platform", db_index=True)
    status = models.CharField(
        max_length=32,
        choices=ImportSessionStatus.choices,
        default=ImportSessionStatus.UPLOADED,
        db_index=True,
    )
    file_name = models.CharField(max_length=255)
    file_format = models.CharField(max_length=16, default="CSV")
    total_rows = models.PositiveIntegerField(default=0)
    valid_rows = models.PositiveIntegerField(default=0)
    invalid_rows = models.PositiveIntegerField(default=0)
    duplicate_rows = models.PositiveIntegerField(default=0)
    duplicate_policy = models.CharField(
        max_length=32,
        choices=ImportDuplicatePolicy.choices,
        default=ImportDuplicatePolicy.SKIP,
    )
    raw_headers = models.JSONField(default=list, blank=True)
    preview_rows = models.JSONField(default=list, blank=True)
    raw_rows_data = models.JSONField(
        default=list, blank=True, help_text="Full parsed staging rows for mapping and commit"
    )
    field_mapping = models.JSONField(
        default=dict, blank=True, help_text="Mapping of raw headers to canonical catalog fields"
    )
    validation_errors = models.JSONField(default=list, blank=True)
    created_record_ids = models.JSONField(
        default=list, blank=True, help_text="Product IDs created by this import for rollback"
    )
    updated_record_ids = models.JSONField(
        default=list, blank=True, help_text="Product IDs updated by this import for rollback"
    )
    initiated_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="medicine_import_sessions",
    )
    committed_at = models.DateTimeField(null=True, blank=True)
    rolled_back_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Medicine Import Session"
        verbose_name_plural = "Medicine Import Sessions"
        indexes = [
            models.Index(fields=["tenant_id", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"ImportSession {self.session_id} [{self.status}] - {self.file_name} ({self.tenant_id})"


class MedicineReviewStatus(models.TextChoices):
    PENDING_REVIEW = "PENDING_REVIEW", "Pending Pharmacist Review"
    APPROVED = "APPROVED", "Approved & Published"
    REJECTED = "REJECTED", "Rejected"
    CHANGES_REQUESTED = "CHANGES_REQUESTED", "Changes Requested"


class ReviewChangeType(models.TextChoices):
    NEW_PRODUCT_DRAFT = "NEW_PRODUCT_DRAFT", "New Product Draft Publication"
    METADATA_UPDATE = "METADATA_UPDATE", "Clinical / Formulation Metadata Update"
    PRICE_REVISION = "PRICE_REVISION", "MRP / Wholesale Cost Price Revision"
    SAFETY_REASSESSMENT = "SAFETY_REASSESSMENT", "Regulatory Safety & Warning Reassessment"


class ProductMetadataReview(models.Model):
    """
    Architecture Decision: ADR-0133 (P133).
    Pharmacist review record for medicine metadata updates, draft approvals,
    price revisions, and regulatory publication.
    Enforces dual-control / four-eyes review principle and records diffs and audit hashes.
    """

    review_id = models.CharField(max_length=64, primary_key=True)
    product = models.ForeignKey(
        "core.MedicineProduct",
        on_delete=models.CASCADE,
        related_name="metadata_reviews",
        help_text="Target medicine product under review.",
    )
    change_type = models.CharField(
        max_length=32,
        choices=ReviewChangeType.choices,
        default=ReviewChangeType.METADATA_UPDATE,
        db_index=True,
    )
    status = models.CharField(
        max_length=32,
        choices=MedicineReviewStatus.choices,
        default=MedicineReviewStatus.PENDING_REVIEW,
        db_index=True,
    )
    proposed_diff = models.JSONField(
        default=dict,
        blank=True,
        help_text="Dictionary of field-by-field differences: {field: {old_value: ..., new_value: ...}}",
    )
    submitted_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_product_reviews",
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conducted_product_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(
        blank=True, default="", help_text="Justification, clinical rationale, or rejection reason"
    )
    submission_notes = models.TextField(blank=True, default="", help_text="Notes from the submitting editor")
    review_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Cryptographic SHA-256 audit signature of review decision and diff",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-submitted_at"]
        verbose_name = "Product Metadata Review"
        verbose_name_plural = "Product Metadata Reviews"
        indexes = [
            models.Index(fields=["product", "status"]),
            models.Index(fields=["status", "submitted_at"]),
        ]

    def compute_review_hash(self) -> str:
        import hashlib
        import json

        payload = (
            f"{self.product_id}|{self.change_type}|{self.status}|"
            f"{json.dumps(self.proposed_diff, sort_keys=True)}|"
            f"{self.submitted_by_id}|{self.reviewed_by_id}|{self.reviewed_at}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        if not self.review_hash:
            self.review_hash = self.compute_review_hash()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Review {self.review_id} [{self.status}] - {self.product_id} ({self.change_type})"


class RegulatorySourceType(models.TextChoices):
    DGDA_GAZETTE = "DGDA_GAZETTE", "DGDA Statutory Gazette Order"
    DGDA_CIRCULAR = "DGDA_CIRCULAR", "DGDA Public Alert / Circular"
    MANUFACTURER_MONOGRAPH = "MANUFACTURER_MONOGRAPH", "Manufacturer Product Monograph / SmPC"
    PHARMACOPOEIA = "PHARMACOPOEIA", "Official Pharmacopoeia (BP / USP / IP)"
    WHO_AWARE = "WHO_AWARE", "WHO AWaRe Antibiotic Classification"
    CLINICAL_GUIDELINE = "CLINICAL_GUIDELINE", "National Clinical Practice Guideline"


class RegulatoryRuleTarget(models.TextChoices):
    CLASSIFICATION = "CLASSIFICATION", "Controlled Dispensing Classification"
    STORAGE_CONDITION = "STORAGE_CONDITION", "Storage & Temperature Control"
    CLASSIFICATION_AND_STORAGE = "CLASSIFICATION_AND_STORAGE", "Unified Classification & Storage Rules"


class RegulatorySourceAttachment(models.Model):
    """
    Architecture Decision: ADR-0134 (P134).
    Attaches legal and manufacturer regulatory sources to classification and storage rules
    with explicit citations, temperature parameters, review dates, and immutable audit hashes.
    """

    attachment_id = models.CharField(max_length=64, primary_key=True)
    product = models.ForeignKey(
        "core.MedicineProduct",
        on_delete=models.CASCADE,
        related_name="regulatory_source_attachments",
        db_index=True,
    )
    source_type = models.CharField(
        max_length=64,
        choices=RegulatorySourceType.choices,
        default=RegulatorySourceType.DGDA_GAZETTE,
        db_index=True,
    )
    rule_target = models.CharField(
        max_length=64,
        choices=RegulatoryRuleTarget.choices,
        default=RegulatoryRuleTarget.CLASSIFICATION_AND_STORAGE,
        db_index=True,
    )
    document_reference_number = models.CharField(
        max_length=128,
        db_index=True,
        help_text="Official statutory gazette citation or manufacturer monograph code",
    )
    issuing_authority = models.CharField(
        max_length=255,
        db_index=True,
        help_text="e.g. DGDA, Square Pharmaceuticals, British Pharmacopoeia Commission",
    )
    publication_date = models.DateField(null=True, blank=True)
    effective_date = models.DateField(db_index=True)
    review_cycle_months = models.PositiveIntegerField(
        default=12,
        help_text="Periodic compliance re-review interval in months (e.g. 12, 24, 36)",
    )
    next_review_due_date = models.DateField(
        db_index=True,
        help_text="Formal review deadline for compliance audit",
    )
    source_url = models.URLField(max_length=512, blank=True, default="")
    storage_rule_specs = models.JSONField(
        default=dict,
        blank=True,
        help_text="Structured storage conditions: requires_cold_chain, min_temp, max_temp, protect_from_light",
    )
    classification_specs = models.JSONField(
        default=dict,
        blank=True,
        help_text="Controlled dispensing rules: classification_type, is_antibiotic, is_narcotic",
    )
    version_number = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    immutable_source_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Cryptographic SHA-256 fingerprint guaranteeing regulatory source immutability",
    )
    reviewed_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_regulatory_attachments",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product", "-version_number"]
        verbose_name = "Regulatory Source Attachment"
        verbose_name_plural = "Regulatory Source Attachments"
        indexes = [
            models.Index(fields=["product", "is_active"]),
            models.Index(fields=["document_reference_number"]),
            models.Index(fields=["issuing_authority"]),
            models.Index(fields=["next_review_due_date"]),
            models.Index(fields=["effective_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "document_reference_number", "version_number"],
                name="unique_product_source_doc_version",
            )
        ]

    def compute_source_hash(self) -> str:
        import hashlib

        eff_str = (
            self.effective_date.isoformat()
            if hasattr(self.effective_date, "isoformat")
            else str(self.effective_date or "")
        )
        raw = (
            f"{self.product_id}|{self.document_reference_number}|{self.version_number}|"
            f"{self.source_type}|{self.rule_target}|{eff_str}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        if not self.immutable_source_hash:
            self.immutable_source_hash = self.compute_source_hash()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.product_id} -> {self.document_reference_number} (v{self.version_number})"


class NonMedicineCategory(models.TextChoices):
    SURGICAL_WOUND_CARE = "SURGICAL_WOUND_CARE", "Surgical Supplies & Wound Care"
    DIAGNOSTIC_DEVICES = "DIAGNOSTIC_DEVICES", "Diagnostic Devices & Monitoring"
    PERSONAL_HYGIENE = "PERSONAL_HYGIENE", "Personal Hygiene & Sanitization"
    BABY_MOTHER_CARE = "BABY_MOTHER_CARE", "Baby & Maternal Care"
    NUTRITION_SUPPLEMENT = "NUTRITION_SUPPLEMENT", "Nutritional Drinks & Dietary Supplements"
    HEALTH_ACCESSORIES = "HEALTH_ACCESSORIES", "Health & Mobility Accessories"


class NonMedicineRetailItem(models.Model):
    """
    Architecture Decision: ADR-0135 (P135).
    Authorized Non-Medicine Retail Healthcare & Consumer Goods.
    Preserves strict domain boundary: is_medicine is fixed to False,
    exempt from prescription verification, DGDA DAR, narcotic, and antibiotic controls.
    """

    item_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="non_medicine_items",
        db_index=True,
    )
    tenant_id_str = models.CharField(
        max_length=64,
        default="platform",
        db_index=True,
        help_text="'platform' for global catalog; tenant_id for store-specific retail item",
    )
    name = models.CharField(max_length=255, db_index=True)
    name_bn = models.CharField(max_length=255, blank=True, default="", help_text="Bengali item name")
    category = models.CharField(
        max_length=64,
        choices=NonMedicineCategory.choices,
        default=NonMedicineCategory.SURGICAL_WOUND_CARE,
        db_index=True,
    )
    brand_name = models.CharField(max_length=255, blank=True, default="", db_index=True)
    manufacturer_or_distributor = models.CharField(max_length=255, blank=True, default="", db_index=True)
    barcode = models.CharField(max_length=64, blank=True, default="", db_index=True)
    sku = models.CharField(max_length=64, blank=True, default="", db_index=True)
    mrp = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Maximum Retail Price in BDT",
    )
    cost_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Standard purchasing cost in BDT",
    )
    vat_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
        help_text="Applicable retail VAT percentage",
    )
    unit_of_measure = models.CharField(max_length=32, blank=True, default="PIECE")
    requires_cold_chain = models.BooleanField(
        default=False,
        help_text="Flag for temperature-sensitive reagents or diagnostic test strips",
    )
    storage_instructions = models.TextField(blank=True, default="")
    is_medicine = models.BooleanField(default=False, editable=False)
    is_prescription_only = models.BooleanField(default=False, editable=False)
    is_antibiotic = models.BooleanField(default=False, editable=False)
    is_narcotic = models.BooleanField(default=False, editable=False)
    is_active = models.BooleanField(default=True, db_index=True)
    version_number = models.PositiveIntegerField(default=1)
    immutable_hash = models.CharField(max_length=64, blank=True, default="")
    created_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_non_medicine_items",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Non-Medicine Retail Item"
        verbose_name_plural = "Non-Medicine Retail Items"
        indexes = [
            models.Index(fields=["tenant_id_str", "category"]),
            models.Index(fields=["barcode"]),
            models.Index(fields=["brand_name"]),
            models.Index(fields=["is_active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant_id_str", "barcode"],
                condition=~models.Q(barcode=""),
                name="unique_tenant_non_medicine_barcode",
            )
        ]

    def compute_hash(self) -> str:
        import hashlib

        raw = (
            f"{self.item_id}|{self.tenant_id_str}|{self.category}|{self.barcode}|"
            f"{self.mrp}|{self.vat_percent}|{self.version_number}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def clean(self):
        super().clean()
        if self.is_medicine:
            raise ValidationError("NonMedicineRetailItem cannot have is_medicine = True.")
        if self.is_prescription_only or self.is_antibiotic or self.is_narcotic:
            raise ValidationError(
                "ERR_NON_MEDICINE_REGULATORY_RESTRICTION: Non-medicine items cannot be marked as prescription-only, antibiotic, or narcotic."
            )

    def save(self, *args, **kwargs):
        self.is_medicine = False
        self.is_prescription_only = False
        self.is_antibiotic = False
        self.is_narcotic = False
        if not self.immutable_hash:
            self.immutable_hash = self.compute_hash()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.name} [{self.category}] - BDT {self.mrp}"


class BarcodeIdentifierType(models.TextChoices):
    PRIMARY_EAN13_GTIN = "PRIMARY_EAN13_GTIN", "Primary Unit Barcode (EAN-13 / GTIN-13 / UPC-A)"
    SECONDARY_BOX_GTIN14 = "SECONDARY_BOX_GTIN14", "Secondary Outer Box / Carton (ITF-14 / GTIN-14)"
    INTERNAL_STORE_BARCODE = "INTERNAL_STORE_BARCODE", "Internal Store Custom Barcode / Shelf Sticker"
    GS1_DATAMATRIX_2D = "GS1_DATAMATRIX_2D", "GS1 2D DataMatrix (GTIN + Batch + Expiry + Serial)"
    NATIONAL_DAR_CODE = "NATIONAL_DAR_CODE", "National DGDA Registration DAR Barcode"
    SUPPLIER_CATALOG_CODE = "SUPPLIER_CATALOG_CODE", "Supplier Wholesale Catalog Code"


class BarcodePackagingLevel(models.TextChoices):
    UNIT = "UNIT", "Base Piece / Unit Dose"
    STRIP = "STRIP", "Blister Strip"
    BOX = "BOX", "Commercial Trade Box"
    SHIPPER_CARTON = "SHIPPER_CARTON", "Wholesale Shipper Case Carton"


class BarcodeAlternateIdentifier(models.Model):
    """
    Architecture Decision: ADR-0136 (P136).
    Primary, Secondary, and Alternate Barcode Identifiers across packaging tiers.
    Supports GS1-128, ITF-14 cartons, internal POS stickers, contextual uniqueness,
    and scan prioritization engines.
    """

    identifier_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="barcode_identifiers",
        db_index=True,
    )
    tenant_id_str = models.CharField(
        max_length=64,
        default="platform",
        db_index=True,
        help_text="'platform' for global catalog; tenant_id for store-specific barcode",
    )
    product = models.ForeignKey(
        "core.MedicineProduct",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="alternate_barcodes",
        db_index=True,
    )
    non_medicine_item = models.ForeignKey(
        "core.NonMedicineRetailItem",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="alternate_barcodes",
        db_index=True,
    )
    variant = models.ForeignKey(
        "core.MedicineVariant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="alternate_barcodes",
        db_index=True,
    )
    barcode_value = models.CharField(max_length=64, db_index=True)
    identifier_type = models.CharField(
        max_length=64,
        choices=BarcodeIdentifierType.choices,
        default=BarcodeIdentifierType.PRIMARY_EAN13_GTIN,
        db_index=True,
    )
    packaging_level = models.CharField(
        max_length=32,
        choices=BarcodePackagingLevel.choices,
        default=BarcodePackagingLevel.UNIT,
        db_index=True,
    )
    priority_order = models.PositiveIntegerField(
        default=1,
        db_index=True,
        help_text="Lookup resolution priority: 1 = Highest (Immediate POS resolution)",
    )
    pack_quantity_multiplier = models.PositiveIntegerField(
        default=1,
        help_text="Unit multiplier represented by this barcode (e.g. 1 for UNIT, 10 for STRIP, 100 for BOX)",
    )
    description = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    version_number = models.PositiveIntegerField(default=1)
    immutable_hash = models.CharField(max_length=64, blank=True, default="")
    created_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_barcode_identifiers",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority_order", "-version_number"]
        verbose_name = "Barcode Alternate Identifier"
        verbose_name_plural = "Barcode Alternate Identifiers"
        indexes = [
            models.Index(fields=["tenant_id_str", "is_active", "priority_order"]),
            models.Index(fields=["barcode_value", "is_active"]),
            models.Index(fields=["product", "is_active"]),
            models.Index(fields=["non_medicine_item", "is_active"]),
            models.Index(fields=["variant", "is_active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant_id_str", "barcode_value"],
                condition=models.Q(is_active=True),
                name="unique_active_tenant_barcode_value",
            )
        ]

    def clean(self):
        super().clean()
        targets = [self.product_id, self.non_medicine_item_id, self.variant_id]
        if not any(targets):
            raise ValidationError("Barcode identifier must link to a medicine product, retail item, or variant.")
        clean_barcode = str(self.barcode_value or "").strip()
        if len(clean_barcode) < 3:
            raise ValidationError("Barcode value must be at least 3 characters long.")

    def compute_hash(self) -> str:
        import hashlib

        raw = (
            f"{self.identifier_id}|{self.barcode_value}|{self.identifier_type}|"
            f"{self.packaging_level}|{self.priority_order}|{self.tenant_id_str}|{self.version_number}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        self.barcode_value = str(self.barcode_value or "").strip()
        if not self.immutable_hash:
            self.immutable_hash = self.compute_hash()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.barcode_value} [{self.identifier_type} - {self.packaging_level}]"


class CatalogueLifecycleStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active Normal In-Catalog"
    DISCONTINUED = "DISCONTINUED", "Manufacturer Discontinued"
    RECALLED = "RECALLED", "Statutory Safety Recall (Dispensing Blocked)"
    WITHDRAWN = "WITHDRAWN", "Permanent Market Withdrawal"
    QUARANTINED = "QUARANTINED", "Quarantined Pending Lab Analysis"
    LIFTED = "LIFTED", "Recall Lifted / Cleared"


class RecallScope(models.TextChoices):
    NATIONWIDE_ALL_BATCHES = "NATIONWIDE_ALL_BATCHES", "Nationwide All Batches"
    SPECIFIC_BATCHES = "SPECIFIC_BATCHES", "Specific Contaminated Batches"
    TENANT_QUARANTINE = "TENANT_QUARANTINE", "Tenant Store-Level Quarantine"


class CatalogueLifecycleNotice(models.Model):
    """
    Architecture Decision: ADR-0137 (P137).
    Statutory safety recall, market withdrawal, and discontinuation governance notice.
    Enforces hard dispensing blocks on recalled/withdrawn inventory at POS checkout
    while preserving permanent relational integrity with historical invoices and prescriptions.
    """

    notice_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="catalogue_lifecycle_notices",
    )
    tenant_id_str = models.CharField(max_length=64, default="platform", db_index=True)
    product = models.ForeignKey(
        "core.MedicineProduct",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="lifecycle_notices",
    )
    non_medicine_item = models.ForeignKey(
        "core.NonMedicineRetailItem",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="lifecycle_notices",
    )
    target_entity_type = models.CharField(
        max_length=32,
        choices=[("MEDICINE", "Medicine"), ("NON_MEDICINE", "Non-Medicine Retail Item")],
        default="MEDICINE",
    )
    lifecycle_status = models.CharField(
        max_length=32,
        choices=CatalogueLifecycleStatus.choices,
        default=CatalogueLifecycleStatus.RECALLED,
        db_index=True,
    )
    recall_scope = models.CharField(
        max_length=32,
        choices=RecallScope.choices,
        default=RecallScope.NATIONWIDE_ALL_BATCHES,
        db_index=True,
    )
    affected_batch_numbers = models.JSONField(default=list, blank=True)
    regulatory_authority = models.CharField(max_length=128)
    notice_number = models.CharField(max_length=128, db_index=True)
    reason = models.TextField()
    effective_date = models.DateTimeField(db_index=True)
    action_required = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    version_number = models.PositiveIntegerField(default=1)
    immutable_hash = models.CharField(max_length=64, blank=True, default="")
    issued_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issued_catalogue_notices",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-effective_date", "-version_number"]
        verbose_name = "Catalogue Lifecycle Notice"
        verbose_name_plural = "Catalogue Lifecycle Notices"
        indexes = [
            models.Index(fields=["tenant_id_str", "is_active", "lifecycle_status"]),
            models.Index(fields=["product", "is_active"]),
            models.Index(fields=["non_medicine_item", "is_active"]),
            models.Index(fields=["notice_number", "is_active"]),
            models.Index(fields=["effective_date", "is_active"]),
        ]

    @property
    def is_dispensing_blocked(self) -> bool:
        return self.is_active and self.lifecycle_status in (
            CatalogueLifecycleStatus.RECALLED,
            CatalogueLifecycleStatus.WITHDRAWN,
            CatalogueLifecycleStatus.QUARANTINED,
        )

    def compute_hash(self) -> str:
        import hashlib

        eff_str = (
            self.effective_date.isoformat()
            if hasattr(self.effective_date, "isoformat")
            else str(self.effective_date or "")
        )
        batches_str = ",".join(sorted([str(b) for b in (self.affected_batch_numbers or [])]))
        raw = (
            f"{self.notice_id}|{self.tenant_id_str}|{self.target_entity_type}|"
            f"{self.lifecycle_status}|{self.recall_scope}|{self.notice_number}|"
            f"{batches_str}|{eff_str}|{self.version_number}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def clean(self):
        super().clean()
        if not self.product_id and not self.non_medicine_item_id:
            raise ValidationError("Notice must link to either a medicine product or retail item.")
        if len(str(self.notice_number or "").strip()) < 3:
            raise ValidationError("Notice number must be at least 3 characters.")
        if len(str(self.reason or "").strip()) < 5:
            raise ValidationError("Reason must be at least 5 characters.")

    def save(self, *args, **kwargs):
        self.clean()
        if not self.immutable_hash:
            self.immutable_hash = self.compute_hash()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"[{self.lifecycle_status}] {self.notice_number} ({self.regulatory_authority})"


class MigrationStrategy(models.TextChoices):
    STRICT = "STRICT", "Strict Schema Match"
    COERCE_SAFE = "COERCE_SAFE", "Safe Type Coercion and Default Injection"
    DRY_RUN_ONLY = "DRY_RUN_ONLY", "Dry Run Only (No DB Writes)"


class MigrationPlanStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft Plan"
    VALIDATED = "VALIDATED", "Dry Run Validated"
    EXECUTING = "EXECUTING", "Migration Executing"
    COMPLETED = "COMPLETED", "Migration Completed"
    ROLLED_BACK = "ROLLED_BACK", "Migration Rolled Back"
    FAILED = "FAILED", "Migration Failed"


class FormTemplateMigrationPlan(models.Model):
    """
    Architecture Decision: ADR-0139 (P139).
    Form Template Migration Plan for evolving dosage form templates safely,
    providing declarative field transformations, non-destructive dry-run validation,
    100% reversible rollbacks, and strictly preserving immutable historical dispensing records.
    """

    plan_id = models.CharField(max_length=64, primary_key=True)
    plan_name = models.CharField(max_length=255)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="form_template_migration_plans",
        help_text="Null denotes platform-wide global catalog migration; populated for tenant-scoped plans.",
    )
    from_template = models.ForeignKey(
        "core.MedicineFormTemplate",
        on_delete=models.PROTECT,
        related_name="source_migration_plans",
        help_text="Source dosage-form template.",
    )
    to_template = models.ForeignKey(
        "core.MedicineFormTemplate",
        on_delete=models.PROTECT,
        related_name="target_migration_plans",
        help_text="Target dosage-form template.",
    )
    strategy = models.CharField(
        max_length=32,
        choices=MigrationStrategy.choices,
        default=MigrationStrategy.COERCE_SAFE,
        db_index=True,
    )
    status = models.CharField(
        max_length=32,
        choices=MigrationPlanStatus.choices,
        default=MigrationPlanStatus.DRAFT,
        db_index=True,
    )
    field_mappings = models.JSONField(
        default=dict,
        blank=True,
        help_text="Declarative mappings: field_renames, default_values, value_transforms, deprecated_fields.",
    )
    dry_run_summary = models.JSONField(
        default=dict,
        blank=True,
        help_text="Dry-run audit summary: affected medicine count, validation warnings, and preview diff.",
    )
    affected_medicine_count = models.PositiveIntegerField(default=0)
    migrated_medicine_count = models.PositiveIntegerField(default=0)
    historical_dispensing_audited_count = models.PositiveIntegerField(
        default=0,
        help_text="Count of historical SaleInvoiceItem and PrescriptionItem records verified immutable.",
    )
    rollback_snapshot_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Frozen before-state snapshot of updated medicine products for deterministic rollback.",
    )
    plan_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Cryptographic SHA-256 hash ensuring plan integrity and audit provenance.",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_form_template_plans",
    )
    executed_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="executed_form_template_plans",
    )
    executed_at = models.DateTimeField(null=True, blank=True)
    rolled_back_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Form Template Migration Plan"
        verbose_name_plural = "Form Template Migration Plans"
        indexes = [
            models.Index(fields=["tenant", "status", "-created_at"]),
            models.Index(fields=["from_template", "to_template"]),
            models.Index(fields=["status", "is_active"]),
        ]

    def compute_plan_hash(self) -> str:
        import hashlib
        import json

        mappings_str = json.dumps(self.field_mappings or {}, sort_keys=True)
        tenant_str = str(self.tenant_id or "platform")
        from_id = str(self.from_template_id or "")
        to_id = str(self.to_template_id or "")
        raw = f"{self.plan_id}|{tenant_str}|{from_id}|{to_id}|{self.strategy}|{mappings_str}|{self.status}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def clean(self):
        super().clean()
        if not self.from_template_id or not self.to_template_id:
            raise ValidationError("Both from_template and to_template must be specified.")
        if self.from_template_id == self.to_template_id:
            raise ValidationError("Target template cannot be identical to source template.")

    def save(self, *args, **kwargs):
        self.clean()
        if not self.plan_hash or "update_fields" not in kwargs:
            self.plan_hash = self.compute_plan_hash()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        t_scope = f"Tenant: {self.tenant_id}" if self.tenant_id else "Global"
        return f"[{self.status}] {self.plan_name} ({t_scope}) - {self.from_template_id} -> {self.to_template_id}"


# ============================================================================
# P141: Tablet & Capsule Dosage-Form Editor Specification
# Architecture Decision: ADR-0141 (Phase 08: Milestone M04 / M05 transition)
# ============================================================================


class SolidDosageFormType(models.TextChoices):
    TABLET = "TABLET", "Standard Tablet"
    CAPSULE = "CAPSULE", "Capsule"
    CHEWABLE_TABLET = "CHEWABLE_TABLET", "Chewable Tablet"
    EFFERVESCENT_TABLET = "EFFERVESCENT_TABLET", "Effervescent Tablet"
    EXTENDED_RELEASE_TABLET = "EXTENDED_RELEASE_TABLET", "Extended Release Tablet"
    EXTENDED_RELEASE_CAPSULE = "EXTENDED_RELEASE_CAPSULE", "Extended Release Capsule"
    SUBLINGUAL_TABLET = "SUBLINGUAL_TABLET", "Sublingual Tablet"


class SolidStrengthUnit(models.TextChoices):
    MG = "mg", "Milligram (mg)"
    MCG = "mcg", "Microgram (mcg)"
    G = "g", "Gram (g)"


class TabletRoute(models.TextChoices):
    ORAL = "Oral", "Oral"
    SUBLINGUAL = "Sublingual", "Sublingual"
    BUCCAL = "Buccal", "Buccal"
    CHEWABLE = "Chewable", "Chewable"
    DISPERSIBLE = "Dispersible", "Dispersible"
    EFFERVESCENT = "Effervescent", "Effervescent"


class SolidStoragePolicy(models.TextChoices):
    ROOM_TEMPERATURE_DRY = "ROOM_TEMPERATURE_DRY", "Room Temperature & Dry (<30°C)"
    PROTECT_FROM_MOISTURE_LIGHT = "PROTECT_FROM_MOISTURE_LIGHT", "Protect From Moisture & Light (<25°C)"
    COOL_AND_DRY = "COOL_AND_DRY", "Cool & Dry (15°C - 25°C)"
    REFRIGERATED = "REFRIGERATED", "Refrigerated (2°C - 8°C)"


class TabletCoatingType(models.TextChoices):
    FILM_COATED = "FILM_COATED", "Film Coated"
    ENTERIC_COATED = "ENTERIC_COATED", "Enteric Coated"
    UNCOATED = "UNCOATED", "Uncoated"
    SUGAR_COATED = "SUGAR_COATED", "Sugar Coated"
    GELATIN_COATED = "GELATIN_COATED", "Gelatin Coated"


class TabletCapsuleFormConfig(models.Model):
    """
    Structured dosage-form configuration for solid oral tablets and capsules.
    Manages packaging conversion multipliers (box -> strip -> unit), dimension
    consistency, loose-sale eligibility, and administration routes.
    """

    config_id = models.CharField(max_length=64, primary_key=True)
    product = models.ForeignKey(
        "core.MedicineProduct",
        on_delete=models.CASCADE,
        related_name="tablet_capsule_configs",
    )
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tablet_capsule_configs",
    )
    dosage_form = models.CharField(
        max_length=32,
        choices=SolidDosageFormType.choices,
        default=SolidDosageFormType.TABLET,
    )
    dose_strength = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        help_text="Numeric active ingredient dose strength per unit (e.g. 500.0000 mg)",
    )
    strength_unit = models.CharField(
        max_length=16,
        choices=SolidStrengthUnit.choices,
        default=SolidStrengthUnit.MG,
    )
    tablets_per_strip = models.PositiveIntegerField(
        default=10,
        help_text="Base tablet or capsule units sealed per primary blister/foil strip",
    )
    strips_per_box = models.PositiveIntegerField(
        default=10,
        help_text="Number of secondary blister/foil strips packed inside master commercial box",
    )
    box_size = models.PositiveIntegerField(
        help_text="Total base units per master commercial box (must equal tablets_per_strip * strips_per_box)",
    )
    loose_sale_eligible = models.BooleanField(
        default=True,
        help_text="Whether individual tablets/capsules can be cut from strips and sold loose at POS",
    )
    route = models.CharField(
        max_length=32,
        choices=TabletRoute.choices,
        default=TabletRoute.ORAL,
    )
    storage_policy = models.CharField(
        max_length=32,
        choices=SolidStoragePolicy.choices,
        default=SolidStoragePolicy.ROOM_TEMPERATURE_DRY,
    )
    is_scored = models.BooleanField(
        default=False,
        help_text="Whether tablet has scored scoreline for splitting halves/quarters",
    )
    coating_type = models.CharField(
        max_length=32,
        choices=TabletCoatingType.choices,
        default=TabletCoatingType.FILM_COATED,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Tablet/Capsule Form Config"
        verbose_name_plural = "Tablet/Capsule Form Configs"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "tenant"],
                name="unique_tablet_capsule_product_tenant",
            ),
            models.CheckConstraint(
                condition=models.Q(tablets_per_strip__gte=1),
                name="check_tablet_units_per_strip_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(strips_per_box__gte=1),
                name="check_tablet_strips_per_box_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(box_size__gte=1),
                name="check_tablet_box_size_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(dose_strength__gt=0),
                name="check_tablet_dose_strength_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "dosage_form", "is_active"]),
            models.Index(fields=["product", "loose_sale_eligible"]),
        ]

    def clean(self):
        super().clean()
        expected_box_size = self.tablets_per_strip * self.strips_per_box
        if not self.box_size:
            self.box_size = expected_box_size
        elif self.box_size != expected_box_size:
            raise ValidationError(
                f"Dimension consistency mismatch: box_size ({self.box_size}) must equal "
                f"tablets_per_strip ({self.tablets_per_strip}) * strips_per_box ({self.strips_per_box}) = {expected_box_size}."
            )

        # Loose-sale business rule
        if self.dosage_form == SolidDosageFormType.EFFERVESCENT_TABLET and self.loose_sale_eligible:
            raise ValidationError(
                "Loose sale is not permitted for effervescent tablets due to rapid atmospheric moisture degradation."
            )
        if self.storage_policy == SolidStoragePolicy.PROTECT_FROM_MOISTURE_LIGHT and self.loose_sale_eligible and self.dosage_form == SolidDosageFormType.EFFERVESCENT_TABLET:
            raise ValidationError(
                "Loose sale is strictly prohibited for moisture-sensitive formulations."
            )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        t_scope = f"Tenant: {self.tenant_id}" if self.tenant_id else "Global"
        return f"{self.product_id} ({self.dosage_form}) - {self.dose_strength}{self.strength_unit} [{self.box_size}s/{self.strips_per_box}x{self.tablets_per_strip}] ({t_scope})"


# ============================================================================
# P142: Syrup & Liquid Oral Dosage-Form Editor Specification
# Architecture Decision: ADR-0142 (Phase 08: Milestone M04 / M05)
# ============================================================================


class LiquidDosageFormType(models.TextChoices):
    SYRUP = "SYRUP", "Syrup"
    SUSPENSION = "SUSPENSION", "Suspension"
    ORAL_SOLUTION = "ORAL_SOLUTION", "Oral Solution"
    ELIXIR = "ELIXIR", "Elixir"
    ORAL_EMULSION = "ORAL_EMULSION", "Oral Emulsion"
    ORAL_DROPS = "ORAL_DROPS", "Oral Drops"
    DRY_SYRUP_RECONSTITUTED = "DRY_SYRUP_RECONSTITUTED", "Dry Syrup / Powder for Suspension"


class LiquidConcentrationUnit(models.TextChoices):
    MG_PER_5ML = "mg/5ml", "mg/5mL"
    MG_PER_ML = "mg/ml", "mg/mL"
    PERCENT_W_V = "%", "% (w/v)"
    G_PER_100ML = "g/100ml", "g/100mL"
    MCG_PER_5ML = "mcg/5ml", "mcg/5mL"
    IU_PER_5ML = "iu/5ml", "IU/5mL"
    MG_PER_10ML = "mg/10ml", "mg/10mL"


class LiquidRoute(models.TextChoices):
    ORAL = "Oral", "Oral"
    ORAL_GAVAGE = "Oral Gavage", "Oral Gavage"
    SUBLINGUAL_DROPS = "Sublingual Drops", "Sublingual Drops"


class LiquidStoragePolicy(models.TextChoices):
    ROOM_TEMPERATURE_BELOW_30C = "ROOM_TEMPERATURE_BELOW_30C", "Room Temperature (Below 30°C)"
    COOL_PLACE_15_25C = "COOL_PLACE_15_25C", "Cool Place (15°C - 25°C)"
    DO_NOT_FREEZE = "DO_NOT_FREEZE", "Do Not Freeze"
    REFRIGERATE_AFTER_RECONSTITUTION_2_8C = "REFRIGERATE_AFTER_RECONSTITUTION_2_8C", "Refrigerate After Reconstitution (2°C - 8°C)"
    PROTECT_FROM_DIRECT_SUNLIGHT = "PROTECT_FROM_DIRECT_SUNLIGHT", "Protect From Direct Sunlight"


class LiquidMeasuringDevice(models.TextChoices):
    MEASURING_CUP = "MEASURING_CUP", "Measuring Cup (5-15 mL)"
    MEASURING_SPOON = "MEASURING_SPOON", "Measuring Spoon (5 mL)"
    CALIBRATED_DROPPER = "CALIBRATED_DROPPER", "Calibrated Dropper (0.1 - 1.0 mL)"
    ORAL_SYRINGE = "ORAL_SYRINGE", "Oral Dosing Syringe"
    NONE = "NONE", "None / Standard Teaspoon"


class LiquidSaleUnit(models.TextChoices):
    BOTTLE = "Bottle", "Bottle"
    PACK = "Pack", "Pack"
    PHIAL = "Phial", "Phial"


class SyrupLiquidFormConfig(models.Model):
    """
    Structured dosage-form configuration for syrups and oral liquid formulations.
    Governs concentration dimensions, nominal bottle volume, oral routes,
    sale units (Bottle/Pack), and reconstitution specifications.
    """

    config_id = models.CharField(max_length=64, primary_key=True)
    product = models.ForeignKey(
        "core.MedicineProduct",
        on_delete=models.CASCADE,
        related_name="syrup_liquid_configs",
    )
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="syrup_liquid_configs",
    )
    dosage_form = models.CharField(
        max_length=32,
        choices=LiquidDosageFormType.choices,
        default=LiquidDosageFormType.SYRUP,
    )
    concentration_value = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        help_text="Numeric active concentration value (e.g. 120.0000 mg)",
    )
    concentration_unit = models.CharField(
        max_length=16,
        choices=LiquidConcentrationUnit.choices,
        default=LiquidConcentrationUnit.MG_PER_5ML,
    )
    bottle_volume_ml = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        help_text="Total nominal bottle volume in milliliters (e.g. 100.00 mL)",
    )
    sale_unit = models.CharField(
        max_length=16,
        choices=LiquidSaleUnit.choices,
        default=LiquidSaleUnit.BOTTLE,
    )
    route = models.CharField(
        max_length=32,
        choices=LiquidRoute.choices,
        default=LiquidRoute.ORAL,
    )
    storage_policy = models.CharField(
        max_length=48,
        choices=LiquidStoragePolicy.choices,
        default=LiquidStoragePolicy.ROOM_TEMPERATURE_BELOW_30C,
    )
    measuring_device = models.CharField(
        max_length=32,
        choices=LiquidMeasuringDevice.choices,
        default=LiquidMeasuringDevice.MEASURING_CUP,
    )
    is_reconstitution_required = models.BooleanField(
        default=False,
        help_text="Whether powder/dry granules require reconstitution with sterile/boiled water before dispensing",
    )
    reconstitution_water_ml = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Volume of water in mL required to reconstitute to target mark",
    )
    shelf_life_days_after_reconstitution = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Permitted shelf life in days once reconstituted (e.g. 7 or 14 days)",
    )
    flavor = models.CharField(max_length=64, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Syrup/Liquid Form Config"
        verbose_name_plural = "Syrup/Liquid Form Configs"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "tenant"],
                name="unique_syrup_liquid_product_tenant",
            ),
            models.CheckConstraint(
                condition=models.Q(concentration_value__gt=0),
                name="check_syrup_concentration_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(bottle_volume_ml__gt=0),
                name="check_syrup_volume_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "dosage_form", "is_active"]),
            models.Index(fields=["product", "sale_unit"]),
        ]

    def clean(self):
        super().clean()
        # 1. Concentration unit validation
        allowed_units = [u.value for u in LiquidConcentrationUnit]
        if self.concentration_unit not in allowed_units:
            raise ValidationError(
                f"Invalid liquid concentration unit '{self.concentration_unit}'. Allowed: {allowed_units}."
            )

        # 2. Reconstitution validation
        if self.is_reconstitution_required or self.dosage_form == LiquidDosageFormType.DRY_SYRUP_RECONSTITUTED:
            if not self.reconstitution_water_ml or self.reconstitution_water_ml <= Decimal("0"):
                raise ValidationError(
                    "Reconstitution water volume in mL is mandatory for dry syrups and powders for suspension."
                )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        t_scope = f"Tenant: {self.tenant_id}" if self.tenant_id else "Global"
        return f"{self.product_id} ({self.dosage_form}) - {self.concentration_value}{self.concentration_unit} [{self.bottle_volume_ml}mL {self.sale_unit}] ({t_scope})"


# ============================================================================
# P143: Suspension Reconstitution & Beyond-Use Date (BUD) Editor Specification
# Architecture Decision: ADR-0143 (Phase 08: Milestone M04 / M05)
# ============================================================================

class ReconstitutionDosageFormType(models.TextChoices):
    SUSPENSION = "SUSPENSION", "Suspension"
    DRY_SYRUP_RECONSTITUTED = "DRY_SYRUP_RECONSTITUTED", "Dry Syrup / Powder for Suspension"
    ORAL_POWDER_SUSPENSION = "ORAL_POWDER_SUSPENSION", "Oral Powder for Suspension"


class ReconstitutionDiluentType(models.TextChoices):
    BOILED_COOLED_WATER = "BOILED_COOLED_WATER", "Boiled and Cooled Water"
    STERILE_WATER_FOR_INJECTION = "STERILE_WATER_FOR_INJECTION", "Sterile Water for Injection"
    PURIFIED_DRINKING_WATER = "PURIFIED_DRINKING_WATER", "Purified Drinking Water"
    NORMAL_SALINE_0_9 = "NORMAL_SALINE_0_9", "Normal Saline (0.9% NaCl)"


class ReconstitutionStoragePolicy(models.TextChoices):
    REFRIGERATE_2_8C_AFTER_RECONSTITUTION = "REFRIGERATE_2_8C_AFTER_RECONSTITUTION", "Refrigerate (2°C - 8°C) After Reconstitution"
    ROOM_TEMPERATURE_BELOW_25C = "ROOM_TEMPERATURE_BELOW_25C", "Room Temperature (Below 25°C)"
    COOL_DRY_PLACE_BELOW_30C = "COOL_DRY_PLACE_BELOW_30C", "Cool, Dry Place (Below 30°C)"
    DO_NOT_FREEZE = "DO_NOT_FREEZE", "Do Not Freeze"


class SuspensionReconstitutionConfig(models.Model):
    """
    Structured dosage-form configuration for dry powder suspension reconstitution.
    Governs diluent type, water volume, final reconstituted volume, multi-step instructions,
    stability shelf life, beyond-use dates (BUD), and licensed pharmacist review.
    """

    config_id = models.CharField(max_length=64, primary_key=True)
    product = models.ForeignKey(
        "core.MedicineProduct",
        on_delete=models.CASCADE,
        related_name="suspension_reconstitution_configs",
    )
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="suspension_reconstitution_configs",
    )
    dosage_form = models.CharField(
        max_length=32,
        choices=ReconstitutionDosageFormType.choices,
        default=ReconstitutionDosageFormType.DRY_SYRUP_RECONSTITUTED,
    )
    reconstitution_diluent = models.CharField(
        max_length=32,
        choices=ReconstitutionDiluentType.choices,
        default=ReconstitutionDiluentType.BOILED_COOLED_WATER,
    )
    water_volume_ml = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        help_text="Volume of diluent water in mL required for reconstitution",
    )
    final_volume_ml = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        help_text="Total final volume in mL of reconstituted suspension",
    )
    instructions_en = models.TextField(
        help_text="Standard English reconstitution and dispensing instructions"
    )
    instructions_bn = models.TextField(
        blank=True,
        default="",
        help_text="Standard Bengali reconstitution and dispensing instructions"
    )
    shelf_life_days = models.PositiveIntegerField(
        default=7,
        help_text="Permitted stability period in days after reconstitution (e.g. 7 or 14 days)",
    )
    opening_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date when water was added / container was opened",
    )
    beyond_use_date = models.DateField(
        null=True,
        blank=True,
        help_text="Calculated or specified beyond-use date after reconstitution",
    )
    storage_policy = models.CharField(
        max_length=48,
        choices=ReconstitutionStoragePolicy.choices,
        default=ReconstitutionStoragePolicy.REFRIGERATE_2_8C_AFTER_RECONSTITUTION,
    )
    handling_guidance_en = models.TextField(
        blank=True,
        default="",
        help_text="Handling and patient counseling guidance in English",
    )
    handling_guidance_bn = models.TextField(
        blank=True,
        default="",
        help_text="Handling and patient counseling guidance in Bengali",
    )
    is_pharmacist_reviewed = models.BooleanField(
        default=False,
        help_text="Whether this reconstitution protocol and counseling note has been verified by a licensed pharmacist",
    )
    reviewed_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="suspension_reconstitution_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewer_notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Suspension Reconstitution Config"
        verbose_name_plural = "Suspension Reconstitution Configs"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "tenant"],
                name="unique_suspension_reconstitution_product_tenant",
            ),
            models.CheckConstraint(
                condition=models.Q(water_volume_ml__gt=0),
                name="check_suspension_water_volume_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(final_volume_ml__gt=0),
                name="check_suspension_final_volume_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "dosage_form", "is_active"]),
            models.Index(fields=["product", "is_pharmacist_reviewed"]),
        ]

    def clean(self):
        super().clean()
        # 1. Dimension Consistency: final_volume_ml >= water_volume_ml
        if self.final_volume_ml and self.water_volume_ml:
            if self.final_volume_ml < self.water_volume_ml:
                raise ValidationError(
                    f"Dimension consistency mismatch: final_volume_ml ({self.final_volume_ml}) "
                    f"cannot be less than water_volume_ml ({self.water_volume_ml})."
                )

        # 2. BUD Calculation / Validation
        if self.opening_date:
            max_bud = self.opening_date + timedelta(days=self.shelf_life_days)
            if self.beyond_use_date is None:
                self.beyond_use_date = max_bud
            else:
                if self.beyond_use_date < self.opening_date:
                    raise ValidationError(
                        f"Beyond-use date ({self.beyond_use_date}) cannot be prior to opening date ({self.opening_date})."
                    )
                if self.beyond_use_date > max_bud:
                    raise ValidationError(
                        f"Beyond-use date ({self.beyond_use_date}) exceeds maximum shelf life "
                        f"({self.shelf_life_days} days from {self.opening_date} = {max_bud})."
                    )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        t_scope = f"Tenant: {self.tenant_id}" if self.tenant_id else "Global"
        rev_status = "Reviewed" if self.is_pharmacist_reviewed else "Unreviewed"
        return f"{self.product_id} ({self.dosage_form}) - {self.water_volume_ml}mL -> {self.final_volume_ml}mL [{self.reconstitution_diluent}] [{rev_status}] ({t_scope})"


# ============================================================================
# P144: Injection Vial, Ampoule, Sterile Route & Cold-Chain Editor Specification
# Architecture Decision: ADR-0144 (Phase 08: Milestone M04 / M05)
# ============================================================================

class InjectionDosageFormType(models.TextChoices):
    INJECTION_SOLUTION = "INJECTION_SOLUTION", "Injection Solution"
    INJECTION_SUSPENSION = "INJECTION_SUSPENSION", "Injection Suspension"
    INJECTION_POWDER_LYOPHILIZED = "INJECTION_POWDER_LYOPHILIZED", "Injection Powder (Lyophilized)"
    INFUSION_IV = "INFUSION_IV", "Intravenous Infusion"


class InjectionContainerType(models.TextChoices):
    VIAL_SINGLE_DOSE = "VIAL_SINGLE_DOSE", "Single-Dose Vial"
    VIAL_MULTI_DOSE = "VIAL_MULTI_DOSE", "Multi-Dose Vial"
    AMPOULE_GLASS = "AMPOULE_GLASS", "Glass Ampoule"
    PREFILLED_SYRINGE = "PREFILLED_SYRINGE", "Pre-filled Syringe"
    INFUSION_BOTTLE = "INFUSION_BOTTLE", "Infusion Bottle"
    CARTRIDGE = "CARTRIDGE", "Cartridge"


class InjectionSterileRoute(models.TextChoices):
    IV = "IV", "Intravenous (IV)"
    IM = "IM", "Intramuscular (IM)"
    SC = "SC", "Subcutaneous (SC)"
    ID = "ID", "Intradermal (ID)"
    INTRAVITREAL = "INTRAVITREAL", "Intravitreal"
    EPIDURAL = "EPIDURAL", "Epidural"


class InjectionStrengthUnit(models.TextChoices):
    MG_PER_ML = "mg/ml", "mg/mL"
    MCG_PER_ML = "mcg/ml", "mcg/mL"
    G_PER_ML = "g/ml", "g/mL"
    IU_PER_ML = "IU/ml", "IU/mL"
    MG_PER_VIAL = "mg/vial", "mg/vial"
    G_PER_VIAL = "g/vial", "g/vial"
    PERCENT = "%", "% (w/v)"


class InjectionDiluentType(models.TextChoices):
    WATER_FOR_INJECTION = "WATER_FOR_INJECTION", "Sterile Water for Injection"
    NORMAL_SALINE_0_9 = "NORMAL_SALINE_0_9", "Normal Saline (0.9% NaCl)"
    LIDOCAINE_1_PERCENT = "LIDOCAINE_1_PERCENT", "Lidocaine HCl 1%"
    DEXTROSE_5_PERCENT = "DEXTROSE_5_PERCENT", "Dextrose 5% in Water"
    NONE = "NONE", "None"


class InjectionVialFormConfig(models.Model):
    """
    Structured dosage-form configuration for injectable medications (vials, ampoules, infusions).
    Governs active ingredient strength, fill volume, sterile route of administration,
    single-dose vs. multi-dose presentation, diluent requirements, and cold-chain temperature thresholds.
    """

    config_id = models.CharField(max_length=64, primary_key=True)
    product = models.ForeignKey(
        "core.MedicineProduct",
        on_delete=models.CASCADE,
        related_name="injection_vial_configs",
    )
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="injection_vial_configs",
    )
    dosage_form = models.CharField(
        max_length=32,
        choices=InjectionDosageFormType.choices,
        default=InjectionDosageFormType.INJECTION_SOLUTION,
    )
    container_type = models.CharField(
        max_length=32,
        choices=InjectionContainerType.choices,
        default=InjectionContainerType.VIAL_SINGLE_DOSE,
    )
    strength_value = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        help_text="Numeric active ingredient strength per unit volume or per container",
    )
    strength_unit = models.CharField(
        max_length=16,
        choices=InjectionStrengthUnit.choices,
        default=InjectionStrengthUnit.MG_PER_ML,
    )
    fill_volume_ml = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        help_text="Nominal extractable or label fill volume in mL (e.g. 1.00 mL, 2.00 mL, 10.00 mL)",
    )
    sterile_route = models.CharField(
        max_length=32,
        choices=InjectionSterileRoute.choices,
        default=InjectionSterileRoute.IV,
    )
    is_cold_chain_required = models.BooleanField(
        default=False,
        help_text="Whether parenteral formulation requires uninterrupted 2°C - 8°C cold-chain storage",
    )
    min_storage_temp_celsius = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Minimum allowed storage temperature in Celsius (e.g. 2.00)",
    )
    max_storage_temp_celsius = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Maximum allowed storage temperature in Celsius (e.g. 8.00)",
    )
    has_preservative = models.BooleanField(
        default=False,
        help_text="Whether formulation contains antimicrobial preservative (multi-dose vials only)",
    )
    requires_diluent = models.BooleanField(
        default=False,
        help_text="Whether powder/lyophilized cake requires diluent reconstitution prior to administration",
    )
    diluent_type = models.CharField(
        max_length=32,
        choices=InjectionDiluentType.choices,
        default=InjectionDiluentType.NONE,
    )
    diluent_volume_ml = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Volume of diluent in mL required for reconstitution",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Injection Vial Form Config"
        verbose_name_plural = "Injection Vial Form Configs"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "tenant"],
                name="unique_injection_vial_product_tenant",
            ),
            models.CheckConstraint(
                condition=models.Q(strength_value__gt=0),
                name="check_injection_strength_value_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(fill_volume_ml__gt=0),
                name="check_injection_fill_volume_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "dosage_form", "is_active"]),
            models.Index(fields=["product", "is_cold_chain_required"]),
        ]

    def clean(self):
        super().clean()
        # 1. Cold chain consistency
        if self.is_cold_chain_required:
            if self.min_storage_temp_celsius is None or self.max_storage_temp_celsius is None:
                raise ValidationError(
                    "Cold-chain formulation requires both min_storage_temp_celsius and max_storage_temp_celsius."
                )
            if self.min_storage_temp_celsius >= self.max_storage_temp_celsius:
                raise ValidationError(
                    f"Invalid temperature range: min_storage_temp_celsius ({self.min_storage_temp_celsius}°C) "
                    f"must be strictly less than max_storage_temp_celsius ({self.max_storage_temp_celsius}°C)."
                )

        # 2. Single-dose ampoules cannot have preservatives
        if self.container_type == InjectionContainerType.AMPOULE_GLASS and self.has_preservative:
            raise ValidationError(
                "Single-dose glass ampoules cannot contain antimicrobial preservatives."
            )

        # 3. Diluent requirements
        if self.requires_diluent or self.dosage_form == InjectionDosageFormType.INJECTION_POWDER_LYOPHILIZED:
            if not self.diluent_volume_ml or self.diluent_volume_ml <= Decimal("0"):
                raise ValidationError(
                    "Lyophilized powder injections require positive diluent volume in mL."
                )
            if self.diluent_type == InjectionDiluentType.NONE:
                raise ValidationError(
                    "Lyophilized powder injections require a valid diluent type."
                )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        t_scope = f"Tenant: {self.tenant_id}" if self.tenant_id else "Global"
        cold_tag = " [COLD-CHAIN]" if self.is_cold_chain_required else ""
        return f"{self.product_id} ({self.container_type}) - {self.strength_value}{self.strength_unit} / {self.fill_volume_ml}mL [{self.sterile_route}]{cold_tag} ({t_scope})"


# ============================================================================
# P145: Insulin Formulation, Container Dimensions & Excursion Governance
# Architecture Decision: ADR-0145 (Phase 08: Milestone M04 / M05)
# ============================================================================

class InsulinActionCategory(models.TextChoices):
    RAPID_ACTING = "RAPID_ACTING", "Rapid-Acting (Aspart / Lispro / Glulisine)"
    SHORT_ACTING_REGULAR = "SHORT_ACTING_REGULAR", "Short-Acting Regular (Crystalline Human)"
    INTERMEDIATE_NPH = "INTERMEDIATE_NPH", "Intermediate-Acting NPH (Isophane)"
    LONG_ACTING_ANALOG = "LONG_ACTING_ANALOG", "Long-Acting Analog (Glargine / Detemir / Degludec)"
    PREMIXED_BIPHASIC = "PREMIXED_BIPHASIC", "Premixed Biphasic (70/30, 50/50)"


class InsulinDeliveryDevice(models.TextChoices):
    PEN_DISPOSABLE = "PEN_DISPOSABLE", "Pre-filled Disposable Pen (3 mL)"
    CARTRIDGE_REFILL = "CARTRIDGE_REFILL", "Pen Cartridge Refill (3 mL)"
    VIAL_MULTI_DOSE = "VIAL_MULTI_DOSE", "Multi-Dose Vial (10 mL)"
    PUMP_RESERVOIR = "PUMP_RESERVOIR", "Insulin Pump Cartridge / Reservoir"


class InsulinExcursionAction(models.TextChoices):
    QUARANTINE_FOR_PHARMACIST_ASSESSMENT = "QUARANTINE_FOR_PHARMACIST_ASSESSMENT", "Quarantine for Pharmacist Assessment"
    DISPOSE_IF_FROZEN = "DISPOSE_IF_FROZEN", "Immediate Disposal If Frozen (<0°C)"
    ACCELERATE_EXPIRY_TO_IN_USE = "ACCELERATE_EXPIRY_TO_IN_USE", "Accelerate Expiry to In-Use Limit (28 Days)"
    RETURN_TO_SUPPLIER = "RETURN_TO_SUPPLIER", "Formal Return to Cold-Chain Supplier"


class InsulinFormConfig(models.Model):
    """
    Structured dosage-form configuration for insulin preparations.
    Governs concentration (IU/mL), container volume (mL), total device units (IU),
    unopened cold-chain storage (2°C - 8°C), in-use room-temperature stability (14 - 56 days),
    and cold-chain temperature excursion protocols.
    """

    config_id = models.CharField(max_length=64, primary_key=True)
    product = models.ForeignKey(
        "core.MedicineProduct",
        on_delete=models.CASCADE,
        related_name="insulin_configs",
    )
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="insulin_configs",
    )
    action_category = models.CharField(
        max_length=32,
        choices=InsulinActionCategory.choices,
        default=InsulinActionCategory.INTERMEDIATE_NPH,
    )
    delivery_device = models.CharField(
        max_length=32,
        choices=InsulinDeliveryDevice.choices,
        default=InsulinDeliveryDevice.PEN_DISPOSABLE,
    )
    concentration_iu_per_ml = models.PositiveIntegerField(
        default=100,
        help_text="Standardized insulin concentration in IU/mL (e.g. 100, 200, 300, 500)",
    )
    volume_ml = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("3.00"),
        help_text="Container fill volume in mL (e.g. 3.00 mL for pen/cartridge, 10.00 mL for vial)",
    )
    total_units = models.PositiveIntegerField(
        help_text="Total base insulin units in container (must equal concentration * volume)",
    )
    in_use_shelf_life_days = models.PositiveIntegerField(
        default=28,
        help_text="Permitted room-temperature (<30°C) stability period once pen/vial is in use (14-56 days)",
    )
    unopened_min_temp_celsius = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("2.00"),
        help_text="Minimum storage temperature for unopened insulin (2°C)",
    )
    unopened_max_temp_celsius = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("8.00"),
        help_text="Maximum storage temperature for unopened insulin (8°C)",
    )
    in_use_max_temp_celsius = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("30.00"),
        help_text="Maximum permitted ambient temperature for in-use insulin (30°C)",
    )
    excursion_action = models.CharField(
        max_length=48,
        choices=InsulinExcursionAction.choices,
        default=InsulinExcursionAction.QUARANTINE_FOR_PHARMACIST_ASSESSMENT,
    )
    patient_counseling_en = models.TextField(
        blank=True,
        default="",
        help_text="Patient injection and cold-chain handling advice in English",
    )
    patient_counseling_bn = models.TextField(
        blank=True,
        default="",
        help_text="Patient injection and cold-chain handling advice in Bengali",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Insulin Form Config"
        verbose_name_plural = "Insulin Form Configs"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "tenant"],
                name="unique_insulin_product_tenant",
            ),
            models.CheckConstraint(
                condition=models.Q(concentration_iu_per_ml__in=[100, 200, 300, 500]),
                name="check_insulin_standard_concentration",
            ),
            models.CheckConstraint(
                condition=models.Q(volume_ml__gt=0),
                name="check_insulin_volume_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(total_units__gt=0),
                name="check_insulin_total_units_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(in_use_shelf_life_days__gte=14) & models.Q(in_use_shelf_life_days__lte=56),
                name="check_insulin_in_use_shelf_life_range",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "action_category", "is_active"]),
            models.Index(fields=["product", "delivery_device"]),
        ]

    def clean(self):
        super().clean()
        # 1. Total units dimension consistency
        expected_units = int(round(Decimal(str(self.concentration_iu_per_ml)) * self.volume_ml))
        if not self.total_units:
            self.total_units = expected_units
        elif self.total_units != expected_units:
            raise ValidationError(
                f"Dimension consistency mismatch: total_units ({self.total_units}) must equal "
                f"concentration ({self.concentration_iu_per_ml} IU/mL) * volume ({self.volume_ml} mL) = {expected_units} IU."
            )

        # 2. In-use shelf life bounds (14 - 56 days)
        if self.in_use_shelf_life_days < 14 or self.in_use_shelf_life_days > 56:
            raise ValidationError(
                f"in_use_shelf_life_days ({self.in_use_shelf_life_days}) must be between 14 and 56 days."
            )

        # 3. Cold chain temperature consistency
        if self.unopened_min_temp_celsius >= self.unopened_max_temp_celsius:
            raise ValidationError(
                f"unopened_min_temp_celsius ({self.unopened_min_temp_celsius}°C) must be less than "
                f"unopened_max_temp_celsius ({self.unopened_max_temp_celsius}°C)."
            )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        t_scope = f"Tenant: {self.tenant_id}" if self.tenant_id else "Global"
        return f"{self.product_id} ({self.delivery_device}) - {self.concentration_iu_per_ml}IU/mL [{self.volume_ml}mL = {self.total_units}IU] ({t_scope})"


# ============================================================================
# P147: Ophthalmic, Otic & Nasal Drops Formulation & Opened-Container Controls
# Architecture Decision: ADR-0147 (Phase 08: Milestone M04 / M05)
# ============================================================================

class DropsRoute(models.TextChoices):
    OPHTHALMIC = "OPHTHALMIC", "Ophthalmic (Eye)"
    OTIC = "OTIC", "Otic (Ear)"
    NASAL = "NASAL", "Nasal (Nose)"


class DropsContainerType(models.TextChoices):
    DROPPER_BOTTLE = "DROPPER_BOTTLE", "Multi-Dose Dropper Bottle"
    SINGLE_DOSE_MINIM = "SINGLE_DOSE_MINIM", "Single-Dose Unit (Minim)"
    SPRAY_PUMP = "SPRAY_PUMP", "Nasal Spray Pump"


class DropsStorageTemp(models.TextChoices):
    ROOM_15_25 = "ROOM_15_25", "Room Temperature (15°C - 25°C)"
    REFRIGERATED_2_8 = "REFRIGERATED_2_8", "Refrigerated (2°C - 8°C)"
    COOL_BELOW_30 = "COOL_BELOW_30", "Store Below 30°C"


class DropsFormConfig(models.Model):
    """
    Structured dosage-form configuration for Ophthalmic, Otic and Nasal drops.
    Enforces route safety, fill volume, concentration, preservative declaration,
    and post-opening beyond-use date (BUD) shelf life.
    """

    config_id = models.CharField(max_length=64, primary_key=True)
    product = models.ForeignKey(
        "core.MedicineProduct",
        on_delete=models.CASCADE,
        related_name="drops_configs",
    )
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="drops_configs",
    )
    route = models.CharField(
        max_length=32,
        choices=DropsRoute.choices,
        default=DropsRoute.OPHTHALMIC,
    )
    container_type = models.CharField(
        max_length=32,
        choices=DropsContainerType.choices,
        default=DropsContainerType.DROPPER_BOTTLE,
    )
    volume_ml = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("5.00"),
        help_text="Container fill volume in mL (e.g. 5.00, 10.00, 15.00 mL)",
    )
    concentration_value = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal("0.5000"),
        help_text="Numerical concentration value (e.g. 0.5 for 0.5%)",
    )
    concentration_unit = models.CharField(
        max_length=32,
        default="PERCENT_W_V",
        help_text="Concentration unit (PERCENT_W_V, MG_PER_ML, IU_PER_ML)",
    )
    is_preservative_free = models.BooleanField(
        default=False,
        help_text="Flag indicating preservative-free formulation (e.g. BAK-free)",
    )
    preservative_name = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Name of antimicrobial preservative (e.g. Benzalkonium Chloride)",
    )
    opened_shelf_life_days = models.PositiveIntegerField(
        default=28,
        help_text="Maximum permissible beyond-use days after opening (standard 28 days)",
    )
    storage_temp = models.CharField(
        max_length=32,
        choices=DropsStorageTemp.choices,
        default=DropsStorageTemp.ROOM_15_25,
    )
    patient_instructions_en = models.TextField(
        blank=True,
        default="",
        help_text="Patient administration instructions in English",
    )
    patient_instructions_bn = models.TextField(
        blank=True,
        default="",
        help_text="Patient administration instructions in Bengali",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Drops Form Config"
        verbose_name_plural = "Drops Form Configs"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "tenant"],
                name="unique_drops_product_tenant",
            ),
            models.CheckConstraint(
                condition=models.Q(volume_ml__gt=0),
                name="check_drops_volume_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(concentration_value__gt=0),
                name="check_drops_concentration_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(opened_shelf_life_days__gte=1) & models.Q(opened_shelf_life_days__lte=60),
                name="check_drops_opened_shelf_life_range",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "route", "is_active"]),
            models.Index(fields=["product", "container_type"]),
        ]

    def clean(self):
        super().clean()
        # 1. Volume must be positive
        if self.volume_ml <= Decimal("0"):
            raise ValidationError("Container volume_ml must be greater than 0.")

        # 2. Concentration must be positive
        if self.concentration_value <= Decimal("0"):
            raise ValidationError("concentration_value must be greater than 0.")

        # 3. Single-dose minim must have 1-day opened shelf life
        if self.container_type == DropsContainerType.SINGLE_DOSE_MINIM and self.opened_shelf_life_days > 1:
            raise ValidationError(
                "Single-dose minim (SINGLE_DOSE_MINIM) must have opened_shelf_life_days = 1 (discard within 24 hours)."
            )

        # 4. Multidose shelf life bounds (1 - 60 days)
        if self.opened_shelf_life_days < 1 or self.opened_shelf_life_days > 60:
            raise ValidationError("opened_shelf_life_days must be between 1 and 60 days.")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        t_scope = f"Tenant: {self.tenant_id}" if self.tenant_id else "Global"
        return f"{self.product_id} ({self.route}) - {self.volume_ml}mL [{self.container_type}] BUD: {self.opened_shelf_life_days}d ({t_scope})"


# -----------------------------------------------------------------------------
# P146 - Inhaler Editor & Actuation Packaging Model
# Architecture Decision: ADR-0146
# -----------------------------------------------------------------------------

class InhalerDeviceType(models.TextChoices):
    MDI_PRESSURIZED = "MDI_PRESSURIZED", "Pressurized Metered-Dose Inhaler (pMDI)"
    DPI_DRY_POWDER = "DPI_DRY_POWDER", "Dry Powder Inhaler (DPI)"
    SMI_SOFT_MIST = "SMI_SOFT_MIST", "Soft Mist Inhaler (SMI)"
    BREATH_ACTUATED_MDI = "BREATH_ACTUATED_MDI", "Breath-Actuated Inhaler (BAI)"
    NEBULIZER_AMPOULE = "NEBULIZER_AMPOULE", "Nebulizer Suspension/Solution Ampoule"


class InhalerRoute(models.TextChoices):
    ORAL_INHALATION = "ORAL_INHALATION", "Oral Inhalation"
    NASAL_INHALATION = "NASAL_INHALATION", "Nasal Inhalation"


class InhalerStorageCondition(models.TextChoices):
    ROOM_15_25 = "ROOM_15_25", "Store between 15°C and 25°C. Do not freeze."
    COLD_2_8 = "COLD_2_8", "Refrigerate 2°C to 8°C before dispensing"
    PROTECT_FROM_HEAT = "PROTECT_FROM_HEAT", "Store below 30°C. Protect from direct heat & sunlight"


class InhalerFormConfig(models.Model):
    """
    Formulation and packaging metadata for inhalers (MDI, DPI, SMI).
    Architecture Decision: ADR-0146
    Requirement Trace: P146 (Inhaler Editor)
    """

    config_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="inhaler_configs",
        help_text="Associated medicine product in master catalog",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="inhaler_configs",
        help_text="Tenant context. Null indicates platform master monograph",
    )
    device_type = models.CharField(
        max_length=32,
        choices=InhalerDeviceType.choices,
        default=InhalerDeviceType.MDI_PRESSURIZED,
        help_text="Delivery device mechanism",
    )
    administration_route = models.CharField(
        max_length=32,
        choices=InhalerRoute.choices,
        default=InhalerRoute.ORAL_INHALATION,
        help_text="Anatomical route of administration",
    )
    dose_per_actuation = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal("100.0000"),
        help_text="Amount of drug delivered per single actuation/puff",
    )
    dose_unit = models.CharField(
        max_length=32,
        default="MCG",
        help_text="Measurement unit for dose per actuation (MCG, MG)",
    )
    total_actuations = models.PositiveIntegerField(
        default=200,
        help_text="Total certified number of metered actuations/puffs per device",
    )
    total_active_content = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Total active medicine content in device (must equal dose_per_actuation * total_actuations if supplied)",
    )
    has_dose_counter = models.BooleanField(
        default=True,
        help_text="Whether inhaler device is fitted with an integrated dose counter",
    )
    has_spacer_included = models.BooleanField(
        default=False,
        help_text="Whether inhaler includes a holding chamber / spacer device",
    )
    storage_condition = models.CharField(
        max_length=32,
        choices=InhalerStorageCondition.choices,
        default=InhalerStorageCondition.ROOM_15_25,
    )
    priming_required = models.BooleanField(
        default=True,
        help_text="Whether device requires shaking and test spray priming",
    )
    priming_puffs = models.PositiveIntegerField(
        default=2,
        help_text="Number of priming test sprays required when new or unused",
    )
    canister_pressure_warning = models.BooleanField(
        default=True,
        help_text="Pressurized container: Do not puncture or incinerate even when empty",
    )
    patient_instructions_en = models.TextField(
        blank=True,
        default="",
        help_text="Patient inhalation and cleaning instructions in English",
    )
    patient_instructions_bn = models.TextField(
        blank=True,
        default="",
        help_text="Patient inhalation and cleaning instructions in Bengali",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Inhaler Form Config"
        verbose_name_plural = "Inhaler Form Configs"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "tenant"],
                name="unique_inhaler_product_tenant",
            ),
            models.CheckConstraint(
                condition=models.Q(dose_per_actuation__gt=0),
                name="check_inhaler_dose_per_actuation_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(total_actuations__gt=0) & models.Q(total_actuations__lte=1000),
                name="check_inhaler_total_actuations_range",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "administration_route", "is_active"]),
            models.Index(fields=["product", "device_type"]),
        ]

    def clean(self):
        super().clean()
        if self.dose_per_actuation <= Decimal("0"):
            raise ValidationError("dose_per_actuation must be greater than 0.")

        if self.total_actuations < 10 or self.total_actuations > 1000:
            raise ValidationError("total_actuations must be between 10 and 1000 actuations.")

        if self.total_active_content is not None:
            expected = (self.dose_per_actuation * Decimal(str(self.total_actuations))).quantize(Decimal("0.0001"))
            actual = self.total_active_content.quantize(Decimal("0.0001"))
            if expected != actual:
                raise ValidationError(
                    f"Dimension mismatch: total_active_content ({actual}) must equal dose_per_actuation * total_actuations ({expected})."
                )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        t_scope = f"Tenant: {self.tenant_id}" if self.tenant_id else "Global"
        return f"{self.product_id} ({self.device_type}) - {self.dose_per_actuation}{self.dose_unit} x {self.total_actuations} puffs ({t_scope})"


# -----------------------------------------------------------------------------
# P148 - Cream, Ointment, Gel, and Lotion Topical Form Model
# Architecture Decision: ADR-0148
# -----------------------------------------------------------------------------

class TopicalFormulationType(models.TextChoices):
    CREAM = "CREAM", "Topical Cream (Emulsion)"
    OINTMENT = "OINTMENT", "Topical Ointment (Hydrocarbon/Petrolatum)"
    GEL = "GEL", "Topical Gel (Hydrogel)"
    LOTION = "LOTION", "Topical Lotion (Fluid Suspension)"
    PASTE = "PASTE", "Topical Paste (High Solid Powder)"


class TopicalRoute(models.TextChoices):
    TOPICAL_CUTANEOUS = "TOPICAL_CUTANEOUS", "Topical Cutaneous (Skin)"
    TOPICAL_OPHTHALMIC = "TOPICAL_OPHTHALMIC", "Topical Ophthalmic (Eye Ointment)"
    TOPICAL_RECTAL = "TOPICAL_RECTAL", "Topical Rectal"
    TOPICAL_VAGINAL = "TOPICAL_VAGINAL", "Topical Vaginal"


class TopicalContainerType(models.TextChoices):
    TUBE_ALUMINUM = "TUBE_ALUMINUM", "Collapsible Aluminum Tube"
    TUBE_PLASTIC = "TUBE_PLASTIC", "Laminated Plastic Tube"
    JAR_POT = "JAR_POT", "Dermatological Jar / Pot"
    BOTTLE_PUMP = "BOTTLE_PUMP", "Dispenser Pump Bottle"
    SACHET = "SACHET", "Single-Use Sachet"


class TopicalStorageCondition(models.TextChoices):
    ROOM_15_25 = "ROOM_15_25", "Store between 15°C and 25°C. Do not freeze."
    COOL_BELOW_30 = "COOL_BELOW_30", "Store below 30°C. Protect from direct heat."
    COLD_2_8 = "COLD_2_8", "Refrigerate between 2°C and 8°C"


class TopicalFormConfig(models.Model):
    """
    Formulation and packaging metadata for Creams, Ointments, Gels, and Lotions.
    Architecture Decision: ADR-0148
    Requirement Trace: P148 (Cream/Ointment Editor)
    """

    config_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="topical_configs",
        help_text="Associated medicine product in master catalog",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="topical_configs",
        help_text="Tenant context. Null indicates platform master monograph",
    )
    formulation_type = models.CharField(
        max_length=32,
        choices=TopicalFormulationType.choices,
        default=TopicalFormulationType.CREAM,
        help_text="Semisolid base categorization",
    )
    administration_route = models.CharField(
        max_length=32,
        choices=TopicalRoute.choices,
        default=TopicalRoute.TOPICAL_CUTANEOUS,
        help_text="Topical anatomical route",
    )
    container_type = models.CharField(
        max_length=32,
        choices=TopicalContainerType.choices,
        default=TopicalContainerType.TUBE_ALUMINUM,
        help_text="Primary packaging container type",
    )
    net_weight_grams = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("15.00"),
        help_text="Container fill weight in grams (or mL for lotions)",
    )
    concentration_value = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal("1.0000"),
        help_text="Numerical concentration value (e.g. 1.0 for 1% or 10 mg/g)",
    )
    concentration_unit = models.CharField(
        max_length=32,
        default="PERCENT_W_W",
        help_text="Concentration unit (PERCENT_W_W, MG_PER_G, PERCENT_W_V, IU_PER_G)",
    )
    is_sterile = models.BooleanField(
        default=False,
        help_text="Sterility guarantee (mandatory for ophthalmic eye ointments)",
    )
    applicator_included = models.BooleanField(
        default=False,
        help_text="Whether package includes a cannula or dispensing applicator",
    )
    opened_shelf_life_days = models.PositiveIntegerField(
        default=28,
        help_text="In-use beyond-use date (BUD) shelf life in days after puncture",
    )
    storage_condition = models.CharField(
        max_length=32,
        choices=TopicalStorageCondition.choices,
        default=TopicalStorageCondition.ROOM_15_25,
    )
    patient_instructions_en = models.TextField(
        blank=True,
        default="",
        help_text="Patient topical application counseling in English",
    )
    patient_instructions_bn = models.TextField(
        blank=True,
        default="",
        help_text="Patient topical application counseling in Bengali",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Topical Form Config"
        verbose_name_plural = "Topical Form Configs"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "tenant"],
                name="unique_topical_product_tenant",
            ),
            models.CheckConstraint(
                condition=models.Q(net_weight_grams__gt=0),
                name="check_topical_net_weight_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(concentration_value__gt=0),
                name="check_topical_concentration_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(opened_shelf_life_days__gte=7) & models.Q(opened_shelf_life_days__lte=180),
                name="check_topical_opened_shelf_life_range",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "formulation_type", "is_active"]),
            models.Index(fields=["product", "administration_route"]),
        ]

    def clean(self):
        super().clean()
        if self.net_weight_grams <= Decimal("0"):
            raise ValidationError("net_weight_grams must be greater than 0.")

        if self.concentration_value <= Decimal("0"):
            raise ValidationError("concentration_value must be greater than 0.")

        if self.administration_route == TopicalRoute.TOPICAL_OPHTHALMIC and not self.is_sterile:
            raise ValidationError(
                "Ophthalmic eye ointments (TOPICAL_OPHTHALMIC) must be certified sterile (is_sterile = True)."
            )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        t_scope = f"Tenant: {self.tenant_id}" if self.tenant_id else "Global"
        return f"{self.product_id} ({self.formulation_type}) - {self.net_weight_grams}g [{self.container_type}] ({t_scope})"


# -----------------------------------------------------------------------------
# P149 - Sachet, Powder, and Reconstitution Granule Model
# Architecture Decision: ADR-0149
# -----------------------------------------------------------------------------

class PowderCategory(models.TextChoices):
    ORAL_REHYDRATION_SALTS = "ORAL_REHYDRATION_SALTS", "Oral Rehydration Salts (ORS)"
    EFFERVESCENT_POWDER = "EFFERVESCENT_POWDER", "Effervescent Powder / Granules"
    BULK_LAXATIVE_HUSK = "BULK_LAXATIVE_HUSK", "Bulk Laxative (Ispaghula/Psyllium Husk)"
    SUSPENSION_GRANULES = "SUSPENSION_GRANULES", "Antibiotic / Granules for Reconstitution"
    TOPICAL_DUSTING_POWDER = "TOPICAL_DUSTING_POWDER", "Topical Cutaneous Dusting Powder"


class SachetRoute(models.TextChoices):
    ORAL = "ORAL", "Oral (Ingestion after mixing/suspension)"
    TOPICAL = "TOPICAL", "Topical (External skin application)"


class PowderDiluentType(models.TextChoices):
    POTABLE_WATER = "POTABLE_WATER", "Fresh Potable Drinking Water"
    BOILED_COOLED_WATER = "BOILED_COOLED_WATER", "Boiled and Cooled Clean Water"
    JUICE_OR_WATER = "JUICE_OR_WATER", "Fruit Juice, Milk or Water"
    NOT_APPLICABLE = "NOT_APPLICABLE", "Not Applicable (External or Dry Ingestion)"


class PowderStorageCondition(models.TextChoices):
    ROOM_15_25 = "ROOM_15_25", "Store between 15°C and 25°C in a dry place"
    COOL_DRY_BELOW_30 = "COOL_DRY_BELOW_30", "Store below 30°C. Protect from moisture and direct sunlight"
    PROTECT_FROM_MOISTURE = "PROTECT_FROM_MOISTURE", "Store in airtight pack. Highly hygroscopic"


class SachetFormConfig(models.Model):
    """
    Formulation, packaging, and dilution metadata for Sachets and Dry Powders.
    Architecture Decision: ADR-0149
    Requirement Trace: P149 (Sachet/Powder Editor)
    """

    config_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="sachet_configs",
        help_text="Associated medicine product in master catalog",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="sachet_configs",
        help_text="Tenant context. Null indicates platform master monograph",
    )
    powder_category = models.CharField(
        max_length=32,
        choices=PowderCategory.choices,
        default=PowderCategory.ORAL_REHYDRATION_SALTS,
        help_text="Clinical and pharmaceutical powder classification",
    )
    administration_route = models.CharField(
        max_length=32,
        choices=SachetRoute.choices,
        default=SachetRoute.ORAL,
        help_text="Route of administration",
    )
    grams_per_sachet = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("10.25"),
        help_text="Net dry powder weight in grams per individual sachet",
    )
    sachets_per_pack = models.PositiveIntegerField(
        default=25,
        help_text="Number of individual sachets in the outer packaging box/pack",
    )
    total_pack_weight_grams = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Total pack weight in grams. Must equal grams_per_sachet * sachets_per_pack if supplied",
    )
    loose_sachet_sale_eligible = models.BooleanField(
        default=True,
        help_text="Whether individual sachets may be dispensed loose at the retail counter",
    )
    requires_dissolution = models.BooleanField(
        default=True,
        help_text="Whether powder must be reconstituted / dissolved in fluid before ingestion",
    )
    diluent_type = models.CharField(
        max_length=32,
        choices=PowderDiluentType.choices,
        default=PowderDiluentType.POTABLE_WATER,
        help_text="Recommended fluid for dissolution",
    )
    diluent_volume_ml = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        default=Decimal("500.00"),
        help_text="Required volume of diluent in mL (e.g. 500 mL for standard ORS)",
    )
    reconstituted_stability_hours = models.PositiveIntegerField(
        default=24,
        help_text="Hours after mixing before remaining solution must be discarded (e.g. 24h for ORS)",
    )
    storage_condition = models.CharField(
        max_length=32,
        choices=PowderStorageCondition.choices,
        default=PowderStorageCondition.COOL_DRY_BELOW_30,
    )
    patient_instructions_en = models.TextField(
        blank=True,
        default="",
        help_text="Patient reconstitution and intake counseling in English",
    )
    patient_instructions_bn = models.TextField(
        blank=True,
        default="",
        help_text="Patient reconstitution and intake counseling in Bengali",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Sachet Form Config"
        verbose_name_plural = "Sachet Form Configs"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "tenant"],
                name="unique_sachet_product_tenant",
            ),
            models.CheckConstraint(
                condition=models.Q(grams_per_sachet__gt=0),
                name="check_sachet_grams_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(sachets_per_pack__gt=0) & models.Q(sachets_per_pack__lte=500),
                name="check_sachets_per_pack_range",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "powder_category", "is_active"]),
            models.Index(fields=["product", "administration_route"]),
        ]

    def clean(self):
        super().clean()
        if self.grams_per_sachet <= Decimal("0"):
            raise ValidationError("grams_per_sachet must be greater than 0.")

        if self.sachets_per_pack < 1 or self.sachets_per_pack > 500:
            raise ValidationError("sachets_per_pack must be between 1 and 500.")

        if self.requires_dissolution and (self.diluent_volume_ml is None or self.diluent_volume_ml <= Decimal("0")):
            raise ValidationError("diluent_volume_ml must be greater than 0 when requires_dissolution is True.")

        if self.total_pack_weight_grams is not None:
            expected = (self.grams_per_sachet * Decimal(str(self.sachets_per_pack))).quantize(Decimal("0.01"))
            actual = self.total_pack_weight_grams.quantize(Decimal("0.01"))
            if expected != actual:
                raise ValidationError(
                    f"Dimension mismatch: total_pack_weight_grams ({actual}) must equal grams_per_sachet * sachets_per_pack ({expected})."
                )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        t_scope = f"Tenant: {self.tenant_id}" if self.tenant_id else "Global"
        return f"{self.product_id} ({self.powder_category}) - {self.grams_per_sachet}g x {self.sachets_per_pack} sachets ({t_scope})"


# -----------------------------------------------------------------------------
# P150 - Suppository and Pessary Model
# Architecture Decision: ADR-0150
# -----------------------------------------------------------------------------

class SuppositoryBaseType(models.TextChoices):
    FATTY_BASE_COCOA_BUTTER = "FATTY_BASE_COCOA_BUTTER", "Fatty Base (Cocoa Butter / Hard Fat, Melts at 30-35°C)"
    WATER_SOLUBLE_PEG = "WATER_SOLUBLE_PEG", "Water Soluble (Polyethylene Glycol / Macrogol)"
    GLYCERINATED_GELATIN = "GLYCERINATED_GELATIN", "Glycerinated Gelatin Base"


class SuppositoryRoute(models.TextChoices):
    RECTAL = "RECTAL", "Rectal (Suppository)"
    VAGINAL = "VAGINAL", "Vaginal (Pessary)"
    URETHRAL = "URETHRAL", "Urethral (Bougie)"


class SuppositoryStorageCondition(models.TextChoices):
    REFRIGERATED_2_8 = "REFRIGERATED_2_8", "Refrigerated (2°C - 8°C). Protect from melting"
    COOL_BELOW_25 = "COOL_BELOW_25", "Store in a cool place below 25°C"
    COOL_BELOW_30 = "COOL_BELOW_30", "Store below 30°C in a dry place"


class SuppositoryFormConfig(models.Model):
    """
    Formulation, packaging, and temperature governance metadata for Suppositories & Pessaries.
    Architecture Decision: ADR-0150
    Requirement Trace: P150 (Suppository Editor)
    """

    config_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="suppository_configs",
        help_text="Associated medicine product in master catalog",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="suppository_configs",
        help_text="Tenant context. Null indicates platform master monograph",
    )
    suppository_base_type = models.CharField(
        max_length=32,
        choices=SuppositoryBaseType.choices,
        default=SuppositoryBaseType.FATTY_BASE_COCOA_BUTTER,
        help_text="Pharmaceutical suppository base vehicle",
    )
    administration_route = models.CharField(
        max_length=32,
        choices=SuppositoryRoute.choices,
        default=SuppositoryRoute.RECTAL,
        help_text="Anatomical insertion route",
    )
    strength_value = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal("250.0000"),
        help_text="Active ingredient strength per single suppository piece",
    )
    strength_unit = models.CharField(
        max_length=32,
        default="MG",
        help_text="Measurement unit for strength (MG, G, IU, MCG)",
    )
    pieces_per_strip = models.PositiveIntegerField(
        default=5,
        help_text="Number of suppository pieces in each blister/strip",
    )
    strips_per_pack = models.PositiveIntegerField(
        default=2,
        help_text="Number of blister strips in the outer box",
    )
    total_pieces_per_pack = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Total individual pieces per pack (must equal pieces_per_strip * strips_per_pack if supplied)",
    )
    loose_piece_sale_eligible = models.BooleanField(
        default=True,
        help_text="Whether individual suppository pieces may be dispensed loose at retail counter",
    )
    requires_refrigeration = models.BooleanField(
        default=False,
        help_text="Whether suppository requires cold-chain storage to prevent melting",
    )
    storage_condition = models.CharField(
        max_length=32,
        choices=SuppositoryStorageCondition.choices,
        default=SuppositoryStorageCondition.COOL_BELOW_25,
    )
    patient_instructions_en = models.TextField(
        blank=True,
        default="",
        help_text="Patient administration instructions in English",
    )
    patient_instructions_bn = models.TextField(
        blank=True,
        default="",
        help_text="Patient administration instructions in Bengali",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Suppository Form Config"
        verbose_name_plural = "Suppository Form Configs"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "tenant"],
                name="unique_suppository_product_tenant",
            ),
            models.CheckConstraint(
                condition=models.Q(strength_value__gt=0),
                name="check_suppository_strength_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(pieces_per_strip__gt=0) & models.Q(pieces_per_strip__lte=50),
                name="check_pieces_per_strip_range",
            ),
            models.CheckConstraint(
                condition=models.Q(strips_per_pack__gt=0) & models.Q(strips_per_pack__lte=100),
                name="check_strips_per_pack_range",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "administration_route", "is_active"]),
            models.Index(fields=["product", "suppository_base_type"]),
        ]

    def clean(self):
        super().clean()
        if self.strength_value <= Decimal("0"):
            raise ValidationError("strength_value must be greater than 0.")

        if self.pieces_per_strip < 1 or self.pieces_per_strip > 50:
            raise ValidationError("pieces_per_strip must be between 1 and 50.")

        if self.strips_per_pack < 1 or self.strips_per_pack > 100:
            raise ValidationError("strips_per_pack must be between 1 and 100.")

        if self.total_pieces_per_pack is not None:
            expected = self.pieces_per_strip * self.strips_per_pack
            if self.total_pieces_per_pack != expected:
                raise ValidationError(
                    f"Dimension mismatch: total_pieces_per_pack ({self.total_pieces_per_pack}) must equal pieces_per_strip * strips_per_pack ({expected})."
                )

        if self.requires_refrigeration and self.storage_condition not in [
            SuppositoryStorageCondition.REFRIGERATED_2_8,
            SuppositoryStorageCondition.COOL_BELOW_25,
        ]:
            raise ValidationError("requires_refrigeration is True but storage_condition is not refrigerated or cool.")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        t_scope = f"Tenant: {self.tenant_id}" if self.tenant_id else "Global"
        return f"{self.product_id} ({self.administration_route}) - {self.strength_value}{self.strength_unit} [{self.pieces_per_strip}x{self.strips_per_pack}] ({t_scope})"


class DeviceCategory(models.TextChoices):
    NEEDLE_OR_SYRINGE = "NEEDLE_OR_SYRINGE", "Needle or Syringe"
    DIAGNOSTIC_TEST_STRIP = "DIAGNOSTIC_TEST_STRIP", "Diagnostic Test Strip"
    MONITORING_DEVICE = "MONITORING_DEVICE", "Monitoring Device (BP, Glucometer, Oximeter)"
    SURGICAL_CONSUMABLE = "SURGICAL_CONSUMABLE", "Surgical Consumable (Cannula, Gauze, Suture)"
    RESPIRATORY_DEVICE = "RESPIRATORY_DEVICE", "Respiratory Device (Nebulizer Mask, Spacer)"
    GENERAL_CONSUMABLE = "GENERAL_CONSUMABLE", "General Medical Consumable"


class NeedleGaugeChoice(models.TextChoices):
    G18 = "18G", "18G"
    G20 = "20G", "20G"
    G21 = "21G", "21G"
    G22 = "22G", "22G"
    G23 = "23G", "23G"
    G25 = "25G", "25G"
    G26 = "26G", "26G"
    G27 = "27G", "27G"
    G29 = "29G", "29G"
    G30 = "30G", "30G"
    G31 = "31G", "31G"
    G32 = "32G", "32G"
    NA = "NA", "Not Applicable"


class RegulatoryDeviceClass(models.TextChoices):
    CLASS_A_LOW_RISK = "CLASS_A_LOW_RISK", "Class A (Low Risk)"
    CLASS_B_LOW_MODERATE = "CLASS_B_LOW_MODERATE", "Class B (Low-Moderate Risk)"
    CLASS_C_MODERATE_HIGH = "CLASS_C_MODERATE_HIGH", "Class C (Moderate-High Risk)"
    CLASS_D_HIGH_RISK = "CLASS_D_HIGH_RISK", "Class D (High Risk)"
    NON_REGULATED = "NON_REGULATED", "Non-Regulated Retail Consumable"


class DeviceConsumableConfig(models.Model):
    """
    Medical devices, needles, test strips, and surgical consumables configuration.
    Architecture Decision: ADR-0151
    Provides device-specific physical and compatibility attributes without pharmaceutical drug restrictions.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="device_consumable_configs",
        help_text="Product reference for this device/consumable",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="device_consumable_configs",
        help_text="Tenant boundary. Null indicates global master configuration.",
    )
    device_category = models.CharField(
        max_length=64,
        choices=DeviceCategory.choices,
        default=DeviceCategory.GENERAL_CONSUMABLE,
    )
    brand_name = models.CharField(max_length=150)
    model_number = models.CharField(max_length=100, blank=True, default="")
    needle_gauge = models.CharField(
        max_length=10,
        choices=NeedleGaugeChoice.choices,
        default=NeedleGaugeChoice.NA,
        help_text="Needle thickness gauge if applicable",
    )
    needle_length_mm = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Needle length in millimeters",
    )
    meter_compatibility = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text="Target device or glucometer compatibility",
    )
    strip_count_per_pack = models.PositiveIntegerField(
        default=0,
        help_text="Count of strips per pack if diagnostic strip",
    )
    sample_volume_microliter = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Required blood sample volume in microliters",
    )
    test_time_seconds = models.PositiveIntegerField(
        default=0,
        help_text="Test reaction time in seconds",
    )
    is_sterile = models.BooleanField(
        default=False,
        help_text="Whether item comes in sterile packaging barrier",
    )
    is_single_use = models.BooleanField(
        default=True,
        help_text="Whether device is single-use disposable or reusable",
    )
    regulatory_device_class = models.CharField(
        max_length=32,
        choices=RegulatoryDeviceClass.choices,
        default=RegulatoryDeviceClass.CLASS_A_LOW_RISK,
    )
    storage_temperature_celsius_min = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("2.00"),
    )
    storage_temperature_celsius_max = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("30.00"),
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Device Consumable Config"
        verbose_name_plural = "Device Consumable Configs"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "tenant"],
                name="unique_device_consumable_product_tenant",
            ),
            models.CheckConstraint(
                condition=models.Q(storage_temperature_celsius_min__lte=models.F("storage_temperature_celsius_max")),
                name="check_device_storage_temp_order",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "device_category", "is_active"]),
            models.Index(fields=["product", "needle_gauge"]),
        ]

    def clean(self):
        super().clean()
        if not self.brand_name or len(self.brand_name.strip()) < 2:
            raise ValidationError("brand_name must be at least 2 characters.")

        if self.storage_temperature_celsius_min > self.storage_temperature_celsius_max:
            raise ValidationError("storage_temperature_celsius_min cannot exceed storage_temperature_celsius_max.")

        if self.device_category == DeviceCategory.NEEDLE_OR_SYRINGE:
            if self.needle_gauge == NeedleGaugeChoice.NA:
                raise ValidationError("Needles and syringes must specify a valid needle_gauge.")
            if self.needle_length_mm <= Decimal("0.00"):
                raise ValidationError("Needle length in mm must be greater than 0.")

        if self.device_category == DeviceCategory.DIAGNOSTIC_TEST_STRIP:
            if not self.meter_compatibility or not self.meter_compatibility.strip():
                raise ValidationError("Diagnostic test strips must specify compatible meter model.")
            if self.strip_count_per_pack < 1:
                raise ValidationError("Diagnostic test strips must have strip_count_per_pack >= 1.")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        t_scope = f"Tenant: {self.tenant_id}" if self.tenant_id else "Global"
        return f"{self.product_id} ({self.device_category}) - {self.brand_name} {self.model_number} ({t_scope})"


class LayoutDisplayDensity(models.TextChoices):
    COMPACT = "COMPACT", "Compact"
    COMFORTABLE = "COMFORTABLE", "Comfortable"
    DENSE = "DENSE", "Dense"


class FieldGroupCode(models.TextChoices):
    GENERAL_IDENTIFICATION = "GENERAL_IDENTIFICATION", "General Identification"
    FORM_SPECIFIC_PARAMETERS = "FORM_SPECIFIC_PARAMETERS", "Form-Specific Clinical Parameters"
    PACKAGING_DIMENSIONS = "PACKAGING_DIMENSIONS", "Packaging Dimensions & Unit Multipliers"
    STORAGE_AND_COLD_CHAIN = "STORAGE_AND_COLD_CHAIN", "Storage & Temperature Controls"
    REGULATORY_AND_DISPENSING = "REGULATORY_AND_DISPENSING", "Regulatory & Dispensing Class"
    FINANCIAL_PRICING = "FINANCIAL_PRICING", "Pricing, MRP & Tax Configuration"


class DynamicFieldGroupLayout(models.Model):
    """
    Dynamic Field Group Layout and Keyboard-Accessible Flow.
    Architecture Decision: ADR-0152 (P152).
    Governs conditional group rendering, tab sequence, accessible bilingual labels,
    and responsive desktop flow.
    """

    layout_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="dynamic_field_layouts",
        help_text="Tenant context. Null indicates system canonical default layout.",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="dynamic_field_layouts",
        help_text="Optional branch-level override.",
    )
    dosage_form_family = models.CharField(
        max_length=64,
        default="ALL",
        help_text="Target formulation family or ALL for global base group",
    )
    group_code = models.CharField(
        max_length=64,
        choices=FieldGroupCode.choices,
        help_text="Semantic identifier for the field group",
    )
    display_order = models.PositiveIntegerField(
        default=1,
        help_text="Rendering sequence order on screen",
    )
    title_en = models.CharField(max_length=150, help_text="Group header label in English")
    title_bn = models.CharField(max_length=150, help_text="Group header label in Bengali")
    is_collapsible = models.BooleanField(default=True)
    default_collapsed = models.BooleanField(default=False)
    keyboard_shortcut_mnemonic = models.CharField(
        max_length=10,
        blank=True,
        default="",
        help_text="Keyboard access mnemonic accelerator (e.g. 'G', 'F', 'P', 'S')",
    )
    display_density = models.CharField(
        max_length=32,
        choices=LayoutDisplayDensity.choices,
        default=LayoutDisplayDensity.COMFORTABLE,
    )
    fields_config = models.JSONField(
        default=list,
        blank=True,
        help_text="Array of field metadata: field_name, tab_index, label_en/bn, control_type, width",
    )
    visibility_condition = models.JSONField(
        default=dict,
        blank=True,
        help_text="Conditional expression for when this group is visible",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "created_at"]
        verbose_name = "Dynamic Field Group Layout"
        verbose_name_plural = "Dynamic Field Group Layouts"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "branch", "dosage_form_family", "group_code"],
                name="unique_layout_tenant_branch_group",
            ),
            models.CheckConstraint(
                condition=models.Q(display_order__gt=0),
                name="check_display_order_gt_zero",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "dosage_form_family", "is_active"]),
            models.Index(fields=["group_code", "display_order"]),
        ]

    def clean(self):
        super().clean()
        if not self.title_en or len(self.title_en.strip()) < 2:
            raise ValidationError("title_en must be at least 2 characters.")
        if not self.title_bn or len(self.title_bn.strip()) < 2:
            raise ValidationError("title_bn must be at least 2 characters.")
        if self.display_order < 1:
            raise ValidationError("display_order must be 1 or greater.")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        t_scope = f"Tenant: {self.tenant_id}" if self.tenant_id else "Global"
        return f"[{self.display_order}] {self.group_code} ({self.dosage_form_family}) - {self.title_en} ({t_scope})"


class BaseDispensingUnit(models.TextChoices):
    PIECE = "PIECE", "Single Piece / Unit"
    TABLET = "TABLET", "Tablet"
    CAPSULE = "CAPSULE", "Capsule"
    VIAL = "VIAL", "Vial"
    AMPOULE = "AMPOULE", "Ampoule"
    BOTTLE = "BOTTLE", "Bottle"
    DEVICE = "DEVICE", "Device"
    SACHET = "SACHET", "Sachet"
    SUPPOSITORY = "SUPPOSITORY", "Suppository"
    TUBE = "TUBE", "Tube"
    CANISTER = "CANISTER", "Canister / Inhaler"
    ML = "ML", "Milliliter"


class ConditionalPricingConfig(models.Model):
    """
    Unit-Aware Multi-Tier Conditional Pricing Configuration.
    Architecture Decision: ADR-0153 (P153).
    Maintains strict zero-loss Decimal conversions across packs, strips, and base single pieces.
    """

    pricing_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="conditional_pricing_configs",
        help_text="Target medicine or retail product",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="conditional_pricing_configs",
        help_text="Tenant context. Null indicates platform master pricing guideline.",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="conditional_pricing_configs",
        help_text="Optional branch-specific pricing override.",
    )
    base_dispensing_unit = models.CharField(
        max_length=32,
        choices=BaseDispensingUnit.choices,
        default=BaseDispensingUnit.PIECE,
    )
    pack_to_strip_multiplier = models.PositiveIntegerField(
        default=1,
        help_text="Number of intermediate strips in one pack box",
    )
    strip_to_piece_multiplier = models.PositiveIntegerField(
        default=1,
        help_text="Number of individual units in one strip",
    )
    total_base_units_per_pack = models.PositiveIntegerField(
        default=1,
        help_text="Total base units per outer pack box (must equal pack_to_strip * strip_to_piece)",
    )
    cost_per_piece_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("1.0000"),
        help_text="Base purchase cost per individual unit in BDT",
    )
    mrp_per_piece_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("1.2000"),
        help_text="Maximum retail price per individual unit in BDT",
    )
    strip_cost_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("10.0000"),
        help_text="Derived or specified purchase cost per strip in BDT",
    )
    strip_mrp_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("12.0000"),
        help_text="Derived or specified maximum retail price per strip in BDT",
    )
    pack_cost_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("100.0000"),
        help_text="Purchase cost for outer pack box in BDT",
    )
    pack_mrp_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("120.0000"),
        help_text="Maximum retail price for outer pack box in BDT",
    )
    allow_strip_sale = models.BooleanField(
        default=True,
        help_text="Whether intermediate strips can be sold separately",
    )
    allow_piece_sale = models.BooleanField(
        default=True,
        help_text="Whether individual loose pieces can be dispensed separately",
    )
    vat_rate_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Standard Bangladesh VAT rate percentage applied at checkout",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Conditional Pricing Config"
        verbose_name_plural = "Conditional Pricing Configs"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "tenant", "branch"],
                name="unique_pricing_product_tenant_branch",
            ),
            models.CheckConstraint(
                condition=models.Q(cost_per_piece_bdt__gt=0),
                name="check_pricing_cost_per_piece_gt_zero",
            ),
            models.CheckConstraint(
                condition=models.Q(mrp_per_piece_bdt__gt=0),
                name="check_pricing_mrp_per_piece_gt_zero",
            ),
            models.CheckConstraint(
                condition=models.Q(total_base_units_per_pack__gt=0),
                name="check_pricing_total_base_units_gt_zero",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "base_dispensing_unit", "is_active"]),
            models.Index(fields=["product", "tenant"]),
        ]

    def clean(self):
        super().clean()
        if self.cost_per_piece_bdt <= Decimal("0"):
            raise ValidationError("cost_per_piece_bdt must be greater than 0.")
        if self.mrp_per_piece_bdt <= Decimal("0"):
            raise ValidationError("mrp_per_piece_bdt must be greater than 0.")
        if self.pack_to_strip_multiplier < 1:
            raise ValidationError("pack_to_strip_multiplier must be at least 1.")
        if self.strip_to_piece_multiplier < 1:
            raise ValidationError("strip_to_piece_multiplier must be at least 1.")

        expected_total = self.pack_to_strip_multiplier * self.strip_to_piece_multiplier
        if self.total_base_units_per_pack != expected_total:
            raise ValidationError(
                f"Dimension mismatch: total_base_units_per_pack ({self.total_base_units_per_pack}) must equal "
                f"pack_to_strip ({self.pack_to_strip_multiplier}) * strip_to_piece ({self.strip_to_piece_multiplier}) = {expected_total}."
            )

        if not self.allow_strip_sale and not self.allow_piece_sale:
            if self.total_base_units_per_pack > 1:
                raise ValidationError("Multi-unit pack must permit either strip sale or loose piece sale.")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        t_scope = f"Tenant: {self.tenant_id}" if self.tenant_id else "Global"
        return f"{self.product_id} Pricing - Pack: BDT {self.pack_mrp_bdt}, Strip: BDT {self.strip_mrp_bdt}, Piece: BDT {self.mrp_per_piece_bdt} ({t_scope})"


class StoragePlacementZone(models.TextChoices):
    SHELF_MIDDLE = "SHELF_MIDDLE", "Middle Shelf (Uniform Temperature)"
    SHELF_TOP = "SHELF_TOP", "Top Shelf"
    SHELF_BOTTOM = "SHELF_BOTTOM", "Bottom Shelf (Above Crisper)"
    CHILLER_TRAY = "CHILLER_TRAY", "Chiller Tray (2°C - 4°C)"
    DOOR_NOT_PERMITTED = "DOOR_NOT_PERMITTED", "Door Placement Prohibited"


class ExcursionHandlingAction(models.TextChoices):
    QUARANTINE_IMMEDIATELY = "QUARANTINE_IMMEDIATELY", "Quarantine Immediately for Assessment"
    RETURN_TO_SUPPLIER = "RETURN_TO_SUPPLIER", "Return to Cold-Chain Supplier"
    DISPOSE_BIOHAZARD = "DISPOSE_BIOHAZARD", "Biohazard Safe Disposal"
    NOTIFY_SUPERVISOR = "NOTIFY_SUPERVISOR", "Notify Chief Pharmacist"


class StorageConditionalConfig(models.Model):
    """
    Architecture Decision: ADR-0155 (P155).
    Formulation and product-specific cold-chain storage parameters, safe ranges,
    refrigerator placement, excursion tolerance, and handling alerts.
    """

    config_id = models.CharField(max_length=64, primary_key=True)
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="storage_configs",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="storage_configs",
    )
    is_cold_chain_required = models.BooleanField(default=False)
    min_temperature_celsius = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("2.00"),
    )
    max_temperature_celsius = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("8.00"),
    )
    refrigerator_placement = models.CharField(
        max_length=48,
        choices=StoragePlacementZone.choices,
        default=StoragePlacementZone.SHELF_MIDDLE,
    )
    max_excursion_hours = models.PositiveIntegerField(
        default=24,
        validators=[MaxValueValidator(72)],
    )
    excursion_handling_action = models.CharField(
        max_length=48,
        choices=ExcursionHandlingAction.choices,
        default=ExcursionHandlingAction.QUARANTINE_IMMEDIATELY,
    )
    requires_ice_pack_transport = models.BooleanField(default=True)
    protect_from_freezing = models.BooleanField(default=True)
    handling_alert_notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = ("product", "tenant")
        indexes = [
            models.Index(fields=["tenant", "is_cold_chain_required"]),
            models.Index(fields=["product", "is_cold_chain_required"]),
        ]
        verbose_name = "Storage Conditional Config"
        verbose_name_plural = "Storage Conditional Configs"

    def __str__(self) -> str:
        status = "COLD-CHAIN" if self.is_cold_chain_required else "AMBIENT"
        return f"StorageConfig {self.config_id} - {self.product.brand_name} ({status})"

    def clean(self):
        super().clean()
        if self.is_cold_chain_required:
            if self.min_temperature_celsius >= self.max_temperature_celsius:
                raise ValidationError(
                    f"min_temperature_celsius ({self.min_temperature_celsius}°C) must be less than "
                    f"max_temperature_celsius ({self.max_temperature_celsius}°C)."
                )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)























class LocalInventoryConfig(models.Model):
    """
    Architecture Decision: ADR-0156 (P156).
    Tenant and branch-scoped inventory configuration: local SKU, custom selling price,
    reorder alert threshold, physical store shelf coordinates, and branch visibility.
    """

    config_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="local_inventory_configs",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="local_inventory_configs",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="local_inventory_configs",
        db_index=True,
    )
    local_sku = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="Store or branch specific barcode / SKU.",
    )
    local_selling_price_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Custom local retail selling price in BDT. Falls back to product mrp_unit if null.",
    )
    reorder_level = models.PositiveIntegerField(
        default=10,
        help_text="Threshold quantity below which low-stock reorder alert triggers.",
    )
    shelf_location = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Physical store location coordinates (e.g. Rack-B / Shelf-2 / Bin-04).",
    )
    is_visible = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Visibility toggle for cashier POS search and inventory display.",
    )
    min_order_quantity = models.PositiveIntegerField(
        default=1,
        help_text="Suggested minimum order replenishment quantity.",
    )
    max_capacity_units = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Maximum storage capacity of the assigned shelf/bin.",
    )
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Local Inventory Configuration"
        verbose_name_plural = "Local Inventory Configurations"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "branch", "product"],
                name="unique_local_inventory_config",
            ),
            models.CheckConstraint(
                condition=models.Q(local_selling_price_bdt__isnull=True)
                | models.Q(local_selling_price_bdt__gte=Decimal("0.01")),
                name="check_positive_local_selling_price",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "branch", "is_visible"]),
            models.Index(fields=["tenant", "local_sku"]),
            models.Index(fields=["tenant", "product"]),
        ]

    def __str__(self) -> str:
        b_name = self.branch.name if self.branch else "All Branches"
        return f"{self.product.brand_name} [{self.tenant.name} - {b_name}] Loc: {self.shelf_location or 'N/A'}"

    def clean(self):
        super().clean()
        if self.local_selling_price_bdt is not None and self.local_selling_price_bdt < Decimal("0.01"):
            raise ValidationError({"local_selling_price_bdt": "Local selling price must be at least 0.01 BDT."})
        if self.max_capacity_units is not None and self.max_capacity_units < self.min_order_quantity:
            raise ValidationError({
                "max_capacity_units": f"max_capacity_units ({self.max_capacity_units}) cannot be less than min_order_quantity ({self.min_order_quantity})."
            })

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class InventoryBaseUnitStrategy(models.Model):
    """
    Architecture Decision: ADR-0161 (P161).
    Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.
    Inventory Base-Unit Strategy per SKU:
    - Defines canonical stock base unit (e.g. TABLET, BOTTLE, VIAL).
    - Specifies indivisibility rules and decimal constraints.
    - Configures multi-tier unit conversions (Pack -> Intermediate -> Base).
    - Protects exact Decimal precision for cost lots and valuation.
    """

    strategy_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="base_unit_strategies",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="base_unit_strategies",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="base_unit_strategies",
        db_index=True,
    )

    base_unit = models.CharField(
        max_length=32,
        choices=BaseDispensingUnit.choices,
        default=BaseDispensingUnit.PIECE,
        db_index=True,
    )
    is_indivisible = models.BooleanField(
        default=True,
        db_index=True,
        help_text="If True, item cannot be broken into fractions during dispensing, checkout, or stock adjustments.",
    )
    allow_decimal_quantities = models.BooleanField(
        default=False,
        help_text="Whether fractional stock movements are permitted (e.g. compounding syrups, bulk liquids in ml).",
    )
    decimal_places_allowed = models.PositiveSmallIntegerField(
        default=0,
        help_text="Allowed decimal precision for quantities (0 for integer items, up to 4 for bulk liquids).",
    )
    minimum_dispense_quantity = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("1.0000"),
        validators=[MinValueValidator(Decimal("0.0001"))],
        help_text="Minimum allowed quantity step for transactions.",
    )

    pack_unit_label = models.CharField(
        max_length=32,
        default="BOX",
        help_text="Outer bulk package unit label (e.g. BOX, CARTON).",
    )
    intermediate_unit_label = models.CharField(
        max_length=32,
        default="STRIP",
        help_text="Intermediate packaging unit label (e.g. STRIP, BLISTER, PACK).",
    )
    pack_to_intermediate_multiplier = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Number of intermediate units per outer pack box.",
    )
    intermediate_to_base_multiplier = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Number of base units per intermediate unit.",
    )
    total_base_units_per_pack = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Derived total base units per pack (must equal pack_to_intermediate * intermediate_to_base).",
    )

    cost_per_base_unit = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Exact purchase cost per base unit in BDT (4 decimal places).",
    )
    mrp_per_base_unit = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Maximum retail selling price per base unit in BDT (4 decimal places).",
    )

    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Inventory Base Unit Strategy"
        verbose_name_plural = "Inventory Base Unit Strategies"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "branch", "product"],
                name="unique_tenant_branch_product_base_unit_strategy",
            ),
            models.CheckConstraint(
                condition=models.Q(cost_per_base_unit__gte=Decimal("0.0000")),
                name="check_positive_base_unit_cost",
            ),
            models.CheckConstraint(
                condition=models.Q(mrp_per_base_unit__gte=models.F("cost_per_base_unit")),
                name="check_mrp_gte_cost_base_unit",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "product"]),
            models.Index(fields=["tenant", "base_unit"]),
            models.Index(fields=["tenant", "is_indivisible"]),
        ]

    def __str__(self) -> str:
        b_name = self.branch.name if self.branch else "All Branches"
        return f"{self.product.brand_name} [{self.tenant.name} - {b_name}] Base: {self.base_unit} (Indivisible: {self.is_indivisible})"

    def clean(self):
        super().clean()
        expected_total = self.pack_to_intermediate_multiplier * self.intermediate_to_base_multiplier
        if self.total_base_units_per_pack != expected_total:
            self.total_base_units_per_pack = expected_total

        if self.is_indivisible:
            if self.allow_decimal_quantities:
                raise ValidationError({
                    "allow_decimal_quantities": "Indivisible items cannot allow decimal quantities."
                })
            if self.decimal_places_allowed != 0:
                raise ValidationError({
                    "decimal_places_allowed": "Indivisible items must have decimal_places_allowed set to 0."
                })
            if self.minimum_dispense_quantity % Decimal("1") != Decimal("0"):
                raise ValidationError({
                    "minimum_dispense_quantity": "Minimum dispense quantity for indivisible items must be a whole integer."
                })

        if self.mrp_per_base_unit < self.cost_per_base_unit:
            raise ValidationError({
                "mrp_per_base_unit": f"MRP per base unit ({self.mrp_per_base_unit}) cannot be lower than cost ({self.cost_per_base_unit})."
            })

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class PackagingHierarchyNode(models.Model):
    """
    Architecture Decision: ADR-0162 (P162).
    Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.
    N-Tier Packaging Hierarchy Node with Effective-Dated Factors.
    Models packaging chains such as:
    - CARTON -> BOX -> STRIP -> TABLET
    - CARTON -> BOTTLE
    - SHIPPER -> PACK -> DEVICE
    """

    node_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="packaging_hierarchy_nodes",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="packaging_hierarchy_nodes",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="packaging_hierarchy_nodes",
        db_index=True,
    )

    parent_unit = models.CharField(
        max_length=32,
        db_index=True,
        help_text="Parent unit packaging tier (e.g. CARTON, BOX, TRAY, PACK).",
    )
    child_unit = models.CharField(
        max_length=32,
        db_index=True,
        help_text="Child unit packaging tier (e.g. BOX, STRIP, TABLET, BOTTLE, PIECE).",
    )
    conversion_factor = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Number of child units contained in one parent unit.",
    )

    tier_level = models.PositiveSmallIntegerField(
        default=1,
        help_text="Hierarchy depth level (1=immediate parent of base, 2=outer box, 3=shipper).",
    )
    is_base_level = models.BooleanField(
        default=False,
        help_text="True if child_unit is the atomic inventory base dispensing unit.",
    )
    tier_gtin_barcode = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="GTIN-14 or alternate barcode specifically identifying this packaging tier.",
    )

    effective_from = models.DateField(
        db_index=True,
        help_text="Start date when this packaging factor becomes effective.",
    )
    effective_to = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        help_text="End date for this packaging factor (null means open-ended/active).",
    )

    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-tier_level", "-effective_from"]
        verbose_name = "Packaging Hierarchy Node"
        verbose_name_plural = "Packaging Hierarchy Nodes"
        indexes = [
            models.Index(fields=["tenant", "product", "is_active"]),
            models.Index(fields=["tenant", "product", "effective_from", "effective_to"]),
            models.Index(fields=["parent_unit", "child_unit"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(conversion_factor__gte=1),
                name="check_positive_packaging_factor",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gte=models.F("effective_from")),
                name="check_valid_effective_date_range",
            ),
        ]

    def __str__(self) -> str:
        b_name = self.branch.name if self.branch else "All Branches"
        end_str = self.effective_to.isoformat() if self.effective_to else "Open"
        return f"{self.product.brand_name} [{self.tenant.name} - {b_name}]: 1 {self.parent_unit} = {self.conversion_factor} {self.child_unit} ({self.effective_from} to {end_str})"

    def clean(self):
        super().clean()
        if self.parent_unit and self.child_unit and self.parent_unit.strip().upper() == self.child_unit.strip().upper():
            raise ValidationError({
                "parent_unit": "Parent unit and child unit cannot be identical in packaging hierarchy."
            })
        if self.conversion_factor < 1:
            raise ValidationError({
                "conversion_factor": "Conversion factor must be an integer greater than or equal to 1."
            })
        if self.effective_to and self.effective_from and self.effective_to < self.effective_from:
            raise ValidationError({
                "effective_to": "effective_to date cannot be earlier than effective_from date."
            })

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class UnitConversionPolicy(models.Model):
    """
    Architecture Decision: ADR-0163 (P163).
    Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.
    Multi-Level Unit Conversion & Inappropriate Dosing-Unit Guardrails.
    - Specifies whether a pharmaceutical tablet or solid item is splittable.
    - Limits permitted fractional splits (e.g. 0.5 for half-tablets).
    - Lists prohibited dosing units for the product dosage form (e.g. ML, PUFF for tablets).
    - Stores clinical dosing conversion ratio (e.g. 1 Tablet = 500mg) for reference.
    """

    policy_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="unit_conversion_policies",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="unit_conversion_policies",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="unit_conversion_policies",
        db_index=True,
    )

    is_splittable = models.BooleanField(
        default=False,
        help_text="Whether this solid tablet is scored with an approved breakline for fractional dispensing.",
    )
    allowed_split_fractions = models.JSONField(
        default=list,
        blank=True,
        help_text="List of permitted fractional steps (e.g. [0.5, 1.0]). If not splittable, defaults to [1.0].",
    )

    clinical_dosing_unit = models.CharField(
        max_length=32,
        blank=True,
        default="MG",
        help_text="Canonical active ingredient dosing unit (e.g. MG, MCG, G, ML, PUFF).",
    )
    dosing_conversion_ratio = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("1.0000"),
        validators=[MinValueValidator(Decimal("0.0001"))],
        help_text="Clinical dosing strength per base unit (e.g. 500.0000 for 500mg per tablet).",
    )
    prohibited_dosing_units = models.JSONField(
        default=list,
        blank=True,
        help_text="List of incompatible dosing/packaging units that must be blocked for this product.",
    )

    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Unit Conversion Policy"
        verbose_name_plural = "Unit Conversion Policies"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "branch", "product"],
                name="unique_tenant_branch_product_unit_conversion_policy",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "product", "is_active"]),
            models.Index(fields=["tenant", "is_splittable"]),
        ]

    def __str__(self) -> str:
        b_name = self.branch.name if self.branch else "All Branches"
        return f"{self.product.brand_name} [{self.tenant.name} - {b_name}]: Splittable: {self.is_splittable} (Allowed: {self.allowed_split_fractions})"

    def clean(self):
        super().clean()
        if not self.allowed_split_fractions:
            self.allowed_split_fractions = [0.5, 1.0] if self.is_splittable else [1.0]
        else:
            valid_fractions = []
            for f in self.allowed_split_fractions:
                try:
                    num = float(f)
                    if num <= 0 or num > 1.0:
                        raise ValueError()
                    valid_fractions.append(round(num, 4))
                except Exception:
                    raise ValidationError({
                        "allowed_split_fractions": f"Invalid split fraction '{f}'. Must be positive number between 0 and 1.0."
                    })
            if 1.0 not in valid_fractions:
                valid_fractions.append(1.0)
            valid_fractions.sort()
            self.allowed_split_fractions = valid_fractions

        if not self.is_splittable and self.allowed_split_fractions != [1.0]:
            raise ValidationError({
                "allowed_split_fractions": "Non-splittable tablets cannot have fractional steps other than [1.0]."
            })

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class LooseTabletSalesConfig(models.Model):
    """
    Loose Tablet Sales & Partial Strip Dispensing Policy.
    Architecture Decision: ADR-0164 (P164).
    Requirement Trace: FR-078, FR-082, FR-085.

    Governs whether individual tablets or partial strips can be broken and sold,
    defines strip size to base pieces conversion, pricing tiers, and clinical
    prohibition reasons (e.g. moisture-sensitive hygroscopic, enteric-coated).
    """

    config_id = models.CharField(
        max_length=64,
        primary_key=True,
        default=uuid.uuid4,
        help_text="Unique configuration ID prefixed with ltsc-",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="loose_tablet_sales_configs",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="loose_tablet_sales_configs",
        help_text="Optional branch-level override; if null, applies across tenant.",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="loose_tablet_sales_configs",
        db_index=True,
    )

    allow_loose_sales = models.BooleanField(
        default=True,
        help_text="Whether dispensing loose individual tablets or partial strips is allowed for this product.",
    )
    min_loose_dispense_units = models.PositiveIntegerField(
        default=1,
        help_text="Minimum base pieces for loose dispensing.",
    )
    max_loose_dispense_units = models.PositiveIntegerField(
        default=100,
        help_text="Maximum base pieces for loose dispensing per line item.",
    )
    strip_size_pieces = models.PositiveIntegerField(
        default=10,
        validators=[MinValueValidator(1)],
        help_text="Number of individual base units contained in one full strip.",
    )
    strip_mrp = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="MRP per full strip in BDT.",
    )
    loose_unit_mrp = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="MRP per individual loose tablet in BDT.",
    )
    apply_loose_unit_surcharge = models.BooleanField(
        default=False,
        help_text="Whether a surcharge is applied for breaking strips into loose tablets.",
    )
    loose_surcharge_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
        help_text="Percentage surcharge on loose pieces.",
    )
    prohibition_reason = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Clinical, moisture-sensitivity or regulatory justification when allow_loose_sales is false.",
    )

    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Loose Tablet Sales Config"
        verbose_name_plural = "Loose Tablet Sales Configs"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "branch", "product"],
                name="unique_tenant_branch_product_loose_sales_config",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "product", "is_active"]),
            models.Index(fields=["tenant", "allow_loose_sales"]),
        ]

    def __str__(self) -> str:
        b_name = self.branch.name if self.branch else "All Branches"
        status = "ALLOWED" if self.allow_loose_sales else "PROHIBITED"
        return f"{self.product.brand_name} [{self.tenant.name} - {b_name}]: Loose Sales: {status}"

    def clean(self):
        super().clean()
        if not self.config_id:
            self.config_id = f"ltsc-{uuid.uuid4().hex[:12]}"
        elif not str(self.config_id).startswith("ltsc-"):
            self.config_id = f"ltsc-{self.config_id}"

        if not self.allow_loose_sales and not self.prohibition_reason:
            self.prohibition_reason = "Loose tablet sales prohibited for this medication (moisture/integrity guardrail)."

        if self.strip_size_pieces < 1:
            raise ValidationError({"strip_size_pieces": "Strip size must be at least 1 base unit."})

        if self.min_loose_dispense_units > self.max_loose_dispense_units:
            raise ValidationError({
                "min_loose_dispense_units": "Minimum loose units cannot exceed maximum loose units."
            })

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class NonDivisibleContainerType(models.TextChoices):
    SEALED_BOX = "SEALED_BOX", "Sealed Manufacturer Box / Carton"
    INSULIN_PEN = "INSULIN_PEN", "Pre-filled Insulin Pen / Cartridge"
    INHALER_CANISTER = "INHALER_CANISTER", "Metered Dose Inhaler / Canister"
    EYE_DROPS_BOTTLE = "EYE_DROPS_BOTTLE", "Sterile Ophthalmic Droptainer"
    TOPICAL_TUBE = "TOPICAL_TUBE", "Sealed Topical Ointment/Cream Tube"
    DIAGNOSTIC_KIT = "DIAGNOSTIC_KIT", "Unit Diagnostic Test Kit / Strips Pack"
    STERILE_VIAL_BOX = "STERILE_VIAL_BOX", "Sealed Multi-Vial/Ampoule Box"
    OTHER = "OTHER", "Other Non-Divisible Regulated Container"


class NonDivisiblePackPolicy(models.Model):
    """
    Non-Divisible Regulated Pack & Sterile Container Splitting Policy.
    Architecture Decision: ADR-0165 (P165).
    Requirement Trace: FR-078, FR-082, FR-086.

    Enforces packaging integrity for non-divisible delivery devices (insulin pens,
    inhalers, eye drop bottles, diagnostic kits, sealed sterile boxes), preventing
    unauthorized fractional dispensing or carton splitting without clinical protocols.
    """

    policy_id = models.CharField(
        max_length=64,
        primary_key=True,
        default=uuid.uuid4,
        help_text="Unique policy ID prefixed with ndpp-",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="non_divisible_pack_policies",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="non_divisible_pack_policies",
        help_text="Optional branch-level override; if null, applies across tenant.",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="non_divisible_pack_policies",
        db_index=True,
    )

    container_type = models.CharField(
        max_length=32,
        choices=NonDivisibleContainerType.choices,
        default=NonDivisibleContainerType.SEALED_BOX,
        db_index=True,
        help_text="Type of non-divisible regulated container.",
    )
    is_splitting_allowed = models.BooleanField(
        default=False,
        help_text="Whether breaking the sealed regulated container is permitted under approved rules.",
    )
    require_pharmacist_approval = models.BooleanField(
        default=True,
        help_text="If splitting is permitted, whether a licensed Pharmacist/Manager must sign off.",
    )
    approved_break_factor = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Number of approved sub-units inside container if splitting is authorized (e.g. 5 ampoules).",
    )
    sub_unit_label = models.CharField(
        max_length=32,
        default="PIECE",
        help_text="Label of the sub-unit (e.g. AMPOULE, PEN, CARTRIDGE, BOTTLE, TUBE).",
    )
    prohibition_reason = models.CharField(
        max_length=255,
        blank=True,
        default="Sealed sterile container integrity cannot be broken without approved prescription protocol.",
        help_text="Clinical justification why breaking container is prohibited.",
    )
    exemption_protocols = models.JSONField(
        default=list,
        blank=True,
        help_text="List of approved clinical exemption protocol codes (e.g. ['HOSPITAL_INPATIENT', 'DGDA_COMPASSIONATE_CARE']).",
    )

    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Non-Divisible Pack Policy"
        verbose_name_plural = "Non-Divisible Pack Policies"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "branch", "product"],
                name="unique_tenant_branch_product_non_divisible_pack_policy",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "product", "is_active"]),
            models.Index(fields=["tenant", "container_type"]),
            models.Index(fields=["tenant", "is_splitting_allowed"]),
        ]

    def __str__(self) -> str:
        b_name = self.branch.name if self.branch else "All Branches"
        status = "SPLITTABLE (RESTRICTED)" if self.is_splitting_allowed else "NON-DIVISIBLE (SEALED)"
        return f"{self.product.brand_name} [{self.tenant.name} - {b_name}]: {self.container_type} - {status}"

    def clean(self):
        super().clean()
        if not self.policy_id:
            self.policy_id = f"ndpp-{uuid.uuid4().hex[:12]}"
        elif not str(self.policy_id).startswith("ndpp-"):
            self.policy_id = f"ndpp-{self.policy_id}"

        if not self.is_splitting_allowed and not self.prohibition_reason:
            self.prohibition_reason = "Sealed sterile container integrity cannot be broken without approved prescription protocol."

        if self.approved_break_factor < 1:
            raise ValidationError({"approved_break_factor": "Approved break factor must be at least 1."})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class PurchaseCostLotStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    QUARANTINED = "QUARANTINED", "Quarantined"
    DEPLETED = "DEPLETED", "Depleted"
    ADJUSTED = "ADJUSTED", "Adjusted"
    REVERSED = "REVERSED", "Reversed"


class PurchaseCostLot(models.Model):
    """
    Architecture Decision: ADR-0167 (P167).
    Separate purchase cost lot entity from batch identity so identical batches
    received at different prices retain distinct COGS and acquisition history.
    """

    lot_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="purchase_cost_lots",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="purchase_cost_lots",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="purchase_cost_lots",
        db_index=True,
    )
    batch = models.ForeignKey(
        BatchStock,
        on_delete=models.CASCADE,
        related_name="cost_lots",
        db_index=True,
    )
    supplier = models.ForeignKey(
        "core.Supplier",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchase_cost_lots",
    )
    supplier_name = models.CharField(max_length=255, blank=True, default="")
    purchase_order_number = models.CharField(max_length=64, blank=True, default="", db_index=True)
    grn_number = models.CharField(max_length=64, blank=True, default="", db_index=True)
    invoice_reference = models.CharField(max_length=64, blank=True, default="")
    purchase_date = models.DateField(db_index=True)
    unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Exact acquisition cost per base unit in BDT (4 decimal places).",
    )
    initial_quantity = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Initial received quantity in exact integer base units.",
    )
    remaining_quantity = models.PositiveIntegerField(
        default=0,
        help_text="Remaining unconsumed quantity in exact integer base units.",
    )
    status = models.CharField(
        max_length=32,
        choices=PurchaseCostLotStatus.choices,
        default=PurchaseCostLotStatus.ACTIVE,
        db_index=True,
    )
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["purchase_date", "created_at"]
        verbose_name = "Purchase Cost Lot"
        verbose_name_plural = "Purchase Cost Lots"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(remaining_quantity__lte=models.F("initial_quantity")),
                name="check_cost_lot_remaining_lte_initial",
            ),
            models.CheckConstraint(
                condition=models.Q(unit_cost__gte=Decimal("0.0000")),
                name="check_cost_lot_positive_unit_cost",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "branch", "batch"]),
            models.Index(fields=["batch", "status", "purchase_date"]),
            models.Index(fields=["tenant", "purchase_order_number"]),
        ]

    def __str__(self) -> str:
        return f"Lot {self.lot_id} [Batch {self.batch.batch_number}]: {self.remaining_quantity}/{self.initial_quantity} @ {self.unit_cost} BDT"

    def clean(self):
        super().clean()
        if not self.lot_id:
            self.lot_id = f"pcl-{uuid.uuid4().hex[:12]}"
        elif not str(self.lot_id).startswith("pcl-"):
            self.lot_id = f"pcl-{self.lot_id}"

        if self.remaining_quantity > self.initial_quantity:
            raise ValidationError({"remaining_quantity": "Remaining quantity cannot exceed initial quantity."})

        if self.batch_id and self.branch_id:
            if hasattr(self, 'batch') and self.batch and self.batch.branch_id != self.branch_id:
                raise ValidationError({'branch': 'Cost lot branch must match batch branch.'})
            if hasattr(self, 'batch') and self.batch and self.batch.tenant_id != self.tenant_id:
                raise ValidationError({'tenant': 'Cost lot tenant must match batch tenant.'})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class GoodsReceiptStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    RECEIVED = "RECEIVED", "Received"
    VERIFIED = "VERIFIED", "Verified"
    POSTED = "POSTED", "Posted to Inventory"
    CANCELLED = "CANCELLED", "Cancelled"
    REVERSED = "REVERSED", "Reversed"


class GoodsReceiptNote(models.Model):
    """
    Architecture Decision: ADR-0168 (P168).
    Goods Received Note (GRN) capturing supplier shipments, purchase order numbers,
    and challan/invoice metadata.
    """

    grn_id = models.CharField(max_length=64, primary_key=True)
    grn_number = models.CharField(max_length=64, db_index=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="goods_receipt_notes",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="goods_receipt_notes",
        db_index=True,
    )
    supplier = models.ForeignKey(
        "core.Supplier",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="goods_receipt_notes",
    )
    supplier_name = models.CharField(max_length=255, blank=True, default="")
    purchase_order = models.ForeignKey(
        "core.PurchaseOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="goods_receipt_notes",
    )
    purchase_order_number = models.CharField(max_length=64, blank=True, default="", db_index=True)
    supplier_invoice_number = models.CharField(max_length=64, blank=True, default="", db_index=True)
    challan_number = models.CharField(max_length=64, blank=True, default="", db_index=True)
    challan_date = models.DateField(null=True, blank=True)
    receipt_date = models.DateField(db_index=True)
    storage_condition = models.CharField(max_length=64, default="ROOM_TEMPERATURE")
    storage_temperature_celsius = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    storage_check_passed = models.BooleanField(default=True)
    proof_of_origin = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=32,
        choices=GoodsReceiptStatus.choices,
        default=GoodsReceiptStatus.DRAFT,
        db_index=True,
    )
    total_received_base_units = models.PositiveIntegerField(default=0)
    total_invoice_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
    )
    ap_posting_status = models.CharField(max_length=32, default="PENDING")
    ap_invoice_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
    )
    received_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_grns",
    )
    verified_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_grns",
    )
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-receipt_date", "-created_at"]
        verbose_name = "Goods Receipt Note"
        verbose_name_plural = "Goods Receipt Notes"
        indexes = [
            models.Index(fields=["tenant", "branch", "status"]),
            models.Index(fields=["tenant", "grn_number"]),
            models.Index(fields=["tenant", "purchase_order_number"]),
        ]

    def __str__(self) -> str:
        return f"{self.grn_number} [{self.status}] - {self.supplier_name} ({self.receipt_date})"

    def clean(self):
        super().clean()
        if not self.grn_id:
            self.grn_id = f"grn-{uuid.uuid4().hex[:12]}"
        elif not str(self.grn_id).startswith("grn-"):
            self.grn_id = f"grn-{self.grn_id}"
        if not self.grn_number:
            self.grn_number = f"GRN-{uuid.uuid4().hex[:8].upper()}"

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class GoodsReceiptLine(models.Model):
    """
    Architecture Decision: ADR-0168 (P168).
    Line item within a Goods Received Note representing a received medicine product.
    """

    line_id = models.CharField(max_length=64, primary_key=True)
    grn = models.ForeignKey(
        GoodsReceiptNote,
        on_delete=models.CASCADE,
        related_name="lines",
        db_index=True,
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="goods_receipt_lines",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="goods_receipt_lines",
        db_index=True,
    )
    po_item = models.ForeignKey(
        "core.PurchaseOrderItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="grn_lines",
    )
    batch_number = models.CharField(max_length=64, blank=True, default="", db_index=True)
    expiry_date = models.DateField(null=True, blank=True, db_index=True)
    mfg_date = models.DateField(null=True, blank=True)
    pack_unit = models.CharField(max_length=32, default="BOX")
    package_size = models.PositiveIntegerField(default=1)
    received_packages = models.PositiveIntegerField(default=0)
    ordered_quantity_base_units = models.PositiveIntegerField(default=0)
    received_quantity_base_units = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0000"))],
        default=Decimal("0.0000"),
    )
    mrp_unit = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
    )
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
    )
    tax_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
    )
    line_total = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
    )
    storage_location = models.CharField(max_length=64, blank=True, default="MAIN_RACK")
    is_bonus_goods = models.BooleanField(
        default=False,
        help_text="Promotional bonus units received at zero cost liability",
    )
    excess_quantity = models.PositiveIntegerField(
        default=0,
        help_text="Units received in excess of the PO line ordered quantity",
    )
    excess_reason = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Authorized managerial justification for excess receipt",
    )
    is_quarantined = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True if line is held in quarantine due to cold-chain excursion",
    )
    is_fully_allocated = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Goods Receipt Line"
        verbose_name_plural = "Goods Receipt Lines"
        indexes = [
            models.Index(fields=["grn", "product"]),
            models.Index(fields=["tenant", "is_fully_allocated"]),
        ]

    def __str__(self) -> str:
        return f"Line {self.line_id} ({self.product.brand_name}): {self.received_quantity_base_units} units @ {self.unit_cost} BDT"

    def clean(self):
        super().clean()
        if not self.line_id:
            self.line_id = f"grnl-{uuid.uuid4().hex[:12]}"
        elif not str(self.line_id).startswith("grnl-"):
            self.line_id = f"grnl-{self.line_id}"
        if self.received_packages and self.received_packages > 0:
            calc_units = self.received_packages * max(1, self.package_size)
            if not self.received_quantity_base_units:
                self.received_quantity_base_units = calc_units
        if self.is_bonus_goods:
            self.unit_cost = Decimal("0.0000")
            self.discount_percentage = Decimal("0.00")
            self.tax_percentage = Decimal("0.00")
            self.line_total = Decimal("0.0000")
        elif not self.line_total or self.line_total == Decimal("0.0000"):
            discount_factor = Decimal("1.0000") - ((self.discount_percentage or Decimal("0.00")) / Decimal("100.00"))
            net_cost = (self.unit_cost or Decimal("0.0000")) * discount_factor
            subtotal = net_cost * Decimal(self.received_quantity_base_units or 0)
            tax = subtotal * ((self.tax_percentage or Decimal("0.00")) / Decimal("100.00"))
            self.line_total = (subtotal + tax).quantize(Decimal("0.0001"))

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class GoodsReceiptBatchLink(models.Model):
    """
    Architecture Decision: ADR-0168 (P168).
    Binds a GRN receipt line to a verified manufacturer batch and purchase cost lot
    with strict idempotency to prevent duplicate stock increments.
    """

    link_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="receipt_batch_links",
        db_index=True,
    )
    grn = models.ForeignKey(
        GoodsReceiptNote,
        on_delete=models.CASCADE,
        related_name="batch_links",
        db_index=True,
    )
    line = models.ForeignKey(
        GoodsReceiptLine,
        on_delete=models.CASCADE,
        related_name="batch_allocations",
        db_index=True,
    )
    batch = models.ForeignKey(
        BatchStock,
        on_delete=models.CASCADE,
        related_name="grn_receipt_links",
        db_index=True,
    )
    cost_lot = models.ForeignKey(
        PurchaseCostLot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="grn_links",
        db_index=True,
    )
    quantity_base_units = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0000"))],
    )
    is_posted = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True if this allocation has already incremented inventory stock.",
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Goods Receipt Batch Link"
        verbose_name_plural = "Goods Receipt Batch Links"
        constraints = [
            models.UniqueConstraint(
                fields=["line", "batch"],
                name="unique_grn_line_batch_link",
            ),
        ]
        indexes = [
            models.Index(fields=["grn", "is_posted"]),
            models.Index(fields=["line", "batch"]),
            models.Index(fields=["batch", "is_posted"]),
        ]

    def __str__(self) -> str:
        posted = "POSTED" if self.is_posted else "PENDING"
        return f"Link {self.link_id} -> Batch {self.batch.batch_number}: {self.quantity_base_units} units [{posted}]"

    def clean(self):
        super().clean()
        if not self.link_id:
            self.link_id = f"grnbl-{uuid.uuid4().hex[:12]}"
        elif not str(self.link_id).startswith("grnbl-"):
            self.link_id = f"grnbl-{self.link_id}"

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class PackagingCondition(models.TextChoices):
    INTACT_SEALED = "INTACT_SEALED", "Intact & Sealed"
    ICE_PACKS_FROZEN = "ICE_PACKS_FROZEN", "Ice Packs Fully Frozen"
    ICE_PACKS_MELTED = "ICE_PACKS_MELTED", "Ice Packs Partially / Fully Melted"
    SEAL_BROKEN = "SEAL_BROKEN", "Container Seal Tampered / Broken"
    CONTAINER_DAMAGED = "CONTAINER_DAMAGED", "Outer Container Crushed / Damaged"


class ColdChainQuarantineStatus(models.TextChoices):
    NOT_QUARANTINED = "NOT_QUARANTINED", "Not Quarantined (Within Specs)"
    PENDING_REVIEW = "PENDING_REVIEW", "Quarantined Pending Pharmacist Review"
    RELEASED = "RELEASED", "Released to Active Stock"
    RETURNED_TO_SUPPLIER = "RETURNED_TO_SUPPLIER", "Rejected & Returned to Supplier"
    DISPOSED = "DISPOSED", "Condemned & Disposed"


class ReceiptColdChainEvidence(models.Model):
    """
    Architecture Decision: ADR-0189 (P189).
    Phase 10: Suppliers, Purchase Orders & Supply Chain.
    Captures empirical cold-chain receipt evidence, temperature logger readings,
    excursion detections, and guarded quarantine review transitions.
    """

    evidence_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="cold_chain_evidences",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="cold_chain_evidences",
        db_index=True,
    )
    grn = models.ForeignKey(
        GoodsReceiptNote,
        on_delete=models.CASCADE,
        related_name="cold_chain_evidences",
        db_index=True,
    )
    grn_line = models.ForeignKey(
        GoodsReceiptLine,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="cold_chain_evidences",
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="cold_chain_evidences",
    )
    batch_number = models.CharField(max_length=64, db_index=True)

    # Temperature readings
    logger_serial_number = models.CharField(max_length=64, blank=True, default="")
    recorded_temperature_celsius = models.DecimalField(max_digits=5, decimal_places=2)
    min_temperature_celsius = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    max_temperature_celsius = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    # Excursion & Packaging
    excursion_detected = models.BooleanField(default=False, db_index=True)
    excursion_duration_minutes = models.PositiveIntegerField(default=0)
    packaging_condition = models.CharField(
        max_length=32,
        choices=PackagingCondition.choices,
        default=PackagingCondition.INTACT_SEALED,
    )
    carrier_name = models.CharField(max_length=128, blank=True, default="")
    vehicle_type = models.CharField(max_length=64, blank=True, default="REFRIGERATED_VAN")
    proof_attachment_reference = models.CharField(max_length=255, blank=True, default="")
    inspection_notes = models.TextField(blank=True, default="")

    # Quarantine & Review Workflow
    quarantine_status = models.CharField(
        max_length=32,
        choices=ColdChainQuarantineStatus.choices,
        default=ColdChainQuarantineStatus.NOT_QUARANTINED,
        db_index=True,
    )
    quarantine_reason = models.CharField(max_length=255, blank=True, default="")
    reviewed_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_cold_chain_evidences",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True, default="")
    resolution_action = models.CharField(max_length=32, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Receipt Cold-Chain Evidence"
        verbose_name_plural = "Receipt Cold-Chain Evidences"
        indexes = [
            models.Index(fields=["tenant", "branch", "quarantine_status"]),
            models.Index(fields=["tenant", "batch_number"]),
            models.Index(fields=["grn", "excursion_detected"]),
        ]

    def __str__(self) -> str:
        return f"ColdChainEvidence {self.evidence_id}: Batch {self.batch_number} ({self.recorded_temperature_celsius}°C - {self.quarantine_status})"

    def clean(self):
        super().clean()
        if not self.evidence_id:
            self.evidence_id = f"cce-{uuid.uuid4().hex[:12]}"
        elif not str(self.evidence_id).startswith("cce-"):
            self.evidence_id = f"cce-{self.evidence_id}"

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class InvoiceMatchingStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    MATCHED = "MATCHED", "Matched (Ready for AP)"
    DISCREPANCY_QUEUE = "DISCREPANCY_QUEUE", "Discrepancy Exception Queue"
    APPROVED_EXCEPTION = "APPROVED_EXCEPTION", "Approved Exception"
    REJECTED = "REJECTED", "Rejected / Disputed"
    POSTED_TO_AP = "POSTED_TO_AP", "Posted to Accounts Payable"


class ItemMatchStatus(models.TextChoices):
    MATCHED = "MATCHED", "Matched"
    PRICE_VARIANCE = "PRICE_VARIANCE", "Price Variance"
    QUANTITY_OVER_BILLING = "QUANTITY_OVER_BILLING", "Quantity Over-Billing"
    QUANTITY_UNDER_BILLING = "QUANTITY_UNDER_BILLING", "Quantity Under-Billing"
    UNMATCHED_ITEM = "UNMATCHED_ITEM", "Unmatched Item"


class SupplierInvoice(models.Model):
    """
    Architecture Decision: ADR-0190 (P190).
    3-Way matching between Purchase Order (PO), Goods Receipt Note (GRN), and Supplier Invoice.
    Enforces variance tolerances, discrepancy routing, exception approvals, and AP ledger posting.
    """

    invoice_id = models.CharField(max_length=64, primary_key=True)
    invoice_number = models.CharField(max_length=64, db_index=True, help_text="Vendor invoice/bill reference number")
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="supplier_invoices",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="supplier_invoices",
        db_index=True,
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="invoices",
        db_index=True,
    )
    supplier_name = models.CharField(max_length=255, blank=True, default="")
    purchase_order = models.ForeignKey(
        "core.PurchaseOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supplier_invoices",
    )
    purchase_order_number = models.CharField(max_length=64, blank=True, default="", db_index=True)
    goods_receipt = models.ForeignKey(
        GoodsReceiptNote,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supplier_invoices",
    )
    goods_receipt_number = models.CharField(max_length=64, blank=True, default="", db_index=True)
    invoice_date = models.DateField(db_index=True)
    due_date = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=8, default="BDT")
    subtotal_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
    )
    tax_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
    )
    discount_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
    )
    total_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
    )
    matching_status = models.CharField(
        max_length=32,
        choices=InvoiceMatchingStatus.choices,
        default=InvoiceMatchingStatus.DRAFT,
        db_index=True,
    )
    has_discrepancy = models.BooleanField(default=False)
    discrepancy_reasons = models.JSONField(default=list, blank=True)
    price_variance_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
    )
    quantity_variance_units = models.IntegerField(default=0)
    total_variance_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
    )
    tolerated_price_variance_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("1.50"),
        help_text="Maximum allowed price variance percentage before flagging discrepancy",
    )
    tolerated_amount_variance_bdt = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal("50.0000"),
        help_text="Fixed amount threshold for rounding/tax difference tolerance",
    )
    exception_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_supplier_invoices",
    )
    exception_approved_at = models.DateTimeField(null=True, blank=True)
    exception_justification = models.TextField(blank=True, default="")
    rejection_reason = models.TextField(blank=True, default="")
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rejected_supplier_invoices",
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    posted_to_ap_at = models.DateTimeField(null=True, blank=True)
    ap_ledger_entry = models.ForeignKey(
        SupplierLedgerEntry,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices",
    )
    proof_attachment_reference = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_supplier_invoices",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Supplier Invoice"
        verbose_name_plural = "Supplier Invoices"
        unique_together = ("tenant", "supplier", "invoice_number")
        indexes = [
            models.Index(fields=["tenant", "matching_status"]),
            models.Index(fields=["tenant", "supplier", "invoice_date"]),
            models.Index(fields=["tenant", "has_discrepancy"]),
        ]

    def __str__(self) -> str:
        return f"SupplierInvoice {self.invoice_number} ({self.supplier_name}) - [{self.matching_status}]"

    def clean(self):
        super().clean()
        if not self.invoice_id:
            self.invoice_id = f"si-{uuid.uuid4().hex[:12]}"
        elif not str(self.invoice_id).startswith("si-"):
            self.invoice_id = f"si-{self.invoice_id}"

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class SupplierInvoiceItem(models.Model):
    """
    Architecture Decision: ADR-0190 (P190).
    Line items of a supplier invoice matching PO item and GRN line.
    """

    item_id = models.CharField(max_length=64, primary_key=True)
    invoice = models.ForeignKey(
        SupplierInvoice,
        on_delete=models.CASCADE,
        related_name="items",
        db_index=True,
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="supplier_invoice_items",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.PROTECT,
        related_name="supplier_invoice_items",
    )
    po_item = models.ForeignKey(
        "core.PurchaseOrderItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoice_items",
    )
    grn_line = models.ForeignKey(
        GoodsReceiptLine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoice_items",
    )
    batch_number = models.CharField(max_length=64, blank=True, default="", db_index=True)
    billed_quantity = models.PositiveIntegerField(default=1)
    received_quantity = models.PositiveIntegerField(default=0)
    ordered_quantity = models.PositiveIntegerField(default=0)
    billed_unit_price = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
    )
    po_unit_price = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
    )
    line_subtotal = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
    )
    line_tax_percentage = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    line_discount_percentage = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    line_total = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
    )
    match_status = models.CharField(
        max_length=32,
        choices=ItemMatchStatus.choices,
        default=ItemMatchStatus.MATCHED,
    )
    price_variance = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
    )
    quantity_variance = models.IntegerField(default=0)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["item_id"]
        verbose_name = "Supplier Invoice Item"
        verbose_name_plural = "Supplier Invoice Items"
        indexes = [
            models.Index(fields=["invoice", "product"]),
            models.Index(fields=["tenant", "match_status"]),
        ]

    def __str__(self) -> str:
        return f"SupplierInvoiceItem {self.item_id}: {self.product.brand_name} (Qty: {self.billed_quantity} @ {self.billed_unit_price})"

    def clean(self):
        super().clean()
        if not self.item_id:
            self.item_id = f"sii-{uuid.uuid4().hex[:12]}"
        elif not str(self.item_id).startswith("sii-"):
            self.item_id = f"sii-{self.item_id}"

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class StockReservationStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active Reserved"
    COMMITTED = "COMMITTED", "Committed / Sold"
    RELEASED = "RELEASED", "Released / Cancelled"
    EXPIRED = "EXPIRED", "Expired / Timed Out"


class StockReservation(models.Model):
    """
    Architecture Decision: ADR-0169 (P169).
    Pessimistic concurrency reservation for FEFO allocated batch stock.
    Prevents race conditions, double reservations, and checkout stockouts.
    """

    reservation_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="stock_reservations",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="stock_reservations",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="stock_reservations",
        db_index=True,
    )
    batch = models.ForeignKey(
        BatchStock,
        on_delete=models.CASCADE,
        related_name="reservations",
        db_index=True,
    )
    quantity_base_units = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(
        max_length=32,
        choices=StockReservationStatus.choices,
        default=StockReservationStatus.ACTIVE,
        db_index=True,
    )
    reference_type = models.CharField(
        max_length=64,
        blank=True,
        default="POS_CART",
        help_text="Context: POS_CART, PRESCRIPTION, DISPENSING, TRANSFER",
    )
    reference_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_by = models.ForeignKey(
        "identity.PharmacyUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_reservations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Stock Reservation"
        verbose_name_plural = "Stock Reservations"
        indexes = [
            models.Index(fields=["tenant", "branch", "status"]),
            models.Index(fields=["batch", "status"]),
            models.Index(fields=["reference_type", "reference_id"]),
            models.Index(fields=["expires_at", "status"]),
        ]

    def __str__(self) -> str:
        return f"Reservation {self.reservation_id} -> {self.batch.batch_number}: {self.quantity_base_units} units [{self.status}]"

    def clean(self):
        super().clean()
        if not self.reservation_id:
            self.reservation_id = f"resv-{uuid.uuid4().hex[:12]}"
        elif not str(self.reservation_id).startswith("resv-"):
            self.reservation_id = f"resv-{self.reservation_id}"

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

class SaleLotAllocationStatus(models.TextChoices):
    COMMITTED = "COMMITTED", "Committed / Sold"
    PARTIALLY_RETURNED = "PARTIALLY_RETURNED", "Partially Returned"
    FULLY_RETURNED = "FULLY_RETURNED", "Fully Returned"
    REVERSED = "REVERSED", "Reversed / Voided"


class SaleLotAllocation(models.Model):
    """
    Architecture Decision: ADR-0170 (P170).
    Sale Item Lot Allocation Record binding cashier invoice lines to exact manufacturer
    batches and purchase cost lots for DGDA recall traceability, return restock,
    and unblended GAAP-compliant gross margin derivations.
    """

    allocation_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="sale_lot_allocations",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="sale_lot_allocations",
        db_index=True,
    )
    invoice = models.ForeignKey(
        SaleInvoice,
        on_delete=models.CASCADE,
        related_name="lot_allocations",
        db_index=True,
    )
    invoice_item = models.ForeignKey(
        SaleInvoiceItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lot_allocations",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.PROTECT,
        related_name="sale_lot_allocations",
        db_index=True,
    )
    batch = models.ForeignKey(
        BatchStock,
        on_delete=models.PROTECT,
        related_name="sale_lot_allocations",
        db_index=True,
    )
    cost_lot = models.ForeignKey(
        PurchaseCostLot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sale_allocations",
        db_index=True,
    )
    quantity_base_units = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    returned_quantity_base_units = models.PositiveIntegerField(default=0)
    unit_selling_price = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0000"))],
    )
    unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0000"))],
    )
    total_selling_price = models.DecimalField(max_digits=14, decimal_places=4)
    total_cost = models.DecimalField(max_digits=14, decimal_places=4)
    gross_profit = models.DecimalField(max_digits=14, decimal_places=4)
    profit_margin_pct = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(
        max_length=32,
        choices=SaleLotAllocationStatus.choices,
        default=SaleLotAllocationStatus.COMMITTED,
        db_index=True,
    )
    is_returned = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Sale Lot Allocation"
        verbose_name_plural = "Sale Lot Allocations"
        indexes = [
            models.Index(fields=["tenant", "branch", "created_at"]),
            models.Index(fields=["batch", "created_at"]),
            models.Index(fields=["cost_lot", "created_at"]),
            models.Index(fields=["invoice", "status"]),
            models.Index(fields=["tenant", "is_returned"]),
        ]

    def __str__(self) -> str:
        return f"SaleAllocation {self.allocation_id} -> Inv {self.invoice.invoice_number} Batch {self.batch.batch_number}: {self.quantity_base_units} units [Profit: {self.gross_profit}]"

    def clean(self):
        super().clean()
        if not self.allocation_id:
            self.allocation_id = f"sall-{uuid.uuid4().hex[:12]}"
        elif not str(self.allocation_id).startswith("sall-"):
            self.allocation_id = f"sall-{self.allocation_id}"

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)



class BranchStockAvailability(models.Model):
    """
    Architecture Decision: ADR-0171 (P171).
    Authoritative Branch-Level Multi-Tier Stock Availability Projection Matrix.
    Distinguishes on-hand, reserved, quarantined, pending, physically available,
    and server-confirmed available quantities with stock conservation invariants.
    """

    availability_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="stock_availabilities",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="stock_availabilities",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="branch_availabilities",
        db_index=True,
    )
    quantity_on_hand = models.PositiveIntegerField(
        default=0,
        help_text="Total physical base units in all branch batches regardless of status.",
    )
    quantity_reserved = models.PositiveIntegerField(
        default=0,
        help_text="Base units locked in active reservations or unfinalized POS carts.",
    )
    quantity_quarantined = models.PositiveIntegerField(
        default=0,
        help_text="Base units in quarantined, recalled, or expired batches.",
    )
    quantity_pending_inward = models.PositiveIntegerField(
        default=0,
        help_text="Base units on unposted/draft goods receipts in transit or pending verification.",
    )
    quantity_physically_available = models.PositiveIntegerField(
        default=0,
        help_text="Stock physically on shelf minus quarantined: max(0, on_hand - quarantined).",
    )
    quantity_server_confirmed_available = models.PositiveIntegerField(
        default=0,
        help_text="Guaranteed central stock immediately sellable: max(0, physically_available - reserved).",
    )
    reorder_level_base_units = models.PositiveIntegerField(
        default=0,
        help_text="Reorder threshold for branch warning triggers.",
    )
    is_out_of_stock = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True when quantity_server_confirmed_available == 0.",
    )
    is_below_reorder_level = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True when server_confirmed_available <= reorder_level_base_units > 0.",
    )
    version = models.PositiveIntegerField(
        default=1,
        help_text="Optimistic concurrency tracking version counter.",
    )
    last_recalculated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp of last authoritative calculation sync.",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product__brand_name", "-updated_at"]
        verbose_name = "Branch Stock Availability"
        verbose_name_plural = "Branch Stock Availabilities"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "branch", "product"],
                name="unique_branch_product_stock_availability",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "branch", "product"]),
            models.Index(fields=["branch", "is_out_of_stock"]),
            models.Index(fields=["branch", "is_below_reorder_level"]),
            models.Index(fields=["tenant", "updated_at"]),
        ]

    def __str__(self) -> str:
        return (
            f"BranchAvailability {self.branch.name} - {self.product.brand_name}: "
            f"OnHand={self.quantity_on_hand}, Avail={self.quantity_server_confirmed_available}, "
            f"Resv={self.quantity_reserved}, Quarantined={self.quantity_quarantined}"
        )

    def clean(self):
        super().clean()
        if not self.availability_id:
            self.availability_id = f"bav-{uuid.uuid4().hex[:12]}"
        elif not str(self.availability_id).startswith("bav-"):
            self.availability_id = f"bav-{self.availability_id}"

        # Enforce stock conservation invariant
        self.quantity_physically_available = max(
            0, self.quantity_on_hand - self.quantity_quarantined
        )
        self.quantity_server_confirmed_available = max(
            0, self.quantity_physically_available - self.quantity_reserved
        )
        self.is_out_of_stock = bool(self.quantity_server_confirmed_available == 0)
        self.is_below_reorder_level = bool(
            self.reorder_level_base_units > 0
            and self.quantity_server_confirmed_available <= self.reorder_level_base_units
        )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class BatchExpiryPolicy(models.Model):
    """
    Architecture Decision: ADR-0172 (P172).
    Configurable batch expiry alert windows and sale cutoff guard.
    Enforces multi-tier alerts (warning, critical, quarantine, cutoff)
    and blocks sales of expired or cutoff-breached medicines.
    """

    policy_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="batch_expiry_policies",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="batch_expiry_policies",
        db_index=True,
        help_text="Null designates tenant-wide fallback policy.",
    )
    warning_lead_days = models.PositiveIntegerField(
        default=90,
        validators=[MinValueValidator(1)],
        help_text="Early warning tier (Yellow alert) threshold in days.",
    )
    critical_lead_days = models.PositiveIntegerField(
        default=30,
        validators=[MinValueValidator(1)],
        help_text="Critical near-expiry tier (Amber alert) threshold in days.",
    )
    quarantine_lead_days = models.PositiveIntegerField(
        default=15,
        validators=[MinValueValidator(0)],
        help_text="Urgent disposition tier (Red alert) threshold in days.",
    )
    sale_cutoff_lead_days = models.PositiveIntegerField(
        default=7,
        validators=[MinValueValidator(0)],
        help_text="Mandatory checkout sale cutoff lead threshold in days.",
    )
    auto_quarantine_on_cutoff = models.BooleanField(
        default=True,
        help_text="Automatically update batch to is_quarantined=True when cutoff breached.",
    )
    allow_manager_override_near_expiry = models.BooleanField(
        default=False,
        help_text="Allow authorized manager override for near-expiry (never past actual expiry).",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Batch Expiry Policy"
        verbose_name_plural = "Batch Expiry Policies"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "branch"],
                name="unique_tenant_branch_expiry_policy",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "branch", "is_active"]),
            models.Index(fields=["tenant", "is_active"]),
        ]

    def __str__(self) -> str:
        branch_label = self.branch.name if self.branch else "Tenant-Wide"
        return f"ExpiryPolicy {self.policy_id} [{branch_label}]: Warning={self.warning_lead_days}d, Critical={self.critical_lead_days}d, Cutoff={self.sale_cutoff_lead_days}d"

    def clean(self):
        super().clean()
        if not self.policy_id:
            self.policy_id = f"expol-{uuid.uuid4().hex[:12]}"
        elif not str(self.policy_id).startswith("expol-"):
            self.policy_id = f"expol-{self.policy_id}"

        if self.critical_lead_days > self.warning_lead_days:
            raise ValidationError(
                {"critical_lead_days": "Critical lead days cannot exceed warning lead days."}
            )
        if self.quarantine_lead_days > self.critical_lead_days:
            raise ValidationError(
                {"quarantine_lead_days": "Quarantine lead days cannot exceed critical lead days."}
            )
        if self.sale_cutoff_lead_days > self.quarantine_lead_days:
            raise ValidationError(
                {"sale_cutoff_lead_days": "Sale cutoff lead days cannot exceed quarantine lead days."}
            )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class LandedCostExpenseType(models.TextChoices):
    TRANSPORT_FREIGHT = "TRANSPORT_FREIGHT", "Transport & Freight"
    HANDLING_LABOR = "HANDLING_LABOR", "Handling & Labor"
    INSURANCE_TARIFF = "INSURANCE_TARIFF", "Transit Insurance & Tariffs"
    COLD_CHAIN_LOGISTICS = "COLD_CHAIN_LOGISTICS", "Cold Chain Logistics"
    REGULATORY_FEE = "REGULATORY_FEE", "Regulatory Quality Inspection"
    OTHER = "OTHER", "Other Landed Surcharge"


class LandedCostAllocationMethod(models.TextChoices):
    BY_VALUE = "BY_VALUE", "Pro-Rata by Purchase Value"
    BY_QUANTITY = "BY_QUANTITY", "Pro-Rata by Received Base Quantity"
    MANUAL_EQUAL = "MANUAL_EQUAL", "Equal Division across Lots"


class LandedCostStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft Calculation"
    POSTED = "POSTED", "Posted & Capitalized to Lots"
    REVERSED = "REVERSED", "Reversed"


class LandedCostExpense(models.Model):
    """
    Architecture Decision: ADR-0176 (P176).
    Captures landed transport, freight, handling, and logistics costs
    capitalized across received purchase lots per IAS 2 / GAAP rules.
    """

    expense_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="landed_costs",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="landed_costs",
        db_index=True,
    )
    grn = models.ForeignKey(
        "GoodsReceiptNote",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="landed_costs",
        db_index=True,
    )
    expense_type = models.CharField(
        max_length=32,
        choices=LandedCostExpenseType.choices,
        default=LandedCostExpenseType.TRANSPORT_FREIGHT,
        db_index=True,
    )
    allocation_method = models.CharField(
        max_length=32,
        choices=LandedCostAllocationMethod.choices,
        default=LandedCostAllocationMethod.BY_VALUE,
    )
    total_expense_bdt = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0100"))],
        help_text="Total landed expense in BDT to be absorbed by inventory.",
    )
    invoice_ref = models.CharField(max_length=128, blank=True, default="", db_index=True)
    reason = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=32,
        choices=LandedCostStatus.choices,
        default=LandedCostStatus.POSTED,
        db_index=True,
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recorded_landed_costs",
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    reversal_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Landed Cost Expense"
        verbose_name_plural = "Landed Cost Expenses"
        indexes = [
            models.Index(fields=["tenant", "branch", "status"]),
            models.Index(fields=["grn", "status"]),
        ]

    def __str__(self) -> str:
        return f"LandedCost {self.expense_id} - {self.expense_type} ({self.total_expense_bdt} BDT) [{self.status}]"

    def clean(self):
        super().clean()
        if not self.expense_id:
            self.expense_id = f"landed-{uuid.uuid4().hex[:12]}"
        elif not str(self.expense_id).startswith("landed-"):
            self.expense_id = f"landed-{self.expense_id}"

        if self.total_expense_bdt is not None and self.total_expense_bdt <= Decimal("0.0000"):
            raise ValidationError({"total_expense_bdt": "Landed expense must be strictly positive."})

        if self.branch_id and self.tenant_id:
            if hasattr(self, "branch") and self.branch and self.branch.tenant_id != self.tenant_id:
                raise ValidationError({"branch": "Branch must belong to the same tenant as the landed expense."})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class LandedCostAllocationLine(models.Model):
    """
    Architecture Decision: ADR-0176 (P176).
    Line item connecting an absorbed landed expense share to a specific PurchaseCostLot and BatchStock.
    """

    allocation_id = models.CharField(max_length=64, primary_key=True)
    expense = models.ForeignKey(
        LandedCostExpense,
        on_delete=models.CASCADE,
        related_name="lines",
        db_index=True,
    )
    cost_lot = models.ForeignKey(
        PurchaseCostLot,
        on_delete=models.CASCADE,
        related_name="landed_cost_allocations",
        db_index=True,
    )
    batch = models.ForeignKey(
        BatchStock,
        on_delete=models.CASCADE,
        related_name="landed_cost_allocations",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.CASCADE,
        related_name="landed_cost_allocations",
        db_index=True,
    )
    lot_base_unit_cost_before = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        help_text="Original invoice acquisition unit cost prior to landed expense absorption.",
    )
    allocated_expense_amount = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        help_text="Exact currency amount of landed expense absorbed by this lot.",
    )
    allocated_cost_per_unit = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        help_text="Absorbed landed expense divided by lot initial integer base units.",
    )
    lot_landed_unit_cost_after = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        help_text="Final capitalized unit cost = base_unit_cost_before + allocated_cost_per_unit.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Landed Cost Allocation Line"
        verbose_name_plural = "Landed Cost Allocation Lines"
        indexes = [
            models.Index(fields=["expense", "cost_lot"]),
            models.Index(fields=["cost_lot", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"Alloc {self.allocation_id}: Lot {self.cost_lot_id} (+{self.allocated_cost_per_unit} BDT/unit)"

    def clean(self):
        super().clean()
        if not self.allocation_id:
            self.allocation_id = f"lcalloc-{uuid.uuid4().hex[:12]}"
        elif not str(self.allocation_id).startswith("lcalloc-"):
            self.allocation_id = f"lcalloc-{self.allocation_id}"

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)



class ReturnInspectionDisposition(models.TextChoices):
    RESTOCK_APPROVED = "RESTOCK_APPROVED", "Restock to Original Batch & Lot"
    QUARANTINE_HOLD = "QUARANTINE_HOLD", "Quarantine Hold for QA Investigation"
    DAMAGED_DESTROY = "DAMAGED_DESTROY", "Damaged / Disposed - Do Not Restock"


class ReturnSafetyInspection(models.Model):
    """
    Architecture Decision: ADR-0177 (P177).
    Records formal pharmacist safety inspection outcomes for sales returns,
    enforcing clinical safety checks before restocking to exact batch & cost lot,
    and routing uncertain products to quarantine hold.
    """

    inspection_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="return_inspections",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="return_inspections",
        db_index=True,
    )
    allocation = models.ForeignKey(
        SaleLotAllocation,
        on_delete=models.CASCADE,
        related_name="inspections",
        db_index=True,
    )
    invoice = models.ForeignKey(
        SaleInvoice,
        on_delete=models.CASCADE,
        related_name="return_inspections",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.PROTECT,
        related_name="return_inspections",
        db_index=True,
    )
    batch = models.ForeignKey(
        BatchStock,
        on_delete=models.PROTECT,
        related_name="return_inspections",
        db_index=True,
    )
    cost_lot = models.ForeignKey(
        PurchaseCostLot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="return_inspections",
        db_index=True,
    )
    returned_quantity_base_units = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    disposition = models.CharField(
        max_length=32,
        choices=ReturnInspectionDisposition.choices,
        db_index=True,
    )
    is_seal_intact = models.BooleanField(default=True)
    is_packaging_undamaged = models.BooleanField(default=True)
    is_storage_condition_verified = models.BooleanField(default=True)
    is_unexpired = models.BooleanField(default=True)
    temperature_reading_celsius = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Optional recorded temperature in Celsius for cold-chain returns.",
    )
    inspector = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="return_inspections",
    )
    customer_reason = models.TextField(blank=True, default="")
    inspection_notes = models.TextField(blank=True, default="")
    refund_amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
    )
    restocked_cogs = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
    )
    is_reversed = models.BooleanField(default=False, db_index=True)
    reversal_reason = models.TextField(blank=True, default="")
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reversed_return_inspections",
    )
    reversed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Return Safety Inspection"
        verbose_name_plural = "Return Safety Inspections"
        indexes = [
            models.Index(fields=["tenant", "branch", "-created_at"]),
            models.Index(fields=["allocation", "disposition"]),
            models.Index(fields=["batch", "disposition"]),
            models.Index(fields=["cost_lot", "disposition"]),
        ]

    def __str__(self) -> str:
        return f"ReturnInspection {self.inspection_id} [{self.disposition}] - {self.returned_quantity_base_units} units"

    def clean(self):
        super().clean()
        if not self.inspection_id:
            self.inspection_id = f"retinsp-{uuid.uuid4().hex[:12]}"
        elif not str(self.inspection_id).startswith("retinsp-"):
            self.inspection_id = f"retinsp-{self.inspection_id}"

        if self.branch_id and self.tenant_id:
            if hasattr(self, "branch") and self.branch and self.branch.tenant_id != self.tenant_id:
                raise ValidationError({"branch": "Branch must belong to the same tenant as the inspection."})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)



class BatchMergeRecord(models.Model):
    """
    Architecture Decision: ADR-0178 (P178).
    Captures audited batch consolidation events, enforcing strict compatibility guards
    against merging batches with differing expiry dates, manufacturer lot numbers, or recall/quarantine states.
    """

    merge_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="batch_merges",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="batch_merges",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.PROTECT,
        related_name="batch_merges",
        db_index=True,
    )
    source_batch = models.ForeignKey(
        BatchStock,
        on_delete=models.PROTECT,
        related_name="merges_as_source",
        db_index=True,
    )
    target_batch = models.ForeignKey(
        BatchStock,
        on_delete=models.PROTECT,
        related_name="merges_as_target",
        db_index=True,
    )
    merged_quantity_base_units = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    source_cost_per_unit_before = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        help_text="Unit cost of source batch immediately prior to merge consolidation.",
    )
    target_cost_per_unit_before = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        help_text="Unit cost of target batch immediately prior to merge consolidation.",
    )
    target_cost_per_unit_after = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        help_text="Weighted average unit cost calculated for target batch following merge.",
    )
    reason = models.TextField(help_text="Audited business justification for batch consolidation.")
    merged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="batch_merges",
    )
    is_reversed = models.BooleanField(default=False, db_index=True)
    reversal_reason = models.TextField(blank=True, default="")
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reversed_batch_merges",
    )
    reversed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Batch Merge Record"
        verbose_name_plural = "Batch Merge Records"
        indexes = [
            models.Index(fields=["tenant", "branch", "-created_at"]),
            models.Index(fields=["source_batch", "created_at"]),
            models.Index(fields=["target_batch", "created_at"]),
            models.Index(fields=["product", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"BatchMerge {self.merge_id}: {self.source_batch_id} -> {self.target_batch_id} ({self.merged_quantity_base_units} units)"

    def clean(self):
        super().clean()
        if not self.merge_id:
            self.merge_id = f"bmerge-{uuid.uuid4().hex[:12]}"
        elif not str(self.merge_id).startswith("bmerge-"):
            self.merge_id = f"bmerge-{self.merge_id}"

        if self.branch_id and self.tenant_id:
            if hasattr(self, "branch") and self.branch and self.branch.tenant_id != self.tenant_id:
                raise ValidationError({"branch": "Branch must belong to the same tenant as the merge record."})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class StockCostingMethod(models.TextChoices):
    FIFO_ACTUAL = "FIFO_ACTUAL", "FIFO Actual Lot Cost"
    WEIGHTED_AVERAGE = "WEIGHTED_AVERAGE", "Weighted Average Batch Cost"


class StockValuationSnapshot(models.Model):
    """
    Architecture Decision: ADR-0179 (P179).
    Audited point-in-time stock valuation snapshot capturing FIFO actual or weighted average
    cost valuation, retail/MRP valuation, potential gross margin, 5-tier aging analysis,
    and realized margin from immutable lot allocations.
    """

    snapshot_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="valuation_snapshots",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="valuation_snapshots",
        db_index=True,
    )
    valuation_date = models.DateField(db_index=True)
    costing_method = models.CharField(
        max_length=32,
        choices=StockCostingMethod.choices,
        default=StockCostingMethod.FIFO_ACTUAL,
        db_index=True,
    )
    total_products_count = models.PositiveIntegerField(default=0)
    total_quantity_base_units = models.PositiveIntegerField(default=0)
    total_cost_valuation_bdt = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Total acquisition cost of inventory in BDT.",
    )
    total_retail_valuation_bdt = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0000"))],
        help_text="Total retail/MRP potential selling value in BDT.",
    )
    potential_gross_margin_bdt = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        help_text="Potential gross profit in BDT (retail - cost).",
    )
    potential_margin_pct = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        help_text="Potential gross margin percentage.",
    )
    aging_summary = models.JSONField(
        default=dict,
        blank=True,
        help_text="5-tier inventory aging buckets (0-30, 31-60, 61-90, 91-180, 181+ days).",
    )
    realized_revenue_bdt = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        help_text="Realized sales revenue from committed lot allocations.",
    )
    realized_cogs_bdt = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        help_text="Realized cost of goods sold from committed lot allocations.",
    )
    realized_gross_profit_bdt = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
        help_text="Realized gross profit from committed lot allocations.",
    )
    realized_margin_pct = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Realized profit margin percentage.",
    )
    product_breakdown = models.JSONField(
        default=list,
        blank=True,
        help_text="Itemized product-level stock valuation metrics.",
    )
    notes = models.TextField(blank=True, default="")
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="valuation_snapshots",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-valuation_date", "-created_at"]
        verbose_name = "Stock Valuation Snapshot"
        verbose_name_plural = "Stock Valuation Snapshots"
        indexes = [
            models.Index(fields=["tenant", "branch", "-valuation_date"]),
            models.Index(fields=["tenant", "costing_method", "valuation_date"]),
        ]

    def __str__(self) -> str:
        return f"ValuationSnapshot {self.snapshot_id}: {self.branch.name} on {self.valuation_date} ({self.costing_method}) - BDT {self.total_cost_valuation_bdt}"

    def clean(self):
        super().clean()
        if not self.snapshot_id:
            self.snapshot_id = f"valsnap-{uuid.uuid4().hex[:12]}"
        elif not str(self.snapshot_id).startswith("valsnap-"):
            self.snapshot_id = f"valsnap-{self.snapshot_id}"

        if self.branch_id and self.tenant_id:
            if hasattr(self, "branch") and self.branch and self.branch.tenant_id != self.tenant_id:
                raise ValidationError({"branch": "Branch must belong to the same tenant as the valuation snapshot."})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class PackagingConcurrencyAuditRecord(models.Model):
    """
    Architecture Decision: ADR-0180 (P180).
    Audited stress-test execution record capturing multi-threaded packaging conversions,
    concurrent checkouts, split lot allocations, and mathematical stock conservation proof.
    """

    test_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="concurrency_stress_audits",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="concurrency_stress_audits",
        db_index=True,
    )
    product = models.ForeignKey(
        MedicineProduct,
        on_delete=models.PROTECT,
        related_name="concurrency_stress_audits",
        db_index=True,
    )
    concurrency_level = models.PositiveIntegerField(
        default=10,
        help_text="Number of simulated simultaneous threads/cashier terminals.",
    )
    total_requests = models.PositiveIntegerField(
        default=10,
        help_text="Total allocation and checkout operations attempted.",
    )
    successful_transactions = models.PositiveIntegerField(
        default=0,
        help_text="Count of checkouts successfully committed without collision.",
    )
    rejected_oversells = models.PositiveIntegerField(
        default=0,
        help_text="Count of transactions cleanly rejected due to stock depletion (zero overselling).",
    )
    initial_stock_base_units = models.PositiveIntegerField(
        default=0,
        help_text="Physical inventory on-hand immediately prior to stress execution.",
    )
    final_stock_base_units = models.PositiveIntegerField(
        default=0,
        help_text="Physical inventory remaining on-hand immediately following stress execution.",
    )
    total_allocated_base_units = models.PositiveIntegerField(
        default=0,
        help_text="Cumulative base units successfully allocated to checkouts.",
    )
    stock_conserved = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Asserts initial_stock == final_stock + total_allocated (Zero stock drift).",
    )
    fefo_order_verified = models.BooleanField(
        default=True,
        help_text="Asserts candidate lots were consumed in strict earliest-expiry order.",
    )
    integer_units_verified = models.BooleanField(
        default=True,
        help_text="Asserts all package conversions maintained indivisible whole integer base units.",
    )
    execution_time_ms = models.PositiveIntegerField(
        default=0,
        help_text="Total execution duration of concurrent stress test in milliseconds.",
    )
    notes = models.TextField(blank=True, default="")
    tested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="concurrency_stress_audits",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Packaging Concurrency Audit Record"
        verbose_name_plural = "Packaging Concurrency Audit Records"
        indexes = [
            models.Index(fields=["tenant", "branch", "-created_at"]),
            models.Index(fields=["product", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"ConcurrencyAudit {self.test_id}: {self.product.brand_name} (Threads: {self.concurrency_level}, Conserved: {self.stock_conserved})"

    def clean(self):
        super().clean()
        if not self.test_id:
            self.test_id = f"cstress-{uuid.uuid4().hex[:12]}"
        elif not str(self.test_id).startswith("cstress-"):
            self.test_id = f"cstress-{self.test_id}"

        if self.branch_id and self.tenant_id:
            if hasattr(self, "branch") and self.branch and self.branch.tenant_id != self.tenant_id:
                raise ValidationError({"branch": "Branch must belong to the same tenant as the audit record."})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)
