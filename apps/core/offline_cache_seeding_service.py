"""
backend/apps/core/offline_cache_seeding_service.py
Architecture Decision: ADR-0303 (P303).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Authoritative cloud bootstrap seeding service for JavaFX SQLite offline stores:
- Scopes downloads strictly to authorized (tenant, device.branch) boundaries (never leaking cross-branch or cross-tenant stock/staff).
- Computes deterministic SHA-256 consistency tokens over sorted catalog, branch batch stock (Decimal precision), and minimal staff permissions.
- Supports conditional delta skip via `if_none_match_token` (304-like `not_modified: True`).
- Verifies device fingerprint, active lease, and active tenant subscription before emitting snapshots.
- Records atomic device acknowledgement (`APPLIED`) and advances `device_registration.last_sync_cursor`.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.double_entry_journal_service import q2
from apps.core.models import (
    BatchStatus,
    BatchStock,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    MedicineProduct,
    OfflineCacheSeedSnapshot,
    OfflineCacheSeedStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger(__name__)

SEED_ALLOWED_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "FINANCE_OFFICER",
    "CASHIER",
    "STAFF_PHARMACIST",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}


class OfflineCacheSeedingService:
    """
    Issues versioned, consistency-tokenized offline cache bootstrap packages strictly
    scoped to an authorized registered POS device's tenant and branch.
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
                    "detail": "Tenant subscription is suspended. Offline cache seeding blocked.",
                    "message_en": "Tenant subscription is suspended. Offline seeding blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। অফলাইন ক্যাশ সিডিং বন্ধ রয়েছে।",
                }
            )
        return tenant

    @classmethod
    def _assert_device_and_scope(
        cls,
        tenant: Tenant,
        device_id: str,
        device_fingerprint_sha256: str,
        actor_user: Any,
    ) -> DeviceLocalDbRegistration:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in SEED_ALLOWED_ROLES:
            raise PermissionDenied({"error_code": "ERR_SEED_UNAUTHORIZED", "detail": "Role not permitted to seed POS cache."})

        reg = DeviceLocalDbRegistration.objects.select_related("branch").filter(tenant=tenant, device_id=device_id).first()
        if not reg:
            foreign = DeviceLocalDbRegistration.objects.filter(device_id=device_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant.tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant device seed forbidden."})
            raise NotFound({"error_code": "ERR_DEVICE_NOT_REGISTERED", "detail": f"Device '{device_id}' not registered."})

        if reg.status != DeviceLocalDbStatus.ACTIVE:
            raise PermissionDenied({"error_code": "ERR_DEVICE_REVOKED", "detail": "Revoked device cannot bootstrap cache."})

        if not reg.lease_expires_at or reg.lease_expires_at <= timezone.now():
            raise PermissionDenied({"error_code": "ERR_OFFLINE_LEASE_EXPIRED", "detail": "Offline lease expired. Renew registration first."})

        fp = str(device_fingerprint_sha256 or "").strip().lower()
        if fp != reg.device_fingerprint_sha256:
            raise PermissionDenied({"error_code": "ERR_DEVICE_FINGERPRINT_MISMATCH", "detail": "Device fingerprint mismatch."})

        # Non-owner branch staff can ONLY seed their own assigned branch's device!
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
                raise PermissionDenied(
                    {
                        "error_code": "ERR_BRANCH_SCOPE_DENIED",
                        "detail": "Staff can only bootstrap offline cache for their assigned branch.",
                        "message_en": "You can only seed offline cache for your assigned branch.",
                        "message_bn": "আপনি শুধুমাত্র নিজস্ব শাখার অফলাইন ক্যাশ ডাউনলোড করতে পারবেন।",
                    }
                )
        return reg

    @classmethod
    def _compute_consistency_token(cls, payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    @transaction.atomic
    def generate_bootstrap_seed(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        device_fingerprint_sha256: str,
        if_none_match_token: Optional[str] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        if not str(device_id or "").strip():
            raise ValidationError({"error_code": "ERR_MISSING_DEVICE_ID", "detail": "device_id is required."})

        reg = cls._assert_device_and_scope(tenant, device_id.strip(), device_fingerprint_sha256, actor_user)
        now = timezone.now()

        # 1. Branch-scoped ACTIVE batch stock (strictly tenant + reg.branch ONLY)
        stock_qs = BatchStock.objects.select_related("product").filter(
            tenant=tenant,
            branch=reg.branch,
            status=BatchStatus.ACTIVE,
            quantity_base_units__gte=0,
        ).order_by("batch_id")

        branch_stock: List[Dict[str, Any]] = []
        product_ids = set()
        for b in stock_qs:
            product_ids.add(b.product_id)
            branch_stock.append(
                {
                    "batch_id": str(b.batch_id),
                    "tenant_id": str(tenant.tenant_id),
                    "branch_id": str(reg.branch_id),
                    "product_id": str(b.product_id),
                    "batch_number": b.batch_number,
                    "expiry_date": b.expiry_date.isoformat() if hasattr(b.expiry_date, "isoformat") else str(b.expiry_date),
                    "quantity_base_units": int(b.quantity_base_units),
                    "cost_per_unit_bdt": str(q2(getattr(b, "cost_per_unit", Decimal("0.00")))),
                    "status": b.status,
                }
            )

        # 2. Shared Medicine Catalog (active medicines referenced or general catalog up to safe page limit)
        med_qs = MedicineProduct.objects.all().order_by("product_id")[:500]
        catalog: List[Dict[str, Any]] = []
        for m in med_qs:
            catalog.append(
                {
                    "product_id": str(m.product_id),
                    "tenant_id": str(tenant.tenant_id),
                    "brand_name": m.brand_name,
                    "generic_name": m.generic_name,
                    "dosage_form": str(getattr(m, "dosage_form", "TABLET")),
                    "mrp_bdt": str(q2(getattr(m, "mrp_unit", getattr(m, "mrp_bdt", Decimal("0.00"))))),
                    "rx_required": bool(getattr(m, "requires_prescription", False)),
                    "is_antimicrobial": bool(getattr(m, "is_antimicrobial", False)),
                    "is_active": True,
                }
            )

        # 3. Minimal Authorized Staff Permissions strictly for this branch + tenant owners (NO password hashes/PHI)
        staff_qs = PharmacyUser.objects.filter(
            tenant_id=str(tenant.tenant_id),
            is_active=True,
        ).order_by("email")
        staff_permissions: List[Dict[str, Any]] = []
        for u in staff_qs:
            u_role = str(getattr(u, "role", "CASHIER"))
            u_branch = str(getattr(u, "branch_id", "") or "")
            if u_role not in (UserRole.PHARMACY_OWNER, "PHARMACY_OWNER") and u_branch != str(reg.branch_id):
                continue
            staff_permissions.append(
                {
                    "user_id": str(getattr(u, "user_id", u.pk)),
                    "tenant_id": str(tenant.tenant_id),
                    "branch_id": str(reg.branch_id),
                    "role": u_role,
                    "expires_at": reg.lease_expires_at.isoformat(),
                }
            )

        data_bundle = {
            "tenant_id": str(tenant.tenant_id),
            "branch_id": str(reg.branch_id),
            "device_id": reg.device_id,
            "catalog": catalog,
            "branch_stock": branch_stock,
            "staff_permissions": staff_permissions,
        }
        consistency_token = cls._compute_consistency_token(data_bundle)

        # Conditional skip if device already holds exact same snapshot token
        if if_none_match_token and str(if_none_match_token).strip().lower() == consistency_token:
            return {
                "not_modified": True,
                "consistency_token": consistency_token,
                "device_id": reg.device_id,
                "message_en": "Local offline cache is already consistent with cloud snapshot.",
                "message_bn": "লোকাল অফলাইন ক্যাশ ইতোমধ্যে ক্লাউডের সাথে সম্পূর্ণ সামঞ্জস্যপূর্ণ।",
            }

        prev_max = (
            OfflineCacheSeedSnapshot.objects.filter(device_registration=reg).aggregate(mx=Max("snapshot_version"))["mx"]
            or 0
        )
        next_version = int(prev_max) + 1

        OfflineCacheSeedSnapshot.objects.filter(
            device_registration=reg, status=OfflineCacheSeedStatus.ISSUED
        ).update(status=OfflineCacheSeedStatus.SUPERSEDED)

        snap = OfflineCacheSeedSnapshot.objects.create(
            tenant=tenant,
            branch=reg.branch,
            device_registration=reg,
            snapshot_version=next_version,
            consistency_token=consistency_token,
            catalog_count=len(catalog),
            batch_stock_count=len(branch_stock),
            staff_count=len(staff_permissions),
            status=OfflineCacheSeedStatus.ISSUED,
            expires_at=reg.lease_expires_at,
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        lease_payload = {
            "lease_id": f"lease-{snap.seed_id}",
            "tenant_id": str(tenant.tenant_id),
            "branch_id": str(reg.branch_id),
            "device_id": reg.device_id,
            "plan_tier": str(getattr(tenant, "plan_tier", "PRO")),
            "issued_at": now.isoformat(),
            "expires_at": reg.lease_expires_at.isoformat(),
            "signature_sha256": hashlib.sha256(
                f"{tenant.tenant_id}|{reg.device_id}|{reg.lease_expires_at.isoformat()}|{consistency_token}".encode("utf-8")
            ).hexdigest(),
        }

        return {
            "not_modified": False,
            "seed_id": snap.seed_id,
            "snapshot_version": snap.snapshot_version,
            "consistency_token": consistency_token,
            "tenant_id": str(tenant.tenant_id),
            "branch_id": str(reg.branch_id),
            "device_id": reg.device_id,
            "counts": {
                "catalog": len(catalog),
                "branch_stock": len(branch_stock),
                "staff_permissions": len(staff_permissions),
            },
            "subscription_lease": lease_payload,
            "data": data_bundle,
            "status": snap.status,
            "issued_at": snap.created_at.isoformat(),
        }

    @classmethod
    @transaction.atomic
    def acknowledge_seed_applied(
        cls,
        *,
        tenant_id: str,
        seed_id: str,
        consistency_token: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})

        snap = OfflineCacheSeedSnapshot.objects.select_for_update().select_related("device_registration").filter(
            tenant=tenant, seed_id=seed_id
        ).first()
        if not snap:
            foreign = OfflineCacheSeedSnapshot.objects.filter(seed_id=seed_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant acknowledgement forbidden."})
            raise NotFound({"error_code": "ERR_SEED_NOT_FOUND", "detail": f"Seed '{seed_id}' not found."})

        if str(consistency_token or "").strip().lower() != snap.consistency_token:
            raise ValidationError(
                {
                    "error_code": "ERR_CONSISTENCY_TOKEN_MISMATCH",
                    "detail": "Submitted consistency_token does not match issued cloud snapshot.",
                    "message_en": "Consistency token mismatch detected. Re-seed local cache.",
                    "message_bn": "কনসিস্টেন্সি টোকেন মেলেনি। অনুগ্রহ করে পুনরায় ক্যাশ সিড করুন।",
                }
            )

        now = timezone.now()
        snap.status = OfflineCacheSeedStatus.APPLIED
        snap.applied_at = now
        snap.save()

        reg = snap.device_registration
        reg.last_sync_cursor = f"seed-v{snap.snapshot_version}:{snap.consistency_token[:16]}"
        reg.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "staff@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="OFFLINE_CACHE_SEED_APPLIED",
            resource_type="OfflineCacheSeedSnapshot",
            resource_id=snap.seed_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"snapshot_version": snap.snapshot_version, "cursor": reg.last_sync_cursor},
        )

        return {
            "success": True,
            "seed_id": snap.seed_id,
            "snapshot_version": snap.snapshot_version,
            "status": snap.status,
            "last_sync_cursor": reg.last_sync_cursor,
            "message_en": f"Offline cache seed v{snap.snapshot_version} confirmed applied.",
            "message_bn": f"অফলাইন ক্যাশ সিড v{snap.snapshot_version} সফলভাবে অ্যাপ্লাই নিশ্চিত হয়েছে।",
        }

    @classmethod
    def list_seed_history(cls, *, tenant_id: str, branch_id: Optional[str] = None, actor_user: Any = None) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        qs = OfflineCacheSeedSnapshot.objects.filter(tenant=tenant).select_related("branch", "device_registration")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        items = [
            {
                "seed_id": s.seed_id,
                "device_id": s.device_registration.device_id,
                "branch_id": str(s.branch_id),
                "snapshot_version": s.snapshot_version,
                "consistency_token": s.consistency_token,
                "catalog_count": s.catalog_count,
                "batch_stock_count": s.batch_stock_count,
                "staff_count": s.staff_count,
                "status": s.status,
                "applied_at": s.applied_at.isoformat() if s.applied_at else None,
                "created_at": s.created_at.isoformat(),
            }
            for s in qs[:50]
        ]
        return {"tenant_id": str(tenant.tenant_id), "count": len(items), "seeds": items}
