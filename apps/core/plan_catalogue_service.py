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
