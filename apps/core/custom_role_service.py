"""
Custom Role Builder & Permission-Key Catalog Service (P103 / ADR-0103).
Provides permission catalog, custom role creation, editing, cloning, archiving,
preview, and enforcement of protected clinical and financial safety minimums.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Set

from rest_framework.exceptions import ValidationError

from apps.core.models import (
    CustomRoleDefinition,
    StaffRole,
    Tenant,
)
from apps.core.postgres_service import CrossTenantOperationError

logger = logging.getLogger("pharmacy.custom_roles")


class CustomRoleService:
    """Business logic for permission catalog, custom role builder, and safety guardrails."""

    BLOCKED_PERMISSIONS: Set[str] = {
        "system:hard_delete",
        "stock:dispense_expired",
        "auth:bypass_tenant_isolation",
    }

    PERMISSION_CATALOG: Dict[str, Any] = {
        "version": "2.0.0",
        "total_permissions": 20,
        "categories": [
            {
                "category_id": "clinical",
                "category_name": "Clinical & Dispensing",
                "permissions": [
                    {
                        "key": "dispense:rx_antibiotics",
                        "name": "Dispense Rx & Antibiotics",
                        "safety_level": "PROTECTED_PCB",
                        "description": "Dispense prescription-only medications and regulated antibiotics (DGDA compliance requires verified PCB registration).",
                    },
                    {
                        "key": "rx:verify",
                        "name": "Prescription Clinical Verification",
                        "safety_level": "PROTECTED_PCB",
                        "description": "Perform clinical verification on patient prescriptions before dispensing.",
                    },
                    {
                        "key": "rx:view_attachments",
                        "name": "View Patient Prescription Images",
                        "safety_level": "SENSITIVE",
                        "description": "Access sensitive patient prescription scans and PHI.",
                    },
                ],
            },
            {
                "category_id": "pos",
                "category_name": "Point of Sale & Register",
                "permissions": [
                    {
                        "key": "sales:checkout",
                        "name": "Process Sales Invoices",
                        "safety_level": "STANDARD",
                        "description": "Create and commit retail sales transactions at the counter.",
                    },
                    {
                        "key": "sales:void",
                        "name": "Void / Reverse Finalized Sale",
                        "safety_level": "SENSITIVE",
                        "description": "Cancel or reverse already posted sales invoices (maker-checker audit required).",
                    },
                    {
                        "key": "sales:discount_override",
                        "name": "Discount Override Above Policy",
                        "safety_level": "SENSITIVE",
                        "description": "Apply customer discounts higher than standard branch discount limits.",
                    },
                    {
                        "key": "sales:view_reports",
                        "name": "View Counter Sales Summaries",
                        "safety_level": "STANDARD",
                        "description": "View daily register shift summaries and cashier sales reports.",
                    },
                    {
                        "key": "cash:shift_close",
                        "name": "Close Shift with Cash Discrepancy",
                        "safety_level": "SENSITIVE",
                        "description": "Authorize shift reconciliation and close cash drawer variance.",
                    },
                ],
            },
            {
                "category_id": "inventory",
                "category_name": "Inventory & Supply Chain",
                "permissions": [
                    {
                        "key": "inventory:view_stock",
                        "name": "Inspect Stock & Batch Expiry",
                        "safety_level": "STANDARD",
                        "description": "View real-time batch stock counts, unit conversions, and expiry dates.",
                    },
                    {
                        "key": "inventory:receive_po",
                        "name": "Post Goods Receipt (GRN)",
                        "safety_level": "STANDARD",
                        "description": "Receive supplier purchase lots and commit stock into inventory.",
                    },
                    {
                        "key": "inventory:adjust_writeoff",
                        "name": "Stock Adjustment & Damage Write-off",
                        "safety_level": "SENSITIVE",
                        "description": "Authorize inventory stock write-offs and damaged medicine adjustments.",
                    },
                    {
                        "key": "inventory:transfer",
                        "name": "Inter-Branch Stock Transfer",
                        "safety_level": "STANDARD",
                        "description": "Dispatch and receive inventory transfers between tenant branches.",
                    },
                ],
            },
            {
                "category_id": "finance",
                "category_name": "Finance & Accounting",
                "permissions": [
                    {
                        "key": "finance:due_repayment",
                        "name": "Record & Settle Customer Dues",
                        "safety_level": "STANDARD",
                        "description": "Track credit sales and record partial/full customer due repayments.",
                    },
                    {
                        "key": "finance:bad_debt_writeoff",
                        "name": "Authorize Customer Bad Debt Write-off",
                        "safety_level": "SENSITIVE",
                        "description": "Write off uncollectible customer retail credit balances.",
                    },
                    {
                        "key": "finance:journal_entries",
                        "name": "Post Manual Ledger Journals",
                        "safety_level": "SENSITIVE",
                        "description": "Post manual debit/credit general ledger adjustment entries.",
                    },
                    {
                        "key": "finance:executive_dashboard",
                        "name": "Executive Profitability Dashboard",
                        "safety_level": "SENSITIVE",
                        "description": "Access executive gross profit, net margins, COGS, and sync health.",
                    },
                ],
            },
            {
                "category_id": "admin",
                "category_name": "Staff & Tenant Administration",
                "permissions": [
                    {
                        "key": "staff:invite",
                        "name": "Dispatch Staff Invitations",
                        "safety_level": "STANDARD",
                        "description": "Invite personnel and delegate branch assignments.",
                    },
                    {
                        "key": "staff:manage_memberships",
                        "name": "Manage Staff Roster & Branch Scopes",
                        "safety_level": "STANDARD",
                        "description": "Assign primary and roving branch scopes or deactivate staff.",
                    },
                    {
                        "key": "roles:manage_custom",
                        "name": "Custom Role Builder Management",
                        "safety_level": "SENSITIVE",
                        "description": "Create, edit, clone, and archive tenant custom roles.",
                    },
                    {
                        "key": "devices:enroll",
                        "name": "Enroll POS Terminals & Leases",
                        "safety_level": "STANDARD",
                        "description": "Enroll hardware terminals and manage signed offline lease certificates.",
                    },
                    {
                        "key": "subscription:manage",
                        "name": "Manage SaaS Plan & Add-ons",
                        "safety_level": "SENSITIVE",
                        "description": "Upgrade subscription tiers, request support overrides, or adjust quotas.",
                    },
                    {
                        "key": "audit:export",
                        "name": "Export Statutory Audit Trail",
                        "safety_level": "SENSITIVE",
                        "description": "Export complete tenant audit event trails for regulatory review.",
                    },
                ],
            },
        ],
    }

    @classmethod
    def get_all_valid_keys(cls) -> Set[str]:
        keys = set()
        for cat in cls.PERMISSION_CATALOG["categories"]:
            for p in cat["permissions"]:
                keys.add(p["key"])
        return keys

    @classmethod
    def get_permission_catalog(cls) -> Dict[str, Any]:
        """Returns the canonical catalog of atomic permission keys."""
        return cls.PERMISSION_CATALOG

    @classmethod
    def _get_tenant(cls, tenant_id: str) -> Tenant:
        try:
            return Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise CrossTenantOperationError(f"Tenant '{tenant_id}' does not exist.")

    @classmethod
    def validate_safety_minimums(
        cls,
        permission_keys: List[str],
        requires_pcb_registration: bool,
        role_name: str = "",
    ) -> None:
        """Enforces clinical and financial safety invariants."""
        if not permission_keys or not isinstance(permission_keys, list):
            raise ValidationError(
                {
                    "error_code": "ERR_EMPTY_PERMISSIONS",
                    "message_en": "A custom role must have at least one permission key.",
                    "message_bn": "একটি কাস্টম রোলে কমপক্ষে একটি পারমিশন কি থাকতে হবে।",
                }
            )

        valid_keys = cls.get_all_valid_keys()
        for key in permission_keys:
            if key in cls.BLOCKED_PERMISSIONS:
                raise ValidationError(
                    {
                        "error_code": "ERR_BLOCKED_PERMISSION",
                        "message_en": f"Absolute danger action '{key}' cannot be granted in any custom role.",
                        "message_bn": f"বিপজ্জনক অ্যাকশন '{key}' কোনো কাস্টম রোলে বরাদ্দ করা নিষিদ্ধ।",
                    }
                )
            if key not in valid_keys:
                raise ValidationError(
                    {
                        "error_code": "ERR_UNKNOWN_PERMISSION_KEY",
                        "message_en": f"Permission key '{key}' does not exist in the permission catalog.",
                        "message_bn": f"পারমিশন কি '{key}' ক্যাটালগে নেই।",
                    }
                )

        # Safety Minimum: Prescription dispensing requires PCB credential
        if "dispense:rx_antibiotics" in permission_keys and not requires_pcb_registration:
            raise ValidationError(
                {
                    "error_code": "ERR_SAFETY_MINIMUM_PCB_REQUIRED",
                    "message_en": "Prescription antibiotic dispensing ('dispense:rx_antibiotics') requires verified PCB registration ('requires_pcb_registration=True').",
                    "message_bn": "প্রেসক্রিপশন অ্যান্টিবায়োটিক বিতরণের জন্য পিসিবি রেজিস্ট্রেশন বাধ্যতামূলক ('requires_pcb_registration=True')।",
                }
            )

    @classmethod
    def list_roles(
        cls,
        tenant_id: str,
        include_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        """Lists tenant custom roles and system reference role templates."""
        tenant = cls._get_tenant(tenant_id)
        qs = CustomRoleDefinition.objects.filter(tenant=tenant)
        if not include_archived:
            qs = qs.filter(is_archived=False)

        results = [cls._serialize_role(r) for r in qs]
        return results

    @classmethod
    def create_custom_role(
        cls,
        tenant_id: str,
        role_code: str,
        display_name: str,
        base_role: str,
        permission_keys: List[str],
        requires_pcb_registration: bool = False,
        description: str = "",
        created_by_username: str = "",
    ) -> Dict[str, Any]:
        """Creates a tenant-scoped custom role definition."""
        tenant = cls._get_tenant(tenant_id)

        clean_code = (role_code or "").strip().upper()
        if not re.match(r"^[A-Z0-9_]{3,64}$", clean_code):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_ROLE_CODE",
                    "message_en": "Role code must be 3-64 characters consisting of uppercase letters, numbers, and underscores.",
                    "message_bn": "রোল কোড অবশ্যই ৩-৬৪ অক্ষরের ক্যাপিটাল লেটার, নম্বর ও আন্ডারস্কোর হতে হবে।",
                }
            )

        clean_name = (display_name or "").strip()
        if len(clean_name) < 2:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_DISPLAY_NAME",
                    "message_en": "Role display name must be at least 2 characters.",
                    "message_bn": "রোলের প্রদর্শনী নাম কমপক্ষে ২ অক্ষরের হতে হবে।",
                }
            )

        clean_base = (base_role or StaffRole.CASHIER).strip().upper()
        if clean_base not in StaffRole.values and clean_base not in [
            "PHARMACY_OWNER",
            "STAFF_PHARMACIST",
            "INVENTORY_CLERK",
            "FINANCE_OFFICER",
        ]:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_BASE_ROLE",
                    "message_en": f"Base role '{base_role}' is not a valid staff role.",
                    "message_bn": f"বেইজ রোল '{base_role}' সঠিক নয়।",
                }
            )

        cls.validate_safety_minimums(permission_keys, requires_pcb_registration, clean_name)

        if CustomRoleDefinition.objects.filter(tenant=tenant, role_code=clean_code).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_ROLE_ALREADY_EXISTS",
                    "message_en": f"A role with code '{clean_code}' already exists for this tenant.",
                    "message_bn": f"এই টেন্যান্টে '{clean_code}' কোডের রোল ইতিমধ্যে বিদ্যমান।",
                }
            )

        role_id = f"role-{uuid.uuid4().hex[:12]}"
        role = CustomRoleDefinition.objects.create(
            role_id=role_id,
            tenant=tenant,
            role_code=clean_code,
            display_name=clean_name,
            description=(description or "").strip(),
            base_role=clean_base,
            permission_keys=list(set(permission_keys)),
            requires_pcb_registration=requires_pcb_registration,
            is_system_protected=False,
            is_archived=False,
            created_by_username=(created_by_username or "").strip(),
        )

        logger.info(
            "Created custom role %s (%s) for tenant %s with %d permissions",
            role.role_code,
            role.role_id,
            tenant_id,
            len(role.permission_keys),
        )
        return cls._serialize_role(role)

    @classmethod
    def update_custom_role(
        cls,
        tenant_id: str,
        role_id: str,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        permission_keys: Optional[List[str]] = None,
        requires_pcb_registration: Optional[bool] = None,
        base_role: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Updates an existing mutable custom role."""
        tenant = cls._get_tenant(tenant_id)
        try:
            role = CustomRoleDefinition.objects.get(role_id=role_id, tenant=tenant)
        except CustomRoleDefinition.DoesNotExist:
            raise CrossTenantOperationError(f"Role '{role_id}' does not exist for tenant '{tenant_id}'.")

        if role.is_system_protected:
            raise ValidationError(
                {
                    "error_code": "ERR_PROTECTED_ROLE_IMMUTABLE",
                    "message_en": "Built-in system roles are protected and cannot be modified.",
                    "message_bn": "বিল্ট-ইন সিস্টেম রোল সংরক্ষিত এবং সংশোধনযোগ্য নয়।",
                }
            )

        if display_name is not None:
            clean_name = display_name.strip()
            if len(clean_name) < 2:
                raise ValidationError({"error_code": "ERR_INVALID_DISPLAY_NAME", "message_en": "Name too short."})
            role.display_name = clean_name

        if description is not None:
            role.description = description.strip()

        if base_role is not None:
            clean_base = base_role.strip().upper()
            role.base_role = clean_base

        target_requires_pcb = requires_pcb_registration if requires_pcb_registration is not None else role.requires_pcb_registration
        target_keys = permission_keys if permission_keys is not None else role.permission_keys

        cls.validate_safety_minimums(target_keys, target_requires_pcb, role.display_name)
        role.permission_keys = list(set(target_keys))
        role.requires_pcb_registration = target_requires_pcb
        role.save()

        logger.info("Updated custom role %s (%s)", role.role_code, role_id)
        return cls._serialize_role(role)

    @classmethod
    def clone_role(
        cls,
        tenant_id: str,
        source_role_id: str,
        new_role_code: str,
        new_display_name: str,
        description: str = "",
        cloned_by_username: str = "",
    ) -> Dict[str, Any]:
        """Clones an existing role (system or custom) into a new custom role."""
        tenant = cls._get_tenant(tenant_id)
        try:
            source = CustomRoleDefinition.objects.get(role_id=source_role_id, tenant=tenant)
        except CustomRoleDefinition.DoesNotExist:
            raise CrossTenantOperationError(f"Source role '{source_role_id}' not found for tenant '{tenant_id}'.")

        clean_code = (new_role_code or "").strip().upper()
        if not re.match(r"^[A-Z0-9_]{3,64}$", clean_code):
            raise ValidationError({"error_code": "ERR_INVALID_ROLE_CODE", "message_en": "Invalid role code pattern."})

        clean_name = (new_display_name or "").strip()
        if len(clean_name) < 2:
            raise ValidationError({"error_code": "ERR_INVALID_DISPLAY_NAME", "message_en": "Display name too short."})

        if CustomRoleDefinition.objects.filter(tenant=tenant, role_code=clean_code).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_ROLE_ALREADY_EXISTS",
                    "message_en": f"A role with code '{clean_code}' already exists.",
                    "message_bn": f"'{clean_code}' কোডের রোল ইতিমধ্যে বিদ্যমান।",
                }
            )

        new_id = f"role-{uuid.uuid4().hex[:12]}"
        cloned = CustomRoleDefinition.objects.create(
            role_id=new_id,
            tenant=tenant,
            role_code=clean_code,
            display_name=clean_name,
            description=description.strip() or f"Cloned from {source.display_name}",
            base_role=source.base_role,
            permission_keys=list(source.permission_keys),
            requires_pcb_registration=source.requires_pcb_registration,
            is_system_protected=False,
            is_archived=False,
            cloned_from_id=source.role_id,
            created_by_username=cloned_by_username,
        )

        logger.info("Cloned role %s -> %s for tenant %s", source.role_code, clean_code, tenant_id)
        return cls._serialize_role(cloned)

    @classmethod
    def archive_role(
        cls,
        tenant_id: str,
        role_id: str,
        archived_by_username: str = "",
    ) -> Dict[str, Any]:
        """Soft archives a custom role."""
        tenant = cls._get_tenant(tenant_id)
        try:
            role = CustomRoleDefinition.objects.get(role_id=role_id, tenant=tenant)
        except CustomRoleDefinition.DoesNotExist:
            raise CrossTenantOperationError(f"Role '{role_id}' does not exist for tenant '{tenant_id}'.")

        if role.is_system_protected:
            raise ValidationError(
                {
                    "error_code": "ERR_PROTECTED_ROLE_IMMUTABLE",
                    "message_en": "Built-in system roles cannot be archived.",
                    "message_bn": "বিল্ট-ইন সিস্টেম রোল আর্কাইভ করা নিষিদ্ধ।",
                }
            )

        role.is_archived = True
        role.save()
        logger.info("Archived custom role %s (%s)", role.role_code, role_id)
        return cls._serialize_role(role)

    @classmethod
    def preview_role_permissions(
        cls,
        permission_keys: List[str],
        requires_pcb_registration: bool = False,
    ) -> Dict[str, Any]:
        """Provides dry-run analysis and Separation of Duties (SoD) evaluation for prospective roles."""
        cls.validate_safety_minimums(permission_keys, requires_pcb_registration)

        cat_breakdown: Dict[str, List[str]] = {
            "clinical": [],
            "pos": [],
            "inventory": [],
            "finance": [],
            "admin": [],
        }
        for cat in cls.PERMISSION_CATALOG["categories"]:
            cid = cat["category_id"]
            for p in cat["permissions"]:
                if p["key"] in permission_keys:
                    cat_breakdown[cid].append(p["key"])

        # Detect Separation of Duties conflicts
        sod_warnings: List[str] = []
        if "inventory:receive_po" in permission_keys and "inventory:adjust_writeoff" in permission_keys:
            sod_warnings.append("Role combines purchase lot goods receipt with inventory damage write-off authority.")
        if "sales:checkout" in permission_keys and "sales:void" in permission_keys:
            sod_warnings.append("Role combines checkout counter authority with sale void/reversal capability.")

        return {
            "total_permissions": len(permission_keys),
            "requires_pcb_registration": requires_pcb_registration,
            "clinical_dispensing_cleared": "dispense:rx_antibiotics" in permission_keys and requires_pcb_registration,
            "category_breakdown": cat_breakdown,
            "sod_warnings": sod_warnings,
        }

    @classmethod
    def _serialize_role(cls, role: CustomRoleDefinition) -> Dict[str, Any]:
        return {
            "role_id": role.role_id,
            "tenant_id": role.tenant_id,
            "role_code": role.role_code,
            "display_name": role.display_name,
            "description": role.description,
            "base_role": role.base_role,
            "permission_keys": role.permission_keys,
            "requires_pcb_registration": role.requires_pcb_registration,
            "is_system_protected": role.is_system_protected,
            "is_archived": role.is_archived,
            "cloned_from_id": role.cloned_from_id or None,
            "created_by": role.created_by_username or None,
            "created_at": role.created_at.isoformat(),
            "updated_at": role.updated_at.isoformat(),
        }
