"""
Granular Action Permissions REST API ViewSets (P104 / ADR-0104).
Enforces fine-grained authorization separately for:
sale, refund, discount, cost_visibility, stock_change,
purchasing, expense_approval, export, and rx_review.
Also includes wholesale acquisition cost field-masking.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional, Tuple

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.granular_permission_service import GranularActionPermissionService
from apps.core.models import MedicineProduct
from apps.core.postgres_service import CrossTenantOperationError

logger = logging.getLogger("pharmacy.granular_permissions.views")


class GranularPermissionViewSet(viewsets.ViewSet):
    """
    Granular action permissions evaluation, masking, and operational gate endpoints.
    """

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
        GET /api/v2/permissions/granular/
        Returns caller's effective 9 action permissions and cost visibility status.
        Conforms strictly to granular_action_permissions.schema.json.
        """
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        effective = GranularActionPermissionService.resolve_effective_actions(
            request.user, tenant_id=tenant_id
        )

        user_id = str(getattr(request.user, "user_id", getattr(request.user, "id", "")))
        payload = {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "role": role,
            "cost_visibility_enabled": effective.get("cost_visibility", False),
            "actions": effective,
        }
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="evaluate")
    def evaluate(self, request) -> Response:
        """
        POST /api/v2/permissions/granular/evaluate/
        Evaluates a single prospective action for the authenticated user.
        """
        tenant_id, role, user_branch = self._resolve_tenant_and_role(request)
        action_name = str(request.data.get("action", "")).strip().lower()
        if not action_name:
            raise ValidationError({"error_code": "MISSING_ACTION", "message": "Action name is required."})

        branch_id = request.data.get("branch_id") or user_branch
        allowed, reason = GranularActionPermissionService.has_action_permission(
            user=request.user,
            action_name=action_name,
            branch_id=branch_id,
            tenant_id=tenant_id,
        )

        effective = GranularActionPermissionService.resolve_effective_actions(
            request.user, tenant_id=tenant_id
        )
        return Response(
            {
                "action": action_name,
                "allowed": allowed,
                "reason": reason,
                "role": role,
                "cost_visibility_enabled": effective.get("cost_visibility", False),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="medicines")
    def medicines(self, request) -> Response:
        """
        GET /api/v2/permissions/granular/medicines/
        Returns list of medicine catalog items with wholesale cost price
        masked if the caller lacks 'cost_visibility' permission.
        """
        tenant_id, _, _ = self._resolve_tenant_and_role(request)
        medicines_qs = MedicineProduct.objects.all()[:100]

        items = []
        for med in medicines_qs:
            items.append(
                {
                    "product_id": med.product_id,
                    "brand_name": med.brand_name,
                    "generic_name": med.generic_name,
                    "dosage_form": med.dosage_form,
                    "strength": med.strength,
                    "manufacturer": med.manufacturer,
                    "mrp_unit": str(med.mrp_unit),
                    "cost_unit": str(med.cost_unit) if med.cost_unit is not None else None,
                    "requires_cold_chain": med.requires_cold_chain,
                    "dgda_dar_number": med.dgda_dar_number,
                }
            )

        masked_items = GranularActionPermissionService.mask_cost_fields(
            items, request.user, tenant_id=tenant_id
        )
        has_cost, _ = GranularActionPermissionService.has_action_permission(
            request.user, "cost_visibility", tenant_id=tenant_id
        )

        return Response(
            {
                "count": len(masked_items),
                "cost_visibility_granted": has_cost,
                "results": masked_items,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="sale")
    def sale(self, request) -> Response:
        """POST /api/v2/permissions/granular/sale/"""
        tenant_id, _, user_branch = self._resolve_tenant_and_role(request)
        branch_id = request.data.get("branch_id") or user_branch or "main-branch"
        invoice_id = request.data.get("invoice_id", "inv-test")
        try:
            total_amount_bdt = Decimal(str(request.data.get("total_amount_bdt", "100.00")))
        except Exception:
            raise ValidationError({"error_code": "INVALID_AMOUNT", "message": "Invalid total_amount_bdt."})

        result = GranularActionPermissionService.execute_sale(
            user=request.user,
            tenant_id=tenant_id,
            branch_id=branch_id,
            invoice_id=invoice_id,
            total_amount_bdt=total_amount_bdt,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="refund")
    def refund(self, request) -> Response:
        """POST /api/v2/permissions/granular/refund/"""
        tenant_id, _, user_branch = self._resolve_tenant_and_role(request)
        branch_id = request.data.get("branch_id") or user_branch or "main-branch"
        invoice_id = request.data.get("invoice_id", "inv-test")
        reason = request.data.get("reason", "Customer return")
        try:
            refund_amount_bdt = Decimal(str(request.data.get("refund_amount_bdt", "50.00")))
        except Exception:
            raise ValidationError({"error_code": "INVALID_AMOUNT", "message": "Invalid refund_amount_bdt."})

        result = GranularActionPermissionService.execute_refund(
            user=request.user,
            tenant_id=tenant_id,
            branch_id=branch_id,
            invoice_id=invoice_id,
            refund_amount_bdt=refund_amount_bdt,
            reason=reason,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="discount")
    def discount(self, request) -> Response:
        """POST /api/v2/permissions/granular/discount/"""
        tenant_id, _, user_branch = self._resolve_tenant_and_role(request)
        branch_id = request.data.get("branch_id") or user_branch or "main-branch"
        reason = request.data.get("reason", "Manager special approval")
        try:
            discount_percentage = Decimal(str(request.data.get("discount_percentage", "15.00")))
        except Exception:
            raise ValidationError({"error_code": "INVALID_DISCOUNT", "message": "Invalid discount_percentage."})

        result = GranularActionPermissionService.execute_discount(
            user=request.user,
            tenant_id=tenant_id,
            branch_id=branch_id,
            discount_percentage=discount_percentage,
            reason=reason,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="stock-change")
    def stock_change(self, request) -> Response:
        """POST /api/v2/permissions/granular/stock-change/"""
        tenant_id, _, user_branch = self._resolve_tenant_and_role(request)
        branch_id = request.data.get("branch_id") or user_branch or "main-branch"
        medicine_id = request.data.get("medicine_id")
        if not medicine_id:
            raise ValidationError({"error_code": "MISSING_MEDICINE", "message": "medicine_id is required."})

        quantity_delta = int(request.data.get("quantity_delta", -1))
        reason = request.data.get("reason", "Physical count adjustment")
        batch_id = request.data.get("batch_id")
        adjustment_type = request.data.get("adjustment_type", "DAMAGE")

        try:
            result = GranularActionPermissionService.execute_stock_change(
                user=request.user,
                tenant_id=tenant_id,
                branch_id=branch_id,
                medicine_id=medicine_id,
                quantity_delta=quantity_delta,
                reason=reason,
                batch_id=batch_id,
                adjustment_type=adjustment_type,
            )
        except CrossTenantOperationError as err:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": str(err),
                    "message_bn": "ভুল টেন্যান্ট বা ব্রাঞ্চ রেফারেন্স।",
                }
            )

        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="purchasing")
    def purchasing(self, request) -> Response:
        """POST /api/v2/permissions/granular/purchasing/"""
        tenant_id, _, user_branch = self._resolve_tenant_and_role(request)
        branch_id = request.data.get("branch_id") or user_branch or "main-branch"
        medicine_id = request.data.get("medicine_id", "med-001")
        quantity = int(request.data.get("quantity", 50))
        try:
            unit_cost_bdt = Decimal(str(request.data.get("unit_cost_bdt", "10.00")))
        except Exception:
            raise ValidationError({"error_code": "INVALID_COST", "message": "Invalid unit_cost_bdt."})
        supplier_name = request.data.get("supplier_name", "Beximco Pharmaceuticals")

        result = GranularActionPermissionService.execute_purchasing(
            user=request.user,
            tenant_id=tenant_id,
            branch_id=branch_id,
            medicine_id=medicine_id,
            quantity=quantity,
            unit_cost_bdt=unit_cost_bdt,
            supplier_name=supplier_name,
        )
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="expense-approval")
    def expense_approval(self, request) -> Response:
        """POST /api/v2/permissions/granular/expense-approval/"""
        tenant_id, _, user_branch = self._resolve_tenant_and_role(request)
        branch_id = request.data.get("branch_id") or user_branch or "main-branch"
        category = request.data.get("category", "OPERATIONAL")
        try:
            amount_bdt = Decimal(str(request.data.get("amount_bdt", "500.00")))
        except Exception:
            raise ValidationError({"error_code": "INVALID_AMOUNT", "message": "Invalid amount_bdt."})
        description = request.data.get("description", "Office cleaning and maintenance")
        approval_action = request.data.get("approval_action", "APPROVED")

        try:
            result = GranularActionPermissionService.execute_expense_approval(
                user=request.user,
                tenant_id=tenant_id,
                branch_id=branch_id,
                category=category,
                amount_bdt=amount_bdt,
                description=description,
                approval_action=approval_action,
            )
        except CrossTenantOperationError as err:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": str(err),
                    "message_bn": "ভুল টেন্যান্ট বা ব্রাঞ্চ রেফারেন্স।",
                }
            )

        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="export")
    def export(self, request) -> Response:
        """POST /api/v2/permissions/granular/export/"""
        tenant_id, _, _ = self._resolve_tenant_and_role(request)
        export_dataset = request.data.get("export_dataset", "FINANCIAL_LEDGER")

        result = GranularActionPermissionService.execute_export(
            user=request.user,
            tenant_id=tenant_id,
            export_dataset=export_dataset,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="rx-review")
    def rx_review(self, request) -> Response:
        """POST /api/v2/permissions/granular/rx-review/"""
        tenant_id, _, user_branch = self._resolve_tenant_and_role(request)
        branch_id = request.data.get("branch_id") or user_branch or "main-branch"
        prescription_id = request.data.get("prescription_id", "rx-001")
        clinical_notes = request.data.get(
            "clinical_notes", "Verified dosage, course duration, and patient age."
        )

        result = GranularActionPermissionService.execute_rx_review(
            user=request.user,
            tenant_id=tenant_id,
            branch_id=branch_id,
            prescription_id=prescription_id,
            clinical_notes=clinical_notes,
        )
        return Response(result, status=status.HTTP_200_OK)

