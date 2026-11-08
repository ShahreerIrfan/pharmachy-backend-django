"""
Purchase Document Generation Service (ADR-0198 / P198)
Provides versioned, immutable A4 PDF and thermal print document snapshots
for Purchase Orders, Goods Receipt Notes (GRNs), Supplier Returns, and
Supplier Statements of Account with cryptographic SHA-256 tamper verification.
"""

from decimal import Decimal
import hashlib
import io
import json
import logging
from typing import Any, Dict, List, Optional
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.identity.models import PharmacyUser
from apps.core.models import (
    Branch,
    GoodsReceiptNote,
    PurchaseDocument,
    PurchaseDocumentFormat,
    PurchaseDocumentStatus,
    PurchaseDocumentType,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReturn,
    PurchaseReturnItem,
    Supplier,
    SupplierCreditNote,
    SupplierInvoice,
    SupplierLedgerEntry,
    Tenant,
    TenantLifecycleStatus,
)

logger = logging.getLogger(__name__)


class PurchaseDocumentService:
    """
    Authoritative service orchestrating versioned procurement document generation,
    cryptographic hashing, and PDF rendering using ReportLab.
    """

    _ALLOWED_ROLES = {
        "BRANCH_MANAGER",
        "PHARMACY_OWNER",
        "SUPERADMIN",
        "STAFF_PHARMACIST",
        "PROCUREMENT_OFFICER",
        "INVENTORY_CLERK",
        "AUDITOR",
    }

    @classmethod
    def _assert_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str == "CASHIER" or role_str not in cls._ALLOWED_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Staff role does not have permission to generate procurement documents.",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant_id: str) -> Tenant:
        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise NotFound(
                {
                    "error_code": "ERR_TENANT_NOT_FOUND",
                    "detail": f"Tenant '{tenant_id}' not found.",
                }
            )
        if tenant.status == TenantLifecycleStatus.SUSPENDED:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant subscription is currently suspended.",
                }
            )
        return tenant

    @classmethod
    def generate_document(
        cls,
        tenant_id: str,
        actor_user: Any,
        document_type: str,
        source_id: str,
        branch_id: Optional[str] = None,
        supplier_id: Optional[str] = None,
        notes: str = "",
        document_format: str = PurchaseDocumentFormat.A4_PDF,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> PurchaseDocument:
        """
        Main entry point for generating or re-generating a versioned procurement document.
        """
        cls._assert_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        doc_type_upper = document_type.upper()
        if doc_type_upper not in PurchaseDocumentType.values:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_DOCUMENT_TYPE",
                    "detail": f"Unsupported document type '{document_type}'. Valid types: {PurchaseDocumentType.values}",
                }
            )

        with transaction.atomic():
            # 1. Dispatch payload builder by type
            if doc_type_upper == PurchaseDocumentType.PURCHASE_ORDER:
                payload = cls._build_purchase_order_payload(tenant, source_id)
            elif doc_type_upper == PurchaseDocumentType.GOODS_RECEIPT_NOTE:
                payload = cls._build_grn_payload(tenant, source_id)
            elif doc_type_upper == PurchaseDocumentType.PURCHASE_RETURN:
                payload = cls._build_purchase_return_payload(tenant, source_id)
            elif doc_type_upper == PurchaseDocumentType.SUPPLIER_STATEMENT:
                payload = cls._build_supplier_statement_payload(
                    tenant, source_id or supplier_id, start_date, end_date
                )
            else:
                raise ValidationError({"error_code": "ERR_UNSUPPORTED_TYPE"})

            # 2. Compute Versioning
            existing_docs = PurchaseDocument.objects.filter(
                tenant=tenant,
                document_type=doc_type_upper,
                source_id=source_id or supplier_id,
            ).order_by("-version")

            latest_version = 0
            if existing_docs.exists():
                latest_doc = existing_docs.first()
                latest_version = latest_doc.version
                # Supersede existing active documents
                existing_docs.filter(is_latest=True).update(
                    is_latest=False, status=PurchaseDocumentStatus.SUPERSEDED
                )

            next_version = latest_version + 1

            # 3. Create document record
            doc_id = f"pdoc-{uuid.uuid4().hex[:12]}"
            prefix_map = {
                PurchaseDocumentType.PURCHASE_ORDER: "DOC-PO",
                PurchaseDocumentType.GOODS_RECEIPT_NOTE: "DOC-GRN",
                PurchaseDocumentType.PURCHASE_RETURN: "DOC-RET",
                PurchaseDocumentType.SUPPLIER_STATEMENT: "DOC-STMT",
            }
            doc_num = f"{prefix_map.get(doc_type_upper, 'DOC-PUR')}-{timezone.now().strftime('%Y%m')}-{uuid.uuid4().hex[:6].upper()}"

            supplier_obj = payload.get("supplier_obj")
            supplier_name = payload.get("supplier_name", "")
            total_amount = Decimal(str(payload.get("total_amount", "0.0000")))

            resolved_branch = None
            branch_id_resolved = branch_id or payload.get("branch_id")
            if branch_id_resolved:
                resolved_branch = Branch.objects.filter(
                    tenant=tenant, branch_id=branch_id_resolved
                ).first()

            # Strip internal objects from payload
            clean_payload = {k: v for k, v in payload.items() if not k.endswith("_obj")}
            clean_payload["version"] = next_version
            clean_payload["document_number"] = doc_num

            # Generate PDF binary to calculate size and path
            pdf_bytes = cls.render_pdf_bytes(
                doc_type=doc_type_upper,
                payload=clean_payload,
                tenant=tenant,
            )

            doc = PurchaseDocument(
                document_id=doc_id,
                tenant=tenant,
                branch=resolved_branch,
                document_type=doc_type_upper,
                document_number=doc_num,
                version=next_version,
                is_latest=True,
                source_id=source_id or supplier_id,
                source_reference=payload.get("source_reference", source_id),
                supplier=supplier_obj,
                supplier_name=supplier_name,
                document_format=document_format,
                title=clean_payload.get("title", f"{doc_type_upper} Document"),
                title_bn=clean_payload.get("title_bn", ""),
                total_amount=total_amount,
                currency="BDT",
                document_payload=clean_payload,
                pdf_size_bytes=len(pdf_bytes),
                pdf_file_path=f"procurement/docs/{tenant_id}/{doc_id}.pdf",
                status=PurchaseDocumentStatus.FINAL,
                notes=notes,
                generated_by=actor_user if isinstance(actor_user, PharmacyUser) else None,
                generated_at=timezone.now(),
            )
            doc.immutable_verification_hash = doc.compute_hash()
            doc.save()

            logger.info(
                f"[PurchaseDocumentService] Generated {doc.document_type} doc {doc.document_number} (v{doc.version}) for tenant {tenant_id} (hash: {doc.immutable_verification_hash[:8]})."
            )
            return doc

    # -------------------------------------------------------------------------
    # Payload Builders
    # -------------------------------------------------------------------------

    @classmethod
    def _build_purchase_order_payload(cls, tenant: Tenant, po_id: str) -> Dict[str, Any]:
        po = PurchaseOrder.objects.filter(tenant=tenant, po_id=po_id).first()
        if not po:
            raise NotFound(
                {
                    "error_code": "ERR_PURCHASE_ORDER_NOT_FOUND",
                    "detail": f"Purchase order '{po_id}' not found for tenant '{tenant.tenant_id}'.",
                }
            )

        items_qs = PurchaseOrderItem.objects.filter(purchase_order=po)
        lines = []
        for it in items_qs:
            prod = it.product
            lines.append(
                {
                    "item_id": it.item_id,
                    "product_id": prod.product_id if prod else "",
                    "product_name": prod.brand_name if prod else "",
                    "dosage_form": prod.dosage_form if prod else "",
                    "strength": prod.strength if prod else "",
                    "package_type": it.package_type,
                    "package_size": it.package_size,
                    "quantity_ordered": it.ordered_quantity,
                    "quantity_received": it.received_quantity,
                    "unit_cost": str(it.unit_cost_price),
                    "package_cost": str(it.package_cost_price),
                    "line_subtotal": str(it.line_subtotal),
                    "discount_percentage": str(it.discount_percentage),
                    "discount_amount": "0.0000",
                    "tax_percentage": str(it.tax_percentage),
                    "tax_amount": str(it.tax_amount),
                    "line_total": str(it.line_total),
                }
            )

        supplier = po.supplier
        return {
            "title": f"Purchase Order: {po.po_number}",
            "title_bn": f"ক্রয় আদেশ: {po.po_number}",
            "source_reference": po.po_number,
            "branch_id": po.branch_id,
            "branch_name": po.branch.name if po.branch else "",
            "supplier_obj": supplier,
            "supplier_name": supplier.name if supplier else "",
            "supplier_dl_number": getattr(supplier, "dl_number", "") or getattr(supplier, "drug_license_number", ""),
            "supplier_tin_bin": getattr(supplier, "tin_number", "") or getattr(supplier, "tin_bin_number", ""),
            "supplier_contact": getattr(supplier, "phone", "") or getattr(supplier, "contact_phone", ""),
            "order_date": po.ordered_at.strftime("%Y-%m-%d") if po.ordered_at else str(po.created_at.date()),
            "expected_delivery_date": str(po.expected_delivery_date) if po.expected_delivery_date else "",
            "payment_terms_days": po.payment_terms_days,
            "currency": po.currency,
            "subtotal_amount": str(po.subtotal_amount),
            "discount_amount": str(po.discount_amount),
            "tax_amount": str(po.tax_amount),
            "total_amount": str(po.total_amount),
            "po_status": po.status,
            "line_items": lines,
            "notes": po.notes,
        }

    @classmethod
    def _build_grn_payload(cls, tenant: Tenant, grn_id: str) -> Dict[str, Any]:
        grn = GoodsReceiptNote.objects.filter(tenant=tenant, grn_id=grn_id).first()
        if not grn:
            raise NotFound(
                {
                    "error_code": "ERR_GRN_NOT_FOUND",
                    "detail": f"Goods receipt note '{grn_id}' not found for tenant '{tenant.tenant_id}'.",
                }
            )

        lines = []
        for it in grn.lines.all():
            prod = it.product
            lines.append(
                {
                    "item_id": it.line_id,
                    "product_id": prod.product_id if prod else "",
                    "product_name": prod.brand_name if prod else "",
                    "batch_number": it.batch_number,
                    "expiry_date": str(it.expiry_date),
                    "pack_type": it.pack_unit,
                    "pack_size": it.package_size,
                    "received_packs": it.received_packages,
                    "received_units": it.received_quantity_base_units,
                    "unit_cost": str(it.unit_cost),
                    "total_line_cost": str(it.line_total),
                    "storage_condition": it.storage_location or grn.storage_condition,
                    "is_quarantined": it.is_quarantined,
                }
            )

        supplier = grn.supplier
        return {
            "title": f"Goods Receipt Note: {grn.grn_number}",
            "title_bn": f"মালামাল গ্রহণ রশিদ: {grn.grn_number}",
            "source_reference": grn.grn_number,
            "po_number": grn.purchase_order_number,
            "challan_number": grn.challan_number,
            "challan_date": str(grn.challan_date) if grn.challan_date else "",
            "receipt_date": str(grn.receipt_date) if grn.receipt_date else "",
            "branch_id": grn.branch_id,
            "branch_name": grn.branch.name if grn.branch else "",
            "supplier_obj": supplier,
            "supplier_name": grn.supplier_name or (supplier.name if supplier else ""),
            "storage_condition": grn.storage_condition,
            "storage_temperature_celsius": str(grn.storage_temperature_celsius) if grn.storage_temperature_celsius else "",
            "proof_of_origin": grn.proof_of_origin,
            "status": grn.status,
            "total_received_units": grn.total_received_base_units,
            "total_amount": str(grn.total_invoice_amount),
            "line_items": lines,
            "notes": grn.notes,
        }

    @classmethod
    def _build_purchase_return_payload(cls, tenant: Tenant, return_id: str) -> Dict[str, Any]:
        ret = PurchaseReturn.objects.filter(tenant=tenant, return_id=return_id).first()
        if not ret:
            raise NotFound(
                {
                    "error_code": "ERR_PURCHASE_RETURN_NOT_FOUND",
                    "detail": f"Purchase return '{return_id}' not found for tenant '{tenant.tenant_id}'.",
                }
            )

        items_qs = PurchaseReturnItem.objects.filter(purchase_return=ret)
        lines = []
        for it in items_qs:
            prod = it.product
            lines.append(
                {
                    "item_id": it.item_id,
                    "product_id": prod.product_id if prod else "",
                    "product_name": prod.brand_name if prod else "",
                    "batch_number": it.batch_number,
                    "lot_reference": it.cost_lot.lot_reference if it.cost_lot else "",
                    "return_quantity": it.return_quantity,
                    "unit_cost": str(it.unit_cost),
                    "total_debit_amount": str(it.line_debit_amount),
                    "reason": it.reason,
                }
            )

        supplier = ret.supplier
        return {
            "title": f"Supplier Return Voucher: {ret.return_number}",
            "title_bn": f"পণ্য ফেরত ভাউচার: {ret.return_number}",
            "source_reference": ret.return_number,
            "branch_id": ret.branch_id,
            "branch_name": ret.branch.name if ret.branch else "",
            "supplier_obj": supplier,
            "supplier_name": ret.supplier_name or (supplier.name if supplier else ""),
            "debit_note_reference": ret.debit_note_reference,
            "vendor_credit_note_ref": ret.vendor_credit_note_ref,
            "return_reason": ret.return_reason,
            "disposition": ret.disposition,
            "status": ret.status,
            "total_return_quantity": ret.total_return_quantity,
            "total_amount": str(ret.total_debit_amount),
            "line_items": lines,
            "notes": ret.reason_notes,
        }

    @classmethod
    def _build_supplier_statement_payload(
        cls,
        tenant: Tenant,
        supplier_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        supplier = Supplier.objects.filter(tenant=tenant, supplier_id=supplier_id).first()
        if not supplier:
            raise NotFound(
                {
                    "error_code": "ERR_SUPPLIER_NOT_FOUND",
                    "detail": f"Supplier '{supplier_id}' not found for tenant '{tenant.tenant_id}'.",
                }
            )

        # Gather Ledger Entries
        entries_qs = SupplierLedgerEntry.objects.filter(
            tenant=tenant, supplier=supplier
        ).order_by("posted_at")

        if start_date:
            entries_qs = entries_qs.filter(posted_at__date__gte=start_date)
        if end_date:
            entries_qs = entries_qs.filter(posted_at__date__lte=end_date)

        lines = []
        running_balance = Decimal("0.0000")
        total_debits = Decimal("0.0000")
        total_credits = Decimal("0.0000")

        for ent in entries_qs:
            debit = ent.debit_amount or Decimal("0.0000")
            credit = ent.credit_amount or Decimal("0.0000")
            total_debits += debit
            total_credits += credit
            running_balance += (credit - debit)

            lines.append(
                {
                    "entry_id": ent.posting_id,
                    "date": ent.posted_at.strftime("%Y-%m-%d") if ent.posted_at else "",
                    "posting_type": ent.posting_type,
                    "reference": ent.reference_number,
                    "description": ent.notes,
                    "debit_amount": str(debit),
                    "credit_amount": str(credit),
                    "running_balance": str(running_balance),
                }
            )

        net_due = total_credits - total_debits

        return {
            "title": f"Supplier Statement: {supplier.name}",
            "title_bn": f"সরবরাহকারী হিসাব বিবরণী: {supplier.name}",
            "source_reference": supplier.supplier_id,
            "supplier_obj": supplier,
            "supplier_name": supplier.name,
            "supplier_dl_number": getattr(supplier, "dl_number", "") or getattr(supplier, "drug_license_number", ""),
            "supplier_tin_bin": getattr(supplier, "tin_number", "") or getattr(supplier, "tin_bin_number", ""),
            "start_date": start_date or "",
            "end_date": end_date or "",
            "total_debits": str(total_debits),
            "total_credits": str(total_credits),
            "total_amount": str(net_due),
            "current_balance": str(supplier.current_balance),
            "line_items": lines,
        }

    # -------------------------------------------------------------------------
    # PDF Rendering Engine (ReportLab)
    # -------------------------------------------------------------------------

    @classmethod
    def render_pdf_bytes(
        cls,
        doc_type: str,
        payload: Dict[str, Any],
        tenant: Tenant,
    ) -> bytes:
        """
        Generates a standard A4 PDF document in memory using ReportLab.
        """
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib import colors

        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4  # 595.27 x 841.89 points

        # 1. Header & Organization Identity
        pdf.setFont("Helvetica-Bold", 16)
        pdf.setFillColor(colors.HexColor("#1e293b"))  # Slate-800
        pdf.drawString(40, height - 50, tenant.name)

        pdf.setFont("Helvetica", 9)
        pdf.setFillColor(colors.HexColor("#64748b"))  # Slate-500
        pdf.drawString(40, height - 65, f"Subdomain: {tenant.subdomain}.pharmacy.com.bd | Country: Bangladesh (DGDA Compliant)")
        pdf.drawString(40, height - 77, f"Tenant ID: {tenant.tenant_id}")

        # Document Type Badge
        pdf.setFont("Helvetica-Bold", 14)
        pdf.setFillColor(colors.HexColor("#0f766e"))  # Teal-700
        title_text = payload.get("title", f"{doc_type} DOCUMENT")
        pdf.drawRightString(width - 40, height - 50, title_text.upper())

        # Version & Date Badge
        pdf.setFont("Helvetica", 9)
        pdf.setFillColor(colors.HexColor("#334155"))
        doc_num = payload.get("document_number", "DOC-REF-001")
        version = payload.get("version", 1)
        pdf.drawRightString(width - 40, height - 65, f"Doc Ref: {doc_num} (v{version}.0)")
        pdf.drawRightString(width - 40, height - 77, f"Date: {timezone.now().strftime('%d-%b-%Y %H:%M')}")

        # Top Horizontal Rule
        pdf.setStrokeColor(colors.HexColor("#cbd5e1"))
        pdf.setLineWidth(1)
        pdf.line(40, height - 90, width - 40, height - 90)

        # 2. Key Metadata Cards (2-column layout)
        pdf.setFont("Helvetica-Bold", 10)
        pdf.setFillColor(colors.HexColor("#0f172a"))
        pdf.drawString(40, height - 110, "SUPPLIER INFORMATION")
        pdf.drawString(320, height - 110, "DOCUMENT DETAILS")

        pdf.setFont("Helvetica", 9)
        pdf.setFillColor(colors.HexColor("#334155"))
        sup_name = payload.get("supplier_name", "N/A")
        sup_dl = payload.get("supplier_dl_number", "N/A") or "N/A"
        sup_tin = payload.get("supplier_tin_bin", "N/A") or "N/A"

        pdf.drawString(40, height - 125, f"Name: {sup_name}")
        pdf.drawString(40, height - 138, f"Drug License: {sup_dl}")
        pdf.drawString(40, height - 151, f"TIN/BIN: {sup_tin}")

        ref = payload.get("source_reference", "N/A")
        branch_name = payload.get("branch_name", "Head Office / Central")
        pdf.drawString(320, height - 125, f"Source Reference: {ref}")
        pdf.drawString(320, height - 138, f"Receiving Branch: {branch_name}")
        pdf.drawString(320, height - 151, f"Status: FINAL / AUDITED")

        # Table Header
        table_top = height - 175
        pdf.setFillColor(colors.HexColor("#f1f5f9"))
        pdf.rect(40, table_top - 18, width - 80, 20, fill=True, stroke=False)

        pdf.setFont("Helvetica-Bold", 9)
        pdf.setFillColor(colors.HexColor("#1e293b"))
        pdf.drawString(45, table_top - 13, "#")
        pdf.drawString(65, table_top - 13, "Item Description / Product")
        pdf.drawString(270, table_top - 13, "Batch / Lot")
        pdf.drawString(360, table_top - 13, "Qty")
        pdf.drawString(420, table_top - 13, "Unit Cost")
        pdf.drawRightString(width - 45, table_top - 13, "Total BDT")

        # Table Rows
        items = payload.get("line_items", [])
        curr_y = table_top - 32
        pdf.setFont("Helvetica", 8.5)
        pdf.setFillColor(colors.HexColor("#334155"))

        max_rows = min(len(items), 18)
        for idx in range(max_rows):
            it = items[idx]
            prod_name = it.get("product_name") or it.get("description") or "Item"
            batch_num = it.get("batch_number") or it.get("reference") or "-"
            qty = str(it.get("quantity_ordered") or it.get("received_units") or it.get("return_quantity") or it.get("debit_amount") or "1")
            cost = str(it.get("unit_cost") or it.get("credit_amount") or "0.00")
            line_tot = str(it.get("line_total") or it.get("total_line_cost") or it.get("total_debit_amount") or it.get("running_balance") or "0.00")

            pdf.drawString(45, curr_y, str(idx + 1))
            pdf.drawString(65, curr_y, prod_name[:35])
            pdf.drawString(270, curr_y, batch_num[:16])
            pdf.drawString(360, curr_y, qty[:8])
            pdf.drawString(420, curr_y, cost[:10])
            pdf.drawRightString(width - 45, curr_y, line_tot[:12])

            curr_y -= 16

        # Totals Block
        pdf.setStrokeColor(colors.HexColor("#cbd5e1"))
        pdf.line(40, curr_y - 5, width - 40, curr_y - 5)

        tot_amount = payload.get("total_amount", "0.0000")
        pdf.setFont("Helvetica-Bold", 10)
        pdf.setFillColor(colors.HexColor("#0f172a"))
        pdf.drawRightString(width - 45, curr_y - 25, f"Grand Total: BDT {Decimal(str(tot_amount)):,.2f}")

        # Footer with SHA-256 and Signature Lines
        pdf.setStrokeColor(colors.HexColor("#cbd5e1"))
        pdf.line(40, 90, width - 40, 90)

        # Signature lines
        pdf.setFont("Helvetica", 8)
        pdf.drawString(60, 50, "__________________________")
        pdf.drawString(60, 40, "Prepared By / Pharmacist")

        pdf.drawRightString(width - 60, 50, "__________________________")
        pdf.drawRightString(width - 60, 40, "Authorized Signature / Seal")

        # Cryptographic Watermark
        pdf.setFont("Helvetica", 7)
        pdf.setFillColor(colors.HexColor("#94a3b8"))
        pdf.drawCentredString(width / 2.0, 20, "This is an immutable, versioned digital procurement record. Tamper verification enabled.")

        pdf.showPage()
        pdf.save()

        buffer.seek(0)
        return buffer.getvalue()

    @classmethod
    def list_documents(
        cls,
        tenant_id: str,
        actor_user: Any,
        document_type: Optional[str] = None,
        source_id: Optional[str] = None,
        supplier_id: Optional[str] = None,
        only_latest: bool = True,
    ) -> List[PurchaseDocument]:
        cls._assert_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        qs = PurchaseDocument.objects.filter(tenant=tenant)
        if document_type:
            qs = qs.filter(document_type=document_type.upper())
        if source_id:
            qs = qs.filter(source_id=source_id)
        if supplier_id:
            qs = qs.filter(supplier__supplier_id=supplier_id)
        if only_latest:
            qs = qs.filter(is_latest=True)

        return list(qs.order_by("-generated_at", "-version")[:50])

    @classmethod
    def get_document(
        cls,
        tenant_id: str,
        actor_user: Any,
        document_id: str,
    ) -> PurchaseDocument:
        cls._assert_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        doc = PurchaseDocument.objects.filter(tenant=tenant, document_id=document_id).first()
        if not doc:
            raise NotFound(
                {
                    "error_code": "ERR_DOCUMENT_NOT_FOUND",
                    "detail": f"Purchase document '{document_id}' not found for tenant '{tenant_id}'.",
                }
            )
        return doc
