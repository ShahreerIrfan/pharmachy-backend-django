"""
backend/apps/core/pharmacist_queue_views.py

Architecture Decision: ADR-0243 (P243).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

REST ViewSet for Pharmacist Verification Queue:
- Pharmacist-only access control
- Priority sorting for antibiotics and urgent medications
- Actions: verify, reject, request-clarification, resolve-clarification, audit-trail
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.models import PrescriptionRecord
from apps.core.pharmacist_queue_service import PharmacistQueueService
from apps.core.serializers import (
    PharmacistClarificationInputSerializer,
    PharmacistRejectInputSerializer,
    PharmacistResolveClarificationInputSerializer,
    PharmacistVerifyInputSerializer,
    PrescriptionRecordSerializer,
    PrescriptionVerificationAuditSerializer,
)


class PharmacistReviewQueueViewSet(viewsets.ViewSet):
    """
    Dedicated clinical pharmacist review queue.
    Restricted to certified pharmacists with PCB qualifications.
    """

    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "prescription_id"

    def list(self, request: Request) -> Response:
        """
        GET /api/v2/prescriptions/review-queue/ — List prescriptions awaiting pharmacist review.
        """
        branch_id = request.query_params.get("branch_id")
        status_filter = request.query_params.get("status")

        queue = PharmacistQueueService.get_review_queue(
            request.user,
            branch_id=branch_id,
            status_filter=status_filter,
        )
        serializer = PrescriptionRecordSerializer(queue, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def retrieve(self, request: Request, prescription_id: str = None) -> Response:
        """
        GET /api/v2/prescriptions/review-queue/{id}/ — Detail of queue prescription.
        """
        PharmacistQueueService.assert_pharmacist_role(request.user, "view")
        tenant_id = getattr(request.user, "tenant_id", None)
        rx = PrescriptionRecord.objects.filter(
            prescription_id=prescription_id,
            tenant__tenant_id=tenant_id,
        ).first()

        if not rx:
            return Response(
                {"detail": "Prescription not found in review queue."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = PrescriptionRecordSerializer(rx)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="verify")
    def verify(self, request: Request, prescription_id: str = None) -> Response:
        """
        POST /api/v2/prescriptions/review-queue/{id}/verify/ — Clinical approval.
        """
        serializer = PharmacistVerifyInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        rx, audit = PharmacistQueueService.verify_prescription(
            request.user,
            prescription_id=prescription_id,
            pharmacist_pcb_no=data["pharmacist_pcb_no"],
            notes=data.get("notes", ""),
        )

        return Response(
            {
                "prescription": PrescriptionRecordSerializer(rx).data,
                "audit": PrescriptionVerificationAuditSerializer(audit).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request: Request, prescription_id: str = None) -> Response:
        """
        POST /api/v2/prescriptions/review-queue/{id}/reject/ — Clinical rejection with reason.
        """
        serializer = PharmacistRejectInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        rx, audit = PharmacistQueueService.reject_prescription(
            request.user,
            prescription_id=prescription_id,
            pharmacist_pcb_no=data["pharmacist_pcb_no"],
            rejection_reason=data["rejection_reason"],
        )

        return Response(
            {
                "prescription": PrescriptionRecordSerializer(rx).data,
                "audit": PrescriptionVerificationAuditSerializer(audit).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="request-clarification")
    def request_clarification(self, request: Request, prescription_id: str = None) -> Response:
        """
        POST /api/v2/prescriptions/review-queue/{id}/request-clarification/ — Flag prescription for inquiry.
        """
        serializer = PharmacistClarificationInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        rx, audit = PharmacistQueueService.request_clarification(
            request.user,
            prescription_id=prescription_id,
            pharmacist_pcb_no=data["pharmacist_pcb_no"],
            clarification_query=data["clarification_query"],
        )

        return Response(
            {
                "prescription": PrescriptionRecordSerializer(rx).data,
                "audit": PrescriptionVerificationAuditSerializer(audit).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="resolve-clarification")
    def resolve_clarification(self, request: Request, prescription_id: str = None) -> Response:
        """
        POST /api/v2/prescriptions/review-queue/{id}/resolve-clarification/ — Record prescriber response.
        """
        serializer = PharmacistResolveClarificationInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        rx, audit = PharmacistQueueService.resolve_clarification(
            request.user,
            prescription_id=prescription_id,
            clarification_response=data["clarification_response"],
        )

        return Response(
            {
                "prescription": PrescriptionRecordSerializer(rx).data,
                "audit": PrescriptionVerificationAuditSerializer(audit).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="audit-trail")
    def audit_trail(self, request: Request, prescription_id: str = None) -> Response:
        """
        GET /api/v2/prescriptions/review-queue/{id}/audit-trail/ — View decision history.
        """
        audits = PharmacistQueueService.get_prescription_audit_trail(
            request.user,
            prescription_id=prescription_id,
        )
        serializer = PrescriptionVerificationAuditSerializer(audits, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

