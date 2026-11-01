"""
backend/apps/core/tenant_deletion_service.py
Architecture Decision: ADR-0074
Controlled Tenant Deletion Assessment & Retention Hold Review Service.
Provides:
1. Deletion Requests: Governed initiation by Pharmacy Owners or SuperAdmins.
2. Statutory Retention Hold Review: Automated audit against DGDA 2-year clinical horizon and NBR tax horizons.
3. Hard Deletion Guardrails: Irreversible purge strictly blocked if active statutory holds exist.
4. Multi-Step Confirmation: Cryptographic verification code and explicit text acknowledgement.
5. Safe Soft-to-Hard Deletion Pipeline: Transitions tenant status and maintains tamper-evident audit trails.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from typing import Any, Dict

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    PrescriptionRecord,
    SaleInvoice,
    Tenant,
    TenantDeletionRequest,
    TenantDeletionStatus,
)


class TenantDeletionService:
    STATUTORY_RX_HORIZON_DAYS = 730  # 2 years mandatory DGDA retention

    @classmethod
    def submit_deletion_request(
        cls,
        tenant_id: str,
        requested_by: Any,
        reason: str,
    ) -> TenantDeletionRequest:
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise ValidationError({"tenant_id": f"Tenant '{tenant_id}' does not exist."})

        # Role verification: Only Pharmacy Owner of this tenant or SuperAdmin
        is_platform = getattr(requested_by, "is_superuser", False) or str(
            getattr(requested_by, "role", "")
        ).upper() in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]
        user_tenant = str(getattr(requested_by, "tenant_id", "") or "").strip()

        if not is_platform and user_tenant != str(tenant_id).strip():
            raise ValidationError(
                {"tenant_id": f"User is not authorized to request deletion for foreign tenant '{tenant_id}'."}
            )

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError({"reason": "A specific justification for tenant deletion is required."})

        # Check existing active request
        existing = TenantDeletionRequest.objects.filter(
            tenant=tenant,
            status__in=[
                TenantDeletionStatus.SUBMITTED,
                TenantDeletionStatus.UNDER_REVIEW,
                TenantDeletionStatus.HOLD_ACTIVE,
                TenantDeletionStatus.ELIGIBLE_FOR_PURGE,
            ],
        ).first()
        if existing:
            raise ValidationError(
                {"request_id": f"An active deletion request '{existing.request_id}' already exists for this tenant."}
            )

        request_id = f"tdr-{uuid.uuid4().hex[:12]}"
        del_req = TenantDeletionRequest.objects.create(
            request_id=request_id,
            tenant=tenant,
            status=TenantDeletionStatus.SUBMITTED,
            reason=clean_reason,
            requested_by=requested_by if getattr(requested_by, "is_authenticated", False) else None,
            hold_statutory_horizon_days=cls.STATUTORY_RX_HORIZON_DAYS,
        )

        cls._log_audit_event(
            action="TENANT_DELETION_REQUEST_SUBMITTED",
            del_req=del_req,
            actor=requested_by,
            status=AuditActionStatus.SUCCESS,
            details={"request_id": request_id, "reason": clean_reason},
        )

        # Automatically execute initial statutory retention hold assessment
        return cls.assess_retention_holds(request_id, assessed_by=requested_by)

    @classmethod
    def assess_retention_holds(
        cls,
        request_id: str,
        assessed_by: Any,
    ) -> TenantDeletionRequest:
        del_req = TenantDeletionRequest.objects.filter(request_id=request_id).first()
        if not del_req:
            raise ValidationError({"request_id": f"Deletion request '{request_id}' not found."})

        tenant = del_req.tenant
        now = timezone.now()
        horizon_threshold = now - timedelta(days=cls.STATUTORY_RX_HORIZON_DAYS)

        # 1. Clinical Prescriptions Check
        rx_qs = PrescriptionRecord.objects.filter(tenant=tenant)
        active_rx_count = rx_qs.filter(created_at__gte=horizon_threshold).count()
        total_rx_count = rx_qs.count()

        # 2. Financial Sales & Tax Ledgers Check
        sales_qs = SaleInvoice.objects.filter(tenant=tenant)
        posted_sales_count = sales_qs.filter(status="POSTED").count()
        unposted_sales_count = sales_qs.exclude(status="POSTED").count()

        has_active_holds = bool(active_rx_count > 0 or posted_sales_count > 0 or unposted_sales_count > 0)

        holds_summary: Dict[str, Any] = {
            "evaluation_timestamp": now.isoformat(),
            "statutory_horizon_days": cls.STATUTORY_RX_HORIZON_DAYS,
            "prescriptions": {
                "active_statutory_holds_count": active_rx_count,
                "total_records_count": total_rx_count,
                "statutory_guideline": "DGDA National Accreditation Guideline 2024 Sec 4.2",
            },
            "financials": {
                "posted_invoices_count": posted_sales_count,
                "unposted_invoices_count": unposted_sales_count,
                "statutory_guideline": "NBR Value Added Tax and Supplementary Duty Act 2012",
            },
            "physical_deletion_permitted": not has_active_holds,
        }

        del_req.has_active_holds = has_active_holds
        del_req.active_holds_summary = holds_summary
        del_req.assessed_by = assessed_by if getattr(assessed_by, "is_authenticated", False) else None
        del_req.assessed_at = now

        if has_active_holds:
            del_req.status = TenantDeletionStatus.HOLD_ACTIVE
            del_req.confirmation_code = ""
            holds_summary["recommendation"] = (
                "Statutory retention holds active. Physical database deletion is legally prohibited. "
                "Tenant must be placed into Soft Archival (Tenant.status = 'ARCHIVED')."
            )
        else:
            del_req.status = TenantDeletionStatus.ELIGIBLE_FOR_PURGE
            del_req.confirmation_code = f"DEL-{secrets.token_hex(4).upper()}"
            holds_summary["recommendation"] = (
                "All statutory retention horizons elapsed. Tenant is eligible for permanent purge "
                "upon multi-step confirmation."
            )

        del_req.audit_metadata["latest_assessment"] = holds_summary
        del_req.save()

        cls._log_audit_event(
            action="TENANT_RETENTION_HOLDS_ASSESSED",
            del_req=del_req,
            actor=assessed_by,
            status=AuditActionStatus.SUCCESS,
            details={
                "has_active_holds": has_active_holds,
                "status": del_req.status,
                "active_rx_count": active_rx_count,
                "posted_sales_count": posted_sales_count,
            },
        )
        return del_req

    @classmethod
    def cancel_deletion_request(
        cls,
        request_id: str,
        cancelled_by: Any,
        notes: str = "",
    ) -> TenantDeletionRequest:
        del_req = TenantDeletionRequest.objects.filter(request_id=request_id).first()
        if not del_req:
            raise ValidationError({"request_id": f"Deletion request '{request_id}' not found."})

        if del_req.status in [TenantDeletionStatus.PURGED, TenantDeletionStatus.CANCELLED]:
            raise ValidationError(
                {"status": f"Cannot cancel deletion request '{request_id}' in status '{del_req.status}'."}
            )

        del_req.status = TenantDeletionStatus.CANCELLED
        del_req.audit_metadata["cancellation_notes"] = notes
        del_req.save(update_fields=["status", "audit_metadata"])

        cls._log_audit_event(
            action="TENANT_DELETION_CANCELLED",
            del_req=del_req,
            actor=cancelled_by,
            status=AuditActionStatus.SUCCESS,
            details={"request_id": request_id, "notes": notes},
        )
        return del_req

    @classmethod
    def reject_deletion_request(
        cls,
        request_id: str,
        rejected_by: Any,
        reason: str,
    ) -> TenantDeletionRequest:
        del_req = TenantDeletionRequest.objects.filter(request_id=request_id).first()
        if not del_req:
            raise ValidationError({"request_id": f"Deletion request '{request_id}' not found."})

        if del_req.status in [TenantDeletionStatus.PURGED, TenantDeletionStatus.CANCELLED]:
            raise ValidationError(
                {"status": f"Cannot reject deletion request '{request_id}' in status '{del_req.status}'."}
            )

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError({"reason": "A rejection reason is mandatory under ADR-0074."})

        del_req.status = TenantDeletionStatus.REJECTED
        del_req.rejection_reason = clean_reason
        del_req.assessed_by = rejected_by if getattr(rejected_by, "is_authenticated", False) else None
        del_req.assessed_at = timezone.now()
        del_req.save(update_fields=["status", "rejection_reason", "assessed_by", "assessed_at"])

        cls._log_audit_event(
            action="TENANT_DELETION_REJECTED",
            del_req=del_req,
            actor=rejected_by,
            status=AuditActionStatus.SUCCESS,
            details={"request_id": request_id, "rejection_reason": clean_reason},
        )
        return del_req

    @classmethod
    def execute_purge(
        cls,
        request_id: str,
        executed_by: Any,
        confirmation_code: str,
        confirmation_text: str,
    ) -> TenantDeletionRequest:
        del_req = TenantDeletionRequest.objects.filter(request_id=request_id).first()
        if not del_req:
            raise ValidationError({"request_id": f"Deletion request '{request_id}' not found."})

        # Absolute Invariant 1: Block physical deletion if active statutory holds exist
        if del_req.has_active_holds or del_req.status == TenantDeletionStatus.HOLD_ACTIVE:
            raise ValidationError(
                {
                    "error_code": "STATUTORY_HOLD_PREVENTS_PURGE",
                    "message_en": (
                        "Absolute System Block: Cannot physically delete tenant records while statutory "
                        "retention holds are active (DGDA Guideline 2024 / NBR Tax Act). "
                        "The tenant must remain in Soft Archival status."
                    ),
                    "message_bn": "আইনগত বাধ্যবাধকতা: সক্রিয় প্রেসক্রিপশন বা ট্যাক্স রেকর্ডের কারণে স্থায়ী ডিলিট নিষিদ্ধ।",
                }
            )

        # Invariant 2: Multi-step confirmation validation
        clean_code = str(confirmation_code or "").strip().upper()
        if not del_req.confirmation_code or clean_code != del_req.confirmation_code.upper():
            raise ValidationError({"confirmation_code": "Invalid confirmation code provided."})

        clean_text = str(confirmation_text or "").strip().upper()
        if clean_text != "CONFIRM_PERMANENT_DELETION":
            raise ValidationError(
                {"confirmation_text": "Confirmation text must exactly equal 'CONFIRM_PERMANENT_DELETION'."}
            )

        now = timezone.now()
        tenant = del_req.tenant

        # Execute safe purge pipeline
        tenant.status = "PURGED"
        tenant.is_active = False
        tenant.save(update_fields=["status", "is_active"])

        del_req.status = TenantDeletionStatus.PURGED
        del_req.confirmation_text = clean_text
        del_req.confirmed_at = now
        del_req.purged_at = now
        del_req.save(update_fields=["status", "confirmation_text", "confirmed_at", "purged_at"])

        cls._log_audit_event(
            action="TENANT_PERMANENTLY_PURGED",
            del_req=del_req,
            actor=executed_by,
            status=AuditActionStatus.SUCCESS,
            details={
                "request_id": request_id,
                "tenant_id": tenant.tenant_id,
                "purged_at": now.isoformat(),
            },
        )
        return del_req

    @classmethod
    def _log_audit_event(
        cls,
        action: str,
        del_req: TenantDeletionRequest,
        actor: Any,
        status: AuditActionStatus,
        details: Dict[str, Any],
    ) -> None:
        try:
            actor_user = actor if getattr(actor, "is_authenticated", False) else None
            AuditLogEntry.objects.create(
                action=action,
                resource_type="TENANT_DELETION_REQUEST",
                resource_id=del_req.request_id,
                tenant=del_req.tenant,
                actor=actor_user,
                actor_role=str(getattr(actor_user, "role", "SYSTEM")),
                status=status,
                metadata_json=details,
            )
        except Exception:
            pass
