"""
apps/core/navigation.py

Executable Navigation Information Architecture (IA) Engine for Pharmacy ERP SaaS.
Enforces role-based route permissions (RBAC), subscription plan feature gating,
offline availability checks, and localized empty state definitions.
"""

from dataclasses import dataclass
from enum import Enum


class NavGroup(str, Enum):
    OPERATIONS = "OPERATIONS"
    BUSINESS = "BUSINESS"
    ADMIN = "ADMIN"
    PLATFORM = "PLATFORM"


class PlanTier(int, Enum):
    BASIC = 1
    STANDARD = 2
    PRO = 3


class NavigationStatus(str, Enum):
    RENDER_VIEW = "RENDER_VIEW"
    EMPTY_STATE = "EMPTY_STATE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    PLAN_UPGRADE_REQUIRED = "PLAN_UPGRADE_REQUIRED"
    OFFLINE_UNAVAILABLE = "OFFLINE_UNAVAILABLE"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class NavEmptyState:
    heading_bn: str
    heading_en: str
    description_bn: str
    description_en: str
    icon: str  # Verified SVG identifier (e.g. 'lucide:shopping-bag'). NO EMOJIS!
    cta_label_bn: str
    cta_label_en: str
    cta_action: str


@dataclass(frozen=True)
class NavDestination:
    id: str
    route: str
    group: NavGroup
    label_en: str
    label_bn: str
    icon: str  # Verified SVG identifier. NO EMOJIS!
    allowed_roles: set[str]
    min_plan_tier: PlanTier = PlanTier.BASIC
    supports_offline: bool = False
    empty_state: NavEmptyState | None = None


@dataclass
class NavigationResolutionResult:
    status: NavigationStatus
    destination: NavDestination | None = None
    message: str = ""
    empty_state: NavEmptyState | None = None


