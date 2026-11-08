"""
Supplier Operations Dashboard Service (ADR-0199 / P199)
Compiles unified operations metrics: open orders, overdue deliveries,
accounts payable due balances, and low-stock sourcing opportunities.
"""

from decimal import Decimal
import datetime
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional
import uuid

from django.db.models import Sum, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.identity.models import PharmacyUser
from apps.core.models import (
    BatchStock,
    Branch,
    BranchStockAvailability,
    MedicineProduct,
    PaymentScheduleStatus,
    ProcurementShipment,
    PurchaseOrder,
    PurchaseOrderStatus,
    ShipmentStage,
    Supplier,
    SupplierDashboardSnapshot,
    SupplierPaymentSchedule,
    Tenant,
    TenantLifecycleStatus,
    VendorProductPriceList,
)

logger = logging.getLogger(__name__)


class SupplierDashboardService:
    """
    Authoritative service orchestrating supplier operations monitoring,
    due calculations, delivery tracking, and replenishment sourcing.
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

    _OPEN_PO_STATUSES = [
        PurchaseOrderStatus.DRAFT,
        PurchaseOrderStatus.APPROVED,
        PurchaseOrderStatus.ORDERED,
        PurchaseOrderStatus.PARTIALLY_RECEIVED,
    ]

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
                    "detail": "Staff role does not have permission to view supplier operations dashboard.",
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
    def get_dashboard_summary(
        cls,
        tenant_id: str,
        actor_user: Any,
        branch_id: Optional[str] = None,
        supplier_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        cls._assert_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)
        today = timezone.now().date()
        now = timezone.now()

        # ---------------------------------------------------------------------
        # 1. Open Purchase Orders
        # ---------------------------------------------------------------------
        po_qs = PurchaseOrder.objects.filter(
            tenant=tenant,
            status__in=cls._OPEN_PO_STATUSES,
        )
        if branch_id:
            po_qs = po_qs.filter(branch_id=branch_id)
        if supplier_id:
            po_qs = po_qs.filter(supplier__supplier_id=supplier_id)

        open_orders_count = po_qs.count()
        open_orders_val = po_qs.aggregate(tot=Sum("total_amount"))["tot"] or Decimal("0.0000")

        open_orders_list = []
        for po in po_qs.order_by("-ordered_at", "-created_at")[:25]:
            open_orders_list.append(
                {
                    "po_id": po.po_id,
                    "po_number": po.po_number,
                    "supplier_id": po.supplier_id,
                    "supplier_name": po.supplier.name if po.supplier else "",
                    "branch_id": po.branch_id,
                    "branch_name": po.branch.name if po.branch else "",
                    "status": po.status,
                    "order_date": po.ordered_at.strftime("%Y-%m-%d") if po.ordered_at else str(po.created_at.date()),
                    "expected_delivery_date": str(po.expected_delivery_date) if po.expected_delivery_date else "",
                    "total_amount": str(po.total_amount),
                }
            )

        # ---------------------------------------------------------------------
        # 2. Overdue Deliveries
        # ---------------------------------------------------------------------
        overdue_qs = po_qs.filter(
            expected_delivery_date__isnull=False,
            expected_delivery_date__lt=today,
        )
        overdue_count = overdue_qs.count()
        overdue_val = overdue_qs.aggregate(tot=Sum("total_amount"))["tot"] or Decimal("0.0000")

        overdue_deliveries_list = []
        for po in overdue_qs.order_by("expected_delivery_date")[:25]:
            days_late = (today - po.expected_delivery_date).days if po.expected_delivery_date else 0
            overdue_deliveries_list.append(
                {
                    "po_id": po.po_id,
                    "po_number": po.po_number,
                    "supplier_name": po.supplier.name if po.supplier else "",
                    "expected_delivery_date": str(po.expected_delivery_date),
                    "days_overdue": max(0, days_late),
                    "total_amount": str(po.total_amount),
                    "status": po.status,
                }
            )

        # In-transit Shipment Delays
        shipment_overdue_qs = ProcurementShipment.objects.filter(
            tenant=tenant,
            stage__in=[ShipmentStage.DISPATCHED, ShipmentStage.IN_TRANSIT, ShipmentStage.OUT_FOR_DELIVERY],
            estimated_delivery_at__lt=now,
        )
        if branch_id:
            shipment_overdue_qs = shipment_overdue_qs.filter(branch_id=branch_id)
        if supplier_id:
            shipment_overdue_qs = shipment_overdue_qs.filter(supplier__supplier_id=supplier_id)

        in_transit_delayed_count = shipment_overdue_qs.count()

        # ---------------------------------------------------------------------
        # 3. Due Balances & Accounts Payable (AP)
        # ---------------------------------------------------------------------
        sup_qs = Supplier.objects.filter(tenant=tenant)
        if supplier_id:
            sup_qs = sup_qs.filter(supplier_id=supplier_id)

        total_due_balance = sup_qs.aggregate(tot=Sum("current_balance"))["tot"] or Decimal("0.0000")

        # Check Payment Schedules
        sched_qs = SupplierPaymentSchedule.objects.filter(
            tenant=tenant,
            status__in=[
                PaymentScheduleStatus.SCHEDULED,
                PaymentScheduleStatus.PARTIALLY_SETTLED,
                PaymentScheduleStatus.OVERDUE,
            ],
        )
        if branch_id:
            sched_qs = sched_qs.filter(branch_id=branch_id)
        if supplier_id:
            sched_qs = sched_qs.filter(supplier__supplier_id=supplier_id)

        overdue_scheds = sched_qs.filter(due_date__lt=today)
        overdue_due_balance = overdue_scheds.aggregate(tot=Sum("remaining_amount"))["tot"] or Decimal("0.0000")

        supplier_dues_list = []
        for s in sup_qs.filter(current_balance__gt=0).order_by("-current_balance")[:25]:
            # Oldest unpaid schedule
            oldest = sched_qs.filter(supplier=s).order_by("due_date").first()
            supplier_dues_list.append(
                {
                    "supplier_id": s.supplier_id,
                    "supplier_name": s.name,
                    "contact_phone": s.phone,
                    "payment_terms_days": s.payment_terms_days,
                    "current_balance": str(s.current_balance),
                    "oldest_due_date": str(oldest.due_date) if oldest else "",
                    "is_overdue": oldest.due_date < today if (oldest and oldest.due_date) else False,
                }
            )

        # ---------------------------------------------------------------------
        # 4. Low-Stock Sourcing Opportunities
        # ---------------------------------------------------------------------
        low_stock_candidates = []
        estimated_reorder_cost = Decimal("0.0000")

        # First check BranchStockAvailability
        avail_qs = BranchStockAvailability.objects.filter(
            Q(is_below_reorder_level=True) | Q(is_out_of_stock=True),
            tenant=tenant,
        )
        if branch_id:
            avail_qs = avail_qs.filter(branch_id=branch_id)

        seen_product_ids = set()
        for av in avail_qs.select_related("product")[:30]:
            prod = av.product
            seen_product_ids.add(prod.product_id)
            current_avail = av.quantity_server_confirmed_available
            target_reorder = av.reorder_level_base_units or 100
            order_qty = max(50, (target_reorder * 2) - current_avail)

            # Match Preferred Supplier from VendorProductPriceList
            price_entry = VendorProductPriceList.objects.filter(
                tenant=tenant,
                product=prod,
                is_active=True,
            ).order_by("unit_cost_price").first()

            pref_sup_id = price_entry.supplier_id if price_entry else ""
            pref_sup_name = price_entry.supplier.name if price_entry else ""
            unit_cost = price_entry.unit_cost_price if price_entry else prod.cost_unit
            est_cost = Decimal(str(order_qty)) * unit_cost
            estimated_reorder_cost += est_cost

            low_stock_candidates.append(
                {
                    "product_id": prod.product_id,
                    "brand_name": prod.brand_name,
                    "generic_name": prod.generic_name,
                    "dosage_form": prod.dosage_form,
                    "current_stock": current_avail,
                    "reorder_threshold": target_reorder,
                    "recommended_reorder_qty": order_qty,
                    "preferred_supplier_id": pref_sup_id,
                    "preferred_supplier_name": pref_sup_name,
                    "quoted_unit_cost": str(unit_cost),
                    "estimated_total_cost": str(est_cost),
                    "is_out_of_stock": av.is_out_of_stock,
                }
            )

        # Fallback check on BatchStock if availabilities not populated
        if not low_stock_candidates:
            batches = BatchStock.objects.filter(tenant=tenant)
            if branch_id:
                batches = batches.filter(branch_id=branch_id)

            prod_stock = {}
            for b in batches:
                prod_stock[b.product_id] = prod_stock.get(b.product_id, 0) + b.available_quantity

            for p_id, total_qty in prod_stock.items():
                if total_qty < 30 and p_id not in seen_product_ids:
                    prod = MedicineProduct.objects.filter(product_id=p_id).first()
                    if not prod:
                        continue
                    order_qty = 100 - total_qty
                    unit_cost = prod.cost_unit or Decimal("2.5000")
                    est_cost = Decimal(str(order_qty)) * unit_cost
                    estimated_reorder_cost += est_cost

                    price_entry = VendorProductPriceList.objects.filter(
                        tenant=tenant, product=prod
                    ).first()

                    low_stock_candidates.append(
                        {
                            "product_id": prod.product_id,
                            "brand_name": prod.brand_name,
                            "generic_name": prod.generic_name,
                            "dosage_form": prod.dosage_form,
                            "current_stock": total_qty,
                            "reorder_threshold": 30,
                            "recommended_reorder_qty": order_qty,
                            "preferred_supplier_id": price_entry.supplier_id if price_entry else "",
                            "preferred_supplier_name": price_entry.supplier.name if price_entry else "",
                            "quoted_unit_cost": str(unit_cost),
                            "estimated_total_cost": str(est_cost),
                            "is_out_of_stock": total_qty == 0,
                        }
                    )

        # ---------------------------------------------------------------------
        # 5. Performance KPIs
        # ---------------------------------------------------------------------
        active_sup_count = Supplier.objects.filter(tenant=tenant, is_active=True).count()
        total_pos_count = PurchaseOrder.objects.filter(tenant=tenant).count()

        summary = {
            "tenant_id": tenant_id,
            "branch_id": branch_id or "",
            "snapshot_date": str(today),
            "open_orders": {
                "count": open_orders_count,
                "total_value_bdt": str(open_orders_val),
                "items": open_orders_list,
            },
            "overdue_deliveries": {
                "count": overdue_count,
                "total_delayed_value_bdt": str(overdue_val),
                "in_transit_delayed_count": in_transit_delayed_count,
                "items": overdue_deliveries_list,
            },
            "due_balances": {
                "total_due_balance_bdt": str(total_due_balance),
                "overdue_due_balance_bdt": str(overdue_due_balance),
                "supplier_dues": supplier_dues_list,
            },
            "low_stock_sourcing": {
                "count": len(low_stock_candidates),
                "estimated_reorder_cost_bdt": str(estimated_reorder_cost),
                "candidates": low_stock_candidates,
            },
            "performance_kpis": {
                "active_suppliers_count": active_sup_count,
                "total_purchase_orders_all_time": total_pos_count,
            },
        }
        return summary

    @classmethod
    def create_dashboard_snapshot(
        cls,
        tenant_id: str,
        actor_user: Any,
        branch_id: Optional[str] = None,
        notes: str = "",
    ) -> SupplierDashboardSnapshot:
        cls._assert_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)
        summary = cls.get_dashboard_summary(tenant_id, actor_user, branch_id=branch_id)

        snap_id = f"sdash-{uuid.uuid4().hex[:12]}"
        resolved_branch = None
        if branch_id:
            resolved_branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()

        snapshot = SupplierDashboardSnapshot(
            snapshot_id=snap_id,
            tenant=tenant,
            branch=resolved_branch,
            snapshot_date=timezone.now().date(),
            open_orders_count=summary["open_orders"]["count"],
            open_orders_value=Decimal(summary["open_orders"]["total_value_bdt"]),
            overdue_deliveries_count=summary["overdue_deliveries"]["count"],
            overdue_deliveries_value=Decimal(summary["overdue_deliveries"]["total_delayed_value_bdt"]),
            total_due_balance=Decimal(summary["due_balances"]["total_due_balance_bdt"]),
            overdue_due_balance=Decimal(summary["due_balances"]["overdue_due_balance_bdt"]),
            low_stock_opportunities_count=summary["low_stock_sourcing"]["count"],
            estimated_reorder_cost=Decimal(summary["low_stock_sourcing"]["estimated_reorder_cost_bdt"]),
            active_suppliers_count=summary["performance_kpis"]["active_suppliers_count"],
            dashboard_metrics=summary,
            notes=notes,
            generated_by=actor_user if isinstance(actor_user, PharmacyUser) else None,
            generated_at=timezone.now(),
        )
        snapshot.immutable_verification_hash = snapshot.compute_hash()
        snapshot.save()

        logger.info(
            f"[SupplierDashboardService] Created snapshot {snapshot.snapshot_id} for tenant {tenant_id} (hash: {snapshot.immutable_verification_hash[:8]})."
        )
        return snapshot

    @classmethod
    def list_snapshots(
        cls,
        tenant_id: str,
        actor_user: Any,
        branch_id: Optional[str] = None,
    ) -> List[SupplierDashboardSnapshot]:
        cls._assert_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        qs = SupplierDashboardSnapshot.objects.filter(tenant=tenant)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return list(qs.order_by("-generated_at")[:50])

    @classmethod
    def get_snapshot(
        cls,
        tenant_id: str,
        actor_user: Any,
        snapshot_id: str,
    ) -> SupplierDashboardSnapshot:
        cls._assert_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        snap = SupplierDashboardSnapshot.objects.filter(tenant=tenant, snapshot_id=snapshot_id).first()
        if not snap:
            raise NotFound(
                {
                    "error_code": "ERR_SNAPSHOT_NOT_FOUND",
                    "detail": f"Supplier dashboard snapshot '{snapshot_id}' not found.",
                }
            )
        return snap
