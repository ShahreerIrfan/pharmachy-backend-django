"""
backend/apps/core/stock_availability_service.py
Architecture Decision: ADR-0171 (P171).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Authoritative Multi-Tier Branch Stock Availability Matrix Engine.
Distinguishes on-hand, reserved, quarantined, pending inward,
physically available, and server-confirmed available quantities
with strict stock conservation, DGDA safety checks, and FEFO alignment.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any, Dict, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    BranchStockAvailability,
    GoodsReceiptLine,
    GoodsReceiptStatus,
    InventoryBaseUnitStrategy,
    MedicineProduct,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

VIEW_ALLOWED_ROLES = {
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.INVENTORY_CLERK,
    UserRole.AUDITOR,
    UserRole.SUPERADMIN,
    "CASHIER",
    "STAFF_PHARMACIST",
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "INVENTORY_CLERK",
    "AUDITOR",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

MANAGE_ALLOWED_ROLES = {
    UserRole.STAFF_PHARMACIST,
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.INVENTORY_CLERK,
    UserRole.SUPERADMIN,
    "STAFF_PHARMACIST",
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "INVENTORY_CLERK",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}


class StockAvailabilityService:
    """
    Authoritative Branch Stock Availability Service implementing P171 requirements.
    Calculates and enforces the 6-tier stock matrix:
    1. On-Hand
    2. Reserved
    3. Quarantined (including expired & DGDA recalled)
    4. Pending Inward (unposted goods receipts)
    5. Physically Available = max(0, On-Hand - Quarantined)
    6. Server-Confirmed Available = max(0, Physically Available - Reserved)
    """

    @classmethod
    def _assert_view_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in VIEW_ALLOWED_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Role is not authorized to view stock availability records.",
                    "error_code": "ERR_ROLE_UNAUTHORIZED",
                }
            )

    @classmethod
    def _assert_manage_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in MANAGE_ALLOWED_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Role is not authorized to refresh or recalculate stock availability.",
                    "error_code": "ERR_ROLE_UNAUTHORIZED",
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
                    "detail": "Tenant subscription is suspended. Inventory operations are locked.",
                    "error_code": "ERR_TENANT_SUSPENDED",
                }
            )

    @classmethod
    def format_packaging_breakdown(
        cls, product: MedicineProduct, base_units: int, tenant: Optional[Tenant] = None
    ) -> Dict[str, Any]:
        """
        Converts integer base units into pack / intermediate / loose base unit breakdowns
        according to the product's InventoryBaseUnitStrategy (P161/P162).
        """
        strategy_qs = InventoryBaseUnitStrategy.objects.filter(product=product, is_active=True)
        if tenant:
            strategy = strategy_qs.filter(tenant=tenant).first() or strategy_qs.first()
        else:
            strategy = strategy_qs.first()

        if not strategy:
            return {
                "pack_unit_label": "PACK",
                "intermediate_unit_label": "STRIP",
                "base_unit_label": "PIECE",
                "packs": 0,
                "intermediates": 0,
                "loose_base_units": max(0, base_units),
                "formatted_summary": f"{max(0, base_units)} PIECE",
            }

        pack_label = strategy.pack_unit_label or "BOX"
        inter_label = strategy.intermediate_unit_label or "STRIP"
        base_label = strategy.base_unit or "PIECE"

        inter_to_base = max(1, strategy.intermediate_to_base_multiplier or 1)
        pack_to_inter = max(1, strategy.pack_to_intermediate_multiplier or 1)
        total_base_per_pack = max(1, strategy.total_base_units_per_pack or (inter_to_base * pack_to_inter))

        total_base = max(0, int(base_units))
        packs = total_base // total_base_per_pack
        remainder_after_packs = total_base % total_base_per_pack
        intermediates = remainder_after_packs // inter_to_base
        loose_base_units = remainder_after_packs % inter_to_base

        parts = []
        if packs > 0:
            parts.append(f"{packs} {pack_label}{'s' if packs > 1 else ''}")
        if intermediates > 0:
            parts.append(f"{intermediates} {inter_label}{'s' if intermediates > 1 else ''}")
        if loose_base_units > 0 or not parts:
            parts.append(f"{loose_base_units} {base_label}{'s' if loose_base_units > 1 else ''}")

        return {
            "pack_unit_label": pack_label,
            "intermediate_unit_label": inter_label,
            "base_unit_label": base_label,
            "packs": packs,
            "intermediates": intermediates,
            "loose_base_units": loose_base_units,
            "formatted_summary": ", ".join(parts),
        }

    @classmethod
    @transaction.atomic
    def recalculate_availability(
        cls,
        tenant: Tenant,
        branch: Branch,
        product: MedicineProduct,
        as_of_date: Optional[datetime.date] = None,
    ) -> BranchStockAvailability:
        """
        Authoritative transaction-safe recalculation of the 6 stock availability metrics
        for a single product at a specific branch.
        """
        today = as_of_date or timezone.now().date()

        batches = list(
            BatchStock.objects.select_for_update().filter(
                tenant=tenant,
                branch=branch,
                product=product,
                is_active=True,
            )
        )

        on_hand = 0
        reserved = 0
        quarantined = 0

        for b in batches:
            qty = b.quantity_base_units
            resv = b.reserved_quantity_base_units
            on_hand += qty
            reserved += resv

            # Statutory DGDA & QA Quarantine Rules:
            # 1. Marked is_quarantined=True
            # 2. Status is QUARANTINED, RECALLED, or EXPIRED
            # 3. Active batch whose expiry_date <= today
            # 4. Tied to active recall notice
            is_quarantine_state = (
                b.is_quarantined
                or b.status in [BatchStatus.QUARANTINED, BatchStatus.RECALLED, BatchStatus.EXPIRED]
                or b.expiry_date <= today
                or b.recall_notice_id is not None
            )
            if is_quarantine_state:
                quarantined += qty

        # Pending Inward Shipments from unposted Goods Receipt Notes
        pending_lines = GoodsReceiptLine.objects.filter(
            grn__tenant=tenant,
            grn__branch=branch,
            product=product,
            grn__status__in=[
                GoodsReceiptStatus.DRAFT,
                GoodsReceiptStatus.RECEIVED,
                GoodsReceiptStatus.VERIFIED,
            ],
        )
        pending_inward = sum(line.ordered_quantity_base_units for line in pending_lines)

        # Invariant Calculations
        physically_available = max(0, on_hand - quarantined)
        server_confirmed_available = max(0, physically_available - reserved)

        record, _created = BranchStockAvailability.objects.select_for_update().get_or_create(
            tenant=tenant,
            branch=branch,
            product=product,
            defaults={
                "availability_id": f"bav-{uuid.uuid4().hex[:12]}",
                "quantity_on_hand": on_hand,
                "quantity_reserved": reserved,
                "quantity_quarantined": quarantined,
                "quantity_pending_inward": pending_inward,
                "quantity_physically_available": physically_available,
                "quantity_server_confirmed_available": server_confirmed_available,
                "is_out_of_stock": bool(server_confirmed_available == 0),
            },
        )

        record.quantity_on_hand = on_hand
        record.quantity_reserved = reserved
        record.quantity_quarantined = quarantined
        record.quantity_pending_inward = pending_inward
        record.quantity_physically_available = physically_available
        record.quantity_server_confirmed_available = server_confirmed_available
        record.is_out_of_stock = bool(server_confirmed_available == 0)
        record.version += 1
        record.save()

        return record

    @classmethod
    def get_availability_details(
        cls, user: Any, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Retrieves detailed availability metrics, candidate FEFO batches, and packaging conversions.
        """
        cls._assert_view_role(user)

        tenant_id = params.get("tenant_id") or getattr(user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant_id": "tenant_id is required.", "error_code": "ERR_MISSING_TENANT"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})
        cls._assert_tenant_active(tenant)

        branch_id = params.get("branch_id")
        if not branch_id:
            raise ValidationError({"branch_id": "branch_id is required.", "error_code": "ERR_MISSING_BRANCH"})
        branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
        if not branch:
            raise NotFound({"detail": "Branch not found.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        product_id = params.get("product_id")
        if not product_id:
            raise ValidationError({"product_id": "product_id is required.", "error_code": "ERR_MISSING_PRODUCT"})
        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise NotFound({"detail": "Product not found.", "error_code": "ERR_PRODUCT_NOT_FOUND"})

        # Synchronize and get authoritative record
        record = cls.recalculate_availability(tenant, branch, product)

        strat = InventoryBaseUnitStrategy.objects.filter(product=product, is_active=True).first()
        base_unit_str = strat.base_unit if strat and strat.base_unit else "PIECE"

        packaging_breakdown = cls.format_packaging_breakdown(
            product, record.quantity_server_confirmed_available, tenant=tenant
        )
        on_hand_packaging = cls.format_packaging_breakdown(
            product, record.quantity_on_hand, tenant=tenant
        )

        today = timezone.now().date()
        batch_qs = BatchStock.objects.filter(
            tenant=tenant,
            branch=branch,
            product=product,
            is_active=True,
        ).order_by("expiry_date", "batch_number")

        batch_details = []
        for b in batch_qs:
            is_quar = (
                b.is_quarantined
                or b.status in [BatchStatus.QUARANTINED, BatchStatus.RECALLED, BatchStatus.EXPIRED]
                or b.expiry_date <= today
                or b.recall_notice_id is not None
            )
            avail = max(0, b.quantity_base_units - b.reserved_quantity_base_units) if not is_quar else 0
            batch_details.append(
                {
                    "batch_id": b.batch_id,
                    "batch_number": b.batch_number,
                    "expiry_date": b.expiry_date.isoformat(),
                    "total_quantity": b.quantity_base_units,
                    "reserved_quantity": b.reserved_quantity_base_units,
                    "available_quantity": avail,
                    "is_quarantined": is_quar,
                    "status": b.status,
                }
            )

        return {
            "availability_id": record.availability_id,
            "tenant_id": tenant.tenant_id,
            "branch_id": branch.branch_id,
            "branch_name": branch.name,
            "product_id": product.product_id,
            "product_name": product.brand_name,
            "generic_name": product.generic_name or "",
            "base_unit": base_unit_str,
            "quantity_on_hand": record.quantity_on_hand,
            "quantity_reserved": record.quantity_reserved,
            "quantity_quarantined": record.quantity_quarantined,
            "quantity_pending_inward": record.quantity_pending_inward,
            "quantity_physically_available": record.quantity_physically_available,
            "quantity_server_confirmed_available": record.quantity_server_confirmed_available,
            "is_out_of_stock": record.is_out_of_stock,
            "is_below_reorder_level": record.is_below_reorder_level,
            "reorder_level_base_units": record.reorder_level_base_units,
            "packaging_breakdown": packaging_breakdown,
            "on_hand_packaging": on_hand_packaging,
            "batches": batch_details,
            "version": record.version,
            "last_recalculated_at": record.last_recalculated_at.isoformat(),
        }

    @classmethod
    def precheck_checkout_availability(
        cls, user: Any, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Fast atomic POS pre-checkout availability verification.
        Validates whether all requested items can be fulfilled from server-confirmed stock.
        """
        cls._assert_view_role(user)

        tenant_id = data.get("tenant_id") or getattr(user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant_id": "tenant_id is required.", "error_code": "ERR_MISSING_TENANT"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})
        cls._assert_tenant_active(tenant)

        branch_id = data.get("branch_id")
        if not branch_id:
            raise ValidationError({"branch_id": "branch_id is required.", "error_code": "ERR_MISSING_BRANCH"})
        branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
        if not branch:
            raise NotFound({"detail": "Branch not found.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        items = data.get("items")
        if not items or not isinstance(items, list):
            raise ValidationError({"items": "A list of cart items is required.", "error_code": "ERR_EMPTY_ITEMS"})

        all_available = True
        item_results = []

        for idx, item in enumerate(items):
            pid = item.get("product_id")
            if not pid:
                raise ValidationError({"items": f"Item at index {idx} missing product_id.", "error_code": "ERR_MISSING_PRODUCT"})
            product = MedicineProduct.objects.filter(product_id=pid).first()
            if not product:
                raise NotFound({"detail": f"Product {pid} not found.", "error_code": "ERR_PRODUCT_NOT_FOUND"})

            qty_raw = item.get("requested_quantity_base_units")
            try:
                requested_qty = int(qty_raw)
                if requested_qty <= 0:
                    raise ValueError()
            except Exception:
                raise ValidationError(
                    {"items": f"Item at index {idx} has invalid requested quantity: {qty_raw}.", "error_code": "ERR_INVALID_QUANTITY"}
                )

            # Recalculate or retrieve latest availability
            record = cls.recalculate_availability(tenant, branch, product)
            confirmed_avail = record.quantity_server_confirmed_available

            is_item_avail = confirmed_avail >= requested_qty
            shortage = max(0, requested_qty - confirmed_avail)

            if not is_item_avail:
                all_available = False

            packaging = cls.format_packaging_breakdown(product, confirmed_avail, tenant=tenant)
            strat_item = InventoryBaseUnitStrategy.objects.filter(product=product, is_active=True).first()
            item_base_unit = strat_item.base_unit if strat_item and strat_item.base_unit else "PIECE"

            item_results.append(
                {
                    "product_id": product.product_id,
                    "product_name": product.brand_name,
                    "base_unit": item_base_unit,
                    "requested_quantity_base_units": requested_qty,
                    "server_confirmed_available": confirmed_avail,
                    "shortage_units": shortage,
                    "is_available": is_item_avail,
                    "packaging_summary": packaging.get("formatted_summary", ""),
                }
            )

        return {
            "tenant_id": tenant.tenant_id,
            "branch_id": branch.branch_id,
            "all_items_available": all_available,
            "checked_at": timezone.now().isoformat(),
            "items": item_results,
        }

    @classmethod
    @transaction.atomic
    def refresh_branch_matrix(
        cls, user: Any, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Manually triggers a full synchronization of stock availability records for a branch.
        """
        cls._assert_manage_role(user)

        tenant_id = data.get("tenant_id") or getattr(user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant_id": "tenant_id is required.", "error_code": "ERR_MISSING_TENANT"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})
        cls._assert_tenant_active(tenant)

        branch_id = data.get("branch_id")
        if not branch_id:
            raise ValidationError({"branch_id": "branch_id is required.", "error_code": "ERR_MISSING_BRANCH"})
        branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
        if not branch:
            raise NotFound({"detail": "Branch not found.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        product_id = data.get("product_id")
        if product_id:
            products = MedicineProduct.objects.filter(product_id=product_id)
        else:
            # Sync all products having batches in this branch
            product_ids = BatchStock.objects.filter(
                tenant=tenant, branch=branch, is_active=True
            ).values_list("product_id", flat=True).distinct()
            products = MedicineProduct.objects.filter(product_id__in=product_ids)

        refreshed_count = 0
        for p in products:
            cls.recalculate_availability(tenant, branch, p)
            refreshed_count += 1

        # Audit log entry for administrative transparency
        try:
            AuditLogEntry.objects.create(
                tenant=tenant,
                branch=branch,
                actor=user if hasattr(user, "pk") and user.pk else None,
                actor_email=getattr(user, "email", "system"),
                actor_role=getattr(user, "role", "BRANCH_MANAGER"),
                action="REFRESH_STOCK_AVAILABILITY_MATRIX",
                entity_type="BranchStockAvailability",
                entity_id=branch.branch_id,
                status=AuditActionStatus.SUCCESS,
                details={
                    "branch_id": branch.branch_id,
                    "products_refreshed_count": refreshed_count,
                    "targeted_product_id": product_id,
                },
            )
        except Exception as audit_err:
            logger.warning("Audit log recording failed: %s", audit_err)

        return {
            "status": "SUCCESS",
            "branch_id": branch.branch_id,
            "refreshed_products_count": refreshed_count,
            "synchronized_at": timezone.now().isoformat(),
        }

