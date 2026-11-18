"""
backend/apps/core/sync_cursor_checkpoint_service.py
Architecture Decision: ADR-0308 (P308).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Authoritative Sync Cursor Checkpoint Service:
- Enforces the core crash-safety invariant: pull cursor progress is checkpointed ONLY AFTER
  the POS device proves durable local SQLite apply (`durable_local_apply_verified=True` + 64-char SHA-256 digest).
- Maintains a strictly independent Push Acknowledgment watermark (`push_acked_sequence_no`,
  `push_monetary_reconciled_bdt`) linked to P306 `CanonicalCommandAcknowledgment` so crashes or
  network drops in pull never lose push acks, and push conflicts never block pull progress.
- Provides unified `/recover/` crash-recovery state (returning both watermarks, pull lag, and any
  server-committed P306 canonical acks not yet confirmed applied on the client).
- Supports owner/manager-only audited recovery rewind (`audited_reset_cursor`) with epoch increment.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.canonical_ack_service import CanonicalAckService
from apps.core.change_feed_service import ChangeFeedService, TENANT_WIDE_ROLES
from apps.core.double_entry_journal_service import q2
from apps.core.models import (
    CanonicalCommandAcknowledgment,
    ChangeFeedEvent,
    DeviceChangeFeedCursor,
    DeviceSyncWatermark,
    SyncCheckpointStatus,
    SyncCheckpointStream,
    SyncCursorCheckpointLedger,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

RESET_ALLOWED_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "OWNER_ADMIN",
    "ADMIN",
}


class SyncCursorCheckpointService:
    """
    Crash-safe decoupled pull-cursor & push-acknowledgment checkpoint engine.
    """

    @classmethod
    def _serialize_watermark(cls, wm: DeviceSyncWatermark, latest_server_seq: int = 0) -> Dict[str, Any]:
        lag = max(0, int(latest_server_seq) - int(wm.pull_applied_sequence_no))
        return {
            "watermark_id": wm.watermark_id,
            "tenant_id": str(wm.tenant_id),
            "branch_id": str(wm.branch_id),
            "device_id": wm.device_registration.device_id,
            "pull_applied_sequence_no": wm.pull_applied_sequence_no,
            "pull_apply_digest_sha256": wm.pull_apply_digest_sha256,
            "pull_checkpointed_at": wm.pull_checkpointed_at.isoformat() if wm.pull_checkpointed_at else None,
            "push_acked_sequence_no": wm.push_acked_sequence_no,
            "push_last_ack_id": wm.push_last_ack_id,
            "push_total_acked_count": wm.push_total_acked_count,
            "push_monetary_reconciled_bdt": str(q2(wm.push_monetary_reconciled_bdt)),
            "push_checkpointed_at": wm.push_checkpointed_at.isoformat() if wm.push_checkpointed_at else None,
            "recovery_epoch": wm.recovery_epoch,
            "latest_server_sequence": int(latest_server_seq),
            "pull_lag_events": lag,
            "updated_at": wm.updated_at.isoformat(),
        }

    @classmethod
    def _get_or_create_watermark_locked(cls, tenant, reg) -> DeviceSyncWatermark:
        wm, _ = DeviceSyncWatermark.objects.select_for_update().get_or_create(
            device_registration=reg,
            defaults={
                "tenant": tenant,
                "branch": reg.branch,
                "pull_applied_sequence_no": 0,
                "push_acked_sequence_no": 0,
                "recovery_epoch": 1,
            },
        )
        return wm

    @classmethod
    def commit_pull_checkpoint(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        device_fingerprint_sha256: str,
        acknowledged_sequence_no: int,
        durable_local_apply_verified: bool,
        local_apply_digest_sha256: str,
        cursor_token: str = "",
        applied_event_count: int = 0,
        idempotency_key: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        reg = ChangeFeedService._assert_device(tenant, device_id, device_fingerprint_sha256, actor_user)

        try:
            ack_seq = int(acknowledged_sequence_no)
        except (TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_SEQUENCE", "detail": "acknowledged_sequence_no must be an integer."})

        digest = str(local_apply_digest_sha256 or "").strip().lower()

        # Core P308 invariant: NEVER advance pull cursor if local durable SQLite apply was NOT verified!
        if durable_local_apply_verified is not True or not HEX64_RE.match(digest):
            with transaction.atomic():
                wm = cls._get_or_create_watermark_locked(tenant, reg)
                SyncCursorCheckpointLedger.objects.create(
                    tenant=tenant,
                    branch=reg.branch,
                    device_registration=reg,
                    watermark=wm,
                    stream=SyncCheckpointStream.PULL_FEED,
                    status=SyncCheckpointStatus.REJECTED_UNAPPLIED,
                    previous_sequence_no=wm.pull_applied_sequence_no,
                    new_sequence_no=wm.pull_applied_sequence_no,
                    durable_local_apply_verified=bool(durable_local_apply_verified),
                    local_apply_digest_sha256=digest[:64],
                    recovery_epoch=wm.recovery_epoch,
                    idempotency_key=str(idempotency_key or "").strip(),
                    notes="Blocked pull checkpoint: durable local SQLite apply not verified or invalid 64-char SHA-256 digest.",
                    actor=actor_user if getattr(actor_user, "is_authenticated", False) else None,
                )
            raise ValidationError(
                {
                    "error_code": "ERR_DURABLE_LOCAL_APPLY_REQUIRED",
                    "detail": "Pull cursor cannot advance unless durable_local_apply_verified=True and valid 64-char local_apply_digest_sha256 are supplied.",
                    "message_en": "Local SQLite apply must be verified before advancing sync cursor.",
                    "message_bn": "লোকাল SQLite আপ্লাই নিশ্চিত না হওয়া পর্যন্ত সিঙ্ক কার্সর অগ্রসর করা যাবে না।",
                }
            )

        with transaction.atomic():
            wm = cls._get_or_create_watermark_locked(tenant, reg)
            idem = str(idempotency_key or "").strip()

            if idem:
                prior = SyncCursorCheckpointLedger.objects.filter(
                    tenant=tenant,
                    device_registration=reg,
                    stream=SyncCheckpointStream.PULL_FEED,
                    idempotency_key=idem,
                    status=SyncCheckpointStatus.COMMITTED,
                ).first()
                if prior:
                    latest_seq = (
                        ChangeFeedEvent.objects.filter(tenant=tenant).aggregate(m=Max("sequence_no")).get("m") or 0
                    )
                    return {
                        "idempotent_replay": True,
                        "ledger_id": prior.ledger_id,
                        "watermark": cls._serialize_watermark(wm, latest_seq),
                        "message_en": "Pull checkpoint replay confirmed idempotently.",
                        "message_bn": "পুল চেকপয়েন্ট রিপ্লে নিরাপদে নিশ্চিত হয়েছে।",
                    }

            # Delegate sequence/token/regression validation to P307 ChangeFeedService
            cf_res = ChangeFeedService.checkpoint_cursor(
                tenant_id=str(tenant.tenant_id),
                device_id=reg.device_id,
                device_fingerprint_sha256=reg.device_fingerprint_sha256,
                acknowledged_sequence_no=ack_seq,
                cursor_token=cursor_token,
                applied_event_count=applied_event_count,
                actor_user=actor_user,
            )

            cursor_obj = DeviceChangeFeedCursor.objects.filter(device_registration=reg).first()
            prev_seq = wm.pull_applied_sequence_no
            is_replay = bool(cf_res.get("idempotent_replay")) and prev_seq == ack_seq

            if not is_replay:
                wm.pull_cursor = cursor_obj
                wm.pull_applied_sequence_no = ack_seq
                wm.pull_apply_digest_sha256 = digest
                wm.pull_checkpointed_at = timezone.now()
                wm.save(
                    update_fields=[
                        "pull_cursor",
                        "pull_applied_sequence_no",
                        "pull_apply_digest_sha256",
                        "pull_checkpointed_at",
                        "updated_at",
                    ]
                )

            entry = SyncCursorCheckpointLedger.objects.create(
                tenant=tenant,
                branch=reg.branch,
                device_registration=reg,
                watermark=wm,
                stream=SyncCheckpointStream.PULL_FEED,
                status=SyncCheckpointStatus.IDEMPOTENT_REPLAY if is_replay else SyncCheckpointStatus.COMMITTED,
                previous_sequence_no=prev_seq,
                new_sequence_no=wm.pull_applied_sequence_no,
                durable_local_apply_verified=True,
                local_apply_digest_sha256=digest,
                recovery_epoch=wm.recovery_epoch,
                idempotency_key=idem,
                notes=f"Pull watermark checkpointed after durable local apply ({applied_event_count} events).",
                actor=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            )

            return {
                "idempotent_replay": is_replay,
                "ledger_id": entry.ledger_id,
                "watermark": cls._serialize_watermark(wm, cf_res["latest_server_sequence"]),
                "message_en": f"Pull cursor durably checkpointed at sequence {wm.pull_applied_sequence_no}.",
                "message_bn": f"পুল কার্সর সিকোয়েন্স {wm.pull_applied_sequence_no}-এ নিরাপদে সংরক্ষিত হয়েছে।",
            }

    @classmethod
    @transaction.atomic
    def commit_push_ack_checkpoint(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        device_fingerprint_sha256: str,
        push_acked_sequence_no: int,
        ack_ids: List[str],
        idempotency_key: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Independently checkpoints client-persisted P306 canonical acknowledgments.
        NEVER alters pull_applied_sequence_no!
        """
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        reg = ChangeFeedService._assert_device(tenant, device_id, device_fingerprint_sha256, actor_user)

        try:
            push_seq = int(push_acked_sequence_no)
            if push_seq < 1:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError(
                {"error_code": "ERR_INVALID_PUSH_SEQUENCE", "detail": "push_acked_sequence_no must be >= 1."}
            )

        if not ack_ids or not isinstance(ack_ids, list):
            raise ValidationError({"error_code": "ERR_EMPTY_ACK_IDS", "detail": "ack_ids list is required."})

        wm = cls._get_or_create_watermark_locked(tenant, reg)
        idem = str(idempotency_key or "").strip()

        if idem:
            prior = SyncCursorCheckpointLedger.objects.filter(
                tenant=tenant,
                device_registration=reg,
                stream=SyncCheckpointStream.PUSH_ACK,
                idempotency_key=idem,
                status=SyncCheckpointStatus.COMMITTED,
            ).first()
            if prior:
                latest_seq = ChangeFeedEvent.objects.filter(tenant=tenant).aggregate(m=Max("sequence_no")).get("m") or 0
                return {
                    "idempotent_replay": True,
                    "ledger_id": prior.ledger_id,
                    "confirmed_ack_count": 0,
                    "watermark": cls._serialize_watermark(wm, latest_seq),
                }

        if push_seq < wm.push_acked_sequence_no:
            raise ValidationError(
                {
                    "error_code": "ERR_PUSH_WATERMARK_REGRESSION",
                    "detail": f"Cannot regress push_acked_sequence_no from {wm.push_acked_sequence_no} to {push_seq}.",
                }
            )

        # Verify all supplied ack_ids belong strictly to this tenant + device_registration
        cleaned_ids = [str(a).strip() for a in ack_ids if str(a).strip()]
        acks_qs = list(
            CanonicalCommandAcknowledgment.objects.select_for_update().filter(
                tenant=tenant,
                device_registration=reg,
                ack_id__in=cleaned_ids,
            )
        )
        if len(acks_qs) != len(set(cleaned_ids)):
            raise ValidationError(
                {
                    "error_code": "ERR_UNKNOWN_OR_FOREIGN_ACK_IDS",
                    "detail": "One or more ack_ids do not belong to this tenant/device.",
                }
            )

        # Only reconcile monetary total for newly confirmed acks (idempotent against duplicate replays!)
        newly_confirmed = [a for a in acks_qs if a.acknowledged_by_client_at is None]
        delta_money = sum((q2(a.monetary_total_bdt) for a in newly_confirmed), Decimal("0.00"))

        if newly_confirmed:
            CanonicalAckService.confirm_client_applied(
                tenant_id=str(tenant.tenant_id),
                ack_ids=[a.ack_id for a in newly_confirmed],
                actor_user=actor_user,
            )

        prev_push_seq = wm.push_acked_sequence_no
        is_replay = (push_seq == prev_push_seq) and (len(newly_confirmed) == 0)

        if not is_replay:
            wm.push_acked_sequence_no = push_seq
            wm.push_last_ack_id = acks_qs[-1].ack_id
            wm.push_total_acked_count += len(newly_confirmed)
            wm.push_monetary_reconciled_bdt = q2(wm.push_monetary_reconciled_bdt + delta_money)
            wm.push_checkpointed_at = timezone.now()
            wm.save(
                update_fields=[
                    "push_acked_sequence_no",
                    "push_last_ack_id",
                    "push_total_acked_count",
                    "push_monetary_reconciled_bdt",
                    "push_checkpointed_at",
                    "updated_at",
                ]
            )

        entry = SyncCursorCheckpointLedger.objects.create(
            tenant=tenant,
            branch=reg.branch,
            device_registration=reg,
            watermark=wm,
            stream=SyncCheckpointStream.PUSH_ACK,
            status=SyncCheckpointStatus.IDEMPOTENT_REPLAY if is_replay else SyncCheckpointStatus.COMMITTED,
            previous_sequence_no=prev_push_seq,
            new_sequence_no=wm.push_acked_sequence_no,
            durable_local_apply_verified=True,
            monetary_reconciled_bdt=q2(delta_money),
            recovery_epoch=wm.recovery_epoch,
            idempotency_key=idem,
            notes=f"Confirmed {len(newly_confirmed)} new canonical push ack(s); reconciled BDT {q2(delta_money)}.",
            actor=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        latest_seq = ChangeFeedEvent.objects.filter(tenant=tenant).aggregate(m=Max("sequence_no")).get("m") or 0
        return {
            "idempotent_replay": is_replay,
            "ledger_id": entry.ledger_id,
            "confirmed_ack_count": len(newly_confirmed),
            "delta_monetary_reconciled_bdt": str(q2(delta_money)),
            "watermark": cls._serialize_watermark(wm, latest_seq),
            "message_en": f"Independent push watermark updated to seq {wm.push_acked_sequence_no}.",
            "message_bn": f"স্বতন্ত্র পুশ অ্যাক ওয়াটারমার্ক সিকোয়েন্স {wm.push_acked_sequence_no}-এ আপডেট হয়েছে।",
        }

    @classmethod
    def recover_sync_state(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        device_fingerprint_sha256: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Crash-recovery handshake: returns independent pull watermark, independent push ack watermark,
        and any unconfirmed server-committed P306 canonical acks for immediate client reconciliation.
        """
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        reg = ChangeFeedService._assert_device(tenant, device_id, device_fingerprint_sha256, actor_user)

        with transaction.atomic():
            wm = cls._get_or_create_watermark_locked(tenant, reg)

        latest_seq = ChangeFeedEvent.objects.filter(tenant=tenant).aggregate(m=Max("sequence_no")).get("m") or 0
        unconfirmed = CanonicalCommandAcknowledgment.objects.filter(
            tenant=tenant,
            device_registration=reg,
            acknowledged_by_client_at__isnull=True,
        ).order_by("client_sequence")[:100]

        unconfirmed_list = [CanonicalAckService._serialize_ack(a, is_replay=True) for a in unconfirmed]
        return {
            "watermark": cls._serialize_watermark(wm, latest_seq),
            "unconfirmed_push_acks_count": len(unconfirmed_list),
            "unconfirmed_push_acks": unconfirmed_list,
            "resume_pull_from_sequence": wm.pull_applied_sequence_no,
            "resume_push_after_sequence": wm.push_acked_sequence_no,
            "message_en": f"Recovery ready: pull from seq {wm.pull_applied_sequence_no}, {len(unconfirmed_list)} unconfirmed push ack(s).",
            "message_bn": f"রিকভারি প্রস্তুত: পুল সিকোয়েন্স {wm.pull_applied_sequence_no}, অ-নিশ্চিত পুশ অ্যাক {len(unconfirmed_list)}টি।",
        }

    @classmethod
    @transaction.atomic
    def audited_reset_cursor(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        target_pull_sequence_no: int,
        reason: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Owner/Manager-only audited pull-cursor rewind (e.g. after corrupted local cache rebuild).
        Increments recovery_epoch and preserves independent push_acked_sequence_no intact!
        """
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)

        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in RESET_ALLOWED_ROLES:
            raise PermissionDenied(
                {"error_code": "ERR_RESET_FORBIDDEN", "detail": "Only Pharmacy Owner or Branch Manager may rewind a sync cursor."}
            )

        clean_reason = str(reason or "").strip()
        if len(clean_reason) < 5:
            raise ValidationError(
                {"error_code": "ERR_REASON_REQUIRED", "detail": "Audited cursor rewind requires a reason (>= 5 characters)."}
            )

        try:
            target_seq = int(target_pull_sequence_no)
            if target_seq < 0:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError(
                {"error_code": "ERR_INVALID_TARGET_SEQUENCE", "detail": "target_pull_sequence_no must be >= 0."}
            )

        from apps.core.models import DeviceLocalDbRegistration

        reg = DeviceLocalDbRegistration.objects.select_related("branch").filter(tenant=tenant, device_id=device_id).first()
        if not reg:
            raise PermissionDenied({"error_code": "ERR_DEVICE_NOT_FOUND_OR_CROSS_TENANT", "detail": "Device not found in tenant."})

        if not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            if user_branch and str(reg.branch_id) != user_branch:
                raise PermissionDenied({"error_code": "ERR_BRANCH_SCOPE_DENIED", "detail": "Cannot reset another branch's device."})

        wm = cls._get_or_create_watermark_locked(tenant, reg)
        prev_pull = wm.pull_applied_sequence_no
        wm.pull_applied_sequence_no = target_seq
        wm.pull_apply_digest_sha256 = ""
        wm.recovery_epoch += 1
        wm.save(update_fields=["pull_applied_sequence_no", "pull_apply_digest_sha256", "recovery_epoch", "updated_at"])

        DeviceChangeFeedCursor.objects.filter(device_registration=reg).update(last_sequence_no=target_seq)

        entry = SyncCursorCheckpointLedger.objects.create(
            tenant=tenant,
            branch=reg.branch,
            device_registration=reg,
            watermark=wm,
            stream=SyncCheckpointStream.AUDITED_RESET,
            status=SyncCheckpointStatus.AUDITED_REWIND,
            previous_sequence_no=prev_pull,
            new_sequence_no=target_seq,
            durable_local_apply_verified=False,
            recovery_epoch=wm.recovery_epoch,
            notes=f"Audited pull rewind: {clean_reason}",
            actor=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        latest_seq = ChangeFeedEvent.objects.filter(tenant=tenant).aggregate(m=Max("sequence_no")).get("m") or 0
        return {
            "ledger_id": entry.ledger_id,
            "watermark": cls._serialize_watermark(wm, latest_seq),
            "message_en": f"Pull cursor rewound from {prev_pull} to {target_seq} (epoch {wm.recovery_epoch}); push ack watermark preserved.",
            "message_bn": f"পুল কার্সর {prev_pull} থেকে {target_seq}-এ রিসেট হয়েছে (epoch {wm.recovery_epoch}); পুশ অ্যাক অক্ষত আছে।",
        }

    @classmethod
    def list_watermarks_and_ledger(cls, *, tenant_id: str, actor_user: Any = None) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)

        latest_seq = ChangeFeedEvent.objects.filter(tenant=tenant).aggregate(m=Max("sequence_no")).get("m") or 0
        wm_qs = DeviceSyncWatermark.objects.filter(tenant=tenant).select_related("device_registration", "branch")
        led_qs = SyncCursorCheckpointLedger.objects.filter(tenant=tenant).select_related("device_registration")

        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            wm_qs = wm_qs.filter(branch_id=user_branch)
            led_qs = led_qs.filter(branch_id=user_branch)

        return {
            "tenant_id": str(tenant.tenant_id),
            "latest_server_sequence": int(latest_seq),
            "watermarks": [cls._serialize_watermark(w, latest_seq) for w in wm_qs[:50]],
            "recent_ledger": [
                {
                    "ledger_id": e.ledger_id,
                    "device_id": e.device_registration.device_id,
                    "stream": e.stream,
                    "status": e.status,
                    "previous_sequence_no": e.previous_sequence_no,
                    "new_sequence_no": e.new_sequence_no,
                    "durable_local_apply_verified": e.durable_local_apply_verified,
                    "monetary_reconciled_bdt": str(q2(e.monetary_reconciled_bdt)),
                    "recovery_epoch": e.recovery_epoch,
                    "notes": e.notes,
                    "created_at": e.created_at.isoformat(),
                }
                for e in led_qs[:50]
            ],
        }
