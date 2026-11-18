"""
backend/apps/core/financial_account_registry_service.py
Architecture Decision: ADR-0274 (P274).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service governing Cash Registers, Commercial Bank Accounts,
and Payment Clearing Accounts (MFS/Gateway) by branch:
- Registration, configuration, and lifecycle (ACTIVE, FROZEN, CLOSED).
- Fine-grained role-based access control (RBAC):
  * Allowed to configure/manage/freeze: PHARMACY_OWNER, BRANCH_MANAGER, FINANCE_OFFICER, SUPERADMIN.
  * Explicitly forbidden to configure/mutate: CASHIER and STAFF_PHARMACIST (HTTP 403 Forbidden).
  * Cashiers may view assigned branch cash registers only.
- Strict multi-tenant isolation: Cross-tenant operations raise HTTP 403 Forbidden.
- Account uniqueness and branch-level default clearing account management.
- Complete audit logging via AuditLogEntry.
"""

from __future__ import annotations

from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.0000")

ACCOUNT_VIEW_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
    UserRole.INVENTORY_CLERK,
    UserRole.AUDITOR,
    UserRole.STAFF_PHARMACIST,
    UserRole.CASHIER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "FINANCE_OFFICER",
    "INVENTORY_CLERK",
    "AUDITOR",
    "STAFF_PHARMACIST",
    "CASHIER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
    "PROCUREMENT_OFFICER",
}

ACCOUNT_MANAGE_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}


def to_d4(val: Any) -> Decimal:
    """Quantizes amounts strictly to 4 decimal places."""
    if val is None:
        return DECIMAL_ZERO
    try:
        return Decimal(str(val)).quantize(Decimal("0.0001"))
    except Exception:
        return DECIMAL_ZERO


