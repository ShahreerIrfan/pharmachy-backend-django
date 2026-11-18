"""
backend/apps/core/cloud_push_protocol_service.py
Architecture Decision: ADR-0305 (P305).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Authoritative HTTPS Cloud Push Protocol service:
- Signed device identity verification via HMAC-SHA256 over canonical batch envelope
  keyed with device hardware fingerprint + active P302 DEK fingerprint (if provisioned).
- Bounded batch size enforcement (1 <= len(events) <= 50).
- Strict event ordering (monotonic ascending client_sequence within batch) & supported event_version check.
- Clock-skew protection (±15 minutes window around server UTC time).
- Delegates authoritative idempotent command persistence & two-device stock conflict detection directly
  to DurableOutboxService.ingest_outbox_batch (P304) — zero parallel disconnected logic.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.durable_outbox_service import DurableOutboxService
from apps.core.models import (
    CloudPushBatchStatus,
    CloudSyncPushBatch,
    DeviceEncryptionKeyRecord,
    DeviceEncryptionKeyStatus,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

SUPPORTED_PROTOCOL_VERSION = 1
SUPPORTED_EVENT_VERSIONS = {1, 2}
MAX_PUSH_BATCH_SIZE = 50
MAX_CLOCK_SKEW_SECONDS = 900  # 15 minutes


class CloudPushProtocolService:
    """
    Governs signed HTTPS sync push batches from JavaFX POS terminals.
    """

    @classmethod
    def get_protocol_spec(cls) -> Dict[str, Any]:
        return {
            "protocol_version": SUPPORTED_PROTOCOL_VERSION,
            "supported_event_versions": sorted(SUPPORTED_EVENT_VERSIONS),
            "max_batch_size": MAX_PUSH_BATCH_SIZE,
            "max_clock_skew_seconds": MAX_CLOCK_SKEW_SECONDS,
            "signature_algorithm": "HMAC-SHA256",
            "transport_requirement": "HTTPS_TLS_1_2_PLUS",
        }

    @classmethod
    def canonical_events_sha256(cls, events: List[Dict[str, Any]]) -> str:
        canonical = json.dumps(events, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def compute_device_hmac_signature(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        client_batch_id: str,
        protocol_version: int,
        pushed_at_iso: str,
        events_sha256: str,
        device_fingerprint_sha256: str,
        active_dek_fingerprint_sha256: str = "",
    ) -> str:
        signing_secret = f"{device_fingerprint_sha256.lower()}:{active_dek_fingerprint_sha256.lower()}".encode("utf-8")
        message = f"{tenant_id}|{device_id}|{client_batch_id}|{protocol_version}|{pushed_at_iso}|{events_sha256}".encode("utf-8")
        return hmac.new(signing_secret, message, hashlib.sha256).hexdigest()

    @classmethod
    def assert_tenant_active(cls, tenant_id: str) -> Tenant:
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' not found."})
        if (
            getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
            or getattr(tenant, "subscription_status", None) == "SUSPENDED"
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant subscription is suspended. Cloud sync push blocked.",
                    "message_en": "Tenant subscription is suspended. Cloud sync push blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। ক্লাউড সিঙ্ক পুশ বন্ধ রয়েছে।",
                }
            )
        return tenant

    @classmethod
    @transaction.atomic
    def execute_signed_push(
        cls,
        *,
        tenant_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        tenant = cls.assert_tenant_active(tenant_id)

        # 1. Protocol & Batch Header Validation
        device_id = str(data.get("device_id") or "").strip()
        client_batch_id = str(data.get("client_batch_id") or "").strip()
        pushed_at_raw = str(data.get("pushed_at") or "").strip()
        provided_sig = str(data.get("signature_hmac_sha256") or "").strip().lower()
        proto_ver = int(data.get("protocol_version") or 0)

        if proto_ver != SUPPORTED_PROTOCOL_VERSION:
            raise ValidationError(
                {
                    "error_code": "ERR_UNSUPPORTED_PROTOCOL_VERSION",
                    "detail": f"Unsupported protocol_version '{proto_ver}'. Expected {SUPPORTED_PROTOCOL_VERSION}.",
                }
            )

        if not device_id or not client_batch_id or not pushed_at_raw or len(provided_sig) != 64:
            raise ValidationError(
                {
                    "error_code": "ERR_INCOMPLETE_PUSH_ENVELOPE",
                    "detail": "device_id, client_batch_id, pushed_at, and 64-char signature_hmac_sha256 are required.",
                }
            )

        # Clock-skew protection
        try:
            pushed_dt = datetime.datetime.fromisoformat(pushed_at_raw.replace("Z", "+00:00"))
            if pushed_dt.tzinfo is None:
                pushed_dt = timezone.make_aware(pushed_dt, datetime.timezone.utc)
        except Exception:
            raise ValidationError({"error_code": "ERR_INVALID_PUSH_TIMESTAMP", "detail": "pushed_at must be ISO-8601 UTC."})

        skew_sec = abs((timezone.now() - pushed_dt).total_seconds())
        if skew_sec > MAX_CLOCK_SKEW_SECONDS:
            raise ValidationError(
                {
                    "error_code": "ERR_PUSH_TIMESTAMP_SKEW",
                    "detail": f"Device clock skew ({int(skew_sec)}s) exceeds allowed ±{MAX_CLOCK_SKEW_SECONDS}s window.",
                    "message_en": "Device clock is out of sync beyond allowed 15-minute window.",
                    "message_bn": "ডিভাইসের ঘড়ির সময় ১৫ মিনিটের বেশি অমিল রয়েছে। সময় ঠিক করে আবার চেষ্টা করুন।",
                }
            )

        # 2. Device Identity & Active Lease Verification
        reg = DeviceLocalDbRegistration.objects.select_related("branch").filter(tenant=tenant, device_id=device_id).first()
        if not reg:
            foreign = DeviceLocalDbRegistration.objects.filter(device_id=device_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant push forbidden."})
            raise NotFound({"error_code": "ERR_DEVICE_NOT_REGISTERED", "detail": f"Device '{device_id}' not registered."})

        if reg.status != DeviceLocalDbStatus.ACTIVE:
            raise PermissionDenied({"error_code": "ERR_DEVICE_REVOKED", "detail": "Revoked device cannot push."})
        if not reg.lease_expires_at or reg.lease_expires_at <= timezone.now():
            raise PermissionDenied({"error_code": "ERR_OFFLINE_LEASE_EXPIRED", "detail": "Offline subscription lease expired."})

        # Non-owner staff restricted to own branch
        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in (
            UserRole.PHARMACY_OWNER,
            UserRole.FINANCE_OFFICER,
            "PHARMACY_OWNER",
            "FINANCE_OFFICER",
            "OWNER_ADMIN",
            "ADMIN",
        ):
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            if user_branch and str(reg.branch_id) != user_branch:
                raise PermissionDenied({"error_code": "ERR_BRANCH_SCOPE_DENIED", "detail": "Staff cannot push for another branch's device."})

        # 3. Bounded Batch Size, Event Versions & Strict Ordering Checks
        events = data.get("events")
        if not isinstance(events, list) or len(events) == 0:
            raise ValidationError({"error_code": "ERR_EMPTY_PUSH_BATCH", "detail": "events list must contain at least 1 event."})
        if len(events) > MAX_PUSH_BATCH_SIZE:
            raise ValidationError(
                {
                    "error_code": "ERR_PUSH_BATCH_LIMIT_EXCEEDED",
                    "detail": f"Batch size {len(events)} exceeds max bounded limit of {MAX_PUSH_BATCH_SIZE} events.",
                    "message_en": f"Maximum {MAX_PUSH_BATCH_SIZE} events allowed per sync push batch.",
                    "message_bn": f"প্রতি সিঙ্ক ব্যাচে সর্বোচ্চ {MAX_PUSH_BATCH_SIZE}টি ইভেন্ট পাঠানো যাবে।",
                }
            )

        prev_seq = 0
        for idx, ev in enumerate(events, start=1):
            ev_ver = ev.get("event_version")
            if ev_ver not in SUPPORTED_EVENT_VERSIONS:
                raise ValidationError(
                    {
                        "error_code": "ERR_UNSUPPORTED_EVENT_VERSION",
                        "detail": f"Event #{idx} has unsupported event_version '{ev_ver}'. Allowed: {sorted(SUPPORTED_EVENT_VERSIONS)}.",
                    }
                )
            try:
                seq = int(ev.get("client_sequence", 0))
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_SEQUENCE", "detail": f"Invalid client_sequence on event #{idx}."})
            if seq <= prev_seq:
                raise ValidationError(
                    {
                        "error_code": "ERR_UNORDERED_EVENT_BATCH",
                        "detail": f"Events must be strictly ascending by client_sequence (event #{idx} has {seq} after {prev_seq}).",
                    }
                )
            prev_seq = seq

        # 4. Signed Device Identity HMAC-SHA256 Verification
        active_key = DeviceEncryptionKeyRecord.objects.filter(
            device_registration=reg, status=DeviceEncryptionKeyStatus.ACTIVE
        ).first()
        active_dek_fp = active_key.dek_fingerprint_sha256 if active_key else ""

        events_sha = cls.canonical_events_sha256(events)
        expected_sig = cls.compute_device_hmac_signature(
            tenant_id=str(tenant.tenant_id),
            device_id=reg.device_id,
            client_batch_id=client_batch_id,
            protocol_version=proto_ver,
            pushed_at_iso=pushed_at_raw,
            events_sha256=events_sha,
            device_fingerprint_sha256=reg.device_fingerprint_sha256,
            active_dek_fingerprint_sha256=active_dek_fp,
        )
        if not hmac.compare_digest(provided_sig, expected_sig):
            raise PermissionDenied(
                {
                    "error_code": "ERR_INVALID_DEVICE_SIGNATURE",
                    "detail": "HMAC-SHA256 device push signature verification failed.",
                    "message_en": "Invalid device cryptographic signature.",
                    "message_bn": "ডিভাইসের ক্রিপ্টোগ্রাফিক সিগনেচার যাচাই ব্যর্থ হয়েছে।",
                }
            )

        # 5. Batch-Level Idempotent Replay Guard
        existing_batch = CloudSyncPushBatch.objects.filter(
            device_registration=reg, client_batch_id=client_batch_id
        ).first()
        if existing_batch:
            if existing_batch.events_sha256 != events_sha:
                raise ValidationError(
                    {
                        "error_code": "ERR_BATCH_ID_PAYLOAD_MISMATCH",
                        "detail": f"client_batch_id '{client_batch_id}' already submitted with different payload hash.",
                    }
                )
            return {
                "push_batch_id": existing_batch.push_batch_id,
                "client_batch_id": existing_batch.client_batch_id,
                "is_batch_replay": True,
                "status": existing_batch.status,
                "event_count": existing_batch.event_count,
                "accepted_count": existing_batch.accepted_count,
                "duplicate_replay_count": existing_batch.duplicate_count,
                "conflict_count": existing_batch.conflict_count,
                "signature_verified": True,
                "results": [],
            }

        # 6. Delegate Command Processing directly to P304 DurableOutboxService (Zero parallel ledger!)
        ingest_res = DurableOutboxService.ingest_outbox_batch(
            tenant_id=str(tenant.tenant_id),
            device_id=reg.device_id,
            device_fingerprint_sha256=reg.device_fingerprint_sha256,
            commands=events,
            actor_user=actor_user,
        )

        batch_status = (
            CloudPushBatchStatus.PARTIAL_CONFLICT
            if ingest_res["conflict_count"] > 0
            else CloudPushBatchStatus.COMPLETED
        )

        batch_rec = CloudSyncPushBatch.objects.create(
            tenant=tenant,
            branch=reg.branch,
            device_registration=reg,
            client_batch_id=client_batch_id,
            protocol_version=proto_ver,
            event_count=len(events),
            accepted_count=ingest_res["accepted_count"],
            duplicate_count=ingest_res["duplicate_replay_count"],
            conflict_count=ingest_res["conflict_count"],
            events_sha256=events_sha,
            signature_verified=True,
            status=batch_status,
            pushed_at=pushed_dt,
            submitted_by=actor_user,
        )

        # Advance device last_sync_cursor
        reg.last_sync_cursor = f"push-seq-{prev_seq}:{batch_rec.push_batch_id}"
        reg.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "pos@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "CASHIER")),
            action="CLOUD_SYNC_PUSH_BATCH",
            resource_type="CloudSyncPushBatch",
            resource_id=batch_rec.push_batch_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "device_id": reg.device_id,
                "events": len(events),
                "accepted": ingest_res["accepted_count"],
                "conflicts": ingest_res["conflict_count"],
            },
        )

        return {
            "push_batch_id": batch_rec.push_batch_id,
            "client_batch_id": batch_rec.client_batch_id,
            "is_batch_replay": False,
            "protocol_version": proto_ver,
            "status": batch_rec.status,
            "event_count": len(events),
            "accepted_count": ingest_res["accepted_count"],
            "duplicate_replay_count": ingest_res["duplicate_replay_count"],
            "conflict_count": ingest_res["conflict_count"],
            "signature_verified": True,
            "last_sync_cursor": reg.last_sync_cursor,
            "results": ingest_res["results"],
            "message_en": f"Signed push batch '{client_batch_id}' processed ({ingest_res['accepted_count']} accepted, {ingest_res['conflict_count']} conflicts).",
            "message_bn": f"সাইনড পুশ ব্যাচ '{client_batch_id}' প্রসেস সম্পন্ন ({ingest_res['accepted_count']}টি গৃহীত, {ingest_res['conflict_count']}টি কনফ্লিক্ট)।",
        }

    @classmethod
    def list_push_batches(
        cls, *, tenant_id: str, branch_id: Optional[str] = None, actor_user: Any = None
    ) -> Dict[str, Any]:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        tenant = cls.assert_tenant_active(tenant_id)
        qs = CloudSyncPushBatch.objects.filter(tenant=tenant).select_related("device_registration", "branch")
        if getattr(actor_user, "role", None) in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
            qs = qs.filter(branch_id=str(getattr(actor_user, "branch_id", "")))
        elif branch_id:
            qs = qs.filter(branch_id=branch_id)
        items = [
            {
                "push_batch_id": b.push_batch_id,
                "client_batch_id": b.client_batch_id,
                "device_id": b.device_registration.device_id,
                "branch_name": b.branch.name if b.branch else "",
                "protocol_version": b.protocol_version,
                "event_count": b.event_count,
                "accepted_count": b.accepted_count,
                "duplicate_count": b.duplicate_count,
                "conflict_count": b.conflict_count,
                "signature_verified": b.signature_verified,
                "status": b.status,
                "pushed_at": b.pushed_at.isoformat(),
            }
            for b in qs[:100]
        ]
        return {"tenant_id": str(tenant.tenant_id), "count": len(items), "batches": items}
