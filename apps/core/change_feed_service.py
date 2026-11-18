"""
backend/apps/core/change_feed_service.py
Architecture Decision: ADR-0307 (P307).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Authoritative Change Feed Pull Service:
- Maintains a strictly monotonic per-tenant sequence counter protected via row locking (`select_for_update`).
- Emits append-only UPSERT, TOMBSTONE (soft-delete / recall / deactivation), and REVERSAL change events with
  exact Decimal-serialized monetary figures and deterministic SHA-256 payload digests.
- Serves bounded incremental pulls (`limit <= 100`) filtered strictly by tenant, branch scope
  (tenant-wide events where branch=None + device's own branch events), optional entity_types, and
  optional self-device echo exclusion.
- Issues tamper-evident cursor tokens and enforces monotonic, replay-safe device cursor checkpoints
  (`DeviceChangeFeedCursor`) for crash-safe resumption.
"""

from __future__ import annotations

import hashlib
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.db import models, transaction
from django.db.models import Max, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.double_entry_journal_service import q2
from apps.core.models import (
    Branch,
    ChangeFeedEntityType,
    ChangeFeedEvent,
    ChangeFeedOperation,
    DeviceChangeFeedCursor,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

MAX_PULL_BATCH_SIZE = 100
DEFAULT_PULL_BATCH_SIZE = 50

PUBLISH_ALLOWED_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "PHARMACIST",
    "LICENSED_PHARMACIST",
    "FINANCE_OFFICER",
    "OWNER_ADMIN",
    "ADMIN",
}

TENANT_WIDE_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.FINANCE_OFFICER,
    "PHARMACY_OWNER",
    "FINANCE_OFFICER",
    "OWNER_ADMIN",
    "ADMIN",
}


