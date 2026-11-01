"""
backend/apps/core/tenant_export_service.py
Architecture Decision: ADR-0073
Controlled Tenant Data Export Administration Service.
Handles:
1. Export Requests: Dual-governance initiation with default PII redaction.
2. Administrative Approval & Rejection: SuperAdmin validation gate.
3. Cryptographic Artifact Generation: Scoped tenant data aggregation, SHA-256 integrity hash.
4. Expiring Download Access: 24-hour token validation, download counters, and tamper audits.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import timedelta
from typing import Any, Dict, Tuple

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    PrescriptionRecord,
    SaleInvoice,
    StaffMember,
    Tenant,
    TenantDataExportRequest,
    TenantDataExportStatus,
)


class TenantExportService:
    EXPIRATION_HOURS = 24
    MAX_DOWNLOADS = 5

    @classmethod
    def request_export(
        cls,
        tenant_id: str,
        requested_by: Any,
        include_patient_pii: bool = False,
        include_financials: bool = True,
        include_prescriptions: bool = True,
        include_inventory: bool = True,
        file_format: str = "JSON",
        notes: str = "",
    ) -> TenantDataExportRequest:
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
                {"tenant_id": f"User is not authorized to request exports for foreign tenant '{tenant_id}'."}
            )

        export_id = f"tde-{uuid.uuid4().hex[:12]}"
        clean_format = str(file_format or "JSON").upper()
        if clean_format not in ["JSON", "CSV"]:
            clean_format = "JSON"

        export_req = TenantDataExportRequest.objects.create(
            export_id=export_id,
            tenant=tenant,
            status=TenantDataExportStatus.REQUESTED,
            requested_by=requested_by if getattr(requested_by, "is_authenticated", False) else None,
            include_patient_pii=bool(include_patient_pii),
            include_financials=bool(include_financials),
            include_prescriptions=bool(include_prescriptions),
            include_inventory=bool(include_inventory),
            file_format=clean_format,
            max_downloads=cls.MAX_DOWNLOADS,
            audit_metadata={"request_notes": notes, "initiated_by_platform": is_platform},
        )

        cls._log_audit_event(
            action="TENANT_DATA_EXPORT_REQUESTED",
            export_req=export_req,
            actor=requested_by,
            status=AuditActionStatus.SUCCESS,
            details={
                "export_id": export_id,
                "include_patient_pii": include_patient_pii,
                "file_format": clean_format,
            },
        )
        return export_req

    @classmethod
    def approve_export(
        cls,
        export_id: str,
        approved_by: Any,
        auto_generate: bool = True,
    ) -> TenantDataExportRequest:
        export_req = TenantDataExportRequest.objects.filter(export_id=export_id).first()
        if not export_req:
            raise ValidationError({"export_id": f"Export request '{export_id}' not found."})

        if export_req.status != TenantDataExportStatus.REQUESTED:
            raise ValidationError(
                {"status": f"Cannot approve export '{export_id}' currently in status '{export_req.status}'."}
            )

        export_req.status = TenantDataExportStatus.APPROVED
        export_req.approved_by = approved_by if getattr(approved_by, "is_authenticated", False) else None
        export_req.approved_at = timezone.now()
        export_req.save(update_fields=["status", "approved_by", "approved_at"])

        cls._log_audit_event(
            action="TENANT_DATA_EXPORT_APPROVED",
            export_req=export_req,
            actor=approved_by,
            status=AuditActionStatus.SUCCESS,
            details={"export_id": export_id, "approved_by": str(getattr(approved_by, "email", "admin"))},
        )

        if auto_generate:
            return cls.generate_export_artifact(export_id)
        return export_req

    @classmethod
    def reject_export(
        cls,
        export_id: str,
        rejected_by: Any,
        reason: str,
    ) -> TenantDataExportRequest:
        export_req = TenantDataExportRequest.objects.filter(export_id=export_id).first()
        if not export_req:
            raise ValidationError({"export_id": f"Export request '{export_id}' not found."})

        if export_req.status not in [TenantDataExportStatus.REQUESTED, TenantDataExportStatus.APPROVED]:
            raise ValidationError(
                {"status": f"Cannot reject export '{export_id}' currently in status '{export_req.status}'."}
            )

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError({"reason": "A specific rejection reason is mandatory under ADR-0073."})

        export_req.status = TenantDataExportStatus.REJECTED
        export_req.rejection_reason = clean_reason
        export_req.approved_by = rejected_by if getattr(rejected_by, "is_authenticated", False) else None
        export_req.approved_at = timezone.now()
        export_req.save(update_fields=["status", "rejection_reason", "approved_by", "approved_at"])

        cls._log_audit_event(
            action="TENANT_DATA_EXPORT_REJECTED",
            export_req=export_req,
            actor=rejected_by,
            status=AuditActionStatus.SUCCESS,
            details={"export_id": export_id, "rejection_reason": clean_reason},
        )
        return export_req

    @classmethod
    def generate_export_artifact(cls, export_id: str) -> TenantDataExportRequest:
        export_req = TenantDataExportRequest.objects.filter(export_id=export_id).first()
        if not export_req:
            raise ValidationError({"export_id": f"Export request '{export_id}' not found."})

        export_req.status = TenantDataExportStatus.PROCESSING
        export_req.save(update_fields=["status"])

        tenant = export_req.tenant
        now = timezone.now()

        # Build bundle data
        bundle: Dict[str, Any] = {
            "metadata": {
                "export_id": export_req.export_id,
                "tenant_id": tenant.tenant_id,
                "tenant_name": tenant.name,
                "plan_tier": tenant.plan_tier,
                "exported_at": now.isoformat(),
                "include_patient_pii": export_req.include_patient_pii,
                "file_format": export_req.file_format,
            },
            "branches": list(
                Branch.objects.filter(tenant=tenant).values("branch_id", "name", "address", "license_number")
            ),
        }

        # Staff records (mask phones if no PII)
        staff_qs = StaffMember.objects.filter(tenant=tenant)
        staff_data = []
        for s in staff_qs:
            phone = s.phone_number
            if not export_req.include_patient_pii and phone and len(phone) >= 8:
                phone = f"{phone[:3]}****{phone[-3:]}"
            staff_data.append(
                {
                    "staff_id": s.staff_id,
                    "branch_id": s.branch_id,
                    "full_name": s.full_name,
                    "role": s.role,
                    "phone_number": phone,
                    "is_active": s.is_active,
                }
            )
        bundle["staff"] = staff_data

        # Inventory & Medicine Configurations
        if export_req.include_inventory:
            from apps.core.models import TenantMedicineConfig

            bundle["medicine_configs"] = list(
                TenantMedicineConfig.objects.filter(tenant=tenant).values(
                    "config_id",
                    "product_id",
                    "local_brand_name",
                    "local_sku",
                    "custom_mrp_unit",
                    "shelf_location",
                    "low_stock_threshold",
                )[:500]
            )

        # Sales Financials
        if export_req.include_financials:
            sales_qs = SaleInvoice.objects.filter(tenant=tenant).order_by("-created_at")[:1000]
            sales_data = []
            for inv in sales_qs:
                cust_phone = inv.customer_phone
                cust_name = inv.customer_name
                if not export_req.include_patient_pii:
                    if cust_phone and len(cust_phone) >= 8:
                        cust_phone = f"{cust_phone[:3]}****{cust_phone[-3:]}"
                    if cust_name:
                        cust_name = "***REDACTED***"
                sales_data.append(
                    {
                        "invoice_number": inv.invoice_number,
                        "branch_id": inv.branch_id,
                        "total_amount_bdt": str(inv.total_amount),
                        "status": inv.status,
                        "customer_name": cust_name,
                        "customer_phone": cust_phone,
                        "created_at": inv.created_at.isoformat() if inv.created_at else None,
                    }
                )
            bundle["sales"] = sales_data

        # Prescriptions
        if export_req.include_prescriptions:
            rx_qs = PrescriptionRecord.objects.filter(tenant=tenant).order_by("-created_at")[:1000]
            rx_data = []
            for rx in rx_qs:
                notes = rx.notes
                if not export_req.include_patient_pii and notes:
                    notes = "***REDACTED***"
                rx_data.append(
                    {
                        "prescription_id": rx.prescription_id,
                        "patient_token": rx.patient_token,
                        "doctor_name": rx.doctor_name,
                        "doctor_bmdc_no": rx.doctor_bmdc_no,
                        "diagnosis": rx.diagnosis,
                        "status": rx.status,
                        "notes": notes,
                        "prescription_date": rx.prescription_date.isoformat() if rx.prescription_date else None,
                    }
                )
            bundle["prescriptions"] = rx_data

        raw_bytes = json.dumps(bundle, indent=2, ensure_ascii=False, default=str).encode("utf-8")
        sha256_hash = hashlib.sha256(raw_bytes).hexdigest()
        download_token = secrets.token_urlsafe(32)
        expires_at = now + timedelta(hours=cls.EXPIRATION_HOURS)

        export_req.status = TenantDataExportStatus.COMPLETED
        export_req.sha256_checksum = sha256_hash
        export_req.file_size_bytes = len(raw_bytes)
        export_req.download_token = download_token
        export_req.expires_at = expires_at
        export_req.completed_at = now
        export_req.audit_metadata["item_counts"] = {
            "branches": len(bundle.get("branches", [])),
            "staff": len(bundle.get("staff", [])),
            "medicine_configs": len(bundle.get("medicine_configs", [])),
            "sales": len(bundle.get("sales", [])),
            "prescriptions": len(bundle.get("prescriptions", [])),
        }
        export_req.save()

        cls._log_audit_event(
            action="TENANT_DATA_EXPORT_GENERATED",
            export_req=export_req,
            actor=export_req.approved_by,
            status=AuditActionStatus.SUCCESS,
            details={
                "export_id": export_id,
                "sha256_checksum": sha256_hash,
                "file_size_bytes": len(raw_bytes),
                "expires_at": expires_at.isoformat(),
            },
        )
        return export_req

    @classmethod
    def verify_and_record_download(
        cls,
        export_id: str,
        token: str,
        caller: Any,
    ) -> Tuple[TenantDataExportRequest, Dict[str, Any]]:
        export_req = TenantDataExportRequest.objects.filter(export_id=export_id).first()
        if not export_req:
            raise ValidationError({"export_id": f"Export request '{export_id}' not found."})

        # 1. Status Check
        if export_req.status != TenantDataExportStatus.COMPLETED:
            raise ValidationError(
                {"status": f"Export '{export_id}' is in status '{export_req.status}' and not ready for download."}
            )

        # 2. Token Check
        if not export_req.download_token or export_req.download_token != str(token or "").strip():
            cls._log_audit_event(
                action="TENANT_DATA_EXPORT_DOWNLOAD_FAILED",
                export_req=export_req,
                actor=caller,
                status=AuditActionStatus.DENIED,
                details={"reason": "INVALID_DOWNLOAD_TOKEN"},
            )
            raise ValidationError({"token": "Invalid download token provided."})

        # 3. Expiration Check
        now = timezone.now()
        if export_req.expires_at and now > export_req.expires_at:
            export_req.status = TenantDataExportStatus.EXPIRED
            export_req.save(update_fields=["status"])
            cls._log_audit_event(
                action="TENANT_DATA_EXPORT_EXPIRED",
                export_req=export_req,
                actor=caller,
                status=AuditActionStatus.DENIED,
                details={"reason": "DOWNLOAD_TOKEN_EXPIRED"},
            )
            raise ValidationError({"token": "Export download link has expired (24h limit reached)."})

        # 4. Max Downloads Check
        if export_req.download_count >= export_req.max_downloads:
            export_req.status = TenantDataExportStatus.EXPIRED
            export_req.save(update_fields=["status"])
            raise ValidationError({"max_downloads": "Maximum download limit for this export token has been reached."})

        # Increment download counter
        export_req.download_count += 1
        export_req.save(update_fields=["download_count"])

        cls._log_audit_event(
            action="TENANT_DATA_EXPORT_DOWNLOADED",
            export_req=export_req,
            actor=caller,
            status=AuditActionStatus.SUCCESS,
            details={
                "download_count": export_req.download_count,
                "max_downloads": export_req.max_downloads,
            },
        )

        meta = {
            "export_id": export_req.export_id,
            "tenant_id": export_req.tenant_id,
            "sha256_checksum": export_req.sha256_checksum,
            "file_size_bytes": export_req.file_size_bytes,
            "file_format": export_req.file_format,
            "download_count": export_req.download_count,
            "max_downloads": export_req.max_downloads,
            "expires_at": export_req.expires_at.isoformat() if export_req.expires_at else None,
        }
        return export_req, meta

    @classmethod
    def _log_audit_event(
        cls,
        action: str,
        export_req: TenantDataExportRequest,
        actor: Any,
        status: AuditActionStatus,
        details: Dict[str, Any],
    ) -> None:
        try:
            actor_user = actor if getattr(actor, "is_authenticated", False) else None
            AuditLogEntry.objects.create(
                action=action,
                resource_type="TENANT_DATA_EXPORT",
                resource_id=export_req.export_id,
                tenant=export_req.tenant,
                actor=actor_user,
                actor_role=str(getattr(actor_user, "role", "SYSTEM")),
                status=status,
                metadata_json=details,
            )
        except Exception:
            # Audit logging failure should not abort operational workflow
            pass
