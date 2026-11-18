"""
backend/apps/core/payment_method_registry_service.py
Architecture Decision: ADR-0275 (P275).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service governing payment method configuration (Cash, bKash, Nagad, Cards):
- Enforces strict zero-exposure invariant: Plaintext payment secrets, API keys, MPINs, or CVVs
  are prohibited from being received or persisted in the core application database.
- Tenant and branch-level scoping with optional branch override.
- Fine-grained role-based access control (RBAC):
  * Allowed to configure/manage/toggle: PHARMACY_OWNER, BRANCH_MANAGER, FINANCE_OFFICER, SUPERADMIN.
  * Explicitly forbidden to configure/mutate: CASHIER and STAFF_PHARMACIST (HTTP 403 Forbidden).
  * Cashier and staff pharmacist have read-only access for POS checkout.
- Cross-tenant isolation strictly enforced for branches and clearing accounts.
- Tender validation (limits, reference patterns, customer surcharges).
- Immutable audit log tracking with AuditLogEntry.
"""

from __future__ import annotations

from decimal import Decimal
import logging
import re
from typing import Any, Dict, List, Optional
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    FinancialAccount,
    PaymentMethod,
    PaymentMethodConfig,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")

SENSITIVE_KEY_NAMES = {
    "secret",
    "secret_key",
    "apikey",
    "api_key",
    "password",
    "pin",
    "mpin",
    "cvv",
    "cvc",
    "private_key",
    "auth_token",
    "access_token",
    "client_secret",
    "token_secret",
    "card_number",
    "card_pan",
}

