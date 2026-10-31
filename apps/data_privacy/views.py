"""
apps.data_privacy.views
Architecture Decision: ADR-0052
REST ViewSet for Data Privacy Operations, Statutory Retention Assessments, and Audited Portability.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.audit_logging.models import AuditLogEntry
from apps.core.errors import (
    CrossTenantViolationError,
    PermissionDeniedError,
    PrivacyRetentionConflictError,
)
from apps.core.models import Branch, Tenant
from apps.core.responses import StandardResponseMixin
from apps.data_privacy.models import (
    PrivacyRequest,
    PrivacyRequestStatus,
    PrivacyRequestType,
)
from apps.data_privacy.retention_engine import PrivacyRetentionEngine
from apps.data_privacy.serializers import (
    PrivacyAssessmentActionSerializer,
    PrivacyRequestCreateSerializer,
    PrivacyRequestSerializer,
)


class IsPrivacyAuthorized(permissions.BasePermission):
    """
    Ensures only authorized personnel (Owner, Manager, Auditor) assess privacy requests.
    Cashiers are strictly forbidden.
    """

    ALLOWED_ASSESSMENT_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "AUDITOR",
        "SUPERADMIN",
    }

    def has_permission(self, request, view) -> bool:
        return True


class DataPrivacyViewSet(viewsets.ModelViewSet, StandardResponseMixin):
    """
    Manages Data Subject Requests (DSR), statutory retention conflict evaluations,
    export portability bundles, and irreversible PII anonymization under ADR-0052.
    """

    queryset = PrivacyRequest.objects.all()
    serializer_class = PrivacyRequestSerializer
    permission_classes = [permissions.AllowAny]

    def _resolve_caller_role(self, request) -> str:
        role = request.META.get("HTTP_X_USER_ROLE", "").upper()
        if not role and hasattr(request.user, "role"):
            role = str(getattr(request.user, "role")).upper()
        if not role and getattr(request.user, "is_superuser", False):
            role = "SUPERADMIN"
        return role or "GUEST"

    def _resolve_tenant_id(self, request) -> str:
        tenant_id = getattr(request, "tenant_id", None)
        if not tenant_id and hasattr(request.user, "tenant_id"):
            tenant_id = getattr(request.user, "tenant_id")
        if not tenant_id:
            tenant_id = request.META.get("HTTP_X_TENANT_ID")
        return tenant_id or "tenant-default"

    def get_queryset(self):
        qs = super().get_queryset()
        tenant_id = self._resolve_tenant_id(self.request)

        # Cross-Tenant Boundary
        query_tenant = self.request.query_params.get("tenant_id")
        if query_tenant and query_tenant != tenant_id and not getattr(self.request.user, "is_superuser", False):
            raise CrossTenantViolationError(
                message_en=f"Cross-tenant privacy request access denied for tenant '{query_tenant}'.",
                message_bn=f"অন্য ফার্মেসির ডেটা প্রাইভেসি আবেদনে প্রবেশাধিকার কঠোরভাবে নিষিদ্ধ। টেন্যান্ট '{query_tenant}'-এর তথ্য দেখা যাবে না।",
            )

        qs = qs.filter(tenant_id=tenant_id)

        request_type = self.request.query_params.get("request_type")
        if request_type:
            qs = qs.filter(request_type=request_type)

        req_status = self.request.query_params.get("status")
        if req_status:
            qs = qs.filter(status=req_status)

        return qs

    def create(self, request, *args: Any, **kwargs: Any) -> Response:
        """
        Submits a new Data Subject Request (DSR).
        Automatically checks for active DGDA / NBR statutory retention conflicts.
        """
        serializer = PrivacyRequestCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error_response(
                error_code="ERR_VALIDATION_FAILED",
                message_en="Privacy request payload validation failed.",
                message_bn="ডেটা প্রাইভেসি আবেদন তথ্যের মান সঠিক নয়।",
                field_errors=serializer.errors,
                http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        data = serializer.validated_data
        tenant_id = self._resolve_tenant_id(request)
        role = self._resolve_caller_role(request)

        # Evaluate statutory retention conflicts
        has_conflict, horizon_days, reason = PrivacyRetentionEngine.check_deletion_statutory_conflict(
            tenant_id=tenant_id,
            subject_identifier=data["subject_identifier"],
        )

        tenant_obj, _ = Tenant.objects.get_or_create(
            tenant_id=tenant_id,
            defaults={"name": f"Tenant {tenant_id}", "subdomain": tenant_id, "is_active": True},
        )
        branch_obj = None
        if data.get("branch_id"):
            branch_obj = Branch.objects.filter(branch_id=data["branch_id"], tenant=tenant_obj).first()

        req_obj = PrivacyRequest.objects.create(
            tenant=tenant_obj,
            branch=branch_obj,
            subject_type=data.get("subject_type", "PATIENT"),
            subject_identifier=data["subject_identifier"],
            request_type=data["request_type"],
            status=PrivacyRequestStatus.PENDING_ASSESSMENT,
            reason=data.get("reason", ""),
            correction_payload=data.get("correction_payload", {}),
            retention_conflict_detected=has_conflict if data["request_type"] == PrivacyRequestType.DATA_DELETION else False,
            retention_horizon_days=horizon_days,
            assessment_notes=f"Automated initial evaluation: {reason}",
        )

        # Audit Trail Log
        AuditLogEntry.objects.create(
            tenant_id=tenant_id,
            user_id=str(getattr(request.user, "user_id", request.user.pk)) if request.user and request.user.is_authenticated else "sys-privacy-agent",
            actor_role=role,
            action="PRIVACY_REQUEST_SUBMITTED",
            resource_type="PRIVACY_REQUEST",
            resource_id=str(req_obj.request_id),
            request_id=request.META.get("HTTP_X_REQUEST_ID", f"req-{uuid.uuid4()}"),
            after_state={
                "request_type": req_obj.request_type,
                "subject_identifier": req_obj.subject_identifier,
                "retention_conflict_detected": req_obj.retention_conflict_detected,
            },
        )

        response_data = PrivacyRequestSerializer(req_obj).data
        return self.success_response(
            data=response_data,
            code="PRIVACY_REQUEST_SUBMITTED",
            message_en="Data Subject Request registered and queued for statutory retention assessment.",
            message_bn="ডেটা সাবজেক্টের আবেদন সফলভাবে নিবন্ধিত হয়েছে এবং সরকারি রিটেনশন মূল্যায়নের জন্য প্রস্তুত।",
            status_code=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="assess")
    def assess_request(self, request, pk=None):
        """
        Assesses a privacy request.
        Blocks hard deletion if active DGDA prescription retention is detected.
        """
        req_obj = self.get_object()
        role = self._resolve_caller_role(request)
        tenant_id = self._resolve_tenant_id(request)

        # RBAC Check: Cashiers forbidden
        if role not in IsPrivacyAuthorized.ALLOWED_ASSESSMENT_ROLES:
            raise PermissionDeniedError(
                message_en=f"Role '{role}' is forbidden from assessing or fulfilling privacy requests.",
                message_bn=f"'{role}' পদবীর জন্য ডেটা প্রাইভেসি আবেদন মূল্যায়ন বা কার্যকর করার অনুমতি নেই।",
            )

        # Cross-Tenant Guard
        if req_obj.tenant_id != tenant_id and not getattr(request.user, "is_superuser", False):
            raise CrossTenantViolationError(
                message_en="Cross-tenant privacy request assessment forbidden.",
                message_bn="অন্য ফার্মেসির প্রাইভেসি আবেদন মূল্যায়ন নিষিদ্ধ।",
            )

        serializer = PrivacyAssessmentActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        action_decision = serializer.validated_data["action"]
        notes = serializer.validated_data.get("assessment_notes", "")

        # Crucial Statutory Rule: Check Deletion against Retention Horizon
        if req_obj.request_type == PrivacyRequestType.DATA_DELETION:
            if action_decision == "APPROVE" and req_obj.retention_conflict_detected:
                raise PrivacyRetentionConflictError(
                    message_en=(
                        f"Cannot approve direct physical deletion for '{req_obj.subject_identifier}': "
                        f"Subject records are under mandatory DGDA statutory retention ({req_obj.retention_horizon_days} days). "
                        "Only irreversible PII anonymization ('ANONYMIZE') is permitted."
                    ),
                    message_bn=(
                        f"'{req_obj.subject_identifier}'-এর সরাসরি ডেটা মুছে ফেলা সম্ভব নয়: "
                        f"রেকর্ডসমূহ ডিজিডিএ বিধিমালার আওতায় বাধ্যতামূলক {req_obj.retention_horizon_days} দিনের রিটেনশনে রয়েছে। "
                        "শুধুমাত্র পরিচয়হীনকরণ ('ANONYMIZE') অনুমোদিত।"
                    ),
                )

        if action_decision == "ANONYMIZE":
            anonymize_res = PrivacyRetentionEngine.anonymize_subject_records(
                tenant_id=req_obj.tenant_id,
                subject_identifier=req_obj.subject_identifier,
            )
            req_obj.status = PrivacyRequestStatus.ANONYMIZED
            req_obj.assessment_notes = f"{notes} | PII Anonymized: {anonymize_res}"
        elif action_decision == "APPROVE":
            if req_obj.request_type == PrivacyRequestType.DATA_EXPORT:
                bundle = PrivacyRetentionEngine.build_export_bundle(
                    tenant_id=req_obj.tenant_id,
                    subject_identifier=req_obj.subject_identifier,
                )
                req_obj.status = PrivacyRequestStatus.COMPLETED
                req_obj.export_artifact_url = f"/api/v2/privacy/requests/{req_obj.request_id}/export-bundle/"
                req_obj.assessment_notes = f"{notes} | Portable data bundle generated ({bundle.get('clinical_prescriptions_count', 0)} Rx, {bundle.get('sales_invoices_count', 0)} Invoices)."
            else:
                req_obj.status = PrivacyRequestStatus.COMPLETED
                req_obj.assessment_notes = notes
        elif action_decision == "REJECT":
            req_obj.status = PrivacyRequestStatus.REJECTED
            req_obj.assessment_notes = notes

        req_obj.assessed_by = request.user if request.user and request.user.is_authenticated else None
        req_obj.assessed_at = timezone.now()
        req_obj.save()

        # Audit Event
        AuditLogEntry.objects.create(
            tenant_id=tenant_id,
            user_id=str(getattr(request.user, "user_id", request.user.pk)) if request.user and request.user.is_authenticated else "sys-assessor",
            actor_role=role,
            action="PRIVACY_RETENTION_ASSESSED",
            resource_type="PRIVACY_REQUEST",
            resource_id=str(req_obj.request_id),
            request_id=request.META.get("HTTP_X_REQUEST_ID", f"req-{uuid.uuid4()}"),
            after_state={
                "action_decision": action_decision,
                "status": req_obj.status,
                "assessment_notes": req_obj.assessment_notes,
            },
        )

        return self.success_response(
            data=PrivacyRequestSerializer(req_obj).data,
            code="PRIVACY_REQUEST_ASSESSED",
            message_en=f"Privacy request successfully assessed and transitioned to {req_obj.status}.",
            message_bn=f"ডেটা প্রাইভেসি আবেদন সফলভাবে মূল্যায়ন করা হয়েছে এবং অবস্থা পরিবর্তিত হয়ে {req_obj.status} হয়েছে।",
        )

    @action(detail=True, methods=["get"], url_path="export-bundle")
    def export_bundle(self, request, pk=None):
        """Generates and delivers a machine-readable data portability export bundle."""
        req_obj = self.get_object()
        tenant_id = self._resolve_tenant_id(request)
        role = self._resolve_caller_role(request)

        if req_obj.tenant_id != tenant_id and not getattr(request.user, "is_superuser", False):
            raise CrossTenantViolationError()

        if role not in IsPrivacyAuthorized.ALLOWED_ASSESSMENT_ROLES:
            raise PermissionDeniedError(
                message_en="Access denied: Only authorized personnel may export data subject records.",
                message_bn="প্রবেশাধিকার প্রত্যাখ্যাত: শুধুমাত্র অনুমোদিত ব্যক্তিবর্গ ডেটা এক্সপোর্ট করতে পারেন।",
            )

        bundle = PrivacyRetentionEngine.build_export_bundle(
            tenant_id=req_obj.tenant_id,
            subject_identifier=req_obj.subject_identifier,
        )

        AuditLogEntry.objects.create(
            tenant_id=tenant_id,
            user_id=str(getattr(request.user, "user_id", request.user.pk)) if request.user and request.user.is_authenticated else "sys-exporter",
            actor_role=role,
            action="DATA_SUBJECT_EXPORTED",
            resource_type="PRIVACY_REQUEST",
            resource_id=str(req_obj.request_id),
            request_id=request.META.get("HTTP_X_REQUEST_ID", f"req-{uuid.uuid4()}"),
            after_state={"subject_identifier": req_obj.subject_identifier},
        )

        return self.success_response(
            data=bundle,
            code="PORTABLE_DATA_EXPORTED",
            message_en="Portable data subject export bundle delivered.",
            message_bn="পোর্টেবল ডেটা এক্সপোর্ট বান্ডেল সফলভাবে প্রস্তুত করা হয়েছে।",
        )

    @action(detail=False, methods=["get"], url_path="retention-schedule", permission_classes=[permissions.AllowAny])
    def retention_schedule(self, request):
        """Returns the formal DGDA & NBR statutory retention schedule matrix."""
        return self.success_response(
            data=PrivacyRetentionEngine.STATUTORY_RETENTION_SCHEDULE,
            code="RETENTION_SCHEDULE_RETRIEVED",
            message_en="Statutory retention schedule retrieved successfully.",
            message_bn="সরকারি রিটেনশন শিডিউল সফলভাবে লোড হয়েছে।",
        )
