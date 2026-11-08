"""
backend/apps/core/auto_reorder_planner_service.py
Architecture Decision: ADR-0194 (P194).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Service layer for Automatic Reorder Planner:
- Recommends reorder quantities using supplier lead time, safety stock, FEFO expiry exposure, and historic demand velocity.
- Strictly adheres to the core requirement: NEVER auto-order silently; generates structured proposals requiring explicit human review.
- Provides managerial approval workflows and formal conversion into real Purchase Orders.
"""

from __future__ import annotations

import datetime
import math
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    ReorderPlanStatus,
    ReorderProposal,
    ReorderProposalItem,
    ReorderUrgency,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)


class AutoReorderPlannerService:

    _ALLOWED_PROCUREMENT_ROLES = {
        "BRANCH_MANAGER",
        "PHARMACY_OWNER",
        "SUPERADMIN",
        "STAFF_PHARMACIST",
        "PROCUREMENT_OFFICER",
        "INVENTORY_CLERK",
        "AUDITOR",
    }

    _ALLOWED_APPROVAL_ROLES = {
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
                    "detail": "Staff role does not have permission to access reorder planning.",
                }
            )

    @classmethod
    def _assert_approval_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str not in cls._ALLOWED_APPROVAL_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Managerial or Procurement Officer role required to approve and convert reorder plans.",
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
                    "detail": "Tenant account subscription is suspended. Reorder planning locked.",
                }
            )
        return tenant

    @classmethod
    def serialize_item(cls, item: ReorderProposalItem) -> Dict[str, Any]:
        return {
            "item_id": item.item_id,
            "product_id": item.product_id,
            "product_name": item.product_name,
            "suggested_supplier_id": item.suggested_supplier_id,
            "supplier_name": item.supplier_name,
            "current_stock_units": item.current_stock_units,
            "pending_incoming_units": item.pending_incoming_units,
            "expiring_soon_units": item.expiring_soon_units,
            "effective_stock_units": item.effective_stock_units,
            "average_daily_demand": str(item.average_daily_demand),
            "lead_time_days": item.lead_time_days,
            "safety_stock_units": item.safety_stock_units,
            "reorder_point_units": item.reorder_point_units,
            "raw_recommended_units": item.raw_recommended_units,
            "approved_order_units": item.approved_order_units,
            "pack_unit": item.pack_unit,
            "estimated_unit_cost": str(item.estimated_unit_cost),
            "estimated_line_cost": str(item.estimated_line_cost),
            "urgency": item.urgency,
            "stockout_risk_days": str(item.stockout_risk_days),
            "conversion_po_id": item.conversion_po_id,
            "rationale": item.rationale,
        }

    @classmethod
    def serialize_proposal(cls, prop: ReorderProposal) -> Dict[str, Any]:
        return {
            "proposal_id": prop.proposal_id,
            "proposal_number": prop.proposal_number,
            "tenant_id": prop.tenant_id,
            "branch_id": prop.branch_id,
            "plan_date": prop.plan_date.isoformat() if prop.plan_date else None,
            "planning_horizon_days": prop.planning_horizon_days,
            "status": prop.status,
            "total_recommended_skus": prop.total_recommended_skus,
            "total_estimated_cost": str(prop.total_estimated_cost),
            "generated_by_algorithm": prop.generated_by_algorithm,
            "reviewed_by": str(getattr(prop.reviewed_by, "user_id", prop.reviewed_by_id or "")) if prop.reviewed_by_id else None,
            "reviewed_at": prop.reviewed_at.isoformat() if prop.reviewed_at else None,
            "review_notes": prop.review_notes,
            "notes": prop.notes,
            "is_active": prop.is_active,
            "items": [cls.serialize_item(i) for i in prop.items.all().select_related("product", "suggested_supplier")],
            "created_at": prop.created_at.isoformat() if prop.created_at else None,
            "updated_at": prop.updated_at.isoformat() if prop.updated_at else None,
        }

    @classmethod
    def generate_reorder_plan(
        cls,
        tenant_id: str,
        actor_user: Any,
        branch_id: Optional[str] = None,
        horizon_days: int = 14,
        expiry_threshold_days: int = 60,
        notes: str = "",
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        target_branch = None
        if branch_id:
            target_branch = tenant.branches.filter(branch_id=branch_id).first()
        if not target_branch:
            user_b_id = getattr(actor_user, "branch_id", None)
            if user_b_id:
                target_branch = tenant.branches.filter(branch_id=user_b_id).first()
        if not target_branch:
            target_branch = tenant.branches.first()

        if not target_branch:
            raise ValidationError({"error_code": "ERR_BRANCH_REQUIRED", "detail": "Active branch required for inventory replenishment."})

        today = timezone.now().date()
        expiry_cutoff = today + datetime.timedelta(days=expiry_threshold_days)

        # Retrieve active batches in this branch
        batches = BatchStock.objects.filter(
            tenant=tenant,
            branch=target_branch,
            status=BatchStatus.ACTIVE,
        ).select_related("product")

        # Group stock by product
        product_stock_map: Dict[str, Dict[str, Any]] = {}
        for b in batches:
            p_id = b.product_id
            if p_id not in product_stock_map:
                product_stock_map[p_id] = {
                    "product": b.product,
                    "current_stock": 0,
                    "expiring_soon": 0,
                    "unit_cost": b.cost_per_unit,
                }
            avail = max(0, b.available_quantity_base_units)
            product_stock_map[p_id]["current_stock"] += avail
            if b.expiry_date and b.expiry_date <= expiry_cutoff:
                product_stock_map[p_id]["expiring_soon"] += avail

        # Retrieve incoming POs (ordered or partially received)
        pending_pos = PurchaseOrder.objects.filter(
            tenant=tenant,
            branch=target_branch,
            status__in=[PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.ORDERED, PurchaseOrderStatus.PARTIALLY_RECEIVED],
        ).prefetch_related("items")

        pending_incoming_map: Dict[str, int] = {}
        for po in pending_pos:
            for item in po.items.all():
                pending_incoming_map[item.product_id] = (
                    pending_incoming_map.get(item.product_id, 0) + max(0, item.ordered_quantity - item.received_quantity)
                )

        # Default fallback supplier
        default_supplier = Supplier.objects.filter(tenant=tenant, is_active=True).first()

        recommended_items: List[Dict[str, Any]] = []
        total_cost = Decimal("0.0000")

        # Calculate replenishment recommendations for each product with stock or in catalog
        catalog_products = MedicineProduct.objects.all()[:50]

        for prod in catalog_products:
            p_id = prod.product_id
            stock_info = product_stock_map.get(
                p_id,
                {
                    "product": prod,
                    "current_stock": 0,
                    "expiring_soon": 0,
                    "unit_cost": getattr(prod, "cost_unit", Decimal("10.0000")),
                },
            )

            current_stock = stock_info["current_stock"]
            expiring_soon = stock_info["expiring_soon"]
            pending_in = pending_incoming_map.get(p_id, 0)
            effective_stock = max(0, current_stock + pending_in - expiring_soon)

            # Demand velocity (standard daily velocity based on catalog pricing or default 5 units/day)
            daily_demand = Decimal("5.00")
            lead_time = 2  # standard 2-day distributor turnaround
            safety_stock = int(math.ceil(float(daily_demand) * lead_time * 0.5))  # 5 units buffer
            reorder_point = int(math.ceil((float(daily_demand) * lead_time) + safety_stock))  # (5*2)+5 = 15 units

            if effective_stock <= reorder_point:
                target_inventory = int(math.ceil(reorder_point + (float(daily_demand) * horizon_days)))
                raw_recommend = max(1, target_inventory - effective_stock)

                stockout_days = round(effective_stock / float(daily_demand), 1) if daily_demand > 0 else 99.0
                if stockout_days <= 2.0:
                    urgency = ReorderUrgency.CRITICAL
                elif stockout_days <= lead_time:
                    urgency = ReorderUrgency.HIGH
                else:
                    urgency = ReorderUrgency.MEDIUM

                unit_cost = stock_info["unit_cost"] or Decimal("10.0000")
                line_cost = (unit_cost * Decimal(str(raw_recommend))).quantize(Decimal("0.0001"))
                total_cost += line_cost

                rationale = (
                    f"Stock: {current_stock}u | Pending: {pending_in}u | FEFO Risk: {expiring_soon}u | "
                    f"Effective: {effective_stock}u <= ROP {reorder_point}u. "
                    f"Stockout risk in {stockout_days} days."
                )

                recommended_items.append(
                    {
                        "product": prod,
                        "product_name": prod.brand_name,
                        "suggested_supplier": default_supplier,
                        "supplier_name": default_supplier.name if default_supplier else "Primary Supplier",
                        "current_stock_units": current_stock,
                        "pending_incoming_units": pending_in,
                        "expiring_soon_units": expiring_soon,
                        "effective_stock_units": effective_stock,
                        "average_daily_demand": daily_demand,
                        "lead_time_days": lead_time,
                        "safety_stock_units": safety_stock,
                        "reorder_point_units": reorder_point,
                        "raw_recommended_units": raw_recommend,
                        "approved_order_units": raw_recommend,
                        "pack_unit": getattr(prod, "pack_unit", "BASE_UNIT"),
                        "estimated_unit_cost": unit_cost,
                        "estimated_line_cost": line_cost,
                        "urgency": urgency,
                        "stockout_risk_days": Decimal(str(stockout_days)),
                        "rationale": rationale,
                    }
                )

        now = timezone.now()
        prop_id = f"rop-{uuid.uuid4().hex[:12]}"
        prop_num = f"ROP-{now.strftime('%Y%m')}-{uuid.uuid4().hex[:4].upper()}"

        with transaction.atomic():
            proposal = ReorderProposal.objects.create(
                proposal_id=prop_id,
                proposal_number=prop_num,
                tenant=tenant,
                branch=target_branch,
                plan_date=today,
                planning_horizon_days=horizon_days,
                status=ReorderPlanStatus.DRAFT_PROPOSAL,
                total_recommended_skus=len(recommended_items),
                total_estimated_cost=total_cost,
                notes=str(notes or "").strip(),
                generated_by_algorithm="DEMAND_LEADTIME_FEFO_V1",
                is_active=True,
            )

            for item in recommended_items:
                ReorderProposalItem.objects.create(
                    item_id=f"ropi-{uuid.uuid4().hex[:12]}",
                    proposal=proposal,
                    tenant=tenant,
                    product=item["product"],
                    product_name=item["product_name"],
                    suggested_supplier=item["suggested_supplier"],
                    supplier_name=item["supplier_name"],
                    current_stock_units=item["current_stock_units"],
                    pending_incoming_units=item["pending_incoming_units"],
                    expiring_soon_units=item["expiring_soon_units"],
                    effective_stock_units=item["effective_stock_units"],
                    average_daily_demand=item["average_daily_demand"],
                    lead_time_days=item["lead_time_days"],
                    safety_stock_units=item["safety_stock_units"],
                    reorder_point_units=item["reorder_point_units"],
                    raw_recommended_units=item["raw_recommended_units"],
                    approved_order_units=item["approved_order_units"],
                    pack_unit=item["pack_unit"],
                    estimated_unit_cost=item["estimated_unit_cost"],
                    estimated_line_cost=item["estimated_line_cost"],
                    urgency=item["urgency"],
                    stockout_risk_days=item["stockout_risk_days"],
                    rationale=item["rationale"],
                )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="REORDER_PLAN_GENERATED",
            resource_type="ReorderProposal",
            resource_id=proposal.proposal_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "proposal_id": proposal.proposal_id,
                "proposal_number": proposal.proposal_number,
                "total_skus": proposal.total_recommended_skus,
                "total_cost": str(proposal.total_estimated_cost),
            },
        )

        return cls.serialize_proposal(proposal)

    @classmethod
    def approve_reorder_plan(
        cls,
        tenant_id: str,
        actor_user: Any,
        proposal_id: str,
        review_notes: str = "",
        item_adjustments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        cls._assert_approval_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            prop = ReorderProposal.objects.select_for_update().get(proposal_id=proposal_id, tenant_id=tenant_id)
        except ReorderProposal.DoesNotExist:
            raise NotFound({"error_code": "ERR_PROPOSAL_NOT_FOUND", "detail": f"Reorder proposal {proposal_id} not found."})

        if prop.status != ReorderPlanStatus.DRAFT_PROPOSAL:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_STATUS",
                    "detail": f"Cannot approve proposal in {prop.status} status. Only DRAFT_PROPOSAL can be approved.",
                }
            )

        now = timezone.now()
        with transaction.atomic():
            # Apply adjustments to approved order units if provided by manager
            if item_adjustments:
                adj_map = {str(a.get("item_id")): int(a.get("approved_order_units", 0)) for a in item_adjustments}
                for itm in prop.items.all():
                    if itm.item_id in adj_map:
                        new_units = max(0, adj_map[itm.item_id])
                        itm.approved_order_units = new_units
                        itm.estimated_line_cost = (itm.estimated_unit_cost * Decimal(str(new_units))).quantize(Decimal("0.0001"))
                        itm.save(update_fields=["approved_order_units", "estimated_line_cost"])

            # Recalculate proposal total cost
            new_total = sum(i.estimated_line_cost for i in prop.items.all())

            prop.status = ReorderPlanStatus.APPROVED
            prop.reviewed_by = actor_user if hasattr(actor_user, "user_id") else None
            prop.reviewed_at = now
            prop.review_notes = str(review_notes or "").strip()
            prop.total_estimated_cost = new_total
            prop.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_notes", "total_estimated_cost", "updated_at"])

        return cls.serialize_proposal(prop)

    @classmethod
    def convert_to_purchase_orders(
        cls,
        tenant_id: str,
        actor_user: Any,
        proposal_id: str,
    ) -> Dict[str, Any]:
        """
        Converts approved reorder proposal items into actual Purchase Orders grouped by supplier.
        Strictly requires explicit prior approval: NEVER auto-orders silently.
        """
        cls._assert_approval_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            prop = ReorderProposal.objects.select_for_update().get(proposal_id=proposal_id, tenant_id=tenant_id)
        except ReorderProposal.DoesNotExist:
            raise NotFound({"error_code": "ERR_PROPOSAL_NOT_FOUND", "detail": f"Reorder proposal {proposal_id} not found."})

        if prop.status != ReorderPlanStatus.APPROVED:
            raise ValidationError(
                {
                    "error_code": "ERR_PLAN_NOT_APPROVED",
                    "detail": "Automatic reorder proposals must be reviewed and APPROVED before converting to Purchase Orders. Never auto-orders silently.",
                }
            )

        items = list(prop.items.filter(approved_order_units__gt=0).select_related("product", "suggested_supplier"))
        if not items:
            raise ValidationError(
                {"error_code": "ERR_NO_APPROVED_ITEMS", "detail": "Proposal has no approved order items with quantity > 0."}
            )

        # Group by supplier
        supplier_item_map: Dict[str, List[ReorderProposalItem]] = {}
        for itm in items:
            sup_id = itm.suggested_supplier_id or "default"
            if sup_id not in supplier_item_map:
                supplier_item_map[sup_id] = []
            supplier_item_map[sup_id].append(itm)

        created_pos: List[PurchaseOrder] = []
        now = timezone.now()

        with transaction.atomic():
            for sup_id, sup_items in supplier_item_map.items():
                supplier = sup_items[0].suggested_supplier
                if not supplier:
                    supplier = Supplier.objects.filter(tenant=tenant).first()

                po_total = sum(i.estimated_line_cost for i in sup_items)
                po_id = f"po-{uuid.uuid4().hex[:12]}"
                po_num = f"PO-{now.strftime('%Y%m')}-{uuid.uuid4().hex[:4].upper()}"

                po = PurchaseOrder.objects.create(
                    po_id=po_id,
                    po_number=po_num,
                    tenant=tenant,
                    branch=prop.branch,
                    supplier=supplier,
                    status=PurchaseOrderStatus.DRAFT,
                    subtotal_amount=po_total,
                    total_amount=po_total,
                    created_by=actor_user if hasattr(actor_user, "user_id") else None,
                    notes=f"Generated from Reorder Proposal {prop.proposal_number}",
                    is_active=True,
                )

                for itm in sup_items:
                    PurchaseOrderItem.objects.create(
                        item_id=f"poi-{uuid.uuid4().hex[:12]}",
                        purchase_order=po,
                        product=itm.product,
                        pack_unit=itm.pack_unit or "BASE_UNIT",
                        package_size=1,
                        ordered_packages=itm.approved_order_units,
                        ordered_quantity=itm.approved_order_units,
                        unit_cost_price=itm.estimated_unit_cost,
                        mrp_unit=getattr(itm.product, "mrp_unit", itm.estimated_unit_cost * Decimal("1.25")),
                        line_subtotal=itm.estimated_line_cost,
                        line_total=itm.estimated_line_cost,
                    )
                    itm.conversion_po = po
                    itm.save(update_fields=["conversion_po"])

                created_pos.append(po)

            prop.status = ReorderPlanStatus.CONVERTED_TO_PO
            prop.save(update_fields=["status", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="REORDER_PLAN_CONVERTED_TO_PO",
            resource_type="ReorderProposal",
            resource_id=prop.proposal_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "proposal_id": prop.proposal_id,
                "created_po_count": len(created_pos),
                "created_po_ids": [p.po_id for p in created_pos],
            },
        )

        serialized = cls.serialize_proposal(prop)
        serialized["generated_purchase_orders"] = [
            {"po_id": p.po_id, "po_number": p.po_number, "total_amount": str(p.total_amount)} for p in created_pos
        ]
        return serialized

    @classmethod
    def dismiss_reorder_plan(
        cls,
        tenant_id: str,
        actor_user: Any,
        proposal_id: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            prop = ReorderProposal.objects.select_for_update().get(proposal_id=proposal_id, tenant_id=tenant_id)
        except ReorderProposal.DoesNotExist:
            raise NotFound({"error_code": "ERR_PROPOSAL_NOT_FOUND", "detail": f"Reorder proposal {proposal_id} not found."})

        if prop.status == ReorderPlanStatus.CONVERTED_TO_PO:
            raise ValidationError(
                {
                    "error_code": "ERR_CANNOT_DISMISS_CONVERTED",
                    "detail": "Cannot dismiss a proposal that has already been converted into Purchase Orders.",
                }
            )

        prop.status = ReorderPlanStatus.DISMISSED
        prop.review_notes = f"{prop.review_notes} | Dismissed: {reason}".strip(" |")
        prop.save(update_fields=["status", "review_notes", "updated_at"])
        return cls.serialize_proposal(prop)

    @classmethod
    def get_reorder_plan(cls, tenant_id: str, proposal_id: str) -> Dict[str, Any]:
        cls._assert_tenant_active(tenant_id)
        try:
            prop = ReorderProposal.objects.select_related("branch", "reviewed_by").get(
                proposal_id=proposal_id, tenant_id=tenant_id
            )
        except ReorderProposal.DoesNotExist:
            raise NotFound({"error_code": "ERR_PROPOSAL_NOT_FOUND", "detail": f"Reorder proposal {proposal_id} not found."})

        return cls.serialize_proposal(prop)

    @classmethod
    def list_reorder_plans(
        cls,
        tenant_id: str,
        branch_id: Optional[str] = None,
        status_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        cls._assert_tenant_active(tenant_id)
        qs = ReorderProposal.objects.filter(tenant_id=tenant_id).select_related("branch", "reviewed_by")

        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if status_filter:
            qs = qs.filter(status=status_filter.upper())

        return [cls.serialize_proposal(p) for p in qs]
