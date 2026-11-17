"""
backend/apps/core/incident_management_service.py

Architecture Decision: ADR-0255 (P255).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Service layer for Clinical and Operational Incident Management & Correction Workflow:
- Reporting incorrect dispensing, suspected damages, product recalls, inventory discrepancies
- Severity classification (Low, Medium, High, Critical) and DGDA regulatory flags
- Investigation, Root Cause Analysis (RCA), and Corrective & Preventive Action (CAPA)
- Strict Pharmacist / Manager closure approval gate
- Quarantine stock tracking and branch incident audit metrics
"""

from decimal import Decimal
import datetime
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    IncidentRootCause,
    IncidentSeverity,
    IncidentStatus,
    PharmacyIncidentRecord,
    PharmacyIncidentType,
    Tenant,
)
from apps.core.pos_domain_service import PosDomainService


class IncidentManagementService:
    """
    Domain service for clinical & operational incident management under DGDA guidelines.
    """

    ALLOWED_READ_ROLES = ["SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST", "CASHIER"]
    ALLOWED_CREATE_ROLES = ["SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST", "CASHIER"]
    ALLOWED_INVESTIGATE_ROLES = ["SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST"]
    ALLOWED_CLOSE_ROLES = ["SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST"]

    @classmethod
    def serialize_incident(cls, record: PharmacyIncidentRecord, user: Any = None) -> Dict[str, Any]:
        """
        Serializes PharmacyIncidentRecord with full clinical audit metadata.
        """
        return {
            "incident_id": record.incident_id,
            "tenant_id": str(record.tenant_id),
            "branch_id": str(record.branch_id),
            "branch_name": record.branch.name if record.branch else "",
            "incident_type": record.incident_type,
            "incident_type_display": record.get_incident_type_display(),
            "severity": record.severity,
            "severity_display": record.get_severity_display(),
            "status": record.status,
            "status_display": record.get_status_display(),
            "title": record.title,
            "description": record.description,
            "occurred_at": record.occurred_at.isoformat() if record.occurred_at else None,
            "reported_by_id": str(record.reported_by_id) if record.reported_by_id else None,
            "reported_at": record.reported_at.isoformat() if record.reported_at else None,
            "product_name": record.product_name,
            "batch_number": record.batch_number,
            "affected_quantity": str(record.affected_quantity),
            "financial_impact_bdt": str(record.financial_impact_bdt),
            "sale_id": record.sale_id,
            "prescription_id": record.prescription_id,
            "investigation_notes": record.investigation_notes,
            "root_cause": record.root_cause,
            "root_cause_display": record.get_root_cause_display() if record.root_cause else "",
            "corrective_action_plan": record.corrective_action_plan,
            "preventive_measures": record.preventive_measures,
            "is_stock_quarantined": record.is_stock_quarantined,
            "quarantine_notes": record.quarantine_notes,
            "requires_dgda_notification": record.requires_dgda_notification,
            "dgda_notification_sent": record.dgda_notification_sent,
            "dgda_notification_notes": record.dgda_notification_notes,
            "closed_by_id": str(record.closed_by_id) if record.closed_by_id else None,
            "closed_at": record.closed_at.isoformat() if record.closed_at else None,
            "closure_notes": record.closure_notes,
            "sha256_hash": record.sha256_hash,
            "is_archived": record.is_archived,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        }

    @classmethod
    def list_incidents(
        cls,
        user: Any,
        branch_id: Optional[str] = None,
        incident_type: Optional[str] = None,
        status_filter: Optional[str] = None,
        severity: Optional[str] = None,
        include_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Lists pharmacy incidents filtered by branch, type, status, or severity.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )

        qs = PharmacyIncidentRecord.objects.filter(tenant=tenant).select_related("branch")
        if not include_archived:
            qs = qs.filter(is_archived=False)

        user_role = str(getattr(user, "role", "")).upper()
        if user_role in ("BRANCH_MANAGER", "STAFF_PHARMACIST", "CASHIER"):
            user_branch_id = getattr(user, "branch_id", None) or (user.branch.branch_id if getattr(user, "branch", None) else None)
            if user_branch_id:
                qs = qs.filter(branch_id=user_branch_id)
        elif branch_id:
            qs = qs.filter(branch_id=str(branch_id).strip())

        if incident_type:
            qs = qs.filter(incident_type=str(incident_type).strip().upper())
        if status_filter:
            qs = qs.filter(status=str(status_filter).strip().upper())
        if severity:
            qs = qs.filter(severity=str(severity).strip().upper())

        return [cls.serialize_incident(rec, user=user) for rec in qs]

    @classmethod
    def get_incident(cls, user: Any, incident_id: str) -> Dict[str, Any]:
        """
        Retrieves single pharmacy incident record.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )

        record = PharmacyIncidentRecord.objects.filter(
            incident_id=str(incident_id).strip(),
            tenant=tenant,
        ).select_related("branch").first()

        if not record:
            raise NotFound({"incident_id": f"Pharmacy incident '{incident_id}' not found."})

        return cls.serialize_incident(record, user=user)

    @classmethod
    @transaction.atomic
    def create_or_update_incident(
        cls,
        user: Any,
        data: Dict[str, Any],
        incident_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Creates or updates a pharmacy incident report.
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials required.")

        user_role = str(getattr(user, "role", "")).upper()
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=True, allowed_roles=cls.ALLOWED_CREATE_ROLES
        )

        record = None
        if incident_id:
            if user_role not in cls.ALLOWED_INVESTIGATE_ROLES:
                raise PermissionDenied(
                    detail={
                        "error_code": "ERR_INCIDENT_UPDATE_UNAUTHORIZED",
                        "message_en": "Only Pharmacists and Managers can update incident investigations and CAPA.",
                        "message_bn": "শুধুমাত্র ফার্মাসিস্ট ও ম্যানেজার ইনসিডেন্ট তদন্ত ও সিএপিএ আপডেট করতে পারেন।",
                    }
                )
            record = PharmacyIncidentRecord.objects.filter(
                incident_id=str(incident_id).strip(),
                tenant=tenant,
            ).first()
            if not record:
                raise NotFound({"incident_id": f"Pharmacy incident '{incident_id}' not found."})

        # 1. Title Validation
        raw_title = data.get("title")
        title = str(raw_title).strip() if raw_title is not None else (record.title if record else "")
        if len(title) < 3:
            raise ValidationError({"title": "Incident title (minimum 3 characters) is required."})

        # 2. Type Validation
        raw_type = data.get("incident_type")
        inc_type = str(raw_type).strip().upper() if raw_type is not None else (record.incident_type if record else "")
        if inc_type not in PharmacyIncidentType.values:
            raise ValidationError({
                "incident_type": f"Invalid incident type '{inc_type}'. Must be one of: {list(PharmacyIncidentType.values)}"
            })

        # 3. Severity Validation
        raw_sev = data.get("severity")
        severity = str(raw_sev).strip().upper() if raw_sev is not None else (record.severity if record else IncidentSeverity.MEDIUM)
        if severity not in IncidentSeverity.values:
            raise ValidationError({
                "severity": f"Invalid severity '{severity}'. Must be one of: {list(IncidentSeverity.values)}"
            })

        # 4. Description Validation
        raw_desc = data.get("description")
        description = str(raw_desc).strip() if raw_desc is not None else (record.description if record else "")
        if not description:
            raise ValidationError({"description": "Incident description is required."})

        # 5. Branch Validation
        branch_id = data.get("branch_id") or getattr(user, "branch_id", None)
        branch = None
        if branch_id:
            branch = Branch.objects.filter(branch_id=str(branch_id).strip(), tenant=tenant).first()
            if not branch:
                raise ValidationError({"branch_id": f"Branch '{branch_id}' does not exist for this tenant."})
        elif not record:
            raise ValidationError({"branch_id": "branch_id is required."})

        # 6. Quantities & Financial Impact Validation
        raw_qty = data.get("affected_quantity")
        if raw_qty is not None:
            try:
                affected_qty = Decimal(str(raw_qty))
                if affected_qty < Decimal("0.00"):
                    raise ValidationError({"affected_quantity": "affected_quantity cannot be negative."})
            except Exception:
                raise ValidationError({"affected_quantity": "Invalid affected_quantity decimal format."})
        else:
            affected_qty = record.affected_quantity if record else Decimal("0.00")

        raw_fin = data.get("financial_impact_bdt")
        if raw_fin is not None:
            try:
                financial_impact = Decimal(str(raw_fin))
                if financial_impact < Decimal("0.00"):
                    raise ValidationError({"financial_impact_bdt": "financial_impact_bdt cannot be negative."})
            except Exception:
                raise ValidationError({"financial_impact_bdt": "Invalid financial_impact_bdt decimal format."})
        else:
            financial_impact = record.financial_impact_bdt if record else Decimal("0.00")

        # 7. Occurred At Validation
        occurred_at_raw = data.get("occurred_at")
        if occurred_at_raw:
            try:
                occurred_at = datetime.datetime.fromisoformat(str(occurred_at_raw).replace("Z", "+00:00"))
                if timezone.is_naive(occurred_at):
                    occurred_at = timezone.make_aware(occurred_at)
                if occurred_at > timezone.now() + datetime.timedelta(minutes=5):
                    raise ValidationError({"occurred_at": "occurred_at cannot be in the future."})
            except ValueError:
                raise ValidationError({"occurred_at": "Invalid occurred_at datetime format. Expected ISO-8601."})
        else:
            occurred_at = record.occurred_at if record else timezone.now()

        # DGDA notification requirement for Critical or statutory recalls
        requires_dgda = severity == IncidentSeverity.CRITICAL or inc_type == PharmacyIncidentType.PRODUCT_RECALL

        if record:
            if branch:
                record.branch = branch
            record.title = title
            record.incident_type = inc_type
            record.severity = severity
            record.description = description
            record.occurred_at = occurred_at
            record.product_name = str(data.get("product_name") or record.product_name).strip()
            record.batch_number = str(data.get("batch_number") or record.batch_number).strip()
            record.affected_quantity = affected_qty
            record.financial_impact_bdt = financial_impact
            record.sale_id = str(data.get("sale_id") or record.sale_id).strip()
            record.prescription_id = str(data.get("prescription_id") or record.prescription_id).strip()
            record.investigation_notes = str(data.get("investigation_notes") or record.investigation_notes).strip()
            if "root_cause" in data:
                rc = str(data.get("root_cause") or "").strip().upper()
                if rc and rc not in IncidentRootCause.values:
                    raise ValidationError({"root_cause": f"Invalid root_cause '{rc}'."})
                record.root_cause = rc
            record.corrective_action_plan = str(data.get("corrective_action_plan") or record.corrective_action_plan).strip()
            record.preventive_measures = str(data.get("preventive_measures") or record.preventive_measures).strip()
            if "is_stock_quarantined" in data:
                record.is_stock_quarantined = bool(data["is_stock_quarantined"])
                record.quarantine_notes = str(data.get("quarantine_notes") or record.quarantine_notes).strip()
            record.requires_dgda_notification = requires_dgda
            record.save()
        else:
            new_id = f"inc-{uuid.uuid4().hex[:12]}"
            record = PharmacyIncidentRecord.objects.create(
                incident_id=new_id,
                tenant=tenant,
                branch=branch,
                incident_type=inc_type,
                severity=severity,
                status=IncidentStatus.REPORTED,
                title=title,
                description=description,
                occurred_at=occurred_at,
                reported_by=user,
                product_name=str(data.get("product_name") or "").strip(),
                batch_number=str(data.get("batch_number") or "").strip(),
                affected_quantity=affected_qty,
                financial_impact_bdt=financial_impact,
                sale_id=str(data.get("sale_id") or "").strip(),
                prescription_id=str(data.get("prescription_id") or "").strip(),
                investigation_notes=str(data.get("investigation_notes") or "").strip(),
                root_cause=str(data.get("root_cause") or "").strip().upper(),
                corrective_action_plan=str(data.get("corrective_action_plan") or "").strip(),
                preventive_measures=str(data.get("preventive_measures") or "").strip(),
                is_stock_quarantined=bool(data.get("is_stock_quarantined", False)),
                quarantine_notes=str(data.get("quarantine_notes") or "").strip(),
                requires_dgda_notification=requires_dgda,
            )

        return cls.serialize_incident(record, user=user)

    @classmethod
    @transaction.atomic
    def transition_status(
        cls,
        user: Any,
        incident_id: str,
        new_status: str,
        closure_notes: str = "",
        root_cause: str = "",
        corrective_action_plan: str = "",
    ) -> Dict[str, Any]:
        """
        Executes audited incident state transitions with strict closure authorization.
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials required.")

        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=True, allowed_roles=cls.ALLOWED_READ_ROLES
        )

        record = PharmacyIncidentRecord.objects.filter(
            incident_id=str(incident_id).strip(),
            tenant=tenant,
        ).first()

        if not record:
            raise NotFound({"incident_id": f"Pharmacy incident '{incident_id}' not found."})

        target_status = str(new_status or "").strip().upper()
        if target_status not in IncidentStatus.values:
            raise ValidationError({
                "new_status": f"Invalid incident status '{target_status}'. Must be one of: {list(IncidentStatus.values)}"
            })

        # Closure Approval Gate
        if target_status in (IncidentStatus.CLOSED, IncidentStatus.DISMISSED):
            if not record.can_close(user):
                raise PermissionDenied(
                    detail={
                        "error_code": "ERR_INCIDENT_CLOSURE_UNAUTHORIZED",
                        "message_en": "Only credentialed Pharmacists, Branch Managers, and Owners can approve incident closures.",
                        "message_bn": "শুধুমাত্র ফার্মাসিস্ট, ব্রাঞ্চ ম্যানেজার ও মালিক ইনসিডেন্ট ক্লোজার অনুমোদন করতে পারেন।",
                    }
                )

            # High or Critical severity requires mandatory CAPA and root cause before closure
            effective_rc = root_cause or record.root_cause
            effective_capa = corrective_action_plan or record.corrective_action_plan
            if record.severity in (IncidentSeverity.HIGH, IncidentSeverity.CRITICAL):
                if not effective_rc or not effective_capa:
                    raise ValidationError(
                        detail={
                            "error_code": "ERR_CAPA_REQUIRED_FOR_CLOSURE",
                            "message_en": "High and Critical incidents require documented Root Cause and CAPA prior to closure.",
                            "message_bn": "উচ্চ ও গুরুতর ইনসিডেন্টের ক্ষেত্রে ক্লোজারের পূর্বে মূল কারণ (Root Cause) ও সিএপিএ থাকা আবশ্যক।",
                        }
                    )

            record.status = target_status
            record.closed_by = user
            record.closed_at = timezone.now()
            record.closure_notes = str(closure_notes or record.closure_notes).strip()
            if root_cause:
                record.root_cause = root_cause
            if corrective_action_plan:
                record.corrective_action_plan = corrective_action_plan
            record.save()

        else:
            record.status = target_status
            if root_cause:
                record.root_cause = root_cause
            if corrective_action_plan:
                record.corrective_action_plan = corrective_action_plan
            record.save()

        return cls.serialize_incident(record, user=user)

    @classmethod
    @transaction.atomic
    def quarantine_stock(cls, user: Any, incident_id: str, notes: str = "") -> Dict[str, Any]:
        """
        Quarantines affected medication batch associated with incident.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=True, allowed_roles=cls.ALLOWED_INVESTIGATE_ROLES
        )

        record = PharmacyIncidentRecord.objects.filter(
            incident_id=str(incident_id).strip(),
            tenant=tenant,
        ).first()

        if not record:
            raise NotFound({"incident_id": f"Pharmacy incident '{incident_id}' not found."})

        record.is_stock_quarantined = True
        record.quarantine_notes = str(notes or "Quarantined via incident workflow").strip()
        record.save()

        return {
            "incident_id": record.incident_id,
            "is_stock_quarantined": True,
            "batch_number": record.batch_number,
            "affected_quantity": str(record.affected_quantity),
            "quarantine_notes": record.quarantine_notes,
        }

    @classmethod
    def get_branch_summary(cls, user: Any, branch_id: str) -> Dict[str, Any]:
        """
        Returns incident statistics and financial discrepancy metrics for a branch.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )

        branch = Branch.objects.filter(branch_id=str(branch_id).strip(), tenant=tenant).first()
        if not branch:
            raise NotFound({"branch_id": f"Branch '{branch_id}' not found."})

        qs = PharmacyIncidentRecord.objects.filter(tenant=tenant, branch=branch, is_archived=False)

        status_counts = {}
        severity_counts = {}
        type_counts = {}
        total_financial_loss = Decimal("0.00")
        critical_unresolved_count = 0

        for r in qs:
            status_counts[r.status] = status_counts.get(r.status, 0) + 1
            severity_counts[r.severity] = severity_counts.get(r.severity, 0) + 1
            type_counts[r.incident_type] = type_counts.get(r.incident_type, 0) + 1
            total_financial_loss += r.financial_impact_bdt
            if r.severity == IncidentSeverity.CRITICAL and r.status != IncidentStatus.CLOSED:
                critical_unresolved_count += 1

        return {
            "tenant_id": str(tenant.tenant_id),
            "branch_id": str(branch.branch_id),
            "branch_name": branch.name,
            "total_incidents": qs.count(),
            "status_distribution": status_counts,
            "severity_distribution": severity_counts,
            "type_distribution": type_counts,
            "critical_unresolved_count": critical_unresolved_count,
            "total_financial_impact_bdt": str(total_financial_loss),
        }

