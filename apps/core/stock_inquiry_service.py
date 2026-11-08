"""
backend/apps/core/stock_inquiry_service.py
Architecture Decision: ADR-0203 (P203).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Domain Business Service for Multi-Tenant Branch Stock Inquiries:
- Accurate rollup of batches, base units, available, reserved, quarantined, damaged, and expired stock.
- FEFO-ordered batch breakdown with days until expiry and expiry status categorization.
- Packaging breakdown (Box, Strip, Tablet / Piece units).
- Branch-level RBAC restrictions (Cashiers restricted to assigned branch).
- Tamper-evident inquiry audit logging.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchLocationAssignment,
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    StockInquiryAuditLog,
    StorageCondition,
    Tenant,
)

logger = logging.getLogger(__name__)


class StockInquiryService:
    """
    Authoritative stock inquiry engine providing real-time multi-tenant,
    branch-isolated visibility into medicine batches, availability, reservations,
    quarantine segregation, and expiry lifecycles.
    """

    _ELEVATED_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "PLATFORM_SUPER_ADMIN",
        "TENANT_OWNER",
        "STAFF_PHARMACIST",
        "PHARMACIST",
    }

    @classmethod
    def assert_branch_access(
        cls, actor_user: Any, branch_id: str, tenant_id: str
    ) -> None:
        """
        Enforces tenant and branch isolation:
        - Cross-tenant queries are blocked.
        - Cashiers can only inquire within their assigned branch.
        """
        if not actor_user:
            return

        user_tenant = getattr(actor_user, "tenant_id", None)
        if user_tenant and str(user_tenant) != str(tenant_id):
            raise PermissionDenied(
                "Cross-tenant stock access is strictly prohibited (ERR_TENANT_ISOLATION_VIOLATION)."
            )

        user_role = getattr(actor_user, "role", None)
        role_str = str(user_role).upper() if user_role else ""

        # Cashiers are strictly confined to their assigned branch
        if role_str == "CASHIER":
            user_branch = getattr(actor_user, "branch_id", None)
            if user_branch and str(user_branch) != str(branch_id):
                raise PermissionDenied(
                    f"Cashier is not authorized to query stock outside assigned branch '{user_branch}' "
                    f"(ERR_BRANCH_ACCESS_DENIED)."
                )

    @classmethod
    def classify_expiry_status(
        cls,
        expiry_date: date,
        today: date,
        is_quarantined: bool,
        status: str,
        has_recall: bool,
    ) -> str:
        """Categorizes batch lifecycle and expiry status for stock inquiries."""
        if has_recall or status == BatchStatus.RECALLED:
            return "RECALLED"
        if is_quarantined or status == BatchStatus.QUARANTINED:
            return "QUARANTINED"
        days_until_expiry = (expiry_date - today).days
        if days_until_expiry <= 0 or status == BatchStatus.EXPIRED:
            return "EXPIRED"
        if days_until_expiry <= 30:
            return "CRITICAL_NEAR_EXPIRY"
        if days_until_expiry <= 90:
            return "NEAR_EXPIRY"
        return "ACTIVE"

    @classmethod
    def format_packaging_summary(cls, base_units: int, dosage_form: str = "TABLET") -> Dict[str, Any]:
        """
        Computes standard multi-level packaging breakdown:
        Assuming canonical packaging: 10 base units = 1 strip, 10 strips (100 base units) = 1 box.
        """
        units_per_strip = 10
        strips_per_box = 10
        units_per_box = units_per_strip * strips_per_box

        boxes = base_units // units_per_box
        remainder = base_units % units_per_box
        strips = remainder // units_per_strip
        pieces = remainder % units_per_strip

        text_parts = []
        if boxes > 0:
            text_parts.append(f"{boxes} Box{'es' if boxes > 1 else ''}")
        if strips > 0:
            text_parts.append(f"{strips} Strip{'s' if strips > 1 else ''}")
        if pieces > 0 or not text_parts:
            text_parts.append(f"{pieces} Piece{'s' if pieces > 1 else ''}")

        return {
            "boxes": boxes,
            "strips": strips,
            "pieces": pieces,
            "total_base_units": base_units,
            "formatted_text": ", ".join(text_parts),
        }

    @classmethod
    def query_stock(
        cls,
        tenant_id: str,
        branch_id: str,
        search: str = "",
        product_id: Optional[str] = None,
        batch_number: Optional[str] = None,
        expiry_status: str = "ALL",
        location_code: Optional[str] = None,
        storage_condition: Optional[str] = None,
        include_zero_stock: bool = False,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Executes an authorized stock inquiry returning product rollups and FEFO batch breakdowns.
        """
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound(f"Tenant '{tenant_id}' not found.")

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
        if not branch:
            raise NotFound(f"Branch '{branch_id}' not found under tenant '{tenant_id}'.")

        cls.assert_branch_access(actor_user, branch_id, tenant_id)

        # Base Query
        qs = (
            BatchStock.objects.filter(tenant=tenant, branch=branch)
            .select_related("product")
            .order_by("expiry_date", "batch_number")
        )

        if product_id:
            qs = qs.filter(product_id=product_id)

        if batch_number:
            qs = qs.filter(batch_number__iexact=batch_number.strip())

        if search:
            s = search.strip()
            qs = qs.filter(
                Q(product__brand_name__icontains=s)
                | Q(product__generic_name__icontains=s)
                | Q(product__brand_name_bn__icontains=s)
                | Q(product__generic_name_bn__icontains=s)
                | Q(batch_number__icontains=s)
                | Q(product__barcode=s)
            )

        if location_code:
            qs = qs.filter(storage_location__iexact=location_code.strip())

        if storage_condition:
            qs = qs.filter(storage_condition=storage_condition)

        if not include_zero_stock:
            qs = qs.filter(quantity_base_units__gt=0)

        batches = list(qs)
        today = timezone.now().date()

        # Group by product
        products_map: Dict[str, Dict[str, Any]] = {}
        total_available_in_run = 0
        total_batches_matched = len(batches)

        for b in batches:
            p = b.product
            pid = p.product_id

            days_left = (b.expiry_date - today).days
            cat = cls.classify_expiry_status(
                expiry_date=b.expiry_date,
                today=today,
                is_quarantined=b.is_quarantined,
                status=b.status,
                has_recall=bool(b.recall_notice_id),
            )

            # Filter by expiry_status if requested
            if expiry_status != "ALL":
                if expiry_status == "EXPIRED" and cat != "EXPIRED":
                    continue
                elif expiry_status == "NEAR_EXPIRY" and cat not in ["NEAR_EXPIRY", "CRITICAL_NEAR_EXPIRY"]:
                    continue
                elif expiry_status == "ACTIVE" and cat != "ACTIVE":
                    continue
                elif expiry_status == "QUARANTINED" and cat != "QUARANTINED":
                    continue

            # Available quantity is strictly 0 if batch is expired, quarantined, or recalled
            if cat in ["EXPIRED", "QUARANTINED", "RECALLED"]:
                batch_available = 0
            else:
                batch_available = max(0, b.quantity_base_units - b.reserved_quantity_base_units)

            total_available_in_run += batch_available

            if pid not in products_map:
                products_map[pid] = {
                    "product_id": pid,
                    "brand_name": p.brand_name,
                    "brand_name_bn": p.brand_name_bn,
                    "generic_name": p.generic_name,
                    "generic_name_bn": p.generic_name_bn,
                    "dosage_form": p.dosage_form,
                    "strength": p.strength,
                    "manufacturer": p.manufacturer,
                    "barcode": p.barcode,
                    "is_prescription_only": p.is_prescription_only,
                    "is_narcotic": p.is_narcotic,
                    "is_cold_chain": p.requires_cold_chain,
                    "mrp_unit": str(p.mrp_unit),
                    "cost_unit": str(p.cost_unit),
                    "total_quantity_base_units": 0,
                    "available_quantity_base_units": 0,
                    "reserved_quantity_base_units": 0,
                    "quarantined_quantity_base_units": 0,
                    "expired_quantity_base_units": 0,
                    "near_expiry_quantity_base_units": 0,
                    "total_cost_valuation_bdt": Decimal("0.0000"),
                    "total_mrp_valuation_bdt": Decimal("0.0000"),
                    "batches": [],
                }

            entry = products_map[pid]
            entry["total_quantity_base_units"] += b.quantity_base_units
            entry["available_quantity_base_units"] += batch_available
            entry["reserved_quantity_base_units"] += b.reserved_quantity_base_units

            if cat == "EXPIRED":
                entry["expired_quantity_base_units"] += b.quantity_base_units
            elif cat in ["QUARANTINED", "RECALLED"]:
                entry["quarantined_quantity_base_units"] += b.quantity_base_units
            elif cat in ["CRITICAL_NEAR_EXPIRY", "NEAR_EXPIRY"]:
                entry["near_expiry_quantity_base_units"] += b.quantity_base_units

            batch_cost_val = Decimal(str(b.quantity_base_units)) * b.cost_per_unit
            batch_mrp_val = Decimal(str(b.quantity_base_units)) * p.mrp_unit
            entry["total_cost_valuation_bdt"] += batch_cost_val
            entry["total_mrp_valuation_bdt"] += batch_mrp_val

            entry["batches"].append(
                {
                    "batch_id": b.batch_id,
                    "batch_number": b.batch_number,
                    "expiry_date": str(b.expiry_date),
                    "days_until_expiry": days_left,
                    "expiry_status": cat,
                    "status": b.status,
                    "quantity_base_units": b.quantity_base_units,
                    "reserved_quantity_base_units": b.reserved_quantity_base_units,
                    "available_quantity_base_units": batch_available,
                    "storage_location": b.storage_location or "DEFAULT",
                    "storage_condition": b.storage_condition,
                    "cost_per_unit": str(b.cost_per_unit),
                    "mrp_unit": str(p.mrp_unit),
                    "batch_cost_valuation": str(batch_cost_val),
                }
            )

        # Finalize product summaries
        product_results = []
        for pdata in products_map.values():
            pkg = cls.format_packaging_summary(
                pdata["available_quantity_base_units"], pdata["dosage_form"]
            )
            pdata["packaging_summary"] = pkg
            pdata["total_cost_valuation_bdt"] = str(pdata["total_cost_valuation_bdt"])
            pdata["total_mrp_valuation_bdt"] = str(pdata["total_mrp_valuation_bdt"])
            product_results.append(pdata)

        # Log inquiry audit
        try:
            StockInquiryAuditLog.objects.create(
                tenant=tenant,
                branch=branch,
                queried_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
                search_query=search,
                filter_expiry_status=expiry_status,
                filter_location=location_code or "",
                products_matched_count=len(product_results),
                batches_matched_count=total_batches_matched,
                total_available_units=total_available_in_run,
            )
        except Exception as e:
            logger.warning("Failed to create StockInquiryAuditLog: %s", e)

        return {
            "tenant_id": tenant_id,
            "branch_id": branch_id,
            "inquiry_timestamp": timezone.now().isoformat(),
            "query_filters": {
                "search": search,
                "product_id": product_id,
                "batch_number": batch_number,
                "expiry_status": expiry_status,
                "location_code": location_code,
                "storage_condition": storage_condition,
            },
            "products_count": len(product_results),
            "total_available_units": total_available_in_run,
            "results": product_results,
        }

