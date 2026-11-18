"""
backend/apps/core/device_local_db_schema_service.py
Architecture Decision: ADR-0301 (P301).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Authoritative server-side contract & device-binding registry for the encrypted JavaFX SQLite
local store (8 tables: device_metadata, schema_migrations, cached_catalog_medicines,
cached_branch_stock, cached_staff_permissions, subscription_leases, pending_offline_commands,
sync_conflict_queue).
"""

from __future__ import annotations

import datetime
import hashlib
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
    Branch,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    DeviceOfflineStockPolicy,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

CURRENT_SQLITE_SCHEMA_VERSION = 1
DEFAULT_OFFLINE_LEASE_HOURS = 72

DEVICE_MANAGE_ROLES = {
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

DEVICE_VIEW_ROLES = DEVICE_MANAGE_ROLES | {
    UserRole.AUDITOR,
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    "AUDITOR",
    "CASHIER",
    "STAFF_PHARMACIST",
}

SQLITE_DDL_V1 = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;

CREATE TABLE IF NOT EXISTS device_metadata (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    device_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    device_fingerprint_sha256 TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    offline_stock_policy TEXT NOT NULL,
    last_sync_cursor TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    ddl_sha256 TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cached_catalog_medicines (
    product_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    brand_name TEXT NOT NULL,
    generic_name TEXT NOT NULL,
    dosage_form TEXT NOT NULL,
    mrp_bdt TEXT NOT NULL,
    rx_required INTEGER NOT NULL DEFAULT 0 CHECK (rx_required IN (0, 1)),
    is_antimicrobial INTEGER NOT NULL DEFAULT 0 CHECK (is_antimicrobial IN (0, 1)),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    server_version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cached_branch_stock (
    batch_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    batch_number TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    quantity_base_units INTEGER NOT NULL CHECK (quantity_base_units >= 0),
    reserved_offline_units INTEGER NOT NULL DEFAULT 0 CHECK (reserved_offline_units >= 0),
    cost_per_unit_bdt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    server_version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES cached_catalog_medicines(product_id)
);

CREATE TABLE IF NOT EXISTS cached_staff_permissions (
    user_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    role TEXT NOT NULL,
    permissions_json TEXT NOT NULL,
    pin_hash_sha256 TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1))
);

CREATE TABLE IF NOT EXISTS subscription_leases (
    lease_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    plan_tier TEXT NOT NULL,
    entitlements_json TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    signature_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'EXPIRED', 'REVOKED'))
);

