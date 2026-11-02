"""
Device Registration & Cryptographic Enrollment Service (P111 / ADR-0111).
Handles:
1. Desktop POS hardware cryptographic public key registration (Ed25519 / RSA-2048)
2. Branch ownership and readable labels
3. Maker-checker approval workflow (PENDING_APPROVAL -> APPROVED / REJECTED / REVOKED)
4. Active login verification and challenge gates
5. Immutable audit trails and subscription plan quota synchronization
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any, Dict, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    BranchStaffMembership,
    DeviceKeyAlgorithm,
    DeviceRegistrationAuditEvent,
    DeviceRegistrationStatus,
    DeviceSeat,
    DeviceSeatStatus,
    DeviceType,
    LoginDeviceRegistration,
    Tenant,
)
from apps.core.postgres_service import CrossTenantOperationError

logger = logging.getLogger("pharmacy.device_registration")


class DeviceRegistrationService:
    """Authoritative service for POS workstation enrollment, cryptographic trust, and audit."""

    @classmethod
    def _compute_key_fingerprint(cls, public_key_pem: str) -> str:
        """Computes SHA-256 fingerprint of the normalized public key PEM."""
        clean_key = public_key_pem.strip()
        return hashlib.sha256(clean_key.encode("utf-8")).hexdigest()

    @classmethod
    def _assert_tenant_active(cls, tenant: Tenant) -> None:
        """Blocks device operations if tenant subscription is inactive or suspended."""
        tenant_status = str(getattr(tenant, "status", getattr(tenant, "lifecycle_status", ""))).upper()
        if tenant_status in ["SUSPENDED", "ARCHIVED", "TERMINATED"]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
                    "message_en": "Tenant subscription is suspended or expired. Device registration blocked.",
                    "message_bn": "টেন্যান্ট সাবস্ক্রিপশন স্থগিত রয়েছে। ডিভাইস রেজিস্ট্রেশন সম্পন্ন করা সম্ভব নয়।",
                }
            )

    @classmethod
    def _assert_approval_authority(
        cls,
        actor_user: Any,
        registration: LoginDeviceRegistration,
    ) -> None:
        """
        Enforces separation of duties (RBAC):
        - Pharmacy Owner & Superadmin: Global authority over tenant branches.
        - Branch Manager: Restricted strictly to assigned branch_id.
        - Cashier / Dispenser: Strictly denied approval authority.
        """
        role = str(getattr(actor_user, "role", "UNKNOWN")).upper()
        user_ident = getattr(actor_user, "email", None) or getattr(actor_user, "username", "anon")

        # Superadmin / Platform operator has global authority across all tenants
        if role in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]:
            return

        # Enforce strict tenant boundary: user must belong to same tenant as device registration
        user_tid = getattr(actor_user, "tenant_id", None)
        if hasattr(user_tid, "tenant_id"):
            user_tid = user_tid.tenant_id
        user_tid = str(user_tid) if user_tid else ""

        if user_tid and user_tid != registration.tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": f"Cross-tenant violation: User from tenant '{user_tid}' cannot manage devices for tenant '{registration.tenant_id}'.",
                    "message_bn": "অন্য টেন্যান্টের ডিভাইস পরিচালনা করা নিষিদ্ধ।",
                }
            )

        # Pharmacy Owner has full authority within their own tenant
        if role in ["PHARMACY_OWNER", "TENANT_OWNER"]:
            return

        # Cashiers and frontline personnel are strictly blocked
        if role not in ["BRANCH_MANAGER", "PHARMACY_MANAGER"]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERMISSION_DENIED",
                    "role": role,
                    "message_en": f"Access denied: role '{role}' is not authorized to approve, reject or revoke devices.",
                    "message_bn": f"অননুমোদিত অ্যাকশন: '{role}' রোলে ডিভাইস অনুমোদন বা বাতিল করার অনুমতি নেই।",
                }
            )

        # Branch Manager: check branch alignment
        target_branch_id = registration.branch_id

        # 1. Check explicit BranchStaffMembership
        user_tid = getattr(actor_user, "tenant_id", None)
        if hasattr(user_tid, "tenant_id"):
            user_tid = user_tid.tenant_id
        user_tid = str(user_tid) if user_tid else registration.tenant_id

        has_branch_mem = BranchStaffMembership.objects.filter(
            tenant_id=user_tid,
            staff_member__username=user_ident,
            branch_id=target_branch_id,
            is_active=True,
        ).exists()

        if not has_branch_mem:
            # Check default user.branch_id
            user_bid = str(getattr(actor_user, "branch_id", "") or "")
            if not user_bid or user_bid != target_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_CROSS_BRANCH_VIOLATION",
                        "message_en": f"Cross-branch violation: Branch Manager cannot manage devices for branch '{target_branch_id}'.",
                        "message_bn": f"শাখা লঙ্ঘন: ব্রাঞ্চ ম্যানেজার '{target_branch_id}' শাখার ডিভাইস অনুমোদন করতে পারেন না।",
                    }
                )

    # -------------------------------------------------------------------------
    # 1. REGISTER WORKSTATION
    # -------------------------------------------------------------------------

    @classmethod
    def register_device(
        cls,
        tenant_id: str,
        branch_id: str,
        device_id: str,
        device_label: str,
        public_key_pem: str,
        key_algorithm: str = "ED25519",
        device_type: str = "DESKTOP_POS",
        hardware_fingerprint: str = "",
        mac_address: str = "",
        ip_address: str = "",
        requested_by: str = "",
    ) -> LoginDeviceRegistration:
        """
        Submits initial device registration request with cryptographic public key.
        Starts in PENDING_APPROVAL state awaiting manager/owner sign-off.
        """
        clean_tenant = str(tenant_id).strip()
        clean_branch = str(branch_id).strip()
        clean_dev_id = str(device_id).strip()
        clean_label = str(device_label).strip()
        clean_pub_key = str(public_key_pem).strip()

        if not clean_dev_id:
            raise ValidationError({"error_code": "MISSING_DEVICE_ID", "message": "device_id is required."})
        if not clean_label:
            raise ValidationError({"error_code": "MISSING_LABEL", "message": "device_label is required."})
        if len(clean_pub_key) < 32:
            raise ValidationError({"error_code": "INVALID_PUBLIC_KEY", "message": "public_key_pem is required."})

        try:
            tenant = Tenant.objects.get(tenant_id=clean_tenant)
        except Tenant.DoesNotExist:
            raise CrossTenantOperationError(f"Tenant '{clean_tenant}' does not exist.")

        cls._assert_tenant_active(tenant)

        try:
            branch = Branch.objects.get(branch_id=clean_branch, tenant=tenant)
        except Branch.DoesNotExist:
            raise ValidationError(
                {"error_code": "BRANCH_NOT_FOUND", "message": f"Branch '{clean_branch}' not found in tenant."}
            )

        fingerprint = cls._compute_key_fingerprint(clean_pub_key)

        clean_algo = key_algorithm.upper().strip()
        if clean_algo not in DeviceKeyAlgorithm.values:
            clean_algo = DeviceKeyAlgorithm.ED25519

        # Check existing registration for this device_id in tenant
        existing = LoginDeviceRegistration.objects.filter(tenant=tenant, device_id=clean_dev_id).first()
        if existing:
            if existing.status == DeviceRegistrationStatus.APPROVED:
                raise ValidationError(
                    {
                        "error_code": "ERR_DEVICE_ALREADY_REGISTERED",
                        "message_en": f"Device '{clean_dev_id}' is already registered and approved.",
                        "message_bn": f"ডিভাইস '{clean_dev_id}' ইতিমধ্যে নিবন্ধিত এবং অনুমোদিত রয়েছে।",
                    }
                )
            elif existing.status == DeviceRegistrationStatus.PENDING_APPROVAL:
                # Update existing pending request
                with transaction.atomic():
                    existing.device_label = clean_label
                    existing.branch = branch
                    existing.public_key_pem = clean_pub_key
                    existing.key_fingerprint = fingerprint
                    existing.hardware_fingerprint = hardware_fingerprint
                    existing.mac_address = mac_address
                    existing.ip_address = ip_address
                    existing.save()

                    DeviceRegistrationAuditEvent.objects.create(
                        event_id=f"dev-audit-{uuid.uuid4().hex[:12]}",
                        tenant=tenant,
                        device_registration=existing,
                        action="REGISTER_REQUESTED",
                        actor_username=requested_by,
                        details={"status": "PENDING_APPROVAL", "key_fingerprint": fingerprint},
                        ip_address=ip_address,
                    )
                return existing

        # Create new registration record
        reg_id = f"dreg-{uuid.uuid4().hex[:12]}"
        with transaction.atomic():
            reg = LoginDeviceRegistration.objects.create(
                registration_id=reg_id,
                tenant=tenant,
                branch=branch,
                device_id=clean_dev_id,
                device_label=clean_label,
                device_type=device_type or "DESKTOP_POS",
                public_key_pem=clean_pub_key,
                key_algorithm=clean_algo,
                key_fingerprint=fingerprint,
                hardware_fingerprint=hardware_fingerprint,
                mac_address=mac_address,
                ip_address=ip_address,
                status=DeviceRegistrationStatus.PENDING_APPROVAL,
                requested_by_username=requested_by,
            )

            DeviceRegistrationAuditEvent.objects.create(
                event_id=f"dev-audit-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                device_registration=reg,
                action="REGISTER_REQUESTED",
                actor_username=requested_by,
                details={"status": "PENDING_APPROVAL", "key_fingerprint": fingerprint},
                ip_address=ip_address,
            )

        logger.info("Device %s [%s] registered in PENDING_APPROVAL for tenant %s", clean_dev_id, clean_label, clean_tenant)
        return reg

    # -------------------------------------------------------------------------
    # 2. APPROVE WORKSTATION
    # -------------------------------------------------------------------------

    @classmethod
    def approve_device(
        cls,
        registration_id: str,
        actor_user: Any,
        approval_notes: str = "",
        ip_address: str = "",
    ) -> LoginDeviceRegistration:
        """
        Manager/Owner approves device registration, enabling active cryptographic login trust.
        Also syncs with DeviceSeat model to respect plan limits.
        """
        try:
            reg = LoginDeviceRegistration.objects.select_related("tenant", "branch").get(
                registration_id=registration_id
            )
        except LoginDeviceRegistration.DoesNotExist:
            raise ValidationError({"error_code": "REGISTRATION_NOT_FOUND", "message": "Device registration not found."})

        cls._assert_tenant_active(reg.tenant)
        cls._assert_approval_authority(actor_user, reg)

        actor_name = getattr(actor_user, "email", None) or getattr(actor_user, "username", "system")
        actor_role = str(getattr(actor_user, "role", "UNKNOWN")).upper()

        with transaction.atomic():
            reg.status = DeviceRegistrationStatus.APPROVED
            reg.approved_by_username = actor_name
            reg.approved_at = timezone.now()
            reg.approval_notes = approval_notes
            reg.save()

            # Sync or create DeviceSeat
            DeviceSeat.objects.update_or_create(
                tenant=reg.tenant,
                device_id=reg.device_id,
                defaults={
                    "branch": reg.branch,
                    "device_name": reg.device_label,
                    "device_type": DeviceType.DESKTOP_POS,
                    "status": DeviceSeatStatus.ACTIVE,
                    "mac_address": reg.mac_address,
                    "ip_address": reg.ip_address,
                    "hardware_fingerprint": reg.hardware_fingerprint,
                    "enrolled_by": actor_name,
                },
            )

            DeviceRegistrationAuditEvent.objects.create(
                event_id=f"dev-audit-{uuid.uuid4().hex[:12]}",
                tenant=reg.tenant,
                device_registration=reg,
                action="APPROVED",
                actor_username=actor_name,
                actor_role=actor_role,
                details={"approval_notes": approval_notes, "key_fingerprint": reg.key_fingerprint},
                ip_address=ip_address,
            )

        logger.info("Device %s [%s] APPROVED by %s", reg.device_id, reg.device_label, actor_name)
        return reg

    # -------------------------------------------------------------------------
    # 3. REJECT WORKSTATION
    # -------------------------------------------------------------------------

    @classmethod
    def reject_device(
        cls,
        registration_id: str,
        actor_user: Any,
        rejection_reason: str,
        ip_address: str = "",
    ) -> LoginDeviceRegistration:
        """Rejects pending device registration with mandatory audit reason."""
        clean_reason = str(rejection_reason or "").strip()
        if not clean_reason:
            raise ValidationError(
                {"error_code": "ERR_REASON_REQUIRED", "message": "A rejection reason is required."}
            )

        try:
            reg = LoginDeviceRegistration.objects.select_related("tenant", "branch").get(
                registration_id=registration_id
            )
        except LoginDeviceRegistration.DoesNotExist:
            raise ValidationError({"error_code": "REGISTRATION_NOT_FOUND", "message": "Device registration not found."})

        cls._assert_approval_authority(actor_user, reg)

        actor_name = getattr(actor_user, "email", None) or getattr(actor_user, "username", "system")
        actor_role = str(getattr(actor_user, "role", "UNKNOWN")).upper()

        with transaction.atomic():
            reg.status = DeviceRegistrationStatus.REJECTED
            reg.rejected_by_username = actor_name
            reg.rejected_at = timezone.now()
            reg.rejection_reason = clean_reason
            reg.save()

            DeviceRegistrationAuditEvent.objects.create(
                event_id=f"dev-audit-{uuid.uuid4().hex[:12]}",
                tenant=reg.tenant,
                device_registration=reg,
                action="REJECTED",
                actor_username=actor_name,
                actor_role=actor_role,
                details={"rejection_reason": clean_reason},
                ip_address=ip_address,
            )

        logger.info("Device %s [%s] REJECTED by %s: %s", reg.device_id, reg.device_label, actor_name, clean_reason)
        return reg

    # -------------------------------------------------------------------------
    # 4. REVOKE WORKSTATION
    # -------------------------------------------------------------------------

    @classmethod
    def revoke_device(
        cls,
        registration_id: str,
        actor_user: Any,
        revocation_reason: str,
        ip_address: str = "",
    ) -> LoginDeviceRegistration:
        """Revokes cryptographic trust for active device, immediately blocking logins."""
        clean_reason = str(revocation_reason or "").strip()
        if not clean_reason:
            raise ValidationError(
                {"error_code": "ERR_REASON_REQUIRED", "message": "A revocation reason is required."}
            )

        try:
            reg = LoginDeviceRegistration.objects.select_related("tenant", "branch").get(
                registration_id=registration_id
            )
        except LoginDeviceRegistration.DoesNotExist:
            raise ValidationError({"error_code": "REGISTRATION_NOT_FOUND", "message": "Device registration not found."})

        cls._assert_approval_authority(actor_user, reg)

        actor_name = getattr(actor_user, "email", None) or getattr(actor_user, "username", "system")
        actor_role = str(getattr(actor_user, "role", "UNKNOWN")).upper()

        with transaction.atomic():
            reg.status = DeviceRegistrationStatus.REVOKED
            reg.revoked_by_username = actor_name
            reg.revoked_at = timezone.now()
            reg.revocation_reason = clean_reason
            reg.save()

            # Revoke associated DeviceSeat
            DeviceSeat.objects.filter(tenant=reg.tenant, device_id=reg.device_id).update(
                status=DeviceSeatStatus.REVOKED,
                revoked_at=timezone.now(),
                revoked_by=actor_name,
                revocation_reason=clean_reason,
            )

            DeviceRegistrationAuditEvent.objects.create(
                event_id=f"dev-audit-{uuid.uuid4().hex[:12]}",
                tenant=reg.tenant,
                device_registration=reg,
                action="REVOKED",
                actor_username=actor_name,
                actor_role=actor_role,
                details={"revocation_reason": clean_reason},
                ip_address=ip_address,
            )

        logger.info("Device %s [%s] REVOKED by %s: %s", reg.device_id, reg.device_label, actor_name, clean_reason)
        return reg

    # -------------------------------------------------------------------------
    # 5. VERIFY / CHALLENGE WORKSTATION
    # -------------------------------------------------------------------------

    @classmethod
    def verify_device(
        cls,
        tenant_id: str,
        device_id: str,
        public_key_pem: Optional[str] = None,
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Cryptographic verification gate: determines if device is authorized to login.
        Returns: { is_trusted: bool, status: str, ... }
        """
        reg = (
            LoginDeviceRegistration.objects.filter(tenant_id=tenant_id, device_id=device_id)
            .select_related("branch")
            .first()
        )
        if not reg:
            return {
                "is_trusted": False,
                "status": "NOT_REGISTERED",
                "error_code": "ERR_DEVICE_NOT_FOUND",
                "message_en": f"Device '{device_id}' is not registered with this pharmacy.",
                "message_bn": f"ডিভাইস '{device_id}' এই ফার্মেসিতে নিবন্ধিত নয়।",
            }

        if reg.status == DeviceRegistrationStatus.PENDING_APPROVAL:
            return {
                "is_trusted": False,
                "status": "PENDING_APPROVAL",
                "error_code": "ERR_DEVICE_NOT_APPROVED",
                "message_en": f"Device '{device_id}' is pending manager/owner approval.",
                "message_bn": f"ডিভাইস '{device_id}' ব্যবস্থাপকের অনুমোদনের অপেক্ষায় রয়েছে।",
                "registration_id": reg.registration_id,
            }

        if reg.status == DeviceRegistrationStatus.REJECTED:
            return {
                "is_trusted": False,
                "status": "REJECTED",
                "error_code": "ERR_DEVICE_REJECTED",
                "message_en": f"Device '{device_id}' registration was rejected.",
                "message_bn": f"ডিভাইস '{device_id}'-এর রেজিস্ট্রেশন প্রত্যাখ্যান করা হয়েছে।",
                "rejection_reason": reg.rejection_reason,
            }

        if reg.status == DeviceRegistrationStatus.REVOKED:
            return {
                "is_trusted": False,
                "status": "REVOKED",
                "error_code": "ERR_DEVICE_REVOKED",
                "message_en": f"Device '{device_id}' cryptographic trust has been revoked.",
                "message_bn": f"ডিভাইস '{device_id}'-এর ক্রিপ্টোগ্রাফিক অনুমোদন বাতিল করা হয়েছে।",
                "revocation_reason": reg.revocation_reason,
            }

        # Check public key fingerprint if key provided
        if public_key_pem:
            fingerprint = cls._compute_key_fingerprint(public_key_pem)
            if fingerprint != reg.key_fingerprint:
                return {
                    "is_trusted": False,
                    "status": "KEY_MISMATCH",
                    "error_code": "ERR_DEVICE_KEY_MISMATCH",
                    "message_en": "Cryptographic public key does not match enrolled device key material.",
                    "message_bn": "প্রদানকৃত পাবলিক কি নিবন্ধিত ডিভাইসের কি ম্যাটেরিয়ালের সাথে মিলছে না।",
                }

        # Update last seen timestamp
        reg.last_seen_at = timezone.now()
        if ip_address:
            reg.ip_address = ip_address
        reg.save(update_fields=["last_seen_at", "ip_address"])

        return {
            "is_trusted": True,
            "status": "APPROVED",
            "registration_id": reg.registration_id,
            "device_id": reg.device_id,
            "device_label": reg.device_label,
            "branch_id": reg.branch_id,
            "branch_name": reg.branch.name,
            "key_fingerprint": reg.key_fingerprint,
            "message_en": "Device is trusted and authorized for terminal operations.",
            "message_bn": "ডিভাইসটি সফলভাবে যাচাইকৃত এবং টার্মিনাল ব্যবহারের জন্য অনুমোদিত।",
        }

    # -------------------------------------------------------------------------
    # 6. UPDATE LABEL
    # -------------------------------------------------------------------------

    @classmethod
    def update_device_label(
        cls,
        registration_id: str,
        actor_user: Any,
        new_label: str,
    ) -> LoginDeviceRegistration:
        """Updates human-readable device label."""
        clean_label = str(new_label or "").strip()
        if not clean_label:
            raise ValidationError({"error_code": "MISSING_LABEL", "message": "new_label cannot be empty."})

        try:
            reg = LoginDeviceRegistration.objects.get(registration_id=registration_id)
        except LoginDeviceRegistration.DoesNotExist:
            raise ValidationError({"error_code": "REGISTRATION_NOT_FOUND", "message": "Device registration not found."})

        cls._assert_approval_authority(actor_user, reg)

        actor_name = getattr(actor_user, "email", None) or getattr(actor_user, "username", "system")

        with transaction.atomic():
            old_label = reg.device_label
            reg.device_label = clean_label
            reg.save(update_fields=["device_label", "updated_at"])

            # Also sync label on DeviceSeat
            DeviceSeat.objects.filter(tenant=reg.tenant, device_id=reg.device_id).update(
                device_name=clean_label
            )

            DeviceRegistrationAuditEvent.objects.create(
                event_id=f"dev-audit-{uuid.uuid4().hex[:12]}",
                tenant=reg.tenant,
                device_registration=reg,
                action="LABEL_UPDATED",
                actor_username=actor_name,
                details={"old_label": old_label, "new_label": clean_label},
            )

        return reg

