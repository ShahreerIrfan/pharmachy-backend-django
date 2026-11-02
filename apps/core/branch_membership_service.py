"""
Branch-Specific Staff Membership Service (P105 / ADR-0105).
Allows staff accounts to hold different operational roles in separate authorized branches.
Provides authoritative branch-specific role assignment, resolution, and revocation.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    BranchStaffMembership,
    CustomRoleDefinition,
    StaffMember,
    StaffMembership,
    StaffRole,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.postgres_service import CrossTenantOperationError
from apps.identity.models import PharmacyUser

logger = logging.getLogger("pharmacy.branch_membership")


class BranchMembershipService:
    """Authoritative management and evaluation for branch-specific staff roles."""

    VALID_ROLES = set(StaffRole.values).union(
        {
            "PHARMACY_OWNER",
            "BRANCH_MANAGER",
            "STAFF_PHARMACIST",
            "PHARMACIST",
            "CASHIER",
            "INVENTORY_CLERK",
            "FINANCE_OFFICER",
            "AUDITOR",
        }
    )

    @classmethod
    def _get_tenant(cls, tenant_id: str) -> Tenant:
        try:
            return Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise CrossTenantOperationError(f"Tenant '{tenant_id}' does not exist.")

    @classmethod
    def _check_subscription_active(cls, tenant: Tenant) -> None:
        """Verifies tenant subscription is active and not expired/suspended."""
        if tenant.status in [TenantLifecycleStatus.SUSPENDED, TenantLifecycleStatus.ARCHIVED]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUBSCRIPTION_INACTIVE",
                    "message_en": f"Tenant subscription is {tenant.status}. Branch membership operations are suspended.",
                    "message_bn": "টেন্যান্ট সাবস্ক্রিপশন স্থগিত বা নিষ্ক্রিয় থাকায় ব্রাঞ্চ রোল পরিবর্তন সম্ভব নয়।",
                }
            )

    @classmethod
    def _serialize_membership(cls, bsm: BranchStaffMembership) -> Dict[str, Any]:
        return {
            "membership_id": bsm.membership_id,
            "tenant_id": bsm.tenant.tenant_id,
            "staff_member_id": bsm.staff_member.staff_id,
            "staff_name": bsm.staff_member.full_name,
            "staff_username": bsm.staff_member.username,
            "branch_id": bsm.branch.branch_id,
            "branch_name": bsm.branch.name,
            "role": bsm.role,
            "custom_role_code": bsm.custom_role_code,
            "custom_role_title": bsm.custom_role_title,
            "is_primary": bsm.is_primary,
            "is_active": bsm.is_active,
            "assigned_at": bsm.assigned_at.isoformat() if bsm.assigned_at else None,
            "assigned_by_username": bsm.assigned_by_username,
            "revoked_at": bsm.revoked_at.isoformat() if bsm.revoked_at else None,
            "revocation_reason": bsm.revocation_reason,
            "updated_at": bsm.updated_at.isoformat() if bsm.updated_at else None,
        }

    @classmethod
    def assign_branch_role(
        cls,
        tenant_id: str,
        staff_member_id: str,
        branch_id: str,
        role: str,
        custom_role_code: str = "",
        custom_role_title: str = "",
        is_primary: bool = False,
        assigned_by_username: str = "",
    ) -> Dict[str, Any]:
        """
        Assigns or updates a staff member's specific role in an authorized branch.
        Validates tenant existence, subscription status, branch ownership, and role legality.
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        # 2. Verify Branch exists and belongs to Tenant
        if not branch_id or not str(branch_id).strip():
            raise ValidationError(
                {"error_code": "INVALID_BRANCH", "message": "branch_id is required."}
            )

        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise CrossTenantOperationError(
                f"Branch '{branch_id}' does not exist for tenant '{tenant_id}'."
            )

        # 3. Verify Staff Member exists and belongs to Tenant
        if not staff_member_id or not str(staff_member_id).strip():
            raise ValidationError(
                {"error_code": "INVALID_STAFF", "message": "staff_member_id is required."}
            )

        try:
            staff_member = StaffMember.objects.get(staff_id=staff_member_id, tenant=tenant)
        except StaffMember.DoesNotExist:
            raise CrossTenantOperationError(
                f"Staff member '{staff_member_id}' does not exist for tenant '{tenant_id}'."
            )

        # 4. Role validation
        role_clean = str(role or "").strip().upper()
        if not role_clean:
            raise ValidationError(
                {"error_code": "INVALID_ROLE", "message": "Role must not be empty."}
            )

        # Verify if standard or custom role
        if role_clean not in cls.VALID_ROLES:
            # Check CustomRoleDefinition
            custom_def = CustomRoleDefinition.objects.filter(
                tenant=tenant,
                role_code=role_clean,
                is_archived=False,
            ).first()
            if not custom_def:
                raise ValidationError(
                    {
                        "error_code": "INVALID_ROLE",
                        "message": f"'{role}' is not a recognized standard or custom role in this tenant.",
                    }
                )
            custom_role_code = custom_def.role_code
            if not custom_role_title:
                custom_role_title = custom_def.display_name

        with transaction.atomic():
            # If set as primary, unset any existing primary flags for this staff member
            if is_primary:
                BranchStaffMembership.objects.filter(
                    tenant=tenant,
                    staff_member=staff_member,
                    is_primary=True,
                ).update(is_primary=False)

            membership_id = f"bsm-{uuid.uuid4().hex[:12]}"
            bsm, created = BranchStaffMembership.objects.get_or_create(
                tenant=tenant,
                staff_member=staff_member,
                branch=branch,
                defaults={
                    "membership_id": membership_id,
                    "role": role_clean,
                    "custom_role_code": custom_role_code,
                    "custom_role_title": custom_role_title,
                    "is_primary": is_primary,
                    "is_active": True,
                    "assigned_by_username": assigned_by_username,
                },
            )

            if not created:
                bsm.role = role_clean
                bsm.custom_role_code = custom_role_code
                bsm.custom_role_title = custom_role_title
                bsm.is_primary = is_primary
                bsm.is_active = True
                bsm.revoked_at = None
                bsm.revocation_reason = ""
                bsm.assigned_by_username = assigned_by_username
                bsm.save()

            # Ensure StaffMembership roaming assigned_branches contains this branch
            staff_mem = StaffMembership.objects.filter(
                tenant=tenant,
                staff_member=staff_member,
            ).first()
            if staff_mem:
                staff_mem.assigned_branches.add(branch)
                if is_primary:
                    staff_mem.primary_branch = branch
                    staff_mem.save()

        logger.info(
            "Assigned branch membership %s: staff %s (%s) @ branch %s by %s",
            bsm.membership_id,
            staff_member.username,
            role_clean,
            branch.branch_id,
            assigned_by_username,
        )
        return cls._serialize_membership(bsm)

    @classmethod
    def list_branch_memberships(
        cls,
        tenant_id: str,
        staff_member_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Lists branch-specific memberships matching optional filters."""
        tenant = cls._get_tenant(tenant_id)
        qs = BranchStaffMembership.objects.select_related("staff_member", "branch").filter(
            tenant=tenant
        )

        if staff_member_id:
            qs = qs.filter(staff_member__staff_id=staff_member_id)
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)

        return [cls._serialize_membership(item) for item in qs]

    @classmethod
    def get_effective_branch_role(
        cls,
        tenant_id: str,
        staff_identifier: str,
        branch_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Resolves the active branch-specific role for a staff member in a particular branch.
        staff_identifier can be a staff_id, email, or username.
        """
        tenant = cls._get_tenant(tenant_id)
        branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
        if not branch:
            return None

        # Locate staff member
        staff = StaffMember.objects.filter(tenant=tenant).filter(
            staff_id=staff_identifier
        ).first() or StaffMember.objects.filter(tenant=tenant).filter(
            username=staff_identifier
        ).first()

        if not staff:
            # Check PharmacyUser email
            user = PharmacyUser.objects.filter(tenant_id=tenant_id, email__iexact=staff_identifier).first()
            if user:
                staff = StaffMember.objects.filter(tenant=tenant, username=user.email).first()

        if not staff:
            return None

        bsm = BranchStaffMembership.objects.filter(
            tenant=tenant,
            staff_member=staff,
            branch=branch,
            is_active=True,
        ).first()

        if bsm:
            return cls._serialize_membership(bsm)

        # Check if staff member has general StaffMembership roaming to this branch
        mem = StaffMembership.objects.filter(
            tenant=tenant,
            staff_member=staff,
            is_active=True,
        ).first()
        if mem and (mem.primary_branch == branch or mem.assigned_branches.filter(branch_id=branch_id).exists()):
            return {
                "membership_id": mem.membership_id,
                "tenant_id": tenant_id,
                "staff_member_id": staff.staff_id,
                "staff_name": staff.full_name,
                "staff_username": staff.username,
                "branch_id": branch.branch_id,
                "branch_name": branch.name,
                "role": mem.role,
                "custom_role_code": "",
                "custom_role_title": mem.custom_role_title,
                "is_primary": mem.primary_branch == branch,
                "is_active": True,
                "assigned_at": mem.joined_at.isoformat() if mem.joined_at else None,
                "assigned_by_username": "system_roaming",
                "revoked_at": None,
                "revocation_reason": "",
            }

        return None

    @classmethod
    def revoke_branch_membership(
        cls,
        tenant_id: str,
        membership_id: str,
        revoked_by_username: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        """Revokes a branch-specific role assignment for a staff member."""
        tenant = cls._get_tenant(tenant_id)
        try:
            bsm = BranchStaffMembership.objects.select_related("staff_member", "branch").get(
                membership_id=membership_id, tenant=tenant
            )
        except BranchStaffMembership.DoesNotExist:
            raise CrossTenantOperationError(
                f"Branch membership '{membership_id}' does not exist for tenant '{tenant_id}'."
            )

        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise ValidationError(
                {"error_code": "MISSING_REASON", "message": "Revocation reason is required."}
            )

        bsm.is_active = False
        bsm.revoked_at = timezone.now()
        bsm.revoked_by_username = revoked_by_username
        bsm.revocation_reason = clean_reason
        bsm.save()

        logger.info(
            "Revoked branch membership %s for %s @ %s by %s: %s",
            membership_id,
            bsm.staff_member.username,
            bsm.branch.branch_id,
            revoked_by_username,
            clean_reason,
        )
        return cls._serialize_membership(bsm)
