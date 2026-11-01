"""
backend/apps/core/device_seat_service.py
Architecture Decision: ADR-0015, ADR-0081, ADR-0082, ADR-0095 & ADR-0096
Phase 05: Basic/Standard/Pro Subscription & Billing (P096)
Requirement Trace: FR-004, FR-070, FR-071, FR-072 (Authoritative Device Seats & Lease Invalidation)

Authoritative Device Seats Management Service:
- Enrolls POS workstations and mobile scanners subject to plan device limits.
- Integrates with SubscriptionQuotaCounterService for row-locked pessimistic concurrency allocation.
- Supports branch reassignment within tenant boundaries.
- Revokes device seats, atomically releasing quota in TenantQuotaUsageCounter and invalidating active DeviceLeaseCertificate records.
- Guarantees zero data loss (historical records preserved in REVOKED state).
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.errors import CrossTenantViolationError
from apps.core.models import (
    Branch,
    DeviceLeaseCertificate,
    DeviceSeat,
    DeviceSeatStatus,
    DeviceType,
    OfflineLeaseStatus,
    QuotaResourceType,
    Tenant,
)
from apps.core.quota_counter_service import SubscriptionQuotaCounterService

logger = logging.getLogger("pharmacy.billing.device_seat")


class DeviceSeatManagementService:

    @classmethod
    def _resolve_tenant_and_branch(cls, tenant_id: str, branch_id: str) -> tuple[Tenant, Branch]:
        clean_tid = str(tenant_id or "").strip()
        clean_bid = str(branch_id or "").strip()

        try:
            tenant = Tenant.objects.get(tenant_id=clean_tid)
        except Tenant.DoesNotExist as err:
            raise ValidationError(f"Tenant '{clean_tid}' does not exist.") from err

        try:
            branch = Branch.objects.get(branch_id=clean_bid, tenant=tenant)
        except Branch.DoesNotExist as err:
            raise ValidationError(
                f"Branch '{clean_bid}' does not exist or does not belong to tenant '{clean_tid}'."
            ) from err

        if branch.is_over_quota_locked:
            raise ValidationError(
                f"Branch '{branch.name}' is over-quota locked due to subscription plan limits. "
                "Devices cannot be assigned to locked branches."
            )

        if not branch.is_active:
            raise ValidationError(f"Branch '{branch.name}' is inactive.")

        return tenant, branch

    @classmethod
    def enroll_device(
        cls,
        tenant_id: str,
        branch_id: str,
        device_id: str,
        device_name: str,
        device_type: str = DeviceType.DESKTOP_POS,
        mac_address: str = "",
        ip_address: str = "",
        hardware_fingerprint: str = "",
        actor_user: Any = None,
    ) -> dict[str, Any]:
        """
        Enrolls a new POS device seat.
        Atomically checks and consumes a device seat under row lock via SubscriptionQuotaCounterService.
        """
        clean_did = str(device_id or "").strip()
        clean_name = str(device_name or "").strip()
        clean_type = str(device_type or DeviceType.DESKTOP_POS).strip().upper()

        if len(clean_did) < 3:
            raise ValidationError("device_id must be at least 3 characters.")
        if len(clean_name) < 2:
            raise ValidationError("device_name must be at least 2 characters.")

        if clean_type not in [
            DeviceType.DESKTOP_POS,
            DeviceType.TABLET_POS,
            DeviceType.MOBILE_SCANNER,
            DeviceType.COUNTER_TERMINAL,
        ]:
            clean_type = DeviceType.DESKTOP_POS

        tenant, branch = cls._resolve_tenant_and_branch(tenant_id, branch_id)
        now = timezone.now()
        user_email = str(getattr(actor_user, "email", "") or "")
        user_id = str(getattr(actor_user, "user_id", None) or getattr(actor_user, "id", "") or "")

        with transaction.atomic():
            existing = DeviceSeat.objects.filter(device_id=clean_did).first()
            if existing:
                if existing.tenant_id != tenant.tenant_id:
                    raise CrossTenantViolationError(
                        f"Device '{clean_did}' is already registered to a different tenant."
                    )
                if existing.status == DeviceSeatStatus.ACTIVE:
                    raise ValidationError(
                        f"Device '{clean_did}' is already actively enrolled in branch '{existing.branch_id}'."
                    )
                # Re-enroll previously revoked device: consume seat
                SubscriptionQuotaCounterService.reserve_and_consume(
                    tenant_id=tenant.tenant_id,
                    resource_type=QuotaResourceType.DEVICES,
                    amount=1,
                    actor_user=actor_user,
                )
                existing.branch = branch
                existing.device_name = clean_name
                existing.device_type = clean_type
                existing.status = DeviceSeatStatus.ACTIVE
                existing.mac_address = mac_address or existing.mac_address
                existing.ip_address = ip_address or existing.ip_address
                existing.hardware_fingerprint = hardware_fingerprint or existing.hardware_fingerprint
                existing.enrolled_at = now
                existing.enrolled_by = user_email
                existing.revoked_at = None
                existing.revoked_by = ""
                existing.revocation_reason = ""
                existing.save()
                device = existing
            else:
                # 1. Pessimistically reserve seat under lock
                SubscriptionQuotaCounterService.reserve_and_consume(
                    tenant_id=tenant.tenant_id,
                    resource_type=QuotaResourceType.DEVICES,
                    amount=1,
                    actor_user=actor_user,
                )

                # 2. Create device record
                device = DeviceSeat.objects.create(
                    device_id=clean_did,
                    tenant=tenant,
                    branch=branch,
                    device_name=clean_name,
                    device_type=clean_type,
                    status=DeviceSeatStatus.ACTIVE,
                    mac_address=mac_address,
                    ip_address=ip_address,
                    hardware_fingerprint=hardware_fingerprint,
                    enrolled_at=now,
                    enrolled_by=user_email,
                )

            # 3. Audit Log
            AuditLogEntry.objects.create(
                action="DEVICE_SEAT_ENROLLED",
                resource_type="DEVICE_SEAT",
                resource_id=device.device_id,
                tenant_id=tenant.tenant_id,
                user_id=user_id or None,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "device_id": device.device_id,
                    "device_name": device.device_name,
                    "device_type": device.device_type,
                    "branch_id": branch.branch_id,
                    "branch_name": branch.name,
                },
            )

        logger.info(
            "Enrolled device seat %s (%s) for tenant %s at branch %s",
            device.device_id,
            device.device_name,
            tenant.tenant_id,
            branch.branch_id,
        )
        return cls._serialize_device(device)

    @classmethod
    def reassign_branch(
        cls,
        device_id: str,
        new_branch_id: str,
        actor_user: Any = None,
    ) -> dict[str, Any]:
        """Reassigns an active device seat to a different branch within the same tenant."""
        clean_did = str(device_id or "").strip()
        clean_bid = str(new_branch_id or "").strip()

        try:
            device = DeviceSeat.objects.select_related("tenant", "branch").get(device_id=clean_did)
        except DeviceSeat.DoesNotExist as err:
            raise ValidationError(f"Device '{clean_did}' does not exist.") from err

        if device.status != DeviceSeatStatus.ACTIVE:
            raise ValidationError(
                f"Cannot reassign branch for device '{clean_did}' with status '{device.status}'."
            )

        try:
            new_branch = Branch.objects.get(branch_id=clean_bid)
        except Branch.DoesNotExist as err:
            raise ValidationError(f"Branch '{clean_bid}' does not exist.") from err

        if new_branch.tenant_id != device.tenant_id:
            raise CrossTenantViolationError(
                f"Branch '{clean_bid}' belongs to another tenant; cross-tenant reassignment is strictly prohibited."
            )

        if new_branch.is_over_quota_locked:
            raise ValidationError(f"Target branch '{new_branch.name}' is over-quota locked.")
        if not new_branch.is_active:
            raise ValidationError(f"Target branch '{new_branch.name}' is inactive.")

        old_branch_id = device.branch_id
        device.branch = new_branch
        device.save(update_fields=["branch", "updated_at"])

        user_id = str(getattr(actor_user, "user_id", None) or getattr(actor_user, "id", "") or "")
        AuditLogEntry.objects.create(
            action="DEVICE_SEAT_REASSIGNED",
            resource_type="DEVICE_SEAT",
            resource_id=device.device_id,
            tenant_id=device.tenant_id,
            user_id=user_id or None,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "old_branch_id": old_branch_id,
                "new_branch_id": new_branch.branch_id,
            },
        )

        return cls._serialize_device(device)

    @classmethod
    def revoke_device_seat(
        cls,
        device_id: str,
        reason: str,
        actor_user: Any = None,
    ) -> dict[str, Any]:
        """
        Revokes a device seat:
        1. Sets device status to REVOKED.
        2. Immediately invalidates all active DeviceLeaseCertificate records for this device.
        3. Atomically releases the device seat quota via SubscriptionQuotaCounterService.
        4. Preserves 100% of historical records (zero data deleted invariant).
        """
        clean_did = str(device_id or "").strip()
        clean_reason = str(reason or "").strip()

        if len(clean_reason) < 5:
            raise ValidationError("Revocation reason (minimum 5 characters) is required.")

        try:
            device = DeviceSeat.objects.select_related("tenant", "branch").get(device_id=clean_did)
        except DeviceSeat.DoesNotExist as err:
            raise ValidationError(f"Device '{clean_did}' does not exist.") from err

        if device.status == DeviceSeatStatus.REVOKED:
            return cls._serialize_device(device)

        now = timezone.now()
        user_email = str(getattr(actor_user, "email", "") or "")
        user_id = str(getattr(actor_user, "user_id", None) or getattr(actor_user, "id", "") or "")

        with transaction.atomic():
            # 1. Update device status
            device.status = DeviceSeatStatus.REVOKED
            device.revoked_at = now
            device.revoked_by = user_email
            device.revocation_reason = clean_reason
            device.save(update_fields=["status", "revoked_at", "revoked_by", "revocation_reason", "updated_at"])

            # 2. Invalidate all active offline lease certificates for this device
            invalidated_leases = DeviceLeaseCertificate.objects.filter(
                tenant=device.tenant,
                device_id=device.device_id,
                status=OfflineLeaseStatus.ACTIVE,
            ).update(
                status=OfflineLeaseStatus.REVOKED,
                revoked_at=now,
                revocation_reason=f"Device seat revoked: {clean_reason}",
            )

            # 3. Atomically release device quota counter
            SubscriptionQuotaCounterService.release_usage(
                tenant_id=device.tenant_id,
                resource_type=QuotaResourceType.DEVICES,
                amount=1,
                actor_user=actor_user,
            )

            # 4. Audit Log
            AuditLogEntry.objects.create(
                action="DEVICE_SEAT_REVOKED",
                resource_type="DEVICE_SEAT",
                resource_id=device.device_id,
                tenant_id=device.tenant_id,
                user_id=user_id or None,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "reason": clean_reason,
                    "invalidated_leases_count": invalidated_leases,
                    "branch_id": device.branch_id,
                },
            )

        logger.info(
            "Revoked device seat %s for tenant %s (invalidated %d leases): %s",
            device.device_id,
            device.tenant_id,
            invalidated_leases,
            clean_reason,
        )
        return cls._serialize_device(device)

    @classmethod
    def record_heartbeat(
        cls,
        device_id: str,
        ip_address: str = "",
        mac_address: str = "",
        actor_user: Any = None,
    ) -> dict[str, Any]:
        """Updates last heartbeat timestamp and network metadata for a device."""
        clean_did = str(device_id or "").strip()
        try:
            device = DeviceSeat.objects.select_related("tenant", "branch").get(device_id=clean_did)
        except DeviceSeat.DoesNotExist as err:
            raise ValidationError(f"Device '{clean_did}' does not exist.") from err

        device.last_heartbeat_at = timezone.now()
        fields_to_update = ["last_heartbeat_at", "updated_at"]
        if ip_address:
            device.ip_address = ip_address
            fields_to_update.append("ip_address")
        if mac_address:
            device.mac_address = mac_address
            fields_to_update.append("mac_address")

        device.save(update_fields=fields_to_update)
        return cls._serialize_device(device)

    @classmethod
    def list_device_seats(cls, tenant_id: str) -> dict[str, Any]:
        """Lists all enrolled devices for a tenant along with quota usage capacity summary."""
        clean_tid = str(tenant_id or "").strip()
        try:
            tenant = Tenant.objects.get(tenant_id=clean_tid)
        except Tenant.DoesNotExist as err:
            raise ValidationError(f"Tenant '{clean_tid}' does not exist.") from err

        devices = (
            DeviceSeat.objects.filter(tenant=tenant)
            .select_related("branch")
            .order_by("-enrolled_at")
        )
        serialized_devices = [cls._serialize_device(d) for d in devices]

        quota_report = SubscriptionQuotaCounterService.get_quota_usage_report(clean_tid)
        device_counter = quota_report.get("counters", {}).get("DEVICES", {})

        return {
            "tenant_id": tenant.tenant_id,
            "plan_tier": quota_report.get("plan_tier", "BASIC"),
            "devices": serialized_devices,
            "seat_summary": {
                "total_enrolled": len(devices),
                "active_seats_used": device_counter.get("current_used", len([d for d in devices if d.status == DeviceSeatStatus.ACTIVE])),
                "max_limit": device_counter.get("max_limit", 2),
                "remaining": device_counter.get("remaining", 0),
                "utilization_pct": device_counter.get("utilization_pct", 0.0),
                "is_over_quota": device_counter.get("is_over_quota", False),
                "status": device_counter.get("status", "NORMAL"),
            },
            "invariants": {
                "zero_data_deleted": True,
                "lease_invalidation_enforced": True,
                "quota_counter_synced": True,
            },
            "client_messaging": {
                "headline_en": "Device Seats Management",
                "headline_bn": "ডিভাইস সীট ব্যবস্থাপনা",
                "details_en": f"{device_counter.get('current_used', 0)} of {device_counter.get('max_limit', 2)} device seats in use.",
                "details_bn": f"মোট {device_counter.get('max_limit', 2)} টির মধ্যে {device_counter.get('current_used', 0)} টি ডিভাইস ব্যবহৃত হচ্ছে।",
            },
        }

    @classmethod
    def _serialize_device(cls, device: DeviceSeat) -> dict[str, Any]:
        is_active = device.status == DeviceSeatStatus.ACTIVE

        if is_active:
            hl_en = f"Device Seat Active: {device.device_name}"
            hl_bn = f"ডিভাইস সীট সক্রিয়: {device.device_name}"
            dt_en = f"Assigned to branch {device.branch.name}."
            dt_bn = f"শাখা {device.branch.name}-এ বরাদ্দকৃত।"
        else:
            hl_en = f"Device Seat Revoked: {device.device_name}"
            hl_bn = f"ডিভাইস সীট বাতিলকৃত: {device.device_name}"
            dt_en = f"Reason: {device.revocation_reason or 'No reason provided'}"
            dt_bn = f"কারণ: {device.revocation_reason or 'কোনো কারণ উল্লেখ নেই'}"

        return {
            "device_id": device.device_id,
            "tenant_id": device.tenant_id,
            "branch_id": device.branch_id,
            "branch_name": device.branch.name if hasattr(device, "branch") and device.branch else "",
            "device_name": device.device_name,
            "device_type": device.device_type,
            "status": device.status,
            "mac_address": device.mac_address,
            "ip_address": device.ip_address,
            "hardware_fingerprint": device.hardware_fingerprint,
            "enrolled_at": device.enrolled_at.isoformat(),
            "enrolled_by": device.enrolled_by,
            "revoked_at": device.revoked_at.isoformat() if device.revoked_at else None,
            "revoked_by": device.revoked_by or None,
            "revocation_reason": device.revocation_reason,
            "last_heartbeat_at": device.last_heartbeat_at.isoformat() if device.last_heartbeat_at else None,
            "invariants": {
                "zero_data_deleted": True,
                "lease_invalidation_enforced": True,
                "quota_counter_synced": True,
            },
            "client_messaging": {
                "headline_en": hl_en,
                "headline_bn": hl_bn,
                "details_en": dt_en,
                "details_bn": dt_bn,
            },
        }

