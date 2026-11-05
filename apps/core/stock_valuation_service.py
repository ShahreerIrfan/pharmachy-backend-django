"""
backend/apps/core/stock_valuation_service.py
Architecture Decision: ADR-0179 (P179).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Service calculating authoritative inventory stock valuation, FIFO actual lot cost,
weighted average batch cost, 5-tier aging analysis, potential gross margin,
and realized margin from immutable lot allocations.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    SaleLotAllocation,
    SaleLotAllocationStatus,
    StockCostingMethod,
    StockValuationSnapshot,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

ALLOWED_VALUATION_ROLES = {
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    UserRole.STAFF_PHARMACIST,
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "STAFF_PHARMACIST",
    "OWNER_ADMIN",
    "ADMIN",
    "AUDITOR",
    "CHIEF_PHARMACIST",
}


class StockValuationService:
    """
    Authoritative calculation engine for inventory valuation, aging, and margin analysis.
    """

    @classmethod
    def _assert_manage_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in ALLOWED_VALUATION_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Stock valuation and margin analytics require managerial, owner, or audit authorization.",
                    "error_code": "ERR_PERMISSION_DENIED",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant: Tenant) -> None:
        if (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {
                    "detail": "Tenant subscription is suspended. Valuation services are locked.",
                    "error_code": "ERR_TENANT_SUSPENDED",
                }
            )

    @classmethod
    def calculate_valuation(
        cls,
        tenant_id: str,
        branch_id: Optional[str] = None,
        product_id: Optional[str] = None,
        costing_method: str = StockCostingMethod.FIFO_ACTUAL,
        as_of_date: Optional[datetime.date] = None,
        start_date: Optional[datetime.date] = None,
        end_date: Optional[datetime.date] = None,
        user: Any = None,
    ) -> Dict[str, Any]:
        """
        Pure calculation engine deriving total inventory valuation, 5-tier aging,
        potential gross margin, and realized margin from immutable lot allocations.
        """
        if user:
            cls._assert_manage_role(user)

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": f"Tenant '{tenant_id}' not found.", "error_code": "ERR_TENANT_NOT_FOUND"})

        cls._assert_tenant_active(tenant)

        # Cross-tenant guard
        if user:
            user_tenant_id = getattr(user, "tenant_id", None)
            if (
                user_tenant_id
                and str(user_tenant_id) != str(tenant_id)
                and getattr(user, "role", None) != UserRole.SUPERADMIN
            ):
                raise PermissionDenied(
                    {
                        "detail": "Cross-tenant valuation access is strictly prohibited.",
                        "error_code": "ERR_CROSS_TENANT_ACCESS",
                    }
                )

        branch = None
        if branch_id:
            branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
            if not branch:
                raise NotFound(
                    {"detail": f"Branch '{branch_id}' not found for tenant.", "error_code": "ERR_BRANCH_NOT_FOUND"}
                )

        # Validate costing method
        if costing_method not in (StockCostingMethod.FIFO_ACTUAL, StockCostingMethod.WEIGHTED_AVERAGE):
            raise ValidationError(
                {
                    "detail": f"Invalid costing method '{costing_method}'. Must be FIFO_ACTUAL or WEIGHTED_AVERAGE.",
                    "error_code": "ERR_INVALID_VALUATION_INPUT",
                }
            )

        # Validate dates
        if not as_of_date:
            as_of_date = timezone.now().date()

        if start_date and end_date and start_date > end_date:
            raise ValidationError(
                {
                    "detail": "start_date cannot be later than end_date.",
                    "error_code": "ERR_INVALID_VALUATION_INPUT",
                }
            )

        # 1. Gather Stock & Compute Valuation
        product_map: Dict[str, Dict[str, Any]] = {}
        total_quantity_base_units = 0
        total_cost_valuation = Decimal("0.0000")
        total_retail_valuation = Decimal("0.0000")

        # Aging Buckets: (0-30, 31-60, 61-90, 91-180, 181+)
        aging_buckets = {
            "days_0_to_30": {"label": "0 - 30 Days (Fresh)", "quantity_base_units": 0, "cost_valuation": Decimal("0.0000")},
            "days_31_to_60": {"label": "31 - 60 Days (Normal)", "quantity_base_units": 0, "cost_valuation": Decimal("0.0000")},
            "days_61_to_90": {"label": "61 - 90 Days (Moderate)", "quantity_base_units": 0, "cost_valuation": Decimal("0.0000")},
            "days_91_to_180": {"label": "91 - 180 Days (Slow-Moving)", "quantity_base_units": 0, "cost_valuation": Decimal("0.0000")},
            "days_181_plus": {"label": "181+ Days (Aged / Dormant)", "quantity_base_units": 0, "cost_valuation": Decimal("0.0000")},
        }

        if costing_method == StockCostingMethod.FIFO_ACTUAL:
            # Derive from immutable PurchaseCostLots
            lot_qs = PurchaseCostLot.objects.select_related("product", "batch").filter(
                tenant=tenant,
                status=PurchaseCostLotStatus.ACTIVE,
                remaining_quantity__gt=0,
                purchase_date__lte=as_of_date,
            )
            if branch:
                lot_qs = lot_qs.filter(branch=branch)
            if product_id:
                lot_qs = lot_qs.filter(product_id=product_id)

            for lot in lot_qs:
                qty = lot.remaining_quantity
                cost = Decimal(str(lot.unit_cost))
                cost_subtotal = Decimal(str(qty)) * cost
                prod = lot.product
                mrp = Decimal(str(getattr(prod, "effective_mrp_unit", None) or prod.mrp_unit or "0.0000"))
                retail_subtotal = Decimal(str(qty)) * mrp

                total_quantity_base_units += qty
                total_cost_valuation += cost_subtotal
                total_retail_valuation += retail_subtotal

                # Aging calculation
                age_days = (as_of_date - lot.purchase_date).days if lot.purchase_date else 0
                if age_days <= 30:
                    b_key = "days_0_to_30"
                elif age_days <= 60:
                    b_key = "days_31_to_60"
                elif age_days <= 90:
                    b_key = "days_61_to_90"
                elif age_days <= 180:
                    b_key = "days_91_to_180"
                else:
                    b_key = "days_181_plus"

                aging_buckets[b_key]["quantity_base_units"] += qty
                aging_buckets[b_key]["cost_valuation"] += cost_subtotal

                # Product aggregation
                pid = prod.product_id
                if pid not in product_map:
                    product_map[pid] = {
                        "product_id": pid,
                        "brand_name": prod.brand_name,
                        "generic_name": prod.generic_name,
                        "dosage_form": prod.dosage_form,
                        "quantity_base_units": 0,
                        "cost_valuation": Decimal("0.0000"),
                        "retail_valuation": Decimal("0.0000"),
                        "mrp_unit": mrp,
                    }
                product_map[pid]["quantity_base_units"] += qty
                product_map[pid]["cost_valuation"] += cost_subtotal
                product_map[pid]["retail_valuation"] += retail_subtotal

        else:
            # Weighted average cost derived from active BatchStock records
            batch_qs = BatchStock.objects.select_related("product").filter(
                tenant=tenant,
                status=BatchStatus.ACTIVE,
                quantity_base_units__gt=0,
                purchase_date__lte=as_of_date,
            )
            if branch:
                batch_qs = batch_qs.filter(branch=branch)
            if product_id:
                batch_qs = batch_qs.filter(product_id=product_id)

            for batch in batch_qs:
                qty = batch.quantity_base_units
                cost = Decimal(str(batch.cost_per_unit))
                cost_subtotal = Decimal(str(qty)) * cost
                prod = batch.product
                mrp = Decimal(str(getattr(prod, "effective_mrp_unit", None) or prod.mrp_unit or "0.0000"))
                retail_subtotal = Decimal(str(qty)) * mrp

                total_quantity_base_units += qty
                total_cost_valuation += cost_subtotal
                total_retail_valuation += retail_subtotal

                age_days = (as_of_date - batch.purchase_date).days if batch.purchase_date else 0
                if age_days <= 30:
                    b_key = "days_0_to_30"
                elif age_days <= 60:
                    b_key = "days_31_to_60"
                elif age_days <= 90:
                    b_key = "days_61_to_90"
                elif age_days <= 180:
                    b_key = "days_91_to_180"
                else:
                    b_key = "days_181_plus"

                aging_buckets[b_key]["quantity_base_units"] += qty
                aging_buckets[b_key]["cost_valuation"] += cost_subtotal

                pid = prod.product_id
                if pid not in product_map:
                    product_map[pid] = {
                        "product_id": pid,
                        "brand_name": prod.brand_name,
                        "generic_name": prod.generic_name,
                        "dosage_form": prod.dosage_form,
                        "quantity_base_units": 0,
                        "cost_valuation": Decimal("0.0000"),
                        "retail_valuation": Decimal("0.0000"),
                        "mrp_unit": mrp,
                    }
                product_map[pid]["quantity_base_units"] += qty
                product_map[pid]["cost_valuation"] += cost_subtotal
                product_map[pid]["retail_valuation"] += retail_subtotal

        # 2. Potential Margin Calculation
        potential_gross_margin = total_retail_valuation - total_cost_valuation
        if total_retail_valuation > Decimal("0.0000"):
            potential_margin_pct = (potential_gross_margin / total_retail_valuation) * Decimal("100.00")
            potential_margin_pct = potential_margin_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            potential_margin_pct = Decimal("0.00")

        # 3. Format Aging Summary
        formatted_aging: Dict[str, Dict[str, Any]] = {}
        for k, v in aging_buckets.items():
            cost_val = v["cost_valuation"].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if total_cost_valuation > Decimal("0.0000"):
                pct = (cost_val / total_cost_valuation) * Decimal("100.00")
                pct_str = str(pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            else:
                pct_str = "0.00"
            formatted_aging[k] = {
                "label": v["label"],
                "quantity_base_units": v["quantity_base_units"],
                "cost_valuation_bdt": str(cost_val),
                "pct_of_total_cost": pct_str,
            }

        # 4. Product Breakdown
        product_breakdown: List[Dict[str, Any]] = []
        for pid, pdata in sorted(product_map.items(), key=lambda x: x[1]["brand_name"]):
            p_qty = pdata["quantity_base_units"]
            p_cost_val = pdata["cost_valuation"].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            p_retail_val = pdata["retail_valuation"].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            p_margin_bdt = p_retail_val - p_cost_val
            if p_retail_val > Decimal("0.0000"):
                p_margin_pct = (p_margin_bdt / p_retail_val) * Decimal("100.00")
                p_margin_pct_str = str(p_margin_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            else:
                p_margin_pct_str = "0.00"

            eff_unit_cost = (p_cost_val / Decimal(str(p_qty))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if p_qty > 0 else Decimal("0.0000")

            product_breakdown.append(
                {
                    "product_id": pid,
                    "brand_name": pdata["brand_name"],
                    "generic_name": pdata["generic_name"],
                    "dosage_form": pdata["dosage_form"],
                    "quantity_base_units": p_qty,
                    "cost_valuation_bdt": str(p_cost_val),
                    "retail_valuation_bdt": str(p_retail_val),
                    "potential_margin_pct": p_margin_pct_str,
                    "unit_cost_effective": str(eff_unit_cost),
                    "mrp_unit": str(pdata["mrp_unit"].quantize(Decimal("0.0001"))),
                }
            )

        # 5. Realized Margin Derivation from Immutable SaleLotAllocations
        alloc_qs = SaleLotAllocation.objects.filter(
            tenant=tenant,
            status=SaleLotAllocationStatus.COMMITTED,
        )
        if branch:
            alloc_qs = alloc_qs.filter(branch=branch)
        if product_id:
            alloc_qs = alloc_qs.filter(cost_lot__product_id=product_id)
        if start_date:
            alloc_qs = alloc_qs.filter(created_at__date__gte=start_date)
        if end_date:
            alloc_qs = alloc_qs.filter(created_at__date__lte=end_date)

        realized_revenue = Decimal("0.0000")
        realized_cogs = Decimal("0.0000")
        realized_gross_profit = Decimal("0.0000")
        alloc_count = 0

        for alloc in alloc_qs:
            alloc_count += 1
            realized_revenue += Decimal(str(alloc.total_selling_price))
            realized_cogs += Decimal(str(alloc.total_cost))
            realized_gross_profit += Decimal(str(alloc.gross_profit))

        if realized_revenue > Decimal("0.0000"):
            realized_margin_pct = (realized_gross_profit / realized_revenue) * Decimal("100.00")
            realized_margin_pct = realized_margin_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            realized_margin_pct = Decimal("0.00")

        realized_margin_data = {
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "allocations_count": alloc_count,
            "total_revenue_bdt": str(realized_revenue.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
            "total_cogs_bdt": str(realized_cogs.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
            "realized_gross_profit_bdt": str(realized_gross_profit.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
            "realized_margin_pct": str(realized_margin_pct),
        }

        total_cost_val_str = str(total_cost_valuation.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        total_retail_val_str = str(total_retail_valuation.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        potential_gross_margin_str = str(potential_gross_margin.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

        return {
            "tenant_id": str(tenant_id),
            "branch_id": str(branch_id) if branch_id else None,
            "branch_name": branch.name if branch else "All Branches",
            "as_of_date": as_of_date.isoformat(),
            "costing_method": costing_method,
            "total_products_count": len(product_map),
            "total_quantity_base_units": total_quantity_base_units,
            "total_cost_valuation_bdt": total_cost_val_str,
            "total_retail_valuation_bdt": total_retail_val_str,
            "potential_gross_margin_bdt": potential_gross_margin_str,
            "potential_margin_pct": str(potential_margin_pct),
            "aging_summary": formatted_aging,
            "realized_margin": realized_margin_data,
            "product_breakdown": product_breakdown,
        }

    @classmethod
    @transaction.atomic
    def create_snapshot(
        cls,
        user: Any,
        tenant_id: str,
        branch_id: str,
        costing_method: str = StockCostingMethod.FIFO_ACTUAL,
        as_of_date: Optional[datetime.date] = None,
        start_date: Optional[datetime.date] = None,
        end_date: Optional[datetime.date] = None,
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Creates an immutable, audited valuation snapshot for a specific branch.
        """
        cls._assert_manage_role(user)

        valuation = cls.calculate_valuation(
            tenant_id=tenant_id,
            branch_id=branch_id,
            costing_method=costing_method,
            as_of_date=as_of_date,
            start_date=start_date,
            end_date=end_date,
            user=user,
        )

        tenant = Tenant.objects.get(tenant_id=tenant_id)
        branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)

        snapshot_id = f"valsnap-{uuid.uuid4().hex[:12]}"
        snapshot = StockValuationSnapshot.objects.create(
            snapshot_id=snapshot_id,
            tenant=tenant,
            branch=branch,
            valuation_date=datetime.date.fromisoformat(valuation["as_of_date"]),
            costing_method=costing_method,
            total_products_count=valuation["total_products_count"],
            total_quantity_base_units=valuation["total_quantity_base_units"],
            total_cost_valuation_bdt=Decimal(valuation["total_cost_valuation_bdt"]),
            total_retail_valuation_bdt=Decimal(valuation["total_retail_valuation_bdt"]),
            potential_gross_margin_bdt=Decimal(valuation["potential_gross_margin_bdt"]),
            potential_margin_pct=Decimal(valuation["potential_margin_pct"]),
            aging_summary=valuation["aging_summary"],
            realized_revenue_bdt=Decimal(valuation["realized_margin"]["total_revenue_bdt"]),
            realized_cogs_bdt=Decimal(valuation["realized_margin"]["total_cogs_bdt"]),
            realized_gross_profit_bdt=Decimal(valuation["realized_margin"]["realized_gross_profit_bdt"]),
            realized_margin_pct=Decimal(valuation["realized_margin"]["realized_margin_pct"]),
            product_breakdown=valuation["product_breakdown"],
            notes=notes.strip(),
            generated_by=user if getattr(user, "is_authenticated", False) else None,
        )

        # Audit log
        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            branch_id=str(branch.branch_id),
            user_id=getattr(user, "user_id", str(user)),
            actor_email=str(getattr(user, "email", "system@pharma.com")),
            actor_role=str(getattr(user, "role", "BRANCH_MANAGER")),
            action="STOCK_VALUATION_SNAPSHOT_CREATED",
            resource_type="StockValuationSnapshot",
            resource_id=snapshot.snapshot_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "snapshot_id": snapshot.snapshot_id,
                "branch_id": branch.branch_id,
                "costing_method": costing_method,
                "total_cost_valuation_bdt": str(snapshot.total_cost_valuation_bdt),
                "total_quantity_base_units": snapshot.total_quantity_base_units,
            },
        )

        return cls.serialize_snapshot(snapshot)

    @classmethod
    def serialize_snapshot(cls, snapshot: StockValuationSnapshot) -> Dict[str, Any]:
        """Serializes a StockValuationSnapshot into standard JSON response format."""
        return {
            "snapshot_id": snapshot.snapshot_id,
            "tenant_id": str(snapshot.tenant_id),
            "branch_id": str(snapshot.branch_id),
            "branch_name": snapshot.branch.name if snapshot.branch else "",
            "valuation_date": snapshot.valuation_date.isoformat(),
            "costing_method": snapshot.costing_method,
            "total_products_count": snapshot.total_products_count,
            "total_quantity_base_units": snapshot.total_quantity_base_units,
            "total_cost_valuation_bdt": str(snapshot.total_cost_valuation_bdt.quantize(Decimal("0.0001"))),
            "total_retail_valuation_bdt": str(snapshot.total_retail_valuation_bdt.quantize(Decimal("0.0001"))),
            "potential_gross_margin_bdt": str(snapshot.potential_gross_margin_bdt.quantize(Decimal("0.0001"))),
            "potential_margin_pct": str(snapshot.potential_margin_pct.quantize(Decimal("0.01"))),
            "aging_summary": snapshot.aging_summary or {},
            "realized_margin": {
                "total_revenue_bdt": str(snapshot.realized_revenue_bdt.quantize(Decimal("0.0001"))),
                "total_cogs_bdt": str(snapshot.realized_cogs_bdt.quantize(Decimal("0.0001"))),
                "realized_gross_profit_bdt": str(snapshot.realized_gross_profit_bdt.quantize(Decimal("0.0001"))),
                "realized_margin_pct": str(snapshot.realized_margin_pct.quantize(Decimal("0.01"))),
            },
            "product_breakdown": snapshot.product_breakdown or [],
            "notes": snapshot.notes or "",
            "generated_by_id": str(snapshot.generated_by_id) if snapshot.generated_by_id else None,
            "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
        }
