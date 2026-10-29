"""
Feature Package Taxonomy, Quotas, Downgrade Policies & Clinical Safety Guarantees
Architecture Decision: ADR-0015
Enforces commercial feature tiers while guaranteeing that clinical safety controls are NEVER gated.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class PlanTier(str, Enum):
    BASIC = "BASIC"
    STANDARD = "STANDARD"
    PRO = "PRO"


class SubscriptionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    GRACE_PERIOD = "GRACE_PERIOD"
    OVER_QUOTA = "OVER_QUOTA"
    SUSPENDED = "SUSPENDED"


# Cardinal Safety Invariant: Features that must NEVER be gated behind paid tiers
CLINICALLY_PROTECTED_FEATURES: set[str] = {
    "ANTIBIOTIC_REGISTER",
    "BATCH_EXPIRY_BLOCK",
    "RECALL_QUARANTINE",
    "BANNED_DRUG_RESTRICTION",
    "RX_VERIFICATION",
}


class PlanQuotaExceededError(ValueError):
    """Raised when an action exceeds the tenant's current plan quota."""

    pass


class ClinicalFeatureGatingViolationError(RuntimeError):
    """Raised if any configuration attempts to gate a clinically protected safety feature."""

    pass


@dataclass(frozen=True)
class PlanQuota:
    max_branches: int
    max_devices_per_branch: int
    max_staff_users: int
    monthly_price_bdt: Decimal

    def validate(self) -> None:
        if self.max_branches <= 0:
            raise ValueError(f"max_branches must be positive, got {self.max_branches}")
        if self.max_devices_per_branch <= 0:
            raise ValueError(f"max_devices_per_branch must be positive, got {self.max_devices_per_branch}")
        if self.max_staff_users <= 0:
            raise ValueError(f"max_staff_users must be positive, got {self.max_staff_users}")
        if self.monthly_price_bdt < Decimal("0"):
            raise ValueError(f"monthly_price_bdt cannot be negative, got {self.monthly_price_bdt}")


@dataclass(frozen=True)
class PlanDefinition:
    tier: PlanTier
    name: str
    quota: PlanQuota
    commercial_features: set[str]

    def has_feature(self, feature_name: str) -> bool:
        """
        Evaluates feature entitlement.
        Clinically protected features always return True across all tiers.
        """
        if feature_name in CLINICALLY_PROTECTED_FEATURES:
            return True
        return feature_name in self.commercial_features


@dataclass
class TenantSubscriptionState:
    tenant_id: str
    plan_tier: PlanTier
    active_branch_count: int
    active_device_count: int
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE
    grace_period_days_remaining: int = 0


