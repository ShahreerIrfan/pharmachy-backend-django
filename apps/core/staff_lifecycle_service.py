"""
backend/apps/core/staff_lifecycle_service.py
Architecture Decision: ADR-0005, ADR-0015, ADR-0033, ADR-0082, ADR-0102, ADR-0105, ADR-0106
Phase 06: Milestone M03 - Staff Lifecycle Management (P106)
Requirement Trace: FR-031, FR-032, FR-049, FR-070, FR-071, FR-082

Authoritative business logic for:
1. Staff direct provisioning (create) with subscription quota safeguards.
2. Staff updates with role and branch synchronizations.
3. Soft deactivation (suspension) with instant session revocation.
4. Reactivation with plan seat quota validation.
5. Permanent termination (separation of employment) with cascaded branch membership,
   session, offline lease, and MFA revocations (strictly preserving financial/Rx audit records).
6. Administrative password/credential reset with old session invalidation.
7. Administrative MFA recovery and authenticator re-enrollment clearance.
8. Explicit session and POS device credential revocations.
9. Immutable lifecycle audit event logging.
"""

from __future__ import annotations

import datetime
import logging
import re
import secrets
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    BranchStaffMembership,
    DeviceLeaseCertificate,
    OfflineLeaseStatus,
    StaffLifecycleEvent,
    StaffLifecycleEventType,
    StaffLifecycleStatus,
    StaffMember,
    StaffMembership,
    StaffRole,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.postgres_service import CrossTenantOperationError
from apps.core.quota_guard_service import (
    SubscriptionQuotaGuardService,
)
from apps.identity.models import (
    ActivationStatus,
    MfaChallengeSession,
    PasswordResetToken,
    PharmacyUser,
    UserMfaDevice,
    UserRole,
    UserSession,
)

logger = logging.getLogger("pharmacy.staff_lifecycle")


