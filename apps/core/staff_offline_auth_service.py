"""
Staff Offline Authorization & Risk-Limited Role Snapshot Service.
Architecture Decision: ADR-0113 (P113).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.

Enforces:
- Never plaintext passwords (PBKDF2-HMAC-SHA256 salted hashes only).
- Cryptographically signed role snapshots (HMAC-SHA256).
- Role-specific action whitelisting and risk limits (discounts, caps, supervisor flags).
- Multi-tenant and branch boundaries.
- Active tenant subscription checks (blocks suspended/expired tenants).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import timedelta
from typing import Any, Dict, Optional

from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    StaffLifecycleStatus,
    StaffMember,
    StaffOfflineAuthorization,
    StaffRole,
    Tenant,
    TenantLifecycleStatus,
)

logger = logging.getLogger("pharmacy.core.staff_offline_auth")

DEFAULT_SIGNING_SECRET = getattr(settings, "OFFLINE_AUTH_SIGNING_SECRET", "pharmacy-offline-signing-secret-key-2026")


class StaffOfflineAuthorizationService:
    """Core domain logic for issuing, verifying, and revoking offline staff authorizations."""

    ROLE_POLICIES: Dict[str, Dict[str, Any]] = {
        StaffRole.CASHIER: {
            "permitted_actions": ["pos.sale.create", "pos.sale.view", "pos.receipt.print"],
            "denied_actions": ["pos.refund.process", "pos.discount.override", "pos.stock.writeoff", "admin.config.edit"],
            "risk_limits": {
                "max_offline_discount_percent": 5.0,
                "max_offline_sale_amount": "25000.00",
                "allow_credit_sales": False,
                "require_supervisor_refund": True,
            },
        },
        StaffRole.PHARMACIST: {
            "permitted_actions": ["pos.sale.create", "pos.sale.view", "pos.receipt.print", "pos.prescription.review", "pos.stock.view"],
            "denied_actions": ["pos.discount.override", "pos.stock.writeoff", "admin.config.edit"],
            "risk_limits": {
                "max_offline_discount_percent": 10.0,
                "max_offline_sale_amount": "50000.00",
                "allow_credit_sales": False,
                "require_supervisor_refund": True,
            },
        },
        StaffRole.BRANCH_MANAGER: {
            "permitted_actions": [
                "pos.sale.create",
                "pos.sale.view",
                "pos.receipt.print",
                "pos.prescription.review",
                "pos.stock.view",
                "pos.refund.process",
                "pos.supervisor.override",
            ],
            "denied_actions": ["admin.config.edit"],
            "risk_limits": {
                "max_offline_discount_percent": 20.0,
                "max_offline_sale_amount": "200000.00",
                "allow_credit_sales": True,
                "require_supervisor_refund": False,
            },
        },
        "PHARMACY_OWNER": {
            "permitted_actions": ["*"],
            "denied_actions": [],
            "risk_limits": {
                "max_offline_discount_percent": 100.0,
                "max_offline_sale_amount": "1000000.00",
                "allow_credit_sales": True,
                "require_supervisor_refund": False,
            },
        },
    }

    @classmethod
    def _compute_credential_hash(cls, pin_or_passcode: str, salt_hex: str) -> str:
        """Derives a salted PBKDF2-HMAC-SHA256 hash. Plaintext is never stored."""
        salt = bytes.fromhex(salt_hex)
        return hashlib.pbkdf2_hmac("sha256", pin_or_passcode.encode("utf-8"), salt, 100_000).hex()

    @classmethod
    def _compute_signature(cls, auth_id: str, tenant_id: str, branch_id: Optional[str], staff_id: str, device_id: str, role: str, cred_hash: str, expires_at_iso: str) -> str:
        """Signs authorization payload using HMAC-SHA256."""
        payload = f"{auth_id}:{tenant_id}:{branch_id or ''}:{staff_id}:{device_id}:{role}:{cred_hash}:{expires_at_iso}"
        return hmac.new(DEFAULT_SIGNING_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

    @classmethod
    def issue_offline_authorization(
        cls,
        actor_user: Any,
        tenant_id: str,
        staff_id: str,
        device_id: str,
        pin_or_passcode: str,
        branch_id: Optional[str] = None,
        duration_hours: int = 72,
    ) -> Dict[str, Any]:
        """Issues a cryptographically signed offline authorization lease."""
        if not tenant_id or not staff_id or not device_id or not pin_or_passcode:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_OFFLINE_AUTH_INPUT",
                    "message_en": "Missing required fields: tenant_id, staff_id, device_id, and pin_or_passcode are mandatory.",
                    "message_bn": "প্রয়োজনীয় তথ্য অনুপস্থিত: tenant_id, staff_id, device_id এবং pin_or_passcode বাধ্যতামূলক।",
                }
            )

        if len(pin_or_passcode) < 4:
            raise ValidationError(
                {
                    "error_code": "ERR_INSUFFICIENT_PIN_LENGTH",
                    "message_en": "Offline PIN or passcode must be at least 4 characters.",
                    "message_bn": "অফলাইন পিন বা পাসকোড কমপক্ষে ৪ অক্ষরের হতে হবে।",
                }
            )

        # 1. Tenant & Subscription Check
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise ValidationError({"error_code": "TENANT_NOT_FOUND", "message_en": f"Tenant '{tenant_id}' not found."})

        if tenant.status in [TenantLifecycleStatus.SUSPENDED, TenantLifecycleStatus.ARCHIVED]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
                    "message_en": f"Tenant subscription is {tenant.status}. Offline authorization issuance blocked.",
                    "message_bn": "টেন্যান্ট সাবস্ক্রিপশন স্থগিত বা বাতিল থাকায় অফলাইন অনুমোদন দেওয়া যাবে না।",
                }
            )

        # 2. Actor Authority & Cross-Tenant Check
        actor_tenant = getattr(actor_user, "tenant_id", None)
        actor_role = getattr(actor_user, "role", None)
        is_superadmin = actor_role in ["SUPERADMIN", "SYSTEM_SUPERADMIN"] or getattr(actor_user, "is_superuser", False)

        if not is_superadmin and actor_tenant != tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": f"Cross-tenant access denied. Cannot issue authorizations for tenant '{tenant_id}'.",
                    "message_bn": "অন্য টেন্যান্টের জন্য অফলাইন অনুমোদন জারি করার অনুমতি নেই।",
                }
            )

        # 3. Staff Lookup & Role Check
        staff = StaffMember.objects.filter(tenant=tenant, staff_id=staff_id).first()
        if not staff:
            raise ValidationError({"error_code": "STAFF_NOT_FOUND", "message_en": f"Staff member '{staff_id}' not found."})

        if not staff.is_active or staff.lifecycle_status != StaffLifecycleStatus.ACTIVE:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": f"Staff member is {staff.lifecycle_status} (inactive). Offline authorization denied.",
                    "message_bn": "স্টাফ অ্যাকাউন্টটি নিষ্ক্রিয় বা বাতিল থাকায় অফলাইন অ্যাক্সেস অনুমোদিত নয়।",
                }
            )

        # 4. Resolve Policy & Risk Limits
        role_str = str(staff.role)
        policy = cls.ROLE_POLICIES.get(role_str, cls.ROLE_POLICIES[StaffRole.CASHIER])

        # 5. Cryptographic Salt & Hash (NEVER PLAINTEXT)
        salt_hex = secrets.token_hex(16)
        cred_hash = cls._compute_credential_hash(pin_or_passcode, salt_hex)

        auth_id = f"oath-{uuid.uuid4().hex[:12]}"
        now = timezone.now()
        expires_at = now + timedelta(hours=min(max(1, duration_hours), 72))
        expires_iso = expires_at.isoformat()

        effective_branch_id = branch_id or (staff.branch.branch_id if staff.branch else None)

        sig = cls._compute_signature(
            auth_id=auth_id,
            tenant_id=tenant_id,
            branch_id=effective_branch_id,
            staff_id=staff_id,
            device_id=device_id,
            role=role_str,
            cred_hash=cred_hash,
            expires_at_iso=expires_iso,
        )

        # 6. Save Authorized Record
        branch_obj = Branch.objects.filter(branch_id=effective_branch_id).first() if effective_branch_id else None
        record = StaffOfflineAuthorization.objects.create(
            authorization_id=auth_id,
            tenant=tenant,
            branch=branch_obj,
            staff_member=staff,
            device_id=device_id,
            role_snapshot=role_str,
            credential_salt=salt_hex,
            credential_hash=cred_hash,
            permitted_actions=policy["permitted_actions"],
            denied_actions=policy["denied_actions"],
            risk_limits=policy["risk_limits"],
            issued_at=now,
            expires_at=expires_at,
            signature=sig,
            is_revoked=False,
        )

        return {
            "authorization_id": record.authorization_id,
            "tenant_id": record.tenant.tenant_id,
            "branch_id": effective_branch_id,
            "staff_id": record.staff_member.staff_id,
            "username": record.staff_member.username,
            "full_name": record.staff_member.full_name,
            "role": record.role_snapshot,
            "device_id": record.device_id,
            "credential_salt": record.credential_salt,
            "credential_hash": record.credential_hash,
            "permitted_actions": record.permitted_actions,
            "denied_actions": record.denied_actions,
            "risk_limits": record.risk_limits,
            "issued_at": record.issued_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
            "signature": record.signature,
            "is_revoked": record.is_revoked,
        }

    @classmethod
    def verify_offline_credentials(
        cls,
        payload: Dict[str, Any],
        pin_or_passcode: str,
        current_device_id: str,
        current_time: Optional[timezone.datetime] = None,
    ) -> Dict[str, Any]:
        """
        Verifies offline credentials against the cached signed role snapshot:
        - Signature integrity
        - Device ID binding
        - Time expiry
        - Salted hash check (NO plaintext)
        """
        now = current_time or timezone.now()

        # 1. Device Binding
        if payload.get("device_id") != current_device_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_OFFLINE_DEVICE_MISMATCH",
                    "message_en": f"Device '{current_device_id}' is not authorized for this offline credential.",
                    "message_bn": "এই ডিভাইসটিতে অফলাইন ক্রেডেনশিয়াল ব্যবহারের অনুমতি নেই।",
                }
            )

        # 2. Expiry Check
        expires_at_str = payload.get("expires_at", "")
        try:
            expires_at = timezone.datetime.fromisoformat(expires_at_str)
        except Exception:
            raise ValidationError({"error_code": "ERR_MALFORMED_TIMESTAMP", "message_en": "Malformed expires_at timestamp."})

        if now >= expires_at:
            raise PermissionDenied(
                {
                    "error_code": "ERR_OFFLINE_AUTH_EXPIRED",
                    "message_en": f"Offline authorization expired at {expires_at_str}. Reconnection required.",
                    "message_bn": "অফলাইন অনুমোদনের মেয়াদ উত্তীর্ণ হয়েছে। পুনরায় ইন্টারনেটে সংযুক্ত হতে হবে।",
                }
            )

        # 3. Signature Integrity Check
        expected_sig = cls._compute_signature(
            auth_id=payload["authorization_id"],
            tenant_id=payload["tenant_id"],
            branch_id=payload.get("branch_id"),
            staff_id=payload["staff_id"],
            device_id=payload["device_id"],
            role=payload["role"],
            cred_hash=payload["credential_hash"],
            expires_at_iso=expires_at_str,
        )
        if not hmac.compare_digest(payload.get("signature", ""), expected_sig):
            raise PermissionDenied(
                {
                    "error_code": "ERR_SIGNATURE_INVALID",
                    "message_en": "Cryptographic signature check failed. Tampering detected.",
                    "message_bn": "ক্রিপ্টোগ্রাফিক সিগনেচার যাচাই ব্যর্থ হয়েছে।",
                }
            )

        # 4. Salted Credential Check
        salt_hex = payload.get("credential_salt", "")
        target_hash = cls._compute_credential_hash(pin_or_passcode, salt_hex)
        if not hmac.compare_digest(payload.get("credential_hash", ""), target_hash):
            raise PermissionDenied(
                {
                    "error_code": "ERR_INVALID_CREDENTIALS",
                    "message_en": "Invalid offline PIN or passcode.",
                    "message_bn": "ভুল অফলাইন পিন বা পাসকোড।",
                }
            )

        return {
            "is_authorized": True,
            "staff_id": payload["staff_id"],
            "username": payload["username"],
            "role": payload["role"],
            "tenant_id": payload["tenant_id"],
            "branch_id": payload.get("branch_id"),
            "permitted_actions": payload.get("permitted_actions", []),
            "risk_limits": payload.get("risk_limits", {}),
            "auth_mode": "OFFLINE_SIGNED_LEASE",
        }

    @classmethod
    def revoke_offline_authorization(cls, actor_user: Any, authorization_id: str, reason: str = "MANUAL_REVOCATION") -> Dict[str, Any]:
        """Revokes an active offline authorization lease."""
        record = StaffOfflineAuthorization.objects.filter(authorization_id=authorization_id).first()
        if not record:
            raise ValidationError({"error_code": "OFFLINE_AUTH_NOT_FOUND", "message_en": "Authorization not found."})

        actor_tenant = getattr(actor_user, "tenant_id", None)
        actor_role = getattr(actor_user, "role", None)
        is_super = actor_role in ["SUPERADMIN", "SYSTEM_SUPERADMIN"] or getattr(actor_user, "is_superuser", False)

        if not is_super and actor_tenant != record.tenant.tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": "Cannot revoke offline authorization belonging to another tenant.",
                    "message_bn": "অন্য টেন্যান্টের অফলাইন অনুমোদন বাতিল করার অনুমতি নেই।",
                }
            )

        record.is_revoked = True
        record.revoked_at = timezone.now()
        record.revocation_reason = reason
        record.save(update_fields=["is_revoked", "revoked_at", "revocation_reason", "updated_at"])

        return {
            "authorization_id": record.authorization_id,
            "is_revoked": True,
            "revoked_at": record.revoked_at.isoformat(),
            "status": "REVOKED",
        }
