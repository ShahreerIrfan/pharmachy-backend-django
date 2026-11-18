"""
backend/apps/core/connectivity_detection_service.py
Architecture Decision: ADR-0309 (P309).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Authoritative Connectivity Detection & Last-Sync Status Service:
- Deterministically classifies device connectivity into CONNECTED, DEGRADED, DISCONNECTED, or RECONNECTED
  using probe success, latency_ms (against configurable degraded threshold), packet_loss_pct (Decimal), and
  consecutive failure streaks.
- Integrates directly with P308 `DeviceSyncWatermark` and P307 `ChangeFeedEvent` to report accurate
  `last_successful_sync_at`, `pull_applied_sequence_no`, `push_acked_sequence_no`, `pull_lag_events`,
  and honest `cloud_stock_authoritative` flags (FALSE whenever DISCONNECTED or DEGRADED).
- Records append-only transition logs (`DeviceConnectivityTransitionLog`) with replay-safe idempotency.
- Supports authorized Owner/Manager threshold updates (`PATCH`) and safe archival (`DELETE`).
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.change_feed_service import ChangeFeedService, TENANT_WIDE_ROLES
from apps.core.double_entry_journal_service import q2
from apps.core.models import (
    ChangeFeedEvent,
    DeviceConnectivitySnapshot,
    DeviceConnectivityState,
    DeviceConnectivityTransitionLog,
    DeviceSyncWatermark,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DEGRADED_PACKET_LOSS_PCT = Decimal("10.00")

CONFIG_ALLOWED_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "OWNER_ADMIN",
    "ADMIN",
}


class ConnectivityDetectionService:
    """
    Non-blocking connectivity telemetry, state classification, and accurate last-sync reporting engine.
    """

    @classmethod
    def classify_state(
        cls,
        *,
        previous_state: str,
        probe_succeeded: bool,
        latency_ms: int,
        packet_loss_pct: Decimal,
        consecutive_failures: int,
        degraded_latency_threshold_ms: int,
    ) -> str:
        if not probe_succeeded:
            if consecutive_failures >= 2:
                return DeviceConnectivityState.DISCONNECTED
            return DeviceConnectivityState.DEGRADED

        if latency_ms >= degraded_latency_threshold_ms or packet_loss_pct >= DEGRADED_PACKET_LOSS_PCT:
            return DeviceConnectivityState.DEGRADED

        if previous_state in (DeviceConnectivityState.DISCONNECTED, DeviceConnectivityState.DEGRADED):
            return DeviceConnectivityState.RECONNECTED

        return DeviceConnectivityState.CONNECTED

    @classmethod
    def _localized_status(cls, state: str, pending_count: int) -> Dict[str, str]:
        if state == DeviceConnectivityState.DISCONNECTED:
            return {
                "en": f"Offline — Local Queue Active ({pending_count} pending)",
                "bn": f"অফলাইন — লোকাল কিউ সক্রিয় ({pending_count}টি পেন্ডিং)",
            }
        if state == DeviceConnectivityState.DEGRADED:
            return {
                "en": f"Degraded Network — Sync Delayed ({pending_count} pending)",
                "bn": f"দুর্বল নেটওয়ার্ক — সিঙ্ক বিলম্বিত ({pending_count}টি পেন্ডিং)",
            }
        if state == DeviceConnectivityState.RECONNECTED:
            return {
                "en": f"Reconnected — Catch-up Sync Ready ({pending_count} pending)",
                "bn": f"পুনরায় সংযুক্ত — সিঙ্ক প্রস্তুত ({pending_count}টি পেন্ডিং)",
            }
        return {
            "en": "Connected — Cloud Sync Healthy",
            "bn": "সংযুক্ত — ক্লাউড সিঙ্ক স্বাভাবিক",
        }

    @classmethod
    def _serialize_snapshot(cls, snap: DeviceConnectivitySnapshot, latest_server_seq: int = 0) -> Dict[str, Any]:
        wm = snap.sync_watermark
        pull_seq = wm.pull_applied_sequence_no if wm else 0
        push_seq = wm.push_acked_sequence_no if wm else 0
        pull_lag = max(0, int(latest_server_seq) - int(pull_seq))

        # Derive accurate last_successful_sync_at from persisted watermark timestamps
        last_sync_dt = snap.last_successful_sync_at
        if wm:
            candidates = [dt for dt in (last_sync_dt, wm.pull_checkpointed_at, wm.push_checkpointed_at) if dt]
            if candidates:
                last_sync_dt = max(candidates)

        is_live = snap.connectivity_state in (
            DeviceConnectivityState.CONNECTED,
            DeviceConnectivityState.RECONNECTED,
        )
        loc = cls._localized_status(snap.connectivity_state, snap.pending_outbox_count)

        return {
            "snapshot_id": snap.snapshot_id,
            "tenant_id": str(snap.tenant_id),
            "branch_id": str(snap.branch_id),
            "device_id": snap.device_registration.device_id,
            "connectivity_state": snap.connectivity_state,
            "cloud_stock_authoritative": is_live and pull_lag == 0,
            "latency_ms": snap.latency_ms,
            "packet_loss_pct": str(q2(snap.packet_loss_pct)),
            "consecutive_failures": snap.consecutive_failures,
            "pending_outbox_count": snap.pending_outbox_count,
            "pending_outbox_bdt": str(q2(snap.pending_outbox_bdt)),
            "degraded_latency_threshold_ms": snap.degraded_latency_threshold_ms,
            "pull_applied_sequence_no": pull_seq,
            "push_acked_sequence_no": push_seq,
            "latest_server_sequence": int(latest_server_seq),
            "pull_lag_events": pull_lag,
            "last_probe_at": snap.last_probe_at.isoformat() if snap.last_probe_at else None,
            "last_connected_at": snap.last_connected_at.isoformat() if snap.last_connected_at else None,
            "last_successful_sync_at": last_sync_dt.isoformat() if last_sync_dt else None,
            "state_changed_at": snap.state_changed_at.isoformat() if snap.state_changed_at else None,
            "is_archived": snap.is_archived,
            "status_label_en": loc["en"],
            "status_label_bn": loc["bn"],
        }

    @classmethod
    def _validate_telemetry(
        cls,
        latency_ms: Any,
        packet_loss_pct: Any,
        consecutive_failures: Any,
        pending_outbox_count: Any,
        pending_outbox_bdt: Any,
    ):
        try:
            lat = int(latency_ms if latency_ms is not None else 0)
            if lat < 0 or lat > 120000:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_LATENCY", "detail": "latency_ms must be between 0 and 120000."})

        try:
            pkt = q2(Decimal(str(packet_loss_pct if packet_loss_pct is not None else "0.00")))
            if pkt < Decimal("0.00") or pkt > Decimal("100.00"):
                raise ValueError()
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_PACKET_LOSS", "detail": "packet_loss_pct must be 0.00..100.00."})

        try:
            fails = int(consecutive_failures if consecutive_failures is not None else 0)
            if fails < 0:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_FAILURES", "detail": "consecutive_failures must be >= 0."})

        try:
            pend_cnt = int(pending_outbox_count if pending_outbox_count is not None else 0)
            if pend_cnt < 0:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_PENDING_COUNT", "detail": "pending_outbox_count must be >= 0."})

        try:
            pend_bdt = q2(Decimal(str(pending_outbox_bdt if pending_outbox_bdt is not None else "0.00")))
            if pend_bdt < Decimal("0.00"):
                raise ValueError()
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_PENDING_BDT", "detail": "pending_outbox_bdt must be >= 0.00."})

        return lat, pkt, fails, pend_cnt, pend_bdt

    @classmethod
    @transaction.atomic
    def record_heartbeat(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        device_fingerprint_sha256: str,
        probe_succeeded: bool = True,
        latency_ms: int = 0,
        packet_loss_pct: Any = "0.00",
        consecutive_failures: int = 0,
        pending_outbox_count: int = 0,
        pending_outbox_bdt: Any = "0.00",
        reason: str = "",
        idempotency_key: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        reg = ChangeFeedService._assert_device(tenant, device_id, device_fingerprint_sha256, actor_user)

        lat, pkt, fails, pend_cnt, pend_bdt = cls._validate_telemetry(
            latency_ms, packet_loss_pct, consecutive_failures, pending_outbox_count, pending_outbox_bdt
        )

        wm = DeviceSyncWatermark.objects.filter(device_registration=reg).first()
        snap, _ = DeviceConnectivitySnapshot.objects.select_for_update().get_or_create(
            device_registration=reg,
            defaults={
                "tenant": tenant,
                "branch": reg.branch,
                "sync_watermark": wm,
                "connectivity_state": DeviceConnectivityState.DISCONNECTED,
            },
        )

        latest_seq = ChangeFeedEvent.objects.filter(tenant=tenant).aggregate(m=Max("sequence_no")).get("m") or 0
        idem = str(idempotency_key or "").strip()
        if idem:
            prior = DeviceConnectivityTransitionLog.objects.filter(
                tenant=tenant, device_registration=reg, idempotency_key=idem
            ).first()
            if prior:
                return {
                    "idempotent_replay": True,
                    "state_changed": False,
                    "snapshot": cls._serialize_snapshot(snap, latest_seq),
                }

        now = timezone.now()
        prev_state = snap.connectivity_state
        effective_fails = 0 if probe_succeeded else max(fails, snap.consecutive_failures + 1)

        new_state = cls.classify_state(
            previous_state=prev_state,
            probe_succeeded=bool(probe_succeeded),
            latency_ms=lat,
            packet_loss_pct=pkt,
            consecutive_failures=effective_fails,
            degraded_latency_threshold_ms=snap.degraded_latency_threshold_ms,
        )

        state_changed = new_state != prev_state
        snap.sync_watermark = wm
        snap.connectivity_state = new_state
        snap.latency_ms = lat
        snap.packet_loss_pct = pkt
        snap.consecutive_failures = effective_fails
        snap.pending_outbox_count = pend_cnt
        snap.pending_outbox_bdt = pend_bdt
        snap.last_probe_at = now
        snap.is_archived = False

        if new_state in (DeviceConnectivityState.CONNECTED, DeviceConnectivityState.RECONNECTED):
            snap.last_connected_at = now
        if wm:
            candidates = [dt for dt in (snap.last_successful_sync_at, wm.pull_checkpointed_at, wm.push_checkpointed_at) if dt]
            if candidates:
                snap.last_successful_sync_at = max(candidates)
        if state_changed:
            snap.state_changed_at = now

        snap.save()

        transition_log_id = ""
        if state_changed or idem:
            log = DeviceConnectivityTransitionLog.objects.create(
                tenant=tenant,
                branch=reg.branch,
                device_registration=reg,
                snapshot=snap,
                from_state=prev_state,
                to_state=new_state,
                latency_ms=lat,
                packet_loss_pct=pkt,
                consecutive_failures=effective_fails,
                pending_outbox_count=pend_cnt,
                pending_outbox_bdt=pend_bdt,
                reason=str(reason or ("Automatic heartbeat probe" if probe_succeeded else "Probe failure detected"))[:255],
                idempotency_key=idem,
                recorded_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            )
            transition_log_id = log.log_id

        return {
            "idempotent_replay": False,
            "state_changed": state_changed,
            "transition_log_id": transition_log_id,
            "snapshot": cls._serialize_snapshot(snap, latest_seq),
        }

    @classmethod
    def list_snapshots(cls, *, tenant_id: str, include_archived: bool = False, actor_user: Any = None) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)

        latest_seq = ChangeFeedEvent.objects.filter(tenant=tenant).aggregate(m=Max("sequence_no")).get("m") or 0
        qs = DeviceConnectivitySnapshot.objects.filter(tenant=tenant).select_related(
            "device_registration", "sync_watermark", "branch"
        )
        if not include_archived:
            qs = qs.filter(is_archived=False)

        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            qs = qs.filter(branch_id=user_branch)

        items = [cls._serialize_snapshot(s, latest_seq) for s in qs[:100]]
        return {
            "tenant_id": str(tenant.tenant_id),
            "count": len(items),
            "snapshots": items,
        }

    @classmethod
    def list_transition_history(
        cls, *, tenant_id: str, device_id: Optional[str] = None, actor_user: Any = None
    ) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)

        qs = DeviceConnectivityTransitionLog.objects.filter(tenant=tenant).select_related("device_registration")
        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            qs = qs.filter(branch_id=user_branch)
        if device_id:
            qs = qs.filter(device_registration__device_id=str(device_id).strip())

        rows = [
            {
                "log_id": l.log_id,
                "device_id": l.device_registration.device_id,
                "from_state": l.from_state,
                "to_state": l.to_state,
                "latency_ms": l.latency_ms,
                "packet_loss_pct": str(q2(l.packet_loss_pct)),
                "consecutive_failures": l.consecutive_failures,
                "pending_outbox_count": l.pending_outbox_count,
                "pending_outbox_bdt": str(q2(l.pending_outbox_bdt)),
                "reason": l.reason,
                "created_at": l.created_at.isoformat(),
            }
            for l in qs[:100]
        ]
        return {"tenant_id": str(tenant.tenant_id), "count": len(rows), "transitions": rows}

    @classmethod
    @transaction.atomic
    def update_or_archive_snapshot(
        cls,
        *,
        tenant_id: str,
        snapshot_id: str,
        degraded_latency_threshold_ms: Optional[int] = None,
        archive: Optional[bool] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)

        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in CONFIG_ALLOWED_ROLES:
            raise PermissionDenied(
                {"error_code": "ERR_CONFIG_FORBIDDEN", "detail": "Only Pharmacy Owner or Branch Manager may configure or archive connectivity snapshots."}
            )

        snap = (
            DeviceConnectivitySnapshot.objects.select_for_update()
            .select_related("device_registration", "sync_watermark")
            .filter(tenant=tenant, snapshot_id=snapshot_id)
            .first()
        )
        if not snap:
            raise NotFound({"error_code": "ERR_SNAPSHOT_NOT_FOUND", "detail": f"Snapshot '{snapshot_id}' not found."})

        if not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            if str(snap.branch_id) != str(getattr(actor_user, "branch_id", "") or ""):
                raise PermissionDenied({"error_code": "ERR_BRANCH_SCOPE_DENIED", "detail": "Branch scope denied."})

        if degraded_latency_threshold_ms is not None:
            try:
                th = int(degraded_latency_threshold_ms)
                if th < 100 or th > 30000:
                    raise ValueError()
            except (TypeError, ValueError):
                raise ValidationError(
                    {"error_code": "ERR_INVALID_THRESHOLD", "detail": "degraded_latency_threshold_ms must be 100..30000."}
                )
            snap.degraded_latency_threshold_ms = th

        if archive is not None:
            snap.is_archived = bool(archive)

        snap.save()
        latest_seq = ChangeFeedEvent.objects.filter(tenant=tenant).aggregate(m=Max("sequence_no")).get("m") or 0
        return {"snapshot": cls._serialize_snapshot(snap, latest_seq)}
