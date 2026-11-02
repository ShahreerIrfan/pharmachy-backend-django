"""
Field Visibility REST API ViewSet (P110 / ADR-0110).
Exposes endpoints for:
1. Field visibility profile inspection (/api/v2/field-visibility/)
2. Masked medicine catalog (/api/v2/field-visibility/medicines/)
3. Masked sales invoices & margins (/api/v2/field-visibility/invoices/)
4. Masked staff payroll & salaries (/api/v2/field-visibility/salaries/)
5. Masked prescriptions & clinical notes (/api/v2/field-visibility/prescriptions/)
6. Arbitrary batch masking (/api/v2/field-visibility/mask/)
7. Audited masked dataset export (/api/v2/field-visibility/export/)
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional, Tuple

from django.http import HttpResponse
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.field_visibility_service import FieldVisibilityService
from apps.core.models import (
    MedicineProduct,
    PrescriptionRecord,
    SaleInvoice,
    StaffSalaryRecord,
)

logger = logging.getLogger("pharmacy.field_visibility.views")


class FieldVisibilityViewSet(viewsets.ViewSet):
    """Fine-grained field visibility evaluation, data masking, and secure export viewset."""

    permission_classes = [permissions.IsAuthenticated]

    def _resolve_tenant_and_role(self, request) -> Tuple[str, str, Optional[str]]:
        user = request.user
        role = str(getattr(user, "role", "")).upper()

        if not user or not user.is_authenticated:
            raise PermissionDenied(
                {
                    "error_code": "ERR_AUTHENTICATION_REQUIRED",
                    "message_en": "Authentication credentials were not provided.",
                    "message_bn": "প্রবেশাধিকারের জন্য অথেনটিকেশন প্রয়োজন।",
                }
            )

        tenant_id = (
            getattr(user, "tenant_id", None)
            or getattr(user, "tenant", None)
            or getattr(request, "tenant_id", None)
        )
        if hasattr(tenant_id, "tenant_id"):
            tenant_id = tenant_id.tenant_id
        tenant_id = str(tenant_id) if tenant_id else None

        req_tenant = request.query_params.get("tenant_id") or (
            request.data.get("tenant_id") if isinstance(request.data, dict) else None
        )
        if req_tenant and tenant_id and req_tenant != tenant_id and role not in [
            "SUPERADMIN",
            "SUPER_ADMIN",
            "PLATFORM_OPERATOR",
        ]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": f"Access denied: cannot access foreign tenant '{req_tenant}'.",
                    "message_bn": f"অননুমোদিত টেন্যান্ট অ্যাক্সেস: '{req_tenant}' দেখার অনুমতি নেই।",
                }
            )

        if not tenant_id and req_tenant:
            tenant_id = req_tenant

        if not tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_CONTEXT_MISSING",
                    "message_en": "Tenant context missing.",
                    "message_bn": "টেন্যান্ট কনটেক্সট অনুপস্থিত।",
                }
            )

        user_branch_id = getattr(user, "branch_id", None)
        return str(tenant_id), role, str(user_branch_id) if user_branch_id else None

    def list(self, request) -> Response:
        """
        GET /api/v2/field-visibility/
        Returns caller's effective 5 field visibility permissions along with sample masked payloads.
        Strictly conforms to field_visibility.schema.json.
        """
        tenant_id, role, branch_id = self._resolve_tenant_and_role(request)
        perms = FieldVisibilityService.resolve_field_visibility_profile(
            request.user, tenant_id=tenant_id, branch_id=branch_id
        )

        user_id = str(getattr(request.user, "user_id", getattr(request.user, "id", "")))

        sample_med = FieldVisibilityService.mask_medicine_record(
            {
                "product_id": "sample-med-001",
                "brand_name": "Napa Extra",
                "mrp_unit": "2.50",
                "cost_unit": "1.80",
            },
            perms,
        )

        sample_inv = FieldVisibilityService.mask_invoice_record(
            {
                "invoice_id": "sample-inv-001",
                "customer_name": "Mohammad Ali",
                "customer_phone": "01712345678",
                "total_amount": "500.00",
                "gross_profit": "120.00",
                "cogs_amount": "380.00",
            },
            perms,
        )

        sample_sal = FieldVisibilityService.mask_salary_record(
            {
                "staff_id": "sample-stf-001",
                "username": getattr(request.user, "username", getattr(request.user, "email", "")),
                "base_salary_bdt": "35000.00",
                "bank_account_no": "1234567890123",
            },
            perms,
        )

        sample_rx = FieldVisibilityService.mask_prescription_record(
            {
                "prescription_id": "sample-rx-001",
                "doctor_name": "Dr. S. K. Roy",
                "diagnosis": "Acute Bronchitis & Respiratory Infection",
                "prescription_image_url": "https://cdn.pharmacy-saas.com/rx/sample-rx-001.jpg",
            },
            perms,
        )

        payload = {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "role": role,
            "visibility_permissions": perms,
            "masked_medicine_sample": sample_med,
            "masked_invoice_sample": sample_inv,
            "masked_salary_sample": sample_sal,
            "masked_prescription_sample": sample_rx,
        }
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="mask")
    def mask(self, request) -> Response:
        """
        POST /api/v2/field-visibility/mask/
        Universal masking endpoint: accepts a dataset of arbitrary records and applies
        caller-specific field masking.
        """
        tenant_id, _, branch_id = self._resolve_tenant_and_role(request)
        perms = FieldVisibilityService.resolve_field_visibility_profile(
            request.user, tenant_id=tenant_id, branch_id=branch_id
        )

        dataset_type = str(request.data.get("dataset_type", "AUTO")).upper()
        records = request.data.get("records", [])
        if not isinstance(records, list):
            raise ValidationError({"error_code": "INVALID_RECORDS", "message": "'records' must be a list of objects."})

        masked_records = FieldVisibilityService.mask_dataset(records, perms, dataset_type=dataset_type)
        return Response(
            {
                "dataset_type": dataset_type,
                "count": len(masked_records),
                "permissions_applied": perms,
                "results": masked_records,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="medicines")
    def medicines(self, request) -> Response:
        """
        GET /api/v2/field-visibility/medicines/
        Returns list of medicines with wholesale cost unit masked if cost_visibility is false.
        """
        tenant_id, _, branch_id = self._resolve_tenant_and_role(request)
        perms = FieldVisibilityService.resolve_field_visibility_profile(
            request.user, tenant_id=tenant_id, branch_id=branch_id
        )

        qs = MedicineProduct.objects.all()[:100]
        items = []
        for m in qs:
            items.append(
                {
                    "product_id": m.product_id,
                    "brand_name": m.brand_name,
                    "generic_name": m.generic_name,
                    "dosage_form": m.dosage_form,
                    "mrp_unit": str(m.mrp_unit),
                    "cost_unit": str(m.cost_unit) if m.cost_unit is not None else None,
                    "requires_cold_chain": m.requires_cold_chain,
                }
            )

        masked = FieldVisibilityService.mask_dataset(items, perms, dataset_type="MEDICINE")
        return Response(
            {
                "count": len(masked),
                "cost_visibility": perms.get("cost_visibility", False),
                "results": masked,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="invoices")
    def invoices(self, request) -> Response:
        """
        GET /api/v2/field-visibility/invoices/
        Returns list of sales invoices with:
        - customer_phone & customer_name masked if patient_pii_visibility is false
        - cogs_amount masked if cost_visibility is false
        - gross_profit masked if profit_visibility is false
        """
        tenant_id, _, branch_id = self._resolve_tenant_and_role(request)
        perms = FieldVisibilityService.resolve_field_visibility_profile(
            request.user, tenant_id=tenant_id, branch_id=branch_id
        )

        qs = SaleInvoice.objects.filter(tenant_id=tenant_id).order_by("-created_at")[:50]
        items = []
        for inv in qs:
            # Synthetic profit/cogs calculation for demonstration
            net_amt = inv.net_amount or Decimal("0.00")
            cogs_est = (net_amt * Decimal("0.75")).quantize(Decimal("0.01"))
            gross_prof = (net_amt - cogs_est).quantize(Decimal("0.01"))

            items.append(
                {
                    "invoice_id": inv.invoice_id,
                    "invoice_number": inv.invoice_number,
                    "customer_name": inv.customer_name,
                    "customer_phone": inv.customer_phone,
                    "total_amount": str(inv.total_amount),
                    "net_amount": str(net_amt),
                    "cogs_amount": str(cogs_est),
                    "gross_profit": str(gross_prof),
                    "margin_percent": "25.0",
                    "payment_method": inv.payment_method,
                    "created_at": inv.created_at.isoformat(),
                }
            )

        masked = FieldVisibilityService.mask_dataset(items, perms, dataset_type="INVOICE")
        return Response(
            {
                "count": len(masked),
                "permissions_applied": {
                    "cost_visibility": perms.get("cost_visibility", False),
                    "profit_visibility": perms.get("profit_visibility", False),
                    "patient_pii_visibility": perms.get("patient_pii_visibility", False),
                },
                "results": masked,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get", "post"], url_path="salaries")
    def salaries(self, request) -> Response:
        """
        GET/POST /api/v2/field-visibility/salaries/
        - GET: Returns list of staff salary records. Masked if salary_visibility is false.
        - POST: Creates or updates salary record (strictly gated by salary_visibility).
        """
        tenant_id, role, branch_id = self._resolve_tenant_and_role(request)
        perms = FieldVisibilityService.resolve_field_visibility_profile(
            request.user, tenant_id=tenant_id, branch_id=branch_id
        )

        if request.method == "POST":
            # Creation / modification requires salary_visibility
            staff_user_id = str(request.data.get("staff_user_id", "")).strip()
            if not staff_user_id:
                raise ValidationError({"error_code": "MISSING_STAFF_ID", "message": "staff_user_id is required."})

            try:
                base_sal = Decimal(str(request.data.get("base_salary_bdt", "0.00")))
            except Exception:
                raise ValidationError({"error_code": "INVALID_SALARY", "message": "base_salary_bdt must be a valid number."})

            hourly_rate = Decimal(str(request.data.get("hourly_rate_bdt", "0.00")))
            allowances = Decimal(str(request.data.get("allowances_bdt", "0.00")))
            bank_name = str(request.data.get("bank_name", "")).strip()
            bank_acc = str(request.data.get("bank_account_no", "")).strip()
            branch = request.data.get("branch_id") or branch_id

            record = FieldVisibilityService.create_or_update_salary(
                actor_user=request.user,
                tenant_id=tenant_id,
                staff_user_id=staff_user_id,
                base_salary_bdt=base_sal,
                hourly_rate_bdt=hourly_rate,
                allowances_bdt=allowances,
                bank_name=bank_name,
                bank_account_no=bank_acc,
                branch_id=branch,
            )

            return Response(
                {
                    "message_en": "Salary record saved successfully.",
                    "message_bn": "বেতন সংক্রান্ত তথ্য সফলভাবে সংরক্ষিত হয়েছে।",
                    "salary_id": record.salary_id,
                    "staff_user_id": staff_user_id,
                    "base_salary_bdt": str(record.base_salary_bdt),
                    "bank_account_no": record.bank_account_no,
                },
                status=status.HTTP_201_CREATED,
            )

        # GET: Read salaries with masking
        qs = StaffSalaryRecord.objects.filter(tenant_id=tenant_id, is_active=True).select_related("staff_member")[:50]
        items = []
        for sal in qs:
            items.append(
                {
                    "salary_id": sal.salary_id,
                    "staff_user_id": str(sal.staff_member.user_id),
                    "username": getattr(sal.staff_member, "username", getattr(sal.staff_member, "email", "")),
                    "base_salary_bdt": str(sal.base_salary_bdt),
                    "hourly_rate_bdt": str(sal.hourly_rate_bdt),
                    "allowances_bdt": str(sal.allowances_bdt),
                    "bank_name": sal.bank_name,
                    "bank_account_no": sal.bank_account_no,
                    "effective_from": sal.effective_from.isoformat(),
                }
            )

        masked = FieldVisibilityService.mask_dataset(items, perms, dataset_type="SALARY")
        return Response(
            {
                "count": len(masked),
                "salary_visibility": perms.get("salary_visibility", False),
                "results": masked,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="prescriptions")
    def prescriptions(self, request) -> Response:
        """
        GET /api/v2/field-visibility/prescriptions/
        Returns list of prescriptions with diagnosis and clinical notes masked if prescription_visibility is false,
        and patient PII masked if patient_pii_visibility is false.
        """
        tenant_id, _, branch_id = self._resolve_tenant_and_role(request)
        perms = FieldVisibilityService.resolve_field_visibility_profile(
            request.user, tenant_id=tenant_id, branch_id=branch_id
        )

        qs = PrescriptionRecord.objects.filter(tenant_id=tenant_id).order_by("-created_at")[:50]
        items = []
        for rx in qs:
            items.append(
                {
                    "prescription_id": rx.prescription_id,
                    "doctor_name": rx.doctor_name,
                    "doctor_bmdc_no": rx.doctor_bmdc_no,
                    "patient_token": rx.patient_token,
                    "patient_name": f"Patient-{rx.patient_token}",
                    "patient_age": rx.patient_age,
                    "patient_gender": rx.patient_gender,
                    "diagnosis": rx.diagnosis,
                    "notes": rx.notes,
                    "prescription_image_url": f"https://cdn.pharmacy-saas.com/rx/{rx.prescription_id}.jpg",
                    "status": rx.status,
                    "created_at": rx.created_at.isoformat(),
                }
            )

        masked = FieldVisibilityService.mask_dataset(items, perms, dataset_type="PRESCRIPTION")
        return Response(
            {
                "count": len(masked),
                "prescription_visibility": perms.get("prescription_visibility", False),
                "patient_pii_visibility": perms.get("patient_pii_visibility", False),
                "results": masked,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="export")
    def export(self, request) -> HttpResponse:
        """
        POST /api/v2/field-visibility/export/
        Downloads masked CSV or JSON dataset. Crucially ensures that unauthorized users
        cannot bypass field-level masking by exporting files.
        """
        tenant_id, _, branch_id = self._resolve_tenant_and_role(request)
        perms = FieldVisibilityService.resolve_field_visibility_profile(
            request.user, tenant_id=tenant_id, branch_id=branch_id
        )

        dataset_type = str(request.data.get("dataset_type", "MEDICINE")).upper().strip()
        export_fmt = str(request.data.get("format", "CSV")).upper().strip()

        # Build raw dataset according to type
        raw_items = []
        if dataset_type == "MEDICINE":
            qs = MedicineProduct.objects.all()[:100]
            for m in qs:
                raw_items.append({
                    "product_id": m.product_id,
                    "brand_name": m.brand_name,
                    "generic_name": m.generic_name,
                    "mrp_unit": str(m.mrp_unit),
                    "cost_unit": str(m.cost_unit) if m.cost_unit is not None else None,
                })
        elif dataset_type == "INVOICE":
            qs = SaleInvoice.objects.filter(tenant_id=tenant_id)[:50]
            for inv in qs:
                net_amt = inv.net_amount or Decimal("0.00")
                cogs = (net_amt * Decimal("0.75")).quantize(Decimal("0.01"))
                profit = (net_amt - cogs).quantize(Decimal("0.01"))
                raw_items.append({
                    "invoice_id": inv.invoice_id,
                    "customer_name": inv.customer_name,
                    "customer_phone": inv.customer_phone,
                    "total_amount": str(inv.total_amount),
                    "cogs_amount": str(cogs),
                    "gross_profit": str(profit),
                })
        elif dataset_type == "SALARY":
            qs = StaffSalaryRecord.objects.filter(tenant_id=tenant_id, is_active=True)[:50]
            for sal in qs:
                raw_items.append({
                    "salary_id": sal.salary_id,
                    "staff_user_id": str(sal.staff_member_id),
                    "base_salary_bdt": str(sal.base_salary_bdt),
                    "bank_account_no": sal.bank_account_no,
                })
        elif dataset_type == "PRESCRIPTION":
            qs = PrescriptionRecord.objects.filter(tenant_id=tenant_id)[:50]
            for rx in qs:
                raw_items.append({
                    "prescription_id": rx.prescription_id,
                    "doctor_name": rx.doctor_name,
                    "diagnosis": rx.diagnosis,
                    "notes": rx.notes,
                })
        else:
            raise ValidationError({"error_code": "INVALID_DATASET_TYPE", "message": f"Unknown dataset_type '{dataset_type}'."})

        ip_addr = request.META.get("REMOTE_ADDR", "127.0.0.1")
        content, content_type, filename = FieldVisibilityService.generate_masked_export(
            dataset=raw_items,
            perms=perms,
            dataset_type=dataset_type,
            export_format=export_fmt,
            actor_user=request.user,
            tenant_id=tenant_id,
            ip_address=ip_addr,
        )

        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

