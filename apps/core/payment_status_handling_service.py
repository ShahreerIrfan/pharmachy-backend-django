"""
backend/apps/core/payment_status_handling_service.py
Architecture Decision: ADR-0276 (P276).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service governing Payment Status Lifecycle, Provider Evidence, and Reconciliation:
- Formal state transitions: PENDING -> CONFIRMED -> FAILED, REFUNDED, or REVERSED.
- Cryptographic SHA-256 evidence hashing for gateway/provider webhooks and card authorization slips.
- Fine-grained role-based access control (RBAC):
  * Cashiers can initiate and confirm payments via standard checkout flows.
  * Only Managers, Owners, and Finance Officers are authorized to execute Refunds and Reversals (HTTP 403 Forbidden).
- Strict multi-tenant isolation and suspended tenant write protections.
- Financial account reconciliation updates linked balances safely upon confirmation, refund, or reversal.
- Immutable audit log tracking with AuditLogEntry.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
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
    PaymentLifecycleStatus,
    PaymentMethod,
    PaymentMethodConfig,
    PaymentStatusRecord,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.0000")

PAYMENT_STATUS_VIEW_ROLES = {
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

PAYMENT_STATUS_MANAGE_ROLES = {
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
    """Quantizes decimal values strictly to 4 decimal places."""
    if val is None:
        return DECIMAL_ZERO
    try:
        return Decimal(str(val)).quantize(Decimal("0.0001"))
    except Exception:
        return DECIMAL_ZERO


class PaymentStatusHandlingService:
    """
    Authoritative domain service governing payment status lifecycle, evidence verification,
    and financial reconciliation.
    """

    @classmethod
    def _assert_view_role(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_UNAUTHENTICATED",
                    "detail": "Authentication required to view payment records.",
                }
            )
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if role not in PAYMENT_STATUS_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Staff role is not permitted to view payment records.",
                    "message_en": "Staff role is not permitted to view payment records.",
                    "message_bn": "পেমেন্ট রেকর্ড দেখার অনুমতি নেই।",
                }
            )

    @classmethod
    def _assert_manage_role(cls, actor_user: Any) -> None:
        """Enforces administrative authority for refunds, reversals, and reconciliation."""
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_UNAUTHENTICATED",
                    "detail": "Authentication required for payment administrative operations.",
                }
            )
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if role in [UserRole.CASHIER, "CASHIER", UserRole.STAFF_PHARMACIST, "STAFF_PHARMACIST"] or role not in PAYMENT_STATUS_MANAGE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and staff pharmacist are not authorized to perform refunds or reversals.",
                    "message_en": "Cashier and staff pharmacist are not authorized to perform refunds or reversals.",
                    "message_bn": "ক্যাশিয়ার বা স্টাফ ফার্মাসিস্ট রিফান্ড বা রিভার্সাল সম্পন্ন করার অনুমতিপ্রাপ্ত নন।",
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
                    "detail": "Tenant account subscription is suspended. Payment status operations blocked.",
                    "message_en": "Tenant account subscription is suspended. Payment status operations blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। পেমেন্ট অপারেশন বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    @classmethod
    def serialize_record(cls, record: PaymentStatusRecord) -> Dict[str, Any]:
        """Serializes PaymentStatusRecord into standard response dictionary."""
        return {
            "record_id": record.record_id,
            "tenant_id": str(record.tenant_id),
            "branch_id": str(record.branch_id) if record.branch_id else None,
            "branch_name": record.branch.name if record.branch else "Tenant-Wide",
            "payment_method_config_id": str(record.payment_method_config_id) if record.payment_method_config_id else None,
            "financial_account_id": str(record.financial_account_id) if record.financial_account_id else None,
            "tender_type": record.tender_type,
            "amount_bdt": str(record.amount_bdt),
            "surcharge_bdt": str(record.surcharge_bdt),
            "total_collected_bdt": str(record.total_collected_bdt),
            "status": record.status,
            "provider_name": record.provider_name,
            "provider_reference": record.provider_reference,
            "provider_evidence_json": record.provider_evidence_json,
            "evidence_hash": record.evidence_hash,
            "verified_at": record.verified_at.isoformat() if record.verified_at else None,
            "verified_by_id": str(record.verified_by_id) if record.verified_by_id else None,
            "failure_code": record.failure_code,
            "failure_reason": record.failure_reason,
            "refund_amount_bdt": str(record.refund_amount_bdt),
            "refund_reference": record.refund_reference,
            "refund_reason": record.refund_reason,
            "refunded_at": record.refunded_at.isoformat() if record.refunded_at else None,
            "reversal_reason": record.reversal_reason,
            "reversed_at": record.reversed_at.isoformat() if record.reversed_at else None,
            "is_reconciled": record.is_reconciled,
            "reconciled_at": record.reconciled_at.isoformat() if record.reconciled_at else None,
            "reconciliation_batch_id": record.reconciliation_batch_id,
            "idempotency_key": record.idempotency_key,
            "notes": record.notes,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        }

    @classmethod
    @transaction.atomic
    def initiate_payment(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Initiates a payment in PENDING status awaiting provider confirmation/tender evidence.
        """
        cls._assert_view_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        idempotency_key = str(data.get("idempotency_key", "")).strip()
        if idempotency_key:
            existing = PaymentStatusRecord.objects.filter(tenant=tenant, idempotency_key=idempotency_key).first()
            if existing:
                res = cls.serialize_record(existing)
                res["is_idempotent_replay"] = True
                return res

        amount = to_d4(data.get("amount_bdt", "0.0000"))
        if amount <= DECIMAL_ZERO:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_PAYMENT_AMOUNT",
                    "detail": "Payment amount must be greater than zero.",
                    "message_en": "Payment amount must be greater than zero.",
                    "message_bn": "পেমেন্ট পরিমাণ অবশ্যই শূন্যের চেয়ে বেশি হতে হবে।",
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

        tender_type = str(data.get("tender_type", PaymentMethod.CASH)).strip().upper()
        if tender_type not in PaymentMethod.values:
            raise ValidationError({"error_code": "ERR_INVALID_TENDER_TYPE", "detail": f"Invalid tender type '{tender_type}'."})

        # Linked configuration and financial account
        payment_config = None
        config_id = data.get("payment_method_config_id")
        if config_id:
            payment_config = PaymentMethodConfig.objects.filter(config_id=config_id).first()
            if payment_config and str(payment_config.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Payment config belongs to foreign tenant."})

        financial_account = None
        fin_acc_id = data.get("financial_account_id")
        if fin_acc_id:
            financial_account = FinancialAccount.objects.filter(account_id=fin_acc_id).first()
            if financial_account and str(financial_account.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Financial account belongs to foreign tenant."})
        elif payment_config and payment_config.clearing_account:
            financial_account = payment_config.clearing_account

        surcharge = to_d4(data.get("surcharge_bdt", "0.0000"))
        total_collected = amount + surcharge

        record = PaymentStatusRecord(
            tenant=tenant,
            branch=branch,
            payment_method_config=payment_config,
            financial_account=financial_account,
            tender_type=tender_type,
            amount_bdt=amount,
            surcharge_bdt=surcharge,
            total_collected_bdt=total_collected,
            status=PaymentLifecycleStatus.PENDING,
            provider_name=str(data.get("provider_name", tender_type)).strip(),
            idempotency_key=idempotency_key,
            notes=str(data.get("notes", "")).strip(),
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )
        record.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PAYMENT_INITIATED",
            resource_type="PaymentStatusRecord",
            resource_id=record.record_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "record_id": record.record_id,
                "amount": str(amount),
                "tender_type": tender_type,
            },
        )

        return cls.serialize_record(record)

    @classmethod
    @transaction.atomic
    def confirm_payment(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        record_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Transitions a PENDING payment to CONFIRMED using validated provider evidence.
        Atomically updates the linked financial clearing account balance.
        """
        cls._assert_view_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        record = PaymentStatusRecord.objects.select_for_update().filter(record_id=record_id).first()
        if not record:
            raise NotFound({"error_code": "ERR_PAYMENT_RECORD_NOT_FOUND", "detail": f"Payment record '{record_id}' not found."})
        if str(record.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})

        # Idempotency: if already CONFIRMED with matching reference, return existing
        provider_ref = str(data.get("provider_reference", "")).strip()
        if record.status == PaymentLifecycleStatus.CONFIRMED:
            if provider_ref and provider_ref == record.provider_reference:
                res = cls.serialize_record(record)
                res["is_idempotent_replay"] = True
                return res

        if record.status != PaymentLifecycleStatus.PENDING:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_PAYMENT_STATE_TRANSITION",
                    "detail": f"Cannot confirm payment from status '{record.status}'. Must be PENDING.",
                    "current_status": record.status,
                }
            )

        if not provider_ref and record.tender_type != PaymentMethod.CASH:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_PROVIDER_REFERENCE",
                    "detail": "Provider transaction reference (TrxID/Auth Code) is mandatory for confirmation.",
                    "message_en": "Provider transaction reference is mandatory.",
                    "message_bn": "লেনদেন নিশ্চিতকরণের জন্য প্রোভাইডার রেফারেন্স নম্বর আবশ্যক।",
                }
            )

        evidence_payload = data.get("provider_evidence_json", {})
        if not isinstance(evidence_payload, dict):
            evidence_payload = {}

        provider_name = str(data.get("provider_name", record.provider_name or record.tender_type)).strip()

        # Compute evidence hash
        raw_sig = f"{record.record_id}|{tenant_id}|{provider_ref}|{record.amount_bdt}|{json.dumps(evidence_payload, sort_keys=True)}"
        evidence_hash = hashlib.sha256(raw_sig.encode("utf-8")).hexdigest()

        record.status = PaymentLifecycleStatus.CONFIRMED
        record.provider_name = provider_name
        record.provider_reference = provider_ref
        record.provider_evidence_json = evidence_payload
        record.evidence_hash = evidence_hash
        record.verified_at = timezone.now()
        record.verified_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        record.save()

        # Update linked FinancialAccount balance
        if record.financial_account:
            fin_acc = FinancialAccount.objects.select_for_update().filter(pk=record.financial_account_id).first()
            if fin_acc:
                fin_acc.current_balance += record.total_collected_bdt
                fin_acc.save(update_fields=["current_balance", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "cashier@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "CASHIER")),
            action="PAYMENT_CONFIRMED",
            resource_type="PaymentStatusRecord",
            resource_id=record.record_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "record_id": record.record_id,
                "amount": str(record.amount_bdt),
                "provider_ref": provider_ref,
                "evidence_hash": evidence_hash,
            },
        )

        return cls.serialize_record(record)

    @classmethod
    @transaction.atomic
    def fail_payment(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        record_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Marks a PENDING payment attempt as FAILED with error reason."""
        cls._assert_view_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        record = PaymentStatusRecord.objects.select_for_update().filter(record_id=record_id).first()
        if not record:
            raise NotFound({"error_code": "ERR_PAYMENT_RECORD_NOT_FOUND", "detail": f"Payment record '{record_id}' not found."})
        if str(record.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})

        if record.status != PaymentLifecycleStatus.PENDING:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_PAYMENT_STATE_TRANSITION",
                    "detail": f"Cannot fail payment from status '{record.status}'.",
                }
            )

        fail_code = str(data.get("failure_code", "GATEWAY_REJECTED")).strip()
        fail_reason = str(data.get("failure_reason", "Provider declined or timed out")).strip()

        record.status = PaymentLifecycleStatus.FAILED
        record.failure_code = fail_code
        record.failure_reason = fail_reason
        record.save(update_fields=["status", "failure_code", "failure_reason", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "cashier@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "CASHIER")),
            action="PAYMENT_FAILED",
            resource_type="PaymentStatusRecord",
            resource_id=record.record_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"failure_code": fail_code, "failure_reason": fail_reason},
        )

        return cls.serialize_record(record)

    @classmethod
    @transaction.atomic
    def refund_payment(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        record_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Executes a partial or full refund on a CONFIRMED payment.
        Requires administrative roles (Owner, Manager, Finance Officer).
        """
        cls._assert_manage_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        record = PaymentStatusRecord.objects.select_for_update().filter(record_id=record_id).first()
        if not record:
            raise NotFound({"error_code": "ERR_PAYMENT_RECORD_NOT_FOUND", "detail": f"Payment record '{record_id}' not found."})
        if str(record.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})

        if record.status != PaymentLifecycleStatus.CONFIRMED:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_PAYMENT_STATE_TRANSITION",
                    "detail": f"Cannot refund payment in status '{record.status}'. Must be CONFIRMED.",
                }
            )

        refund_amt = to_d4(data.get("refund_amount_bdt", record.total_collected_bdt))
        if refund_amt <= DECIMAL_ZERO or refund_amt > record.total_collected_bdt:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_REFUND_AMOUNT",
                    "detail": f"Refund amount ({refund_amt}) must be > 0 and <= total collected ({record.total_collected_bdt}).",
                }
            )

        refund_ref = str(data.get("refund_reference", "")).strip()
        refund_reason = str(data.get("refund_reason", "Customer medicine return / refund")).strip()

        record.status = PaymentLifecycleStatus.REFUNDED
        record.refund_amount_bdt = refund_amt
        record.refund_reference = refund_ref
        record.refund_reason = refund_reason
        record.refunded_at = timezone.now()
        record.refunded_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        record.save()

        # Adjust financial account balance
        if record.financial_account:
            fin_acc = FinancialAccount.objects.select_for_update().filter(pk=record.financial_account_id).first()
            if fin_acc:
                fin_acc.current_balance -= refund_amt
                fin_acc.save(update_fields=["current_balance", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PAYMENT_REFUNDED",
            resource_type="PaymentStatusRecord",
            resource_id=record.record_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"refund_amount": str(refund_amt), "refund_reference": refund_ref},
        )

        return cls.serialize_record(record)

    @classmethod
    @transaction.atomic
    def reverse_payment(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        record_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Executes a compensating reversal on a CONFIRMED payment (e.g. duplicate payment or chargeback).
        Requires administrative roles.
        """
        cls._assert_manage_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        record = PaymentStatusRecord.objects.select_for_update().filter(record_id=record_id).first()
        if not record:
            raise NotFound({"error_code": "ERR_PAYMENT_RECORD_NOT_FOUND", "detail": f"Payment record '{record_id}' not found."})
        if str(record.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})

        if record.status != PaymentLifecycleStatus.CONFIRMED:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_PAYMENT_STATE_TRANSITION",
                    "detail": f"Cannot reverse payment in status '{record.status}'. Must be CONFIRMED.",
                }
            )

        reason = str(data.get("reversal_reason", "")).strip()
        if not reason:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_REVERSAL_REASON",
                    "detail": "A mandatory explanation reason is required for payment reversals.",
                    "message_en": "A mandatory explanation reason is required for payment reversals.",
                    "message_bn": "পেমেন্ট রিভার্সাল সম্পন্ন করার জন্য সুনির্দিষ্ট কারণ আবশ্যক।",
                }
            )

        record.status = PaymentLifecycleStatus.REVERSED
        record.reversal_reason = reason
        record.reversed_at = timezone.now()
        record.reversed_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        record.save()

        # Adjust financial account balance
        if record.financial_account:
            fin_acc = FinancialAccount.objects.select_for_update().filter(pk=record.financial_account_id).first()
            if fin_acc:
                fin_acc.current_balance -= record.total_collected_bdt
                fin_acc.save(update_fields=["current_balance", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PAYMENT_REVERSED",
            resource_type="PaymentStatusRecord",
            resource_id=record.record_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"reversal_reason": reason, "reversed_amount": str(record.total_collected_bdt)},
        )

        return cls.serialize_record(record)

    @classmethod
    @transaction.atomic
    def reconcile_payment(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        record_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Marks a settled or reversed payment as reconciled against provider bank/clearing statements.
        """
        cls._assert_manage_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        record = PaymentStatusRecord.objects.select_for_update().filter(record_id=record_id).first()
        if not record:
            raise NotFound({"error_code": "ERR_PAYMENT_RECORD_NOT_FOUND", "detail": f"Payment record '{record_id}' not found."})
        if str(record.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})

        if record.status not in (PaymentLifecycleStatus.CONFIRMED, PaymentLifecycleStatus.REFUNDED, PaymentLifecycleStatus.REVERSED):
            raise ValidationError(
                {
                    "error_code": "ERR_UNSETTLED_PAYMENT_RECONCILIATION_BLOCKED",
                    "detail": f"Cannot reconcile unconfirmed payment in status '{record.status}'.",
                }
            )

        batch_id = str(data.get("reconciliation_batch_id", f"REC-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}")).strip()

        record.is_reconciled = True
        record.reconciled_at = timezone.now()
        record.reconciliation_batch_id = batch_id
        record.save(update_fields=["is_reconciled", "reconciled_at", "reconciliation_batch_id", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PAYMENT_RECONCILED",
            resource_type="PaymentStatusRecord",
            resource_id=record.record_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"batch_id": batch_id},
        )

        return cls.serialize_record(record)

    @classmethod
    def list_payments(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        branch_id: Optional[str] = None,
        status: Optional[str] = None,
        tender_type: Optional[str] = None,
        is_reconciled: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Lists payment status records with filters."""
        cls._assert_view_role(actor_user)
        cls._assert_tenant_active(tenant_id)

        qs = PaymentStatusRecord.objects.filter(tenant_id=tenant_id).select_related("branch", "financial_account")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if status:
            qs = qs.filter(status=status.upper())
        if tender_type:
            qs = qs.filter(tender_type=tender_type.upper())
        if is_reconciled is not None:
            qs = qs.filter(is_reconciled=is_reconciled)

        return [cls.serialize_record(r) for r in qs.order_by("-created_at")]

    @classmethod
    def get_payment(
        cls,
        *,
        tenant_id: str,
        actor_user: Any,
        record_id: str,
    ) -> Dict[str, Any]:
        """Retrieves single payment status record details."""
        cls._assert_view_role(actor_user)
        cls._assert_tenant_active(tenant_id)

        record = (
            PaymentStatusRecord.objects.filter(tenant_id=tenant_id, record_id=record_id)
            .select_related("branch", "financial_account")
            .first()
        )
        if not record:
            raise NotFound({"error_code": "ERR_PAYMENT_RECORD_NOT_FOUND", "detail": f"Payment record '{record_id}' not found."})

        return cls.serialize_record(record)
