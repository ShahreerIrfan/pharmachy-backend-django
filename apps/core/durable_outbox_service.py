"""
backend/apps/core/durable_outbox_service.py
Architecture Decision: ADR-0304 (P304).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Server-authoritative durable outbox ingestion & conflict queue service:
- Replays pending JavaFX SQLite outbox commands strictly in monotonic client_sequence order.
- Guarantees exact idempotency via UNIQUE (tenant, idempotency_key): repeated deliveries return
  DUPLICATE_REPLAY with original server_reference_id and NEVER double-deduct stock or double-post revenue.
- Detects payload tampering on duplicate idempotency key, out-of-order sequences, and two-device
  stock conflicts (routing them safely to CONFLICT_NEEDS_REVIEW rather than silently dropping invoices).
- Provides audited manager conflict resolution workflow.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.double_entry_journal_service import q2
from apps.core.models import (
    BatchStock,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    OfflineCommandSyncStatus,
    OfflineOutboxCommandReceipt,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

OUTBOX_RESOLVE_ROLES = {
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


class DurableOutboxService:
    """
    Authoritative server ingestion and conflict-management service for the P304 durable outbox.
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
            raise PermissionDenied({"error_code": "ERR_TENANT_SUSPENDED", "detail": "Tenant subscription is suspended."})
        return tenant

    @classmethod
    def _payload_hash(cls, payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _serialize_receipt(cls, rec: OfflineOutboxCommandReceipt) -> Dict[str, Any]:
        return {
            "receipt_id": rec.receipt_id,
            "tenant_id": str(rec.tenant_id),
            "branch_id": str(rec.branch_id),
            "device_id": rec.device_registration.device_id,
            "command_id": rec.command_id,
            "idempotency_key": rec.idempotency_key,
            "client_sequence": rec.client_sequence,
            "command_type": rec.command_type,
            "aggregate_ref": rec.aggregate_ref,
            "monetary_total_bdt": str(q2(rec.monetary_total_bdt)),
            "sync_status": rec.sync_status,
            "server_reference_id": rec.server_reference_id,
            "conflict_reason": rec.conflict_reason,
            "resolution_notes": rec.resolution_notes,
            "created_at": rec.created_at.isoformat(),
        }

    @classmethod
    @transaction.atomic
    def ingest_outbox_batch(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        device_fingerprint_sha256: str,
        commands: List[Dict[str, Any]],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        tenant = cls.assert_tenant_active(tenant_id)

        reg = DeviceLocalDbRegistration.objects.select_related("branch").filter(tenant=tenant, device_id=device_id).first()
        if not reg:
            foreign = DeviceLocalDbRegistration.objects.filter(device_id=device_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant outbox replay forbidden."})
            raise NotFound({"error_code": "ERR_DEVICE_NOT_REGISTERED", "detail": f"Device '{device_id}' not registered."})

        if reg.status != DeviceLocalDbStatus.ACTIVE:
            raise PermissionDenied({"error_code": "ERR_DEVICE_REVOKED", "detail": "Revoked device cannot replay outbox."})
        if not reg.lease_expires_at or reg.lease_expires_at <= timezone.now():
            raise PermissionDenied({"error_code": "ERR_OFFLINE_LEASE_EXPIRED", "detail": "Offline lease expired."})
        if str(device_fingerprint_sha256 or "").strip().lower() != reg.device_fingerprint_sha256:
            raise PermissionDenied({"error_code": "ERR_DEVICE_FINGERPRINT_MISMATCH", "detail": "Fingerprint mismatch."})

        if not commands or not isinstance(commands, list):
            raise ValidationError({"error_code": "ERR_EMPTY_OUTBOX_BATCH", "detail": "commands list must be non-empty."})

        # Strictly sort incoming commands by client_sequence ascending
        try:
            ordered = sorted(commands, key=lambda c: int(c.get("client_sequence", 0)))
        except Exception:
            raise ValidationError({"error_code": "ERR_INVALID_SEQUENCE", "detail": "Every command must have integer client_sequence >= 1."})

        results: List[Dict[str, Any]] = []
        accepted_count = 0
        duplicate_count = 0
        conflict_count = 0

        for cmd in ordered:
            cmd_id = str(cmd.get("command_id") or "").strip()
            idem_key = str(cmd.get("idempotency_key") or "").strip()
            cmd_type = str(cmd.get("command_type") or "").strip().upper()
            agg_ref = str(cmd.get("aggregate_ref") or "").strip()
            payload = cmd.get("payload") if isinstance(cmd.get("payload"), dict) else {}

            if not cmd_id or not idem_key or not cmd_type or not agg_ref:
                raise ValidationError(
                    {"error_code": "ERR_INCOMPLETE_OUTBOX_COMMAND", "detail": "command_id, idempotency_key, command_type, aggregate_ref are required."}
                )

            try:
                seq = int(cmd.get("client_sequence"))
                if seq < 1:
                    raise ValueError()
                amt = q2(Decimal(str(cmd.get("monetary_total_bdt", "0.00"))))
                if amt < Decimal("0.00"):
                    raise ValueError()
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_COMMAND_VALUES", "detail": "Invalid client_sequence or monetary_total_bdt."})

            p_hash = cls._payload_hash(payload)

            # 1. Idempotency check (repeated delivery safety)
            existing = OfflineOutboxCommandReceipt.objects.filter(tenant=tenant, idempotency_key=idem_key).first()
            if existing:
                if existing.payload_sha256 == p_hash:
                    duplicate_count += 1
                    results.append(
                        {
                            "command_id": cmd_id,
                            "idempotency_key": idem_key,
                            "client_sequence": seq,
                            "outcome": OfflineCommandSyncStatus.DUPLICATE_REPLAY,
                            "receipt_id": existing.receipt_id,
                            "server_reference_id": existing.server_reference_id,
                        }
                    )
                    continue
                else:
                    conflict_count += 1
                    results.append(
                        {
                            "command_id": cmd_id,
                            "idempotency_key": idem_key,
                            "client_sequence": seq,
                            "outcome": OfflineCommandSyncStatus.CONFLICT_NEEDS_REVIEW,
                            "conflict_reason": "ERR_IDEMPOTENCY_PAYLOAD_MISMATCH: Payload changed for existing idempotency_key.",
                        }
                    )
                    continue

            # 2. Sequence collision check on same device
            if OfflineOutboxCommandReceipt.objects.filter(device_registration=reg, client_sequence=seq).exists():
                conflict_count += 1
                results.append(
                    {
                        "command_id": cmd_id,
                        "idempotency_key": idem_key,
                        "client_sequence": seq,
                        "outcome": OfflineCommandSyncStatus.CONFLICT_NEEDS_REVIEW,
                        "conflict_reason": f"ERR_CLIENT_SEQUENCE_CONFLICT: Sequence {seq} already consumed by device {reg.device_id}.",
                    }
                )
                continue

            # 3. Real batch stock check for two-device race detection
            sync_status = OfflineCommandSyncStatus.ACCEPTED
            conflict_reason = ""
            server_ref = f"SRV-{uuid.uuid4().hex[:10].upper()}"

            batch_id = payload.get("batch_id")
            qty_raw = payload.get("quantity_base_units")
            if cmd_type == "OFFLINE_POS_SALE" and batch_id and qty_raw is not None:
                req_qty = int(qty_raw)
                batch = BatchStock.objects.select_for_update().filter(
                    tenant=tenant, branch=reg.branch, batch_id=str(batch_id)
                ).first()
                if not batch or batch.quantity_base_units < req_qty:
                    sync_status = OfflineCommandSyncStatus.CONFLICT_NEEDS_REVIEW
                    avail = batch.quantity_base_units if batch else 0
                    conflict_reason = (
                        f"ERR_TWO_DEVICE_STOCK_CONFLICT: Batch '{batch_id}' has {avail} units remaining on server, "
                        f"cannot fulfill offline sale of {req_qty} units."
                    )
                    server_ref = ""
                else:
                    batch.quantity_base_units -= req_qty
                    batch.save()

            rec = OfflineOutboxCommandReceipt.objects.create(
                tenant=tenant,
                branch=reg.branch,
                device_registration=reg,
                command_id=cmd_id,
                idempotency_key=idem_key,
                client_sequence=seq,
                command_type=cmd_type,
                aggregate_ref=agg_ref,
                monetary_total_bdt=amt,
                payload_json=payload,
                payload_sha256=p_hash,
                sync_status=sync_status,
                server_reference_id=server_ref,
                conflict_reason=conflict_reason,
                submitted_by=actor_user,
            )

            if sync_status == OfflineCommandSyncStatus.ACCEPTED:
                accepted_count += 1
            else:
                conflict_count += 1

            results.append(
                {
                    "command_id": cmd_id,
                    "idempotency_key": idem_key,
                    "client_sequence": seq,
                    "outcome": sync_status,
                    "receipt_id": rec.receipt_id,
                    "server_reference_id": server_ref,
                    "conflict_reason": conflict_reason,
                }
            )

        return {
            "tenant_id": str(tenant.tenant_id),
            "device_id": reg.device_id,
            "processed_count": len(results),
            "accepted_count": accepted_count,
            "duplicate_replay_count": duplicate_count,
            "conflict_count": conflict_count,
            "results": results,
        }

    @classmethod
    def list_receipts(
        cls, *, tenant_id: str, sync_status: Optional[str] = None, branch_id: Optional[str] = None, actor_user: Any = None
    ) -> Dict[str, Any]:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        tenant = cls.assert_tenant_active(tenant_id)
        qs = OfflineOutboxCommandReceipt.objects.filter(tenant=tenant).select_related("device_registration")
        if sync_status:
            qs = qs.filter(sync_status=sync_status.upper().strip())
        if getattr(actor_user, "role", None) in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
            qs = qs.filter(branch_id=str(getattr(actor_user, "branch_id", "")))
        elif branch_id:
            qs = qs.filter(branch_id=branch_id)
        items = [cls._serialize_receipt(r) for r in qs[:100]]
        return {"tenant_id": str(tenant.tenant_id), "count": len(items), "receipts": items}

    @classmethod
    @transaction.atomic
    def resolve_conflict(
        cls,
        *,
        tenant_id: str,
        receipt_id: str,
        resolution_action: str,
        notes: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if not getattr(actor_user, "is_superuser", False) and getattr(actor_user, "role", None) not in OUTBOX_RESOLVE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CONFLICT_RESOLVE_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner, Finance Officer, or Branch Manager can resolve offline sync conflicts.",
                }
            )
        tenant = cls.assert_tenant_active(tenant_id)
        if not str(notes or "").strip():
            raise ValidationError({"error_code": "ERR_MISSING_NOTES", "detail": "Resolution notes are mandatory."})

        rec = OfflineOutboxCommandReceipt.objects.select_for_update().select_related("device_registration").filter(
            tenant=tenant, receipt_id=receipt_id
        ).first()
        if not rec:
            foreign = OfflineOutboxCommandReceipt.objects.filter(receipt_id=receipt_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant conflict resolution forbidden."})
            raise NotFound({"error_code": "ERR_RECEIPT_NOT_FOUND", "detail": f"Receipt '{receipt_id}' not found."})

        if rec.sync_status != OfflineCommandSyncStatus.CONFLICT_NEEDS_REVIEW:
            raise ValidationError({"error_code": "ERR_NOT_IN_CONFLICT", "detail": "Receipt is not awaiting conflict review."})

        action = str(resolution_action or "").upper().strip()
        if action == "ACCEPT_ADJUSTED":
            rec.sync_status = OfflineCommandSyncStatus.ACCEPTED
            rec.server_reference_id = f"SRV-ADJ-{uuid.uuid4().hex[:8].upper()}"
        elif action == "REJECT":
            rec.sync_status = OfflineCommandSyncStatus.REJECTED
        else:
            raise ValidationError({"error_code": "ERR_INVALID_RESOLUTION_ACTION", "detail": "Use ACCEPT_ADJUSTED or REJECT."})

        rec.resolution_notes = notes.strip()
        rec.resolved_by = actor_user
        rec.resolved_at = timezone.now()
        rec.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="OFFLINE_OUTBOX_CONFLICT_RESOLVED",
            resource_type="OfflineOutboxCommandReceipt",
            resource_id=rec.receipt_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"action": action, "notes": notes.strip()},
        )
        return cls._serialize_receipt(rec)
