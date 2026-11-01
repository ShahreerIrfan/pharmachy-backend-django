"""
Staff Membership & Invitation Service (P102 / ADR-0102).
Handles owner and branch-scoped invitations, accepted memberships, resend, revocation,
token expiration, role-based branch scopes, and subscription quota safeguards.
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
from rest_framework.exceptions import ValidationError

from apps.core.models import (
    Branch,
    InvitationStatus,
    StaffInvitation,
    StaffMember,
    StaffMembership,
    StaffRole,
    Tenant,
)
from apps.core.postgres_service import CrossTenantOperationError
from apps.core.quota_guard_service import (
    SubscriptionQuotaGuardService,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.staff_membership")


class StaffMembershipService:
    """Authoritative business logic for tenant staff onboarding, invitations, and memberships."""

    @staticmethod
    def _map_staff_role_to_user_role(role: str) -> str:
        role_map = {
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
        return role_map.get(role, UserRole.CASHIER)

    @classmethod
    def _validate_email(cls, email: str) -> str:
        clean = (email or "").strip().lower()
        if not clean or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", clean):
            raise ValidationError(
                {"error_code": "INVALID_EMAIL", "message": f"'{email}' is not a valid email address."}
            )
        return clean

    @classmethod
    def _get_tenant(cls, tenant_id: str) -> Tenant:
        try:
            return Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise CrossTenantOperationError(f"Tenant '{tenant_id}' does not exist.")

    @classmethod
    def create_invitation(
        cls,
        tenant_id: str,
        email: str,
        full_name: str,
        role: str,
        branch_id: Optional[str] = None,
        custom_role_title: str = "",
        pcb_registration_no: str = "",
        phone_number: str = "",
        invited_by_username: str = "",
    ) -> Dict[str, Any]:
        """Creates a pending staff invitation gated by subscription quotas and branch validity."""
        clean_email = cls._validate_email(email)
        clean_full_name = (full_name or "").strip()
        if len(clean_full_name) < 2:
            raise ValidationError(
                {"error_code": "INVALID_NAME", "message": "Full name must be at least 2 characters."}
            )

        role_clean = str(role or "").upper()
        if role_clean not in StaffRole.values and role_clean not in [
            "PHARMACY_OWNER",
            "STAFF_PHARMACIST",
            "INVENTORY_CLERK",
            "FINANCE_OFFICER",
        ]:
            raise ValidationError(
                {"error_code": "INVALID_ROLE", "message": f"'{role}' is not an authorized staff role."}
            )

        tenant = cls._get_tenant(tenant_id)

        # 1. Enforce Subscription Quotas (QuotaGuard)
        SubscriptionQuotaGuardService.check_staff_creation_allowed(tenant_id)

        # 2. Check Branch Scope validity
        branch = None
        if branch_id:
            try:
                branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
            except Branch.DoesNotExist:
                raise CrossTenantOperationError(
                    f"Branch '{branch_id}' does not exist for tenant '{tenant_id}'."
                )

        # 3. Check for existing pending invitation for same email in tenant
        existing = StaffInvitation.objects.filter(
            tenant=tenant,
            email=clean_email,
            status=InvitationStatus.PENDING,
            expires_at__gt=timezone.now(),
        ).first()
        if existing:
            raise ValidationError(
                {
                    "error_code": "INVITATION_ALREADY_EXISTS",
                    "message": f"A pending invitation already exists for {clean_email}.",
                }
            )

        # 4. Generate cryptographically secure token & 7-day expiration
        token = secrets.token_urlsafe(32)
        invitation_id = f"inv-{uuid.uuid4().hex[:12]}"
        expires_at = timezone.now() + datetime.timedelta(days=7)

        invitation = StaffInvitation.objects.create(
            invitation_id=invitation_id,
            tenant=tenant,
            branch=branch,
            email=clean_email,
            phone_number=(phone_number or "").strip(),
            full_name=clean_full_name,
            role=role_clean,
            custom_role_title=(custom_role_title or "").strip(),
            pcb_registration_no=(pcb_registration_no or "").strip(),
            invite_token=token,
            status=InvitationStatus.PENDING,
            invited_by_username=(invited_by_username or "").strip(),
            expires_at=expires_at,
        )

        logger.info(
            "Created staff invitation %s for %s (%s) at tenant %s branch %s",
            invitation_id,
            clean_email,
            role_clean,
            tenant_id,
            branch_id,
        )
        return cls._serialize_invitation(invitation)

    @classmethod
    def list_invitations(
        cls,
        tenant_id: str,
        branch_id: Optional[str] = None,
        status: Optional[str] = None,
        role: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists invitations for a tenant, automatically marking past-due pending invitations as EXPIRED."""
        tenant = cls._get_tenant(tenant_id)
        now = timezone.now()

        # Mark expired
        StaffInvitation.objects.filter(
            tenant=tenant,
            status=InvitationStatus.PENDING,
            expires_at__lte=now,
        ).update(status=InvitationStatus.EXPIRED)

        qs = StaffInvitation.objects.filter(tenant=tenant)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if status:
            qs = qs.filter(status=status.upper())
        if role:
            qs = qs.filter(role=role.upper())

        return [cls._serialize_invitation(inv) for inv in qs]

    @classmethod
    def resend_invitation(
        cls,
        tenant_id: str,
        invitation_id: str,
        requester_username: str = "",
    ) -> Dict[str, Any]:
        """Refreshes invitation token, extends expiration by 7 days, and increments resend_count."""
        tenant = cls._get_tenant(tenant_id)
        try:
            inv = StaffInvitation.objects.get(invitation_id=invitation_id, tenant=tenant)
        except StaffInvitation.DoesNotExist:
            raise CrossTenantOperationError(
                f"Invitation '{invitation_id}' not found for tenant '{tenant_id}'."
            )

        if inv.status == InvitationStatus.ACCEPTED:
            raise ValidationError(
                {"error_code": "ALREADY_ACCEPTED", "message": "Cannot resend an already accepted invitation."}
            )
        if inv.status == InvitationStatus.REVOKED:
            raise ValidationError(
                {"error_code": "CANNOT_RESEND_REVOKED", "message": "Cannot resend a revoked invitation."}
            )

        # Quota check
        SubscriptionQuotaGuardService.check_staff_creation_allowed(tenant_id)

        inv.invite_token = secrets.token_urlsafe(32)
        inv.expires_at = timezone.now() + datetime.timedelta(days=7)
        inv.status = InvitationStatus.PENDING
        inv.resend_count += 1
        inv.last_resent_at = timezone.now()
        inv.save()

        logger.info(
            "Resent invitation %s to %s (attempt #%s)",
            invitation_id,
            inv.email,
            inv.resend_count,
        )
        return cls._serialize_invitation(inv)

    @classmethod
    def revoke_invitation(
        cls,
        tenant_id: str,
        invitation_id: str,
        revoked_by_username: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        """Revokes an outstanding invitation."""
        tenant = cls._get_tenant(tenant_id)
        try:
            inv = StaffInvitation.objects.get(invitation_id=invitation_id, tenant=tenant)
        except StaffInvitation.DoesNotExist:
            raise CrossTenantOperationError(
                f"Invitation '{invitation_id}' not found for tenant '{tenant_id}'."
            )

        if inv.status != InvitationStatus.PENDING and inv.status != InvitationStatus.EXPIRED:
            raise ValidationError(
                {"error_code": "CANNOT_REVOKE", "message": f"Cannot revoke invitation with status '{inv.status}'."}
            )

        inv.status = InvitationStatus.REVOKED
        inv.revoked_at = timezone.now()
        inv.revoked_by_username = (revoked_by_username or "").strip()
        inv.revocation_reason = (reason or "").strip()
        inv.save()

        logger.info(
            "Revoked invitation %s for %s. Reason: %s",
            invitation_id,
            inv.email,
            reason,
        )
        return cls._serialize_invitation(inv)

    @classmethod
    def accept_invitation(
        cls,
        invite_token: str,
        username: str,
        password: Optional[str] = None,
        phone_number: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Accepts a pending invitation, provisionally creates or activates staff and membership records."""
        clean_token = (invite_token or "").strip()
        if not clean_token:
            raise ValidationError(
                {"error_code": "ERR_INVALID_INVITATION_TOKEN", "message": "Invite token is required."}
            )

        try:
            inv = StaffInvitation.objects.select_related("tenant", "branch").get(invite_token=clean_token)
        except StaffInvitation.DoesNotExist:
            raise ValidationError(
                {"error_code": "ERR_INVALID_INVITATION_TOKEN", "message": "Invalid invitation token."}
            )

        if inv.status == InvitationStatus.REVOKED:
            raise ValidationError(
                {"error_code": "ERR_INVITATION_REVOKED", "message": "This invitation has been revoked by the pharmacy administrator."}
            )
        if inv.status == InvitationStatus.ACCEPTED:
            raise ValidationError(
                {"error_code": "ERR_INVITATION_ALREADY_ACCEPTED", "message": "This invitation has already been accepted."}
            )
        if inv.status == InvitationStatus.EXPIRED or inv.expires_at <= timezone.now():
            inv.status = InvitationStatus.EXPIRED
            inv.save()
            raise ValidationError(
                {"error_code": "ERR_INVITATION_EXPIRED", "message": "This invitation link has expired. Please request a new invite."}
            )

        tenant = inv.tenant
        # Enforce Quota
        SubscriptionQuotaGuardService.check_staff_creation_allowed(tenant.tenant_id)

        clean_username = (username or inv.email.split("@")[0]).strip()

        with transaction.atomic():
            # 1. Create or retrieve StaffMember
            staff_id = f"stf-{uuid.uuid4().hex[:12]}"
            staff_member, created = StaffMember.objects.get_or_create(
                tenant=tenant,
                username=clean_username,
                defaults={
                    "staff_id": staff_id,
                    "branch": inv.branch,
                    "full_name": inv.full_name,
                    "role": inv.role,
                    "mobile_number": phone_number or inv.phone_number or "N/A",
                    "pcb_registration_no": inv.pcb_registration_no,
                    "is_active": True,
                },
            )
            if not created:
                staff_member.branch = inv.branch
                staff_member.full_name = inv.full_name
                staff_member.role = inv.role
                staff_member.is_active = True
                staff_member.save()

            # 2. Create or link StaffMembership
            membership_id = f"mem-{uuid.uuid4().hex[:12]}"
            membership, mem_created = StaffMembership.objects.get_or_create(
                staff_member=staff_member,
                defaults={
                    "membership_id": membership_id,
                    "tenant": tenant,
                    "primary_branch": inv.branch,
                    "role": inv.role,
                    "custom_role_title": inv.custom_role_title,
                    "is_active": True,
                    "invitation": inv,
                },
            )
            if not mem_created:
                membership.primary_branch = inv.branch
                membership.role = inv.role
                membership.custom_role_title = inv.custom_role_title
                membership.is_active = True
                membership.invitation = inv
                membership.save()

            if inv.branch:
                membership.assigned_branches.add(inv.branch)

            # 3. Create or link PharmacyUser
            user_role = cls._map_staff_role_to_user_role(inv.role)
            first_branch = tenant.branches.first()
            default_branch_id = (
                inv.branch.branch_id
                if inv.branch
                else (first_branch.branch_id if first_branch else "br-main")
            )
            user, u_created = PharmacyUser.objects.get_or_create(
                email=inv.email,
                defaults={
                    "tenant_id": tenant.tenant_id,
                    "branch_id": default_branch_id,
                    "full_name": inv.full_name,
                    "role": user_role,
                    "phone_number": phone_number or inv.phone_number or "+8801700000000",
                    "pcb_registration_no": inv.pcb_registration_no,
                    "is_active": True,
                },
            )
            if password and (u_created or not user.password):
                user.set_password(password)
                user.save()

            # 4. Finalize invitation record
            inv.status = InvitationStatus.ACCEPTED
            inv.accepted_at = timezone.now()
            inv.accepted_by_username = clean_username
            inv.save()

        logger.info(
            "Staff invitation %s accepted by %s -> StaffMember %s, Membership %s",
            inv.invitation_id,
            clean_username,
            staff_member.staff_id,
            membership.membership_id,
        )
        return cls._serialize_membership(membership)

    @classmethod
    def list_memberships(
        cls,
        tenant_id: str,
        branch_id: Optional[str] = None,
        is_active: Optional[bool] = None,
        role: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists active or archived staff memberships within tenant."""
        tenant = cls._get_tenant(tenant_id)
        qs = StaffMembership.objects.select_related("staff_member", "primary_branch", "invitation").filter(tenant=tenant)
        if branch_id:
            qs = qs.filter(primary_branch_id=branch_id)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if role:
            qs = qs.filter(role=role.upper())

        return [cls._serialize_membership(mem) for mem in qs]

    @classmethod
    def update_membership_branch_scopes(
        cls,
        tenant_id: str,
        membership_id: str,
        primary_branch_id: Optional[str] = None,
        assigned_branch_ids: Optional[List[str]] = None,
        custom_role_title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Updates primary branch and authorized roving branch scopes for a staff membership."""
        tenant = cls._get_tenant(tenant_id)
        try:
            mem = StaffMembership.objects.select_related("staff_member", "primary_branch").get(
                membership_id=membership_id, tenant=tenant
            )
        except StaffMembership.DoesNotExist:
            raise CrossTenantOperationError(
                f"Membership '{membership_id}' does not exist for tenant '{tenant_id}'."
            )

        if primary_branch_id is not None:
            if primary_branch_id == "":
                mem.primary_branch = None
                mem.staff_member.branch = None
            else:
                try:
                    p_branch = Branch.objects.get(branch_id=primary_branch_id, tenant=tenant)
                    mem.primary_branch = p_branch
                    mem.staff_member.branch = p_branch
                except Branch.DoesNotExist:
                    raise CrossTenantOperationError(
                        f"Primary branch '{primary_branch_id}' not found for tenant '{tenant_id}'."
                    )
            mem.staff_member.save()

        if assigned_branch_ids is not None:
            branches = Branch.objects.filter(branch_id__in=assigned_branch_ids, tenant=tenant)
            mem.assigned_branches.set(branches)

        if custom_role_title is not None:
            mem.custom_role_title = custom_role_title.strip()

        mem.save()
        logger.info("Updated branch scopes for membership %s", membership_id)
        return cls._serialize_membership(mem)

    @classmethod
    def deactivate_membership(
        cls,
        tenant_id: str,
        membership_id: str,
        deactivated_by_username: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        """Deactivates staff membership and corresponding StaffMember / PharmacyUser records."""
        tenant = cls._get_tenant(tenant_id)
        try:
            mem = StaffMembership.objects.select_related("staff_member").get(
                membership_id=membership_id, tenant=tenant
            )
        except StaffMembership.DoesNotExist:
            raise CrossTenantOperationError(
                f"Membership '{membership_id}' does not exist for tenant '{tenant_id}'."
            )

        mem.is_active = False
        mem.deactivated_at = timezone.now()
        mem.deactivation_reason = (reason or "").strip()
        mem.save()

        # Also deactivate StaffMember
        sm = mem.staff_member
        sm.is_active = False
        sm.save()

        # Deactivate corresponding PharmacyUser if exists
        try:
            u = PharmacyUser.objects.get(tenant_id=tenant_id, email__iexact=sm.username)
            u.is_active = False
            u.save()
        except PharmacyUser.DoesNotExist:
            pass

        logger.info("Deactivated membership %s for %s", membership_id, sm.username)
        return cls._serialize_membership(mem)

    @classmethod
    def _serialize_invitation(cls, inv: StaffInvitation) -> Dict[str, Any]:
        return {
            "invitation_id": inv.invitation_id,
            "tenant_id": inv.tenant_id,
            "branch_id": inv.branch_id,
            "branch_name": inv.branch.name if inv.branch else None,
            "email": inv.email,
            "phone_number": inv.phone_number,
            "full_name": inv.full_name,
            "role": inv.role,
            "custom_role_title": inv.custom_role_title,
            "pcb_registration_no": inv.pcb_registration_no,
            "status": inv.status,
            "invite_token": inv.invite_token,
            "invited_by": inv.invited_by_username,
            "expires_at": inv.expires_at.isoformat(),
            "accepted_at": inv.accepted_at.isoformat() if inv.accepted_at else None,
            "accepted_by": inv.accepted_by_username or None,
            "revoked_at": inv.revoked_at.isoformat() if inv.revoked_at else None,
            "revocation_reason": inv.revocation_reason or None,
            "resend_count": inv.resend_count,
            "last_resent_at": inv.last_resent_at.isoformat() if inv.last_resent_at else None,
            "created_at": inv.created_at.isoformat(),
        }

    @classmethod
    def _serialize_membership(cls, mem: StaffMembership) -> Dict[str, Any]:
        assigned_branches = list(mem.assigned_branches.values_list("branch_id", flat=True))
        return {
            "membership_id": mem.membership_id,
            "tenant_id": mem.tenant_id,
            "staff_id": mem.staff_member.staff_id,
            "username": mem.staff_member.username,
            "full_name": mem.staff_member.full_name,
            "primary_branch_id": mem.primary_branch_id,
            "primary_branch_name": mem.primary_branch.name if mem.primary_branch else None,
            "assigned_branch_ids": assigned_branches,
            "role": mem.role,
            "custom_role_title": mem.custom_role_title,
            "is_active": mem.is_active,
            "invitation_id": mem.invitation_id,
            "joined_at": mem.joined_at.isoformat(),
            "deactivated_at": mem.deactivated_at.isoformat() if mem.deactivated_at else None,
            "deactivation_reason": mem.deactivation_reason or None,
        }