class PlanTaxonomyManager:
    """Manages plan catalog, entitlement verification, quota checks, and safe downgrades."""

    def __init__(self):
        self._plans: dict[PlanTier, PlanDefinition] = {}
        self._initialize_canonical_plans()

    def _initialize_canonical_plans(self) -> None:
        basic_quota = PlanQuota(
            max_branches=1,
            max_devices_per_branch=1,
            max_staff_users=3,
            monthly_price_bdt=Decimal("1500.00"),
        )
        basic_quota.validate()
        self._plans[PlanTier.BASIC] = PlanDefinition(
            tier=PlanTier.BASIC,
            name="Basic Pharmacy",
            quota=basic_quota,
            commercial_features={
                "OFFLINE_SYNC",
                "THERMAL_RECEIPT_PRINT",
                "MEDICINE_CATALOG",
                "BASIC_INVENTORY",
            },
        )

        standard_quota = PlanQuota(
            max_branches=3,
            max_devices_per_branch=5,
            max_staff_users=10,
            monthly_price_bdt=Decimal("3500.00"),
        )
        standard_quota.validate()
        self._plans[PlanTier.STANDARD] = PlanDefinition(
            tier=PlanTier.STANDARD,
            name="Standard Pharmacy",
            quota=standard_quota,
            commercial_features={
                "OFFLINE_SYNC",
                "THERMAL_RECEIPT_PRINT",
                "MEDICINE_CATALOG",
                "BASIC_INVENTORY",
                "PURCHASE_ORDERS",
                "SUPPLIER_DUES",
                "CUSTOMER_LEDGER",
                "FLUTTER_OWNER_APP",
                "DAILY_BACKUP",
            },
        )

        pro_quota = PlanQuota(
            max_branches=50,
            max_devices_per_branch=20,
            max_staff_users=100,
            monthly_price_bdt=Decimal("7500.00"),
        )
        pro_quota.validate()
        self._plans[PlanTier.PRO] = PlanDefinition(
            tier=PlanTier.PRO,
            name="Pro Enterprise Pharmacy",
            quota=pro_quota,
            commercial_features={
                "OFFLINE_SYNC",
                "THERMAL_RECEIPT_PRINT",
                "MEDICINE_CATALOG",
                "BASIC_INVENTORY",
                "PURCHASE_ORDERS",
                "SUPPLIER_DUES",
                "CUSTOMER_LEDGER",
                "FLUTTER_OWNER_APP",
                "DAILY_BACKUP",
                "CONSOLIDATED_REPORTING",
                "MULTI_BRANCH_TRANSFER",
                "WEBHOOKS_AND_API",
                "HOURLY_RPO_BACKUP",
            },
        )

    def get_plan(self, tier: PlanTier) -> PlanDefinition:
        if tier not in self._plans:
            raise ValueError(f"Unknown plan tier: {tier}")
        return self._plans[tier]

    def verify_entitlement(self, tier: PlanTier, feature_name: str) -> bool:
        """Checks if a plan has access to a feature, preserving clinical safety invariants."""
        plan = self.get_plan(tier)
        return plan.has_feature(feature_name)

    def assert_feature_not_clinically_gated(self, feature_name: str) -> None:
        """Enforces that no gatekeeper can conditionally restrict a clinically protected safety feature."""
        if feature_name in CLINICALLY_PROTECTED_FEATURES:
            raise ClinicalFeatureGatingViolationError(
                f"Feature '{feature_name}' is a clinically protected safety control (DGDA mandate) and can NEVER be placed behind a paywall or tier gate."
            )

    def can_provision_branch(self, subscription: TenantSubscriptionState) -> bool:
        """Evaluates if the tenant has branch quota headroom."""
        plan = self.get_plan(subscription.plan_tier)
        if subscription.active_branch_count >= plan.quota.max_branches:
            raise PlanQuotaExceededError(
                f"Branch limit reached: Tenant has {subscription.active_branch_count} branches on {plan.name} (Max: {plan.quota.max_branches}). Upgrade required."
            )
        return True

    def can_provision_device(self, subscription: TenantSubscriptionState, branch_devices_count: int) -> bool:
        """Evaluates if a branch has offline device quota headroom."""
        plan = self.get_plan(subscription.plan_tier)
        if branch_devices_count >= plan.quota.max_devices_per_branch:
            raise PlanQuotaExceededError(
                f"Device limit reached: Branch has {branch_devices_count} devices on {plan.name} (Max: {plan.quota.max_devices_per_branch} per branch). Upgrade required."
            )
        return True

    def execute_downgrade(
        self,
        subscription: TenantSubscriptionState,
        target_tier: PlanTier,
        actor_role: str,
        actor_tenant_id: str,
    ) -> TenantSubscriptionState:
        """
        Executes safe plan downgrade.
        Never discards historical data. Sets OVER_QUOTA soft-lock if active branches exceed new tier.
        """
        # Security & Tenant boundary check
        if actor_role != "PLATFORM_SUPER_ADMIN" and actor_tenant_id != subscription.tenant_id:
            raise PermissionError(
                f"Tenant '{actor_tenant_id}' cannot alter subscription for tenant '{subscription.tenant_id}'"
            )

        new_plan = self.get_plan(target_tier)
        subscription.plan_tier = target_tier

        if subscription.active_branch_count > new_plan.quota.max_branches:
            subscription.status = SubscriptionStatus.OVER_QUOTA
            subscription.grace_period_days_remaining = 7
        else:
            subscription.status = SubscriptionStatus.ACTIVE
            subscription.grace_period_days_remaining = 0

        return subscription
