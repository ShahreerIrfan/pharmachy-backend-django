"""
backend/apps/core/branch_lifecycle_service.py
Architecture Decision: ADR-0005, ADR-0015, ADR-0081, ADR-0082, ADR-0091, ADR-0096, ADR-0105, ADR-0107
Phase 06: Milestone M03 - Branch Lifecycle Management (P107)
Requirement Trace: FR-001, FR-004, FR-049, FR-070, FR-071, FR-082

Authoritative business logic for:
1. Branch provisioning within subscription plan branch quotas.
2. Branch profile and regulatory license updates.
3. Temporary branch suspension with offline lease revocation.
4. Quota-safeguarded branch reactivation.
5. Branch closure / decommissioning with 100% historical transaction & inventory preservation.
6. Cascaded workstation seat and offline lease invalidation.
7. Immutable branch lifecycle event audit logging.
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
    BranchLifecycleEvent,
    BranchLifecycleEventType,
    BranchLifecycleStatus,
    BranchStaffMembership,
    DeviceLeaseCertificate,
    DeviceSeat,
    DeviceSeatStatus,
    OfflineLeaseStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.postgres_service import CrossTenantOperationError
from apps.core.quota_guard_service import (
    SubscriptionQuotaGuardService,
)

logger = logging.getLogger("pharmacy.branch_lifecycle")


class BranchLifecycleService:
    """Authoritative service for physical branch store lifecycle transitions and audit logging."""

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
                    "message_en": f"Tenant subscription is {tenant.status}. Branch lifecycle operations are suspended.",
                    "message_bn": "টেন্যান্ট সাবস্ক্রিপশন স্থগিত বা নিষ্ক্রিয় থাকায় ব্রাঞ্চ সংক্রান্ত পরিবর্তন সম্ভব নয়।",
                }
            )

    @classmethod
    def _record_event(
        cls,
        tenant: Tenant,
        branch: Branch,
        event_type: str,
        performed_by_username: str = "",
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        ip_address: str = "",
    ) -> BranchLifecycleEvent:
        event_id = f"evt-br-{uuid.uuid4().hex[:12]}"
        return BranchLifecycleEvent.objects.create(
            event_id=event_id,
            tenant=tenant,
            branch=branch,
            event_type=event_type,
            performed_by_username=(performed_by_username or "").strip(),
            reason=(reason or "").strip(),
            metadata=metadata or {},
            ip_address=ip_address or "",
        )

    @classmethod
    def create_branch(
        cls,
        tenant_id: str,
        name: str,
        license_number: str,
        address: str = "",
        branch_code: str = "",
        phone_number: str = "",
        email: str = "",
        manager_name: str = "",
        is_main_branch: bool = False,
        performed_by_username: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Creates a new branch store subject to plan branch quota safeguards.
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        # 1. Quota Check
        SubscriptionQuotaGuardService.check_branch_creation_allowed(tenant_id)

        # 2. Input Validation
        clean_name = (name or "").strip()
        if len(clean_name) < 2:
            raise ValidationError(
                {"error_code": "INVALID_BRANCH_NAME", "message": "Branch name must be at least 2 characters."}
            )

        clean_license = (license_number or "").strip()
        if len(clean_license) < 3:
            raise ValidationError(
                {"error_code": "INVALID_LICENSE_NUMBER", "message": "DGDA Drug License number must be at least 3 characters."}
            )

        # Check tenant-scoped uniqueness of name
        if Branch.objects.filter(tenant=tenant, name__iexact=clean_name).exists():
            raise ValidationError(
                {"error_code": "BRANCH_NAME_ALREADY_EXISTS", "message": f"A branch named '{clean_name}' already exists in this pharmacy."}
            )

        branch_id = f"br-{uuid.uuid4().hex[:12]}"
        clean_code = (branch_code or f"BR-{uuid.uuid4().hex[:6].upper()}").strip()

        with transaction.atomic():
            if is_main_branch:
                # Unset previous main branch
                Branch.objects.filter(tenant=tenant, is_main_branch=True).update(is_main_branch=False)

            branch = Branch.objects.create(
                branch_id=branch_id,
                tenant=tenant,
                name=clean_name,
                branch_code=clean_code,
                license_number=clean_license,
                address=(address or "").strip(),
                phone_number=(phone_number or "").strip(),
                email=(email or "").strip(),
                manager_name=(manager_name or "").strip(),
                is_main_branch=is_main_branch,
                status=BranchLifecycleStatus.ACTIVE,
                is_active=True,
            )

            cls._record_event(
                tenant=tenant,
                branch=branch,
                event_type=BranchLifecycleEventType.CREATED,
                performed_by_username=performed_by_username,
                reason="Direct branch store provisioning",
                metadata={"branch_code": clean_code, "license_number": clean_license},
                ip_address=ip_address,
            )

        logger.info("Successfully provisioned branch %s (%s) for tenant %s", branch_id, clean_name, tenant_id)
        return cls.get_branch_detail(tenant_id=tenant_id, branch_id=branch_id)

    @classmethod
    def update_branch(
        cls,
        tenant_id: str,
        branch_id: str,
        name: Optional[str] = None,
        license_number: Optional[str] = None,
        address: Optional[str] = None,
        branch_code: Optional[str] = None,
        phone_number: Optional[str] = None,
        email: Optional[str] = None,
        manager_name: Optional[str] = None,
        is_main_branch: Optional[bool] = None,
        performed_by_username: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """Updates branch attributes and records immutable audit event."""
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise CrossTenantOperationError(f"Branch '{branch_id}' not found for tenant '{tenant_id}'.")

        if branch.status == BranchLifecycleStatus.CLOSED:
            raise ValidationError(
                {"error_code": "CANNOT_UPDATE_CLOSED_BRANCH", "message": "Cannot update a closed branch."}
            )

        changes: Dict[str, Any] = {}

        if name is not None:
            clean_name = name.strip()
            if len(clean_name) < 2:
                raise ValidationError({"error_code": "INVALID_BRANCH_NAME", "message": "Branch name must be at least 2 characters."})
            if Branch.objects.filter(tenant=tenant, name__iexact=clean_name).exclude(branch_id=branch_id).exists():
                raise ValidationError({"error_code": "BRANCH_NAME_ALREADY_EXISTS", "message": f"Branch name '{clean_name}' already exists."})
            branch.name = clean_name
            changes["name"] = clean_name

        if license_number is not None:
            clean_lic = license_number.strip()
            if len(clean_lic) < 3:
                raise ValidationError({"error_code": "INVALID_LICENSE_NUMBER", "message": "License number must be at least 3 characters."})
            branch.license_number = clean_lic
            changes["license_number"] = clean_lic

        if address is not None:
            branch.address = address.strip()
            changes["address"] = branch.address

        if branch_code is not None:
            branch.branch_code = branch_code.strip()
            changes["branch_code"] = branch.branch_code

        if phone_number is not None:
            branch.phone_number = phone_number.strip()
            changes["phone_number"] = branch.phone_number

        if email is not None:
            branch.email = email.strip()
            changes["email"] = branch.email

        if manager_name is not None:
            branch.manager_name = manager_name.strip()
            changes["manager_name"] = branch.manager_name

        with transaction.atomic():
            if is_main_branch is True:
                Branch.objects.filter(tenant=tenant, is_main_branch=True).exclude(branch_id=branch_id).update(is_main_branch=False)
                branch.is_main_branch = True
                changes["is_main_branch"] = True
            elif is_main_branch is False:
                branch.is_main_branch = False
                changes["is_main_branch"] = False

            branch.save()

            cls._record_event(
                tenant=tenant,
                branch=branch,
                event_type=BranchLifecycleEventType.UPDATED,
                performed_by_username=performed_by_username,
                reason="Branch profile update",
                metadata=changes,
                ip_address=ip_address,
            )

        logger.info("Updated branch %s (%s)", branch_id, changes)
        return cls.get_branch_detail(tenant_id=tenant_id, branch_id=branch_id)

    @classmethod
    def suspend_branch(
        cls,
        tenant_id: str,
        branch_id: str,
        reason: str = "",
        performed_by_username: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Temporarily suspends branch operations and revokes active offline POS leases.
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise CrossTenantOperationError(f"Branch '{branch_id}' not found for tenant '{tenant_id}'.")

        if branch.status == BranchLifecycleStatus.CLOSED:
            raise ValidationError(
                {"error_code": "CANNOT_SUSPEND_CLOSED_BRANCH", "message": "Cannot suspend an already closed branch."}
            )

        now = timezone.now()
        clean_reason = (reason or "Administrative branch suspension").strip()

        with transaction.atomic():
            branch.status = BranchLifecycleStatus.SUSPENDED
            branch.is_active = False
            branch.save()

            revoked_leases = DeviceLeaseCertificate.objects.filter(
                branch=branch,
                status=OfflineLeaseStatus.ACTIVE,
            ).update(
                status=OfflineLeaseStatus.REVOKED,
                revoked_at=now,
                revocation_reason=f"BRANCH_SUSPENDED: {clean_reason}",
            )

            cls._record_event(
                tenant=tenant,
                branch=branch,
                event_type=BranchLifecycleEventType.SUSPENDED,
                performed_by_username=performed_by_username,
                reason=clean_reason,
                metadata={"revoked_leases": revoked_leases},
                ip_address=ip_address,
            )

        logger.info("Suspended branch %s. Revoked %d offline leases.", branch_id, revoked_leases)
        return cls.get_branch_detail(tenant_id=tenant_id, branch_id=branch_id)

    @classmethod
    def reactivate_branch(
        cls,
        tenant_id: str,
        branch_id: str,
        reason: str = "",
        performed_by_username: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """Reactivates a suspended branch subject to plan branch quota limits."""
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise CrossTenantOperationError(f"Branch '{branch_id}' not found for tenant '{tenant_id}'.")

        if branch.status == BranchLifecycleStatus.CLOSED:
            raise ValidationError(
                {
                    "error_code": "CANNOT_REACTIVATE_CLOSED_BRANCH",
                    "message": "Closed branches cannot be reactivated; please provision a new branch.",
                }
            )

        # Quota Check: reactivating adds an active branch
        SubscriptionQuotaGuardService.check_branch_creation_allowed(tenant_id)

        clean_reason = (reason or "Branch reactivated by administrator").strip()

        with transaction.atomic():
            branch.status = BranchLifecycleStatus.ACTIVE
            branch.is_active = True
            branch.is_over_quota_locked = False
            branch.save()

            cls._record_event(
                tenant=tenant,
                branch=branch,
                event_type=BranchLifecycleEventType.REACTIVATED,
                performed_by_username=performed_by_username,
                reason=clean_reason,
                metadata={},
                ip_address=ip_address,
            )

        logger.info("Reactivated branch %s", branch_id)
        return cls.get_branch_detail(tenant_id=tenant_id, branch_id=branch_id)

    @classmethod
    def close_branch(
        cls,
        tenant_id: str,
        branch_id: str,
        reason: str,
        performed_by_username: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Permanently closes a branch store.
        Core Rule: Preserves 100% of historical invoices, customer due ledgers, batch stock, and prescription logs.
        Cascades:
        - Revokes active offline leases for this branch
        - Suspends active device seats
        - Soft-revokes branch staff memberships
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        clean_reason = (reason or "").strip()
        if not clean_reason or len(clean_reason) < 4:
            raise ValidationError(
                {"error_code": "CLOSURE_REASON_REQUIRED", "message": "A detailed closure reason (minimum 4 characters) is mandatory."}
            )

        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise CrossTenantOperationError(f"Branch '{branch_id}' not found for tenant '{tenant_id}'.")

        now = timezone.now()

        with transaction.atomic():
            branch.status = BranchLifecycleStatus.CLOSED
            branch.is_active = False
            branch.closed_at = now
            branch.closed_by = (performed_by_username or "").strip()
            branch.closure_reason = clean_reason
            if branch.is_main_branch:
                branch.is_main_branch = False
            branch.save()

            # Revoke active offline device leases
            revoked_leases = DeviceLeaseCertificate.objects.filter(
                branch=branch,
                status=OfflineLeaseStatus.ACTIVE,
            ).update(
                status=OfflineLeaseStatus.REVOKED,
                revoked_at=now,
                revocation_reason=f"BRANCH_CLOSED: {clean_reason}",
            )

            # Suspend device seats
            suspended_devices = DeviceSeat.objects.filter(
                branch=branch,
                status=DeviceSeatStatus.ACTIVE,
            ).update(
                status=DeviceSeatStatus.SUSPENDED,
                revoked_at=now,
                revocation_reason=f"BRANCH_CLOSED: {clean_reason}",
            )

            # Soft-revoke branch staff memberships
            revoked_memberships = BranchStaffMembership.objects.filter(
                branch=branch,
                is_active=True,
            ).update(
                is_active=False,
                revoked_at=now,
                revoked_by_username=performed_by_username,
                revocation_reason=f"BRANCH_CLOSED: {clean_reason}",
            )

            cls._record_event(
                tenant=tenant,
                branch=branch,
                event_type=BranchLifecycleEventType.CLOSED,
                performed_by_username=performed_by_username,
                reason=clean_reason,
                metadata={
                    "revoked_leases": revoked_leases,
                    "suspended_devices": suspended_devices,
                    "revoked_memberships": revoked_memberships,
                },
                ip_address=ip_address,
            )

        logger.info(
            "Closed branch %s. Preserved all historical records. Revoked %d leases, %d seats, %d memberships.",
            branch_id,
            revoked_leases,
            suspended_devices,
            revoked_memberships,
        )
        return cls.get_branch_detail(tenant_id=tenant_id, branch_id=branch_id)

    @classmethod
    def get_branch_detail(cls, tenant_id: str, branch_id: str) -> Dict[str, Any]:
        """Returns comprehensive branch profile conforming to branch_lifecycle.schema.json."""
        tenant = cls._get_tenant(tenant_id)
        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise CrossTenantOperationError(f"Branch '{branch_id}' not found for tenant '{tenant_id}'.")

        active_staff = BranchStaffMembership.objects.filter(branch=branch, is_active=True).count()
        active_devices = DeviceSeat.objects.filter(branch=branch, status=DeviceSeatStatus.ACTIVE).count()

        # Count preserved historical records
        historical_invoices = branch.invoices.count() if hasattr(branch, "invoices") else 0
        historical_batches = branch.batches.count() if hasattr(branch, "batches") else 0

        return {
            "branch_id": branch.branch_id,
            "tenant_id": branch.tenant_id,
            "name": branch.name,
            "branch_code": branch.branch_code or branch.branch_id,
            "license_number": branch.license_number,
            "address": branch.address,
            "phone_number": branch.phone_number,
            "email": branch.email or None,
            "manager_name": branch.manager_name,
            "is_main_branch": branch.is_main_branch,
            "status": branch.status,
            "is_active": branch.is_active,
            "is_over_quota_locked": branch.is_over_quota_locked,
            "active_staff_count": active_staff,
            "active_devices_count": active_devices,
            "historical_invoices_count": historical_invoices,
            "historical_stock_batches_count": historical_batches,
            "created_at": branch.created_at.isoformat(),
            "closed_at": branch.closed_at.isoformat() if branch.closed_at else None,
            "closure_reason": branch.closure_reason or None,
        }

    @classmethod
    def get_historical_summary(cls, tenant_id: str, branch_id: str) -> Dict[str, Any]:
        """Returns an audit-safe summary of historical transactions, batch inventory, and prescriptions."""
        tenant = cls._get_tenant(tenant_id)
        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise CrossTenantOperationError(f"Branch '{branch_id}' not found for tenant '{tenant_id}'.")

        invoices_count = branch.invoices.count() if hasattr(branch, "invoices") else 0
        batches_count = branch.batches.count() if hasattr(branch, "batches") else 0
        prescriptions_count = branch.prescriptions.count() if hasattr(branch, "prescriptions") else 0

        return {
            "branch_id": branch.branch_id,
            "branch_name": branch.name,
            "status": branch.status,
            "is_active": branch.is_active,
            "is_closed": branch.status == BranchLifecycleStatus.CLOSED,
            "closed_at": branch.closed_at.isoformat() if branch.closed_at else None,
            "historical_invoices_count": invoices_count,
            "historical_stock_batches_count": batches_count,
            "historical_prescriptions_count": prescriptions_count,
            "zero_data_loss_guaranteed": True,
            "message_en": "100% of historical transactions, financial ledgers, and inventory trails are permanently preserved for DGDA audit.",
            "message_bn": "ডিজিডিএ অডিট ও আর্থিক সমন্বয়ের স্বার্থে ১০০% ঐতিহাসিক বিক্রয় এবং ইনভেন্টরি ডাটা স্থায়ীভাবে সংরক্ষিত রয়েছে।",
        }

    @classmethod
    def list_branches(
        cls,
        tenant_id: str,
        status: Optional[str] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists branches for tenant with optional filters."""
        tenant = cls._get_tenant(tenant_id)
        qs = Branch.objects.filter(tenant=tenant)
        if status:
            qs = qs.filter(status=status.upper())
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if search:
            clean_q = search.strip()
            qs = qs.filter(name__icontains=clean_q)

        return [cls.get_branch_detail(tenant_id=tenant_id, branch_id=b.branch_id) for b in qs]

    @classmethod
    def list_branch_events(cls, tenant_id: str, branch_id: str) -> List[Dict[str, Any]]:
        """Returns the immutable lifecycle event history for a branch."""
        tenant = cls._get_tenant(tenant_id)
        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise CrossTenantOperationError(f"Branch '{branch_id}' not found for tenant '{tenant_id}'.")

        events = BranchLifecycleEvent.objects.filter(tenant=tenant, branch=branch)
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
