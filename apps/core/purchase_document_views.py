"""
Purchase Document Generation REST API Views (ADR-0198 / P198)
Exposes endpoints for generating, versioning, inspecting, and downloading
A4 PDF and print documents for POs, GRNs, returns, and statements.
"""

from typing import Any
from django.http import HttpResponse
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

from apps.core.models import PurchaseDocument, PurchaseDocumentFormat
from apps.core.purchase_document_service import PurchaseDocumentService


class PurchaseDocumentViewSet(viewsets.ViewSet):
    """
    REST API ViewSet managing versioned procurement document generation and PDF delivery.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_tenant_id(self, request: Request) -> str:
        tenant_id = getattr(request, "tenant_id", None)
        if not tenant_id and hasattr(request.user, "tenant_id"):
            tenant_id = str(request.user.tenant_id)
        if not tenant_id:
            raise ValidationError(
                {
                    "error_code": "ERR_TENANT_REQUIRED",
                    "detail": "Tenant identification is required.",
                }
            )
        return str(tenant_id)

    def _serialize_document(self, doc: PurchaseDocument, include_payload: bool = False) -> dict[str, Any]:
        data = {
            "document_id": doc.document_id,
            "document_type": doc.document_type,
            "document_number": doc.document_number,
            "version": doc.version,
            "is_latest": doc.is_latest,
            "source_id": doc.source_id,
            "source_reference": doc.source_reference,
            "supplier_id": doc.supplier_id,
            "supplier_name": doc.supplier_name,
            "document_format": doc.document_format,
            "title": doc.title,
            "title_bn": doc.title_bn,
            "total_amount": str(doc.total_amount),
            "currency": doc.currency,
            "immutable_verification_hash": doc.immutable_verification_hash,
            "pdf_size_bytes": doc.pdf_size_bytes,
            "pdf_file_path": doc.pdf_file_path,
            "status": doc.status,
            "notes": doc.notes,
            "generated_by_id": str(doc.generated_by_id) if doc.generated_by_id else None,
            "generated_at": doc.generated_at.isoformat() if doc.generated_at else None,
        }
        if include_payload:
            data["document_payload"] = doc.document_payload
        return data

    @action(detail=False, methods=["post"], url_path="generate")
    def generate(self, request: Request) -> Response:
        """
        Generates or re-generates a versioned purchase document.
        Body: document_type (required), source_id (required), notes (optional), format (optional)
        """
        tenant_id = self._get_tenant_id(request)
        doc_type = request.data.get("document_type")
        source_id = request.data.get("source_id")
        if not doc_type:
            raise ValidationError(
                {
                    "error_code": "ERR_DOC_TYPE_REQUIRED",
                    "detail": "Field 'document_type' is required.",
                }
            )
        if not source_id and not request.data.get("supplier_id"):
            raise ValidationError(
                {
                    "error_code": "ERR_SOURCE_ID_REQUIRED",
                    "detail": "Field 'source_id' or 'supplier_id' is required.",
                }
            )

        doc = PurchaseDocumentService.generate_document(
            tenant_id=tenant_id,
            actor_user=request.user,
            document_type=doc_type,
            source_id=source_id or request.data.get("supplier_id"),
            branch_id=request.data.get("branch_id"),
            supplier_id=request.data.get("supplier_id"),
            notes=request.data.get("notes", ""),
            document_format=request.data.get("format", PurchaseDocumentFormat.A4_PDF),
            start_date=request.data.get("start_date"),
            end_date=request.data.get("end_date"),
        )
        return Response(self._serialize_document(doc, include_payload=True), status=status.HTTP_201_CREATED)

    def list(self, request: Request) -> Response:
        """
        Lists generated documents with filtering by type, source, and supplier.
        """
        tenant_id = self._get_tenant_id(request)
        doc_type = request.query_params.get("document_type")
        source_id = request.query_params.get("source_id")
        supplier_id = request.query_params.get("supplier_id")
        only_latest = request.query_params.get("only_latest", "true").lower() in ("true", "1")

        docs = PurchaseDocumentService.list_documents(
            tenant_id=tenant_id,
            actor_user=request.user,
            document_type=doc_type,
            source_id=source_id,
            supplier_id=supplier_id,
            only_latest=only_latest,
        )
        return Response([self._serialize_document(d) for d in docs], status=status.HTTP_200_OK)

    def retrieve(self, request: Request, pk: str = None) -> Response:
        """
        Retrieves a document snapshot by document_id.
        """
        tenant_id = self._get_tenant_id(request)
        doc = PurchaseDocumentService.get_document(
            tenant_id=tenant_id,
            actor_user=request.user,
            document_id=pk,
        )
        return Response(self._serialize_document(doc, include_payload=True), status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="pdf")
    def download_pdf(self, request: Request, pk: str = None) -> HttpResponse:
        """
        Renders and downloads the document as an A4 PDF binary stream.
        """
        tenant_id = self._get_tenant_id(request)
        doc = PurchaseDocumentService.get_document(
            tenant_id=tenant_id,
            actor_user=request.user,
            document_id=pk,
        )

        pdf_bytes = PurchaseDocumentService.render_pdf_bytes(
            doc_type=doc.document_type,
            payload=doc.document_payload,
            tenant=doc.tenant,
        )

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{doc.document_number}.pdf"'
        response["Content-Length"] = len(pdf_bytes)
        return response

    @action(detail=False, methods=["get"], url_path="versions")
    def versions(self, request: Request) -> Response:
        """
        Lists all historical versions of a document for an underlying source entity.
        Query params: document_type, source_id
        """
        tenant_id = self._get_tenant_id(request)
        doc_type = request.query_params.get("document_type")
        source_id = request.query_params.get("source_id")

        if not doc_type or not source_id:
            raise ValidationError(
                {
                    "error_code": "ERR_PARAMS_REQUIRED",
                    "detail": "Both 'document_type' and 'source_id' query parameters are required.",
                }
            )

        docs = PurchaseDocumentService.list_documents(
            tenant_id=tenant_id,
            actor_user=request.user,
            document_type=doc_type,
            source_id=source_id,
            only_latest=False,
        )
        return Response([self._serialize_document(d) for d in docs], status=status.HTTP_200_OK)

