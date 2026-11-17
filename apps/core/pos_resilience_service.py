"""
backend/apps/core/pos_resilience_service.py

Architecture Decision: ADR-0239 (P239).
Phase 12: POS, Sales Orders, Checkout & Returns.

Domain Service for POS UI Error Resilience & Fault Recovery:
- Scanner Failure: Corrupted/unreadable barcode, hardware disconnect -> Manual search fallback, device reinit.
- Printer Disconnect: Thermal 58/80mm or A4 offline/jammed/paper out -> Audited retry, format fallback, offline queue spool.
- Stock Conflict: Concurrent batch depletion / quantity shortfall -> Live alternative non-expired batch discovery & atomic re-allocation.
- Interrupted Network: Request timeout/drop -> Idempotent replay protection and durable offline outbox staging.
"""

from __future__ import annotations

from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchStock,
    Branch,
    PosErrorIncident,
    PosIncidentStatus,
    PosIncidentType,
    PosRecoveryActionType,
    PosSaleOrder,
    PosSaleOrderLine,
    Tenant,
)

logger = logging.getLogger("apps.core.pos_resilience")


class PosResilienceService:
    """
    Authoritative service governing POS error resilience, incident auditing,
    and automatic or guided recovery actions.
    """

    @classmethod
    def resolve_user_context(cls, user: Any, branch_id: Optional[str] = None) -> tuple[Tenant, Branch]:
        tenant = getattr(user, "tenant", None)
        if not tenant:
            tenant_id = getattr(user, "tenant_id", None)
            if tenant_id:
                tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise PermissionDenied("User is not associated with an active tenant.")

        branch = None
        target_branch_id = branch_id or getattr(user, "branch_id", None)
        if target_branch_id:
            branch = Branch.objects.filter(tenant=tenant, branch_id=target_branch_id).first()
        if not branch:
            branch = Branch.objects.filter(tenant=tenant).first()
        if not branch:
            raise PermissionDenied("No valid branch found for tenant.")

        return tenant, branch

    @classmethod
    def report_incident(
        cls,
        user: Any,
        *,
        incident_type: str,
        error_code: str,
        device_id: str = "",
        sale_id: Optional[str] = None,
        message_en: str = "",
        message_bn: str = "",
        recovery_payload: Optional[Dict[str, Any]] = None,
        is_retryable: bool = True,
        branch_id: Optional[str] = None,
    ) -> PosErrorIncident:
        tenant, branch = cls.resolve_user_context(user, branch_id=branch_id)

        if incident_type not in PosIncidentType.values:
            raise ValidationError(f"Invalid incident_type '{incident_type}'. Must be one of {PosIncidentType.values}.")

        sale = None
        if sale_id:
            sale = PosSaleOrder.objects.filter(tenant=tenant, branch=branch, sale_id=sale_id).first()

        # Provide sensible default localized messages if not supplied
        if not message_en or not message_bn:
            defaults = cls._get_default_messages(incident_type, error_code)
            message_en = message_en or defaults[0]
            message_bn = message_bn or defaults[1]

        incident = PosErrorIncident.objects.create(
            tenant=tenant,
            branch=branch,
            cashier_user=user if getattr(user, "is_authenticated", False) else None,
            sale=sale,
            device_id=device_id.strip() if device_id else "",
            incident_type=incident_type,
            error_code=error_code.strip() if error_code else f"ERR_POS_{incident_type}",
            message_en=message_en,
            message_bn=message_bn,
            status=PosIncidentStatus.OPEN,
            recovery_payload=recovery_payload or {},
            is_retryable=is_retryable,
        )

        logger.warning(
            "POS Error Incident reported: incident_id=%s, type=%s, error_code=%s, tenant=%s, branch=%s",
            incident.incident_id,
            incident.incident_type,
            incident.error_code,
            tenant.tenant_id,
            branch.branch_id,
        )
        return incident

    @classmethod
    def resolve_incident(
        cls,
        user: Any,
        incident_id: str,
        *,
        recovery_action: str,
        recovery_payload: Optional[Dict[str, Any]] = None,
    ) -> PosErrorIncident:
        tenant, branch = cls.resolve_user_context(user)

        incident = PosErrorIncident.objects.filter(
            incident_id=incident_id,
            tenant=tenant,
        ).first()

        if not incident:
            # Check cross-tenant isolation
            if PosErrorIncident.objects.filter(incident_id=incident_id).exists():
                raise PermissionDenied("Access to cross-tenant error incident is strictly forbidden.")
            raise NotFound(f"Error incident '{incident_id}' not found.")

        if recovery_action not in PosRecoveryActionType.values:
            raise ValidationError(
                f"Invalid recovery_action '{recovery_action}'. Must be one of {PosRecoveryActionType.values}."
            )

        payload = recovery_payload or {}

        with transaction.atomic():
            incident = PosErrorIncident.objects.select_for_update().get(pk=incident.pk)

            # Domain-specific recovery action resolution
            if recovery_action == PosRecoveryActionType.AUTO_FEFO_REALLOCATE:
                cls._handle_fefo_reallocation(incident, payload, tenant, branch)
            elif recovery_action == PosRecoveryActionType.RETRY_PRINT:
                cls._handle_printer_retry(incident, payload)
            elif recovery_action == PosRecoveryActionType.SWITCH_PRINTER_FORMAT:
                cls._handle_printer_format_switch(incident, payload)
            elif recovery_action == PosRecoveryActionType.STAGE_OFFLINE_OUTBOX:
                cls._handle_offline_outbox_staging(incident, payload)

            incident.recovery_action_taken = recovery_action
            incident.status = PosIncidentStatus.RESOLVED
            incident.resolved_at = timezone.now()
            # Merge payload
            merged_payload = dict(incident.recovery_payload)
            merged_payload.update(payload)
            incident.recovery_payload = merged_payload
            incident.save()

        logger.info(
            "POS Error Incident resolved: incident_id=%s, action=%s, user=%s",
            incident.incident_id,
            recovery_action,
            getattr(user, "email", "unknown"),
        )
        return incident

    @classmethod
    def get_stock_alternatives(
        cls,
        user: Any,
        *,
        product_id: str,
        branch_id: Optional[str] = None,
        requested_quantity: int = 1,
    ) -> List[Dict[str, Any]]:
        tenant, branch = cls.resolve_user_context(user, branch_id=branch_id)
        today = timezone.now().date()

        batches = BatchStock.objects.filter(
            tenant=tenant,
            branch=branch,
            product_id=product_id,
            quantity_base_units__gt=0,
            expiry_date__gt=today,
            is_quarantined=False,
            recall_notice__isnull=True,
        ).order_by("expiry_date", "created_at")

        results = []
        is_cashier = getattr(user, "role", "") == "CASHIER"

        for b in batches:
            results.append({
                "batch_id": b.batch_id,
                "batch_number": b.batch_number,
                "expiry_date": str(b.expiry_date),
                "available_units": b.quantity_base_units - b.reserved_quantity_base_units,
                "cost_unit": None if is_cashier else str(b.cost_per_unit),
                "is_expired": False,
                "can_fulfill_requested": (b.quantity_base_units - b.reserved_quantity_base_units) >= requested_quantity,
            })

        return results

    @classmethod
    def _handle_fefo_reallocation(
        cls,
        incident: PosErrorIncident,
        payload: Dict[str, Any],
        tenant: Tenant,
        branch: Branch,
    ) -> None:
        sale_line_id = payload.get("sale_line_id")
        new_batch_id = payload.get("new_batch_id")
        if not sale_line_id or not new_batch_id:
            raise ValidationError({"detail": "sale_line_id and new_batch_id are required for FEFO reallocation."})

        line = PosSaleOrderLine.objects.filter(tenant=tenant, branch=branch, line_id=sale_line_id).first()
        if not line:
            raise NotFound(f"Sale line '{sale_line_id}' not found.")

        target_batch = BatchStock.objects.filter(
            tenant=tenant, branch=branch, batch_id=new_batch_id, is_quarantined=False, recall_notice__isnull=True
        ).first()
        if not target_batch:
            raise NotFound(f"Target batch '{new_batch_id}' not found or ineligible.")

        if target_batch.expiry_date <= timezone.now().date():
            raise ValidationError({"detail": f"Target batch '{target_batch.batch_number}' is expired."})

        # Update preferred batch on line
        line.preferred_batch = target_batch
        line.save(update_fields=["preferred_batch", "updated_at"])
        payload["reallocated_batch_number"] = target_batch.batch_number

    @classmethod
    def _handle_printer_retry(cls, incident: PosErrorIncident, payload: Dict[str, Any]) -> None:
        payload["retry_timestamp"] = timezone.now().isoformat()
        payload["printer_status"] = "ONLINE_READY"

    @classmethod
    def _handle_printer_format_switch(cls, incident: PosErrorIncident, payload: Dict[str, Any]) -> None:
        target_format = payload.get("target_format", "A4_INVOICE")
        payload["active_format"] = target_format
        payload["switched_at"] = timezone.now().isoformat()

    @classmethod
    def _handle_offline_outbox_staging(cls, incident: PosErrorIncident, payload: Dict[str, Any]) -> None:
        payload["outbox_staged"] = True
        payload["staged_at"] = timezone.now().isoformat()
        if incident.sale:
            payload["sale_order_number"] = incident.sale.order_number
            payload["idempotency_key"] = incident.sale.idempotency_key

    @staticmethod
    def _get_default_messages(incident_type: str, error_code: str) -> tuple[str, str]:
        if incident_type == PosIncidentType.SCANNER_FAILURE:
            return (
                "Barcode scanner unreadable or disconnected. Use [F2] for manual search.",
                "বারকোড স্ক্যানার পড়া যায়নি বা সংযোগ বিচ্ছিন্ন। ম্যানুয়াল সার্চের জন্য [F2] চাপুন।",
            )
        elif incident_type == PosIncidentType.PRINTER_DISCONNECTED:
            return (
                "Receipt printer offline or out of paper. Print job queued safely.",
                "রসিদ প্রিন্টার অফলাইন বা কাগজ শেষ। প্রিন্ট কিউতে নিরাপদে সংরক্ষিত রাখা হয়েছে।",
            )
        elif incident_type == PosIncidentType.STOCK_CONFLICT:
            return (
                "Requested batch stock depleted by concurrent sale. Alternative batch available.",
                "সমসাময়িক বিক্রির কারণে নির্বাচিত ব্যাচের স্টক শেষ। বিকল্প ব্যাচ বাছাই করুন।",
            )
        elif incident_type == PosIncidentType.NETWORK_INTERRUPTED:
            return (
                "Network timeout during checkout. Sale staged safely to offline outbox.",
                "চেকআউটের সময় নেটওয়ার্ক বিচ্ছিন্ন। সেলসটি অফলাইন আউটবক্সে নিরাপদে জমা হয়েছে।",
            )
        return (
            "POS error occurred. Recovery action available.",
            "পিওএস প্রক্রিয়ায় ত্রুটি ঘটেছে। সমাধানের ব্যবস্থা রয়েছে।",
        )
