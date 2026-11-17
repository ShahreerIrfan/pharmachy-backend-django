"""
backend/apps/core/compliance_views.py

Architecture Decision: ADR-0251 (P251) & ADR-0252 (P252).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

DRF ViewSets for Regulatory Compliance:
- /api/v2/compliance/drug-licenses/ (Branch Drug License tracking & review reminders)
- /api/v2/compliance/pcb-registrations/ (Pharmacy Council PCB registration tracking)
"""

from __future__ import annotations

from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.drug_license_service import DrugLicenseComplianceService
from apps.core.models import Branch, BranchDrugLicenseRecord, PharmacyPersonnelPcbRegistration
from apps.core.pcb_registration_service import PcbRegistrationService
from apps.core.pos_domain_service import PosDomainService


class BranchDrugLicenseViewSet(viewsets.ViewSet):
    """
    ViewSet for branch drug license records under Bangladesh DGDA regulations (ADR-0251).
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request: Request) -> Response:
        """GET /api/v2/compliance/drug-licenses/"""
        branch_id = request.query_params.get("branch_id")
        records = DrugLicenseComplianceService.list_branch_licenses(request.user, branch_id=branch_id)
        return Response({"count": len(records), "results": records}, status=status.HTTP_200_OK)

    def create(self, request: Request) -> Response:
        """POST /api/v2/compliance/drug-licenses/"""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            request.user, require_write=True, allowed_roles=DrugLicenseComplianceService.ALLOWED_MANAGE_ROLES
        )

        branch_id = request.data.get("branch_id") or getattr(request.user, "branch_id", None)
        if not branch_id:
            raise ValidationError({"branch_id": "branch_id is required."})

        branch = Branch.objects.filter(branch_id=str(branch_id).strip(), tenant=tenant).first()
        if not branch:
            raise NotFound({"branch_id": f"Branch '{branch_id}' not found."})

        result = DrugLicenseComplianceService.create_or_renew_license(request.user, branch, request.data)
        return Response(result, status=status.HTTP_201_CREATED)

    def retrieve(self, request: Request, pk: str = None) -> Response:
        """GET /api/v2/compliance/drug-licenses/{id}/"""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            request.user, require_write=False, allowed_roles=DrugLicenseComplianceService.ALLOWED_READ_ROLES
        )
        record = BranchDrugLicenseRecord.objects.filter(license_record_id=pk, tenant=tenant).first()
        if not record:
            raise NotFound(f"Drug license record '{pk}' not found.")
        return Response(DrugLicenseComplianceService.serialize_license(record), status=status.HTTP_200_OK)

    def partial_update(self, request: Request, pk: str = None) -> Response:
        """PATCH /api/v2/compliance/drug-licenses/{id}/"""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            request.user, require_write=True, allowed_roles=DrugLicenseComplianceService.ALLOWED_MANAGE_ROLES
        )
        record = BranchDrugLicenseRecord.objects.filter(license_record_id=pk, tenant=tenant).first()
        if not record:
            raise NotFound(f"Drug license record '{pk}' not found.")

        data = request.data
        if "review_notes" in data:
            record.review_notes = str(data["review_notes"]).strip()
        if "status" in data:
            record.status = str(data["status"]).strip().upper()
        if "document_file_uri" in data:
            record.document_file_uri = str(data["document_file_uri"]).strip()
        record.last_reviewed_by = request.user
        record.save()
        return Response(DrugLicenseComplianceService.serialize_license(record), status=status.HTTP_200_OK)

    def destroy(self, request: Request, pk: str = None) -> Response:
        """DELETE /api/v2/compliance/drug-licenses/{id}/"""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            request.user, require_write=True, allowed_roles=DrugLicenseComplianceService.ALLOWED_MANAGE_ROLES
        )
        record = BranchDrugLicenseRecord.objects.filter(license_record_id=pk, tenant=tenant).first()
        if not record:
            raise NotFound(f"Drug license record '{pk}' not found.")
        record.is_archived = True
        record.save(update_fields=["is_archived", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"], url_path="status-summary")
    def status_summary(self, request: Request) -> Response:
        """
        GET /api/v2/compliance/drug-licenses/status-summary/?branch_id=...
        Returns active drug license status and expiry warnings for a branch.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            request.user, require_write=False, allowed_roles=DrugLicenseComplianceService.ALLOWED_READ_ROLES
        )
        branch_id = request.query_params.get("branch_id") or getattr(request.user, "branch_id", None)
        if not branch_id:
            raise ValidationError({"branch_id": "branch_id query parameter is required."})

        branch = Branch.objects.filter(branch_id=str(branch_id).strip(), tenant=tenant).first()
        if not branch:
            raise NotFound({"branch_id": f"Branch '{branch_id}' not found."})

        summary = DrugLicenseComplianceService.evaluate_branch_drug_license(branch)
        return Response(summary, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="reminders")
    def reminders(self, request: Request) -> Response:
        """
        GET /api/v2/compliance/drug-licenses/reminders/
        Returns upcoming license expiration reminders.
        """
        reminders = DrugLicenseComplianceService.list_compliance_reminders(request.user)
        return Response({"count": len(reminders), "results": reminders}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# ADR-0252 (P252): Pharmacy Personnel PCB Registration Tracking ViewSet
# ---------------------------------------------------------------------------

from apps.core.pcb_registration_service import PcbRegistrationService


class PcbRegistrationViewSet(viewsets.ViewSet):
    """
    ViewSet for Pharmacy Council of Bangladesh (PCB) personnel registration tracking,
    validity verification, branch assignment, and confidential evidence access (ADR-0252).
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request: Request) -> Response:
        """GET /api/v2/compliance/pcb-registrations/"""
        branch_id = request.query_params.get("branch_id")
        grade = request.query_params.get("grade")
        status_filter = request.query_params.get("status")
        records = PcbRegistrationService.list_registrations(
            request.user, branch_id=branch_id, grade=grade, status_filter=status_filter
        )
        return Response({"count": len(records), "results": records}, status=status.HTTP_200_OK)

    def retrieve(self, request: Request, pk: str = None) -> Response:
        """GET /api/v2/compliance/pcb-registrations/{id}/"""
        record = PcbRegistrationService.get_registration(request.user, registration_id=pk)
        return Response(record, status=status.HTTP_200_OK)

    def create(self, request: Request) -> Response:
        """POST /api/v2/compliance/pcb-registrations/"""
        record = PcbRegistrationService.create_or_update_registration(request.user, request.data)
        return Response(record, status=status.HTTP_201_CREATED)

    def partial_update(self, request: Request, pk: str = None) -> Response:
        """PATCH /api/v2/compliance/pcb-registrations/{id}/"""
        record = PcbRegistrationService.create_or_update_registration(
            request.user, request.data, registration_id=pk
        )
        return Response(record, status=status.HTTP_200_OK)

    def destroy(self, request: Request, pk: str = None) -> Response:
        """DELETE /api/v2/compliance/pcb-registrations/{id}/"""
        PcbRegistrationService.archive_registration(request.user, registration_id=pk)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"], url_path="evidence")
    def evidence(self, request: Request, pk: str = None) -> Response:
        """
        GET /api/v2/compliance/pcb-registrations/{id}/evidence/
        Returns unredacted official registration certificate evidence.
        Strictly restricted to owners, branch managers, and the credential holder.
        """
        evidence_data = PcbRegistrationService.get_registration_evidence(request.user, registration_id=pk)
        return Response(evidence_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="branch-audit-summary")
    def branch_audit_summary(self, request: Request) -> Response:
        """
        GET /api/v2/compliance/pcb-registrations/branch-audit-summary/?branch_id=...
        Returns active, expiring soon, and grade distribution metrics for branch compliance audit.
        """
        branch_id = request.query_params.get("branch_id")
        if not branch_id:
            raise ValidationError({"branch_id": "branch_id query parameter is required."})
        summary = PcbRegistrationService.get_branch_audit_summary(request.user, branch_id=branch_id)
        return Response(summary, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="verify-credential")
    def verify_credential(self, request: Request) -> Response:
        """
        POST /api/v2/compliance/pcb-registrations/verify-credential/
        Statutory gate verifying active PCB credentials before prescription verification or clinical overrides.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            request.user, require_write=False, allowed_roles=PcbRegistrationService.ALLOWED_READ_ROLES
        )
        reg_no = request.data.get("registration_no")
        req_grade = request.data.get("required_grade")
        if not reg_no:
            raise ValidationError({"registration_no": "registration_no is required."})

        tenant_id = str(tenant.tenant_id if hasattr(tenant, "tenant_id") else tenant)
        result = PcbRegistrationService.verify_credential_gate(tenant_id, reg_no, required_grade=req_grade)
        return Response(result, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# ADR-0254 (P254): Statutory Inspection Evidence Repository ViewSet
# ---------------------------------------------------------------------------

from apps.core.inspection_evidence_service import InspectionEvidenceService


class InspectionEvidenceViewSet(viewsets.ViewSet):
    """
    ViewSet for statutory Inspection Evidence Repository under DGDA Good Pharmacy Practice (ADR-0254).
    Enforces protected clearance, confidential redactions, and sealed audit package exports.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request: Request) -> Response:
        """GET /api/v2/compliance/inspection-evidence/"""
        branch_id = request.query_params.get("branch_id")
        evidence_type = request.query_params.get("evidence_type")
        access_level = request.query_params.get("access_level")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        include_archived = request.query_params.get("include_archived", "false").lower() == "true"

        records = InspectionEvidenceService.list_evidence(
            request.user,
            branch_id=branch_id,
            evidence_type=evidence_type,
            access_level=access_level,
            start_date=start_date,
            end_date=end_date,
            include_archived=include_archived,
        )
        return Response({"count": len(records), "results": records}, status=status.HTTP_200_OK)

    def retrieve(self, request: Request, pk: str = None) -> Response:
        """GET /api/v2/compliance/inspection-evidence/{id}/"""
        record = InspectionEvidenceService.get_evidence(request.user, pk)
        return Response(record, status=status.HTTP_200_OK)

    def create(self, request: Request) -> Response:
        """POST /api/v2/compliance/inspection-evidence/"""
        record = InspectionEvidenceService.create_or_update_evidence(request.user, request.data)
        return Response(record, status=status.HTTP_201_CREATED)

    def partial_update(self, request: Request, pk: str = None) -> Response:
        """PATCH /api/v2/compliance/inspection-evidence/{id}/"""
        record = InspectionEvidenceService.create_or_update_evidence(request.user, request.data, evidence_id=pk)
        return Response(record, status=status.HTTP_200_OK)

    def destroy(self, request: Request, pk: str = None) -> Response:
        """DELETE /api/v2/compliance/inspection-evidence/{id}/"""
        reason = request.data.get("reason", "") if request.data else ""
        res = InspectionEvidenceService.archive_evidence(request.user, pk, reason=reason)
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="document")
    def document(self, request: Request, pk: str = None) -> Response:
        """
        GET /api/v2/compliance/inspection-evidence/{id}/document/
        Returns unredacted evidence document details. Requires authorized clearance.
        """
        doc = InspectionEvidenceService.get_evidence_document(request.user, pk)
        return Response(doc, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="export-audit-package")
    def export_audit_package(self, request: Request) -> Response:
        """
        POST /api/v2/compliance/inspection-evidence/export-audit-package/
        Compiles a sealed DGDA statutory audit evidence bundle.
        """
        branch_id = request.data.get("branch_id")
        start_date = request.data.get("start_date")
        end_date = request.data.get("end_date")
        evidence_types = request.data.get("evidence_types")

        if not branch_id or not start_date or not end_date:
            raise ValidationError("branch_id, start_date, and end_date are required.")

        package = InspectionEvidenceService.export_audit_package(
            request.user,
            branch_id=branch_id,
            start_date=start_date,
            end_date=end_date,
            evidence_types=evidence_types,
        )
        return Response(package, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# ADR-0255 (P255): Incident & Correction Workflow ViewSet
# ---------------------------------------------------------------------------

from apps.core.incident_management_service import IncidentManagementService


class PharmacyIncidentViewSet(viewsets.ViewSet):
    """
    ViewSet for clinical and operational incident reporting, CAPA workflows,
    quarantine tracking, and pharmacist closure approvals (ADR-0255).
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request: Request) -> Response:
        """GET /api/v2/compliance/incidents/"""
        branch_id = request.query_params.get("branch_id")
        incident_type = request.query_params.get("incident_type")
        status_filter = request.query_params.get("status")
        severity = request.query_params.get("severity")
        include_archived = request.query_params.get("include_archived", "false").lower() == "true"

        records = IncidentManagementService.list_incidents(
            request.user,
            branch_id=branch_id,
            incident_type=incident_type,
            status_filter=status_filter,
            severity=severity,
            include_archived=include_archived,
        )
        return Response({"count": len(records), "results": records}, status=status.HTTP_200_OK)

    def retrieve(self, request: Request, pk: str = None) -> Response:
        """GET /api/v2/compliance/incidents/{id}/"""
        record = IncidentManagementService.get_incident(request.user, pk)
        return Response(record, status=status.HTTP_200_OK)

    def create(self, request: Request) -> Response:
        """POST /api/v2/compliance/incidents/"""
        record = IncidentManagementService.create_or_update_incident(request.user, request.data)
        return Response(record, status=status.HTTP_201_CREATED)

    def partial_update(self, request: Request, pk: str = None) -> Response:
        """PATCH /api/v2/compliance/incidents/{id}/"""
        record = IncidentManagementService.create_or_update_incident(request.user, request.data, incident_id=pk)
        return Response(record, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="transition-status")
    def transition_status(self, request: Request, pk: str = None) -> Response:
        """
        POST /api/v2/compliance/incidents/{id}/transition-status/
        Transitions status with strict pharmacist/manager closure authorization.
        """
        new_status = request.data.get("new_status")
        closure_notes = request.data.get("closure_notes", "")
        root_cause = request.data.get("root_cause", "")
        capa = request.data.get("corrective_action_plan", "")

        if not new_status:
            raise ValidationError({"new_status": "new_status is required."})

        record = IncidentManagementService.transition_status(
            request.user,
            incident_id=pk,
            new_status=new_status,
            closure_notes=closure_notes,
            root_cause=root_cause,
            corrective_action_plan=capa,
        )
        return Response(record, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="quarantine-stock")
    def quarantine_stock(self, request: Request, pk: str = None) -> Response:
        """
        POST /api/v2/compliance/incidents/{id}/quarantine-stock/
        Quarantines affected batch associated with incident.
        """
        notes = request.data.get("quarantine_notes", "")
        res = IncidentManagementService.quarantine_stock(request.user, pk, notes=notes)
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="branch-summary")
    def branch_summary(self, request: Request) -> Response:
        """
        GET /api/v2/compliance/incidents/branch-summary/?branch_id=...
        Returns incident metrics, severity breakdown, and financial impact for a branch.
        """
        branch_id = request.query_params.get("branch_id") or getattr(request.user, "branch_id", None)
        if not branch_id:
            raise ValidationError({"branch_id": "branch_id query parameter is required."})
        summary = IncidentManagementService.get_branch_summary(request.user, branch_id)
        return Response(summary, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# ADR-0256 (P256): Batch Recall Notification & Propagation ViewSet
# ---------------------------------------------------------------------------

from apps.core.batch_recall_service import BatchRecallService


class BatchRecallViewSet(viewsets.ViewSet):
    """
    ViewSet for batch recall publication, automated stock quarantine,
    reservation eviction, dispensing gate checks, and historical sales audit (ADR-0256).
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request: Request) -> Response:
        """GET /api/v2/compliance/recalls/"""
        branch_id = request.query_params.get("branch_id")
        batch_number = request.query_params.get("batch_number")
        status_filter = request.query_params.get("status")
        recall_class = request.query_params.get("recall_class")

        records = BatchRecallService.list_recalls(
            request.user,
            branch_id=branch_id,
            batch_number=batch_number,
            status_filter=status_filter,
            recall_class=recall_class,
        )
        return Response({"count": len(records), "results": records}, status=status.HTTP_200_OK)

    def retrieve(self, request: Request, pk: str = None) -> Response:
        """GET /api/v2/compliance/recalls/{id}/"""
        record = BatchRecallService.get_recall(request.user, pk)
        return Response(record, status=status.HTTP_200_OK)

    def create(self, request: Request) -> Response:
        """POST /api/v2/compliance/recalls/"""
        record = BatchRecallService.publish_recall(request.user, request.data)
        return Response(record, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="historical-impact")
    def historical_impact(self, request: Request, pk: str = None) -> Response:
        """
        GET /api/v2/compliance/recalls/{id}/historical-impact/
        Extracts historical invoices and patient contacts for recall returns without rewriting history.
        """
        impact = BatchRecallService.get_historical_sales_impact(request.user, pk)
        return Response(impact, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="lift")
    def lift(self, request: Request, pk: str = None) -> Response:
        """
        POST /api/v2/compliance/recalls/{id}/lift/
        Lifts or rescinds a recall notice.
        """
        reason = request.data.get("reason", "")
        record = BatchRecallService.lift_recall(request.user, pk, reason=reason)
        return Response(record, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="check-dispensing-gate")
    def check_dispensing_gate(self, request: Request) -> Response:
        """
        POST /api/v2/compliance/recalls/check-dispensing-gate/
        Real-time POS scanning gate verifying if a scanned batch is under active recall.
        """
        batch_number = request.data.get("batch_number")
        branch_id = request.data.get("branch_id") or getattr(request.user, "branch_id", None)
        tenant_id = getattr(request.user, "tenant_id", None)

        gate_result = BatchRecallService.check_dispensing_gate(
            tenant_id=tenant_id,
            batch_number=batch_number,
            branch_id=branch_id,
        )
        return Response(gate_result, status=status.HTTP_200_OK)


from apps.core.temperature_safety_review_service import TemperatureSafetyReviewService


class TemperatureSafetyReviewViewSet(viewsets.ViewSet):
    """
    DRF ViewSet for Temperature Excursion Safety Reviews (ADR-0257).
    Enforces immutable pharmacist decisions for cold-chain medicine lots.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request: Request) -> Response:
        """GET /api/v2/compliance/temperature-reviews/"""
        branch_id = request.query_params.get("branch_id")
        batch_number = request.query_params.get("batch_number")
        decision_filter = request.query_params.get("review_decision")
        records = TemperatureSafetyReviewService.list_reviews(
            request.user,
            branch_id=branch_id,
            batch_number=batch_number,
            decision_filter=decision_filter,
        )
        return Response({"count": len(records), "results": records}, status=status.HTTP_200_OK)

    def retrieve(self, request: Request, pk: str = None) -> Response:
        """GET /api/v2/compliance/temperature-reviews/{id}/"""
        record = TemperatureSafetyReviewService.get_review(request.user, pk)
        return Response(record, status=status.HTTP_200_OK)

    def create(self, request: Request) -> Response:
        """POST /api/v2/compliance/temperature-reviews/"""
        record = TemperatureSafetyReviewService.record_review(request.user, request.data)
        return Response(record, status=status.HTTP_201_CREATED)

    def update(self, request: Request, pk: str = None) -> Response:
        """PUT /api/v2/compliance/temperature-reviews/{id}/"""
        raise ValidationError({
            "error_code": "ERR_REVIEW_DECISION_IMMUTABLE",
            "message_en": "Temperature excursion safety review decisions are immutable once signed and sealed.",
            "message_bn": "তাপমাত্রা পর্যালোচনা সিদ্ধান্ত চূড়ান্ত হওয়ার পর পরিবর্তন করা সম্পূর্ণ নিষিদ্ধ।",
        })

    def partial_update(self, request: Request, pk: str = None) -> Response:
        """PATCH /api/v2/compliance/temperature-reviews/{id}/"""
        raise ValidationError({
            "error_code": "ERR_REVIEW_DECISION_IMMUTABLE",
            "message_en": "Temperature excursion safety review decisions are immutable once signed and sealed.",
            "message_bn": "তাপমাত্রা পর্যালোচনা সিদ্ধান্ত চূড়ান্ত হওয়ার পর পরিবর্তন করা সম্পূর্ণ নিষিদ্ধ।",
        })

    def destroy(self, request: Request, pk: str = None) -> Response:
        """DELETE /api/v2/compliance/temperature-reviews/{id}/"""
        raise PermissionDenied({
            "error_code": "ERR_REVIEW_DELETE_FORBIDDEN",
            "message_en": "Safety review records cannot be deleted; statutory audit trail is permanent.",
            "message_bn": "সুরক্ষা পর্যালোচনার রেকর্ড মুছে ফেলা সম্পূর্ণ নিষিদ্ধ; অডিট ট্রেইল স্থায়ী।",
        })

    @action(detail=False, methods=["get", "post"], url_path="check-batch-safety")
    def check_batch_safety(self, request: Request) -> Response:
        """
        GET/POST /api/v2/compliance/temperature-reviews/check-batch-safety/
        Preflight dispensing gate for POS checkout and Rx verification.
        """
        batch_number = request.query_params.get("batch_number") or request.data.get("batch_number")
        branch_id = request.query_params.get("branch_id") or request.data.get("branch_id") or getattr(request.user, "branch_id", None)
        tenant_id = getattr(request.user, "tenant_id", None)

        safety_res = TemperatureSafetyReviewService.check_batch_temperature_safety(
            tenant_id=tenant_id,
            batch_number=batch_number,
            branch_id=branch_id,
        )
        return Response(safety_res, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="verify-audit-hash")
    def verify_audit_hash(self, request: Request, pk: str = None) -> Response:
        """
        GET /api/v2/compliance/temperature-reviews/{id}/verify-audit-hash/
        Cryptographic verification of immutable decision record.
        """
        review = TemperatureSafetyReviewService.get_review(request.user, pk)
        return Response({
            "review_id": review["review_id"],
            "sha256_audit_hash": review["sha256_audit_hash"],
            "is_tamper_free": review["is_tamper_free"],
            "is_immutable": review["is_immutable"],
        }, status=status.HTTP_200_OK)