class ChangeFeedService:
    """
    Incremental cloud-to-POS change feed engine with strict tenant/branch isolation,
    monotonic sequence cursors, tombstones, and audited reversals.
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
                    "detail": "Tenant subscription is suspended. Change feed sync blocked.",
                    "message_en": "Tenant subscription is suspended.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে।",
                }
            )
        return tenant

    @classmethod
    def _compute_payload_sha256(cls, payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _compute_cursor_token(cls, tenant_id: str, device_id: str, sequence_no: int) -> str:
        raw = f"cf-cursor|{tenant_id}|{device_id}|{sequence_no}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        return f"seq:{sequence_no}:{digest}"

    @classmethod
    def _serialize_event(cls, ev: ChangeFeedEvent) -> Dict[str, Any]:
        return {
            "event_id": ev.event_id,
            "tenant_id": str(ev.tenant_id),
            "branch_id": str(ev.branch_id) if ev.branch_id else None,
            "sequence_no": ev.sequence_no,
            "entity_type": ev.entity_type,
            "entity_id": ev.entity_id,
            "operation": ev.operation,
            "entity_version": ev.entity_version,
            "payload_json": ev.payload_json,
            "payload_sha256": ev.payload_sha256,
            "monetary_impact_bdt": str(q2(ev.monetary_impact_bdt)),
            "source_device_id": ev.source_device_id,
            "idempotency_key": ev.idempotency_key,
            "created_at": ev.created_at.isoformat(),
        }

    @classmethod
    def _assert_authenticated_tenant_user(cls, tenant: Tenant, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return
        user_tenant = str(getattr(actor_user, "tenant_id", "") or "")
        if user_tenant and user_tenant != str(tenant.tenant_id):
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "detail": "Cross-tenant change feed access is strictly forbidden.",
                    "message_en": "Access denied for another pharmacy tenant.",
                    "message_bn": "অন্য ফার্মেসির ডেটায় অ্যাক্সেস অনুমোদিত নয়।",
                }
            )

    @classmethod
    def _assert_device(
        cls,
        tenant: Tenant,
        device_id: str,
        device_fingerprint_sha256: str,
        actor_user: Any,
    ) -> DeviceLocalDbRegistration:
        cls._assert_authenticated_tenant_user(tenant, actor_user)
        if not device_id or not str(device_id).strip():
            raise ValidationError({"error_code": "ERR_DEVICE_ID_REQUIRED", "detail": "device_id is required."})

        reg = (
            DeviceLocalDbRegistration.objects.select_related("branch")
            .filter(tenant=tenant, device_id=str(device_id).strip())
            .first()
        )
        if not reg:
            foreign = DeviceLocalDbRegistration.objects.filter(device_id=str(device_id).strip()).first()
            if foreign and str(foreign.tenant_id) != str(tenant.tenant_id):
                raise PermissionDenied(
                    {"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant device pull forbidden."}
                )
            raise NotFound({"error_code": "ERR_DEVICE_NOT_REGISTERED", "detail": f"Device '{device_id}' not registered."})

        if reg.status != DeviceLocalDbStatus.ACTIVE:
            raise PermissionDenied({"error_code": "ERR_DEVICE_REVOKED", "detail": "Device registration is revoked."})
        if not reg.lease_expires_at or reg.lease_expires_at <= timezone.now():
            raise PermissionDenied({"error_code": "ERR_OFFLINE_LEASE_EXPIRED", "detail": "Offline device lease expired."})
        if str(device_fingerprint_sha256 or "").strip().lower() != reg.device_fingerprint_sha256:
            raise PermissionDenied(
                {"error_code": "ERR_DEVICE_FINGERPRINT_MISMATCH", "detail": "Device fingerprint mismatch."}
            )

        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            if user_branch and str(reg.branch_id) != user_branch:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_BRANCH_SCOPE_DENIED",
                        "detail": "User cannot pull change feed for a device outside their assigned branch.",
                    }
                )
        return reg

    @classmethod
    @transaction.atomic
    def publish_change_event(
        cls,
        *,
        tenant_id: str,
        entity_type: str,
        entity_id: str,
        operation: str = ChangeFeedOperation.UPSERT,
        payload_json: Optional[Dict[str, Any]] = None,
        branch_id: Optional[str] = None,
        entity_version: int = 1,
        monetary_impact_bdt: Any = "0.00",
        source_device_id: str = "",
        idempotency_key: str = "",
        actor_user: Any = None,
        system_internal: bool = False,
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)

        if not system_internal:
            cls._assert_authenticated_tenant_user(tenant, actor_user)
            role = getattr(actor_user, "role", None)
            if not getattr(actor_user, "is_superuser", False) and role not in PUBLISH_ALLOWED_ROLES:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PUBLISH_FORBIDDEN",
                        "detail": "Only Pharmacy Owner, Manager, Pharmacist, or Finance Officer may publish change events.",
                    }
                )

        ent_type = str(entity_type or "").strip().upper()
        if ent_type not in ChangeFeedEntityType.values:
            raise ValidationError(
                {"error_code": "ERR_INVALID_ENTITY_TYPE", "detail": f"Unsupported entity_type '{entity_type}'."}
            )

        op = str(operation or "").strip().upper()
        if op not in ChangeFeedOperation.values:
            raise ValidationError(
                {"error_code": "ERR_INVALID_OPERATION", "detail": f"Unsupported operation '{operation}'."}
            )

        ent_id = str(entity_id or "").strip()
        if not ent_id:
            raise ValidationError({"error_code": "ERR_ENTITY_ID_REQUIRED", "detail": "entity_id is required."})

        try:
            ver = int(entity_version)
            if ver < 1:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_VERSION", "detail": "entity_version must be >= 1."})

        try:
            amt = q2(Decimal(str(monetary_impact_bdt if monetary_impact_bdt is not None else "0.00")))
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError(
                {"error_code": "ERR_INVALID_MONETARY_AMOUNT", "detail": "monetary_impact_bdt must be a valid decimal."}
            )

        branch_obj: Optional[Branch] = None
        if branch_id:
            branch_obj = Branch.objects.filter(branch_id=str(branch_id).strip(), tenant=tenant).first()
            if not branch_obj:
                foreign_branch = Branch.objects.filter(branch_id=str(branch_id).strip()).first()
                if foreign_branch:
                    raise PermissionDenied(
                        {"error_code": "ERR_CROSS_TENANT_BRANCH", "detail": "Branch belongs to another tenant."}
                    )
                raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        if not system_internal and branch_obj and not getattr(actor_user, "is_superuser", False):
            role = getattr(actor_user, "role", None)
            if role not in TENANT_WIDE_ROLES:
                user_branch = str(getattr(actor_user, "branch_id", "") or "")
                if user_branch and str(branch_obj.branch_id) != user_branch:
                    raise PermissionDenied(
                        {"error_code": "ERR_BRANCH_SCOPE_DENIED", "detail": "Cannot publish event to another branch."}
                    )

        payload = dict(payload_json or {})
        if op == ChangeFeedOperation.TOMBSTONE:
            payload.setdefault("tombstoned", True)
            payload.setdefault("tombstone_reason", "SOFT_DELETED_OR_RECALLED")
            payload.setdefault("deleted_at", timezone.now().isoformat())
        elif op == ChangeFeedOperation.REVERSAL:
            if not payload.get("reversal_reason") and not payload.get("reversed_entity_id"):
                raise ValidationError(
                    {
                        "error_code": "ERR_REVERSAL_REASON_REQUIRED",
                        "detail": "REVERSAL events require reversal_reason or reversed_entity_id in payload_json.",
                    }
                )

        # Serialize per-tenant sequence allocation via row lock on Tenant
        locked_tenant = Tenant.objects.select_for_update().get(tenant_id=tenant.tenant_id)
        idem = str(idempotency_key or "").strip()
        if idem:
            existing = ChangeFeedEvent.objects.filter(tenant=locked_tenant, idempotency_key=idem).first()
            if existing:
                return {
                    "idempotent_replay": True,
                    "event": cls._serialize_event(existing),
                }

        max_seq = (
            ChangeFeedEvent.objects.filter(tenant=locked_tenant).aggregate(max_seq=Max("sequence_no")).get("max_seq")
            or 0
        )
        next_seq = int(max_seq) + 1
        digest = cls._compute_payload_sha256(payload)

        ev = ChangeFeedEvent.objects.create(
            tenant=locked_tenant,
            branch=branch_obj,
            sequence_no=next_seq,
            entity_type=ent_type,
            entity_id=ent_id,
            operation=op,
            entity_version=ver,
            payload_json=payload,
            payload_sha256=digest,
            monetary_impact_bdt=amt,
            source_device_id=str(source_device_id or "").strip(),
            idempotency_key=idem,
            emitted_by=actor_user if (actor_user and getattr(actor_user, "is_authenticated", False)) else None,
        )
        return {
            "idempotent_replay": False,
            "event": cls._serialize_event(ev),
        }

    @classmethod
    @transaction.atomic
    def pull_change_feed(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        device_fingerprint_sha256: str,
        since_sequence: Optional[int] = None,
        limit: int = DEFAULT_PULL_BATCH_SIZE,
        entity_types: Optional[List[str]] = None,
        exclude_self_device: bool = False,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        reg = cls._assert_device(tenant, device_id, device_fingerprint_sha256, actor_user)

        try:
            batch_limit = int(limit if limit is not None else DEFAULT_PULL_BATCH_SIZE)
        except (TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_LIMIT", "detail": "limit must be an integer."})
        if batch_limit < 1 or batch_limit > MAX_PULL_BATCH_SIZE:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_LIMIT",
                    "detail": f"limit must be between 1 and {MAX_PULL_BATCH_SIZE}.",
                }
            )

        cursor, _ = DeviceChangeFeedCursor.objects.select_for_update().get_or_create(
            device_registration=reg,
            defaults={
                "tenant": tenant,
                "branch": reg.branch,
                "last_sequence_no": 0,
            },
        )

        if since_sequence is None:
            effective_since = cursor.last_sequence_no
        else:
            try:
                effective_since = int(since_sequence)
            except (TypeError, ValueError):
                raise ValidationError(
                    {"error_code": "ERR_INVALID_CURSOR", "detail": "since_sequence must be a non-negative integer."}
                )
            if effective_since < 0:
                raise ValidationError(
                    {"error_code": "ERR_INVALID_CURSOR", "detail": "since_sequence cannot be negative."}
                )

        # Strict tenant + branch scoping: tenant-wide events (branch IS NULL) + device's own branch ONLY
        qs = ChangeFeedEvent.objects.filter(
            tenant=tenant,
            sequence_no__gt=effective_since,
        ).filter(Q(branch__isnull=True) | Q(branch=reg.branch))

        if entity_types:
            if not isinstance(entity_types, list):
                raise ValidationError({"error_code": "ERR_INVALID_ENTITY_TYPES", "detail": "entity_types must be a list."})
            cleaned_types = [str(t).strip().upper() for t in entity_types if str(t).strip()]
            invalid_types = [t for t in cleaned_types if t not in ChangeFeedEntityType.values]
            if invalid_types:
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_ENTITY_TYPES",
                        "detail": f"Unsupported entity_types: {invalid_types}",
                    }
                )
            if cleaned_types:
                qs = qs.filter(entity_type__in=cleaned_types)

        if exclude_self_device:
            qs = qs.exclude(source_device_id=reg.device_id)

        qs = qs.order_by("sequence_no")
        fetched = list(qs[: batch_limit + 1])
        has_more = len(fetched) > batch_limit
        page_events = fetched[:batch_limit]

        latest_server_seq = (
            ChangeFeedEvent.objects.filter(tenant=tenant).aggregate(max_seq=Max("sequence_no")).get("max_seq") or 0
        )
        next_cursor_seq = page_events[-1].sequence_no if page_events else effective_since
        cursor_token = cls._compute_cursor_token(str(tenant.tenant_id), reg.device_id, next_cursor_seq)

        cursor.pull_count += 1
        cursor.last_pull_at = timezone.now()
        cursor.save(update_fields=["pull_count", "last_pull_at", "updated_at"])

        serialized = [cls._serialize_event(e) for e in page_events]
        tombstone_count = sum(1 for e in page_events if e.operation == ChangeFeedOperation.TOMBSTONE)
        reversal_count = sum(1 for e in page_events if e.operation == ChangeFeedOperation.REVERSAL)

        return {
            "tenant_id": str(tenant.tenant_id),
            "branch_id": str(reg.branch_id),
            "device_id": reg.device_id,
            "since_sequence": effective_since,
            "next_cursor_sequence": next_cursor_seq,
            "cursor_token": cursor_token,
            "latest_server_sequence": int(latest_server_seq),
            "has_more": has_more,
            "event_count": len(serialized),
            "tombstone_count": tombstone_count,
            "reversal_count": reversal_count,
            "events": serialized,
            "message_en": f"Pulled {len(serialized)} incremental change(s) up to sequence {next_cursor_seq}.",
            "message_bn": f"সিকোয়েন্স {next_cursor_seq} পর্যন্ত {len(serialized)}টি পরিবর্তন পুল হয়েছে।",
        }

    @classmethod
    @transaction.atomic
    def checkpoint_cursor(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        device_fingerprint_sha256: str,
        acknowledged_sequence_no: int,
        cursor_token: str = "",
        applied_event_count: int = 0,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        reg = cls._assert_device(tenant, device_id, device_fingerprint_sha256, actor_user)

        try:
            ack_seq = int(acknowledged_sequence_no)
        except (TypeError, ValueError):
            raise ValidationError(
                {"error_code": "ERR_INVALID_CURSOR", "detail": "acknowledged_sequence_no must be an integer."}
            )
        if ack_seq < 0:
            raise ValidationError(
                {"error_code": "ERR_INVALID_CURSOR", "detail": "acknowledged_sequence_no cannot be negative."}
            )

        expected_token = cls._compute_cursor_token(str(tenant.tenant_id), reg.device_id, ack_seq)
        if cursor_token and str(cursor_token).strip() != expected_token:
            raise ValidationError(
                {
                    "error_code": "ERR_CURSOR_TOKEN_MISMATCH",
                    "detail": "Cursor token does not match tenant, device, and sequence_no.",
                }
            )

        latest_server_seq = (
            ChangeFeedEvent.objects.filter(tenant=tenant).aggregate(max_seq=Max("sequence_no")).get("max_seq") or 0
        )
        if ack_seq > int(latest_server_seq):
            raise ValidationError(
                {
                    "error_code": "ERR_CURSOR_AHEAD_OF_SERVER",
                    "detail": f"acknowledged_sequence_no ({ack_seq}) exceeds server sequence ({latest_server_seq}).",
                }
            )

        cursor, _ = DeviceChangeFeedCursor.objects.select_for_update().get_or_create(
            device_registration=reg,
            defaults={
                "tenant": tenant,
                "branch": reg.branch,
                "last_sequence_no": 0,
            },
        )

        if ack_seq < cursor.last_sequence_no:
            raise ValidationError(
                {
                    "error_code": "ERR_CURSOR_REGRESSION",
                    "detail": f"Cannot move cursor backwards from {cursor.last_sequence_no} to {ack_seq}.",
                }
            )

        is_replay = ack_seq == cursor.last_sequence_no
        if not is_replay:
            cursor.last_sequence_no = ack_seq
            cursor.last_cursor_token = expected_token
            cursor.total_events_applied += max(0, int(applied_event_count or 0))
            cursor.last_checkpoint_at = timezone.now()
            cursor.save(
                update_fields=[
                    "last_sequence_no",
                    "last_cursor_token",
                    "total_events_applied",
                    "last_checkpoint_at",
                    "updated_at",
                ]
            )

        lag = max(0, int(latest_server_seq) - int(cursor.last_sequence_no))
        return {
            "cursor_id": cursor.cursor_id,
            "tenant_id": str(tenant.tenant_id),
            "branch_id": str(reg.branch_id),
            "device_id": reg.device_id,
            "last_sequence_no": cursor.last_sequence_no,
            "last_cursor_token": cursor.last_cursor_token,
            "latest_server_sequence": int(latest_server_seq),
            "lag_events": lag,
            "total_events_applied": cursor.total_events_applied,
            "idempotent_replay": is_replay,
            "last_checkpoint_at": cursor.last_checkpoint_at.isoformat() if cursor.last_checkpoint_at else None,
        }

    @classmethod
    def list_events(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        entity_type: Optional[str] = None,
        since_sequence: int = 0,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        cls._assert_authenticated_tenant_user(tenant, actor_user)

        qs = ChangeFeedEvent.objects.filter(tenant=tenant, sequence_no__gt=max(0, int(since_sequence or 0)))
        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            qs = qs.filter(Q(branch__isnull=True) | Q(branch_id=user_branch))
        elif branch_id:
            qs = qs.filter(Q(branch__isnull=True) | Q(branch_id=str(branch_id).strip()))

        if entity_type:
            qs = qs.filter(entity_type=str(entity_type).strip().upper())

        items = [cls._serialize_event(ev) for ev in qs.order_by("sequence_no")[:100]]
        return {
            "tenant_id": str(tenant.tenant_id),
            "count": len(items),
            "events": items,
        }

    @classmethod
    def list_device_cursors(cls, *, tenant_id: str, actor_user: Any = None) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        cls._assert_authenticated_tenant_user(tenant, actor_user)

        latest_server_seq = (
            ChangeFeedEvent.objects.filter(tenant=tenant).aggregate(max_seq=Max("sequence_no")).get("max_seq") or 0
        )
        qs = DeviceChangeFeedCursor.objects.filter(tenant=tenant).select_related("device_registration", "branch")
        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            qs = qs.filter(branch_id=user_branch)

        rows = []
        for c in qs[:100]:
            lag = max(0, int(latest_server_seq) - int(c.last_sequence_no))
            rows.append(
                {
                    "cursor_id": c.cursor_id,
                    "device_id": c.device_registration.device_id,
                    "branch_id": str(c.branch_id),
                    "last_sequence_no": c.last_sequence_no,
                    "latest_server_sequence": int(latest_server_seq),
                    "lag_events": lag,
                    "pull_count": c.pull_count,
                    "total_events_applied": c.total_events_applied,
                    "last_pull_at": c.last_pull_at.isoformat() if c.last_pull_at else None,
                    "last_checkpoint_at": c.last_checkpoint_at.isoformat() if c.last_checkpoint_at else None,
                }
            )
        return {
            "tenant_id": str(tenant.tenant_id),
            "latest_server_sequence": int(latest_server_seq),
            "count": len(rows),
            "cursors": rows,
        }