class NavigationRegistry:
    """Canonical registry containing all approved desktop, mobile, and web routes."""

    def __init__(self):
        self._destinations: dict[str, NavDestination] = {}
        self._bootstrap_approved_routes()

    def register(self, dest: NavDestination):
        self._destinations[dest.route] = dest

    def get_by_route(self, route: str) -> NavDestination | None:
        return self._destinations.get(route)

    def get_all(self) -> list[NavDestination]:
        return list(self._destinations.values())

    def _bootstrap_approved_routes(self):
        # 1. OPERATIONS GROUP
        self.register(
            NavDestination(
                id="dashboard",
                route="/app/dashboard",
                group=NavGroup.OPERATIONS,
                label_en="Dashboard",
                label_bn="ড্যাশবোর্ড",
                icon="lucide:layout-dashboard",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER", "PHARMACIST", "CASHIER", "ACCOUNTANT"},
                supports_offline=True,
            )
        )
        self.register(
            NavDestination(
                id="pos",
                route="/app/pos",
                group=NavGroup.OPERATIONS,
                label_en="POS",
                label_bn="বিক্রয় কাউন্টার (POS)",
                icon="lucide:shopping-cart",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER", "PHARMACIST", "CASHIER"},
                supports_offline=True,
                empty_state=NavEmptyState(
                    heading_bn="কার্ট খালি আছে",
                    heading_en="Cart is Empty",
                    description_bn="ওষুধ যোগ করতে বারকোড স্ক্যান করুন বা নাম লিখে খুঁজুন।",
                    description_en="Scan a barcode or search medicine to add items.",
                    icon="lucide:shopping-bag",
                    cta_label_bn="বারকোড সার্চে ফোকাস করুন",
                    cta_label_en="Focus Search Bar",
                    cta_action="FOCUS_SEARCH",
                ),
            )
        )
        self.register(
            NavDestination(
                id="sales",
                route="/app/sales",
                group=NavGroup.OPERATIONS,
                label_en="Sales & Returns",
                label_bn="বিক্রয় ও রিটার্ন",
                icon="lucide:receipt",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER", "PHARMACIST", "CASHIER", "ACCOUNTANT"},
                supports_offline=True,
                empty_state=NavEmptyState(
                    heading_bn="কোনো বিক্রয় রেকর্ড পাওয়া যায়নি",
                    heading_en="No Sales Records Found",
                    description_bn="নির্বাচিত তারিখ বা ফিল্টারে এখনো কোনো বিক্রয় সম্পন্ন হয়নি।",
                    description_en="No completed sales for the selected date range.",
                    icon="lucide:receipt",
                    cta_label_bn="নতুন বিক্রয় শুরু করুন",
                    cta_label_en="Go to POS",
                    cta_action="NAVIGATE_POS",
                ),
            )
        )
        self.register(
            NavDestination(
                id="purchases",
                route="/app/purchases",
                group=NavGroup.OPERATIONS,
                label_en="Purchases & Receiving",
                label_bn="ক্রয় ও রিসিভিং",
                icon="lucide:truck",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER", "PURCHASER", "INVENTORY_STAFF", "ACCOUNTANT"},
                supports_offline=True,
                empty_state=NavEmptyState(
                    heading_bn="কোনো ক্রয় আদেশ নেই",
                    heading_en="No Purchase Orders",
                    description_bn="সাপ্লায়ার থেকে ওষুধ ক্রয়ের জন্য নতুন অর্ডার তৈরি করুন।",
                    description_en="Create a new purchase order to replenish medicine stock.",
                    icon="lucide:truck",
                    cta_label_bn="নতুন পিও তৈরি করুন",
                    cta_label_en="Create Purchase Order",
                    cta_action="CREATE_PO",
                ),
            )
        )
        self.register(
            NavDestination(
                id="inventory",
                route="/app/inventory",
                group=NavGroup.OPERATIONS,
                label_en="Inventory Ledger",
                label_bn="ইনভেন্টরি লেজার",
                icon="lucide:boxes",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER", "PHARMACIST", "INVENTORY_STAFF", "ACCOUNTANT"},
                supports_offline=True,
            )
        )
        self.register(
            NavDestination(
                id="catalog",
                route="/app/catalog",
                group=NavGroup.OPERATIONS,
                label_en="Medicine Catalog",
                label_bn="ওষুধ ক্যাটালগ",
                icon="lucide:pill",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER", "PHARMACIST", "CASHIER", "INVENTORY_STAFF"},
                supports_offline=True,
            )
        )
        self.register(
            NavDestination(
                id="batches",
                route="/app/batches",
                group=NavGroup.OPERATIONS,
                label_en="Batches & Expiry",
                label_bn="ব্যাচ ও মেয়াদোত্তীর্ণ তালিকা",
                icon="lucide:calendar-clock",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER", "PHARMACIST", "INVENTORY_STAFF"},
                supports_offline=True,
            )
        )
        self.register(
            NavDestination(
                id="prescriptions",
                route="/app/prescriptions",
                group=NavGroup.OPERATIONS,
                label_en="Prescriptions (Rx)",
                label_bn="প্রেসক্রিপশন ও ড্রাগ ভেরিফিকেশন",
                icon="lucide:file-text",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER", "PHARMACIST"},
                supports_offline=True,
                empty_state=NavEmptyState(
                    heading_bn="কোনো প্রেসক্রিপশন পেন্ডিং নেই",
                    heading_en="No Pending Prescriptions",
                    description_bn="বর্তমানে যাচাইয়ের জন্য কোনো প্রেসক্রিপশন অপেক্ষমাণ নেই।",
                    description_en="All uploaded customer prescriptions have been verified.",
                    icon="lucide:file-check",
                    cta_label_bn="প্রেসক্রিপশন স্ক্যান করুন",
                    cta_label_en="Upload / Scan Rx",
                    cta_action="SCAN_RX",
                ),
            )
        )
        self.register(
            NavDestination(
                id="antibiotics",
                route="/app/antibiotics",
                group=NavGroup.OPERATIONS,
                label_en="Antibiotic Register",
                label_bn="অ্যান্টিবায়োটিক রেজিস্টার",
                icon="lucide:shield-alert",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER", "PHARMACIST", "AUDITOR"},
                supports_offline=True,
                empty_state=NavEmptyState(
                    heading_bn="অ্যান্টিবায়োটিক রেকর্ড খালি",
                    heading_en="Antibiotic Register Empty",
                    description_bn="এই ব্রাঞ্চে এখনো কোনো অ্যান্টিবায়োটিক বিক্রি রেকর্ড করা হয়নি।",
                    description_en="No antibiotic dispensing recorded for this period.",
                    icon="lucide:shield-check",
                    cta_label_bn="ডিসপেনসিং নির্দেশিকা দেখুন",
                    cta_label_en="View DGDA Rules",
                    cta_action="VIEW_GUIDELINES",
                ),
            )
        )

        # 2. BUSINESS GROUP
        self.register(
            NavDestination(
                id="customers",
                route="/app/customers",
                group=NavGroup.BUSINESS,
                label_en="Customers & Dues",
                label_bn="গ্রাহক ও বাকির খাতা",
                icon="lucide:users",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER", "CASHIER", "ACCOUNTANT"},
                supports_offline=True,
                empty_state=NavEmptyState(
                    heading_bn="কোনো গ্রাহক তালিকাভুক্ত নেই",
                    heading_en="No Customers Registered",
                    description_bn="নিয়মিত গ্রাহকদের বাকির হিসাব ও যোগাযোগের জন্য যুক্ত করুন।",
                    description_en="Register regular customers to manage credit and history.",
                    icon="lucide:user-plus",
                    cta_label_bn="নতুন গ্রাহক যুক্ত করুন",
                    cta_label_en="Add Customer",
                    cta_action="ADD_CUSTOMER",
                ),
            )
        )
        self.register(
            NavDestination(
                id="suppliers",
                route="/app/suppliers",
                group=NavGroup.BUSINESS,
                label_en="Suppliers",
                label_bn="সরবরাহকারী তালিকা",
                icon="lucide:building-2",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER", "PURCHASER", "ACCOUNTANT"},
                supports_offline=True,
            )
        )
        self.register(
            NavDestination(
                id="accounting",
                route="/app/accounting",
                group=NavGroup.BUSINESS,
                label_en="General Ledger",
                label_bn="জেনারেল লেজার ও হিসাব",
                icon="lucide:book-open",
                allowed_roles={"TENANT_OWNER", "ACCOUNTANT", "AUDITOR"},
                min_plan_tier=PlanTier.STANDARD,
                supports_offline=False,
            )
        )
        self.register(
            NavDestination(
                id="expenses",
                route="/app/expenses",
                group=NavGroup.BUSINESS,
                label_en="Operating Expenses",
                label_bn="দৈনন্দিন খরচ (Expenses)",
                icon="lucide:wallet",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER", "ACCOUNTANT"},
                supports_offline=True,
            )
        )
        self.register(
            NavDestination(
                id="reports",
                route="/app/reports",
                group=NavGroup.BUSINESS,
                label_en="Reports & Analytics",
                label_bn="রিপোর্ট ও বিশ্লেষণ",
                icon="lucide:bar-chart-3",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER", "ACCOUNTANT", "AUDITOR"},
                min_plan_tier=PlanTier.BASIC,
                supports_offline=False,
            )
        )
        self.register(
            NavDestination(
                id="compliance",
                route="/app/compliance",
                group=NavGroup.BUSINESS,
                label_en="DGDA Compliance",
                label_bn="ডিজিডিএ রেগুলেটরি কমপ্লায়েন্স",
                icon="lucide:clipboard-check",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER", "PHARMACIST", "AUDITOR"},
                min_plan_tier=PlanTier.STANDARD,
                supports_offline=False,
                empty_state=NavEmptyState(
                    heading_bn="কমপ্লায়েন্স লগ হালনাগাদ",
                    heading_en="Compliance Log Up to Date",
                    description_bn="কোনো বকেয়া পরিদর্শন বা এক্সপায়ারি সতর্কতা নেই।",
                    description_en="All regulatory checklists and cold-chain logs are current.",
                    icon="lucide:award",
                    cta_label_bn="অডিট রিপোর্ট এক্সপোর্ট",
                    cta_label_en="Export Inspection Report",
                    cta_action="EXPORT_AUDIT",
                ),
            )
        )

        # 3. ADMIN GROUP
        self.register(
            NavDestination(
                id="staff",
                route="/app/staff",
                group=NavGroup.ADMIN,
                label_en="Staff & Roles",
                label_bn="কর্মচারী ও রোল বরাদ্দ",
                icon="lucide:user-cog",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER"},
                supports_offline=False,
            )
        )
        self.register(
            NavDestination(
                id="branches",
                route="/app/branches",
                group=NavGroup.ADMIN,
                label_en="Branches",
                label_bn="ফার্মাসি ব্রাঞ্চসমূহ",
                icon="lucide:git-branch",
                allowed_roles={"TENANT_OWNER"},
                min_plan_tier=PlanTier.STANDARD,
                supports_offline=False,
            )
        )
        self.register(
            NavDestination(
                id="billing",
                route="/app/billing",
                group=NavGroup.ADMIN,
                label_en="Subscription & Billing",
                label_bn="সাবস্ক্রিপশন ও বিলিং",
                icon="lucide:credit-card",
                allowed_roles={"TENANT_OWNER"},
                supports_offline=False,
            )
        )
        self.register(
            NavDestination(
                id="sync",
                route="/app/sync",
                group=NavGroup.ADMIN,
                label_en="Device & Sync Status",
                label_bn="ডিভাইস ও সিঙ্ক স্ট্যাটাস",
                icon="lucide:refresh-cw",
                allowed_roles={"TENANT_OWNER", "BRANCH_MANAGER", "CASHIER"},
                supports_offline=True,
            )
        )

        # 4. PLATFORM CONSOLE GROUP
        self.register(
            NavDestination(
                id="platform_metrics",
                route="/platform/metrics",
                group=NavGroup.PLATFORM,
                label_en="Platform Metrics",
                label_bn="প্ল্যাটফর্ম মেট্রিক্স",
                icon="lucide:server",
                allowed_roles={"SUPER_ADMIN"},
                supports_offline=False,
            )
        )
        self.register(
            NavDestination(
                id="platform_tenants",
                route="/platform/tenants",
                group=NavGroup.PLATFORM,
                label_en="Tenants Directory",
                label_bn="টেন্যান্ট ডিরেক্টরি",
                icon="lucide:building",
                allowed_roles={"SUPER_ADMIN"},
                supports_offline=False,
            )
        )


