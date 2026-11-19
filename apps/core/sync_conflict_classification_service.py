"""
backend/apps/core/sync_conflict_classification_service.py
Architecture Decision: ADR-0313 (P313).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

Sync Conflict Classification Engine:
- Authoritatively classifies offline commands against server invariants:
  1. Stale Stock (CONFLICT_STALE_STOCK): Local sale/reservation against depleted, quarantined, or recalled batch.
  2. Stale Price (CONFLICT_STALE_PRICE): Outdated MRP or tax profile beyond allowable variance tolerance.
  3. Revoked Permission (CONFLICT_REVOKED_PERMISSION): Cashier/staff credentials revoked/deactivated prior to sync.
  4. Expired Subscription (CONFLICT_EXPIRED_SUBSCRIPTION): Subscription tier expired or device lease invalid.
  5. Unverified Prescription (CONFLICT_UNVERIFIED_PRESCRIPTION): Rx/antimicrobial medicine dispensed without valid auth token.
  6. Duplicate Command Tampered (CONFLICT_DUPLICATE_COMMAND_TAMPERED): Replayed idempotency key with altered payload or sequence collision.
- Guarantees non-destructive review routing: Conflicting commands NEVER silently alter posted stock or financial data.
- Enforces audited manager resolution workflows with immutable AuditLogEntry and P307 ChangeFeedEvent emission.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.double_entry_journal_service import q2
from apps.core.models import (
    BatchStock,
    Branch,
    ChangeFeedEntityType,
    ChangeFeedEvent,
    ChangeFeedOperation,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    OfflineOutboxCommandReceipt,
    OfflinePrescriptionAuthToken,
    OfflineRxTokenStatus,
    MedicineProduct,
    SignedOfflineLeaseState,
    SignedOfflineSubscriptionLease,
    SyncConflictCategory,
    SyncConflictErrorCode,
    SyncConflictRecord,
    SyncConflictResolutionStatus,
    ConflictProposedDecision,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger(__name__)

CONFLICT_RESOLVE_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}


class SyncConflictClassificationService:
    """
    Authoritative server classification and resolution engine for offline sync conflicts.
    """

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
    def _payload_hash(cls, payload: Dict[str, Any]) -> str:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def classify_offline_command(
        cls,
        *,
        tenant: Tenant,
        device_reg: DeviceLocalDbRegistration,
        command: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Evaluates a single offline command against authoritative cloud invariants.
        Returns classification details. If no conflict exists, has_conflict=False.
        """
        cmd_id = str(command.get("command_id") or "").strip()
        idem_key = str(command.get("idempotency_key") or "").strip()
        cmd_type = str(command.get("command_type") or "").strip().upper()
        payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
        client_seq = int(command.get("client_sequence", 1))
        monetary_amt = q2(Decimal(str(command.get("monetary_total_bdt", "0.00"))))

        # Check 1: Duplicate Command Tampering & Sequence Collisions
        if idem_key:
            existing_receipt = OfflineOutboxCommandReceipt.objects.filter(
                tenant=tenant, idempotency_key=idem_key
            ).first()
            if existing_receipt:
                cur_hash = cls._payload_hash(payload)
                if existing_receipt.payload_sha256 != cur_hash:
                    return {
                        "has_conflict": True,
                        "conflict_category": SyncConflictCategory.DUPLICATE_COMMAND_TAMPERED,
                        "error_code": SyncConflictErrorCode.CONFLICT_DUPLICATE_COMMAND_TAMPERED,
                        "conflict_reason": (
                            f"Idempotency key '{idem_key}' replayed with altered payload. "
                            f"Original hash: {existing_receipt.payload_sha256[:16]}, incoming: {cur_hash[:16]}."
                        ),
                        "disputed_monetary_bdt": monetary_amt,
                        "server_snapshot": {
                            "existing_receipt_id": existing_receipt.receipt_id,
                            "existing_payload_sha256": existing_receipt.payload_sha256,
                            "tampered_payload_sha256": cur_hash,
                        },
                    }

        # Check sequence collision on same device
        if OfflineOutboxCommandReceipt.objects.filter(
            device_registration=device_reg, client_sequence=client_seq
        ).exclude(idempotency_key=idem_key).exists():
            return {
                "has_conflict": True,
                "conflict_category": SyncConflictCategory.DUPLICATE_COMMAND_TAMPERED,
                "error_code": SyncConflictErrorCode.CONFLICT_DUPLICATE_COMMAND_TAMPERED,
                "conflict_reason": f"Client sequence {client_seq} already consumed on device {device_reg.device_id}.",
                "disputed_monetary_bdt": monetary_amt,
                "server_snapshot": {"colliding_sequence": client_seq, "device_id": device_reg.device_id},
            }

        # Check 2: Expired Subscription or Revoked Offline Lease
        active_lease = SignedOfflineSubscriptionLease.objects.filter(
            device_registration=device_reg,
            status=SignedOfflineLeaseState.ACTIVE,
            expires_at__gt=timezone.now(),
        ).first()
        if not active_lease or (device_reg.lease_expires_at and device_reg.lease_expires_at <= timezone.now()):
            return {
                "has_conflict": True,
                "conflict_category": SyncConflictCategory.EXPIRED_SUBSCRIPTION,
                "error_code": SyncConflictErrorCode.CONFLICT_EXPIRED_SUBSCRIPTION,
                "conflict_reason": (
                    f"Device {device_reg.device_id} lease or tenant subscription was expired at sync time."
                ),
                "disputed_monetary_bdt": monetary_amt,
                "server_snapshot": {
                    "device_lease_expires_at": str(device_reg.lease_expires_at),
                    "lease_active": bool(active_lease),
                    "tenant_status": tenant.status,
                },
            }

        # Check 3: Staff Revocation / Inactive Cashier Permission
        cashier_id = str(payload.get("cashier_user_id") or payload.get("user_id") or "").strip()
        if cashier_id:
            user_obj = PharmacyUser.objects.filter(user_id=cashier_id).first()
            if not user_obj or not user_obj.is_active or str(user_obj.tenant_id) != str(tenant.tenant_id):
                return {
                    "has_conflict": True,
                    "conflict_category": SyncConflictCategory.REVOKED_PERMISSION,
                    "error_code": SyncConflictErrorCode.CONFLICT_REVOKED_PERMISSION,
                    "conflict_reason": f"Cashier/user '{cashier_id}' was deactivated, removed, or unauthorized.",
                    "disputed_monetary_bdt": monetary_amt,
                    "server_snapshot": {
                        "user_id": cashier_id,
                        "exists": bool(user_obj),
                        "is_active": getattr(user_obj, "is_active", False),
                    },
                }

        # Check 4: Unverified Prescription Policy
        rx_token_id = str(payload.get("prescription_auth_token_id") or payload.get("rx_token_id") or "").strip()
        rx_required_flag = bool(payload.get("rx_required", False) or payload.get("is_antimicrobial", False))
        if rx_required_flag or rx_token_id:
            if not rx_token_id:
                return {
                    "has_conflict": True,
                    "conflict_category": SyncConflictCategory.UNVERIFIED_PRESCRIPTION,
                    "error_code": SyncConflictErrorCode.CONFLICT_UNVERIFIED_PRESCRIPTION,
                    "conflict_reason": "Rx-required/antimicrobial medicine dispensed without prescription auth token.",
                    "disputed_monetary_bdt": monetary_amt,
                    "server_snapshot": {"rx_required": True, "token_provided": False},
                }
            rx_token = OfflinePrescriptionAuthToken.objects.filter(
                tenant=tenant, token_id=rx_token_id
            ).first()
            if not rx_token or rx_token.status != OfflineRxTokenStatus.ACTIVE or rx_token.expires_at <= timezone.now():
                token_status = rx_token.status if rx_token else "NOT_FOUND"
                return {
                    "has_conflict": True,
                    "conflict_category": SyncConflictCategory.UNVERIFIED_PRESCRIPTION,
                    "error_code": SyncConflictErrorCode.CONFLICT_UNVERIFIED_PRESCRIPTION,
                    "conflict_reason": f"Prescription token '{rx_token_id}' is invalid, revoked, or expired (Status: {token_status}).",
                    "disputed_monetary_bdt": monetary_amt,
                    "server_snapshot": {
                        "rx_token_id": rx_token_id,
                        "token_status": token_status,
                        "expires_at": str(rx_token.expires_at) if rx_token else None,
                    },
                }

        # Check 5: Stale Price / MRP Variance Check
        sold_price_raw = payload.get("unit_price_bdt") or payload.get("mrp_bdt")
        product_id = str(payload.get("product_id") or "").strip()
        if sold_price_raw is not None and product_id:
            sold_price = q2(Decimal(str(sold_price_raw)))
            prod = MedicineProduct.objects.filter(product_id=product_id).first()
            if prod:
                authoritative_mrp = q2(Decimal(str(prod.mrp_unit or "0.00")))
                tolerance = Decimal("0.05")
                variance = abs(sold_price - authoritative_mrp)
                if variance > tolerance:
                    return {
                        "has_conflict": True,
                        "conflict_category": SyncConflictCategory.STALE_PRICE,
                        "error_code": SyncConflictErrorCode.CONFLICT_STALE_PRICE,
                        "conflict_reason": (
                            f"Sold at stale price BDT {sold_price} differing from authoritative MRP BDT {authoritative_mrp} "
                            f"(Variance: BDT {variance} > tolerance BDT {tolerance})."
                        ),
                        "disputed_monetary_bdt": monetary_amt,
                        "server_snapshot": {
                            "product_id": product_id,
                            "product_name": prod.brand_name,
                            "sold_unit_price_bdt": str(sold_price),
                            "server_mrp_bdt": str(authoritative_mrp),
                            "variance_bdt": str(variance),
                        },
                    }

        # Check 6: Stale Stock / Depleted Batch Check
        batch_id = str(payload.get("batch_id") or "").strip()
        qty_needed = int(payload.get("quantity_base_units") or payload.get("quantity") or 0)
        if batch_id and qty_needed > 0:
            batch = BatchStock.objects.filter(tenant=tenant, branch=device_reg.branch, batch_id=batch_id).first()
            if not batch:
                return {
                    "has_conflict": True,
                    "conflict_category": SyncConflictCategory.STALE_STOCK,
                    "error_code": SyncConflictErrorCode.CONFLICT_STALE_STOCK,
                    "conflict_reason": f"Batch '{batch_id}' not found on server for branch '{device_reg.branch.name}'.",
                    "disputed_monetary_bdt": monetary_amt,
                    "server_snapshot": {"batch_id": batch_id, "found": False},
                }
            if batch.status != "ACTIVE":
                return {
                    "has_conflict": True,
                    "conflict_category": SyncConflictCategory.STALE_STOCK,
                    "error_code": SyncConflictErrorCode.CONFLICT_STALE_STOCK,
                    "conflict_reason": f"Batch '{batch_id}' is in non-sellable status '{batch.status}' (Quarantined/Recalled).",
                    "disputed_monetary_bdt": monetary_amt,
                    "server_snapshot": {
                        "batch_id": batch_id,
                        "batch_number": batch.batch_number,
                        "batch_status": batch.status,
                    },
                }
            if batch.quantity_base_units < qty_needed:
                return {
                    "has_conflict": True,
                    "conflict_category": SyncConflictCategory.STALE_STOCK,
                    "error_code": SyncConflictErrorCode.CONFLICT_STALE_STOCK,
                    "conflict_reason": (
                        f"Batch '{batch_id}' has insufficient server stock ({batch.quantity_base_units} units) "
                        f"for offline sale of {qty_needed} units."
                    ),
                    "disputed_monetary_bdt": monetary_amt,
                    "server_snapshot": {
                        "batch_id": batch_id,
                        "batch_number": batch.batch_number,
                        "server_quantity_base_units": batch.quantity_base_units,
                        "requested_quantity_base_units": qty_needed,
                        "shortage_units": qty_needed - batch.quantity_base_units,
                    },
                }

        # No invariant violations detected
        return {
            "has_conflict": False,
            "conflict_category": None,
            "error_code": None,
            "conflict_reason": "",
            "disputed_monetary_bdt": Decimal("0.00"),
            "server_snapshot": {},
        }

    @classmethod
    @transaction.atomic
    def record_conflict_ticket(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        command: Dict[str, Any],
        classification: Dict[str, Any],
        actor_user: Any = None,
    ) -> SyncConflictRecord:
        """
        Creates an immutable SyncConflictRecord in the review queue.
        Guarantees non-destructive routing: NEVER silently modifies or deletes posted local sales.
        """
        tenant = cls.assert_tenant_active(tenant_id)
        device_reg = DeviceLocalDbRegistration.objects.select_related("branch").filter(
            tenant=tenant, device_id=device_id
        ).first()
        if not device_reg:
            raise NotFound({"error_code": "ERR_DEVICE_NOT_FOUND", "detail": f"Device '{device_id}' not found."})

        cmd_id = str(command.get("command_id") or f"cmd-{uuid.uuid4().hex[:10]}").strip()
        idem_key = str(command.get("idempotency_key") or f"idem-{uuid.uuid4().hex[:12]}").strip()
        seq = int(command.get("client_sequence", 1))
        monetary_amt = q2(Decimal(str(classification.get("disputed_monetary_bdt", "0.00"))))

        ticket_id = f"conf-{uuid.uuid4().hex[:12]}"

        ticket = SyncConflictRecord.objects.create(
            conflict_id=ticket_id,
            tenant=tenant,
            branch=device_reg.branch,
            device_registration=device_reg,
            command_id=cmd_id,
            idempotency_key=idem_key,
            client_sequence=seq,
            conflict_category=classification["conflict_category"],
            error_code=classification["error_code"],
            disputed_monetary_bdt=monetary_amt,
            local_payload_json=command,
            server_state_snapshot=classification.get("server_snapshot", {}),
            resolution_status=SyncConflictResolutionStatus.OPEN,
            resolution_notes=classification.get("conflict_reason", ""),
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=str(getattr(actor_user, "user_id", "") or ""),
            actor_email=str(getattr(actor_user, "email", "") or ""),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SYNC_CONFLICT_CLASSIFIED",
            resource_type="SyncConflictRecord",
            resource_id=ticket.conflict_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "command_id": cmd_id,
                "idempotency_key": idem_key,
                "device_id": device_id,
                "conflict_category": ticket.conflict_category,
                "error_code": ticket.error_code,
                "disputed_monetary_bdt": str(monetary_amt),
            },
        )

        return ticket

    @classmethod
    @transaction.atomic
    def propose_conflict_decision(
        cls,
        *,
        tenant_id: str,
        conflict_id: str,
        proposed_decision: str,
        proposed_notes: str,
        actor_user: Any = None,
    ) -> SyncConflictRecord:
        """
        Manager proposes a resolution decision in the Conflict Review Center.
        """
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})

        user_role = getattr(actor_user, "role", None)
        if user_role not in CONFLICT_RESOLVE_ROLES:
            raise PermissionDenied(
                {"error_code": "ERR_PERMISSION_DENIED", "detail": "Only Pharmacy Owner or Branch Manager can propose decisions."}
            )

        tenant = cls.assert_tenant_active(tenant_id)
        ticket = (
            SyncConflictRecord.objects.select_for_update()
            .select_related("tenant", "branch", "device_registration")
            .filter(tenant=tenant, conflict_id=conflict_id)
            .first()
        )
        if not ticket:
            raise NotFound({"error_code": "ERR_CONFLICT_NOT_FOUND", "detail": f"Conflict ticket '{conflict_id}' not found."})

        if ticket.resolution_status not in (SyncConflictResolutionStatus.OPEN, SyncConflictResolutionStatus.DECISION_PROPOSED):
            raise ValidationError(
                {"error_code": "ERR_CONFLICT_ALREADY_RESOLVED", "detail": f"Cannot propose decision for '{ticket.resolution_status}' ticket."}
            )

        decision = str(proposed_decision or "").strip().upper()
        if decision not in ConflictProposedDecision.values:
            raise ValidationError(
                {"error_code": "ERR_INVALID_PROPOSED_DECISION", "detail": f"Valid decisions: {ConflictProposedDecision.values}."}
            )

        notes = str(proposed_notes or "").strip()
        if len(notes) < 3:
            raise ValidationError(
                {"error_code": "ERR_NOTES_REQUIRED", "detail": "Proposed decision notes must be at least 3 characters."}
            )

        ticket.proposed_decision = decision
        ticket.proposed_by = actor_user
        ticket.proposed_at = timezone.now()
        ticket.proposed_notes = notes
        ticket.resolution_status = SyncConflictResolutionStatus.DECISION_PROPOSED
        ticket.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=str(getattr(actor_user, "user_id", "") or ""),
            actor_email=str(getattr(actor_user, "email", "") or ""),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SYNC_CONFLICT_DECISION_PROPOSED",
            resource_type="SyncConflictRecord",
            resource_id=ticket.conflict_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "proposed_decision": ticket.proposed_decision,
                "proposed_notes": ticket.proposed_notes,
            },
        )

        return ticket

    @classmethod
    @transaction.atomic
    def approve_and_execute_decision(
        cls,
        *,
        tenant_id: str,
        conflict_id: str,
        approval_notes: str,
        resolution_reference_id: str = "",
        actor_user: Any = None,
    ) -> SyncConflictRecord:
        """
        Manager or Owner approves and authoritatively executes the proposed decision into final cloud outcome.
        """
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})

        user_role = getattr(actor_user, "role", None)
        if user_role not in CONFLICT_RESOLVE_ROLES:
            raise PermissionDenied(
                {"error_code": "ERR_PERMISSION_DENIED", "detail": "Only Pharmacy Owner or Branch Manager can approve decisions."}
            )

        tenant = cls.assert_tenant_active(tenant_id)
        ticket = (
            SyncConflictRecord.objects.select_for_update()
            .select_related("tenant", "branch", "device_registration")
            .filter(tenant=tenant, conflict_id=conflict_id)
            .first()
        )
        if not ticket:
            raise NotFound({"error_code": "ERR_CONFLICT_NOT_FOUND", "detail": f"Conflict ticket '{conflict_id}' not found."})

        if not ticket.proposed_decision:
            raise ValidationError(
                {"error_code": "ERR_NO_DECISION_PROPOSED", "detail": "No decision has been proposed yet for this ticket."}
            )

        app_notes = str(approval_notes or "").strip()
        if len(app_notes) < 3:
            raise ValidationError(
                {"error_code": "ERR_APPROVAL_NOTES_REQUIRED", "detail": "Approval notes must be at least 3 characters."}
            )

        ref_id = str(resolution_reference_id or f"app-{uuid.uuid4().hex[:8]}").strip()

        # Map proposed decision to authoritative final resolution status
        decision_map = {
            ConflictProposedDecision.KEEP_SERVER_DISCARD_LOCAL: SyncConflictResolutionStatus.RESOLVED_KEEP_SERVER,
            ConflictProposedDecision.OVERRIDE_WITH_INVENTORY_ADJUSTMENT: SyncConflictResolutionStatus.RESOLVED_ADJUSTED,
            ConflictProposedDecision.SPLIT_BATCH_ALLOCATION: SyncConflictResolutionStatus.RESOLVED_ADJUSTED,
            ConflictProposedDecision.ESCALATE_TO_OWNER: SyncConflictResolutionStatus.ESCALATED,
            ConflictProposedDecision.REJECT_FRAUD: SyncConflictResolutionStatus.REJECTED,
        }
        final_status = decision_map.get(ticket.proposed_decision, SyncConflictResolutionStatus.RESOLVED_KEEP_SERVER)

        ticket.resolution_status = final_status
        ticket.approved_by = actor_user
        ticket.approved_at = timezone.now()
        ticket.approval_notes = app_notes
        ticket.resolved_by = actor_user
        ticket.resolved_at = timezone.now()
        ticket.resolution_notes = f"Approved '{ticket.proposed_decision}': {app_notes}"
        ticket.resolution_reference_id = ref_id

        # Generate final cloud outcome audit package
        ticket.final_cloud_outcome = {
            "execution_status": "COMMITTED_CLOUD",
            "final_resolution_status": final_status,
            "decision": ticket.proposed_decision,
            "reference_id": ref_id,
            "reconciled_disputed_bdt": str(ticket.disputed_monetary_bdt),
            "approved_by_email": str(getattr(actor_user, "email", "") or ""),
            "executed_at": timezone.now().isoformat(),
        }
        ticket.save()

        # Emit change feed event
        from apps.core.change_feed_service import ChangeFeedService
        ChangeFeedService.publish_change_event(
            tenant_id=str(tenant.tenant_id),
            branch_id=str(ticket.branch.branch_id),
            entity_type=ChangeFeedEntityType.BATCH_STOCK,
            entity_id=ticket.conflict_id,
            operation=ChangeFeedOperation.REVERSAL if final_status == SyncConflictResolutionStatus.REJECTED else ChangeFeedOperation.UPSERT,
            payload_json={
                "conflict_id": ticket.conflict_id,
                "command_id": ticket.command_id,
                "resolution_status": ticket.resolution_status,
                "proposed_decision": ticket.proposed_decision,
                "resolved_by_id": str(getattr(actor_user, "user_id", "") or ""),
                "resolution_reference_id": ticket.resolution_reference_id,
                "final_cloud_outcome": ticket.final_cloud_outcome,
                "reversal_reason": "SYNC_CONFLICT_REJECTED" if final_status == SyncConflictResolutionStatus.REJECTED else "",
            },
            system_internal=True,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=str(getattr(actor_user, "user_id", "") or ""),
            actor_email=str(getattr(actor_user, "email", "") or ""),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SYNC_CONFLICT_DECISION_APPROVED_EXECUTED",
            resource_type="SyncConflictRecord",
            resource_id=ticket.conflict_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "final_resolution_status": ticket.resolution_status,
                "proposed_decision": ticket.proposed_decision,
                "resolution_reference_id": ticket.resolution_reference_id,
                "approval_notes": ticket.approval_notes,
            },
        )

        return ticket

    @classmethod
    @transaction.atomic
    def resolve_conflict_ticket(
        cls,
        *,
        tenant_id: str,
        conflict_id: str,
        resolution_status: str,
        resolution_notes: str,
        resolution_reference_id: str = "",
        actor_user: Any = None,
    ) -> SyncConflictRecord:
        """
        Audited resolution workflow: Only branch managers, pharmacy owners, or finance officers
        can resolve conflict tickets.
        """
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})

        user_role = getattr(actor_user, "role", None)
        if user_role not in CONFLICT_RESOLVE_ROLES:
            raise PermissionDenied(
                {"error_code": "ERR_PERMISSION_DENIED", "detail": "Only Pharmacy Owner or Branch Manager can resolve conflicts."}
            )

        tenant = cls.assert_tenant_active(tenant_id)
        ticket = (
            SyncConflictRecord.objects.select_for_update()
            .select_related("tenant", "branch", "device_registration")
            .filter(tenant=tenant, conflict_id=conflict_id)
            .first()
        )
        if not ticket:
            raise NotFound({"error_code": "ERR_CONFLICT_NOT_FOUND", "detail": f"Conflict ticket '{conflict_id}' not found."})

        if ticket.resolution_status != SyncConflictResolutionStatus.OPEN:
            raise ValidationError(
                {"error_code": "ERR_CONFLICT_ALREADY_RESOLVED", "detail": f"Ticket is already '{ticket.resolution_status}'."}
            )

        valid_resolutions = {
            SyncConflictResolutionStatus.RESOLVED_KEEP_SERVER,
            SyncConflictResolutionStatus.RESOLVED_ADJUSTED,
            SyncConflictResolutionStatus.ESCALATED,
            SyncConflictResolutionStatus.REJECTED,
        }
        res_status = str(resolution_status).strip().upper()
        if res_status not in valid_resolutions:
            raise ValidationError(
                {"error_code": "ERR_INVALID_RESOLUTION_STATUS", "detail": f"Valid statuses: {sorted(list(valid_resolutions))}."}
            )

        ticket.resolution_status = res_status
        ticket.resolution_notes = str(resolution_notes or "").strip()
        ticket.resolution_reference_id = str(resolution_reference_id or f"adj-{uuid.uuid4().hex[:8]}").strip()
        ticket.resolved_by = actor_user
        ticket.resolved_at = timezone.now()
        ticket.save()

        # Emit audited P307 change feed event via authoritative ChangeFeedService
        from apps.core.change_feed_service import ChangeFeedService
        ChangeFeedService.publish_change_event(
            tenant_id=str(tenant.tenant_id),
            branch_id=str(ticket.branch.branch_id),
            entity_type=ChangeFeedEntityType.BATCH_STOCK,
            entity_id=ticket.conflict_id,
            operation=ChangeFeedOperation.REVERSAL if res_status == SyncConflictResolutionStatus.REJECTED else ChangeFeedOperation.UPSERT,
            payload_json={
                "conflict_id": ticket.conflict_id,
                "command_id": ticket.command_id,
                "resolution_status": ticket.resolution_status,
                "resolved_by_id": str(getattr(actor_user, "user_id", "") or ""),
                "resolution_reference_id": ticket.resolution_reference_id,
                "reversal_reason": "SYNC_CONFLICT_REJECTED" if res_status == SyncConflictResolutionStatus.REJECTED else "",
            },
            system_internal=True,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=str(getattr(actor_user, "user_id", "") or ""),
            actor_email=str(getattr(actor_user, "email", "") or ""),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SYNC_CONFLICT_RESOLVED",
            resource_type="SyncConflictRecord",
            resource_id=ticket.conflict_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "resolution_status": ticket.resolution_status,
                "resolution_notes": ticket.resolution_notes,
                "resolution_reference_id": ticket.resolution_reference_id,
            },
        )

        return ticket

    @classmethod
    def list_conflicts(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        resolution_status: Optional[str] = None,
        conflict_category: Optional[str] = None,
        error_code: Optional[str] = None,
        actor_user: Any = None,
    ):
        tenant = cls.assert_tenant_active(tenant_id)
        qs = SyncConflictRecord.objects.select_related("branch", "device_registration", "resolved_by").filter(tenant=tenant)
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)
        if resolution_status:
            qs = qs.filter(resolution_status=resolution_status.upper())
        if conflict_category:
            qs = qs.filter(conflict_category=conflict_category.upper())
        if error_code:
            qs = qs.filter(error_code=error_code.upper())
        return qs.order_by("-created_at")

    @classmethod
    def get_conflict_summary(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        qs = cls.list_conflicts(tenant_id=tenant_id, branch_id=branch_id, actor_user=actor_user)
        total_count = qs.count()
        open_count = qs.filter(resolution_status=SyncConflictResolutionStatus.OPEN).count()

        open_tickets = qs.filter(resolution_status=SyncConflictResolutionStatus.OPEN)
        total_disputed_bdt = sum((t.disputed_monetary_bdt for t in open_tickets), Decimal("0.00"))

        category_breakdown: Dict[str, int] = {}
        for cat in SyncConflictCategory.values:
            cnt = open_tickets.filter(conflict_category=cat).count()
            if cnt > 0:
                category_breakdown[cat] = cnt

        return {
            "total_conflicts": total_count,
            "open_conflicts": open_count,
            "total_disputed_bdt": str(q2(total_disputed_bdt)),
            "category_breakdown": category_breakdown,
        }
