"""
backend/apps/core/offline_entitlement_service.py
Architecture Decision: ADR-0082, ADR-0092 & ADR-0093
Phase 05: Basic/Standard/Pro Subscription & Billing (P093)
Requirement Trace: FR-071 (SaaS Plans, Billing & Tenant Lifecycle)

Cryptographically Signed Offline Entitlement Lease Service:
- Issues time-bounded, tamper-proof offline leases bound to tenant, branch, device, and user role.
- Enforces HMAC-SHA256 cryptographic signatures with canonical payload serialization.
- Bounded offline duration: 24 hours for Trial/Basic, 72 hours for Standard/Pro.
- Evaluates clinical safety invariants (always True offline) and commercial checkout gates.
- Gating: Rejects locked over-quota branches, suspended tenants, and lapsed subscriptions.
- Zero data loss: Expired or revoked leases never destroy local database or ledger history.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    DeviceLeaseCertificate,
    OfflineLeaseStatus,
    PlanTier,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
    TenantAppliedEntitlement,
    TenantLifecycleStatus,
)

logger = logging.getLogger("pharmacy.billing.offline_lease")

# Clinically protected safety features that MUST remain active even during offline emergency
CLINICALLY_PROTECTED_OFFLINE_FEATURES = [
    "ANTIBIOTIC_REGISTER",
    "BATCH_EXPIRY_BLOCK",
    "RECALL_QUARANTINE",
    "BANNED_DRUG_RESTRICTION",
    "RX_VERIFICATION",
]


class OfflineEntitlementLeaseService:
    MAX_HOURS_TRIAL = 24
    MAX_HOURS_BASIC = 24
    MAX_HOURS_STANDARD = 72
    MAX_HOURS_PRO = 72

    @classmethod
    def _get_signing_key(cls) -> bytes:
        raw_key = getattr(settings, "OFFLINE_LEASE_SIGNING_KEY", settings.SECRET_KEY)
        return raw_key.encode("utf-8")

    @classmethod
    def generate_canonical_payload(
        cls,
        lease_id: str,
        tenant_id: str,
        branch_id: str,
        device_id: str,
        role_snapshot: str,
        plan_tier: str,
        policy_version: str,
        duration_hours: int,
        issued_at_iso: str,
        expires_at_iso: str,
        capabilities_snapshot: dict[str, bool],
    ) -> str:
        """
        Generates deterministic, sorted canonical JSON payload string for cryptographic signing.
        """
        payload = {
            "capabilities_snapshot": {k: capabilities_snapshot[k] for k in sorted(capabilities_snapshot.keys())},
            "device_id": str(device_id).strip(),
            "duration_hours": int(duration_hours),
            "expires_at": str(expires_at_iso).strip(),
            "issued_at": str(issued_at_iso).strip(),
            "lease_id": str(lease_id).strip(),
            "plan_tier": str(plan_tier).strip().upper(),
            "policy_version": str(policy_version).strip(),
            "role_snapshot": str(role_snapshot).strip().upper(),
            "tenant_id": str(tenant_id).strip(),
            "branch_id": str(branch_id).strip(),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def compute_signature(cls, canonical_payload: str) -> str:
        """Computes HMAC-SHA256 hex digest over canonical payload string."""
        return hmac.new(
            cls._get_signing_key(),
            canonical_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @classmethod
    def verify_signature(cls, canonical_payload: str, signature: str) -> bool:
        """Verifies HMAC-SHA256 signature in constant time."""
        expected = cls.compute_signature(canonical_payload)
        return hmac.compare_digest(expected.lower(), str(signature).lower().strip())

    @classmethod
    def issue_offline_lease(
        cls,
        tenant_id: str,
        branch_id: str,
        device_id: str,
        device_name: str = "",
        policy_version: str = "v2.0",
        requested_duration_hours: int | None = None,
        actor_user: Any = None,
    ) -> dict[str, Any]:
        """
        Issues a cryptographically signed, time-bound offline lease certificate.
        Strictly validates tenant, branch quota status, subscription standing, and device identity.
        """
        clean_tenant_id = str(tenant_id or "").strip()
        clean_branch_id = str(branch_id or "").strip()
        clean_device_id = str(device_id or "").strip()

        if not clean_device_id or len(clean_device_id) < 3:
            raise ValidationError("Valid device_id (minimum 3 characters) is required.")

        # 1. Tenant Verification
        try:
            tenant = Tenant.objects.get(tenant_id=clean_tenant_id)
        except Tenant.DoesNotExist as err:
            raise ValidationError(f"Tenant '{clean_tenant_id}' does not exist.") from err

        if tenant.status in [TenantLifecycleStatus.SUSPENDED, TenantLifecycleStatus.ARCHIVED]:
            raise ValidationError(f"Tenant '{clean_tenant_id}' is suspended; cannot issue offline lease.")

        # 2. Branch Verification
        try:
            branch = Branch.objects.get(branch_id=clean_branch_id, tenant=tenant)
        except Branch.DoesNotExist as err:
            raise ValidationError(
                f"Branch '{clean_branch_id}' does not exist or does not belong to tenant '{clean_tenant_id}'."
            ) from err

        if branch.is_over_quota_locked:
            raise ValidationError(
                f"Branch '{branch.name}' is over-quota locked due to subscription plan limits. "
                "Offline leases cannot be issued for locked branches."
            )

        if not branch.is_active:
            raise ValidationError(f"Branch '{branch.name}' is inactive.")

        # 3. Subscription Verification
        subscription = (
            SubscriptionRecord.objects.filter(tenant=tenant)
            .order_by("-created_at")
            .first()
        )
        if not subscription:
            raise ValidationError(f"Tenant '{clean_tenant_id}' does not have a subscription record.")

        if subscription.status in [
            SubscriptionStatus.SUSPENDED,
            SubscriptionStatus.EXPIRED,
            SubscriptionStatus.CANCELLED,
        ]:
            raise ValidationError(
                f"Subscription is currently {subscription.status}. "
                "Offline leases are blocked until subscription is reactivated."
            )

        # 4. Determine Max Allowed Offline Duration based on Tier
        plan_tier = str(subscription.plan_tier).upper()
        if plan_tier == PlanTier.PRO:
            max_duration = cls.MAX_HOURS_PRO
        elif plan_tier == PlanTier.STANDARD:
            max_duration = cls.MAX_HOURS_STANDARD
        elif plan_tier == PlanTier.BASIC:
            max_duration = cls.MAX_HOURS_BASIC
        else:
            max_duration = cls.MAX_HOURS_TRIAL

        # Special Grace Period rule: lease duration cannot exceed remaining grace period
        now = timezone.now()
        today = now.date()
        if subscription.status == SubscriptionStatus.GRACE_PERIOD and subscription.grace_period_end_date:
            days_left = max(0, (subscription.grace_period_end_date - today).days)
            hours_left = max(1, days_left * 24)
            max_duration = min(max_duration, hours_left)

        duration = max_duration
        if requested_duration_hours and int(requested_duration_hours) > 0:
            duration = min(int(requested_duration_hours), max_duration)

        # 5. Build Capabilities Snapshot
        capabilities: dict[str, bool] = {
            "CHECKOUT_DISPENSING": True,
            "OFFLINE_EMERGENCY_DISPENSE": True,
        }
        for safety_key in CLINICALLY_PROTECTED_OFFLINE_FEATURES:
            capabilities[safety_key] = True

        # Copy applied features snapshot if available
        entitlement = TenantAppliedEntitlement.objects.filter(tenant=tenant, is_active=True).first()
        if entitlement and isinstance(entitlement.features_snapshot, dict):
            for k, v in entitlement.features_snapshot.items():
                capabilities[k] = bool(v)

        # 6. Role Snapshot
        role = str(getattr(actor_user, "role", "CASHIER")).upper()
        user_id = str(getattr(actor_user, "user_id", None) or getattr(actor_user, "id", "") or "")
        user_email = str(getattr(actor_user, "email", "") or "")

        # 7. Device Quota Enforcement
        # Count currently active leases for other distinct devices
        max_total_devices = 10
        if entitlement:
            max_total_devices = int(getattr(entitlement, "max_total_devices", 10))

        active_distinct_devices = (
            DeviceLeaseCertificate.objects.filter(
                tenant=tenant,
                status=OfflineLeaseStatus.ACTIVE,
                expires_at__gt=now,
            )
            .exclude(device_id=clean_device_id)
            .values("device_id")
            .distinct()
            .count()
        )
        if active_distinct_devices >= max_total_devices:
            raise ValidationError(
                f"Tenant active device quota ({max_total_devices}) reached. "
                "Revoke an existing device before issuing a new lease."
            )

        # 8. Supersede prior active leases for this exact device
        DeviceLeaseCertificate.objects.filter(
            tenant=tenant,
            branch=branch,
            device_id=clean_device_id,
            status=OfflineLeaseStatus.ACTIVE,
        ).update(status=OfflineLeaseStatus.SUPERSEDED)

        # 9. Compute Timestamps & Cryptographic Signature
        issued_at = now
        expires_at = now + timedelta(hours=duration)
        lease_id = f"ls-{uuid.uuid4().hex[:16]}"
        policy_ver = str(policy_version or "v2.0").strip()

        canonical_payload = cls.generate_canonical_payload(
            lease_id=lease_id,
            tenant_id=tenant.tenant_id,
            branch_id=branch.branch_id,
            device_id=clean_device_id,
            role_snapshot=role,
            plan_tier=plan_tier,
            policy_version=policy_ver,
            duration_hours=duration,
            issued_at_iso=issued_at.isoformat(),
            expires_at_iso=expires_at.isoformat(),
            capabilities_snapshot=capabilities,
        )
        signature = cls.compute_signature(canonical_payload)

        # 10. Persist Certificate
        certificate = DeviceLeaseCertificate.objects.create(
            lease_id=lease_id,
            tenant=tenant,
            branch=branch,
            device_id=clean_device_id,
            device_name=str(device_name or "").strip(),
            user_id=user_id,
            user_email=user_email,
            role_snapshot=role,
            plan_tier=plan_tier,
            policy_version=policy_ver,
            duration_hours=duration,
            issued_at=issued_at,
            expires_at=expires_at,
            capabilities_snapshot=capabilities,
            signature_algorithm="HMAC-SHA256",
            signature=signature,
            status=OfflineLeaseStatus.ACTIVE,
        )

        AuditLogEntry.objects.create(
            action="OFFLINE_LEASE_ISSUED",
            resource_type="DEVICE_LEASE",
            resource_id=lease_id,
            tenant_id=tenant.tenant_id,
            user_id=user_id or None,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "device_id": clean_device_id,
                "branch_id": clean_branch_id,
                "duration_hours": duration,
                "expires_at": expires_at.isoformat(),
                "role_snapshot": role,
                "plan_tier": plan_tier,
            },
        )

        logger.info(
            "Issued offline lease %s for tenant '%s', branch '%s', device '%s' (valid for %dh)",
            lease_id,
            tenant.tenant_id,
            branch.branch_id,
            clean_device_id,
            duration,
        )

        return cls._serialize_certificate(certificate)

    @classmethod
    def verify_offline_lease(
        cls,
        lease_id: str | None = None,
        raw_payload: dict[str, Any] | None = None,
        signature: str | None = None,
    ) -> dict[str, Any]:
        """
        Cryptographically verifies an offline lease.
        Checks HMAC-SHA256 integrity, expiration, branch status, and database revocation.
        """
        now = timezone.now()

        # If payload and signature are provided directly (offline audit mode)
        if raw_payload and signature:
            clean_sig = str(signature).strip()
            # Reconstruct canonical string
            canonical = cls.generate_canonical_payload(
                lease_id=raw_payload.get("lease_id", ""),
                tenant_id=raw_payload.get("tenant_id", ""),
                branch_id=raw_payload.get("branch_id", ""),
                device_id=raw_payload.get("device_id", ""),
                role_snapshot=raw_payload.get("role_snapshot", ""),
                plan_tier=raw_payload.get("plan_tier", ""),
                policy_version=raw_payload.get("policy_version", "v2.0"),
                duration_hours=int(raw_payload.get("duration_hours", 72)),
                issued_at_iso=raw_payload.get("issued_at", ""),
                expires_at_iso=raw_payload.get("expires_at", ""),
                capabilities_snapshot=raw_payload.get("capabilities_snapshot", {}),
            )
            if not cls.verify_signature(canonical, clean_sig):
                return {
                    "is_valid": False,
                    "reason": "INVALID_SIGNATURE",
                    "details": "Cryptographic HMAC-SHA256 signature mismatch. Certificate may have been tampered.",
                    "time_remaining_seconds": 0,
                }

        clean_lease_id = str(lease_id or (raw_payload.get("lease_id") if raw_payload else "")).strip()
        try:
            cert = DeviceLeaseCertificate.objects.select_related("tenant", "branch").get(lease_id=clean_lease_id)
        except DeviceLeaseCertificate.DoesNotExist:
            return {
                "is_valid": False,
                "reason": "LEASE_NOT_FOUND",
                "details": f"Lease certificate '{clean_lease_id}' not found in registry.",
                "time_remaining_seconds": 0,
            }

        # Check Revocation
        if cert.status == OfflineLeaseStatus.REVOKED:
            return {
                "is_valid": False,
                "reason": "LEASE_REVOKED",
                "details": f"Lease certificate was revoked on {cert.revoked_at}. Reason: {cert.revocation_reason}",
                "time_remaining_seconds": 0,
            }

        if cert.status == OfflineLeaseStatus.SUPERSEDED:
            return {
                "is_valid": False,
                "reason": "LEASE_SUPERSEDED",
                "details": "Lease certificate has been superseded by a newer lease issued to this device.",
                "time_remaining_seconds": 0,
            }

        # Check Expiration
        if now >= cert.expires_at:
            if cert.status == OfflineLeaseStatus.ACTIVE:
                cert.status = OfflineLeaseStatus.EXPIRED
                cert.save(update_fields=["status"])
            return {
                "is_valid": False,
                "reason": "LEASE_EXPIRED",
                "details": f"Lease certificate expired on {cert.expires_at.isoformat()}.",
                "time_remaining_seconds": 0,
            }

        # Check Branch over-quota status
        if cert.branch.is_over_quota_locked:
            return {
                "is_valid": False,
                "reason": "BRANCH_LOCKED",
                "details": "Branch is locked due to over-quota downgrade policy.",
                "time_remaining_seconds": 0,
            }

        seconds_remaining = int((cert.expires_at - now).total_seconds())
        hours_remaining = max(1, seconds_remaining // 3600)

        return {
            "is_valid": True,
            "reason": "VALID",
            "time_remaining_seconds": seconds_remaining,
            "certificate": cls._serialize_certificate(cert),
            "client_messaging": {
                "headline_en": f"Signed Offline Lease Active ({hours_remaining} Hours Remaining)",
                "headline_bn": f"স্বাক্ষরিত অফলাইন ইজারা সক্রিয় (অবশিষ্ট {hours_remaining} ঘণ্টা)",
                "details_en": "POS operations permitted offline. Patient dispensing protected.",
                "details_bn": "অফলাইনে বিক্রয় সচল রয়েছে। রোগীদের সেবা ও নিয়মতান্ত্রিক ডিসপেন্সিং সুরক্ষিত।",
            },
        }

    @classmethod
    def revoke_offline_lease(
        cls,
        lease_id: str,
        reason: str,
        actor_user: Any = None,
    ) -> dict[str, Any]:
        """Revokes a device lease (e.g., lost or compromised hardware)."""
        clean_lease_id = str(lease_id or "").strip()
        clean_reason = str(reason or "").strip()
        if len(clean_reason) < 5:
            raise ValidationError("Revocation reason (minimum 5 characters) is required.")

        try:
            cert = DeviceLeaseCertificate.objects.get(lease_id=clean_lease_id)
        except DeviceLeaseCertificate.DoesNotExist as err:
            raise ValidationError(f"Lease '{clean_lease_id}' does not exist.") from err

        cert.status = OfflineLeaseStatus.REVOKED
        cert.revoked_at = timezone.now()
        cert.revocation_reason = clean_reason
        cert.save(update_fields=["status", "revoked_at", "revocation_reason"])

        user_id = str(getattr(actor_user, "user_id", None) or getattr(actor_user, "id", "") or "")
        AuditLogEntry.objects.create(
            action="OFFLINE_LEASE_REVOKED",
            resource_type="DEVICE_LEASE",
            resource_id=clean_lease_id,
            tenant_id=cert.tenant_id,
            user_id=user_id or None,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "reason": clean_reason,
                "device_id": cert.device_id,
                "branch_id": cert.branch_id,
            },
        )

        logger.info("Revoked offline lease %s for tenant %s: %s", clean_lease_id, cert.tenant_id, clean_reason)
        return cls._serialize_certificate(cert)

    @classmethod
    def _serialize_certificate(cls, cert: DeviceLeaseCertificate) -> dict[str, Any]:
        now = timezone.now()
        seconds_remaining = max(0, int((cert.expires_at - now).total_seconds())) if cert.expires_at > now else 0
        hours_remaining = max(0, seconds_remaining // 3600)

        if cert.status == OfflineLeaseStatus.ACTIVE and seconds_remaining > 0:
            hl_en = f"Signed Offline Lease Active ({hours_remaining} Hours Remaining)"
            hl_bn = f"স্বাক্ষরিত অফলাইন ইজারা সক্রিয় (অবশিষ্ট {hours_remaining} ঘণ্টা)"
            dt_en = "Device authorized for secure offline counter dispensing."
            dt_bn = "ডিভাইসটি নিরাপদ অফলাইন কাউন্টার বিক্রয়ের জন্য অনুমোদিত।"
        elif cert.status == OfflineLeaseStatus.REVOKED:
            hl_en = "Offline Lease Revoked"
            hl_bn = "অফলাইন ইজারা বাতিল করা হয়েছে"
            dt_en = f"Lease was revoked. Reason: {cert.revocation_reason}"
            dt_bn = f"ইজারা বাতিল করা হয়েছে। কারণ: {cert.revocation_reason}"
        elif cert.status == OfflineLeaseStatus.EXPIRED or seconds_remaining == 0:
            hl_en = "Offline Lease Expired — Reconnect to Renew"
            hl_bn = "অফলাইন ইজারার মেয়াদ শেষ — রিনিউ করতে অনলাইনে যুক্ত হোন"
            dt_en = "Connect device to internet to synchronize records and renew lease."
            dt_bn = "ডাটা সিঙ্ক ও ইজারা নবায়নের জন্য ডিভাইসটি ইন্টারনেটে সংযুক্ত করুন।"
        else:
            hl_en = f"Offline Lease: {cert.status}"
            hl_bn = f"অফলাইন ইজারা: {cert.status}"
            dt_en = "Device lease status updated."
            dt_bn = "ডিভাইস ইজারা স্ট্যাটাস হালনাগাদ করা হয়েছে।"

        return {
            "lease_id": cert.lease_id,
            "tenant_id": cert.tenant_id,
            "branch_id": cert.branch_id,
            "device_id": cert.device_id,
            "device_name": cert.device_name,
            "role_snapshot": cert.role_snapshot,
            "plan_tier": cert.plan_tier,
            "policy_version": cert.policy_version,
            "duration_hours": cert.duration_hours,
            "issued_at": cert.issued_at.isoformat(),
            "expires_at": cert.expires_at.isoformat(),
            "capabilities_snapshot": cert.capabilities_snapshot,
            "signature_algorithm": cert.signature_algorithm,
            "signature": cert.signature,
            "status": cert.status,
            "client_messaging": {
                "headline_en": hl_en,
                "headline_bn": hl_bn,
                "details_en": dt_en,
                "details_bn": dt_bn,
            },
        }