class StaffLifecycleService:
    """Authoritative service for managing pharmacy staff lifecycle transitions and security revocations."""

    VALID_ROLES = {
        StaffRole.PLATFORM_SUPER_ADMIN: UserRole.SUPERADMIN,
        StaffRole.TENANT_OWNER: UserRole.PHARMACY_OWNER,
        StaffRole.BRANCH_MANAGER: UserRole.BRANCH_MANAGER,
        StaffRole.PHARMACIST: UserRole.STAFF_PHARMACIST,
        StaffRole.CASHIER: UserRole.CASHIER,
        StaffRole.AUDITOR: UserRole.AUDITOR,
        "PHARMACY_OWNER": UserRole.PHARMACY_OWNER,
        "STAFF_PHARMACIST": UserRole.STAFF_PHARMACIST,
        "INVENTORY_CLERK": UserRole.INVENTORY_CLERK,
        "FINANCE_OFFICER": UserRole.FINANCE_OFFICER,
    }

    @classmethod
    def _map_role_to_user_role(cls, role: str) -> str:
        clean = str(role or "").upper()
        return cls.VALID_ROLES.get(clean, UserRole.CASHIER)

    @classmethod
    def _get_tenant(cls, tenant_id: str) -> Tenant:
        try:
            return Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise CrossTenantOperationError(f"Tenant '{tenant_id}' does not exist.")

    @classmethod
    def _check_subscription_active(cls, tenant: Tenant) -> None:
        """Verifies tenant subscription is active and not suspended or archived."""
        if tenant.status in [TenantLifecycleStatus.SUSPENDED, TenantLifecycleStatus.ARCHIVED]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUBSCRIPTION_INACTIVE",
                    "message_en": f"Tenant subscription is {tenant.status}. Staff lifecycle mutations are prohibited.",
                    "message_bn": "টেন্যান্ট সাবস্ক্রিপশন স্থগিত বা নিষ্ক্রিয় থাকায় স্টাফ সংক্রান্ত পরিবর্তন সম্ভব নয়।",
                }
            )

    @classmethod
    def _record_event(
        cls,
        tenant: Tenant,
        staff_member: StaffMember,
        event_type: str,
        performed_by_username: str = "",
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        ip_address: str = "",
    ) -> StaffLifecycleEvent:
        event_id = f"evt-{uuid.uuid4().hex[:12]}"
        return StaffLifecycleEvent.objects.create(
            event_id=event_id,
            tenant=tenant,
            staff_member=staff_member,
            event_type=event_type,
            performed_by_username=(performed_by_username or "").strip(),
            reason=(reason or "").strip(),
            metadata=metadata or {},
            ip_address=ip_address or "",
        )

    @classmethod
    def _get_linked_user(cls, tenant_id: str, sm: StaffMember) -> Optional[PharmacyUser]:
        u = PharmacyUser.objects.filter(tenant_id=tenant_id, email__iexact=sm.username).first()
        if u:
            return u
        u = PharmacyUser.objects.filter(tenant_id=tenant_id, email__istartswith=f"{sm.username}@").first()
        if u:
            return u
        created_evt = StaffLifecycleEvent.objects.filter(
            staff_member=sm,
            event_type=StaffLifecycleEventType.CREATED,
        ).first()
        if created_evt and created_evt.metadata.get("email"):
            u = PharmacyUser.objects.filter(tenant_id=tenant_id, email__iexact=created_evt.metadata["email"]).first()
            if u:
                return u
        if sm.mobile_number:
            u = PharmacyUser.objects.filter(tenant_id=tenant_id, phone_number=sm.mobile_number).first()
            if u:
                return u
        return PharmacyUser.objects.filter(tenant_id=tenant_id, full_name=sm.full_name).first()

    @classmethod
    def _validate_email(cls, email: str) -> str:
        clean = (email or "").strip().lower()
        if not clean or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", clean):
            raise ValidationError(
                {"error_code": "INVALID_EMAIL", "message": f"'{email}' is not a valid email address."}
            )
        return clean

    @classmethod
    def create_staff(
        cls,
        tenant_id: str,
        username: str,
        email: str,
        full_name: str,
        role: str,
        branch_id: Optional[str] = None,
        mobile_number: str = "",
        pcb_registration_no: str = "",
        password: Optional[str] = None,
        performed_by_username: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Directly provisions a staff member within tenant subject to plan quota limits.
        Synchronizes StaffMember, StaffMembership, and PharmacyUser.
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        clean_username = (username or "").strip().lower()
        if not clean_username or len(clean_username) < 3:
            raise ValidationError(
                {"error_code": "INVALID_USERNAME", "message": "Username must be at least 3 characters."}
            )

        clean_email = cls._validate_email(email)
        clean_full_name = (full_name or "").strip()
        if len(clean_full_name) < 2:
            raise ValidationError(
                {"error_code": "INVALID_FULL_NAME", "message": "Full name must be at least 2 characters."}
            )

        role_clean = str(role or "").upper()
        if role_clean not in cls.VALID_ROLES:
            raise ValidationError(
                {"error_code": "INVALID_ROLE", "message": f"'{role}' is not an authorized staff role."}
            )

        # 1. Quota Check
        SubscriptionQuotaGuardService.check_staff_creation_allowed(tenant_id)

        # Unique username check within tenant
        if StaffMember.objects.filter(tenant=tenant, username=clean_username).exists():
            raise ValidationError(
                {"error_code": "USERNAME_ALREADY_EXISTS", "message": f"Username '{clean_username}' already exists in tenant."}
            )

        # Unique email check in PharmacyUser
        if PharmacyUser.objects.filter(email=clean_email).exists():
            raise ValidationError(
                {"error_code": "EMAIL_ALREADY_EXISTS", "message": f"User with email '{clean_email}' already exists."}
            )

        branch = None
        if branch_id:
            try:
                branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
            except Branch.DoesNotExist:
                raise CrossTenantOperationError(
                    f"Branch '{branch_id}' does not exist for tenant '{tenant_id}'."
                )

        with transaction.atomic():
            phone_val = (mobile_number or "").strip()
            if not phone_val:
                phone_val = f"+88017{secrets.randbelow(90000000) + 10000000}"

            staff_id = f"stf-{uuid.uuid4().hex[:12]}"
            staff_member = StaffMember.objects.create(
                staff_id=staff_id,
                tenant=tenant,
                branch=branch,
                username=clean_username,
                full_name=clean_full_name,
                role=role_clean,
                mobile_number=phone_val,
                pcb_registration_no=(pcb_registration_no or "").strip(),
                lifecycle_status=StaffLifecycleStatus.ACTIVE,
                is_active=True,
            )

            # StaffMembership
            membership_id = f"mem-{uuid.uuid4().hex[:12]}"
            membership = StaffMembership.objects.create(
                membership_id=membership_id,
                tenant=tenant,
                staff_member=staff_member,
                primary_branch=branch,
                role=role_clean,
                is_active=True,
            )
            if branch:
                membership.assigned_branches.add(branch)

            # PharmacyUser
            user_role = cls._map_role_to_user_role(role_clean)
            initial_password = password or f"Pharma@{secrets.token_urlsafe(8)}#"
            first_branch = tenant.branches.first()
            default_branch_id = (
                branch.branch_id
                if branch
                else (first_branch.branch_id if first_branch else "br-main")
            )

            user = PharmacyUser.objects.create_user(
                email=clean_email,
                password=initial_password,
                full_name=clean_full_name,
                phone_number=phone_val,
                role=user_role,
                tenant_id=tenant_id,
                branch_id=default_branch_id,
                pcb_registration_no=(pcb_registration_no or "").strip(),
                is_active=True,
            )

            cls._record_event(
                tenant=tenant,
                staff_member=staff_member,
                event_type=StaffLifecycleEventType.CREATED,
                performed_by_username=performed_by_username,
                reason="Direct staff onboarding and provisioning",
                metadata={
                    "email": clean_email,
                    "role": role_clean,
                    "branch_id": branch.branch_id if branch else None,
                    "user_id": str(user.pk),
                },
                ip_address=ip_address,
            )

        logger.info("Successfully provisioned staff %s (%s) for tenant %s", staff_id, clean_username, tenant_id)
        result = cls.get_staff_detail(tenant_id=tenant_id, staff_id=staff_id)
        result["temporary_password"] = initial_password if not password else None
        return result

    @classmethod
    def update_staff(
        cls,
        tenant_id: str,
        staff_id: str,
        full_name: Optional[str] = None,
        role: Optional[str] = None,
        branch_id: Optional[str] = None,
        mobile_number: Optional[str] = None,
        pcb_registration_no: Optional[str] = None,
        performed_by_username: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """Updates staff profile and synchronizes corresponding membership and user records."""
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            sm = StaffMember.objects.select_related("branch").get(staff_id=staff_id, tenant=tenant)
        except StaffMember.DoesNotExist:
            raise CrossTenantOperationError(f"Staff member '{staff_id}' not found for tenant '{tenant_id}'.")

        if sm.lifecycle_status == StaffLifecycleStatus.TERMINATED:
            raise ValidationError(
                {"error_code": "CANNOT_UPDATE_TERMINATED", "message": "Cannot update a terminated staff member."}
            )

        changes: Dict[str, Any] = {}

        if full_name is not None:
            clean_name = full_name.strip()
            if len(clean_name) < 2:
                raise ValidationError({"error_code": "INVALID_FULL_NAME", "message": "Full name must be at least 2 characters."})
            sm.full_name = clean_name
            changes["full_name"] = clean_name

        if mobile_number is not None:
            sm.mobile_number = mobile_number.strip()
            changes["mobile_number"] = sm.mobile_number

        if pcb_registration_no is not None:
            sm.pcb_registration_no = pcb_registration_no.strip()
            changes["pcb_registration_no"] = sm.pcb_registration_no

        if branch_id is not None:
            if branch_id == "":
                sm.branch = None
                changes["branch_id"] = None
            else:
                try:
                    new_branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
                    sm.branch = new_branch
                    changes["branch_id"] = new_branch.branch_id
                except Branch.DoesNotExist:
                    raise CrossTenantOperationError(f"Branch '{branch_id}' not found for tenant '{tenant_id}'.")

        if role is not None:
            clean_role = role.strip().upper()
            if clean_role not in cls.VALID_ROLES:
                raise ValidationError({"error_code": "INVALID_ROLE", "message": f"'{role}' is not an authorized role."})
            sm.role = clean_role
            changes["role"] = clean_role

        with transaction.atomic():
            sm.save()

            # Update StaffMembership
            mem = StaffMembership.objects.filter(staff_member=sm, tenant=tenant).first()
            if mem:
                if "role" in changes:
                    mem.role = changes["role"]
                if "branch_id" in changes:
                    mem.primary_branch = sm.branch
                    if sm.branch:
                        mem.assigned_branches.add(sm.branch)
                mem.save()

            # Update PharmacyUser
            user = cls._get_linked_user(tenant_id, sm)

            if user:
                if "full_name" in changes:
                    user.full_name = changes["full_name"]
                if "mobile_number" in changes:
                    user.phone_number = changes["mobile_number"]
                if "role" in changes:
                    user.role = cls._map_role_to_user_role(changes["role"])
                if "branch_id" in changes and sm.branch:
                    user.branch_id = sm.branch.branch_id
                if "pcb_registration_no" in changes:
                    user.pcb_registration_no = changes["pcb_registration_no"]
                user.save()

            cls._record_event(
                tenant=tenant,
                staff_member=sm,
                event_type=StaffLifecycleEventType.UPDATED,
                performed_by_username=performed_by_username,
                reason="Staff details updated",
                metadata=changes,
                ip_address=ip_address,
            )

        logger.info("Updated staff member %s (%s)", staff_id, changes)
        return cls.get_staff_detail(tenant_id=tenant_id, staff_id=staff_id)

    @classmethod
    def deactivate_staff(
        cls,
        tenant_id: str,
        staff_id: str,
        reason: str = "",
        performed_by_username: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Temporarily deactivates/suspends a staff member.
        Security Invariant: Automatically revokes all active UserSession records.
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            sm = StaffMember.objects.get(staff_id=staff_id, tenant=tenant)
        except StaffMember.DoesNotExist:
            raise CrossTenantOperationError(f"Staff member '{staff_id}' not found for tenant '{tenant_id}'.")

        if sm.lifecycle_status == StaffLifecycleStatus.TERMINATED:
            raise ValidationError(
                {"error_code": "CANNOT_DEACTIVATE_TERMINATED", "message": "Cannot deactivate an already terminated staff member."}
            )

        now = timezone.now()
        clean_reason = (reason or "Administrative staff deactivation").strip()

        with transaction.atomic():
            sm.lifecycle_status = StaffLifecycleStatus.DEACTIVATED
            sm.is_active = False
            sm.deactivated_at = now
            sm.deactivated_by = (performed_by_username or "").strip()
            sm.deactivation_reason = clean_reason
            sm.save()

            StaffMembership.objects.filter(staff_member=sm, tenant=tenant).update(
                is_active=False,
                deactivated_at=now,
                deactivation_reason=clean_reason,
            )

            # User & Session Revocation
            user = cls._get_linked_user(tenant_id, sm)

            revoked_sessions = 0
            if user:
                user.is_active = False
                user.activation_status = ActivationStatus.SUSPENDED
                user.save()

                revoked_sessions = UserSession.objects.filter(user=user, is_active=True).update(
                    is_active=False,
                    revocation_reason=f"STAFF_DEACTIVATION: {clean_reason}",
                    revoked_at=now,
                )

            cls._record_event(
                tenant=tenant,
                staff_member=sm,
                event_type=StaffLifecycleEventType.DEACTIVATED,
                performed_by_username=performed_by_username,
                reason=clean_reason,
                metadata={"revoked_sessions_count": revoked_sessions},
                ip_address=ip_address,
            )

        logger.info("Deactivated staff %s. Revoked %d active sessions.", staff_id, revoked_sessions)
        return cls.get_staff_detail(tenant_id=tenant_id, staff_id=staff_id)

    @classmethod
    def reactivate_staff(
        cls,
        tenant_id: str,
        staff_id: str,
        reason: str = "",
        performed_by_username: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Reactivates a suspended/deactivated staff member subject to plan quota validation.
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            sm = StaffMember.objects.get(staff_id=staff_id, tenant=tenant)
        except StaffMember.DoesNotExist:
            raise CrossTenantOperationError(f"Staff member '{staff_id}' not found for tenant '{tenant_id}'.")

        if sm.lifecycle_status == StaffLifecycleStatus.TERMINATED:
            raise ValidationError(
                {
                    "error_code": "CANNOT_REACTIVATE_TERMINATED",
                    "message": "Terminated staff cannot be reactivated directly; please provision a new account.",
                }
            )

        # Quota Safeguard
        SubscriptionQuotaGuardService.check_staff_creation_allowed(tenant_id)

        clean_reason = (reason or "Staff reactivated by administrator").strip()

        with transaction.atomic():
            sm.lifecycle_status = StaffLifecycleStatus.ACTIVE
            sm.is_active = True
            sm.deactivated_at = None
            sm.deactivated_by = ""
            sm.deactivation_reason = ""
            sm.save()

            StaffMembership.objects.filter(staff_member=sm, tenant=tenant).update(
                is_active=True,
                deactivated_at=None,
                deactivation_reason="",
            )

            user = cls._get_linked_user(tenant_id, sm)

            if user:
                user.is_active = True
                user.activation_status = ActivationStatus.ACTIVE
                user.save()

            cls._record_event(
                tenant=tenant,
                staff_member=sm,
                event_type=StaffLifecycleEventType.REACTIVATED,
                performed_by_username=performed_by_username,
                reason=clean_reason,
                metadata={},
                ip_address=ip_address,
            )

        logger.info("Reactivated staff %s", staff_id)
        return cls.get_staff_detail(tenant_id=tenant_id, staff_id=staff_id)

    @classmethod
    def terminate_staff(
        cls,
        tenant_id: str,
        staff_id: str,
        reason: str,
        performed_by_username: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Permanently terminates/separates a staff member.
        Hard DELETE is forbidden (preserves dispensing, sales, and ledger records).
        Cascades:
        - Inactivates StaffMember & StaffMembership
        - Revokes all BranchStaffMembership
        - Inactivates and archives PharmacyUser
        - Revokes ALL active UserSession records
        - Revokes ALL DeviceLeaseCertificate offline leases
        - Clears/disables UserMfaDevice
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        clean_reason = (reason or "").strip()
        if not clean_reason or len(clean_reason) < 4:
            raise ValidationError(
                {"error_code": "TERMINATION_REASON_REQUIRED", "message": "A detailed termination reason (minimum 4 characters) is required."}
            )

        try:
            sm = StaffMember.objects.get(staff_id=staff_id, tenant=tenant)
        except StaffMember.DoesNotExist:
            raise CrossTenantOperationError(f"Staff member '{staff_id}' not found for tenant '{tenant_id}'.")

        now = timezone.now()

        with transaction.atomic():
            sm.lifecycle_status = StaffLifecycleStatus.TERMINATED
            sm.is_active = False
            sm.terminated_at = now
            sm.terminated_by = (performed_by_username or "").strip()
            sm.termination_reason = clean_reason
            sm.save()

            StaffMembership.objects.filter(staff_member=sm, tenant=tenant).update(
                is_active=False,
                deactivated_at=now,
                deactivation_reason=f"TERMINATED: {clean_reason}",
            )

            # Revoke all branch memberships
            BranchStaffMembership.objects.filter(tenant=tenant, staff_member=sm, is_active=True).update(
                is_active=False,
                revoked_at=now,
                revoked_by_username=performed_by_username,
                revocation_reason=f"STAFF_TERMINATED: {clean_reason}",
            )

            # User, Sessions, Leases, and MFA
            user = cls._get_linked_user(tenant_id, sm)

            revoked_sessions = 0
            revoked_leases = 0

            if user:
                user.archive(by_user=performed_by_username, reason=clean_reason)

                revoked_sessions = UserSession.objects.filter(user=user, is_active=True).update(
                    is_active=False,
                    revocation_reason=f"STAFF_TERMINATED: {clean_reason}",
                    revoked_at=now,
                )

                revoked_leases = DeviceLeaseCertificate.objects.filter(
                    tenant=tenant,
                    user_email=user.email,
                    status=OfflineLeaseStatus.ACTIVE,
                ).update(
                    status=OfflineLeaseStatus.REVOKED,
                    revoked_at=now,
                    revocation_reason=f"STAFF_TERMINATED: {clean_reason}",
                )

                UserMfaDevice.objects.filter(user=user).delete()
                user.mfa_enabled = False
                user.save()

            cls._record_event(
                tenant=tenant,
                staff_member=sm,
                event_type=StaffLifecycleEventType.TERMINATED,
                performed_by_username=performed_by_username,
                reason=clean_reason,
                metadata={
                    "revoked_sessions_count": revoked_sessions,
                    "revoked_leases_count": revoked_leases,
                },
                ip_address=ip_address,
            )

        logger.info(
            "Terminated staff %s. Revoked %d sessions, %d offline leases.",
            staff_id,
            revoked_sessions,
            revoked_leases,
        )
        return cls.get_staff_detail(tenant_id=tenant_id, staff_id=staff_id)

    @classmethod
    def reset_credentials(
        cls,
        tenant_id: str,
        staff_id: str,
        new_password: Optional[str] = None,
        performed_by_username: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Administrative password reset for a staff member.
        Security Invariant: Automatically invalidates all active sessions.
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            sm = StaffMember.objects.get(staff_id=staff_id, tenant=tenant)
        except StaffMember.DoesNotExist:
            raise CrossTenantOperationError(f"Staff member '{staff_id}' not found for tenant '{tenant_id}'.")

        if sm.lifecycle_status == StaffLifecycleStatus.TERMINATED:
            raise ValidationError(
                {"error_code": "CANNOT_RESET_TERMINATED", "message": "Cannot reset credentials for a terminated staff member."}
            )

        user = cls._get_linked_user(tenant_id, sm)

        if not user:
            raise ValidationError(
                {"error_code": "USER_NOT_FOUND", "message": f"Associated login user not found for staff '{staff_id}'."}
            )

        generated_pass = new_password or f"TmpPass@{secrets.token_urlsafe(8)}#"
        now = timezone.now()

        with transaction.atomic():
            user.set_password(generated_pass)
            user.failed_login_attempts = 0
            user.lockout_until = None
            user.password_changed_at = now
            user.save()

            # Revoke all active sessions so stale/compromised tokens are invalidated
            revoked_sessions = UserSession.objects.filter(user=user, is_active=True).update(
                is_active=False,
                revocation_reason="ADMIN_CREDENTIAL_RESET",
                revoked_at=now,
            )

            # Generate password reset token
            reset_token_str = secrets.token_urlsafe(32)
            PasswordResetToken.objects.create(
                user=user,
                token=reset_token_str,
                otp_code=f"{secrets.randbelow(900000) + 100000}",
                expires_at=now + datetime.timedelta(hours=24),
            )

            sm.last_credential_reset_at = now
            sm.save()

            cls._record_event(
                tenant=tenant,
                staff_member=sm,
                event_type=StaffLifecycleEventType.CREDENTIALS_RESET,
                performed_by_username=performed_by_username,
                reason="Administrative password reset",
                metadata={"revoked_sessions_count": revoked_sessions},
                ip_address=ip_address,
            )

        logger.info("Reset credentials for staff %s (%s). Revoked %d sessions.", staff_id, sm.username, revoked_sessions)
        return {
            "staff_id": staff_id,
            "username": sm.username,
            "email": user.email,
            "temporary_password": generated_pass,
            "reset_token": reset_token_str,
            "revoked_sessions_count": revoked_sessions,
            "reset_at": now.isoformat(),
        }

    @classmethod
    def mfa_recovery(
        cls,
        tenant_id: str,
        staff_id: str,
        reason: str = "",
        performed_by_username: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Administrative MFA recovery / reset for staff member locked out of authenticator.
        Clears existing MFA device and disables MFA to allow re-enrollment.
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            sm = StaffMember.objects.get(staff_id=staff_id, tenant=tenant)
        except StaffMember.DoesNotExist:
            raise CrossTenantOperationError(f"Staff member '{staff_id}' not found for tenant '{tenant_id}'.")

        user = cls._get_linked_user(tenant_id, sm)

        if not user:
            raise ValidationError(
                {"error_code": "USER_NOT_FOUND", "message": f"Associated login user not found for staff '{staff_id}'."}
            )

        now = timezone.now()
        clean_reason = (reason or "Administrative MFA device recovery").strip()

        with transaction.atomic():
            # Delete MFA device configuration
            deleted_count, _ = UserMfaDevice.objects.filter(user=user).delete()
            user.mfa_enabled = False
            user.save()

            # Delete any unverified challenge sessions
            MfaChallengeSession.objects.filter(user=user, is_verified=False).delete()

            sm.last_mfa_reset_at = now
            sm.save()

            cls._record_event(
                tenant=tenant,
                staff_member=sm,
                event_type=StaffLifecycleEventType.MFA_RESET,
                performed_by_username=performed_by_username,
                reason=clean_reason,
                metadata={"deleted_mfa_devices_count": deleted_count},
                ip_address=ip_address,
            )

        logger.info("MFA recovery executed for staff %s (%s). Cleared %d device configs.", staff_id, sm.username, deleted_count)
        return {
            "staff_id": staff_id,
            "username": sm.username,
            "email": user.email,
            "mfa_enabled": False,
            "cleared_devices_count": deleted_count,
            "recovered_at": now.isoformat(),
            "message_en": "MFA configuration successfully reset. Staff member can now log in and re-enroll their authenticator.",
            "message_bn": "এমএফএ সফলভাবে রিসেট করা হয়েছে। স্টাফ মেম্বার এখন লগইন করে পুনরায় অথেন্টিকেটর সেটআপ করতে পারবেন।",
        }

    @classmethod
    def revoke_sessions(
        cls,
        tenant_id: str,
        staff_id: str,
        reason: str = "ADMIN_REVOCATION",
        performed_by_username: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """Immediately revokes all active client sessions across Desktop POS, Mobile, and Web Admin."""
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            sm = StaffMember.objects.get(staff_id=staff_id, tenant=tenant)
        except StaffMember.DoesNotExist:
            raise CrossTenantOperationError(f"Staff member '{staff_id}' not found for tenant '{tenant_id}'.")

        user = cls._get_linked_user(tenant_id, sm)

        now = timezone.now()
        revoked_count = 0
        if user:
            revoked_count = UserSession.objects.filter(user=user, is_active=True).update(
                is_active=False,
                revocation_reason=reason,
                revoked_at=now,
            )

        sm.last_session_revocation_at = now
        sm.save()

        cls._record_event(
            tenant=tenant,
            staff_member=sm,
            event_type=StaffLifecycleEventType.SESSIONS_REVOKED,
            performed_by_username=performed_by_username,
            reason=reason,
            metadata={"revoked_sessions_count": revoked_count},
            ip_address=ip_address,
        )

        logger.info("Revoked %d sessions for staff %s", revoked_count, staff_id)
        return {
            "staff_id": staff_id,
            "revoked_sessions_count": revoked_count,
            "revoked_at": now.isoformat(),
        }

    @classmethod
    def revoke_device_credentials(
        cls,
        tenant_id: str,
        staff_id: str,
        reason: str = "DEVICE_CREDENTIALS_REVOKED",
        performed_by_username: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """Invalidates all offline device lease certificates associated with this staff member."""
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            sm = StaffMember.objects.get(staff_id=staff_id, tenant=tenant)
        except StaffMember.DoesNotExist:
            raise CrossTenantOperationError(f"Staff member '{staff_id}' not found for tenant '{tenant_id}'.")

        user = cls._get_linked_user(tenant_id, sm)

        now = timezone.now()
        revoked_count = 0
        if user:
            revoked_count = DeviceLeaseCertificate.objects.filter(
                tenant=tenant,
                user_email=user.email,
                status=OfflineLeaseStatus.ACTIVE,
            ).update(
                status=OfflineLeaseStatus.REVOKED,
                revoked_at=now,
                revocation_reason=reason,
            )

        cls._record_event(
            tenant=tenant,
            staff_member=sm,
            event_type=StaffLifecycleEventType.DEVICE_REVOKED,
            performed_by_username=performed_by_username,
            reason=reason,
            metadata={"revoked_leases_count": revoked_count},
            ip_address=ip_address,
        )

        logger.info("Revoked %d device leases for staff %s", revoked_count, staff_id)
        return {
            "staff_id": staff_id,
            "revoked_leases_count": revoked_count,
            "revoked_at": now.isoformat(),
        }

    @classmethod
    def get_staff_detail(cls, tenant_id: str, staff_id: str) -> Dict[str, Any]:
        """Returns comprehensive staff profile matching staff_lifecycle.schema.json."""
        tenant = cls._get_tenant(tenant_id)
        try:
            sm = StaffMember.objects.select_related("branch").get(staff_id=staff_id, tenant=tenant)
        except StaffMember.DoesNotExist:
            raise CrossTenantOperationError(f"Staff member '{staff_id}' not found for tenant '{tenant_id}'.")

        user = cls._get_linked_user(tenant_id, sm)

        active_sessions = UserSession.objects.filter(user=user, is_active=True).count() if user else 0
        active_leases = (
            DeviceLeaseCertificate.objects.filter(
                tenant=tenant,
                user_email=user.email,
                status=OfflineLeaseStatus.ACTIVE,
            ).count()
            if user
            else 0
        )
        mfa_enabled = bool(user and user.mfa_enabled)

        return {
            "staff_id": sm.staff_id,
            "tenant_id": sm.tenant_id,
            "branch_id": sm.branch.branch_id if sm.branch else None,
            "branch_name": sm.branch.name if sm.branch else None,
            "username": sm.username,
            "email": user.email if user else None,
            "full_name": sm.full_name,
            "role": sm.role,
            "mobile_number": sm.mobile_number,
            "pcb_registration_no": sm.pcb_registration_no,
            "lifecycle_status": sm.lifecycle_status,
            "is_active": sm.is_active,
            "mfa_enabled": mfa_enabled,
            "active_sessions_count": active_sessions,
            "active_leases_count": active_leases,
            "created_at": sm.created_at.isoformat(),
            "deactivated_at": sm.deactivated_at.isoformat() if sm.deactivated_at else None,
            "terminated_at": sm.terminated_at.isoformat() if sm.terminated_at else None,
            "last_credential_reset_at": sm.last_credential_reset_at.isoformat() if sm.last_credential_reset_at else None,
            "last_mfa_reset_at": sm.last_mfa_reset_at.isoformat() if sm.last_mfa_reset_at else None,
        }

    @classmethod
    def list_staff(
        cls,
        tenant_id: str,
        branch_id: Optional[str] = None,
        role: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Lists staff members within tenant with optional filters."""
        tenant = cls._get_tenant(tenant_id)
        qs = StaffMember.objects.select_related("branch").filter(tenant=tenant)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if role:
            qs = qs.filter(role=role.upper())
        if lifecycle_status:
            qs = qs.filter(lifecycle_status=lifecycle_status.upper())
        if is_active is not None:
            qs = qs.filter(is_active=is_active)

        return [cls.get_staff_detail(tenant_id=tenant_id, staff_id=s.staff_id) for s in qs]

    @classmethod
    def list_lifecycle_events(cls, tenant_id: str, staff_id: str) -> List[Dict[str, Any]]:
        """Returns immutable audit event history for a staff member."""
        tenant = cls._get_tenant(tenant_id)
        try:
            sm = StaffMember.objects.get(staff_id=staff_id, tenant=tenant)
        except StaffMember.DoesNotExist:
            raise CrossTenantOperationError(f"Staff member '{staff_id}' not found for tenant '{tenant_id}'.")

        events = StaffLifecycleEvent.objects.filter(tenant=tenant, staff_member=sm)
        return [
            {
                "event_id": e.event_id,
                "event_type": e.event_type,
                "performed_by": e.performed_by_username,
                "reason": e.reason,
                "metadata": e.metadata,
                "ip_address": e.ip_address,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ]
