"""
apps/core/plan_catalogue_service.py
Architecture Decision: ADR-0015 & ADR-0081
Phase 05: Basic/Standard/Pro Subscription & Billing (P081)

Plan Catalogue & Applied Entitlement Orchestration:
- Canonical tier pricing in BDT with Decimal precision.
- Mandatory clinical safety non-gating invariant (INV-SAFETY-01).
- Versioned plan catalogue specifications.
- Immutable tenant applied entitlement snapshots.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    PlanCatalogueRecord,
    PlanTier,
    StaffMember,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
    TenantAppliedEntitlement,
)
from apps.core.plans import CLINICALLY_PROTECTED_FEATURES

logger = logging.getLogger("pharmacy.billing.plans")


class PlanCatalogueService:
    """Manages SaaS Plan Catalogue definitions, canonical seedings, and immutable tenant entitlements."""

    @classmethod
    def ensure_canonical_plans_seeded(cls) -> None:
        """Idempotently seeds or refreshes canonical Basic, Standard, and Pro plan records."""
        canonical_plans = [
            {
                "plan_code": "PLAN_BASIC",
                "tier": PlanTier.BASIC,
                "name": "Basic Pharmacy",
                "description_en": "Essential single-branch pharmacy POS with regulatory compliance and offline sync.",
                "description_bn": "নিয়ন্ত্রক কমপ্লায়েন্স ও অফলাইন সিঙ্ক সহ একক শাখার জন্য বেসিক ফার্মেসি সমাধান।",
                "version": 1,
                "monthly_price_bdt": Decimal("1500.00"),
                "annual_price_bdt": Decimal("15000.00"),
                "currency": "BDT",
                "supported_billing_cycles": ["MONTHLY", "ANNUAL"],
                "max_branches": 1,
                "max_devices_per_branch": 1,
                "max_total_devices": 1,
                "max_staff_users": 3,
                "features": {
                    # Non-gated clinical safety
                    "ANTIBIOTIC_REGISTER": True,
                    "BATCH_EXPIRY_BLOCK": True,
                    "RECALL_QUARANTINE": True,
                    "BANNED_DRUG_RESTRICTION": True,
                    "RX_VERIFICATION": True,
                    # Commercial features
                    "OFFLINE_SYNC": True,
                    "THERMAL_RECEIPT_PRINT": True,
                    "MEDICINE_CATALOG": True,
                    "BASIC_INVENTORY": True,
                    "PURCHASE_ORDERS": False,
                    "SUPPLIER_DUES": False,
                    "CUSTOMER_LEDGER": False,
                    "FLUTTER_OWNER_APP": False,
                    "DAILY_BACKUP": False,
                    "CONSOLIDATED_REPORTING": False,
                    "MULTI_BRANCH_TRANSFER": False,
                    "WEBHOOKS_AND_API": False,
                },
                "is_active": True,
            },
            {
                "plan_code": "PLAN_STANDARD",
                "tier": PlanTier.STANDARD,
                "name": "Standard Pharmacy",
                "description_en": "Growing pharmacy with multi-branch management, supplier dues, and mobile owner app.",
                "description_bn": "বহু-শাখা ব্যবস্থাপনা, সরবরাহকারী বাকি ও মোবাইল অ্যাপ সহ স্ট্যান্ডার্ড ফার্মেসি সমাধান।",
                "version": 1,
                "monthly_price_bdt": Decimal("3500.00"),
                "annual_price_bdt": Decimal("35000.00"),
                "currency": "BDT",
                "supported_billing_cycles": ["MONTHLY", "ANNUAL"],
                "max_branches": 3,
                "max_devices_per_branch": 5,
                "max_total_devices": 15,
                "max_staff_users": 10,
                "features": {
                    # Non-gated clinical safety
                    "ANTIBIOTIC_REGISTER": True,
                    "BATCH_EXPIRY_BLOCK": True,
                    "RECALL_QUARANTINE": True,
                    "BANNED_DRUG_RESTRICTION": True,
                    "RX_VERIFICATION": True,
                    # Commercial features
                    "OFFLINE_SYNC": True,
                    "THERMAL_RECEIPT_PRINT": True,
                    "MEDICINE_CATALOG": True,
                    "BASIC_INVENTORY": True,
                    "PURCHASE_ORDERS": True,
                    "SUPPLIER_DUES": True,
                    "CUSTOMER_LEDGER": True,
                    "FLUTTER_OWNER_APP": True,
                    "DAILY_BACKUP": True,
                    "CONSOLIDATED_REPORTING": False,
                    "MULTI_BRANCH_TRANSFER": False,
                    "WEBHOOKS_AND_API": False,
                },
                "is_active": True,
            },
            {
                "plan_code": "PLAN_PRO",
                "tier": PlanTier.PRO,
                "name": "Pro Enterprise Pharmacy",
                "description_en": "Enterprise multi-chain pharmacy with consolidated GL, API webhooks, and unlimited scale.",
                "description_bn": "বৃহৎ ফার্মেসি চেইনের জন্য সমন্বিত অডিট লেজার, এপিআই ওয়েবহুক ও উচ্চ ক্ষমতা সম্পন্ন সমাধান।",
                "version": 1,
                "monthly_price_bdt": Decimal("7500.00"),
                "annual_price_bdt": Decimal("75000.00"),
                "currency": "BDT",
                "supported_billing_cycles": ["MONTHLY", "ANNUAL"],
                "max_branches": 50,
                "max_devices_per_branch": 20,
                "max_total_devices": 250,
                "max_staff_users": 100,
                "features": {
                    # Non-gated clinical safety
                    "ANTIBIOTIC_REGISTER": True,
                    "BATCH_EXPIRY_BLOCK": True,
                    "RECALL_QUARANTINE": True,
                    "BANNED_DRUG_RESTRICTION": True,
                    "RX_VERIFICATION": True,
                    # Commercial features
                    "OFFLINE_SYNC": True,
                    "THERMAL_RECEIPT_PRINT": True,
                    "MEDICINE_CATALOG": True,
                    "BASIC_INVENTORY": True,
                    "PURCHASE_ORDERS": True,
                    "SUPPLIER_DUES": True,
                    "CUSTOMER_LEDGER": True,
                    "FLUTTER_OWNER_APP": True,
                    "DAILY_BACKUP": True,
                    "CONSOLIDATED_REPORTING": True,
                    "MULTI_BRANCH_TRANSFER": True,
                    "WEBHOOKS_AND_API": True,
                    "HOURLY_RPO_BACKUP": True,
                },
                "is_active": True,
            },
        ]

        for item in canonical_plans:
            # Enforce Cardinal Invariant INV-SAFETY-01: clinical features must never be False
            for safety_key in CLINICALLY_PROTECTED_FEATURES:
                item["features"][safety_key] = True

            PlanCatalogueRecord.objects.update_or_create(
                plan_code=item["plan_code"],
                defaults=item,
            )

    @classmethod
    def list_catalogue_plans(cls, include_inactive: bool = False) -> list[dict[str, Any]]:
        """Returns all catalogue plans formatted according to schema."""
        cls.ensure_canonical_plans_seeded()
        qs = PlanCatalogueRecord.objects.all()
        if not include_inactive:
            qs = qs.filter(is_active=True)

        results = []
        for p in qs:
            results.append(
                {
                    "plan_code": p.plan_code,
                    "tier": p.tier,
                    "name": p.name,
                    "description_en": p.description_en,
                    "description_bn": p.description_bn,
                    "version": p.version,
                    "monthly_price_bdt": f"{p.monthly_price_bdt:.2f}",
                    "annual_price_bdt": f"{p.annual_price_bdt:.2f}",
                    "currency": p.currency,
                    "supported_billing_cycles": p.supported_billing_cycles,
                    "quotas": {
                        "max_branches": p.max_branches,
                        "max_devices_per_branch": p.max_devices_per_branch,
                        "max_total_devices": p.max_total_devices,
                        "max_staff_users": p.max_staff_users,
                    },
                    "features": p.features,
                    "is_active": p.is_active,
                }
            )
        return results

    @classmethod
    def get_plan_by_code(cls, plan_code: str) -> PlanCatalogueRecord:
        """Retrieves a plan catalogue record by its code."""
        cls.ensure_canonical_plans_seeded()
        return PlanCatalogueRecord.objects.get(plan_code=plan_code)

    @classmethod
    def get_plan_by_tier(cls, tier: str) -> PlanCatalogueRecord:
        """Retrieves the active plan catalogue record for a tier."""
        cls.ensure_canonical_plans_seeded()
        return PlanCatalogueRecord.objects.filter(tier=tier.upper(), is_active=True).first()

    @classmethod
    @transaction.atomic
    def apply_plan_to_tenant(
        cls,
        tenant_id: str,
        plan_code: str,
        billing_cycle: str = "MONTHLY",
        operator_id: str = "SYSTEM",
        notes: str = "",
    ) -> dict[str, Any]:
        """
        Applies a plan from the catalogue to a tenant, snapshotting an immutable TenantAppliedEntitlement.
        Updates tenant tier and subscription record atomically.
        """
        cls.ensure_canonical_plans_seeded()
        try:
            tenant = Tenant.objects.select_for_update().get(tenant_id=tenant_id)
        except Tenant.DoesNotExist as err:
            raise ValueError(f"Tenant '{tenant_id}' does not exist.") from err

        try:
            plan = PlanCatalogueRecord.objects.get(plan_code=plan_code, is_active=True)
        except PlanCatalogueRecord.DoesNotExist as err:
            raise ValueError(f"Active plan with code '{plan_code}' does not exist.") from err

        billing_cycle = billing_cycle.upper().strip()
        if billing_cycle not in plan.supported_billing_cycles:
            raise ValueError(
                f"Billing cycle '{billing_cycle}' is not supported by plan '{plan_code}'. "
                f"Supported: {plan.supported_billing_cycles}"
            )

        # Calculate unit price for cycle
        if billing_cycle == "ANNUAL":
            unit_price = plan.annual_price_bdt
        else:
            unit_price = plan.monthly_price_bdt

        now = timezone.now()

        # Deactivate any previous active entitlements for this tenant
        TenantAppliedEntitlement.objects.filter(tenant=tenant, is_active=True).update(
            is_active=False,
            effective_until=now,
        )

        # Snapshot immutable applied entitlement
        entitlement_id = f"ent_{uuid.uuid4().hex[:16]}"
        entitlement = TenantAppliedEntitlement.objects.create(
            entitlement_id=entitlement_id,
            tenant=tenant,
            plan_code=plan.plan_code,
            plan_tier=plan.tier,
            plan_version=plan.version,
            billing_cycle=billing_cycle,
            unit_price_bdt=unit_price,
            currency=plan.currency,
            effective_from=now,
            max_branches=plan.max_branches,
            max_devices_per_branch=plan.max_devices_per_branch,
            max_total_devices=plan.max_total_devices,
            max_staff_users=plan.max_staff_users,
            features_snapshot=dict(plan.features),
            is_active=True,
            applied_by=operator_id,
            notes=notes,
        )

        # Synchronize Tenant tier
        tenant.plan_tier = plan.tier
        tenant.save(update_fields=["plan_tier", "updated_at"])

        # Synchronize or create SubscriptionRecord
        sub = SubscriptionRecord.objects.filter(tenant=tenant).first()
        renewal_date = (now + timezone.timedelta(days=365 if billing_cycle == "ANNUAL" else 30)).date()

        if not sub:
            sub = SubscriptionRecord(
                subscription_id=f"sub_{uuid.uuid4().hex[:16]}",
                tenant=tenant,
                start_date=now.date(),
            )
        sub.plan_tier = plan.tier
        sub.status = SubscriptionStatus.ACTIVE
        sub.amount_bdt = unit_price
        sub.billing_cycle = billing_cycle
        sub.licensed_branches = plan.max_branches
        sub.licensed_devices = plan.max_total_devices
        sub.features_enabled = dict(plan.features)
        sub.renewal_date = renewal_date
        sub.save()

        # Audit log creation
        AuditLogEntry.objects.create(
            tenant_id=tenant.tenant_id,
            user_id=operator_id,
            action="TENANT_ENTITLEMENT_APPLIED",
            resource_type="TENANT_ENTITLEMENT",
            resource_id=entitlement_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "plan_code": plan.plan_code,
                "plan_tier": plan.tier,
                "plan_version": plan.version,
                "billing_cycle": billing_cycle,
                "unit_price_bdt": str(unit_price),
                "notes": notes,
            },
        )

        return cls._format_entitlement_dict(entitlement)

    @classmethod
    def get_active_tenant_entitlement(cls, tenant_id: str) -> dict[str, Any]:
        """
        Retrieves the active applied entitlement for the tenant.
        If no entitlement exists yet, auto-applies from the tenant's current plan_tier.
        """
        cls.ensure_canonical_plans_seeded()
        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist as err:
            raise ValueError(f"Tenant '{tenant_id}' does not exist.") from err

        entitlement = TenantAppliedEntitlement.objects.filter(tenant=tenant, is_active=True).first()
        if not entitlement:
            # Auto-apply according to current tenant plan_tier or default to STANDARD
            target_tier = tenant.plan_tier or PlanTier.STANDARD
            plan = cls.get_plan_by_tier(target_tier) or cls.get_plan_by_code("PLAN_STANDARD")
            return cls.apply_plan_to_tenant(
                tenant_id=tenant.tenant_id,
                plan_code=plan.plan_code,
                billing_cycle="MONTHLY",
                operator_id="SYSTEM_AUTO_BOOTSTRAP",
                notes="Initial auto-applied entitlement bootstrap",
            )

        return cls._format_entitlement_dict(entitlement)

    @classmethod
    def verify_feature_entitlement(cls, tenant_id: str, feature_key: str) -> bool:
        """
        Evaluates whether a tenant has access to a specific feature.
        INV-SAFETY-01: Clinically protected safety features are ALWAYS True.
        """
        feature_upper = feature_key.upper().strip()
        if feature_upper in CLINICALLY_PROTECTED_FEATURES:
            return True

        entitlement_data = cls.get_active_tenant_entitlement(tenant_id)
        features = entitlement_data.get("features_snapshot", {})
        return bool(features.get(feature_upper, False))

    @classmethod
    def get_plan_comparison_matrix(cls) -> dict[str, Any]:
        """
        Returns side-by-side comparison matrix for Basic, Standard, and Pro plans,
        including annual savings calculations and guaranteed clinical safety features (ADR-0083).
        """
        cls.ensure_canonical_plans_seeded()
        plans = PlanCatalogueRecord.objects.filter(is_active=True).order_by("monthly_price_bdt")

        plan_cards = []
        for p in plans:
            # Annual savings: 12 * monthly - annual
            annual_nominal = p.monthly_price_bdt * Decimal("12")
            annual_savings = annual_nominal - p.annual_price_bdt
            if annual_savings > Decimal("0.00"):
                savings_pct = f"{(annual_savings / annual_nominal * Decimal('100')).quantize(Decimal('0.01'))}%"
            else:
                savings_pct = "0.00%"
            effective_monthly = (p.annual_price_bdt / Decimal("12")).quantize(Decimal("0.01"))

            badge_map = {
                "PLAN_BASIC": ("Starter Pharmacy", "স্টার্টার ফার্মেসি"),
                "PLAN_STANDARD": ("Most Popular", "জনপ্রিয় পছন্দ"),
                "PLAN_PRO": ("Enterprise Multi-Chain", "এন্টারপ্রাইজ মাল্টি-চেইন"),
            }
            badge_en, badge_bn = badge_map.get(p.plan_code, ("Commercial Plan", "বাণিজ্যিক প্ল্যান"))

            plan_cards.append(
                {
                    "plan_code": p.plan_code,
                    "tier": p.tier,
                    "name": p.name,
                    "description_en": p.description_en,
                    "description_bn": p.description_bn,
                    "monthly_price_bdt": f"{p.monthly_price_bdt:.2f}",
                    "annual_price_bdt": f"{p.annual_price_bdt:.2f}",
                    "effective_monthly_bdt": f"{effective_monthly:.2f}",
                    "annual_savings_bdt": f"{annual_savings:.2f}",
                    "savings_percentage": savings_pct,
                    "supported_billing_cycles": p.supported_billing_cycles,
                    "quotas": {
                        "max_branches": p.max_branches,
                        "max_devices_per_branch": p.max_devices_per_branch,
                        "max_total_devices": p.max_total_devices,
                        "max_staff_users": p.max_staff_users,
                    },
                    "features": p.features,
                    "badge_en": badge_en,
                    "badge_bn": badge_bn,
                }
            )

        clinical_safety_invariants = [
            {
                "key": "ANTIBIOTIC_REGISTER",
                "name_en": "Antibiotic Dispensing Register",
                "name_bn": "অ্যান্টিবায়োটিক বিতরণ রেজিস্টার",
                "description_en": "Mandatory red-flagged antibiotic tracking and doctor prescription capture.",
                "description_bn": "বাধ্যতামূলক অ্যান্টিবায়োটিক ট্র্যাকিং ও প্রেসক্রিপশন সংরক্ষণ।",
                "guaranteed": True,
            },
            {
                "key": "BATCH_EXPIRY_BLOCK",
                "name_en": "Batch Expiry POS Blocking",
                "name_bn": "মেয়াদোত্তীর্ণ ওষুধ বিক্রয় রোধ",
                "description_en": "Automatic point-of-sale blocking for expired batches with FEFO enforcement.",
                "description_bn": "মেয়াদোত্তীর্ণ ওষুধ পয়েন্ট-অব-সেলে বিক্রয় স্বয়ংক্রিয়ভাবে বন্ধ এবং FEFO নীতি প্রয়োগ।",
                "guaranteed": True,
            },
            {
                "key": "RECALL_QUARANTINE",
                "name_en": "DGDA Drug Recall Quarantine",
                "name_bn": "ডিজিডিএ ওষুধ প্রত্যাহার কোয়ারেন্টাইন",
                "description_en": "Instant quarantine of manufacturer recalled batches preventing sale.",
                "description_bn": "প্রত্যাহারকৃত ব্যাচ অবিলম্বে কোয়ারেন্টাইনে স্থানান্তর ও বিক্রয় রোধ।",
                "guaranteed": True,
            },
            {
                "key": "BANNED_DRUG_RESTRICTION",
                "name_en": "Banned Drug Dispensing Prevention",
                "name_bn": "নিষিদ্ধ ওষুধ বিক্রয় নিয়ন্ত্রণ",
                "description_en": "Central restriction database blocking sale of regulatory banned medicines.",
                "description_bn": "নিয়ন্ত্রক সংস্থা কর্তৃক নিষিদ্ধ ওষুধের বিক্রয় প্রতিরোধ।",
                "guaranteed": True,
            },
            {
                "key": "RX_VERIFICATION",
                "name_en": "Prescription Only (POM) Verification",
                "name_bn": "প্রেসক্রিপশন নির্ভর ওষুধ যাচাইকরণ",
                "description_en": "Mandatory registered doctor verification for schedule G/H medicines.",
                "description_bn": "তফসিলি প্রেসক্রিপশন ওষুধের জন্য নিবন্ধিত চিকিৎসকের তথ্য যাচাই।",
                "guaranteed": True,
            },
        ]

        billing_cycles = [
            {
                "cycle": "MONTHLY",
                "duration_days": 30,
                "label_en": "Monthly",
                "label_bn": "মাসিক",
                "savings_label_en": "Standard monthly pricing",
                "savings_label_bn": "সাধারণ মাসিক মূল্য",
            },
            {
                "cycle": "ANNUAL",
                "duration_days": 365,
                "label_en": "Annual",
                "label_bn": "বার্ষিক",
                "savings_label_en": "Save 16.67% (2 Months Free)",
                "savings_label_bn": "১৬.৬৭% সাশ্রয় (২ মাস ফ্রি)",
            },
        ]

        return {
            "plans": plan_cards,
            "clinical_safety_invariants": clinical_safety_invariants,
            "billing_cycles": billing_cycles,
        }

    @classmethod
    def calculate_plan_quote(
        cls,
        tenant_id: str,
        plan_code: str,
        billing_cycle: str = "MONTHLY",
    ) -> dict[str, Any]:
        """
        Calculates checkout quote, savings, renewal terms, and quota compliance for a tenant (ADR-0083).
        """
        cls.ensure_canonical_plans_seeded()
        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist as err:
            raise ValueError(f"Tenant '{tenant_id}' does not exist.") from err

        try:
            plan = PlanCatalogueRecord.objects.get(plan_code=plan_code, is_active=True)
        except PlanCatalogueRecord.DoesNotExist as err:
            raise ValueError(f"Active plan with code '{plan_code}' does not exist.") from err

        cycle = billing_cycle.upper().strip()
        if cycle not in ["MONTHLY", "ANNUAL"]:
            raise ValueError(f"Unsupported billing cycle '{billing_cycle}'. Supported: ['MONTHLY', 'ANNUAL']")

        monthly_base = plan.monthly_price_bdt
        annual_base = plan.annual_price_bdt
        now = timezone.now()

        if cycle == "ANNUAL":
            subtotal = (monthly_base * Decimal("12")).quantize(Decimal("0.01"))
            discount = (subtotal - annual_base).quantize(Decimal("0.01"))
            total = annual_base.quantize(Decimal("0.01"))
            effective_monthly = (total / Decimal("12")).quantize(Decimal("0.01"))
            savings_pct = f"{(discount / subtotal * Decimal('100')).quantize(Decimal('0.01'))}%"
            renewal_days = 365
        else:
            subtotal = monthly_base.quantize(Decimal("0.01"))
            discount = Decimal("0.00")
            total = monthly_base.quantize(Decimal("0.01"))
            effective_monthly = monthly_base.quantize(Decimal("0.01"))
            savings_pct = "0.00%"
            renewal_days = 30

        projected_renewal_date = (now + timezone.timedelta(days=renewal_days)).date().isoformat()

        # Quota Compliance Check
        active_branches = Branch.objects.filter(tenant=tenant, is_active=True).count()
        active_staff = StaffMember.objects.filter(tenant=tenant, is_active=True).count()

        violations = []
        if active_branches > plan.max_branches:
            violations.append(
                f"Current active branches ({active_branches}) exceed target plan limit ({plan.max_branches})."
            )
        if active_staff > plan.max_staff_users:
            violations.append(
                f"Current active staff ({active_staff}) exceed target plan limit ({plan.max_staff_users})."
            )

        return {
            "tenant_id": tenant.tenant_id,
            "plan_code": plan.plan_code,
            "plan_tier": plan.tier,
            "billing_cycle": cycle,
            "currency": plan.currency,
            "subtotal_bdt": f"{subtotal:.2f}",
            "discount_bdt": f"{discount:.2f}",
            "total_bdt": f"{total:.2f}",
            "effective_monthly_bdt": f"{effective_monthly:.2f}",
            "savings_percentage": savings_pct,
            "renewal_period_days": renewal_days,
            "projected_renewal_date": projected_renewal_date,
            "quotas": {
                "max_branches": plan.max_branches,
                "max_devices_per_branch": plan.max_devices_per_branch,
                "max_total_devices": plan.max_total_devices,
                "max_staff_users": plan.max_staff_users,
            },
            "current_usage": {
                "active_branches": active_branches,
                "active_staff_users": active_staff,
            },
            "safety_features_guaranteed": True,
            "quota_compliance": {
                "is_compliant": len(violations) == 0,
                "violations": violations,
            },
        }

    @classmethod
    def _format_entitlement_dict(cls, ent: TenantAppliedEntitlement) -> dict[str, Any]:
        current_branches = Branch.objects.filter(tenant=ent.tenant, is_active=True).count()
        current_staff = StaffMember.objects.filter(tenant=ent.tenant, is_active=True).count()

        return {
            "entitlement_id": ent.entitlement_id,
            "tenant_id": ent.tenant_id,
            "plan_code": ent.plan_code,
            "plan_tier": ent.plan_tier,
            "plan_version": ent.plan_version,
            "billing_cycle": ent.billing_cycle,
            "unit_price_bdt": f"{ent.unit_price_bdt:.2f}",
            "currency": ent.currency,
            "effective_from": ent.effective_from.isoformat(),
            "effective_until": ent.effective_until.isoformat() if ent.effective_until else None,
            "quotas": {
                "max_branches": ent.max_branches,
                "max_devices_per_branch": ent.max_devices_per_branch,
                "max_total_devices": ent.max_total_devices,
                "max_staff_users": ent.max_staff_users,
            },
            "current_usage": {
                "active_branches": current_branches,
                "active_staff_users": current_staff,
            },
            "features_snapshot": ent.features_snapshot,
            "is_active": ent.is_active,
            "applied_by": ent.applied_by,
        }
