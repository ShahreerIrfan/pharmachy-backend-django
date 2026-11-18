"""
backend/apps/core/canonical_ack_service.py
Architecture Decision: ADR-0306 (P306).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Authoritative Canonical Acknowledgment Service:
- Transactionally commits cloud commands via P304 DurableOutboxService and issues stable
  server-assigned canonical entity IDs, structured error codes, and SHA-256 ack signatures.
- Guarantees replay-safe acknowledgements: if a network drop prevents the client from receiving
  the original HTTP 200, re-submitting or looking up by idempotency_key returns the EXACT same
  ack_id, canonical_entity_id, and ack_signature_sha256 while incrementing replay_count (zero duplicate effects).
- Supports client confirmation (`confirm_client_applied_acks`) once SQLite has durably reconciled.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.double_entry_journal_service import q2
from apps.core.durable_outbox_service import DurableOutboxService
from apps.core.models import (
    CanonicalAckDisposition,
    CanonicalCommandAcknowledgment,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    OfflineCommandSyncStatus,
    OfflineOutboxCommandReceipt,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class CanonicalAckService:
    """
    Transactional commit & replay-safe canonical acknowledgment engine.
    """

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
                    "detail": "Tenant subscription is suspended. Canonical acknowledgments blocked.",
                    "message_en": "Tenant subscription is suspended.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে।",
                }
            )
        return tenant

    @classmethod
    def _compute_ack_signature(
        cls,
        tenant_id: str,
        device_id: str,
        idempotency_key: str,
        canonical_entity_id: str,
        disposition: str,
        monetary_total_bdt: Decimal,
    ) -> str:
        raw = f"{tenant_id}|{device_id}|{idempotency_key}|{canonical_entity_id}|{disposition}|{q2(monetary_total_bdt)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def _serialize_ack(cls, ack: CanonicalCommandAcknowledgment, is_replay: bool = False) -> Dict[str, Any]:
        return {
            "ack_id": ack.ack_id,
            "tenant_id": str(ack.tenant_id),
            "branch_id": str(ack.branch_id),
            "device_id": ack.device_registration.device_id,
            "command_id": ack.command_id,
            "idempotency_key": ack.idempotency_key,
            "client_sequence": ack.client_sequence,
            "aggregate_type": ack.aggregate_type,
            "local_aggregate_ref": ack.local_aggregate_ref,
            "canonical_entity_id": ack.canonical_entity_id,
            "disposition": CanonicalAckDisposition.REPLAY_IDEMPOTENT if is_replay else ack.disposition,
            "persisted_disposition": ack.disposition,
            "error_code": ack.error_code,
            "error_detail": ack.error_detail,
            "monetary_total_bdt": str(q2(ack.monetary_total_bdt)),
            "ack_signature_sha256": ack.ack_signature_sha256,
            "replay_count": ack.replay_count,
            "acknowledged_by_client_at": ack.acknowledged_by_client_at.isoformat() if ack.acknowledged_by_client_at else None,
            "created_at": ack.created_at.isoformat(),
        }

    @classmethod
    def _assert_device(
        cls, tenant: Tenant, device_id: str, device_fingerprint_sha256: str, actor_user: Any
    ) -> DeviceLocalDbRegistration:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        reg = DeviceLocalDbRegistration.objects.select_related("branch").filter(tenant=tenant, device_id=device_id).first()
        if not reg:
            foreign = DeviceLocalDbRegistration.objects.filter(device_id=device_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant.tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant ack commit forbidden."})
            raise NotFound({"error_code": "ERR_DEVICE_NOT_REGISTERED", "detail": f"Device '{device_id}' not registered."})
        if reg.status != DeviceLocalDbStatus.ACTIVE:
            raise PermissionDenied({"error_code": "ERR_DEVICE_REVOKED", "detail": "Revoked device."})
        if not reg.lease_expires_at or reg.lease_expires_at <= timezone.now():
            raise PermissionDenied({"error_code": "ERR_OFFLINE_LEASE_EXPIRED", "detail": "Offline lease expired."})
        if str(device_fingerprint_sha256 or "").strip().lower() != reg.device_fingerprint_sha256:
            raise PermissionDenied({"error_code": "ERR_DEVICE_FINGERPRINT_MISMATCH", "detail": "Device fingerprint mismatch."})

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
                raise PermissionDenied({"error_code": "ERR_BRANCH_SCOPE_DENIED", "detail": "Branch scope denied."})
        return reg

    @classmethod
    @transaction.atomic
    def commit_and_acknowledge(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        device_fingerprint_sha256: str,
        commands: List[Dict[str, Any]],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        reg = cls._assert_device(tenant, device_id, device_fingerprint_sha256, actor_user)

        if not commands or not isinstance(commands, list):
            raise ValidationError({"error_code": "ERR_EMPTY_COMMANDS", "detail": "At least one command is required."})

        acks: List[Dict[str, Any]] = []
        committed_count = 0
        replay_count = 0
        conflict_count = 0

        for cmd in commands:
            idem_key = str(cmd.get("idempotency_key") or "").strip()
            cmd_id = str(cmd.get("command_id") or "").strip()
            agg_ref = str(cmd.get("aggregate_ref") or "").strip()
            agg_type = str(cmd.get("aggregate_type") or "SALE_INVOICE").strip().upper()
            if not idem_key or not cmd_id or not agg_ref:
                raise ValidationError(
                    {"error_code": "ERR_INCOMPLETE_COMMAND", "detail": "command_id, idempotency_key, and aggregate_ref are required."}
                )

            # 1. Replay-Safe Lookup: If already acknowledged, increment replay_count and return EXACT canonical ack
            existing_ack = CanonicalCommandAcknowledgment.objects.select_for_update().select_related(
                "device_registration"
            ).filter(tenant=tenant, idempotency_key=idem_key).first()
            if existing_ack:
                existing_ack.replay_count += 1
                existing_ack.save(update_fields=["replay_count", "updated_at"])
                replay_count += 1
                acks.append(cls._serialize_ack(existing_ack, is_replay=True))
                continue

            # 2. Transactional commit via P304 DurableOutboxService inside per-command savepoint
            with transaction.atomic():
                ingest = DurableOutboxService.ingest_outbox_batch(
                    tenant_id=str(tenant.tenant_id),
                    device_id=reg.device_id,
                    device_fingerprint_sha256=reg.device_fingerprint_sha256,
                    commands=[cmd],
                    actor_user=actor_user,
                )
                r = ingest["results"][0]
                receipt = OfflineOutboxCommandReceipt.objects.filter(receipt_id=r.get("receipt_id")).first()
                amt = q2(Decimal(str(cmd.get("monetary_total_bdt", "0.00"))))
                seq = int(cmd.get("client_sequence", 1))

                if r["outcome"] in (OfflineCommandSyncStatus.ACCEPTED, OfflineCommandSyncStatus.DUPLICATE_REPLAY):
                    disposition = CanonicalAckDisposition.COMMITTED
                    canonical_id = r.get("server_reference_id") or f"CAN-{uuid.uuid4().hex[:10].upper()}"
                    err_code = ""
                    err_detail = ""
                    committed_count += 1
                else:
                    disposition = CanonicalAckDisposition.TERMINAL_CONFLICT
                    canonical_id = ""
                    err_code = "ERR_SYNC_CONFLICT"
                    err_detail = r.get("conflict_reason", "Conflict requires manager review.")
                    conflict_count += 1

                sig = cls._compute_ack_signature(
                    str(tenant.tenant_id), reg.device_id, idem_key, canonical_id, disposition, amt
                )
                ack = CanonicalCommandAcknowledgment.objects.create(
                    tenant=tenant,
                    branch=reg.branch,
                    device_registration=reg,
                    outbox_receipt=receipt,
                    command_id=cmd_id,
                    idempotency_key=idem_key,
                    client_sequence=seq,
                    aggregate_type=agg_type,
                    local_aggregate_ref=agg_ref,
                    canonical_entity_id=canonical_id,
                    disposition=disposition,
                    error_code=err_code,
                    error_detail=err_detail,
                    monetary_total_bdt=amt,
                    ack_signature_sha256=sig,
                    replay_count=0,
                )
                acks.append(cls._serialize_ack(ack, is_replay=False))

        return {
            "tenant_id": str(tenant.tenant_id),
            "device_id": reg.device_id,
            "committed_count": committed_count,
            "replay_idempotent_count": replay_count,
            "conflict_count": conflict_count,
            "acknowledgments": acks,
            "message_en": f"Processed {len(acks)} commands ({committed_count} committed, {replay_count} idempotent replays, {conflict_count} conflicts).",
            "message_bn": f"{len(acks)}টি কমান্ড প্রসেস হয়েছে ({committed_count}টি কমিটেড, {replay_count}টি রিপ্লে, {conflict_count}টি কনফ্লিক্ট)।",
        }

    @classmethod
    def lookup_by_idempotency_keys(
        cls, *, tenant_id: str, idempotency_keys: List[str], actor_user: Any = None
    ) -> Dict[str, Any]:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        tenant = cls.assert_tenant_active(tenant_id)
        if not idempotency_keys or not isinstance(idempotency_keys, list):
            raise ValidationError({"error_code": "ERR_EMPTY_KEYS", "detail": "idempotency_keys list required."})

        qs = CanonicalCommandAcknowledgment.objects.filter(
            tenant=tenant, idempotency_key__in=[str(k).strip() for k in idempotency_keys[:100]]
        ).select_related("device_registration")
        found = [cls._serialize_ack(a, is_replay=True) for a in qs]
        found_keys = {a["idempotency_key"] for a in found}
        missing = [k for k in idempotency_keys if k not in found_keys]
        return {"tenant_id": str(tenant.tenant_id), "found_count": len(found), "missing_keys": missing, "acknowledgments": found}

    @classmethod
    @transaction.atomic
    def confirm_client_applied(
        cls, *, tenant_id: str, ack_ids: List[str], actor_user: Any = None
    ) -> Dict[str, Any]:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        tenant = cls.assert_tenant_active(tenant_id)
        if not ack_ids or not isinstance(ack_ids, list):
            raise ValidationError({"error_code": "ERR_EMPTY_ACK_IDS", "detail": "ack_ids list required."})

        updated = CanonicalCommandAcknowledgment.objects.filter(
            tenant=tenant, ack_id__in=ack_ids
        ).update(acknowledged_by_client_at=timezone.now())
        return {"success": True, "confirmed_count": updated}

    @classmethod
    def list_acknowledgments(
        cls, *, tenant_id: str, disposition: Optional[str] = None, actor_user: Any = None
    ) -> Dict[str, Any]:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        tenant = cls.assert_tenant_active(tenant_id)
        qs = CanonicalCommandAcknowledgment.objects.filter(tenant=tenant).select_related("device_registration")
        if disposition:
            qs = qs.filter(disposition=disposition.upper().strip())
        if getattr(actor_user, "role", None) in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
            qs = qs.filter(branch_id=str(getattr(actor_user, "branch_id", "")))
        items = [cls._serialize_ack(a, is_replay=False) for a in qs[:100]]
        return {"tenant_id": str(tenant.tenant_id), "count": len(items), "acknowledgments": items}
