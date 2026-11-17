"""
backend/apps/core/compliance_audit_export_service.py
Architecture Decision: ADR-0258 (P258).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Domain service managing statutory compliance audit exports, composite manifest signing,
time-bounded query filtering, and strict patient privacy (PHI) redactions.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    AntibioticDispenseRegisterEntry,
    AuditExportScope,
    BatchRecallNotification,
    Branch,
    ComplianceAuditExportManifest,
    InspectionEvidenceRecord,
    PharmacyIncidentRecord,
    PrescriptionRecord,
    PrivacyRedactionLevel,
    TemperatureExcursionSafetyReview,
    Tenant,
)
from apps.core.pos_domain_service import PosDomainService


class ComplianceAuditExportService:
    """
    Produces verifiable, tamper-evident statutory compliance audit packages
    for Bangladesh DGDA inspections, Pharmacy Council audits, and internal clinical governance,
    enforcing automated patient privacy redaction and cryptographic manifest hashing.
    """

    ALLOWED_EXPORT_ROLES = ["SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST"]

    @classmethod
    def redact_patient_name(cls, name: Optional[str]) -> str:
        """Masks patient names (e.g., 'Mohammad Rahim' -> 'M*** R***')."""
        if not name or not name.strip():
            return "P-ANONYMOUS"
        parts = name.strip().split()
        masked_parts = []
        for part in parts:
            if len(part) <= 1:
                masked_parts.append(part + "***")
            else:
                masked_parts.append(part[0] + "***")
        return " ".join(masked_parts)

    @classmethod
    def redact_phone_number(cls, phone: Optional[str]) -> str:
        """Masks middle digits of phone numbers (e.g., '+8801712345678' -> '+88017****5678')."""
        if not phone or not phone.strip():
            return "N/A"
        clean = phone.strip()
        if len(clean) >= 10:
            return clean[:6] + "****" + clean[-4:]
        return clean[:2] + "****"

    @classmethod
    def redact_address(cls, address: Optional[str]) -> str:
        """Redacts street-level patient address, preserving district/city."""
        if not address or not address.strip():
            return "N/A"
        return "[REDACTED_RESIDENTIAL_ADDRESS]"

    @classmethod
    def serialize_manifest(cls, manifest: ComplianceAuditExportManifest, user: Any = None) -> Dict[str, Any]:
        """Serializes export manifest with integrity status."""
        computed_hash = manifest.compute_manifest_sha256()
        is_tamper_free = manifest.manifest_sha256 == computed_hash

        return {
            "export_id": manifest.export_id,
            "tenant_id": str(manifest.tenant_id),
            "branch_id": str(manifest.branch_id),
            "branch_name": manifest.branch.name if manifest.branch else "",
            "export_scope": manifest.export_scope,
            "export_scope_display": manifest.get_export_scope_display(),
            "audit_period_start": manifest.audit_period_start.isoformat(),
            "audit_period_end": manifest.audit_period_end.isoformat(),
            "is_patient_data_redacted": manifest.is_patient_data_redacted,
            "redaction_level": manifest.redaction_level,
            "manifest_records_count": manifest.manifest_records_count,
            "total_financial_value_bdt": f"{manifest.total_financial_value_bdt:.2f}",
            "manifest_sha256": manifest.manifest_sha256,
            "is_tamper_free": is_tamper_free,
            "is_sealed": manifest.is_sealed,
            "exported_by_id": str(manifest.exported_by_id) if manifest.exported_by_id else None,
            "exported_by_name": getattr(manifest.exported_by, "full_name", None) or getattr(manifest.exported_by, "email", "Compliance Officer"),
            "exported_at": manifest.exported_at.isoformat() if manifest.exported_at else None,
            "manifest_payload": manifest.manifest_payload,
        }

    @classmethod
    def list_exports(
        cls,
        user: Any,
        branch_id: Optional[str] = None,
        scope_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists historical compliance audit exports."""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_EXPORT_ROLES
        )

        qs = ComplianceAuditExportManifest.objects.filter(tenant=tenant).select_related("branch", "exported_by")
        if branch_id:
            qs = qs.filter(branch_id=str(branch_id).strip())
        if scope_filter:
            qs = qs.filter(export_scope=str(scope_filter).strip().upper())

        return [cls.serialize_manifest(m, user=user) for m in qs]

    @classmethod
    def get_export(cls, user: Any, export_id: str) -> Dict[str, Any]:
        """Retrieves single export manifest with items."""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_EXPORT_ROLES
        )

        record = ComplianceAuditExportManifest.objects.filter(
            export_id=str(export_id).strip(),
            tenant=tenant,
        ).select_related("branch", "exported_by").first()

        if not record:
            raise NotFound({"export_id": f"Compliance audit export '{export_id}' not found."})

        return cls.serialize_manifest(record, user=user)

    @classmethod
    @transaction.atomic
    def generate_audit_export(cls, user: Any, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compiles, redacts, and cryptographically seals a statutory compliance audit export.
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials required.")

        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in cls.ALLOWED_EXPORT_ROLES:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_AUDIT_EXPORT_UNAUTHORIZED",
                    "message_en": "Only credentialed Pharmacists, Branch Managers, and Owners can generate compliance audit exports.",
                    "message_bn": "শুধুমাত্র ফার্মাসিস্ট, ব্রাঞ্চ ম্যানেজার ও ফার্মেসি মালিক অডিট এক্সপোর্ট তৈরি করতে পারেন।",
                }
            )

        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=True, allowed_roles=cls.ALLOWED_EXPORT_ROLES
        )

        # 1. Branch Validation
        branch_id = data.get("branch_id")
        if not branch_id:
            raise ValidationError({"branch_id": "branch_id is required."})
        branch = Branch.objects.filter(branch_id=str(branch_id).strip(), tenant=tenant).first()
        if not branch:
            raise ValidationError({"branch_id": f"Branch '{branch_id}' does not exist for this tenant."})

        # 2. Scope Validation
        raw_scope = data.get("export_scope", AuditExportScope.FULL_REGULATORY_BUNDLE)
        export_scope = str(raw_scope).strip().upper()
        if export_scope not in AuditExportScope.values:
            raise ValidationError({
                "export_scope": f"Invalid export_scope '{export_scope}'. Must be one of: {list(AuditExportScope.values)}"
            })

        # 3. Date Range Validation
        start_date_raw = data.get("start_date") or data.get("audit_period_start")
        end_date_raw = data.get("end_date") or data.get("audit_period_end")

        if not start_date_raw or not end_date_raw:
            raise ValidationError({"date_range": "Both start_date and end_date are required (YYYY-MM-DD)."})

        try:
            s_date = datetime.date.fromisoformat(str(start_date_raw).strip())
            e_date = datetime.date.fromisoformat(str(end_date_raw).strip())
        except ValueError:
            raise ValidationError({"date_range": "Invalid date format. Expected YYYY-MM-DD."})

        if s_date > e_date:
            raise ValidationError({"date_range": "start_date cannot be later than end_date."})

        # 4. Redaction Settings
        is_redacted = bool(data.get("is_patient_data_redacted", True))
        raw_redaction = data.get("redaction_level", PrivacyRedactionLevel.STRICT_STATUTORY)
        redaction_level = str(raw_redaction).strip().upper()
        if redaction_level not in PrivacyRedactionLevel.values:
            redaction_level = PrivacyRedactionLevel.STRICT_STATUTORY

        # 5. Extract and Redact Records
        exported_items = []
        total_val = Decimal("0.00")

        # A. Antibiotic Register Records
        if export_scope in [AuditExportScope.FULL_REGULATORY_BUNDLE, AuditExportScope.ANTIBIOTIC_STEWARDSHIP]:
            ab_qs = AntibioticDispenseRegisterEntry.objects.filter(
                tenant=tenant,
                branch=branch,
                dispensed_at__date__gte=s_date,
                dispensed_at__date__lte=e_date,
            ).select_related("approving_pharmacist", "branch")

            for ab in ab_qs:
                patient_str = cls.redact_patient_name(ab.patient_token) if is_redacted else ab.patient_token

                item_data = {
                    "record_type": "ANTIBIOTIC_DISPENSING",
                    "register_id": ab.register_entry_id,
                    "medicine_name": ab.brand_name,
                    "generic_name": ab.generic_name,
                    "classification_tier": ab.classification_tier,
                    "quantity_dispensed": str(ab.dispensed_base_units),
                    "dispensed_date": ab.dispensed_at.date().isoformat(),
                    "patient_identity_redacted": patient_str,
                    "prescriber_name": ab.doctor_name,
                    "prescriber_bmdc_reg": ab.doctor_bmdc_no,
                    "pharmacist_pcb_reg": ab.approving_pharmacist_pcb_no,
                }
                raw_hash = f"{ab.register_entry_id}|{ab.dispensed_base_units}|{ab.dispensed_at.isoformat()}"
                item_data["item_sha256"] = hashlib.sha256(raw_hash.encode("utf-8")).hexdigest()
                exported_items.append(item_data)

        # B. Temperature Excursion Safety Reviews
        if export_scope in [AuditExportScope.FULL_REGULATORY_BUNDLE, AuditExportScope.COLD_CHAIN_TEMPERATURE]:
            temp_qs = TemperatureExcursionSafetyReview.objects.filter(
                tenant=tenant,
                branch=branch,
                reviewed_at__date__gte=s_date,
                reviewed_at__date__lte=e_date,
            )

            for tr in temp_qs:
                total_val += tr.total_exposed_value_bdt
                item_data = {
                    "record_type": "TEMPERATURE_SAFETY_REVIEW",
                    "review_id": tr.review_id,
                    "product_name": tr.product_name,
                    "batch_number": tr.batch_number,
                    "max_exposed_temp": str(tr.max_exposed_temperature),
                    "review_decision": tr.review_decision,
                    "reviewed_date": tr.reviewed_at.date().isoformat(),
                    "total_exposed_value_bdt": f"{tr.total_exposed_value_bdt:.2f}",
                    "clinical_justification": tr.clinical_justification,
                    "audit_hash": tr.sha256_audit_hash,
                }
                exported_items.append(item_data)

        # C. Inspection Evidence Records
        if export_scope in [AuditExportScope.FULL_REGULATORY_BUNDLE, AuditExportScope.COLD_CHAIN_TEMPERATURE]:
            ev_qs = InspectionEvidenceRecord.objects.filter(
                tenant=tenant,
                branch=branch,
                log_date__gte=s_date,
                log_date__lte=e_date,
                is_archived=False,
            )
            for ev in ev_qs:
                item_data = {
                    "record_type": "INSPECTION_EVIDENCE",
                    "evidence_id": ev.evidence_id,
                    "evidence_type": ev.evidence_type,
                    "title": ev.title,
                    "log_date": ev.log_date.isoformat(),
                    "file_name": ev.file_name,
                    "sha256_hash": ev.sha256_hash,
                }
                exported_items.append(item_data)

        # D. Batch Recalls
        if export_scope in [AuditExportScope.FULL_REGULATORY_BUNDLE, AuditExportScope.INCIDENT_CORRECTION]:
            rcl_qs = BatchRecallNotification.objects.filter(
                tenant=tenant,
                effective_date__gte=s_date,
                effective_date__lte=e_date,
                is_archived=False,
            )
            if branch:
                rcl_qs = rcl_qs.filter(branch=branch)

            for rcl in rcl_qs:
                item_data = {
                    "record_type": "BATCH_RECALL",
                    "recall_id": rcl.recall_id,
                    "batch_number": rcl.batch_number,
                    "product_name": rcl.product_name,
                    "recall_class": rcl.recall_class,
                    "status": rcl.status,
                    "effective_date": rcl.effective_date.isoformat(),
                    "sha256_hash": rcl.sha256_hash,
                }
                exported_items.append(item_data)

        # E. Pharmacy Incidents
        if export_scope in [AuditExportScope.FULL_REGULATORY_BUNDLE, AuditExportScope.INCIDENT_CORRECTION]:
            inc_qs = PharmacyIncidentRecord.objects.filter(
                tenant=tenant,
                branch=branch,
                occurred_at__date__gte=s_date,
                occurred_at__date__lte=e_date,
                is_archived=False,
            )
            for inc in inc_qs:
                item_data = {
                    "record_type": "PHARMACY_INCIDENT",
                    "incident_id": inc.incident_id,
                    "incident_type": inc.incident_type,
                    "severity": inc.severity,
                    "status": inc.status,
                    "incident_date": inc.occurred_at.date().isoformat(),
                    "sha256_hash": inc.sha256_hash,
                }
                exported_items.append(item_data)

        # 6. Composite Manifest Construction
        payload = {
            "metadata": {
                "tenant_name": tenant.name,
                "branch_name": branch.name,
                "scope": export_scope,
                "period_start": s_date.isoformat(),
                "period_end": e_date.isoformat(),
                "exported_by": getattr(user, "email", "Authorized Pharmacist"),
                "exported_at": timezone.now().isoformat(),
                "redaction_applied": is_redacted,
                "redaction_level": redaction_level,
                "statutory_framework": "Bangladesh DGDA National PMS Guideline 2024",
            },
            "records_count": len(exported_items),
            "items": exported_items,
        }

        export_id = f"cae-{uuid.uuid4().hex[:12]}"
        manifest = ComplianceAuditExportManifest(
            export_id=export_id,
            tenant=tenant,
            branch=branch,
            export_scope=export_scope,
            audit_period_start=s_date,
            audit_period_end=e_date,
            is_patient_data_redacted=is_redacted,
            redaction_level=redaction_level,
            manifest_records_count=len(exported_items),
            total_financial_value_bdt=total_val,
            manifest_payload=payload,
            exported_by=user,
            is_sealed=True,
        )
        manifest.manifest_sha256 = manifest.compute_manifest_sha256()
        manifest.save()

        return cls.serialize_manifest(manifest, user=user)

    @classmethod
    def verify_manifest_integrity(cls, user: Any, export_id: str) -> Dict[str, Any]:
        """Cryptographically verifies sealed audit manifest integrity."""
        record_data = cls.get_export(user, export_id)
        return {
            "export_id": record_data["export_id"],
            "manifest_sha256": record_data["manifest_sha256"],
            "is_tamper_free": record_data["is_tamper_free"],
            "is_sealed": record_data["is_sealed"],
            "verified_at": timezone.now().isoformat(),
        }