PAYMENT_CONFIG_VIEW_ROLES = {
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

PAYMENT_CONFIG_MANAGE_ROLES = {
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


def to_d2(val: Any, default: str = "0.00") -> Decimal:
    """Quantizes decimal values strictly to 2 decimal places."""
    if val is None:
        return Decimal(default)
    try:
        return Decimal(str(val)).quantize(Decimal("0.01"))
    except Exception:
        return Decimal(default)


class PaymentMethodRegistryService:
    """
    Authoritative domain service governing payment method configurations.
    """

    @classmethod
    def _assert_zero_secrets(cls, data: Dict[str, Any]) -> None:
        """
        Inspects incoming payload to ensure no sensitive credentials or keys are passed in plaintext.
        """
        if not isinstance(data, dict):
            return

        for key, val in data.items():
            k_lower = str(key).strip().lower()
            if k_lower in SENSITIVE_KEY_NAMES or any(bad in k_lower for bad in ["secret", "apikey", "mpin", "cvv", "private_key"]):
                # Allow vault_secret_reference which is an opaque vault pointer like vault://mfs/bkash
                if k_lower == "vault_secret_reference":
                    continue
                if val:  # Non-empty sensitive content
                    raise ValidationError(
                        {
                            "error_code": "ERR_SENSITIVE_SECRET_EXPOSURE_BLOCKED",
                            "detail": (
                                f"Direct submission of sensitive payment secret or credential '{key}' is barred. "
                                f"Use external secrets vault reference instead."
                            ),
                            "message_en": "Direct submission of sensitive payment secrets is prohibited.",
                            "message_bn": "সরাসরি পেমেন্ট গেটওয়ে গোপন কোড, পিন বা পাসওয়ার্ড সংরক্ষণ সম্পূর্ণ নিষিদ্ধ।",
                        }
                    )

    @classmethod
    def _assert_view_role(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_UNAUTHENTICATED",
                    "detail": "Authentication required to view payment method configurations.",
                }
            )
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if role not in PAYMENT_CONFIG_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Staff role is not permitted to view payment configurations.",
                    "message_en": "Staff role is not permitted to view payment configurations.",
                    "message_bn": "পেমেন্ট মেথড কনফিগারেশন দেখার অনুমতি নেই।",
                }
            )

    @classmethod
    def _assert_manage_role(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_UNAUTHENTICATED",
                    "detail": "Authentication required to manage payment methods.",
                }
            )
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if role in [UserRole.CASHIER, "CASHIER", UserRole.STAFF_PHARMACIST, "STAFF_PHARMACIST"] or role not in PAYMENT_CONFIG_MANAGE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and staff pharmacist are not permitted to configure payment methods.",
                    "message_en": "Cashier and staff pharmacist are not permitted to configure payment methods.",
                    "message_bn": "ক্যাশিয়ার বা স্টাফ ফার্মাসিস্ট পেমেন্ট মেথড কনফিগার বা পরিবর্তন করার অনুমতিপ্রাপ্ত নন।",
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
                    "detail": "Tenant account subscription is suspended. Payment configuration access blocked.",
                    "message_en": "Tenant account subscription is suspended. Payment configuration access blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। পেমেন্ট কনফিগারেশন বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    @classmethod
    def serialize_config(cls, cfg: PaymentMethodConfig) -> Dict[str, Any]:
        """Serializes PaymentMethodConfig into standard dictionary."""
        return {
            "config_id": cfg.config_id,
            "tenant_id": str(cfg.tenant_id),
            "branch_id": str(cfg.branch_id) if cfg.branch_id else None,
            "branch_name": cfg.branch.name if cfg.branch else "Tenant-Wide",
            "method_type": cfg.method_type,
            "display_name": cfg.display_name,
            "display_order": cfg.display_order,
            "is_enabled": cfg.is_enabled,
            "clearing_account_id": str(cfg.clearing_account_id) if cfg.clearing_account_id else None,
            "clearing_account_name": cfg.clearing_account.account_name if cfg.clearing_account else None,
            "requires_reference": cfg.requires_reference,
            "reference_label": cfg.reference_label,
            "reference_pattern": cfg.reference_pattern,
            "customer_surcharge_percentage": str(cfg.customer_surcharge_percentage),
            "customer_surcharge_flat_bdt": str(cfg.customer_surcharge_flat_bdt),
            "merchant_fee_percentage": str(cfg.merchant_fee_percentage),
            "min_transaction_bdt": str(cfg.min_transaction_bdt),
            "max_transaction_bdt": str(cfg.max_transaction_bdt),
            "allow_offline": cfg.allow_offline,
            "instructions_en": cfg.instructions_en,
            "instructions_bn": cfg.instructions_bn,
            "vault_secret_reference": cfg.vault_secret_reference,
            "notes": cfg.notes,
            "created_by_id": str(cfg.created_by_id) if cfg.created_by_id else None,
            "created_at": cfg.created_at.isoformat() if cfg.created_at else None,
            "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
        }

    @classmethod
    @transaction.atomic
    def configure_payment_method(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Creates or updates a payment method configuration for the tenant or branch.
        """
        cls._assert_manage_role(actor_user)
        cls._assert_zero_secrets(data)
        tenant = cls._assert_tenant_active(tenant_id)

        method_type = str(data.get("method_type", "")).strip().upper()
        if not method_type or method_type not in PaymentMethod.values:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_PAYMENT_METHOD",
                    "detail": f"Invalid payment method '{method_type}'. Allowed: {list(PaymentMethod.values)}",
                    "message_en": f"Invalid payment method '{method_type}'.",
                    "message_bn": "অকার্যকর পেমেন্ট মেথড।",
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

        clearing_account = None
        clearing_account_id = data.get("clearing_account_id")
        if clearing_account_id:
            clearing_account = FinancialAccount.objects.filter(account_id=clearing_account_id).first()
            if not clearing_account:
                raise NotFound({"error_code": "ERR_ACCOUNT_NOT_FOUND", "detail": f"Clearing account '{clearing_account_id}' not found."})
            if str(clearing_account.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Clearing account belongs to foreign tenant."})

        min_amt = to_d2(data.get("min_transaction_bdt", "0.00"))
        max_amt = to_d2(data.get("max_transaction_bdt", "500000.00"))
        if min_amt > max_amt:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_TRANSACTION_LIMITS",
                    "detail": f"Minimum transaction ({min_amt}) cannot exceed maximum transaction ({max_amt}).",
                }
            )

        surcharge_pct = to_d2(data.get("customer_surcharge_percentage", "0.00"))
        if surcharge_pct < DECIMAL_ZERO or surcharge_pct > Decimal("100.00"):
            raise ValidationError({"error_code": "ERR_INVALID_SURCHARGE", "detail": "Surcharge percentage must be between 0 and 100."})

        merch_fee_pct = to_d2(data.get("merchant_fee_percentage", "0.00"))
        if merch_fee_pct < DECIMAL_ZERO or merch_fee_pct > Decimal("100.00"):
            raise ValidationError({"error_code": "ERR_INVALID_MERCHANT_FEE", "detail": "Merchant fee percentage must be between 0 and 100."})

        # Check existing config for same tenant, branch, and method_type
        existing = PaymentMethodConfig.objects.filter(tenant=tenant, branch=branch, method_type=method_type).first()
        if existing:
            # Update existing
            return cls.update_payment_method(
                tenant_id=tenant_id,
                actor_user=actor_user,
                config_id=existing.config_id,
                data=data,
            )

        display_name = str(data.get("display_name", "")).strip() or dict(PaymentMethod.choices).get(method_type, method_type)
        display_order = int(data.get("display_order", 10))
        is_enabled = bool(data.get("is_enabled", True))
        requires_ref = bool(data.get("requires_reference", False))
        ref_label = str(data.get("reference_label", "")).strip()
        ref_pattern = str(data.get("reference_pattern", "")).strip()
        allow_offline = bool(data.get("allow_offline", method_type == PaymentMethod.CASH))
        vault_ref = str(data.get("vault_secret_reference", "")).strip()

        cfg = PaymentMethodConfig(
            tenant=tenant,
            branch=branch,
            method_type=method_type,
            display_name=display_name,
            display_order=display_order,
            is_enabled=is_enabled,
            clearing_account=clearing_account,
            requires_reference=requires_ref,
            reference_label=ref_label,
            reference_pattern=ref_pattern,
            customer_surcharge_percentage=surcharge_pct,
            customer_surcharge_flat_bdt=to_d2(data.get("customer_surcharge_flat_bdt", "0.00")),
            merchant_fee_percentage=merch_fee_pct,
            min_transaction_bdt=min_amt,
            max_transaction_bdt=max_amt,
            allow_offline=allow_offline,
            instructions_en=str(data.get("instructions_en", "")).strip(),
            instructions_bn=str(data.get("instructions_bn", "")).strip(),
            vault_secret_reference=vault_ref,
            notes=str(data.get("notes", "")).strip(),
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )
        cfg.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PAYMENT_METHOD_CONFIGURED",
            resource_type="PaymentMethodConfig",
            resource_id=cfg.config_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "method_type": cfg.method_type,
                "display_name": cfg.display_name,
                "branch_id": branch.branch_id if branch else None,
                "is_enabled": cfg.is_enabled,
            },
        )

        return cls.serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def update_payment_method(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        config_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Updates payment method configuration metadata, limits, fees, or status."""
        cls._assert_manage_role(actor_user)
        cls._assert_zero_secrets(data)
        tenant = cls._assert_tenant_active(tenant_id)

        cfg = PaymentMethodConfig.objects.select_for_update().filter(config_id=config_id).first()
        if not cfg:
            raise NotFound({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Payment configuration '{config_id}' not found."})
        if str(cfg.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})

        if "display_name" in data:
            name = str(data["display_name"]).strip()
            if name:
                cfg.display_name = name

        if "display_order" in data:
            cfg.display_order = int(data["display_order"])

        if "is_enabled" in data:
            cfg.is_enabled = bool(data["is_enabled"])

        if "clearing_account_id" in data:
            acc_id = data["clearing_account_id"]
            if acc_id:
                acc = FinancialAccount.objects.filter(account_id=acc_id).first()
                if not acc:
                    raise NotFound({"error_code": "ERR_ACCOUNT_NOT_FOUND", "detail": "Clearing account not found."})
                if str(acc.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Clearing account cross-tenant access forbidden."})
                cfg.clearing_account = acc
            else:
                cfg.clearing_account = None

        if "requires_reference" in data:
            cfg.requires_reference = bool(data["requires_reference"])

        if "reference_label" in data:
            cfg.reference_label = str(data["reference_label"]).strip()

        if "reference_pattern" in data:
            cfg.reference_pattern = str(data["reference_pattern"]).strip()

        if "customer_surcharge_percentage" in data:
            s_pct = to_d2(data["customer_surcharge_percentage"])
            if s_pct < DECIMAL_ZERO or s_pct > Decimal("100.00"):
                raise ValidationError({"error_code": "ERR_INVALID_SURCHARGE", "detail": "Surcharge must be 0-100%."})
            cfg.customer_surcharge_percentage = s_pct

        if "customer_surcharge_flat_bdt" in data:
            cfg.customer_surcharge_flat_bdt = to_d2(data["customer_surcharge_flat_bdt"])

        if "merchant_fee_percentage" in data:
            m_pct = to_d2(data["merchant_fee_percentage"])
            if m_pct < DECIMAL_ZERO or m_pct > Decimal("100.00"):
                raise ValidationError({"error_code": "ERR_INVALID_MERCHANT_FEE", "detail": "Merchant fee must be 0-100%."})
            cfg.merchant_fee_percentage = m_pct

        if "min_transaction_bdt" in data:
            cfg.min_transaction_bdt = to_d2(data["min_transaction_bdt"])

        if "max_transaction_bdt" in data:
            cfg.max_transaction_bdt = to_d2(data["max_transaction_bdt"])

        if cfg.min_transaction_bdt > cfg.max_transaction_bdt:
            raise ValidationError({"error_code": "ERR_INVALID_TRANSACTION_LIMITS", "detail": "Min amount cannot exceed max amount."})

        if "allow_offline" in data:
            cfg.allow_offline = bool(data["allow_offline"])

        if "instructions_en" in data:
            cfg.instructions_en = str(data["instructions_en"]).strip()

        if "instructions_bn" in data:
            cfg.instructions_bn = str(data["instructions_bn"]).strip()

        if "vault_secret_reference" in data:
            cfg.vault_secret_reference = str(data["vault_secret_reference"]).strip()

        if "notes" in data:
            cfg.notes = str(data["notes"]).strip()

        cfg.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PAYMENT_METHOD_UPDATED",
            resource_type="PaymentMethodConfig",
            resource_id=cfg.config_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"config_id": cfg.config_id, "updated_fields": list(data.keys())},
        )

        return cls.serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def toggle_payment_method(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        config_id: str,
        is_enabled: bool,
    ) -> Dict[str, Any]:
        """Enables or disables a payment method."""
        cls._assert_manage_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        cfg = PaymentMethodConfig.objects.select_for_update().filter(config_id=config_id).first()
        if not cfg:
            raise NotFound({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Payment configuration '{config_id}' not found."})
        if str(cfg.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})

        cfg.is_enabled = is_enabled
        cfg.save(update_fields=["is_enabled", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PAYMENT_METHOD_TOGGLED",
            resource_type="PaymentMethodConfig",
            resource_id=cfg.config_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"method_type": cfg.method_type, "is_enabled": is_enabled},
        )

        return cls.serialize_config(cfg)

    @classmethod
    def list_payment_methods(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        branch_id: Optional[str] = None,
        only_enabled: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Lists payment methods configured for tenant, with optional branch filtering.
        If a cashier requests, automatically filters by their branch.
        """
        cls._assert_view_role(actor_user)
        cls._assert_tenant_active(tenant_id)

        qs = PaymentMethodConfig.objects.filter(tenant_id=tenant_id).select_related("branch", "clearing_account")

        # Determine effective branch
        eff_branch_id = branch_id
        role = getattr(actor_user, "role", None)
        if role in [UserRole.CASHIER, "CASHIER"]:
            cashier_branch_id = getattr(actor_user, "branch_id", None)
            if cashier_branch_id:
                eff_branch_id = cashier_branch_id

        if eff_branch_id:
            # Query branch-specific overrides OR tenant-wide defaults (branch is null)
            from django.db.models import Q
            qs = qs.filter(Q(branch_id=eff_branch_id) | Q(branch__isnull=True))
        else:
            if branch_id:
                qs = qs.filter(branch_id=branch_id)

        if only_enabled:
            qs = qs.filter(is_enabled=True)

        return [cls.serialize_config(cfg) for cfg in qs.order_by("display_order", "display_name")]

    @classmethod
    def get_payment_method(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        config_id: str,
    ) -> Dict[str, Any]:
        """Retrieves single payment method configuration details."""
        cls._assert_view_role(actor_user)
        cls._assert_tenant_active(tenant_id)

        cfg = (
            PaymentMethodConfig.objects.filter(tenant_id=tenant_id, config_id=config_id)
            .select_related("branch", "clearing_account")
            .first()
        )
        if not cfg:
            raise NotFound({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Payment configuration '{config_id}' not found."})

        return cls.serialize_config(cfg)

    @classmethod
    def validate_tender(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        config_id: str,
        amount_bdt: Decimal,
        reference: str = "",
        is_offline: bool = False,
    ) -> Dict[str, Any]:
        """
        Validates tender amount, reference format, offline eligibility, and surcharge calculation.
        """
        cls._assert_view_role(actor_user)
        cls._assert_tenant_active(tenant_id)

        cfg = PaymentMethodConfig.objects.filter(tenant_id=tenant_id, config_id=config_id).first()
        if not cfg:
            raise NotFound({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": "Payment configuration not found."})

        if not cfg.is_enabled:
            raise ValidationError(
                {
                    "error_code": "ERR_PAYMENT_METHOD_DISABLED",
                    "detail": f"Payment method '{cfg.display_name}' is currently disabled.",
                    "message_en": f"Payment method '{cfg.display_name}' is disabled.",
                    "message_bn": f"পেমেন্ট মেথড '{cfg.display_name}' বর্তমানে নিষ্ক্রিয় রয়েছে।",
                }
            )

        if is_offline and not cfg.allow_offline:
            raise ValidationError(
                {
                    "error_code": "ERR_OFFLINE_PAYMENT_NOT_ALLOWED",
                    "detail": f"Payment method '{cfg.display_name}' cannot be used in offline POS mode.",
                    "message_en": f"Payment method '{cfg.display_name}' cannot be used offline.",
                    "message_bn": f"পেমেন্ট মেথড '{cfg.display_name}' অফলাইন মোডে গ্রহণযোগ্য নয়।",
                }
            )

        amt = to_d2(amount_bdt)
        if amt < cfg.min_transaction_bdt or amt > cfg.max_transaction_bdt:
            raise ValidationError(
                {
                    "error_code": "ERR_TRANSACTION_AMOUNT_OUT_OF_BOUNDS",
                    "detail": (
                        f"Amount {amt} BDT is outside permitted bounds "
                        f"[{cfg.min_transaction_bdt} - {cfg.max_transaction_bdt} BDT]."
                    ),
                    "min_transaction_bdt": str(cfg.min_transaction_bdt),
                    "max_transaction_bdt": str(cfg.max_transaction_bdt),
                }
            )

        ref_clean = reference.strip()
        if cfg.requires_reference and not ref_clean:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_TRANSACTION_REFERENCE",
                    "detail": f"Transaction reference ({cfg.reference_label or 'Reference'}) is required.",
                    "message_en": f"Transaction reference ({cfg.reference_label or 'Reference'}) is required.",
                    "message_bn": "লেনদেন রেফারেন্স / ট্রানজ্যাকশন আইডি প্রদান আবশ্যক।",
                }
            )

        if cfg.requires_reference and cfg.reference_pattern and ref_clean:
            if not re.match(cfg.reference_pattern, ref_clean):
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_REFERENCE_FORMAT",
                        "detail": f"Transaction reference '{ref_clean}' does not match required pattern '{cfg.reference_pattern}'.",
                        "pattern": cfg.reference_pattern,
                    }
                )

        # Calculate surcharge
        surcharge_amt = Decimal("0.00")
        if cfg.customer_surcharge_percentage > Decimal("0.00"):
            surcharge_amt += (amt * cfg.customer_surcharge_percentage / Decimal("100.00")).quantize(Decimal("0.01"))
        surcharge_amt += cfg.customer_surcharge_flat_bdt
        total_payable = amt + surcharge_amt

        return {
            "is_valid": True,
            "method_type": cfg.method_type,
            "display_name": cfg.display_name,
            "amount_bdt": str(amt),
            "surcharge_bdt": str(surcharge_amt),
            "total_payable_bdt": str(total_payable),
            "reference": ref_clean,
            "clearing_account_id": str(cfg.clearing_account_id) if cfg.clearing_account_id else None,
        }
