"""
apps/core/permission_matrix.py

Architecture Decision: ADR-0005 & ADR-0059
API Endpoint Permission Matrix, Least Privilege Defaults, and Field Masking Engine.
Guarantees:
  - Default DENY policy for unauthorized roles.
  - Strict branch boundary checks for branch-bound personas (Cashier, Inventory).
  - Absolute system blocks against destructive deletion of posted records.
  - Granular field masking (e.g. purchase cost prices masked for cashiers; patient PII redacted).
  - Subscription plan tier verification (BASIC, STANDARD, PRO).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from apps.identity.models import UserRole


class PlanTier:
    BASIC = "BASIC"
    STANDARD = "STANDARD"
    PRO = "PRO"

    TIER_LEVELS = {
        BASIC: 1,
        STANDARD: 2,
        PRO: 3,
    }

    @classmethod
    def satisfies(cls, current_plan: str, required_plan: str) -> bool:
        current_lvl = cls.TIER_LEVELS.get(current_plan.upper(), 1)
        required_lvl = cls.TIER_LEVELS.get(required_plan.upper(), 1)
        return current_lvl >= required_lvl


class ScopeLevel:
    GLOBAL = "GLOBAL"
    TENANT = "TENANT"
    BRANCH = "BRANCH"
    SHIFT = "SHIFT"
    PUBLIC = "PUBLIC"


@dataclass
class FieldMaskingPolicy:
    mask_type: str  # "REDACTED", "PARTIAL_PHONE", "HIDDEN"
    exempt_roles: List[str] = field(default_factory=list)


@dataclass
class EndpointPermissionRule:
    path_pattern: str
    method: str
    allowed_roles: List[str]
    scope: str = ScopeLevel.GLOBAL
    min_plan: str = PlanTier.BASIC
    is_absolute_blocked: bool = False
    field_masking: Dict[str, FieldMaskingPolicy] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path_pattern": self.path_pattern,
            "method": self.method,
            "allowed_roles": self.allowed_roles,
            "scope": self.scope,
            "min_plan": self.min_plan,
            "is_absolute_blocked": self.is_absolute_blocked,
            "field_masking": {
                f: {"mask_type": p.mask_type, "exempt_roles": p.exempt_roles} for f, p in self.field_masking.items()
            },
        }


EndpointPermission = EndpointPermissionRule


# Canonical Exempt Roles for Commercial Cost Prices
COST_PRICE_EXEMPT_ROLES = [
    str(UserRole.PHARMACY_OWNER),
    str(UserRole.BRANCH_MANAGER),
    str(UserRole.FINANCE_OFFICER),
    str(UserRole.SUPERADMIN),
]

# Clinical / Auditor Exempt Roles for Patient PII
PII_EXEMPT_ROLES = [
    str(UserRole.PHARMACY_OWNER),
    str(UserRole.BRANCH_MANAGER),
    str(UserRole.STAFF_PHARMACIST),
    str(UserRole.AUDITOR),
    str(UserRole.SUPERADMIN),
]

MEDICINE_FIELD_MASKING = {
    "cost_price": FieldMaskingPolicy(
        mask_type="REDACTED",
        exempt_roles=COST_PRICE_EXEMPT_ROLES,
    ),
    "purchase_price": FieldMaskingPolicy(
        mask_type="REDACTED",
        exempt_roles=COST_PRICE_EXEMPT_ROLES,
    ),
}

INVOICE_FIELD_MASKING = {
    "customer_phone": FieldMaskingPolicy(
        mask_type="PARTIAL_PHONE",
        exempt_roles=PII_EXEMPT_ROLES,
    ),
    "supplier_unit_cost": FieldMaskingPolicy(
        mask_type="REDACTED",
        exempt_roles=COST_PRICE_EXEMPT_ROLES,
    ),
}


# Canonical Endpoint Permission Inventory
ENDPOINT_PERMISSION_RULES: List[EndpointPermissionRule] = [
    # 1. Medicines & Master Catalog
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/medicines/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.INVENTORY_CLERK,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
        field_masking={
            "cost_price": FieldMaskingPolicy(
                mask_type="REDACTED",
                exempt_roles=COST_PRICE_EXEMPT_ROLES,
            ),
            "purchase_price": FieldMaskingPolicy(
                mask_type="REDACTED",
                exempt_roles=COST_PRICE_EXEMPT_ROLES,
            ),
        },
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/medicines/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.INVENTORY_CLERK,
        ],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/medicines/(?:[^/]+/)?$",
        method="PUT",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.INVENTORY_CLERK,
        ],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/medicines/(?:[^/]+/)?$",
        method="DELETE",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    # 2. Batch Inventory & Stock Ledger
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/batches/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.INVENTORY_CLERK,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking={
            "cost_price": FieldMaskingPolicy(
                mask_type="REDACTED",
                exempt_roles=COST_PRICE_EXEMPT_ROLES,
            ),
            "lot_cost": FieldMaskingPolicy(
                mask_type="REDACTED",
                exempt_roles=COST_PRICE_EXEMPT_ROLES,
            ),
        },
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/batches/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.INVENTORY_CLERK,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/batches/(?:[^/]+/)?$",
        method="DELETE",
        allowed_roles=[],
        is_absolute_blocked=True,  # Absolute block: posted stock cannot be hard deleted
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # 3. Sales Invoices
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/invoices/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.CASHIER,
            UserRole.STAFF_PHARMACIST,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking=INVOICE_FIELD_MASKING,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/invoices/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking=INVOICE_FIELD_MASKING,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/invoices/(?:[^/]+/)?$",
        method="DELETE",
        allowed_roles=[],
        is_absolute_blocked=True,  # Absolute block: posted invoices cannot be hard deleted
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # 4. Pharmacist Verification Queue & Prescriptions
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/review-queue/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/review-queue/[^/]+/(?:verify|reject|request-clarification|resolve-clarification)/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/review-queue/[^/]+/audit-trail/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.STAFF_PHARMACIST,
            UserRole.PHARMACY_OWNER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # 4. Prescriptions
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking={
            "patient_national_id": FieldMaskingPolicy(
                mask_type="REDACTED",
                exempt_roles=[UserRole.STAFF_PHARMACIST, UserRole.PHARMACY_OWNER],
            ),
        },
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/(?:[^/]+/)?$",
        method="PATCH",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/[^/]+/verify/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/[^/]+/dispense/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/[^/]+/cancel/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/[^/]+/upload-attachment/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/[^/]+/attachments/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/[^/]+/check-validity/?$",
        method="ANY",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/[^/]+/reject-validity/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/[^/]+/match-items/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/[^/]+/commit-matching/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/[^/]+/reverse-dispense/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescription-attachments/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescription-attachments/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescription-attachments/[^/]+/download-url/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescription-attachments/[^/]+/verify/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescription-attachments/(?:[^/]+/)?$",
        method="DELETE",
        allowed_roles=[],
        is_absolute_blocked=True,
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/prescriptions/(?:[^/]+/)?$",
        method="DELETE",
        allowed_roles=[],
        is_absolute_blocked=True,
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # 5. Branches Management
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/branches/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.INVENTORY_CLERK,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/branches/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.STANDARD,  # Multi-branch requires STANDARD plan or higher
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/branches/(?:[^/]+/)?$",
        method="DELETE",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.STANDARD,
    ),
    # 6. Audit Logs
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/audit-logs/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.STANDARD,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/audit-logs/(?:[^/]+/)?$",
        method="DELETE",
        allowed_roles=[],
        is_absolute_blocked=True,  # Audit records cannot be deleted by anyone
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.STANDARD,
    ),
    # 7. Analytics & Reports
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/analytics/chain-wide/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
        ],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.PRO,  # Chain-wide multi-branch consolidated analytics requires PRO plan
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/analytics/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    # 8. Subscription Management
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/subscriptions/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/subscriptions/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    # 9. Tenant Secrets Vault
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/secrets/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
        field_masking={
            "secret_value": FieldMaskingPolicy(
                mask_type="REDACTED",
                exempt_roles=[UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER],
            ),
        },
    ),
    # 10. Data Privacy Requests
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/privacy/requests/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER, UserRole.AUDITOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/privacy/requests/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    # 11. Platform Super Admin Console (ADR-0063, ADR-0064, ADR-0066 & ADR-0067)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/console/(?:metrics|kpis|tenants|actions)(?:/[^/]+)*/?$",
        method="GET",
        allowed_roles=[UserRole.SUPERADMIN],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/console/(?:actions|tenants/[^/]+/(?:suspend|restore))/?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    # 12. Secure Delegated Support Access Governance & Break-Glass (ADR-0068)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/support-access/(?:grants|[^/]+/revoke|request|impersonate)/?$",
        method="GET",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/support-access/(?:request|[^/]+/revoke|impersonate)/?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/support-access/[^/]+/active/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/support-access/[^/]+/[^/]+/(?:approve|reject|revoke)/?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    # 13. Platform Support Ticket System (ADR-0069)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/support/tickets/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/support/tickets/[^/]+/(?:assign|status|notes)/?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/support/tickets/[^/]+(?:/[^/]+)?/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/support/tickets/[^/]+/(?:create|[^/]+/(?:reply|close))/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    # 14. Global Centrally Curated Medicine Master Catalogue (ADR-0070)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/products/(?:[^/]+/(?:curation-history/)?)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/products/(?:[^/]+/(?:approve|recall|deprecate)/?)?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/products/[^/]+/?$",
        method="PUT",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/products/[^/]+/?$",
        method="PATCH",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/products/[^/]+/?$",
        method="DELETE",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    # 15. Tenant-Specific Medicine Configuration & Local Overrides (ADR-0071)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/medicine-configs/(?:by-product/[^/]+/?|[^/]+/?)*$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/medicine-configs/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/medicine-configs/[^/]+/?$",
        method="PUT",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/medicine-configs/[^/]+/?$",
        method="PATCH",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/medicine-configs/[^/]+/?$",
        method="DELETE",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    # 16. Platform Feature Flags & Tenant Targeting (ADR-0072)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/feature-flags/(?:[^/]+/(?:kill-switch|tenant-override(?:/[^/]+)?)?)?$",
        method="GET",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/feature-flags/(?:[^/]+/(?:kill-switch|tenant-override(?:/[^/]+)?)?)?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/feature-flags/[^/]+/?$",
        method="PUT",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/feature-flags/[^/]+/?$",
        method="PATCH",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/feature-flags/(?:[^/]+/(?:kill-switch|tenant-override/[^/]+)?|[^/]+)/?$",
        method="DELETE",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    # Tenant-Scoped Feature Flag Evaluation (ADR-0072)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/feature-flags/(?:eval(?:/[^/]+)?)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    # 17. Tenant Data Export Administration (ADR-0073)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/data-exports/(?:[^/]+/(?:approve|reject|revoke)/?)?$",
        method="GET",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/data-exports/(?:[^/]+/(?:approve|reject|revoke)/?)?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/data-exports/[^/]+/?$",
        method="DELETE",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    # Tenant-Scoped Data Export Request & Download (ADR-0073)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/data-exports/(?:[^/]+/(?:download/?)?)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/data-exports/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    # 18. Tenant Deletion Request & Statutory Retention Review (ADR-0074)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/deletion-assessments/(?:[^/]+/(?:assess|reject|purge)/?)?$",
        method="GET",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/deletion-assessments/(?:[^/]+/(?:assess|reject|purge)/?)?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/deletion-assessments/[^/]+/?$",
        method="DELETE",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    # Tenant-Scoped Deletion Request & Cancellation (ADR-0074)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/deletion-requests/(?:[^/]+/(?:cancel/?)?)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/deletion-requests/(?:[^/]+/(?:cancel/?)?)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    # 19. Cross-Tenant Metrics Safe Aggregation & Tenant Benchmarks (ADR-0075)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/cross-tenant-metrics/(?:[^/]+/(?:publish|archive)/?)?$",
        method="GET",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/cross-tenant-metrics/(?:[^/]+/(?:publish|archive)/?)?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    # Tenant-Scoped Safe Platform Benchmarks (ADR-0075)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/analytics/platform-benchmarks/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    # 20. Audited Platform System Announcements & Read Receipts (ADR-0076)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/announcements/(?:[^/]+/(?:publish|archive|stats)/?)?$",
        method="GET",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/announcements/(?:[^/]+/(?:publish|archive)/?)?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    # Tenant-Scoped Announcement Feeds & Acknowledgements (ADR-0076)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/announcements/(?:[^/]+/(?:acknowledge)/?)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/tenants/announcements/(?:[^/]+/(?:acknowledge)/?)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.TENANT,
        min_plan=PlanTier.BASIC,
    ),
    # Platform API Access Policies & MFA Step-Up (ADR-0077)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/access-policies/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR, UserRole.PLATFORM_AUDITOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/access-policies/(?:evaluate|verify-step-up)/?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR, UserRole.PLATFORM_AUDITOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/access-policies/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/access-policies/[^/]+/?$",
        method="PATCH",
        allowed_roles=[UserRole.SUPERADMIN],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/access-policies/[^/]+/?$",
        method="DELETE",
        allowed_roles=[UserRole.SUPERADMIN],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    # 22. Platform Admin Audit Search & Forbidden Payload Redaction (ADR-0078)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/audit-search/(?:export|[^/]+/)?$",
        method="GET",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR, UserRole.PLATFORM_AUDITOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/audit-search/(?:export/)?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR, UserRole.PLATFORM_AUDITOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/audit-search/[^/]+/?$",
        method="DELETE",
        allowed_roles=[],
        is_absolute_blocked=True,
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    # 23. Platform Tenant Onboarding Wizard (ADR-0079)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/onboarding-wizard/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/onboarding-wizard/(?:[^/]+/(?:update-step|complete|abandon)/?)?$",
        method="POST",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/onboarding-wizard/[^/]+/?$",
        method="PATCH",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/platform/onboarding-wizard/[^/]+/?$",
        method="DELETE",
        allowed_roles=[UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    # 12. Phase 12 POS Domain Model Endpoints (P221)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/customers/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/customers/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/customers/(?:[^/]+/)?$",
        method="PATCH",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/customers/(?:[^/]+/)?$",
        method="PUT",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/customers/(?:[^/]+/)?$",
        method="DELETE",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/(?:(?:quick-cart|[^/]+/(?:hold|resume|verify-rx|allocate|record-payment|finalize|scan-add|void|correct))/?)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking={
            "unit_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
            "total_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
        },
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/[^/]+/reverse/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking={
            "unit_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
            "total_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
        },
    ),
    # 13. Phase 12 Fast Search & Barcode Cart Endpoints (P222)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/[^/]+/cart-line/?$",
        method="PATCH",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking={
            "unit_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
            "total_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
        },
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/search/(?:(?:barcode-lookup|snapshot-cache|scan-events|unit-options)/?)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking={
            "unit_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
        },
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/search/refresh-snapshot/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.INVENTORY_CLERK,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # 14. Phase 12 Cart Item Unit Selection Endpoints (P223)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/[^/]+/select-unit/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking={
            "unit_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
            "total_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
        },
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/[^/]+/(?:unit-options|unit-audits)/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking={
            "unit_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
            "total_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
        },
    ),
    # 15. Phase 12 Cart Prices, Discounts, Taxes & Confirmation Endpoints (P224)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/[^/]+/(?:update-pricing|confirm-pricing|pricing-breakdown|approve-discount)/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking={
            "unit_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
            "total_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
        },
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/[^/]+/(?:pricing-breakdown|pricing-audits)/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking={
            "unit_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
            "total_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
        },
    ),
    # 16. Phase 12 Held & Resumed Carts Endpoints (P225)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/(?:expire-stale-holds|[^/]+/(?:expire-hold|cancel-hold))/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking={
            "unit_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
            "total_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
        },
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/(?:held|[^/]+/hold-audits)/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking={
            "unit_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
            "total_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
        },
    ),
    # 17. Phase 12 Sales Preorders & Delivery Fulfillment Endpoints (P226)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/deliveries/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking={
            "delivery_address_line": FieldMaskingPolicy(
                mask_type="MASKED",
                exempt_roles={
                    UserRole.SUPERADMIN,
                    UserRole.PHARMACY_OWNER,
                    UserRole.BRANCH_MANAGER,
                    UserRole.STAFF_PHARMACIST,
                    UserRole.AUDITOR,
                },
            ),
            "recipient_phone": FieldMaskingPolicy(
                mask_type="MASKED",
                exempt_roles={
                    UserRole.SUPERADMIN,
                    UserRole.PHARMACY_OWNER,
                    UserRole.BRANCH_MANAGER,
                    UserRole.STAFF_PHARMACIST,
                    UserRole.AUDITOR,
                },
            ),
        },
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/deliveries/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/deliveries/[^/]+/audits/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/deliveries/(?:[^/]+/(?:reserve-stock|pack|dispatch|out-for-delivery|deliver|fail-attempt|return-to-branch|cancel|reveal-address|redact-address))?/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/deliveries/[^/]+/?$",
        method="PATCH",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/deliveries/[^/]+/?$",
        method="DELETE",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # 18. Phase 12 POS Prescription Safety Gate & Antibiotic Register Endpoints (P227)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/[^/]+/(?:rx-gate|rx-audits|receipt|rx-gate/pharmacist-overrides|rx-gate/substitutions)/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/clinical-safety-disclaimers/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/drug-licenses/(?:[^/]+/)?(?:status-summary|reminders)?/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.AUDITOR,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/drug-licenses/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/drug-licenses/[^/]+/?$",
        method="PATCH",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/drug-licenses/[^/]+/?$",
        method="DELETE",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/pcb-registrations/(?:[^/]+/)?(?:evidence|branch-summary)?/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.AUDITOR,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/pcb-registrations/verify-gate/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/pcb-registrations/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/pcb-registrations/[^/]+/?$",
        method="PATCH",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/pcb-registrations/[^/]+/?$",
        method="DELETE",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # 19. Phase 13 Compliance: PCB Registration Tracking Endpoints (P252 / ADR-0252)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/pcb-registrations/(?:[^/]+/)?(?:evidence|branch-audit-summary)?/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.AUDITOR,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/pcb-registrations/verify-credential/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/pcb-registrations/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/pcb-registrations/[^/]+/?$",
        method="PATCH",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/pcb-registrations/[^/]+/?$",
        method="DELETE",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/inspection-evidence/(?:[^/]+/)?(?:document)?/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/inspection-evidence/(?:export-audit-package)?/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/inspection-evidence/[^/]+/?$",
        method="PATCH",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/inspection-evidence/[^/]+/?$",
        method="DELETE",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/incidents/(?:[^/]+/)?(?:branch-summary)?/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/incidents/(?:[^/]+/)?(?:transition-status|quarantine-stock)?/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/incidents/[^/]+/?$",
        method="PATCH",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/recalls/(?:[^/]+/)?(?:historical-impact)?/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/recalls/(?:[^/]+/)?(?:lift|check-dispensing-gate)?/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/temperature-reviews/(?:[^/]+/)?(?:check-batch-safety|verify-audit-hash)?/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/temperature-reviews/check-batch-safety/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/temperature-reviews/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/audit-exports/(?:[^/]+/)?(?:verify-manifest)?/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/audit-exports/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # 20. Phase 13 Compliance: Regulatory Reference Register Endpoints (P259 / ADR-0259)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/regulatory-references/(?:[^/]+/)?(?:overdue|verify-hash)?/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/regulatory-references/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/regulatory-references/[^/]+/?$",
        method="PATCH",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/regulatory-references/[^/]+/?$",
        method="PUT",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/compliance/regulatory-references/[^/]+/?$",
        method="DELETE",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/[^/]+/(?:attach-prescription|rx-gate/offline-preflight)/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/[^/]+/(?:verify-rx|reject-rx|rx-gate/offline-authorize|rx-gate/pharmacist-override|rx-gate/substitute-item)/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/antibiotic-register/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/antibiotic-register/(?:dgda-export|summary)/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/antibiotic-register/[^/]+/reverse/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # Phase 13 OTC Rx Status Versioning Endpoints (P247)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/catalog/classifications/(?:current|point-in-time|history|check-recall|[^/]+)/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/catalog/classifications/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/catalog/classifications/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
        ],
        scope=ScopeLevel.GLOBAL,
        min_plan=PlanTier.BASIC,
    ),
    # 19. Phase 12 POS Checkout FEFO Allocation & Override Endpoints (P228)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/[^/]+/(?:fefo-plan|allocation-audits)/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # 20. Phase 12 POS Cash & Verified Digital Split Payments, Journal & Close Reconciliation (P229)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/(?:close-reconciliation|[^/]+/payment-journals)/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
        field_masking={
            "unit_cost_bdt": FieldMaskingPolicy(mask_type="REDACTED", exempt_roles=COST_PRICE_EXEMPT_ROLES),
        },
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/(?:close-reconciliation|[^/]+/(?:split-payments|verify-payment-attempt|void-payment-attempt))/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/[^/]+/refund-split/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # 21. Phase 12 POS Approved Customer Credit Sales, Receivables & Due Collection (P230)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/(?:customer-dues/(?:[^/]+/)?|customers/[^/]+/(?:ledger|purchase-history)/?)$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
            UserRole.AUDITOR,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.STANDARD,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/customer-dues/[^/]+/collect/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.STANDARD,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/(?:customers/[^/]+/(?:approve-credit|suspend-credit)|customer-dues/(?:[^/]+/write-off|collections/[^/]+/reverse))/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.STANDARD,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/[^/]+/attach-customer/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # 22. Phase 12 POS Invoice Number Policy & Sequential Collision-Resistant Identifiers (P231)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/invoice-policies/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/invoice-policies/preview-next/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/invoice-policies/reserve-next/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/invoice-policies/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/invoice-policies/[^/]+/?$",
        method="PATCH",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # 23. Phase 12 POS 58mm/80mm Thermal & A4 Receipt Printing, Duplicate Watermarks & Audit (P232)
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/[^/]+/receipt-preview/?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/sales/[^/]+/print-receipt/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermissionRule(
        path_pattern=r"^/api/v2/pos/receipt-logs/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # -------------------------------------------------------------------------
    # 24. POS Cashier Shift & Drawer Variance (P233)
    # -------------------------------------------------------------------------
    EndpointPermission(
        path_pattern=r"^/api/v2/pos/shifts/(?:[^/]+/(?:cash-movement|close)/|open/)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermission(
        path_pattern=r"^/api/v2/pos/shifts/[^/]+/approve-variance/$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermission(
        path_pattern=r"^/api/v2/pos/shifts/(?:[^/]+/|active/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # -------------------------------------------------------------------------
    # 25. POS Sales Return & Refund (P234)
    # -------------------------------------------------------------------------
    EndpointPermission(
        path_pattern=r"^/api/v2/pos/returns/(?:[^/]+/)?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermission(
        path_pattern=r"^/api/v2/pos/returns/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    # -------------------------------------------------------------------------
    # 26. POS Daily Sales Close & Ledger Reconciliation (P237)
    # -------------------------------------------------------------------------
    EndpointPermission(
        path_pattern=r"^/api/v2/pos/daily-close/(?:[^/]+/|summary/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
            UserRole.FINANCE_OFFICER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermission(
        path_pattern=r"^/api/v2/pos/daily-close/submit/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermission(
        path_pattern=r"^/api/v2/pos/daily-close/[^/]+/approve/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermission(
        path_pattern=r"^/api/v2/pos/error-incidents/(?:[^/]+/|stock-alternatives/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermission(
        path_pattern=r"^/api/v2/pos/error-incidents/(?:report|stock-alternatives|[^/]+/resolve)/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.CASHIER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermission(
        path_pattern=r"^/api/v2/pos/e2e-qa/(?:[^/]+/)?$",
        method="GET",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
    EndpointPermission(
        path_pattern=r"^/api/v2/pos/e2e-qa/run-verification/?$",
        method="POST",
        allowed_roles=[
            UserRole.SUPERADMIN,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
        ],
        scope=ScopeLevel.BRANCH,
        min_plan=PlanTier.BASIC,
    ),
]


class EndpointPermissionMatrixEngine:
    """
    Central evaluation and field-masking engine implementing ADR-0005 & ADR-0059.
    """

    @classmethod
    def get_inventory(cls, role_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        rules = ENDPOINT_PERMISSION_RULES
        if role_filter:
            rules = [r for r in rules if role_filter in r.allowed_roles]
        return [r.to_dict() for r in rules]

    @classmethod
    def mask_value(cls, field_name: str, raw_value: Any, policy: FieldMaskingPolicy, role: str) -> Any:
        role_clean = str(role or "").upper().strip()
        exempt = {str(r).upper().strip() for r in policy.exempt_roles}
        if role_clean in exempt:
            return raw_value
        if policy.mask_type == "REDACTED":
            return "***REDACTED***"
        elif policy.mask_type == "PARTIAL_PHONE":
            val_str = str(raw_value)
            if len(val_str) >= 11:
                return f"{val_str[:6]}****{val_str[-4:]}"
            return "***REDACTED***"
        elif policy.mask_type == "HIDDEN":
            return None
        return "***REDACTED***"

    @classmethod
    def apply_field_masking(
        cls,
        data: Dict[str, Any],
        field_masking_rules: Dict[str, FieldMaskingPolicy],
        role: str,
    ) -> Dict[str, Any]:
        result = dict(data)
        for field_name, policy in field_masking_rules.items():
            if field_name in result:
                result[field_name] = cls.mask_value(field_name, result[field_name], policy, role)
        return result

    @classmethod
    def evaluate_request(
        cls,
        user: Any,
        method: str,
        path: str,
        target_branch_id: Optional[str] = None,
        tenant_plan: str = PlanTier.BASIC,
        raw_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Evaluates authorization, branch constraints, absolute blocks, plan tiers,
        and applies sensitive field masking.
        """
        method_upper = str(method or "").upper()
        norm_path = "/" + str(path or "").lstrip("/")

        # 1. Unauthenticated Caller Guard
        is_authenticated = bool(user and getattr(user, "is_authenticated", False))
        if not is_authenticated:
            return {
                "is_allowed": False,
                "status_code": 401,
                "error_code": "UNAUTHENTICATED",
                "message_en": "Authentication required. Bearer token missing or expired.",
                "message_bn": "প্রবেশাধিকার প্রয়োজন। বিয়ারার টোকেন অনুপস্থিত অথবা মেয়াদোত্তীর্ণ।",
                "denial_reason": "Caller is not authenticated.",
                "masked_data": None,
            }

        user_role = str(getattr(user, "role", "") or UserRole.CASHIER).upper()
        user_branch = str(getattr(user, "branch_id", "") or "").strip()
        user_tenant = str(getattr(user, "tenant_id", "") or "default-tenant").strip()

        # 2. Absolute Blocks (Zero Override)
        if method_upper == "DELETE":
            blocked_paths = ["/invoices", "/batches", "/prescriptions", "/audit-logs", "/payments"]
            if any(bp in norm_path for bp in blocked_paths):
                return {
                    "is_allowed": False,
                    "status_code": 403,
                    "error_code": "ABSOLUTE_SYSTEM_BLOCK",
                    "message_en": "Absolute Block: Destructive deletion of posted records is prohibited.",
                    "message_bn": "চূড়ান্ত নিষেধাজ্ঞা: পোস্ট করা রেকর্ড ডিলিট করা সম্পূর্ণ নিষিদ্ধ।",
                    "denial_reason": "Destructive deletion on posted records violates financial & clinical integrity rules.",
                    "masked_data": None,
                }

        # 3. Match Route Pattern
        matched_rule: Optional[EndpointPermissionRule] = None
        for rule in ENDPOINT_PERMISSION_RULES:
            if rule.method == method_upper and re.match(rule.path_pattern, norm_path):
                matched_rule = rule
                break

        if not matched_rule:
            return {
                "is_allowed": False,
                "status_code": 403,
                "error_code": "ENDPOINT_NOT_PERMITTED",
                "message_en": f"Method '{method_upper}' on path '{norm_path}' is not permitted under least-privilege defaults.",
                "message_bn": f"লিস্ট-প্রিভিলেজ নীতিমালায় '{norm_path}' পাথে '{method_upper}' মেথডের অনুমতি নেই।",
                "denial_reason": "No explicit authorization rule matches this endpoint and method.",
                "masked_data": None,
            }

        # 4. Check Absolute Block in Rule
        if matched_rule.is_absolute_blocked:
            return {
                "is_allowed": False,
                "status_code": 403,
                "error_code": "ABSOLUTE_SYSTEM_BLOCK",
                "message_en": "Action is permanently blocked by system safety invariants.",
                "message_bn": "এই কাজটি সিস্টেমের স্থায়ী সুরক্ষা নীতি দ্বারা নিষিদ্ধ।",
                "denial_reason": "Rule explicitly marks action as is_absolute_blocked.",
                "masked_data": None,
            }

        # 5. RBAC Role Whitelist Check
        if user_role not in matched_rule.allowed_roles:
            return {
                "is_allowed": False,
                "status_code": 403,
                "error_code": "ROLE_PERMISSION_DENIED",
                "message_en": f"Role '{user_role}' is not authorized for {method_upper} {norm_path}.",
                "message_bn": f"'{user_role}' রোলের ব্যবহারকারীর জন্য {norm_path} এ {method_upper} করার অনুমতি নেই।",
                "denial_reason": f"Role '{user_role}' not in allowed roles {matched_rule.allowed_roles}.",
                "masked_data": None,
            }

        # 6. Scope & Branch Boundary Enforcement
        if matched_rule.scope == ScopeLevel.BRANCH:
            # Branch-scoped roles: CASHIER, INVENTORY_CLERK, STAFF_PHARMACIST
            branch_bound_roles = [UserRole.CASHIER, UserRole.INVENTORY_CLERK, UserRole.STAFF_PHARMACIST]
            if user_role in branch_bound_roles and target_branch_id:
                clean_target_branch = str(target_branch_id).strip()
                if user_branch and clean_target_branch and user_branch != clean_target_branch:
                    # Log cross-branch violation
                    try:
                        from apps.audit_logging.models import AuditActionStatus, AuditLogEntry

                        AuditLogEntry.objects.create(
                            action="SECURITY_INCIDENT_BRANCH_VIOLATION",
                            resource_type="BRANCH_BOUNDARY",
                            tenant_id=user_tenant,
                            status=AuditActionStatus.DENIED,
                            metadata_json={
                                "event": "CROSS_BRANCH_ACCESS_ATTEMPT",
                                "user_role": user_role,
                                "user_branch": user_branch,
                                "target_branch": clean_target_branch,
                                "endpoint": norm_path,
                                "method": method_upper,
                            },
                        )
                    except Exception:
                        pass

                    return {
                        "is_allowed": False,
                        "status_code": 403,
                        "error_code": "FORBIDDEN_BRANCH_BOUNDARY",
                        "message_en": f"Cross-branch violation: User assigned to '{user_branch}' cannot access branch '{clean_target_branch}'.",
                        "message_bn": f"শাখা লঙ্ঘন: '{user_branch}' শাখার কর্মী '{clean_target_branch}' শাখায় প্রবেশ করতে পারে না।",
                        "denial_reason": "User is restricted to assigned branch.",
                        "masked_data": None,
                    }

        # 7. Subscription Plan Tier Gate
        if not PlanTier.satisfies(tenant_plan, matched_rule.min_plan):
            return {
                "is_allowed": False,
                "status_code": 403,
                "error_code": "INSUFFICIENT_PLAN_TIER",
                "message_en": f"This endpoint requires '{matched_rule.min_plan}' plan. Current plan is '{tenant_plan}'.",
                "message_bn": f"এই সুবিধার জন্য '{matched_rule.min_plan}' প্ল্যান প্রয়োজন। বর্তমান প্ল্যান '{tenant_plan}'।",
                "denial_reason": f"Required plan tier '{matched_rule.min_plan}' exceeds tenant tier '{tenant_plan}'.",
                "masked_data": None,
            }

        # 8. Field Masking
        masked_output = None
        if raw_data is not None:
            masked_output = cls.apply_field_masking(raw_data, matched_rule.field_masking, user_role)

        return {
            "is_allowed": True,
            "status_code": 200,
            "error_code": None,
            "message_en": "Access authorized under least-privilege matrix.",
            "message_bn": "লিস্ট-প্রিভিলেজ পারমিশন ম্যাট্রিক্স অনুযায়ী প্রবেশাধিকার অনুমোদিত হয়েছে।",
            "denial_reason": None,
            "rule": matched_rule.to_dict(),
            "masked_data": masked_output,
        }
