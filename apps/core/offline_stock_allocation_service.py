"""
backend/apps/core/offline_stock_allocation_service.py
Architecture Decision: ADR-0310 (P310).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Authoritative Offline Stock Allocation Policy & Non-Destructive Oversell Dispute Service:
- Governs branch-level offline reservation policy (`STRICT_LOCAL_ONLY`, `SOFT_RESERVE_FLAG_OVERSELL`,
  `CONTROLLED_RECONCILIATION`) and honest local reserve caps (`max_offline_reserve_pct`).
- Strictly blocks offline reservation on expired, quarantined, or recalled batches (`BatchStock`).
- Reconciles multi-device offline reservations under `select_for_update()` locking:
  - When combined reservations fit within `BatchStock.quantity_base_units`, commits cleanly and deducts stock.
  - When a second device causes cross-device oversell, NEVER silently modifies or reverses posted records
    or drives stock negative — instead creates an immutable `OfflineStockOversellDispute` with exact
    Decimal cost valuation (`disputed_cost_bdt`).
- Requires authorized Owner/Manager/Finance explicit audited resolution (`RESOLVED_STOCK_ADJUSTMENT`,
  `RESOLVED_AUDITED_REVERSAL`, `RESOLVED_SHORTAGE_LOGGED`) and emits a P307 `ChangeFeedEvent`.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.change_feed_service import ChangeFeedService, TENANT_WIDE_ROLES
from apps.core.double_entry_journal_service import q2
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    OfflineReservationStatus,
    OfflineStockAllocationPolicy,
    OfflineStockOversellDispute,
    OfflineStockPolicyMode,
    OfflineStockReservation,
    OversellDisputeStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

POLICY_AND_DISPUTE_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "FINANCE_OFFICER",
    "OWNER_ADMIN",
    "ADMIN",
}


class OfflineStockAllocationService:
    """
    Honest offline reservation policy engine & non-destructive two-device oversell dispute resolver.
    """

    @classmethod
    def _serialize_policy(cls, p: OfflineStockAllocationPolicy) -> Dict[str, Any]:
        return {
            "policy_id": p.policy_id,
            "tenant_id": str(p.tenant_id),
            "branch_id": str(p.branch_id),
            "policy_mode": p.policy_mode,
            "max_offline_reserve_pct": p.max_offline_reserve_pct,
            "block_expired_and_quarantined": p.block_expired_and_quarantined,
            "allow_negative_stock_posting": p.allow_negative_stock_posting,
            "require_manager_for_dispute_resolution": p.require_manager_for_dispute_resolution,
            "is_archived": p.is_archived,
            "stock_certainty_label_en": "Local Estimate — Pending Cloud Reconciliation",
            "stock_certainty_label_bn": "লোকাল আনুমানিক স্টক — ক্লাউড সমন্বয়ের অপেক্ষায়",
            "updated_at": p.updated_at.isoformat(),
        }

    @classmethod
    def _serialize_reservation(cls, r: OfflineStockReservation) -> Dict[str, Any]:
        return {
            "reservation_id": r.reservation_id,
            "tenant_id": str(r.tenant_id),
            "branch_id": str(r.branch_id),
            "device_id": r.device_registration.device_id,
            "batch_id": str(r.batch_id),
            "local_invoice_ref": r.local_invoice_ref,
            "idempotency_key": r.idempotency_key,
            "reserved_base_units": r.reserved_base_units,
            "unit_cost_bdt": str(r.unit_cost_bdt),
            "reserved_value_bdt": str(q2(r.reserved_value_bdt)),
            "status": r.status,
            "notes": r.notes,
            "created_at": r.created_at.isoformat(),
        }

    @classmethod
    def _serialize_dispute(cls, d: OfflineStockOversellDispute) -> Dict[str, Any]:
        return {
            "dispute_id": d.dispute_id,
            "tenant_id": str(d.tenant_id),
            "branch_id": str(d.branch_id),
            "batch_id": str(d.batch_id),
            "reservation_id": d.reservation_id,
            "device_id": d.device_registration.device_id,
            "available_units_at_reconcile": d.available_units_at_reconcile,
            "requested_units": d.requested_units,
            "oversell_shortage_units": d.oversell_shortage_units,
            "unit_cost_bdt": str(d.unit_cost_bdt),
            "disputed_cost_bdt": str(q2(d.disputed_cost_bdt)),
            "status": d.status,
            "posted_records_preserved": d.posted_records_preserved,
            "resolution_reference_id": d.resolution_reference_id,
            "resolution_notes": d.resolution_notes,
            "resolved_at": d.resolved_at.isoformat() if d.resolved_at else None,
            "created_at": d.created_at.isoformat(),
        }

    @classmethod
    def _assert_manager_role(cls, actor_user: Any, branch_id: Optional[str] = None) -> None:
        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in POLICY_AND_DISPUTE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_POLICY_ROLE_FORBIDDEN",
                    "detail": "Only Pharmacy Owner, Branch Manager, or Finance Officer may manage policies or resolve oversell disputes.",
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
        policy_mode: str = OfflineStockPolicyMode.CONTROLLED_RECONCILIATION,
        max_offline_reserve_pct: int = 80,
        allow_negative_stock_posting: bool = False,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)
        cls._assert_manager_role(actor_user, branch_id=branch_id)

        branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
        if not branch:
            foreign = Branch.objects.filter(branch_id=branch_id).first()
            if foreign:
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_BRANCH", "detail": "Branch belongs to another tenant."})
            raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        mode = str(policy_mode or "").strip().upper()
        if mode not in OfflineStockPolicyMode.values:
            raise ValidationError({"error_code": "ERR_INVALID_POLICY_MODE", "detail": f"Unsupported policy_mode '{policy_mode}'."})

        try:
            pct = int(max_offline_reserve_pct)
            if pct < 1 or pct > 100:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_RESERVE_PCT", "detail": "max_offline_reserve_pct must be 1..100."})

        # Safety invariant: negative stock posting is NEVER silently allowed in pharmacy SaaS
        if allow_negative_stock_posting is True:
            raise ValidationError(
                {
                    "error_code": "ERR_NEGATIVE_STOCK_FORBIDDEN",
                    "detail": "Pharmacy compliance forbids silent negative stock posting; use oversell dispute queue.",
                }
            )

        policy, created = OfflineStockAllocationPolicy.objects.select_for_update().update_or_create(
            branch=branch,
            defaults={
                "tenant": tenant,
                "policy_mode": mode,
                "max_offline_reserve_pct": pct,
                "block_expired_and_quarantined": True,
                "allow_negative_stock_posting": False,
                "require_manager_for_dispute_resolution": True,
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
        policy = OfflineStockAllocationPolicy.objects.select_for_update().filter(tenant=tenant, policy_id=policy_id).first()
        if not policy:
            raise NotFound({"error_code": "ERR_POLICY_NOT_FOUND", "detail": f"Policy '{policy_id}' not found."})
        cls._assert_manager_role(actor_user, branch_id=str(policy.branch_id))
        policy.is_archived = True
        policy.updated_by = actor_user
        policy.save(update_fields=["is_archived", "updated_by", "updated_at"])
        return {"policy": cls._serialize_policy(policy)}

    @classmethod
    def list_policies(cls, *, tenant_id: str, actor_user: Any = None) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)
        qs = OfflineStockAllocationPolicy.objects.filter(tenant=tenant, is_archived=False)
        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            qs = qs.filter(branch_id=str(getattr(actor_user, "branch_id", "") or ""))
        rows = [cls._serialize_policy(p) for p in qs[:50]]
        return {"tenant_id": str(tenant.tenant_id), "count": len(rows), "policies": rows}

    @classmethod
    @transaction.atomic
    def reconcile_offline_reservation(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        device_fingerprint_sha256: str,
        batch_id: str,
        reserved_base_units: int,
        local_invoice_ref: str,
        idempotency_key: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Reconciles an offline POS device's batch stock reservation against authoritative BatchStock.
        - Blocks expired/quarantined/recalled stock unconditionally.
        - Replay-safe via UNIQUE (tenant, idempotency_key).
        - Detects two-device cross-device oversell under row lock without silently mutating posted records!
        """
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        reg = ChangeFeedService._assert_device(tenant, device_id, device_fingerprint_sha256, actor_user)

        idem = str(idempotency_key or "").strip()
        inv_ref = str(local_invoice_ref or "").strip()
        if not idem or not inv_ref or not batch_id:
            raise ValidationError(
                {"error_code": "ERR_INCOMPLETE_RESERVATION", "detail": "batch_id, local_invoice_ref, and idempotency_key are required."}
            )

        try:
            units = int(reserved_base_units)
            if units < 1:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError({"error_code": "ERR_INVALID_UNITS", "detail": "reserved_base_units must be >= 1."})

        # Replay-safe idempotency check
        existing = OfflineStockReservation.objects.select_related("device_registration").filter(
            tenant=tenant, idempotency_key=idem
        ).first()
        if existing:
            dispute = existing.oversell_disputes.first()
            return {
                "idempotent_replay": True,
                "oversell_detected": existing.status == OfflineReservationStatus.OVERSELL_DISPUTE,
                "reservation": cls._serialize_reservation(existing),
                "dispute": cls._serialize_dispute(dispute) if dispute else None,
            }

        batch = (
            BatchStock.objects.select_for_update()
            .filter(tenant=tenant, branch=reg.branch, batch_id=str(batch_id).strip())
            .first()
        )
        if not batch:
            foreign_batch = BatchStock.objects.filter(batch_id=str(batch_id).strip()).first()
            if foreign_batch:
                raise PermissionDenied(
                    {"error_code": "ERR_CROSS_TENANT_OR_BRANCH_BATCH", "detail": "Batch belongs to another tenant or branch."}
                )
            raise NotFound({"error_code": "ERR_BATCH_NOT_FOUND", "detail": f"Batch '{batch_id}' not found."})

        # Hard pharmacy safety rule: NEVER sell expired, quarantined, or recalled batches!
        today = timezone.now().date()
        if batch.status != BatchStatus.ACTIVE or (batch.expiry_date and batch.expiry_date <= today):
            raise ValidationError(
                {
                    "error_code": "ERR_BATCH_NOT_SELLABLE_OFFLINE",
                    "detail": f"Batch '{batch.batch_number}' is expired, quarantined, or inactive and cannot be allocated.",
                    "message_en": "Expired or quarantined medicine batch cannot be dispensed.",
                    "message_bn": "মেয়াদোত্তীর্ণ বা কোয়ারেন্টাইনকৃত ব্যাচ বিক্রয় করা যাবে না।",
                }
            )

        policy = OfflineStockAllocationPolicy.objects.filter(tenant=tenant, branch=reg.branch, is_archived=False).first()
        unit_cost = batch.cost_per_unit
        res_value = q2(Decimal(units) * unit_cost)

        # Case A: Sufficient cloud stock -> commit cleanly and deduct BatchStock
        if batch.quantity_base_units >= units:
            batch.quantity_base_units -= units
            batch.save(update_fields=["quantity_base_units", "updated_at"])

            res = OfflineStockReservation.objects.create(
                tenant=tenant,
                branch=reg.branch,
                device_registration=reg,
                batch=batch,
                local_invoice_ref=inv_ref,
                idempotency_key=idem,
                reserved_base_units=units,
                unit_cost_bdt=unit_cost,
                reserved_value_bdt=res_value,
                status=OfflineReservationStatus.COMMITTED,
                notes=f"Committed cleanly ({units} units deducted; remaining={batch.quantity_base_units}).",
            )
            return {
                "idempotent_replay": False,
                "oversell_detected": False,
                "remaining_batch_units": batch.quantity_base_units,
                "policy_mode": policy.policy_mode if policy else OfflineStockPolicyMode.CONTROLLED_RECONCILIATION,
                "reservation": cls._serialize_reservation(res),
                "dispute": None,
                "message_en": f"Allocated {units} units cleanly; {batch.quantity_base_units} remaining.",
                "message_bn": f"{units} ইউনিট স্টক সফলভাবে সমন্বয় হয়েছে; অবশিষ্ট {batch.quantity_base_units}।",
            }

        # Case B: Cross-device oversell! NEVER drive stock negative or silently alter posted records!
        avail_before = batch.quantity_base_units
        shortage = units - avail_before
        disputed_cost = q2(Decimal(shortage) * unit_cost)

        res = OfflineStockReservation.objects.create(
            tenant=tenant,
            branch=reg.branch,
            device_registration=reg,
            batch=batch,
            local_invoice_ref=inv_ref,
            idempotency_key=idem,
            reserved_base_units=units,
            unit_cost_bdt=unit_cost,
            reserved_value_bdt=res_value,
            status=OfflineReservationStatus.OVERSELL_DISPUTE,
            notes=f"Cross-device oversell detected: requested {units}, available {avail_before} (shortage {shortage}). Posted stock NOT modified silently.",
        )
        dispute = OfflineStockOversellDispute.objects.create(
            tenant=tenant,
            branch=reg.branch,
            batch=batch,
            reservation=res,
            device_registration=reg,
            available_units_at_reconcile=avail_before,
            requested_units=units,
            oversell_shortage_units=shortage,
            unit_cost_bdt=unit_cost,
            disputed_cost_bdt=disputed_cost,
            status=OversellDisputeStatus.OPEN,
            posted_records_preserved=True,
        )
        return {
            "idempotent_replay": False,
            "oversell_detected": True,
            "remaining_batch_units": batch.quantity_base_units,  # Unchanged! Zero silent mutation!
            "policy_mode": policy.policy_mode if policy else OfflineStockPolicyMode.CONTROLLED_RECONCILIATION,
            "reservation": cls._serialize_reservation(res),
            "dispute": cls._serialize_dispute(dispute),
            "message_en": f"Cross-device oversell detected (shortage {shortage} units, BDT {disputed_cost}). Posted records preserved; dispute queued.",
            "message_bn": f"টু-ডিভাইস ওভারসেল শনাক্ত হয়েছে (ঘাটতি {shortage} ইউনিট, ৳{disputed_cost})। পোস্টেড রেকর্ড অক্ষত রেখে ডিসপুট কিউতে পাঠানো হয়েছে।",
        }

    @classmethod
    def list_disputes(cls, *, tenant_id: str, status_filter: Optional[str] = None, actor_user: Any = None) -> Dict[str, Any]:
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)

        qs = OfflineStockOversellDispute.objects.filter(tenant=tenant).select_related("device_registration", "batch")
        role = getattr(actor_user, "role", None)
        if not getattr(actor_user, "is_superuser", False) and role not in TENANT_WIDE_ROLES:
            qs = qs.filter(branch_id=str(getattr(actor_user, "branch_id", "") or ""))
        if status_filter:
            qs = qs.filter(status=str(status_filter).strip().upper())

        rows = [cls._serialize_dispute(d) for d in qs[:100]]
        return {"tenant_id": str(tenant.tenant_id), "count": len(rows), "disputes": rows}

    @classmethod
    @transaction.atomic
    def resolve_dispute(
        cls,
        *,
        tenant_id: str,
        dispute_id: str,
        resolution_status: str,
        resolution_reference_id: str,
        resolution_notes: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Explicit audited dispute resolution by Owner/Manager/Finance.
        NEVER silently alters historical posted records; records resolution reference & emits P307 ChangeFeedEvent.
        """
        tenant = ChangeFeedService.assert_tenant_active(tenant_id)
        ChangeFeedService._assert_authenticated_tenant_user(tenant, actor_user)

        dispute = (
            OfflineStockOversellDispute.objects.select_for_update()
            .select_related("device_registration", "reservation", "batch")
            .filter(tenant=tenant, dispute_id=dispute_id)
            .first()
        )
        if not dispute:
            raise NotFound({"error_code": "ERR_DISPUTE_NOT_FOUND", "detail": f"Dispute '{dispute_id}' not found."})

        cls._assert_manager_role(actor_user, branch_id=str(dispute.branch_id))

        if dispute.status != OversellDisputeStatus.OPEN:
            raise ValidationError(
                {"error_code": "ERR_DISPUTE_ALREADY_RESOLVED", "detail": f"Dispute already resolved as {dispute.status}."}
            )

        res_status = str(resolution_status or "").strip().upper()
        valid_resolutions = {
            OversellDisputeStatus.RESOLVED_STOCK_ADJUSTMENT,
            OversellDisputeStatus.RESOLVED_AUDITED_REVERSAL,
            OversellDisputeStatus.RESOLVED_SHORTAGE_LOGGED,
        }
        if res_status not in valid_resolutions:
            raise ValidationError(
                {"error_code": "ERR_INVALID_RESOLUTION_STATUS", "detail": f"Unsupported resolution_status '{resolution_status}'."}
            )

        notes = str(resolution_notes or "").strip()
        ref_id = str(resolution_reference_id or "").strip()
        if len(notes) < 5 or not ref_id:
            raise ValidationError(
                {
                    "error_code": "ERR_AUDIT_EVIDENCE_REQUIRED",
                    "detail": "resolution_reference_id and resolution_notes (>= 5 chars) are mandatory for audited resolution.",
                }
            )

        dispute.status = res_status
        dispute.resolution_reference_id = ref_id
        dispute.resolution_notes = notes
        dispute.resolved_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        dispute.resolved_at = timezone.now()
        dispute.posted_records_preserved = True
        dispute.save()

        # Update reservation status explicitly without touching historical posted BatchStock
        if res_status == OversellDisputeStatus.RESOLVED_AUDITED_REVERSAL:
            dispute.reservation.status = OfflineReservationStatus.RELEASED
            dispute.reservation.notes = f"Audited reversal ref={ref_id}: {notes}"
            dispute.reservation.save(update_fields=["status", "notes", "updated_at"])

        # Emit P307 ChangeFeedEvent so all branch terminals observe the audited outcome!
        feed_op = "REVERSAL" if res_status == OversellDisputeStatus.RESOLVED_AUDITED_REVERSAL else "UPSERT"
        ChangeFeedService.publish_change_event(
            tenant_id=str(tenant.tenant_id),
            entity_type="BATCH_STOCK",
            entity_id=str(dispute.batch_id),
            operation=feed_op,
            payload_json={
                "dispute_id": dispute.dispute_id,
                "resolution_status": res_status,
                "resolution_reference_id": ref_id,
                "reversed_entity_id": dispute.reservation.local_invoice_ref,
                "reversal_reason": notes,
                "oversell_shortage_units": dispute.oversell_shortage_units,
                "disputed_cost_bdt": str(q2(dispute.disputed_cost_bdt)),
            },
            branch_id=str(dispute.branch_id),
            entity_version=2,
            monetary_impact_bdt=-q2(dispute.disputed_cost_bdt) if feed_op == "REVERSAL" else q2(dispute.disputed_cost_bdt),
            source_device_id=dispute.device_registration.device_id,
            idempotency_key=f"osdisp-res:{dispute.dispute_id}",
            actor_user=actor_user,
            system_internal=True,
        )

        return {
            "dispute": cls._serialize_dispute(dispute),
            "message_en": f"Oversell dispute resolved via {res_status} (ref {ref_id}); posted records preserved.",
            "message_bn": f"ওভারসেল ডিসপুট {res_status} দ্বারা অডিটেডভাবে সমাধান হয়েছে (রেফ {ref_id}); পোস্টেড রেকর্ড অক্ষত আছে।",
        }