from apps.core.compliance_audit_export_service import ComplianceAuditExportService


class ComplianceAuditExportViewSet(viewsets.ViewSet):
    """
    DRF ViewSet for Compliance Audit Export Manifests (ADR-0258).
    Manages sealed, privacy-redacted regulatory audit packages for DGDA/PCB inspections.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request: Request) -> Response:
        """GET /api/v2/compliance/audit-exports/"""
        branch_id = request.query_params.get("branch_id")
        scope_filter = request.query_params.get("export_scope")
        records = ComplianceAuditExportService.list_exports(
            request.user, branch_id=branch_id, scope_filter=scope_filter
        )
        return Response({"count": len(records), "results": records}, status=status.HTTP_200_OK)

    def retrieve(self, request: Request, pk: str = None) -> Response:
        """GET /api/v2/compliance/audit-exports/{id}/"""
        record = ComplianceAuditExportService.get_export(request.user, pk)
        return Response(record, status=status.HTTP_200_OK)

    def create(self, request: Request) -> Response:
        """POST /api/v2/compliance/audit-exports/"""
        manifest = ComplianceAuditExportService.generate_audit_export(request.user, request.data)
        return Response(manifest, status=status.HTTP_201_CREATED)

    def update(self, request: Request, pk: str = None) -> Response:
        """PUT /api/v2/compliance/audit-exports/{id}/"""
        raise ValidationError({
            "error_code": "ERR_AUDIT_MANIFEST_IMMUTABLE",
            "message_en": "Compliance audit export manifests are sealed and immutable.",
            "message_bn": "কমপ্লায়েন্স অডিট এক্সপোর্ট ম্যানিফেস্ট সিল করা এবং অপরিবর্তনীয়।",
        })

    def partial_update(self, request: Request, pk: str = None) -> Response:
        """PATCH /api/v2/compliance/audit-exports/{id}/"""
        raise ValidationError({
            "error_code": "ERR_AUDIT_MANIFEST_IMMUTABLE",
            "message_en": "Compliance audit export manifests are sealed and immutable.",
            "message_bn": "কমপ্লায়েন্স অডিট এক্সপোর্ট ম্যানিফেস্ট সিল করা এবং অপরিবর্তনীয়।",
        })

    def destroy(self, request: Request, pk: str = None) -> Response:
        """DELETE /api/v2/compliance/audit-exports/{id}/"""
        raise PermissionDenied({
            "error_code": "ERR_AUDIT_MANIFEST_DELETE_FORBIDDEN",
            "message_en": "Compliance audit manifests cannot be deleted; permanent statutory record.",
            "message_bn": "কমপ্লায়েন্স অডিট ম্যানিফেস্ট মুছে ফেলা নিষিদ্ধ; এটি স্থায়ী রেকর্ড।",
        })

    @action(detail=True, methods=["get"], url_path="verify-manifest")
    def verify_manifest(self, request: Request, pk: str = None) -> Response:
        """
        GET /api/v2/compliance/audit-exports/{id}/verify-manifest/
        Cryptographic integrity check for sealed audit manifest.
        """
        verification = ComplianceAuditExportService.verify_manifest_integrity(request.user, pk)
        return Response(verification, status=status.HTTP_200_OK)


from apps.core.regulatory_reference_service import RegulatoryReferenceService


class RegulatoryReferenceViewSet(viewsets.ViewSet):
    """
    DRF ViewSet for Regulatory Reference Register (ADR-0259).
    Maintains reviewed statutory and clinical guidelines with next-review scheduling.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request: Request) -> Response:
        """GET /api/v2/compliance/regulatory-references/"""
        jurisdiction = request.query_params.get("jurisdiction")
        status_filter = request.query_params.get("status")
        overdue_only = request.query_params.get("overdue_only", "false").lower() == "true"
        records = RegulatoryReferenceService.list_references(
            request.user,
            jurisdiction=jurisdiction,
            status_filter=status_filter,
            overdue_only=overdue_only,
        )
        return Response({"count": len(records), "results": records}, status=status.HTTP_200_OK)

    def retrieve(self, request: Request, pk: str = None) -> Response:
        """GET /api/v2/compliance/regulatory-references/{id}/"""
        record = RegulatoryReferenceService.get_reference(request.user, pk)
        return Response(record, status=status.HTTP_200_OK)

    def create(self, request: Request) -> Response:
        """POST /api/v2/compliance/regulatory-references/"""
        record = RegulatoryReferenceService.create_reference(request.user, request.data)
        return Response(record, status=status.HTTP_201_CREATED)

    def update(self, request: Request, pk: str = None) -> Response:
        """PUT /api/v2/compliance/regulatory-references/{id}/"""
        record = RegulatoryReferenceService.update_reference(request.user, pk, request.data)
        return Response(record, status=status.HTTP_200_OK)

    def partial_update(self, request: Request, pk: str = None) -> Response:
        """PATCH /api/v2/compliance/regulatory-references/{id}/"""
        record = RegulatoryReferenceService.update_reference(request.user, pk, request.data)
        return Response(record, status=status.HTTP_200_OK)

    def destroy(self, request: Request, pk: str = None) -> Response:
        """DELETE /api/v2/compliance/regulatory-references/{id}/ -> archives safely"""
        record = RegulatoryReferenceService.archive_reference(request.user, pk, reason="User deletion request")
        return Response(record, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="overdue")
    def overdue(self, request: Request) -> Response:
        """GET /api/v2/compliance/regulatory-references/overdue/"""
        records = RegulatoryReferenceService.list_references(request.user, overdue_only=True)
        return Response({"count": len(records), "results": records}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="verify-hash")
    def verify_hash(self, request: Request, pk: str = None) -> Response:
        """GET /api/v2/compliance/regulatory-references/{id}/verify-hash/"""
        record = RegulatoryReferenceService.get_reference(request.user, pk)
        return Response({
            "reference_id": record["reference_id"],
            "sha256_document_hash": record["sha256_document_hash"],
            "is_tamper_free": record["is_tamper_free"],
            "is_review_overdue": record["is_review_overdue"],
        }, status=status.HTTP_200_OK)
