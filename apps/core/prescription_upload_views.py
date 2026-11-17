"""
backend/apps/core/prescription_upload_views.py

Architecture Decision: ADR-0051 & ADR-0242 (P242).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

REST ViewSet for Secure Prescription Document/Image Uploads:
- Private object storage partition
- MIME & Antivirus inspection
- Expiring signed download URLs
- Pharmacist verification sign-off
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.models import PrescriptionAttachment
from apps.core.prescription_upload_service import PrescriptionUploadService
from apps.core.serializers import (
    PrescriptionAttachmentSerializer,
    PrescriptionAttachmentVerifySerializer,
)


class PrescriptionAttachmentViewSet(viewsets.ModelViewSet):
    """
    Manages secure prescription attachments, signed URLs, and clinical pharmacist sign-offs.
    """

    queryset = PrescriptionAttachment.objects.all()
    serializer_class = PrescriptionAttachmentSerializer
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    lookup_field = "attachment_id"

    def get_queryset(self):
        user = self.request.user
        qs = PrescriptionAttachment.objects.select_related("file_record", "uploaded_by", "verified_by")

        if not getattr(user, "is_superuser", False):
            tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
            if not tenant_id:
                return PrescriptionAttachment.objects.none()
            qs = qs.filter(tenant__tenant_id=tenant_id)
        else:
            tenant_id = self.request.query_params.get("tenant_id")
            if tenant_id:
                qs = qs.filter(tenant_id=tenant_id)

        rx_id = self.request.query_params.get("prescription_id")
        if rx_id:
            qs = qs.filter(prescription_id=rx_id)

        return qs

    def create(self, request: Request, *args, **kwargs) -> Response:
        """
        POST /api/v2/prescription-attachments/ — Upload a prescription image or document.
        Accepts multipart/form-data or direct binary payload.
        """
        uploaded_file = request.FILES.get("file")
        prescription_id = request.data.get("prescription_id")

        if not uploaded_file:
            # Check if raw body content was passed
            raw_content = request.body
            if not raw_content:
                return Response(
                    {"file": ["A file attachment is required (multipart 'file' or raw body)."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            content = raw_content
            filename = request.data.get("file_name", "prescription_document.jpg")
            declared_mime = request.content_type or "image/jpeg"
        else:
            content = uploaded_file.read()
            filename = uploaded_file.name
            declared_mime = uploaded_file.content_type or "image/jpeg"

        if not prescription_id:
            return Response(
                {"prescription_id": ["Target prescription_id is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        notes = request.data.get("notes", "")
        retention_days = request.data.get("retention_days")

        attachment = PrescriptionUploadService.upload_attachment(
            request.user,
            prescription_id=prescription_id,
            filename=filename,
            content=content,
            declared_mime=declared_mime,
            notes=notes,
            retention_days=retention_days,
        )

        serializer = self.get_serializer(attachment)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="download-url")
    def download_url(self, request: Request, pk: str = None, attachment_id: str = None) -> Response:
        """
        GET /api/v2/prescription-attachments/{id}/download-url/ — Generate expiring signed download URL.
        """
        target_id = attachment_id or pk
        ttl_seconds = int(request.query_params.get("ttl", 900))
        url_payload = PrescriptionUploadService.get_attachment_signed_url(
            request.user,
            attachment_id=target_id,
            ttl_seconds=ttl_seconds,
        )
        return Response(url_payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="verify")
    def verify_attachment(self, request: Request, pk: str = None, attachment_id: str = None) -> Response:
        """
        POST /api/v2/prescription-attachments/{id}/verify/ — Clinical pharmacist verification sign-off.
        """
        target_id = attachment_id or pk
        serializer = PrescriptionAttachmentVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        attachment = PrescriptionUploadService.verify_attachment(
            request.user,
            attachment_id=target_id,
            pharmacist_pcb_no=data["pharmacist_pcb_no"],
            notes=data.get("notes", ""),
        )
        out_serializer = self.get_serializer(attachment)
        return Response(out_serializer.data, status=status.HTTP_200_OK)