CREATE TABLE IF NOT EXISTS pending_offline_commands (
    command_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    device_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    client_sequence INTEGER NOT NULL UNIQUE CHECK (client_sequence >= 1),
    command_type TEXT NOT NULL,
    aggregate_ref TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    monetary_total_bdt TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    sync_state TEXT NOT NULL CHECK (sync_state IN (
        'SAVED_LOCALLY', 'SYNCING', 'SYNCED', 'RETRY_SCHEDULED', 'CONFLICT_NEEDS_REVIEW', 'REJECTED'
    )),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    next_retry_at TEXT,
    server_ack_id TEXT,
    error_detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_conflict_queue (
    conflict_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    conflict_reason TEXT NOT NULL,
    local_payload_json TEXT NOT NULL,
    server_state_json TEXT NOT NULL DEFAULT '{}',
    resolution_status TEXT NOT NULL DEFAULT 'OPEN' CHECK (resolution_status IN (
        'OPEN', 'RESOLVED_KEEP_SERVER', 'RESOLVED_ADJUSTED', 'ESCALATED'
    )),
    resolved_by TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (command_id) REFERENCES pending_offline_commands(command_id)
);

CREATE INDEX IF NOT EXISTS idx_cached_stock_branch_prod ON cached_branch_stock(tenant_id, branch_id, product_id);
CREATE INDEX IF NOT EXISTS idx_outbox_sync_state_seq ON pending_offline_commands(sync_state, client_sequence);
CREATE INDEX IF NOT EXISTS idx_conflict_open ON sync_conflict_queue(tenant_id, resolution_status, created_at);
""".strip()

SQLITE_DDL_V1_SHA256 = hashlib.sha256(SQLITE_DDL_V1.encode("utf-8")).hexdigest()


class DeviceLocalDbSchemaService:
    """
    Governs authoritative SQLite offline schema distribution, device fingerprint binding,
    subscription lease issuance, and offline command envelope validation.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return
        if getattr(actor_user, "role", None) not in DEVICE_VIEW_ROLES:
            raise PermissionDenied({"error_code": "ERR_DEVICE_VIEW_DENIED", "detail": "Not authorized."})

    @classmethod
    def assert_manage_authorized(cls, actor_user: Any, branch_id: Optional[str] = None) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if role not in DEVICE_MANAGE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_DEVICE_MANAGE_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner, Finance Officer, or Branch Manager can register or manage offline POS devices.",
                    "message_en": "Only Owner, Finance Officer, or Branch Manager can register POS devices.",
                    "message_bn": "শুধুমাত্র ওনার, ফিন্যান্স অফিসার বা ব্রাঞ্চ ম্যানেজার অফলাইন পিওএস ডিভাইস নিবন্ধন করতে পারেন।",
                }
            )
        if role in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER") and branch_id:
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            if user_branch and str(branch_id) != user_branch:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_BRANCH_SCOPE_DENIED",
                        "detail": "Branch Manager can only manage POS devices in their assigned branch.",
                    }
                )

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
                    "detail": "Tenant subscription is suspended. Offline device registration and leases are blocked.",
                    "message_en": "Tenant subscription is suspended. Offline lease denied.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। অফলাইন লিজ ও ডিভাইস রেজিস্ট্রেশন বন্ধ রয়েছে।",
                }
            )
        return tenant

    @classmethod
    def get_schema_manifest(cls, *, tenant_id: str, actor_user: Any = None) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)
        return {
            "tenant_id": str(tenant.tenant_id),
            "schema_version": CURRENT_SQLITE_SCHEMA_VERSION,
            "ddl_sha256": SQLITE_DDL_V1_SHA256,
            "sqlite_cipher_mode": "SQLCIPHER_AES_256_GCM",
            "tables": [
                "device_metadata",
                "schema_migrations",
                "cached_catalog_medicines",
                "cached_branch_stock",
                "cached_staff_permissions",
                "subscription_leases",
                "pending_offline_commands",
                "sync_conflict_queue",
            ],
            "ddl_sql": SQLITE_DDL_V1,
        }

    @classmethod
    def _serialize_registration(cls, reg: DeviceLocalDbRegistration) -> Dict[str, Any]:
        now = timezone.now()
        lease_valid = bool(reg.lease_expires_at and reg.lease_expires_at > now and reg.status == DeviceLocalDbStatus.ACTIVE)
        return {
            "registration_id": reg.registration_id,
            "tenant_id": str(reg.tenant_id),
            "branch_id": str(reg.branch_id),
            "branch_name": reg.branch.name if reg.branch else "",
            "device_id": reg.device_id,
            "device_label": reg.device_label,
            "device_fingerprint_sha256": reg.device_fingerprint_sha256,
            "schema_version": reg.schema_version,
            "ddl_sha256": reg.ddl_sha256,
            "sqlite_cipher_mode": reg.sqlite_cipher_mode,
            "offline_stock_policy": reg.offline_stock_policy,
            "last_sync_cursor": reg.last_sync_cursor,
            "lease_expires_at": reg.lease_expires_at.isoformat() if reg.lease_expires_at else None,
            "is_lease_valid": lease_valid,
            "status": reg.status,
            "created_at": reg.created_at.isoformat(),
            "updated_at": reg.updated_at.isoformat(),
        }

    @classmethod
    @transaction.atomic
    def register_device(
        cls,
        *,
        tenant_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        branch_id = str(data.get("branch_id") or "").strip()
        cls.assert_manage_authorized(actor_user, branch_id=branch_id)
        tenant = cls.assert_tenant_active(tenant_id)

        if not branch_id:
            raise ValidationError({"error_code": "ERR_MISSING_BRANCH", "detail": "branch_id is required."})
        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
        if not branch:
            foreign = Branch.objects.filter(branch_id=branch_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant branch access forbidden."})
            raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        device_id = str(data.get("device_id") or "").strip()
        fp = str(data.get("device_fingerprint_sha256") or "").strip().lower()
        if not device_id or len(fp) != 64:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_DEVICE_BINDING",
                    "detail": "device_id and a 64-char SHA-256 device_fingerprint_sha256 are required.",
                    "message_en": "Valid device_id and 64-character SHA-256 fingerprint are required.",
                    "message_bn": "সঠিক ডিভাইস আইডি এবং ৬৪-অক্ষরের SHA-256 ফিঙ্গারপ্রিন্ট আবশ্যক।",
                }
            )

        policy = str(
            data.get("offline_stock_policy", DeviceOfflineStockPolicy.CONTROLLED_RECONCILIATION)
        ).upper().strip()
        if policy not in DeviceOfflineStockPolicy.values:
            raise ValidationError({"error_code": "ERR_INVALID_STOCK_POLICY", "detail": f"Invalid offline_stock_policy '{policy}'."})

        # Guard against cross-tenant device_id collision or fingerprint spoofing
        existing = DeviceLocalDbRegistration.objects.filter(tenant=tenant, device_id=device_id).first()
        if existing:
            if existing.device_fingerprint_sha256 != fp:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_DEVICE_FINGERPRINT_MISMATCH",
                        "detail": "Device ID is already bound to a different hardware fingerprint.",
                    }
                )
            if existing.status == DeviceLocalDbStatus.REVOKED:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_DEVICE_REVOKED",
                        "detail": "This device registration has been revoked and cannot be reused.",
                    }
                )
            existing.lease_expires_at = timezone.now() + datetime.timedelta(hours=DEFAULT_OFFLINE_LEASE_HOURS)
            existing.schema_version = CURRENT_SQLITE_SCHEMA_VERSION
            existing.ddl_sha256 = SQLITE_DDL_V1_SHA256
            existing.offline_stock_policy = policy
            existing.save()
            reg = existing
        else:
            reg = DeviceLocalDbRegistration.objects.create(
                tenant=tenant,
                branch=branch,
                device_id=device_id,
                device_label=str(data.get("device_label", "POS Counter Terminal")).strip(),
                device_fingerprint_sha256=fp,
                schema_version=CURRENT_SQLITE_SCHEMA_VERSION,
                ddl_sha256=SQLITE_DDL_V1_SHA256,
                sqlite_cipher_mode="SQLCIPHER_AES_256_GCM",
                offline_stock_policy=policy,
                lease_expires_at=timezone.now() + datetime.timedelta(hours=DEFAULT_OFFLINE_LEASE_HOURS),
                status=DeviceLocalDbStatus.ACTIVE,
                registered_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="OFFLINE_DEVICE_SCHEMA_REGISTER",
            resource_type="DeviceLocalDbRegistration",
            resource_id=reg.registration_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"device_id": device_id, "schema_version": reg.schema_version},
        )

        res = cls._serialize_registration(reg)
        res["schema_manifest"] = cls.get_schema_manifest(tenant_id=tenant_id, actor_user=actor_user)
        return res

    @classmethod
    def list_devices(cls, *, tenant_id: str, branch_id: Optional[str] = None, actor_user: Any = None) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)
        qs = DeviceLocalDbRegistration.objects.filter(tenant=tenant).select_related("branch")
        if getattr(actor_user, "role", None) in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
            qs = qs.filter(branch_id=str(getattr(actor_user, "branch_id", "")))
        elif branch_id:
            qs = qs.filter(branch_id=branch_id)
        items = [cls._serialize_registration(r) for r in qs]
        return {"tenant_id": str(tenant.tenant_id), "count": len(items), "devices": items}

    @classmethod
    @transaction.atomic
    def update_device_policy(
        cls, *, tenant_id: str, registration_id: str, data: Dict[str, Any], actor_user: Any = None
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        reg = DeviceLocalDbRegistration.objects.select_for_update().filter(tenant=tenant, registration_id=registration_id).first()
        if not reg:
            foreign = DeviceLocalDbRegistration.objects.filter(registration_id=registration_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_DEVICE_NOT_FOUND", "detail": f"Device '{registration_id}' not found."})

        cls.assert_manage_authorized(actor_user, branch_id=str(reg.branch_id))
        if reg.status == DeviceLocalDbStatus.REVOKED:
            raise ValidationError({"error_code": "ERR_DEVICE_REVOKED", "detail": "Revoked device cannot be modified."})

        if "offline_stock_policy" in data:
            pol = str(data["offline_stock_policy"]).upper().strip()
            if pol not in DeviceOfflineStockPolicy.values:
                raise ValidationError({"error_code": "ERR_INVALID_STOCK_POLICY", "detail": f"Invalid policy '{pol}'."})
            reg.offline_stock_policy = pol
        if "device_label" in data and str(data["device_label"]).strip():
            reg.device_label = str(data["device_label"]).strip()
        if "last_sync_cursor" in data:
            reg.last_sync_cursor = str(data["last_sync_cursor"] or "").strip()
        reg.save()
        return cls._serialize_registration(reg)

    @classmethod
    @transaction.atomic
    def revoke_device(
        cls, *, tenant_id: str, registration_id: str, reason: str = "", actor_user: Any = None
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        reg = DeviceLocalDbRegistration.objects.select_for_update().filter(tenant=tenant, registration_id=registration_id).first()
        if not reg:
            foreign = DeviceLocalDbRegistration.objects.filter(registration_id=registration_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant revocation forbidden."})
            raise NotFound({"error_code": "ERR_DEVICE_NOT_FOUND", "detail": f"Device '{registration_id}' not found."})

        cls.assert_manage_authorized(actor_user, branch_id=str(reg.branch_id))
        reg.status = DeviceLocalDbStatus.REVOKED
        reg.revocation_reason = str(reason or "Device revoked by administrator").strip()
        reg.save()
        return {
            "success": True,
            "registration_id": reg.registration_id,
            "status": reg.status,
            "message_en": f"Offline POS device '{reg.device_id}' revoked.",
            "message_bn": f"অফলাইন পিওএস ডিভাইস '{reg.device_id}' বাতিল করা হয়েছে।",
        }

    @classmethod
    def validate_command_envelope(
        cls, *, tenant_id: str, envelope: Dict[str, Any], actor_user: Any = None
    ) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        device_id = str(envelope.get("device_id") or "").strip()
        reg = DeviceLocalDbRegistration.objects.filter(tenant=tenant, device_id=device_id).first()
        if not reg:
            raise NotFound({"error_code": "ERR_DEVICE_NOT_REGISTERED", "detail": f"Device '{device_id}' not registered."})
        if reg.status != DeviceLocalDbStatus.ACTIVE:
            raise PermissionDenied({"error_code": "ERR_DEVICE_REVOKED", "detail": "Device is revoked."})
        if not reg.lease_expires_at or reg.lease_expires_at <= timezone.now():
            raise PermissionDenied({"error_code": "ERR_OFFLINE_LEASE_EXPIRED", "detail": "Offline subscription lease expired."})

        for req_field in ("command_id", "idempotency_key", "command_type", "aggregate_ref"):
            if not str(envelope.get(req_field) or "").strip():
                raise ValidationError({"error_code": "ERR_INCOMPLETE_ENVELOPE", "detail": f"{req_field} is required."})

        try:
            seq = int(envelope.get("client_sequence"))
            if seq < 1:
                raise ValueError()
        except Exception:
            raise ValidationError({"error_code": "ERR_INVALID_SEQUENCE", "detail": "client_sequence must be >= 1."})

        try:
            amt = q2(Decimal(str(envelope.get("monetary_total_bdt", "0.00"))))
            if amt < Decimal("0.00"):
                raise ValueError()
        except Exception:
            raise ValidationError({"error_code": "ERR_INVALID_MONETARY_AMOUNT", "detail": "monetary_total_bdt must be a valid non-negative Decimal."})

        return {
            "valid": True,
            "device_id": device_id,
            "command_id": str(envelope["command_id"]),
            "idempotency_key": str(envelope["idempotency_key"]),
            "client_sequence": seq,
            "normalized_monetary_total_bdt": str(amt),
            "schema_version": reg.schema_version,
        }
