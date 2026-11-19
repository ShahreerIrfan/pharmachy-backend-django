"""
backend/apps/core/device_compromise_recovery_service.py
Architecture Decision: ADR-0319 (P319).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

Authoritative service governing device revocation, compromise quarantining, and data recovery:
- Revokes stolen, lost, or compromised POS terminals.
- Immediately terminates active cryptographic encryption keys and offline leases.
- Retains forensic evidence of unsynced local outbox transactions for authorized owner recovery.
- Rejects any subsequent protected operations from revoked devices.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    CompromiseType,
    DeviceCompromiseRecoveryDocket,
    DeviceEncryptionKeyRecord,
    DeviceEncryptionKeyStatus,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    OfflineOutboxCommandReceipt,
    RecoveryDocketStatus,
    SignedOfflineSubscriptionLease,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class DeviceCompromiseRecoveryService:

    @classmethod
    def assert_tenant_active(cls, tenant_id: str) -> Tenant:
        try:
            t = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' does not exist."})
        if t.status != TenantLifecycleStatus.ACTIVE:
            raise PermissionDenied(
                {"error_code": "ERR_TENANT_SUSPENDED", "detail": f"Tenant '{tenant_id}' is suspended or inactive."}
            )
        return t

    @classmethod
    def _assert_owner_or_admin(cls, actor_user: Any, branch_id: str = ""):
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if role not in (UserRole.PHARMACY_OWNER, "PHARMACY_OWNER", "OWNER_ADMIN", "ADMIN"):
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            if not user_branch or user_branch != str(branch_id) or role not in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "detail": "Only Pharmacy Owner, Superadmin, or authorized Branch Manager can revoke devices.",
                    }
                )

    @classmethod
    @transaction.atomic
    def revoke_device_and_quarantine(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        compromise_type: str,
        revocation_reason: str,
        unsynced_evidence: Optional[Dict[str, Any]] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        device_reg = DeviceLocalDbRegistration.objects.select_related("branch").filter(
            tenant=tenant, device_id=device_id
        ).first()

        if not device_reg:
            foreign = DeviceLocalDbRegistration.objects.filter(device_id=device_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant revocation forbidden."})
            raise NotFound({"error_code": "ERR_DEVICE_NOT_FOUND", "detail": f"Device '{device_id}' not found."})

        cls._assert_owner_or_admin(actor_user, branch_id=str(device_reg.branch_id))

        if not revocation_reason or len(revocation_reason.strip()) < 5:
            raise ValidationError(
                {"error_code": "ERR_INVALID_REVOCATION_REASON", "detail": "A clear revocation reason (min 5 chars) is required."}
            )

        valid_types = [c[0] for c in CompromiseType.choices]
        norm_type = compromise_type.upper().strip()
        if norm_type not in valid_types:
            raise ValidationError(
                {"error_code": "ERR_INVALID_COMPROMISE_TYPE", "detail": f"compromise_type must be one of {valid_types}."}
            )

        now = timezone.now()

        # 1. Update Device Registration status to REVOKED
        device_reg.status = DeviceLocalDbStatus.REVOKED
        device_reg.revocation_reason = revocation_reason.strip()
        device_reg.lease_expires_at = now  # Immediately terminate offline lease
        device_reg.save(update_fields=["status", "revocation_reason", "lease_expires_at", "updated_at"])

        # 2. Revoke active encryption keys
        DeviceEncryptionKeyRecord.objects.filter(
            device_registration=device_reg,
            status=DeviceEncryptionKeyStatus.ACTIVE,
        ).update(
            status=DeviceEncryptionKeyStatus.COMPROMISED_REVOKED,
            revocation_reason=f"Compromise Revocation: {revocation_reason.strip()}",
        )

        # 3. Revoke active subscription leases
        SignedOfflineSubscriptionLease.objects.filter(
            device_registration=device_reg,
            status="ACTIVE",
        ).update(
            status="REVOKED",
            revocation_reason=f"Compromise Revocation: {revocation_reason.strip()}",
        )

        # 4. Determine last known sequence from receipts
        last_receipt = OfflineOutboxCommandReceipt.objects.filter(
            tenant=tenant, device_registration=device_reg
        ).order_by("-client_sequence").first()
        last_seq = last_receipt.client_sequence if last_receipt else 0

        # 5. Create Forensic Compromise Docket
        docket_id = f"dkt-{uuid.uuid4().hex[:12]}"
        docket = DeviceCompromiseRecoveryDocket.objects.create(
            docket_id=docket_id,
            tenant=tenant,
            branch=device_reg.branch,
            device_registration=device_reg,
            compromise_type=norm_type,
            revocation_reason=revocation_reason.strip(),
            revoked_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            retained_unsynced_evidence_json=unsynced_evidence or {},
            last_known_client_sequence=last_seq,
            last_known_sync_cursor=device_reg.last_sync_cursor,
            recovery_status=RecoveryDocketStatus.EVIDENCE_RETAINED,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=str(getattr(actor_user, "user_id", "") or ""),
            actor_email=str(getattr(actor_user, "email", "") or ""),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="DEVICE_REVOKED_COMPROMISE",
            resource_type="DeviceCompromiseRecoveryDocket",
            resource_id=docket.docket_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "device_id": device_id,
                "compromise_type": norm_type,
                "revocation_reason": revocation_reason,
                "last_sequence": last_seq,
            },
        )

        return {
            "docket_id": docket.docket_id,
            "device_id": device_id,
            "status": "REVOKED",
            "compromise_type": docket.compromise_type,
            "recovery_status": docket.recovery_status,
            "last_known_client_sequence": docket.last_known_client_sequence,
            "revoked_at": docket.revoked_at.isoformat(),
        }

    @classmethod
    @transaction.atomic
    def reconcile_recovery_docket(
        cls,
        *,
        tenant_id: str,
        docket_id: str,
        resolution_action: str,
        reconciliation_notes: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        docket = DeviceCompromiseRecoveryDocket.objects.select_related("branch", "device_registration").filter(
            tenant=tenant, docket_id=docket_id
        ).first()

        if not docket:
            raise NotFound({"error_code": "ERR_DOCKET_NOT_FOUND", "detail": f"Recovery docket '{docket_id}' not found."})

        cls._assert_owner_or_admin(actor_user, branch_id=str(docket.branch_id))

        norm_action = resolution_action.upper().strip()
        if norm_action not in (RecoveryDocketStatus.MANUALLY_RECONCILED, RecoveryDocketStatus.ABANDONED):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_RESOLUTION_ACTION",
                    "detail": "resolution_action must be 'MANUALLY_RECONCILED' or 'ABANDONED'.",
                }
            )

        now = timezone.now()
        docket.recovery_status = norm_action
        docket.reconciliation_notes = reconciliation_notes.strip()
        docket.reconciled_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        docket.reconciled_at = now
        docket.save(update_fields=["recovery_status", "reconciliation_notes", "reconciled_by", "reconciled_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=str(getattr(actor_user, "user_id", "") or ""),
            actor_email=str(getattr(actor_user, "email", "") or ""),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="DEVICE_RECOVERY_DOCKET_RECONCILED",
            resource_type="DeviceCompromiseRecoveryDocket",
            resource_id=docket.docket_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "docket_id": docket_id,
                "recovery_status": norm_action,
                "notes": reconciliation_notes,
            },
        )

        return {
            "docket_id": docket.docket_id,
            "device_id": docket.device_registration.device_id,
            "recovery_status": docket.recovery_status,
            "reconciliation_notes": docket.reconciliation_notes,
            "reconciled_at": docket.reconciled_at.isoformat(),
        }

    @classmethod
    def list_recovery_dockets(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        tenant = cls.assert_tenant_active(tenant_id)
        qs = DeviceCompromiseRecoveryDocket.objects.select_related("branch", "device_registration").filter(tenant=tenant)
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)

        return [
            {
                "docket_id": d.docket_id,
                "device_id": d.device_registration.device_id,
                "branch_id": d.branch.branch_id,
                "compromise_type": d.compromise_type,
                "revocation_reason": d.revocation_reason,
                "recovery_status": d.recovery_status,
                "last_known_client_sequence": d.last_known_client_sequence,
                "retained_evidence": d.retained_unsynced_evidence_json,
                "revoked_at": d.revoked_at.isoformat(),
            }
            for d in qs.order_by("-revoked_at")
        ]