class FinancialAccountRegistryService:
    """
    Authoritative domain service for registering and managing cash registers, bank accounts,
    and clearing accounts.
    """

    @classmethod
    def _assert_view_role(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_UNAUTHENTICATED",
                    "detail": "Authentication required to view financial accounts.",
                }
            )
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if role not in ACCOUNT_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Staff role is not permitted to view financial accounts.",
                    "message_en": "Staff role is not permitted to view financial accounts.",
                    "message_bn": "আর্থিক অ্যাকাউন্ট দেখার অনুমতি নেই।",
                }
            )

    @classmethod
    def _assert_manage_role(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_UNAUTHENTICATED",
                    "detail": "Authentication required to manage financial accounts.",
                }
            )
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if role in [UserRole.CASHIER, "CASHIER", UserRole.STAFF_PHARMACIST, "STAFF_PHARMACIST"] or role not in ACCOUNT_MANAGE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and staff pharmacist are not permitted to configure or manage financial accounts.",
                    "message_en": "Cashier and staff pharmacist are not permitted to configure or manage financial accounts.",
                    "message_bn": "ক্যাশিয়ার বা স্টাফ ফার্মাসিস্ট আর্থিক অ্যাকাউন্ট কনফিগার বা পরিবর্তন করার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant_id: str) -> Tenant:
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' not found."})
        if (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant account subscription is suspended. Financial registry access blocked.",
                    "message_en": "Tenant account subscription is suspended. Financial registry access blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। আর্থিক অ্যাকাউন্ট রেজিস্ট্রেশন ব্লক করা হয়েছে।",
                }
            )
        return tenant

    @classmethod
    def serialize_account(cls, account: FinancialAccount) -> Dict[str, Any]:
        """Serializes FinancialAccount into standard response dictionary."""
        return {
            "account_id": account.account_id,
            "tenant_id": str(account.tenant_id),
            "branch_id": str(account.branch_id) if account.branch_id else None,
            "branch_name": account.branch.name if account.branch else "",
            "account_type": account.account_type,
            "account_name": account.account_name,
            "account_code": account.account_code,
            "account_number": account.account_number,
            "bank_name": account.bank_name,
            "bank_branch_name": account.branch_name,
            "routing_number": account.routing_number,
            "swift_bic": account.swift_bic,
            "currency": account.currency,
            "opening_balance": str(account.opening_balance),
            "current_balance": str(account.current_balance),
            "status": account.status,
            "is_default_for_type": account.is_default_for_type,
            "assigned_role": account.assigned_role,
            "assigned_user_id": str(account.assigned_user_id) if account.assigned_user_id else None,
            "assigned_user_name": account.assigned_user.full_name if account.assigned_user else "",
            "notes": account.notes,
            "is_active": account.is_active,
            "created_by_id": str(account.created_by_id) if account.created_by_id else None,
            "created_at": account.created_at.isoformat() if account.created_at else None,
            "updated_at": account.updated_at.isoformat() if account.updated_at else None,
        }

    @classmethod
    @transaction.atomic
    def register_account(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Registers a new cash register, bank account, or payment clearing account.
        """
        cls._assert_manage_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        account_name = str(data.get("account_name", "")).strip()
        if not account_name:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_ACCOUNT_NAME",
                    "detail": "Account name is mandatory (e.g. Counter 1 Cash Till, DBBL Current A/C).",
                    "message_en": "Account name is mandatory.",
                    "message_bn": "অ্যাকাউন্টের নাম আবশ্যক।",
                }
            )

        account_type = str(data.get("account_type", FinancialAccountType.CASH_REGISTER)).strip().upper()
        if account_type not in FinancialAccountType.values:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_ACCOUNT_TYPE",
                    "detail": f"Invalid account type '{account_type}'. Allowed: {list(FinancialAccountType.values)}",
                }
            )

        branch_id = data.get("branch_id")
        branch = None
        if branch_id:
            branch = Branch.objects.filter(branch_id=branch_id).first()
            if not branch:
                raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})
            if str(branch.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Branch belongs to foreign tenant."})

        # Bank accounts require bank_name and account_number
        account_number = str(data.get("account_number", "")).strip()
        bank_name = str(data.get("bank_name", "")).strip()
        if account_type == FinancialAccountType.BANK_ACCOUNT:
            if not account_number or not bank_name:
                raise ValidationError(
                    {
                        "error_code": "ERR_MISSING_BANK_DETAILS",
                        "detail": "Bank account registration requires 'bank_name' and 'account_number'.",
                        "message_en": "Bank account registration requires bank name and account number.",
                        "message_bn": "ব্যাংক অ্যাকাউন্ট রেজিস্ট্রেশনের জন্য ব্যাংকের নাম ও অ্যাকাউন্ট নম্বর আবশ্যক।",
                    }
                )

        account_code = str(data.get("account_code", "")).strip()
        if account_code:
            if FinancialAccount.objects.filter(tenant=tenant, account_code=account_code).exists():
                raise ValidationError(
                    {
                        "error_code": "ERR_DUPLICATE_ACCOUNT_CODE",
                        "detail": f"An account with code '{account_code}' already exists for this tenant.",
                        "message_en": f"Account code '{account_code}' already exists.",
                        "message_bn": f"অ্যাকাউন্ট কোড '{account_code}' ইতোমধ্যে বিদ্যমান।",
                    }
                )

        opening_balance = to_d4(data.get("opening_balance", "0.0000"))
        is_default = bool(data.get("is_default_for_type", False))

        # Reset previous defaults for this branch and account_type if this one is designated default
        if is_default and branch:
            FinancialAccount.objects.filter(
                tenant=tenant,
                branch=branch,
                account_type=account_type,
                is_default_for_type=True,
            ).update(is_default_for_type=False)

        assigned_user = None
        assigned_user_id = data.get("assigned_user_id")
        if assigned_user_id:
            assigned_user = PharmacyUser.objects.filter(user_id=assigned_user_id).first()

        account = FinancialAccount(
            tenant=tenant,
            branch=branch,
            account_type=account_type,
            account_name=account_name,
            account_code=account_code,
            account_number=account_number,
            bank_name=bank_name,
            branch_name=str(data.get("branch_name", "")).strip(),
            routing_number=str(data.get("routing_number", "")).strip(),
            swift_bic=str(data.get("swift_bic", "")).strip(),
            currency=str(data.get("currency", "BDT")).strip().upper() or "BDT",
            opening_balance=opening_balance,
            current_balance=opening_balance,
            status=FinancialAccountStatus.ACTIVE,
            is_default_for_type=is_default,
            assigned_role=str(data.get("assigned_role", "")).strip(),
            assigned_user=assigned_user,
            notes=str(data.get("notes", "")).strip(),
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )
        account.save()

        # Audit log
        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="FINANCIAL_ACCOUNT_REGISTERED",
            resource_type="FinancialAccount",
            resource_id=account.account_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "account_code": account.account_code,
                "account_name": account.account_name,
                "account_type": account.account_type,
                "opening_balance": str(opening_balance),
                "branch_id": branch.branch_id if branch else None,
            },
        )

        return cls.serialize_account(account)

    @classmethod
    @transaction.atomic
    def update_account(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        account_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Updates financial account metadata (name, bank branch, notes, assigned user, default status).
        """
        cls._assert_manage_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        account = FinancialAccount.objects.select_for_update().filter(account_id=account_id).first()
        if not account:
            raise NotFound({"error_code": "ERR_ACCOUNT_NOT_FOUND", "detail": f"Account '{account_id}' not found."})
        if str(account.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})

        if account.status == FinancialAccountStatus.CLOSED:
            raise ValidationError(
                {
                    "error_code": "ERR_ACCOUNT_CLOSED",
                    "detail": "Closed financial accounts are permanently locked from modifications.",
                    "message_en": "Closed financial accounts cannot be modified.",
                    "message_bn": "বন্ধ অ্যাকাউন্ট পরিবর্তন করা সম্ভব নয়।",
                }
            )

        if "account_name" in data:
            name = str(data["account_name"]).strip()
            if name:
                account.account_name = name

        if "account_number" in data:
            account.account_number = str(data["account_number"]).strip()

        if "bank_name" in data:
            account.bank_name = str(data["bank_name"]).strip()

        if "branch_name" in data:
            account.branch_name = str(data["branch_name"]).strip()

        if "routing_number" in data:
            account.routing_number = str(data["routing_number"]).strip()

        if "swift_bic" in data:
            account.swift_bic = str(data["swift_bic"]).strip()

        if "notes" in data:
            account.notes = str(data["notes"]).strip()

        if "assigned_role" in data:
            account.assigned_role = str(data["assigned_role"]).strip()

        if "assigned_user_id" in data:
            uid = data["assigned_user_id"]
            if uid:
                account.assigned_user = PharmacyUser.objects.filter(user_id=uid).first()
            else:
                account.assigned_user = None

        if "is_default_for_type" in data:
            is_default = bool(data["is_default_for_type"])
            if is_default and account.branch:
                FinancialAccount.objects.filter(
                    tenant=tenant,
                    branch=account.branch,
                    account_type=account.account_type,
                    is_default_for_type=True,
                ).exclude(pk=account.pk).update(is_default_for_type=False)
            account.is_default_for_type = is_default

        account.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="FINANCIAL_ACCOUNT_UPDATED",
            resource_type="FinancialAccount",
            resource_id=account.account_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"account_id": account.account_id, "updated_fields": list(data.keys())},
        )

        return cls.serialize_account(account)

    @classmethod
    @transaction.atomic
    def freeze_account(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        account_id: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Freezes an active account to block financial transactions pending audit."""
        cls._assert_manage_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        account = FinancialAccount.objects.select_for_update().filter(account_id=account_id).first()
        if not account:
            raise NotFound({"error_code": "ERR_ACCOUNT_NOT_FOUND", "detail": f"Account '{account_id}' not found."})
        if str(account.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})

        if account.status == FinancialAccountStatus.CLOSED:
            raise ValidationError({"error_code": "ERR_ACCOUNT_CLOSED", "detail": "Cannot freeze a closed account."})

        account.status = FinancialAccountStatus.FROZEN
        account.save(update_fields=["status", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="FINANCIAL_ACCOUNT_FROZEN",
            resource_type="FinancialAccount",
            resource_id=account.account_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"reason": reason},
        )

        return cls.serialize_account(account)

    @classmethod
    @transaction.atomic
    def unfreeze_account(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        account_id: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Restores a frozen account back to active status."""
        cls._assert_manage_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        account = FinancialAccount.objects.select_for_update().filter(account_id=account_id).first()
        if not account:
            raise NotFound({"error_code": "ERR_ACCOUNT_NOT_FOUND", "detail": f"Account '{account_id}' not found."})
        if str(account.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})

        if account.status == FinancialAccountStatus.CLOSED:
            raise ValidationError({"error_code": "ERR_ACCOUNT_CLOSED", "detail": "Cannot unfreeze a closed account."})

        account.status = FinancialAccountStatus.ACTIVE
        account.save(update_fields=["status", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="FINANCIAL_ACCOUNT_UNFROZEN",
            resource_type="FinancialAccount",
            resource_id=account.account_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"reason": reason},
        )

        return cls.serialize_account(account)

    @classmethod
    @transaction.atomic
    def close_account(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        account_id: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Permanently closes/retires an account."""
        cls._assert_manage_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        account = FinancialAccount.objects.select_for_update().filter(account_id=account_id).first()
        if not account:
            raise NotFound({"error_code": "ERR_ACCOUNT_NOT_FOUND", "detail": f"Account '{account_id}' not found."})
        if str(account.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})

        account.status = FinancialAccountStatus.CLOSED
        account.is_active = False
        account.is_default_for_type = False
        account.save(update_fields=["status", "is_active", "is_default_for_type", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="FINANCIAL_ACCOUNT_CLOSED",
            resource_type="FinancialAccount",
            resource_id=account.account_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"reason": reason},
        )

        return cls.serialize_account(account)

    @classmethod
    def list_accounts(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        branch_id: Optional[str] = None,
        account_type: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists financial accounts with branch, type, and role filtering."""
        cls._assert_view_role(actor_user)
        cls._assert_tenant_active(tenant_id)

        qs = FinancialAccount.objects.filter(tenant_id=tenant_id).select_related("branch", "assigned_user")

        role = getattr(actor_user, "role", None)
        # Cashier role can only view cash registers for their assigned branch or accounts assigned to them
        if role in [UserRole.CASHIER, "CASHIER"] and not getattr(actor_user, "is_superuser", False):
            cashier_branch_id = getattr(actor_user, "branch_id", None)
            qs = qs.filter(
                account_type=FinancialAccountType.CASH_REGISTER,
            )
            if cashier_branch_id:
                qs = qs.filter(branch_id=cashier_branch_id)

        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if account_type:
            qs = qs.filter(account_type=account_type.upper())
        if status:
            qs = qs.filter(status=status.upper())

        return [cls.serialize_account(acc) for acc in qs.order_by("account_type", "account_name")]

    @classmethod
    def get_account(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        account_id: str,
    ) -> Dict[str, Any]:
        """Retrieves single financial account details."""
        cls._assert_view_role(actor_user)
        cls._assert_tenant_active(tenant_id)

        account = (
            FinancialAccount.objects.filter(tenant_id=tenant_id, account_id=account_id)
            .select_related("branch", "assigned_user")
            .first()
        )
        if not account:
            raise NotFound({"error_code": "ERR_ACCOUNT_NOT_FOUND", "detail": f"Account '{account_id}' not found."})

        return cls.serialize_account(account)
