"""
backend/apps/core/offline_prescription_policy_service.py
Architecture Decision: ADR-0311 (P311).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Authoritative Offline Prescription Policy & Minimal-PHI Authorization Token Service:
- Governs which prescription classes may be cached as pre-verified offline authorization tokens.
- Hard clinical safety invariants:
  1. Controlled / narcotic substances (`product.is_narcotic`) are NEVER permitted for offline caching or dispensing.
  2. Antimicrobial / antibiotic Rx (`product.is_antibiotic`) requires explicit branch policy opt-in AND
     pharmacist/manager pre-verification AND flags `antimicrobial_register_required=True`.
  3. Offline cache stores ONLY cryptographic SHA-256 `prescription_ref_hash_sha256` (minimized PHI — zero raw patient PII).
  4. Offline verification ALWAYS FAILS CLOSED if token is missing, expired, revoked, signature-tampered,
     or requested units exceed remaining authorized units.
- Revoking a token emits a P307 `ChangeFeedEvent` (`TOMBSTONE`) so POS devices invalidate it immediately.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from decimal import Decimal
from typing import Any, Dict, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.change_feed_service import ChangeFeedService, TENANT_WIDE_ROLES
from apps.core.double_entry_journal_service import q2
from apps.core.models import (
    Branch,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    MedicineProduct,
    OfflinePrescriptionAuthToken,
    OfflinePrescriptionPolicy,
    OfflineRxCategory,
    OfflineRxDispenseAudit,
    OfflineRxTokenStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

RX_POLICY_AND_ISSUE_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "PHARMACIST",
    "LICENSED_PHARMACIST",
    "OWNER_ADMIN",
    "ADMIN",
}


class OfflinePrescriptionPolicyService:
    """
    Minimal-PHI, policy-approved offline prescription authorization engine with fail-closed safety.
    """

    @classmethod
    def classify_product_rx_category(cls, product: MedicineProduct) -> str:
        if getattr(product, "is_narcotic", False):
            return OfflineRxCategory.CONTROLLED_NARCOTIC
        if getattr(product, "is_antibiotic", False):
            return OfflineRxCategory.ANTIMICROBIAL_RX
        if getattr(product, "is_prescription_only", False):
            return OfflineRxCategory.STANDARD_RX
        return OfflineRxCategory.OTC

    @classmethod
    def _compute_token_signature(
        cls,
        tenant_id: str,
        branch_id: str,
        device_id: str,
        product_id: str,
        rx_category: str,
        rx_ref_hash: str,
        approved_max_units: int,
        expires_at_iso: str,
    ) -> str:
        raw = f"orxtok|{tenant_id}|{branch_id}|{device_id}|{product_id}|{rx_category}|{rx_ref_hash}|{approved_max_units}|{expires_at_iso}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def _serialize_policy(cls, p: OfflinePrescriptionPolicy) -> Dict[str, Any]:
        return {
            "policy_id": p.policy_id,
            "tenant_id": str(p.tenant_id),
            "branch_id": str(p.branch_id),
            "allow_offline_otc": p.allow_offline_otc,
            "allow_offline_standard_rx": p.allow_offline_standard_rx,
            "allow_offline_antimicrobial_rx": p.allow_offline_antimicrobial_rx,
            "allow_offline_controlled_narcotic": False,
            "require_pharmacist_pre_verification": p.require_pharmacist_pre_verification,
            "fail_closed_when_unverified": True,
            "max_token_ttl_hours": p.max_token_ttl_hours,
            "is_archived": p.is_archived,
            "updated_at": p.updated_at.isoformat(),
        }

    @classmethod
    def _serialize_token(cls, t: OfflinePrescriptionAuthToken) -> Dict[str, Any]:
        remaining = max(0, int(t.approved_max_units) - int(t.dispensed_units))
        return {
            "token_id": t.token_id,
            "tenant_id": str(t.tenant_id),
            "branch_id": str(t.branch_id),
            "device_id": t.device_registration.device_id,
            "product_id": str(t.product_id),
            "rx_category": t.rx_category,
            "prescription_ref_hash_sha256": t.prescription_ref_hash_sha256,
            "approved_max_units": t.approved_max_units,
            "dispensed_units": t.dispensed_units,
            "remaining_units": remaining,
            "unit_price_bdt": str(q2(t.unit_price_bdt)),
            "authorized_total_bdt": str(q2(t.authorized_total_bdt)),
            "token_signature_sha256": t.token_signature_sha256,
            "expires_at": t.expires_at.isoformat(),
            "status": t.status,
            "revocation_reason": t.revocation_reason,
            "antimicrobial_register_required": t.rx_category == OfflineRxCategory.ANTIMICROBIAL_RX,
            "created_at": t.created_at.isoformat(),
        }

    @classmethod
    def _assert_rx_authority(cls, actor_user: Any, branch_id: Optional[str] = None) -> None:
        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in RX_POLICY_AND_ISSUE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_RX_AUTHORITY_REQUIRED",
                    "detail": "Only Pharmacy Owner, Branch Manager, or Licensed Pharmacist may manage offline Rx policy or tokens.",
                    "message_en": "Pharmacist or Manager authorization required.",
                    "message_bn": "ফার্মাসিস্ট বা ম্যানেজার অনুমতি প্রয়োজন।",
                }
            )
        if branch_id and not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            if user_branch and user_branch != str(branch_id):
                raise PermissionDenied({"error_code": "ERR_BRANCH_SCOPE_DENIED", "detail": "Branch scope denied."})

    @classmethod
    @transaction.atomic
    def upsert_policy(
        cls,
        *,
        tenant_id: str,
        branch_id: str,
        allow_offline_otc: bool = True,
        allow_offline_standard_rx: bool = True,
        allow_offline_antimicrobial_rx: bool = False,
        allow_offline_controlled_narcotic: bool = False,
        fail_closed_when_unverified: bool = True,
        max_token_ttl_hours: int = 24,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)
        cls._assert_rx_authority(actor_user, branch_id=branch_id)

        branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
        if not branch:
            if Branch.objects.filter(branch_id=branch_id).exists():
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_BRANCH", "detail": "Branch belongs to another tenant."})
            raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        # Hard regulatory & clinical safety invariants
        if allow_offline_controlled_narcotic is True:
            raise ValidationError(
                {
                    "error_code": "ERR_CONTROLLED_NARCOTIC_OFFLINE_FORBIDDEN",
                    "detail": "Controlled/narcotic dispensing cannot be enabled offline under safety policy.",
                }
            )
        if fail_closed_when_unverified is False:
            raise ValidationError(
                {
                    "error_code": "ERR_FAIL_OPEN_FORBIDDEN",
                    "detail": "Offline prescription verification must always fail closed when unverified.",
                }
            )

        try:
            ttl = int(max_token_ttl_hours)
            if ttl < 1 or ttl > 72:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_TTL", "detail": "max_token_ttl_hours must be 1..72."})

        policy, created = OfflinePrescriptionPolicy.objects.select_for_update().update_or_create(
            branch=branch,
            defaults={
                "tenant": tenant,
                "allow_offline_otc": bool(allow_offline_otc),
                "allow_offline_standard_rx": bool(allow_offline_standard_rx),
                "allow_offline_antimicrobial_rx": bool(allow_offline_antimicrobial_rx),
                "allow_offline_controlled_narcotic": False,
                "require_pharmacist_pre_verification": True,
                "fail_closed_when_unverified": True,
                "max_token_ttl_hours": ttl,
                "is_archived": False,
                "updated_by": actor_user if getattr(actor_user, "is_authenticated", False) else None,
            },
        )
        return {"created": created, "policy": cls._serialize_policy(policy)}

    @classmethod
    @transaction.atomic
    def archive_policy(cls, *, tenant_id: str, policy_id: str, actor_user: Any = None) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)
        p = OfflinePrescriptionPolicy.objects.select_for_update().filter(tenant=tenant, policy_id=policy_id).first()
        if not p:
            raise NotFound({"error_code": "ERR_POLICY_NOT_FOUND", "detail": f"Policy '{policy_id}' not found."})
        cls._assert_rx_authority(actor_user, branch_id=str(p.branch_id))
        p.is_archived = True
        p.updated_by = actor_user
        p.save(update_fields=["is_archived", "updated_by", "updated_at"])
        return {"policy": cls._serialize_policy(p)}

    @classmethod
    def list_policies(cls, *, tenant_id: str, actor_user: Any = None) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)
        qs = OfflinePrescriptionPolicy.objects.filter(tenant=tenant, is_archived=False)
        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            qs = qs.filter(branch_id=str(getattr(actor_user, "branch_id", "") or ""))
        rows = [cls._serialize_policy(p) for p in qs[:50]]
        return {"tenant_id": str(tenant.tenant_id), "count": len(rows), "policies": rows}

    @classmethod
    @transaction.atomic
    def issue_offline_auth_token(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        product_id: str,
        prescription_reference_id: str,
        approved_max_units: int,
        ttl_hours: int = 12,
        idempotency_key: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Issues a policy-approved, minimal-PHI, SHA-256 signed offline prescription authorization token
        for a registered POS device. Strictly blocks controlled/narcotic items and policy-disallowed categories.
        """
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)

        reg = DeviceLocalDbRegistration.objects.select_related("branch").filter(tenant=tenant, device_id=device_id).first()
        if not reg:
            if DeviceLocalDbRegistration.objects.filter(device_id=device_id).exists():
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant device forbidden."})
            raise NotFound({"error_code": "ERR_DEVICE_NOT_REGISTERED", "detail": f"Device '{device_id}' not found."})

        cls._assert_rx_authority(actor_user, branch_id=str(reg.branch_id))

        if reg.status != DeviceLocalDbStatus.ACTIVE or not reg.lease_expires_at or reg.lease_expires_at <= timezone.now():
            raise PermissionDenied({"error_code": "ERR_DEVICE_LEASE_INVALID", "detail": "Device lease is expired or revoked."})

        rx_ref = str(prescription_reference_id or "").strip()
        idem = str(idempotency_key or "").strip()
        if not rx_ref or not idem or not product_id:
            raise ValidationError(
                {"error_code": "ERR_INCOMPLETE_RX_TOKEN", "detail": "product_id, prescription_reference_id, and idempotency_key are required."}
            )

        existing = OfflinePrescriptionAuthToken.objects.select_related("device_registration").filter(
            tenant=tenant, idempotency_key=idem
        ).first()
        if existing:
            return {"idempotent_replay": True, "token": cls._serialize_token(existing)}

        product = MedicineProduct.objects.filter(product_id=str(product_id).strip()).first()
        if not product:
            raise NotFound({"error_code": "ERR_PRODUCT_NOT_FOUND", "detail": f"Product '{product_id}' not found."})

        try:
            max_u = int(approved_max_units)
            if max_u < 1:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_UNITS", "detail": "approved_max_units must be >= 1."})

        policy = OfflinePrescriptionPolicy.objects.filter(tenant=tenant, branch=reg.branch, is_archived=False).first()
        rx_cat = cls.classify_product_rx_category(product)

        if rx_cat == OfflineRxCategory.CONTROLLED_NARCOTIC:
            raise ValidationError(
                {
                    "error_code": "ERR_CONTROLLED_NARCOTIC_OFFLINE_FORBIDDEN",
                    "detail": "Controlled/narcotic medicines can NEVER be cached for offline prescription dispensing.",
                    "message_en": "Controlled/narcotic dispensing requires live online pharmacist verification.",
                    "message_bn": "নিয়ন্ত্রিত/নারকোটিক ওষুধ অফলাইনে বিক্রয় বা ক্যাশ করা যাবে না।",
                }
            )
        if rx_cat == OfflineRxCategory.ANTIMICROBIAL_RX and not (policy and policy.allow_offline_antimicrobial_rx):
            raise ValidationError(
                {
                    "error_code": "ERR_OFFLINE_RX_DISALLOWED_BY_POLICY",
                    "detail": "Branch policy disallows caching antimicrobial prescriptions offline.",
                }
            )
        if rx_cat == OfflineRxCategory.STANDARD_RX and policy and not policy.allow_offline_standard_rx:
            raise ValidationError(
                {
                    "error_code": "ERR_OFFLINE_RX_DISALLOWED_BY_POLICY",
                    "detail": "Branch policy disallows caching standard prescriptions offline.",
                }
            )

        max_ttl = policy.max_token_ttl_hours if policy else 24
        try:
            eff_ttl = int(ttl_hours)
            if eff_ttl < 1 or eff_ttl > max_ttl:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError(
                {"error_code": "ERR_TTL_EXCEEDS_POLICY", "detail": f"ttl_hours must be between 1 and branch max ({max_ttl}h)."}
            )

        # Minimize PHI: store ONLY salted SHA-256 hash of prescription reference
        rx_ref_hash = hashlib.sha256(f"{tenant.tenant_id}|{rx_ref}".encode("utf-8")).hexdigest()
        expires_at = timezone.now() + datetime.timedelta(hours=eff_ttl)
        unit_price = q2(product.mrp_unit)
        auth_total = q2(Decimal(max_u) * unit_price)
        sig = cls._compute_token_signature(
            str(tenant.tenant_id),
            str(reg.branch_id),
            reg.device_id,
            product.product_id,
            rx_cat,
            rx_ref_hash,
            max_u,
            expires_at.isoformat(),
        )

        tok = OfflinePrescriptionAuthToken.objects.create(
            tenant=tenant,
            branch=reg.branch,
            device_registration=reg,
            product=product,
            rx_category=rx_cat,
            prescription_ref_hash_sha256=rx_ref_hash,
            approved_max_units=max_u,
            dispensed_units=0,
            unit_price_bdt=unit_price,
            authorized_total_bdt=auth_total,
            verified_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            token_signature_sha256=sig,
            expires_at=expires_at,
            status=OfflineRxTokenStatus.ACTIVE,
            idempotency_key=idem,
        )
        return {
            "idempotent_replay": False,
            "token": cls._serialize_token(tok),
            "message_en": f"Policy-approved offline Rx token issued (max {max_u} units, valid {eff_ttl}h).",
            "message_bn": f"পলিসি-অনুমোদিত অফলাইন প্রেসক্রিপশন টোকেন ইস্যু হয়েছে (সর্বোচ্চ {max_u} ইউনিট, মেয়াদ {eff_ttl} ঘণ্টা)।",
        }

    @classmethod
    @transaction.atomic
    def verify_and_record_dispense(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        device_fingerprint_sha256: str,
        token_id: str,
        token_signature_sha256: str,
        dispensed_units: int,
        local_invoice_ref: str,
        idempotency_key: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Fail-closed verification & replay-safe dispensing reconciliation against a policy-approved Rx token.
        """
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        reg = ChangeFeedService._assert_device(tenant, device_id, device_fingerprint_sha256, actor_user)

        idem = str(idempotency_key or "").strip()
        inv_ref = str(local_invoice_ref or "").strip()
        if not token_id or not idem or not inv_ref:
            raise ValidationError(
                {"error_code": "ERR_FAIL_CLOSED_UNVERIFIED_RX", "detail": "Valid token_id, local_invoice_ref, and idempotency_key are required."}
            )

        existing_audit = OfflineRxDispenseAudit.objects.select_related("token", "token__device_registration").filter(
            tenant=tenant, idempotency_key=idem
        ).first()
        if existing_audit:
            return {
                "idempotent_replay": True,
                "audit_id": existing_audit.audit_id,
                "dispensed_value_bdt": str(q2(existing_audit.dispensed_value_bdt)),
                "antimicrobial_register_required": existing_audit.antimicrobial_register_required,
                "token": cls._serialize_token(existing_audit.token),
            }

        try:
            units = int(dispensed_units)
            if units < 1:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_UNITS", "detail": "dispensed_units must be >= 1."})

        tok = (
            OfflinePrescriptionAuthToken.objects.select_for_update()
            .select_related("device_registration")
            .filter(tenant=tenant, branch=reg.branch, device_registration=reg, token_id=str(token_id).strip())
            .first()
        )
        if not tok:
            raise ValidationError(
                {
                    "error_code": "ERR_FAIL_CLOSED_TOKEN_MISSING",
                    "detail": "Offline prescription authorization could not be established for this device/branch; dispensing blocked.",
                }
            )

        if tok.status == OfflineRxTokenStatus.REVOKED:
            raise ValidationError({"error_code": "ERR_RX_TOKEN_REVOKED", "detail": "Prescription authorization token was revoked."})
        if tok.expires_at <= timezone.now() or tok.status == OfflineRxTokenStatus.EXPIRED:
            if tok.status == OfflineRxTokenStatus.ACTIVE:
                tok.status = OfflineRxTokenStatus.EXPIRED
                tok.save(update_fields=["status", "updated_at"])
            raise ValidationError({"error_code": "ERR_RX_TOKEN_EXPIRED", "detail": "Prescription authorization token has expired; online verification required."})
        if str(token_signature_sha256 or "").strip().lower() != tok.token_signature_sha256:
            raise PermissionDenied({"error_code": "ERR_RX_TOKEN_SIGNATURE_TAMPERED", "detail": "Offline Rx token signature mismatch."})

        remaining = tok.approved_max_units - tok.dispensed_units
        if units > remaining:
            raise ValidationError(
                {
                    "error_code": "ERR_RX_APPROVED_QTY_EXCEEDED",
                    "detail": f"Requested {units} units exceeds remaining authorized Rx quantity ({remaining}).",
                }
            )

        tok.dispensed_units += units
        if tok.dispensed_units >= tok.approved_max_units:
            tok.status = OfflineRxTokenStatus.CONSUMED
        tok.save(update_fields=["dispensed_units", "status", "updated_at"])

        disp_val = q2(Decimal(units) * tok.unit_price_bdt)
        abx_req = tok.rx_category == OfflineRxCategory.ANTIMICROBIAL_RX

        audit = OfflineRxDispenseAudit.objects.create(
            tenant=tenant,
            branch=reg.branch,
            token=tok,
            device_registration=reg,
            local_invoice_ref=inv_ref,
            dispensed_units=units,
            dispensed_value_bdt=disp_val,
            antimicrobial_register_required=abx_req,
            idempotency_key=idem,
            dispensed_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )
        return {
            "idempotent_replay": False,
            "audit_id": audit.audit_id,
            "dispensed_value_bdt": str(disp_val),
            "antimicrobial_register_required": abx_req,
            "token": cls._serialize_token(tok),
            "message_en": f"Offline Rx dispense verified ({units} units, BDT {disp_val}).",
            "message_bn": f"অফলাইন প্রেসক্রিপশন ডিস্পেন্স যাচাই হয়েছে ({units} ইউনিট, ৳{disp_val})।",
        }

    @classmethod
    @transaction.atomic
    def revoke_token(
        cls, *, tenant_id: str, token_id: str, reason: str, actor_user: Any = None
    ) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)

        tok = (
            OfflinePrescriptionAuthToken.objects.select_for_update()
            .select_related("device_registration")
            .filter(tenant=tenant, token_id=str(token_id).strip())
            .first()
        )
        if not tok:
            raise NotFound({"error_code": "ERR_TOKEN_NOT_FOUND", "detail": f"Token '{token_id}' not found."})

        cls._assert_rx_authority(actor_user, branch_id=str(tok.branch_id))
        clean_reason = str(reason or "").strip()
        if len(clean_reason) < 5:
            raise ValidationError({"error_code": "ERR_REASON_REQUIRED", "detail": "Revocation reason (>= 5 chars) is required."})

        tok.status = OfflineRxTokenStatus.REVOKED
        tok.revocation_reason = clean_reason[:255]
        tok.revoked_at = timezone.now()
        tok.save(update_fields=["status", "revocation_reason", "revoked_at", "updated_at"])

        # Emit P307 ChangeFeed TOMBSTONE so offline POS devices immediately invalidate this Rx token!
        ChangeFeedService.publish_change_event(
            tenant_id=str(tenant.tenant_id),
            entity_type="STAFF_PERMISSION",
            entity_id=tok.token_id,
            operation="TOMBSTONE",
            payload_json={
                "tombstone_reason": clean_reason,
                "revoked_rx_token_id": tok.token_id,
                "product_id": str(tok.product_id),
            },
            branch_id=str(tok.branch_id),
            entity_version=2,
            source_device_id=tok.device_registration.device_id,
            idempotency_key=f"orxtok-rev:{tok.token_id}",
            actor_user=actor_user,
            system_internal=True,
        )

        return {
            "token": cls._serialize_token(tok),
            "message_en": "Offline prescription token revoked and tombstone published to branch change feed.",
            "message_bn": "অফলাইন প্রেসক্রিপশন টোকেন বাতিল হয়েছে এবং ব্রাঞ্চ চেঞ্জ ফিডে টুম্বস্টোন পাঠানো হয়েছে।",
        }

    @classmethod
    def list_tokens(cls, *, tenant_id: str, status_filter: Optional[str] = None, actor_user: Any = None) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)
        qs = OfflinePrescriptionAuthToken.objects.filter(tenant=tenant).select_related("device_registration")
        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            qs = qs.filter(branch_id=str(getattr(actor_user, "branch_id", "") or ""))
        if status_filter:
            qs = qs.filter(status=str(status_filter).strip().upper())
        rows = [cls._serialize_token(t) for t in qs[:100]]
        return {"tenant_id": str(tenant.tenant_id), "count": len(rows), "tokens": rows}
