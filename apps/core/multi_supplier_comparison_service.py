"""
backend/apps/core/multi_supplier_comparison_service.py
Architecture Decision: ADR-0195 (P195).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Service layer governing Multi-Supplier Comparison:
- Compares authorized pharmaceutical suppliers by net product cost, package size,
  delivery reliability, nominal/actual lead times, and approved payment terms.
- Calculates deterministic multi-factor composite scores and identifies optimal vendor.
- Creates audited comparison evaluations and converts winning selections into Purchase Orders.
"""

from __future__ import annotations

import datetime
import logging
import math
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Avg, Count, F, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    GoodsReceiptNote,
    MedicineProduct,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    Supplier,
    SupplierComparisonEvaluation,
    SupplierComparisonLine,
    SupplierComparisonStatus,
    Tenant,
    TenantLifecycleStatus,
    VendorProductPriceList,
)

logger = logging.getLogger(__name__)


class MultiSupplierComparisonService:
    """
    Authoritative service for comparing vendors on cost, reliability, lead time, pack size, and terms.
    """

    _ALLOWED_PROCUREMENT_ROLES = {
        "BRANCH_MANAGER",
        "PHARMACY_OWNER",
        "SUPERADMIN",
        "STAFF_PHARMACIST",
        "PROCUREMENT_OFFICER",
        "INVENTORY_CLERK",
        "AUDITOR",
    }

    _ALLOWED_MANAGERIAL_ROLES = {
        "BRANCH_MANAGER",
        "PHARMACY_OWNER",
        "SUPERADMIN",
        "PROCUREMENT_OFFICER",
    }

    @classmethod
    def _assert_procurement_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str == "CASHIER" or role_str not in cls._ALLOWED_PROCUREMENT_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Staff role does not have permission to access supplier comparisons.",
                }
            )

    @classmethod
    def _assert_managerial_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str not in cls._ALLOWED_MANAGERIAL_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Managerial role required to finalize evaluation or place purchase orders.",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant_id: str) -> Tenant:
        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant {tenant_id} not found."})

        if tenant.status == TenantLifecycleStatus.SUSPENDED:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant subscription is suspended.",
                }
            )
        return tenant

    @classmethod
    def calculate_delivery_reliability(cls, tenant: Tenant, supplier: Supplier) -> Decimal:
        """
        Calculates supplier historical on-time delivery percentage (0.00 to 100.00%).
        Based on PO expected delivery date vs GRN receipt date.
        """
        grns = GoodsReceiptNote.objects.filter(
            tenant=tenant,
            supplier=supplier,
            purchase_order__isnull=False,
        ).select_related("purchase_order")

        total_receipts = grns.count()
        if total_receipts == 0:
            # Default to baseline 100.00% if no historical delivery data exists yet
            return Decimal("100.00")

        on_time_count = 0
        for grn in grns:
            po = grn.purchase_order
            if po and po.expected_delivery_date:
                # Receipt on or before expected date is considered on-time
                if grn.receipt_date <= po.expected_delivery_date:
                    on_time_count += 1
            else:
                on_time_count += 1

        score = (Decimal(str(on_time_count)) / Decimal(str(total_receipts))) * Decimal("100.00")
        return score.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @classmethod
    def compare_suppliers_for_product(
        cls,
        tenant_id: str,
        actor_user: Any,
        product_id: str,
        target_quantity: int = 100,
        branch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Compares all active suppliers offering the specified product.
        Evaluates cost, reliability, lead time, pack units, and terms.
        """
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            product = MedicineProduct.objects.get(product_id=product_id)
        except MedicineProduct.DoesNotExist:
            raise NotFound(
                {
                    "error_code": "ERR_PRODUCT_NOT_FOUND",
                    "detail": f"Product with ID {product_id} was not found.",
                }
            )

        target_qty = max(1, target_quantity)

        # 1. Gather all price lists for this product and tenant
        price_entries = VendorProductPriceList.objects.filter(
            tenant=tenant,
            product=product,
            is_active=True,
        ).select_related("supplier")

        # 2. Gather active suppliers for this tenant
        active_suppliers = Supplier.objects.filter(
            tenant=tenant,
            is_active=True,
        )

        supplier_map: Dict[str, Supplier] = {s.supplier_id: s for s in active_suppliers}

        # Build candidate lines
        candidates: List[Dict[str, Any]] = []

        seen_supplier_ids = set()
        for pe in price_entries:
            sup = pe.supplier
            if not sup or not sup.is_active:
                continue
            seen_supplier_ids.add(sup.supplier_id)

            pkg_size = max(1, pe.package_size)
            pkg_cost = pe.package_cost_price
            unit_cost = pe.unit_cost_price
            if unit_cost <= Decimal("0.0000") and pkg_cost > Decimal("0.0000"):
                unit_cost = (pkg_cost / Decimal(str(pkg_size))).quantize(Decimal("0.0001"))

            disc = pe.discount_percentage
            disc_factor = Decimal("1.0000") - (disc / Decimal("100.00"))
            effective_net_cost = (unit_cost * disc_factor).quantize(Decimal("0.0001"))

            rel_score = cls.calculate_delivery_reliability(tenant, sup)
            lead_time = sup.lead_time_days or 2
            terms_days = sup.payment_terms_days or 30

            candidates.append(
                {
                    "supplier": sup,
                    "supplier_id": sup.supplier_id,
                    "supplier_name": sup.name,
                    "package_type": pe.package_type or "BOX",
                    "package_size": pkg_size,
                    "package_cost_price": pkg_cost,
                    "unit_cost_price": unit_cost,
                    "mrp_unit": pe.mrp_unit,
                    "discount_percentage": disc,
                    "effective_net_unit_cost": effective_net_cost,
                    "minimum_order_quantity": pe.minimum_order_quantity,
                    "lead_time_days": lead_time,
                    "payment_terms_days": terms_days,
                    "delivery_reliability_score": rel_score,
                    "proof_of_origin_verified": sup.proof_of_origin_verified,
                    "notes": pe.notes,
                }
            )

        # Also include suppliers who might not have an explicit price list entry but are active suppliers
        for sup_id, sup in supplier_map.items():
            if sup_id in seen_supplier_ids:
                continue

            # Fallback to default product catalog pricing
            base_cost = getattr(product, "cost_unit", Decimal("10.0000"))
            mrp = getattr(product, "mrp_unit", Decimal("12.0000"))
            rel_score = cls.calculate_delivery_reliability(tenant, sup)
            lead_time = sup.lead_time_days or 2
            terms_days = sup.payment_terms_days or 30

            candidates.append(
                {
                    "supplier": sup,
                    "supplier_id": sup.supplier_id,
                    "supplier_name": sup.name,
                    "package_type": "BOX",
                    "package_size": 100,
                    "package_cost_price": (base_cost * Decimal("100")).quantize(Decimal("0.0001")),
                    "unit_cost_price": base_cost,
                    "mrp_unit": mrp,
                    "discount_percentage": Decimal("0.00"),
                    "effective_net_unit_cost": base_cost,
                    "minimum_order_quantity": 10,
                    "lead_time_days": lead_time,
                    "payment_terms_days": terms_days,
                    "delivery_reliability_score": rel_score,
                    "proof_of_origin_verified": sup.proof_of_origin_verified,
                    "notes": "Standard catalog quotation",
                }
            )

        if not candidates:
            # If no suppliers configured in tenant, return empty result structure
            return {
                "product_id": product.product_id,
                "product_name": product.brand_name,
                "target_order_quantity": target_qty,
                "total_suppliers_compared": 0,
                "recommended_supplier_id": None,
                "comparison_lines": [],
            }

        # Calculate composite score for each candidate
        # Weights: Cost: 40%, Reliability: 30%, Lead Time: 20%, Payment Terms: 10%
        costs = [c["effective_net_unit_cost"] for c in candidates]
        min_cost = min(costs) if costs else Decimal("1.0000")

        lts = [c["lead_time_days"] for c in candidates]
        min_lt = min(lts) if lts else 1

        for c in candidates:
            # Cost score (0-40): ratio of min_cost / effective_cost * 40
            eff_c = max(Decimal("0.0001"), c["effective_net_unit_cost"])
            cost_norm = min(Decimal("1.0"), min_cost / eff_c)
            cost_score = (cost_norm * Decimal("40.00")).quantize(Decimal("0.01"))

            # Reliability score (0-30): percentage * 30
            rel_score = ((c["delivery_reliability_score"] / Decimal("100.00")) * Decimal("30.00")).quantize(Decimal("0.01"))

            # Lead time score (0-20): ratio of min_lt / lead_time * 20
            lt = max(1, c["lead_time_days"])
            lt_norm = min(Decimal("1.0"), Decimal(str(min_lt)) / Decimal(str(lt)))
            lt_score = (lt_norm * Decimal("20.00")).quantize(Decimal("0.01"))

            # Payment terms score (0-10): credit window up to 60 days
            terms_cap = min(60, c["payment_terms_days"])
            terms_score = ((Decimal(str(terms_cap)) / Decimal("60.00")) * Decimal("10.00")).quantize(Decimal("0.01"))

            total_score = (cost_score + rel_score + lt_score + terms_score).quantize(Decimal("0.01"))
            c["composite_score"] = min(Decimal("100.00"), max(Decimal("0.00"), total_score))

        # Sort by composite_score desc, then effective_net_unit_cost asc
        candidates.sort(key=lambda x: (-x["composite_score"], x["effective_net_unit_cost"]))

        # Assign rank and recommendation
        recommended_sup_id = None
        for idx, c in enumerate(candidates, start=1):
            c["composite_rank"] = idx
            is_rec = idx == 1
            c["is_recommended"] = is_rec
            if is_rec:
                recommended_sup_id = c["supplier_id"]

        clean_lines = []
        for c in candidates:
            sup_obj = c.get("supplier")
            clean_lines.append({
                "supplier_id": c["supplier_id"],
                "supplier_name": c["supplier_name"],
                "package_type": c["package_type"],
                "package_size": c["package_size"],
                "package_cost_price": str(c["package_cost_price"]),
                "unit_cost_price": str(c["unit_cost_price"]),
                "mrp_unit": str(c["mrp_unit"]),
                "discount_percentage": str(c["discount_percentage"]),
                "effective_net_unit_cost": str(c["effective_net_unit_cost"]),
                "minimum_order_quantity": c["minimum_order_quantity"],
                "lead_time_days": c["lead_time_days"],
                "payment_terms_days": c["payment_terms_days"],
                "delivery_reliability_score": str(c["delivery_reliability_score"]),
                "proof_of_origin_verified": c["proof_of_origin_verified"],
                "composite_rank": c["composite_rank"],
                "composite_score": str(c["composite_score"]),
                "is_recommended": c["is_recommended"],
                "notes": c.get("notes", ""),
                "_supplier_obj": sup_obj,
            })

        return {
            "product_id": product.product_id,
            "product_name": product.brand_name,
            "target_order_quantity": target_qty,
            "total_suppliers_compared": len(clean_lines),
            "recommended_supplier_id": recommended_sup_id,
            "comparison_lines": clean_lines,
        }

    @classmethod
    def serialize_line(cls, line: SupplierComparisonLine) -> Dict[str, Any]:
        return {
            "line_id": line.line_id,
            "supplier_id": line.supplier_id,
            "supplier_name": line.supplier_name,
            "package_type": line.package_type,
            "package_size": line.package_size,
            "package_cost_price": str(line.package_cost_price),
            "unit_cost_price": str(line.unit_cost_price),
            "mrp_unit": str(line.mrp_unit),
            "discount_percentage": str(line.discount_percentage),
            "effective_net_unit_cost": str(line.effective_net_unit_cost),
            "minimum_order_quantity": line.minimum_order_quantity,
            "lead_time_days": line.lead_time_days,
            "payment_terms_days": line.payment_terms_days,
            "delivery_reliability_score": str(line.delivery_reliability_score),
            "proof_of_origin_verified": line.proof_of_origin_verified,
            "composite_rank": line.composite_rank,
            "composite_score": str(line.composite_score),
            "is_recommended": line.is_recommended,
            "notes": line.notes,
        }

    @classmethod
    def serialize_evaluation(cls, ev: SupplierComparisonEvaluation) -> Dict[str, Any]:
        return {
            "evaluation_id": ev.evaluation_id,
            "evaluation_number": ev.evaluation_number,
            "tenant_id": ev.tenant_id,
            "branch_id": ev.branch_id,
            "product_id": ev.product_id,
            "product_name": ev.product.brand_name if ev.product else "",
            "target_order_quantity": ev.target_order_quantity,
            "status": ev.status,
            "selected_supplier_id": ev.selected_supplier_id,
            "selected_supplier_name": ev.selected_supplier.name if ev.selected_supplier else None,
            "selection_reason": ev.selection_reason,
            "evaluated_by": str(getattr(ev.evaluated_by, "user_id", ev.evaluated_by_id or "")) if ev.evaluated_by_id else None,
            "evaluated_at": ev.evaluated_at.isoformat() if ev.evaluated_at else None,
            "created_po_id": ev.created_po_id,
            "notes": ev.notes,
            "is_active": ev.is_active,
            "lines": [cls.serialize_line(l) for l in ev.comparison_lines.all().order_by("composite_rank")],
            "created_at": ev.created_at.isoformat() if ev.created_at else None,
            "updated_at": ev.updated_at.isoformat() if ev.updated_at else None,
        }

    @classmethod
    @transaction.atomic
    def create_comparison_evaluation(
        cls,
        tenant_id: str,
        actor_user: Any,
        product_id: str,
        target_quantity: int = 100,
        branch_id: Optional[str] = None,
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Creates and persists a formal comparison snapshot in the database.
        """
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        target_branch = None
        if branch_id:
            target_branch = tenant.branches.filter(branch_id=branch_id).first()
        if not target_branch:
            target_branch = tenant.branches.first()

        comp_result = cls.compare_suppliers_for_product(
            tenant_id=tenant_id,
            actor_user=actor_user,
            product_id=product_id,
            target_quantity=target_quantity,
            branch_id=branch_id,
        )

        product = MedicineProduct.objects.get(product_id=product_id)

        now = timezone.now()
        eval_id = f"sce-{uuid.uuid4().hex[:12]}"
        eval_num = f"SCE-{now.strftime('%Y%m')}-{uuid.uuid4().hex[:6].upper()}"

        evaluation = SupplierComparisonEvaluation.objects.create(
            evaluation_id=eval_id,
            evaluation_number=eval_num,
            tenant=tenant,
            branch=target_branch,
            product=product,
            target_order_quantity=max(1, target_quantity),
            status=SupplierComparisonStatus.DRAFT,
            evaluated_by=actor_user if hasattr(actor_user, "user_id") else None,
            evaluated_at=now,
            notes=notes,
            is_active=True,
        )

        for line_data in comp_result["comparison_lines"]:
            line_id = f"scl-{uuid.uuid4().hex[:12]}"
            SupplierComparisonLine.objects.create(
                line_id=line_id,
                evaluation=evaluation,
                tenant=tenant,
                supplier=line_data.get("_supplier_obj") or Supplier.objects.get(tenant=tenant, supplier_id=line_data["supplier_id"]),
                supplier_name=line_data["supplier_name"],
                package_type=line_data["package_type"],
                package_size=line_data["package_size"],
                package_cost_price=line_data["package_cost_price"],
                unit_cost_price=line_data["unit_cost_price"],
                mrp_unit=line_data["mrp_unit"],
                discount_percentage=line_data["discount_percentage"],
                effective_net_unit_cost=line_data["effective_net_unit_cost"],
                minimum_order_quantity=line_data["minimum_order_quantity"],
                lead_time_days=line_data["lead_time_days"],
                payment_terms_days=line_data["payment_terms_days"],
                delivery_reliability_score=line_data["delivery_reliability_score"],
                proof_of_origin_verified=line_data["proof_of_origin_verified"],
                composite_rank=line_data["composite_rank"],
                composite_score=line_data["composite_score"],
                is_recommended=line_data["is_recommended"],
                notes=line_data.get("notes", ""),
            )

        return cls.serialize_evaluation(evaluation)

    @classmethod
    @transaction.atomic
    def select_supplier_and_create_po(
        cls,
        tenant_id: str,
        actor_user: Any,
        evaluation_id: str,
        selected_supplier_id: str,
        selection_reason: str,
        order_quantity: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Formally selects a supplier from the comparison and generates a Purchase Order.
        """
        cls._assert_managerial_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            ev = SupplierComparisonEvaluation.objects.select_for_update().get(
                tenant=tenant,
                evaluation_id=evaluation_id,
                is_active=True,
            )
        except SupplierComparisonEvaluation.DoesNotExist:
            raise NotFound({"error_code": "ERR_EVALUATION_NOT_FOUND", "detail": f"Evaluation {evaluation_id} not found."})

        if ev.status == SupplierComparisonStatus.ORDER_PLACED:
            raise ValidationError(
                {
                    "error_code": "ERR_ORDER_ALREADY_PLACED",
                    "detail": f"Purchase order {ev.created_po_id} has already been placed from this evaluation.",
                }
            )

        try:
            line = ev.comparison_lines.get(supplier_id=selected_supplier_id)
        except SupplierComparisonLine.DoesNotExist:
            raise ValidationError(
                {
                    "error_code": "ERR_SUPPLIER_NOT_IN_COMPARISON",
                    "detail": f"Supplier {selected_supplier_id} was not evaluated in this comparison.",
                }
            )

        qty = order_quantity if (order_quantity and order_quantity > 0) else ev.target_order_quantity

        if qty < line.minimum_order_quantity:
            raise ValidationError(
                {
                    "error_code": "ERR_BELOW_MINIMUM_ORDER_QTY",
                    "detail": f"Order quantity {qty} is below supplier MOQ of {line.minimum_order_quantity}.",
                }
            )

        supplier = line.supplier
        now = timezone.now()
        po_id = f"po-{uuid.uuid4().hex[:12]}"
        po_num = f"PO-{now.strftime('%Y%m')}-{uuid.uuid4().hex[:6].upper()}"

        # Calculate packages and prices
        pkg_size = max(1, line.package_size)
        ordered_packages = int(math.ceil(qty / pkg_size))
        actual_total_qty = ordered_packages * pkg_size

        line_subtotal = (line.unit_cost_price * Decimal(str(actual_total_qty))).quantize(Decimal("0.0001"))
        disc_amt = (line_subtotal * (line.discount_percentage / Decimal("100.00"))).quantize(Decimal("0.0001"))
        total_amt = line_subtotal - disc_amt

        expected_delivery = (now + datetime.timedelta(days=line.lead_time_days)).date()

        po = PurchaseOrder.objects.create(
            po_id=po_id,
            po_number=po_num,
            tenant=tenant,
            branch=ev.branch,
            supplier=supplier,
            status=PurchaseOrderStatus.DRAFT,
            payment_terms_days=line.payment_terms_days,
            subtotal_amount=line_subtotal,
            discount_amount=disc_amt,
            tax_amount=Decimal("0.0000"),
            total_amount=total_amt,
            expected_delivery_date=expected_delivery,
            created_by=actor_user if hasattr(actor_user, "user_id") else None,
            notes=f"Generated from Comparison Evaluation {ev.evaluation_number}. Reason: {selection_reason}",
            is_active=True,
        )

        PurchaseOrderItem.objects.create(
            item_id=f"poi-{uuid.uuid4().hex[:12]}",
            purchase_order=po,
            product=ev.product,
            pack_unit=line.package_type,
            package_size=pkg_size,
            ordered_packages=ordered_packages,
            ordered_quantity=actual_total_qty,
            unit_cost_price=line.unit_cost_price,
            package_cost_price=line.package_cost_price,
            mrp_unit=line.mrp_unit,
            discount_percentage=line.discount_percentage,
            tax_percentage=Decimal("0.00"),
            line_subtotal=line_subtotal,
            tax_amount=Decimal("0.0000"),
            line_total=total_amt,
            expected_delivery_date=expected_delivery,
            notes=f"Quotation Rank #{line.composite_rank} (Score {line.composite_score})",
        )

        ev.selected_supplier = supplier
        ev.selection_reason = selection_reason
        ev.status = SupplierComparisonStatus.ORDER_PLACED
        ev.created_po = po
        ev.evaluated_by = actor_user if hasattr(actor_user, "user_id") else None
        ev.evaluated_at = now
        ev.save(update_fields=["selected_supplier", "selection_reason", "status", "created_po", "evaluated_by", "evaluated_at", "updated_at"])

        return {
            "evaluation": cls.serialize_evaluation(ev),
            "purchase_order": {
                "po_id": po.po_id,
                "po_number": po.po_number,
                "supplier_id": supplier.supplier_id,
                "supplier_name": supplier.name,
                "ordered_quantity": actual_total_qty,
                "total_amount": str(po.total_amount),
                "expected_delivery_date": expected_delivery.isoformat(),
            },
        }

    @classmethod
    def get_evaluation(cls, tenant_id: str, actor_user: Any, evaluation_id: str) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            ev = SupplierComparisonEvaluation.objects.get(
                tenant=tenant,
                evaluation_id=evaluation_id,
                is_active=True,
            )
        except SupplierComparisonEvaluation.DoesNotExist:
            raise NotFound({"error_code": "ERR_EVALUATION_NOT_FOUND", "detail": f"Evaluation {evaluation_id} not found."})

        return cls.serialize_evaluation(ev)

    @classmethod
    def list_evaluations(
        cls,
        tenant_id: str,
        actor_user: Any,
        product_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        qs = SupplierComparisonEvaluation.objects.filter(tenant=tenant, is_active=True)
        if product_id:
            qs = qs.filter(product_id=product_id)
        if status:
            qs = qs.filter(status=status)

        return [cls.serialize_evaluation(ev) for ev in qs.order_by("-created_at")[:50]]
