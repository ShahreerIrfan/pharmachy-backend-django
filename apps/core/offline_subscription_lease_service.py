"""
backend/apps/core/offline_subscription_lease_service.py
Architecture Decision: ADR-0312 (P312).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Authoritative Signed Offline Subscription & Role Lease Service:
- Issues HMAC-SHA256 signed offline subscription/role leases bound strictly to a registered POS device
  and updates `DeviceLocalDbRegistration.lease_expires_at`.
- Prevents device-clock-rollback bypasses via server clock anchor + monotonic high-water mark:
  if `client_reported_epoch_ms` drops backwards beyond tolerance (5 minutes), quarantines the lease
  as `CLOCK_TAMPER_QUARANTINED` and blocks new offline sales (`403 ERR_DEVICE_CLOCK_ROLLBACK_DETECTED`).
- Preserves PROTECTED SAFE SYNC RECOVERY PATHS (`/protected-recovery-sync/`):
  even when a lease is expired (`EXPIRED_RECOVERY_ONLY`), quarantined, OR the tenant subscription is
  `SUSPENDED` / lapsed, queued offline invoices & financial totals can still be safely flushed/exported
  with `new_offline_sales_allowed=False` and zero silent data deletion!
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.change_feed_service import ChangeFeedService, TENANT_WIDE_ROLES
from apps.core.double_entry_journal_service import q2
from apps.core.models import (
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    OfflineLeaseAuditEvent,
    SignedOfflineLeaseState,
    SignedOfflineSubscriptionLease,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

CLOCK_ROLLBACK_TOLERANCE_MS = 300_000  # 5 minutes max NTP jitter

LEASE_ADMIN_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "OWNER_ADMIN",
    "ADMIN",
}


class OfflineSubscriptionLeaseService:
    """
    Signed offline subscription/role lease issuer, anti-clock-tampering verifier,
    and protected sync recovery coordinator.
    """

    @classmethod
    def _signing_secret(cls) -> bytes:
        key = getattr(settings, "SECRET_KEY", "pharmacy-offline-lease-fallback")
        return f"p312-offline-lease-hmac|{key}".encode("utf-8")

    @classmethod
    def compute_lease_hmac(
        cls,
        *,
        tenant_id: str,
        branch_id: str,
        device_id: str,
        device_fingerprint_sha256: str,
        plan_tier: str,
        allowed_roles: List[str],
        issued_epoch_ms: int,
        expires_epoch_ms: int,
        monotonic_counter: int,
    ) -> str:
        payload = json.dumps(
            {
                "tenant_id": tenant_id,
                "branch_id": branch_id,
                "device_id": device_id,
                "fp": device_fingerprint_sha256,
                "plan": plan_tier,
                "roles": sorted(allowed_roles),
                "issued_ms": issued_epoch_ms,
                "expires_ms": expires_epoch_ms,
                "counter": monotonic_counter,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(cls._signing_secret(), payload, hashlib.sha256).hexdigest()

    @classmethod
    def _serialize_lease(cls, lease: SignedOfflineSubscriptionLease) -> Dict[str, Any]:
        now = timezone.now()
        is_expired = lease.expires_at <= now
        effective_status = lease.status
        if effective_status == SignedOfflineLeaseState.ACTIVE and is_expired:
            effective_status = SignedOfflineLeaseState.EXPIRED_RECOVERY_ONLY

        new_sales_ok = effective_status == SignedOfflineLeaseState.ACTIVE and not is_expired
        recovery_ok = effective_status != SignedOfflineLeaseState.REVOKED or lease.pending_recovery_count > 0

        return {
            "lease_id": lease.lease_id,
            "tenant_id": str(lease.tenant_id),
            "branch_id": str(lease.branch_id),
            "device_id": lease.device_registration.device_id,
            "plan_tier": lease.plan_tier,
            "allowed_roles": lease.allowed_roles_json,
            "entitlements": lease.entitlements_json,
            "issued_at": lease.issued_at.isoformat(),
            "expires_at": lease.expires_at.isoformat(),
            "server_clock_anchor_epoch_ms": lease.server_clock_anchor_epoch_ms,
            "last_observed_client_epoch_ms": lease.last_observed_client_epoch_ms,
            "monotonic_counter": lease.monotonic_counter,
            "grace_recovery_window_hours": lease.grace_recovery_window_hours,
            "lease_signature_hmac_sha256": lease.lease_signature_hmac_sha256,
            "status": effective_status,
            "new_offline_sales_allowed": new_sales_ok,
            "protected_sync_recovery_allowed": recovery_ok,
            "pending_recovery_count": lease.pending_recovery_count,
            "pending_recovery_bdt": str(q2(lease.pending_recovery_bdt)),
            "revocation_reason": lease.revocation_reason,
            "created_at": lease.created_at.isoformat(),
        }

    @classmethod
    def _assert_admin_role(cls, actor_user: Any, branch_id: Optional[str] = None) -> None:
        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in LEASE_ADMIN_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_LEASE_ADMIN_REQUIRED",
                    "detail": "Only Pharmacy Owner or Branch Manager may issue, update, or revoke offline leases.",
                }
            )
        if branch_id and not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            if str(getattr(actor_user, "branch_id", "") or "") != str(branch_id):
                raise PermissionDenied({"error_code": "ERR_BRANCH_SCOPE_DENIED", "detail": "Branch scope denied."})

    @classmethod
    @transaction.atomic
    def issue_signed_lease(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        plan_tier: str = "STANDARD",
        allowed_roles: Optional[List[str]] = None,
        entitlements: Optional[Dict[str, Any]] = None,
        lease_duration_hours: int = 72,
        grace_recovery_window_hours: int = 168,
        idempotency_key: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)

        reg = (
            DeviceLocalDbRegistration.objects.select_for_update()
            .select_related("branch")
            .filter(tenant=tenant, device_id=str(device_id).strip())
            .first()
        )
        if not reg:
            if DeviceLocalDbRegistration.objects.filter(device_id=str(device_id).strip()).exists():
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant device lease forbidden."})
            raise NotFound({"error_code": "ERR_DEVICE_NOT_REGISTERED", "detail": f"Device '{device_id}' not registered."})

        cls._assert_admin_role(actor_user, branch_id=str(reg.branch_id))
        if reg.status != DeviceLocalDbStatus.ACTIVE:
            raise PermissionDenied({"error_code": "ERR_DEVICE_REVOKED", "detail": "Revoked device cannot receive an offline lease."})

        idem = str(idempotency_key or "").strip()
        if not idem:
            raise ValidationError({"error_code": "ERR_IDEMPOTENCY_KEY_REQUIRED", "detail": "idempotency_key is required."})

        existing = SignedOfflineSubscriptionLease.objects.select_related("device_registration").filter(
            tenant=tenant, idempotency_key=idem
        ).first()
        if existing:
            return {"idempotent_replay": True, "lease": cls._serialize_lease(existing)}

        try:
            dur_h = int(lease_duration_hours)
            if dur_h < 1 or dur_h > 336:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_LEASE_DURATION", "detail": "lease_duration_hours must be 1..336."})

        try:
            grace_h = int(grace_recovery_window_hours)
            if grace_h < 24 or grace_h > 720:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_GRACE_WINDOW", "detail": "grace_recovery_window_hours must be 24..720."})

        roles = [str(r).strip().upper() for r in (allowed_roles or ["CASHIER", "BRANCH_MANAGER", "PHARMACY_OWNER"]) if str(r).strip()]
        if not roles:
            raise ValidationError({"error_code": "ERR_EMPTY_ALLOWED_ROLES", "detail": "allowed_roles must contain at least one role."})

        max_ctr = (
            SignedOfflineSubscriptionLease.objects.filter(device_registration=reg).aggregate(m=Max("monotonic_counter")).get("m")
            or 0
        )
        next_ctr = int(max_ctr) + 1
        now = timezone.now()
        expires = now + datetime.timedelta(hours=dur_h)
        issued_ms = int(now.timestamp() * 1000)
        expires_ms = int(expires.timestamp() * 1000)

        sig = cls.compute_lease_hmac(
            tenant_id=str(tenant.tenant_id),
            branch_id=str(reg.branch_id),
            device_id=reg.device_id,
            device_fingerprint_sha256=reg.device_fingerprint_sha256,
            plan_tier=str(plan_tier or "STANDARD").strip().upper(),
            allowed_roles=roles,
            issued_epoch_ms=issued_ms,
            expires_epoch_ms=expires_ms,
            monotonic_counter=next_ctr,
        )

        lease = SignedOfflineSubscriptionLease.objects.create(
            tenant=tenant,
            branch=reg.branch,
            device_registration=reg,
            plan_tier=str(plan_tier or "STANDARD").strip().upper(),
            allowed_roles_json=roles,
            entitlements_json=dict(entitlements or {"offline_pos": True, "change_feed": True}),
            issued_at=now,
            expires_at=expires,
            server_clock_anchor_epoch_ms=issued_ms,
            last_observed_client_epoch_ms=issued_ms,
            monotonic_counter=next_ctr,
            grace_recovery_window_hours=grace_h,
            lease_signature_hmac_sha256=sig,
            status=SignedOfflineLeaseState.ACTIVE,
            idempotency_key=idem,
            issued_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        # Keep P301 DeviceLocalDbRegistration.lease_expires_at in sync!
        reg.lease_expires_at = expires
        reg.save(update_fields=["lease_expires_at", "updated_at"])

        OfflineLeaseAuditEvent.objects.create(
            tenant=tenant,
            branch=reg.branch,
            lease=lease,
            event_type="LEASE_ISSUED",
            client_reported_epoch_ms=issued_ms,
            server_epoch_ms=issued_ms,
            detail=f"Issued signed offline lease (plan={lease.plan_tier}, duration={dur_h}h, counter={next_ctr}).",
            actor=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        return {
            "idempotent_replay": False,
            "lease": cls._serialize_lease(lease),
            "message_en": f"Signed offline lease issued for {dur_h}h (counter #{next_ctr}).",
            "message_bn": f"{dur_h} ঘণ্টার স্বাক্ষরিত অফলাইন লিজ ইস্যু হয়েছে (কাউন্টার #{next_ctr})।",
        }

    @classmethod
    def verify_lease(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        device_fingerprint_sha256: str,
        lease_id: str,
        lease_signature_hmac_sha256: str,
        client_reported_epoch_ms: int,
        requested_role: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Verifies HMAC signature, role entitlement, expiration, and monotonic clock integrity.
        Rejects ANY device-clock-rollback bypass attempt by quarantining the lease!
        """
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)

        reg = DeviceLocalDbRegistration.objects.select_related("branch").filter(tenant=tenant, device_id=device_id).first()
        if not reg:
            if DeviceLocalDbRegistration.objects.filter(device_id=device_id).exists():
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant verification forbidden."})
            raise NotFound({"error_code": "ERR_DEVICE_NOT_REGISTERED", "detail": f"Device '{device_id}' not registered."})

        if str(device_fingerprint_sha256 or "").strip().lower() != reg.device_fingerprint_sha256:
            raise PermissionDenied({"error_code": "ERR_DEVICE_FINGERPRINT_MISMATCH", "detail": "Device fingerprint mismatch."})

        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            if str(getattr(actor_user, "branch_id", "") or "") != str(reg.branch_id):
                raise PermissionDenied({"error_code": "ERR_BRANCH_SCOPE_DENIED", "detail": "Branch scope denied."})

        try:
            client_ms = int(client_reported_epoch_ms)
            if client_ms <= 0:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_CLOCK_EPOCH", "detail": "client_reported_epoch_ms must be positive."})

        now = timezone.now()
        server_ms = int(now.timestamp() * 1000)

        with transaction.atomic():
            lease = (
                SignedOfflineSubscriptionLease.objects.select_for_update()
                .select_related("device_registration")
                .filter(tenant=tenant, device_registration=reg, lease_id=str(lease_id).strip())
                .first()
            )
            if not lease:
                raise NotFound({"error_code": "ERR_LEASE_NOT_FOUND", "detail": f"Lease '{lease_id}' not found."})

            if not hmac.compare_digest(str(lease_signature_hmac_sha256 or "").strip().lower(), lease.lease_signature_hmac_sha256):
                raise PermissionDenied({"error_code": "ERR_LEASE_SIGNATURE_TAMPERED", "detail": "Signed offline lease HMAC mismatch."})

            if lease.status == SignedOfflineLeaseState.REVOKED:
                raise PermissionDenied({"error_code": "ERR_LEASE_REVOKED", "detail": "Offline lease was revoked."})

            # Anti-device-clock-bypass check: detect backward clock jump against server anchor OR high-water mark!
            high_water = max(lease.server_clock_anchor_epoch_ms, lease.last_observed_client_epoch_ms)
            if client_ms < (high_water - CLOCK_ROLLBACK_TOLERANCE_MS):
                lease.status = SignedOfflineLeaseState.CLOCK_TAMPER_QUARANTINED
                lease.save(update_fields=["status", "updated_at"])
                OfflineLeaseAuditEvent.objects.create(
                    tenant=tenant,
                    branch=reg.branch,
                    lease=lease,
                    event_type="CLOCK_ROLLBACK_DETECTED",
                    client_reported_epoch_ms=client_ms,
                    server_epoch_ms=server_ms,
                    detail=f"Device clock rolled back ({client_ms} < high-water {high_water}); quarantined from new offline sales.",
                    actor=actor_user if getattr(actor_user, "is_authenticated", False) else None,
                )
                # Commit quarantine status before raising 403!
                raise_tamper = True
            else:
                raise_tamper = False

        if raise_tamper:
            raise PermissionDenied(
                {
                    "error_code": "ERR_DEVICE_CLOCK_ROLLBACK_DETECTED",
                    "detail": "Device clock rollback detected; offline sales blocked. Protected sync recovery remains available.",
                    "protected_sync_recovery_allowed": True,
                    "message_en": "Device clock tampering detected. New offline sales blocked; sync recovery protected.",
                    "message_bn": "ডিভাইসের ঘড়ি পেছানো শনাক্ত হয়েছে। নতুন অফলাইন বিক্রয় ব্লকড; পেন্ডিং সিঙ্ক রিকভারি সুরক্ষিত আছে।",
                }
            )

        with transaction.atomic():
            lease = SignedOfflineSubscriptionLease.objects.select_for_update().select_related("device_registration").get(
                lease_id=lease.lease_id
            )
            if lease.status == SignedOfflineLeaseState.CLOCK_TAMPER_QUARANTINED:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_LEASE_CLOCK_QUARANTINED",
                        "detail": "Lease quarantined due to prior clock rollback; re-issue required after online verification.",
                    }
                )

            if requested_role:
                req_r = str(requested_role).strip().upper()
                if req_r not in [r.upper() for r in (lease.allowed_roles_json or [])]:
                    raise PermissionDenied(
                        {"error_code": "ERR_ROLE_NOT_IN_OFFLINE_LEASE", "detail": f"Role '{req_r}' is not authorized in this offline lease."}
                    )

            # Enforce authoritative server clock expiration (device clock CANNOT extend expiry!)
            if lease.expires_at <= now:
                lease.status = SignedOfflineLeaseState.EXPIRED_RECOVERY_ONLY
                lease.save(update_fields=["status", "updated_at"])
                return {
                    "verified": False,
                    "new_offline_sales_allowed": False,
                    "protected_sync_recovery_allowed": True,
                    "lease": cls._serialize_lease(lease),
                    "message_en": "Offline lease expired. New offline sales blocked; protected sync recovery remains available.",
                    "message_bn": "অফলাইন লিজ মেয়াদোত্তীর্ণ। নতুন অফলাইন বিক্রয় বন্ধ; সুরক্ষিত সিঙ্ক রিকভারি চালু আছে।",
                }

            if client_ms > lease.last_observed_client_epoch_ms:
                lease.last_observed_client_epoch_ms = client_ms
                lease.save(update_fields=["last_observed_client_epoch_ms", "updated_at"])

            return {
                "verified": True,
                "new_offline_sales_allowed": True,
                "protected_sync_recovery_allowed": True,
                "lease": cls._serialize_lease(lease),
                "message_en": "Signed offline lease verified cleanly.",
                "message_bn": "স্বাক্ষরিত অফলাইন লিজ সঠিকভাবে যাচাই হয়েছে।",
            }

    @classmethod
    @transaction.atomic
    def protected_recovery_sync(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        device_fingerprint_sha256: str,
        lease_id: str,
        pending_outbox_count: int,
        pending_outbox_bdt: Any,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        PROTECTED SAFE SYNC RECOVERY PATH:
        Deliberately remains accessible even if the offline lease is EXPIRED_RECOVERY_ONLY,
        CLOCK_TAMPER_QUARANTINED, OR the tenant subscription is SUSPENDED — guaranteeing queued
        offline invoices are NEVER silently deleted or stranded (while new_offline_sales_allowed=False)!
        """
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' not found."})
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)

        reg = DeviceLocalDbRegistration.objects.select_related("branch").filter(tenant=tenant, device_id=device_id).first()
        if not reg:
            if DeviceLocalDbRegistration.objects.filter(device_id=device_id).exists():
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant recovery forbidden."})
            raise NotFound({"error_code": "ERR_DEVICE_NOT_REGISTERED", "detail": f"Device '{device_id}' not registered."})

        if str(device_fingerprint_sha256 or "").strip().lower() != reg.device_fingerprint_sha256:
            raise PermissionDenied({"error_code": "ERR_DEVICE_FINGERPRINT_MISMATCH", "detail": "Fingerprint mismatch."})

        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            if str(getattr(actor_user, "branch_id", "") or "") != str(reg.branch_id):
                raise PermissionDenied({"error_code": "ERR_BRANCH_SCOPE_DENIED", "detail": "Branch scope denied."})

        try:
            cnt = int(pending_outbox_count)
            bdt = q2(Decimal(str(pending_outbox_bdt)))
            if cnt < 0 or bdt < Decimal("0.00"):
                raise ValueError()
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_RECOVERY_PAYLOAD", "detail": "pending_outbox_count and pending_outbox_bdt must be non-negative."})

        lease = (
            SignedOfflineSubscriptionLease.objects.select_for_update()
            .select_related("device_registration")
            .filter(tenant=tenant, device_registration=reg, lease_id=str(lease_id).strip())
            .first()
        )
        if not lease:
            raise NotFound({"error_code": "ERR_LEASE_NOT_FOUND", "detail": f"Lease '{lease_id}' not found."})

        lease.pending_recovery_count = cnt
        lease.pending_recovery_bdt = bdt
        lease.save(update_fields=["pending_recovery_count", "pending_recovery_bdt", "updated_at"])

        now_ms = int(timezone.now().timestamp() * 1000)
        ev = OfflineLeaseAuditEvent.objects.create(
            tenant=tenant,
            branch=reg.branch,
            lease=lease,
            event_type="PROTECTED_RECOVERY_SYNC",
            server_epoch_ms=now_ms,
            recovered_outbox_count=cnt,
            recovered_bdt=bdt,
            detail=f"Protected recovery sync accepted {cnt} queued item(s) totaling BDT {bdt} (tenant_status={tenant.status}, lease_status={lease.status}).",
            actor=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        is_suspended = getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        return {
            "recovery_accepted": True,
            "audit_event_id": ev.event_id,
            "data_preserved_zero_deletion": True,
            "new_offline_sales_allowed": False if (is_suspended or lease.status != SignedOfflineLeaseState.ACTIVE) else True,
            "tenant_status": tenant.status,
            "recovered_outbox_count": cnt,
            "recovered_bdt": str(bdt),
            "lease": cls._serialize_lease(lease),
            "message_en": f"Protected recovery sync preserved {cnt} pending item(s) (BDT {bdt}) safely.",
            "message_bn": f"সুরক্ষিত রিকভারি সিঙ্কে {cnt}টি পেন্ডিং আইটেম (৳{bdt}) নিরাপদে সংরক্ষিত হয়েছে।",
        }

    @classmethod
    @transaction.atomic
    def revoke_lease(cls, *, tenant_id: str, lease_id: str, reason: str, actor_user: Any = None) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)

        lease = (
            SignedOfflineSubscriptionLease.objects.select_for_update()
            .select_related("device_registration")
            .filter(tenant=tenant, lease_id=str(lease_id).strip())
            .first()
        )
        if not lease:
            raise NotFound({"error_code": "ERR_LEASE_NOT_FOUND", "detail": f"Lease '{lease_id}' not found."})

        cls._assert_admin_role(actor_user, branch_id=str(lease.branch_id))
        clean_reason = str(reason or "").strip()
        if len(clean_reason) < 5:
            raise ValidationError({"error_code": "ERR_REASON_REQUIRED", "detail": "Revocation reason (>= 5 chars) is required."})

        lease.status = SignedOfflineLeaseState.REVOKED
        lease.revocation_reason = clean_reason[:255]
        lease.save(update_fields=["status", "revocation_reason", "updated_at"])

        OfflineLeaseAuditEvent.objects.create(
            tenant=tenant,
            branch=lease.branch,
            lease=lease,
            event_type="LEASE_REVOKED",
            server_epoch_ms=int(timezone.now().timestamp() * 1000),
            detail=f"Lease revoked: {clean_reason}",
            actor=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        # Emit P307 ChangeFeed TOMBSTONE so POS disables offline sales immediately
        ChangeFeedService.publish_change_event(
            tenant_id=str(tenant.tenant_id),
            entity_type="SUBSCRIPTION_LEASE",
            entity_id=lease.lease_id,
            operation="TOMBSTONE",
            payload_json={"tombstone_reason": clean_reason, "revoked_lease_id": lease.lease_id},
            branch_id=str(lease.branch_id),
            entity_version=lease.monotonic_counter + 1,
            source_device_id=lease.device_registration.device_id,
            idempotency_key=f"sol-rev:{lease.lease_id}",
            actor_user=actor_user,
            system_internal=True,
        )

        return {"lease": cls._serialize_lease(lease)}

    @classmethod
    def list_leases(cls, *, tenant_id: str, actor_user: Any = None) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)
        qs = SignedOfflineSubscriptionLease.objects.filter(tenant=tenant).select_related("device_registration")
        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            qs = qs.filter(branch_id=str(getattr(actor_user, "branch_id", "") or ""))
        rows = [cls._serialize_lease(l) for l in qs[:100]]
        return {"tenant_id": str(tenant.tenant_id), "count": len(rows), "leases": rows}