class NavigationRouter:
    """Evaluates route accessibility and determines the resulting UI state."""

    def __init__(self, registry: NavigationRegistry | None = None):
        self.registry = registry or NavigationRegistry()

    def get_sidebar_destinations(
        self, user_role: str, user_plan: PlanTier, is_online: bool = True
    ) -> dict[NavGroup, list[NavDestination]]:
        """
        Returns all sidebar destinations accessible by the user's role.
        Destinations requiring a higher plan tier or cloud connectivity are retained
        with appropriate state indicators rather than silently disappearing.
        """
        grouped: dict[NavGroup, list[NavDestination]] = {
            NavGroup.OPERATIONS: [],
            NavGroup.BUSINESS: [],
            NavGroup.ADMIN: [],
            NavGroup.PLATFORM: [],
        }

        for dest in self.registry.get_all():
            if user_role in dest.allowed_roles:
                grouped[dest.group].append(dest)

        return grouped

    def resolve_route(
        self, route: str, user_role: str, user_plan: PlanTier, is_online: bool = True, has_data: bool = True
    ) -> NavigationResolutionResult:
        """
        Deterministically resolves a route to RENDER_VIEW, EMPTY_STATE,
        PERMISSION_DENIED, PLAN_UPGRADE_REQUIRED, OFFLINE_UNAVAILABLE, or NOT_FOUND.
        """
        dest = self.registry.get_by_route(route)
        if not dest:
            return NavigationResolutionResult(
                status=NavigationStatus.NOT_FOUND, message=f"Route '{route}' does not exist in navigation registry."
            )

        # 1. RBAC Privilege Check
        if user_role not in dest.allowed_roles:
            return NavigationResolutionResult(
                status=NavigationStatus.PERMISSION_DENIED,
                destination=dest,
                message=f"Role '{user_role}' is not authorized to access '{route}'.",
            )

        # 2. SaaS Subscription Tier Entitlement Check
        if user_plan.value < dest.min_plan_tier.value:
            return NavigationResolutionResult(
                status=NavigationStatus.PLAN_UPGRADE_REQUIRED,
                destination=dest,
                message=f"Route requires '{dest.min_plan_tier.name}' plan tier (current: '{user_plan.name}').",
            )

        # 3. Connectivity & Offline Availability Check
        if not is_online and not dest.supports_offline:
            return NavigationResolutionResult(
                status=NavigationStatus.OFFLINE_UNAVAILABLE,
                destination=dest,
                message=f"Route '{route}' requires active cloud connectivity.",
            )

        # 4. Empty State vs Data Render View
        if not has_data and dest.empty_state:
            return NavigationResolutionResult(
                status=NavigationStatus.EMPTY_STATE,
                destination=dest,
                empty_state=dest.empty_state,
                message=dest.empty_state.heading_en,
            )

        return NavigationResolutionResult(
            status=NavigationStatus.RENDER_VIEW, destination=dest, message="Ready to render view."
        )
