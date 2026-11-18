"""
backend/apps/core/device_encryption_key_service.py
Architecture Decision: ADR-0302 (P302).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Authoritative domain service governing Local SQLite Encryption Key Material, Rotation & Safe Recovery:
- Stores ONLY wrapped DEK ciphertext blobs, SHA-256 DEK fingerprints, and salted recovery code hashes.
- Rejects raw plaintext keys or malformed wrapped blobs.
- Atomic key rotation increments key_version, retires prior key, and logs AuditLogEntry.
- Safe device recovery verifies constant-time recovery_code hash and rotates key material immediately.
- Emergency compromise revocation marks key COMPROMISED_REVOKED and revokes linked DeviceLocalDbRegistration.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import logging
import secrets
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    DeviceEncryptionKeyRecord,
    DeviceEncryptionKeyStatus,
    DeviceKeyAlgorithm,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DEFAULT_KEY_ROTATION_DAYS = 90

KEY_MANAGE_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

KEY_VIEW_ROLES = KEY_MANAGE_ROLES | {
    UserRole.BRANCH_MANAGER,
    UserRole.AUDITOR,
    "BRANCH_MANAGER",
    "AUDITOR",
}


class DeviceEncryptionKeyService:
    """
    Governs SQLite encryption key provisioning, rotation, safe recovery, and compromise revocation.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return
        if getattr(actor_user, "role", None) not in KEY_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_DEVICE_KEY_VIEW_DENIED",
                    "detail": "Counter staff are not authorized to inspect device encryption keys.",
                    "message_en": "Counter staff are not authorized to access encryption key records.",
                    "message_bn": "কাউন্টার স্টাফ এনক্রিপশন কী ব্যবস্থাপনা দেখার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def assert_manage_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return
        if getattr(actor_user, "role", None) not in KEY_MANAGE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_DEVICE_KEY_MANAGE_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Finance Officer can provision, rotate, recover, or revoke SQLite encryption keys.",
                    "message_en": "Only Pharmacy Owner or Finance Officer can manage encryption keys.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা ফিন্যান্স অফিসার এনক্রিপশন কী রোটেশন বা রিকভারি করতে পারেন।",
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
            raise PermissionDenied({"error_code": "ERR_TENANT_SUSPENDED", "detail": "Tenant subscription is suspended."})
        return tenant

    @classmethod
    def _hash_recovery_code(cls, tenant_id: str, device_id: str, raw_code: str) -> str:
        return hashlib.sha256(f"{tenant_id}|{device_id}|{raw_code.strip()}".encode("utf-8")).hexdigest()

    @classmethod
    def _validate_key_material(cls, dek_fp: str, wrapped_blob: str, algorithm: str) -> str:
        fp = str(dek_fp or "").strip().lower()
        if len(fp) != 64:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_DEK_FINGERPRINT",
                    "detail": "dek_fingerprint_sha256 must be a 64-character SHA-256 hex digest.",
                }
            )
        blob = str(wrapped_blob or "").strip()
        if len(blob) < 32 or blob.startswith("RAW:") or "PLAINTEXT" in blob.upper():
            raise ValidationError(
                {
                    "error_code": "ERR_UNWRAPPED_OR_INVALID_KEY_BLOB",
                    "detail": "wrapped_dek_blob must be an encrypted ciphertext envelope (>= 32 chars); plaintext keys are forbidden.",
                    "message_en": "Plaintext encryption keys are forbidden; supply an AES-256-GCM wrapped ciphertext blob.",
                    "message_bn": "প্লেইনটেক্সট কী অনুমোদিত নয়; এনক্রিপ্টেড কী এনভেলপ প্রদান করুন।",
                }
            )
        alg = str(algorithm or DeviceKeyAlgorithm.AES_256_GCM_ENVELOPE).upper().strip()
        if alg not in DeviceKeyAlgorithm.values:
            raise ValidationError({"error_code": "ERR_INVALID_ALGORITHM", "detail": f"Unsupported algorithm '{alg}'."})
        return alg

    @classmethod
    def _serialize_key(cls, rec: DeviceEncryptionKeyRecord, include_wrapped: bool = False) -> Dict[str, Any]:
        now = timezone.now()
        res = {
            "key_id": rec.key_id,
            "tenant_id": str(rec.tenant_id),
            "branch_id": str(rec.branch_id),
            "device_registration_id": str(rec.device_registration_id),
            "device_id": rec.device_registration.device_id,
            "key_version": rec.key_version,
            "algorithm": rec.algorithm,
            "dek_fingerprint_sha256": rec.dek_fingerprint_sha256,
            "status": rec.status,
            "rotated_from_key_id": str(rec.rotated_from_id) if rec.rotated_from_id else None,
            "expires_at": rec.expires_at.isoformat(),
            "needs_rotation": rec.expires_at <= now and rec.status == DeviceEncryptionKeyStatus.ACTIVE,
            "rotated_at": rec.rotated_at.isoformat() if rec.rotated_at else None,
            "created_at": rec.created_at.isoformat(),
        }
        if include_wrapped:
            res["wrapped_dek_blob"] = rec.wrapped_dek_blob
        return res

    @classmethod
    @transaction.atomic
    def provision_initial_key(
        cls, *, tenant_id: str, data: Dict[str, Any], actor_user: Any = None
    ) -> Dict[str, Any]:
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        reg_id = str(data.get("device_registration_id") or "").strip()
        reg = DeviceLocalDbRegistration.objects.filter(tenant=tenant, registration_id=reg_id).first()
        if not reg:
            foreign = DeviceLocalDbRegistration.objects.filter(registration_id=reg_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant device key provisioning forbidden."})
            raise NotFound({"error_code": "ERR_DEVICE_NOT_FOUND", "detail": f"Device registration '{reg_id}' not found."})

        if reg.status != DeviceLocalDbStatus.ACTIVE:
            raise ValidationError({"error_code": "ERR_DEVICE_REVOKED", "detail": "Cannot provision key for revoked device."})

        if DeviceEncryptionKeyRecord.objects.filter(
            device_registration=reg, status=DeviceEncryptionKeyStatus.ACTIVE
        ).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_ACTIVE_KEY_ALREADY_EXISTS",
                    "detail": "Device already has an active encryption key. Use /rotate/ to rotate key material.",
                }
            )

        alg = cls._validate_key_material(
            data.get("dek_fingerprint_sha256", ""),
            data.get("wrapped_dek_blob", ""),
            data.get("algorithm", DeviceKeyAlgorithm.AES_256_GCM_ENVELOPE),
        )

        # Generate one-time safe device recovery code (returned ONCE to Owner, stored only as salted SHA-256 hash)
        raw_recovery_code = f"RCV-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
        recovery_hash = cls._hash_recovery_code(str(tenant.tenant_id), reg.device_id, raw_recovery_code)

        rec = DeviceEncryptionKeyRecord.objects.create(
            tenant=tenant,
            branch=reg.branch,
            device_registration=reg,
            key_version=1,
            algorithm=alg,
            dek_fingerprint_sha256=str(data["dek_fingerprint_sha256"]).strip().lower(),
            wrapped_dek_blob=str(data["wrapped_dek_blob"]).strip(),
            recovery_code_hash_sha256=recovery_hash,
            status=DeviceEncryptionKeyStatus.ACTIVE,
            expires_at=timezone.now() + datetime.timedelta(days=DEFAULT_KEY_ROTATION_DAYS),
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="DEVICE_SQLITE_KEY_PROVISION",
            resource_type="DeviceEncryptionKeyRecord",
            resource_id=rec.key_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"device_id": reg.device_id, "key_version": 1, "algorithm": alg},
        )

        res = cls._serialize_key(rec, include_wrapped=False)
        res["one_time_recovery_code"] = raw_recovery_code
        res["message_en"] = "Encrypted key record provisioned. Store one_time_recovery_code offline securely."
        res["message_bn"] = "এনক্রিপশন কী সংরক্ষিত হয়েছে। ওয়ান-টাইম রিকভারি কোডটি নিরাপদে সংরক্ষণ করুন।"
        return res

    @classmethod
    @transaction.atomic
    def rotate_device_key(
        cls, *, tenant_id: str, key_id: str, data: Dict[str, Any], actor_user: Any = None
    ) -> Dict[str, Any]:
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        old_rec = DeviceEncryptionKeyRecord.objects.select_for_update().select_related("device_registration").filter(
            tenant=tenant, key_id=key_id
        ).first()
        if not old_rec:
            foreign = DeviceEncryptionKeyRecord.objects.filter(key_id=key_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant rotation forbidden."})
            raise NotFound({"error_code": "ERR_KEY_NOT_FOUND", "detail": f"Key '{key_id}' not found."})

        if old_rec.status != DeviceEncryptionKeyStatus.ACTIVE:
            raise ValidationError({"error_code": "ERR_KEY_NOT_ACTIVE", "detail": f"Cannot rotate key in status '{old_rec.status}'."})

        alg = cls._validate_key_material(
            data.get("new_dek_fingerprint_sha256", ""),
            data.get("new_wrapped_dek_blob", ""),
            data.get("algorithm", old_rec.algorithm),
        )
        new_fp = str(data["new_dek_fingerprint_sha256"]).strip().lower()
        if new_fp == old_rec.dek_fingerprint_sha256:
            raise ValidationError(
                {
                    "error_code": "ERR_KEY_REUSE_FORBIDDEN",
                    "detail": "Rotated DEK fingerprint must differ from the previous key version.",
                }
            )

        now = timezone.now()
        old_rec.status = DeviceEncryptionKeyStatus.RETIRED
        old_rec.rotated_at = now
        old_rec.save()

        new_rec = DeviceEncryptionKeyRecord.objects.create(
            tenant=tenant,
            branch=old_rec.branch,
            device_registration=old_rec.device_registration,
            key_version=old_rec.key_version + 1,
            algorithm=alg,
            dek_fingerprint_sha256=new_fp,
            wrapped_dek_blob=str(data["new_wrapped_dek_blob"]).strip(),
            recovery_code_hash_sha256=old_rec.recovery_code_hash_sha256,
            status=DeviceEncryptionKeyStatus.ACTIVE,
            rotated_from=old_rec,
            expires_at=now + datetime.timedelta(days=DEFAULT_KEY_ROTATION_DAYS),
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="DEVICE_SQLITE_KEY_ROTATE",
            resource_type="DeviceEncryptionKeyRecord",
            resource_id=new_rec.key_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"old_version": old_rec.key_version, "new_version": new_rec.key_version},
        )

        res = cls._serialize_key(new_rec, include_wrapped=False)
        res["message_en"] = f"Key rotated from v{old_rec.key_version} to v{new_rec.key_version}."
        res["message_bn"] = f"এনক্রিপশন কী ভার্সন v{old_rec.key_version} থেকে v{new_rec.key_version}-এ রোটেট হয়েছে।"
        return res

    @classmethod
    @transaction.atomic
    def recover_device_key(
        cls, *, tenant_id: str, key_id: str, recovery_code: str, reason: str, actor_user: Any = None
    ) -> Dict[str, Any]:
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        if not str(recovery_code or "").strip() or not str(reason or "").strip():
            raise ValidationError(
                {"error_code": "ERR_MISSING_RECOVERY_FIELDS", "detail": "recovery_code and reason are required."}
            )

        rec = DeviceEncryptionKeyRecord.objects.select_for_update().select_related("device_registration").filter(
            tenant=tenant, key_id=key_id
        ).first()
        if not rec:
            foreign = DeviceEncryptionKeyRecord.objects.filter(key_id=key_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant recovery forbidden."})
            raise NotFound({"error_code": "ERR_KEY_NOT_FOUND", "detail": f"Key '{key_id}' not found."})

        if rec.status == DeviceEncryptionKeyStatus.COMPROMISED_REVOKED or rec.device_registration.status == DeviceLocalDbStatus.REVOKED:
            raise PermissionDenied({"error_code": "ERR_DEVICE_OR_KEY_REVOKED", "detail": "Revoked device/key cannot be recovered."})

        candidate_hash = cls._hash_recovery_code(str(tenant.tenant_id), rec.device_registration.device_id, recovery_code)
        if not hmac.compare_digest(candidate_hash, rec.recovery_code_hash_sha256):
            raise PermissionDenied(
                {
                    "error_code": "ERR_INVALID_RECOVERY_CODE",
                    "detail": "Invalid device recovery code.",
                    "message_en": "Invalid device recovery code.",
                    "message_bn": "ভুল ডিভাইস রিকভারি কোড।",
                }
            )

        # Rotate recovery code immediately so the used code cannot be replayed
        next_code = f"RCV-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
        rec.recovery_code_hash_sha256 = cls._hash_recovery_code(
            str(tenant.tenant_id), rec.device_registration.device_id, next_code
        )
        rec.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="DEVICE_SQLITE_KEY_RECOVER",
            resource_type="DeviceEncryptionKeyRecord",
            resource_id=rec.key_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"device_id": rec.device_registration.device_id, "reason": reason.strip()},
        )

        res = cls._serialize_key(rec, include_wrapped=True)
        res["next_one_time_recovery_code"] = next_code
        res["message_en"] = "Wrapped key envelope released for audited device recovery. Rotate DEK after restore."
        res["message_bn"] = "অডিটেড ডিভাইস রিকভারির জন্য এনক্রিপ্টেড কী এনভেলপ প্রদান করা হয়েছে।"
        return res

    @classmethod
    @transaction.atomic
    def revoke_compromised_key(
        cls, *, tenant_id: str, key_id: str, reason: str = "", actor_user: Any = None
    ) -> Dict[str, Any]:
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        rec = DeviceEncryptionKeyRecord.objects.select_for_update().select_related("device_registration").filter(
            tenant=tenant, key_id=key_id
        ).first()
        if not rec:
            foreign = DeviceEncryptionKeyRecord.objects.filter(key_id=key_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant revocation forbidden."})
            raise NotFound({"error_code": "ERR_KEY_NOT_FOUND", "detail": f"Key '{key_id}' not found."})

        clean_reason = str(reason or "Suspected device compromise").strip()
        rec.status = DeviceEncryptionKeyStatus.COMPROMISED_REVOKED
        rec.revocation_reason = clean_reason
        rec.save()

        # Also cascade-revoke linked P301 DeviceLocalDbRegistration!
        reg = rec.device_registration
        reg.status = DeviceLocalDbStatus.REVOKED
        reg.revocation_reason = f"Key {rec.key_id} revoked: {clean_reason}"
        reg.save()

        return {
            "success": True,
            "key_id": rec.key_id,
            "status": rec.status,
            "device_status": reg.status,
            "message_en": f"Key '{rec.key_id}' and device '{reg.device_id}' revoked immediately.",
            "message_bn": f"কী '{rec.key_id}' এবং ডিভাইস '{reg.device_id}' তাৎক্ষণিকভাবে বাতিল করা হয়েছে।",
        }

    @classmethod
    def list_keys(
        cls, *, tenant_id: str, device_registration_id: Optional[str] = None, actor_user: Any = None
    ) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)
        qs = DeviceEncryptionKeyRecord.objects.filter(tenant=tenant).select_related("device_registration")
        if device_registration_id:
            qs = qs.filter(device_registration_id=device_registration_id)
        if getattr(actor_user, "role", None) in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
            qs = qs.filter(branch_id=str(getattr(actor_user, "branch_id", "")))
        items = [cls._serialize_key(r, include_wrapped=False) for r in qs]
        return {"tenant_id": str(tenant.tenant_id), "count": len(items), "keys": items}
