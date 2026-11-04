"""
backend/apps/core/fefo_allocation_service.py
Architecture Decision: ADR-0169 (P169).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Deterministic First-Expired, First-Out (FEFO) Inventory Allocation Engine
with Expiry Blocks, Quarantine Safeguards, Concurrency Reservations,
and Multi-Tier Tie-Breaker Ordering.
"""

from __future__ import annotations

import datetime
import logging
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
    PurchaseCostLot,
    PurchaseCostLotStatus,
    StockReservation,
    StockReservationStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DISPENSE_ALLOWED_ROLES = {
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.INVENTORY_CLERK,
    UserRole.SUPERADMIN,
    "CASHIER",
    "STAFF_PHARMACIST",
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "INVENTORY_CLERK",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}


class FefoAllocationService:
    """
    Authoritative FEFO Batch Stock Allocation and Concurrency Reservation Service.
    Enforces statutory DGDA healthcare safety constraints and deterministic tie-breaker sorting.
    """

    @classmethod
    def _assert_dispense_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in DISPENSE_ALLOWED_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Role is not authorized to execute inventory allocation or dispensing.",
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
    def get_candidate_batches_queryset(
        cls,
        tenant: Tenant,
        branch: Branch,
        product: MedicineProduct,
        as_of_date: Optional[datetime.date] = None,
    ):
        """
        Retrieves eligible candidate batches sorted by strict deterministic FEFO rules:
        1. expiry_date ASC (Earliest expiring first)
        2. purchase_date ASC (FIFO tie-breaker)
        3. batch_number ASC (Lexicographical string tie-breaker)
        4. batch_id ASC (Primary key tie-breaker)
        """
        today = as_of_date or timezone.now().date()

        return (
            BatchStock.objects.filter(
                tenant=tenant,
                branch=branch,
                product=product,
                is_active=True,
                status=BatchStatus.ACTIVE,
                is_quarantined=False,
                recall_notice__isnull=True,
                expiry_date__gt=today,
            )
            .exclude(status__in=[BatchStatus.EXPIRED, BatchStatus.RECALLED, BatchStatus.ARCHIVED, BatchStatus.DEPLETED])
            .order_by("expiry_date", "purchase_date", "batch_number", "batch_id")
        )

    @classmethod
    def preview_fefo_batches(cls, user: Any, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Returns preview of eligible FEFO candidate batches in deterministic allocation order.
        """
        cls._assert_dispense_role(user)

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

        candidates = cls.get_candidate_batches_queryset(tenant, branch, product)
        results = []
        for b in candidates:
            avail = max(0, b.quantity_base_units - b.reserved_quantity_base_units)
            if avail > 0:
                results.append(
                    {
                        "batch_id": b.batch_id,
                        "batch_number": b.batch_number,
                        "expiry_date": b.expiry_date.isoformat(),
                        "purchase_date": b.purchase_date.isoformat(),
                        "total_quantity_base_units": b.quantity_base_units,
                        "reserved_quantity_base_units": b.reserved_quantity_base_units,
                        "available_quantity_base_units": avail,
                        "cost_per_unit": str(b.cost_per_unit),
                    }
                )
        return results

    @classmethod
    @transaction.atomic
    def allocate_fefo(cls, user: Any, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes deterministic FEFO allocation and optional atomic concurrency reservation.
        """
        cls._assert_dispense_role(user)

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
        if not product_id:
            raise ValidationError({"product_id": "product_id is required.", "error_code": "ERR_MISSING_PRODUCT"})
        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise NotFound({"detail": "Product not found.", "error_code": "ERR_PRODUCT_NOT_FOUND"})

        qty_raw = data.get("requested_quantity_base_units")
        if qty_raw is None:
            raise ValidationError(
                {"requested_quantity_base_units": "Quantity is required.", "error_code": "ERR_MISSING_QUANTITY"}
            )
        try:
            requested_qty = int(qty_raw)
            if requested_qty <= 0:
                raise ValueError()
        except Exception:
            raise ValidationError(
                {"requested_quantity_base_units": "Must be a positive integer.", "error_code": "ERR_INVALID_QUANTITY"}
            )

        should_reserve = bool(data.get("reserve", False))
        reference_type = data.get("reference_type", "POS_CART")
        reference_id = data.get("reference_id", "")
        expires_in_minutes = int(data.get("expires_in_minutes", 15))
        now = timezone.now()
        expires_at = now + datetime.timedelta(minutes=expires_in_minutes)

        # Retrieve and pessimistically lock candidate batches
        today = now.date()
        candidate_qs = (
            BatchStock.objects.select_for_update()
            .filter(
                tenant=tenant,
                branch=branch,
                product=product,
                is_active=True,
                status=BatchStatus.ACTIVE,
                is_quarantined=False,
                recall_notice__isnull=True,
                expiry_date__gt=today,
            )
            .exclude(status__in=[BatchStatus.EXPIRED, BatchStatus.RECALLED, BatchStatus.ARCHIVED, BatchStatus.DEPLETED])
            .order_by("expiry_date", "purchase_date", "batch_number", "batch_id")
        )

        candidates = list(candidate_qs)

        remaining_needed = requested_qty
        allocations = []
        total_cogs = Decimal("0.0000")

        for batch in candidates:
            available = max(0, batch.quantity_base_units - batch.reserved_quantity_base_units)
            if available <= 0:
                continue

            take_units = min(available, remaining_needed)

            # Resolve unblended FIFO purchase cost lot if present, else fallback to batch.cost_per_unit
            cost_lot = (
                PurchaseCostLot.objects.filter(
                    tenant=tenant,
                    branch=branch,
                    product=product,
                    batch=batch,
                    status=PurchaseCostLotStatus.ACTIVE,
                    remaining_quantity__gt=0,
                )
                .order_by("purchase_date", "created_at")
                .first()
            )

            unit_cost = cost_lot.unit_cost if cost_lot else Decimal(str(batch.cost_per_unit))
            lot_cogs = unit_cost * Decimal(take_units)
            total_cogs += lot_cogs

            reservation_id = None
            if should_reserve:
                resv_pk = f"resv-{uuid.uuid4().hex[:12]}"
                reservation = StockReservation.objects.create(
                    reservation_id=resv_pk,
                    tenant=tenant,
                    branch=branch,
                    product=product,
                    batch=batch,
                    quantity_base_units=take_units,
                    status=StockReservationStatus.ACTIVE,
                    reference_type=reference_type,
                    reference_id=reference_id,
                    expires_at=expires_at,
                    created_by=user if hasattr(user, "user_id") else None,
                )
                reservation_id = reservation.reservation_id

                # Increment batch reserved units atomically
                batch.reserved_quantity_base_units += take_units
                batch.save(update_fields=["reserved_quantity_base_units", "updated_at"])

            allocations.append(
                {
                    "batch_id": batch.batch_id,
                    "batch_number": batch.batch_number,
                    "expiry_date": batch.expiry_date.isoformat(),
                    "purchase_date": batch.purchase_date.isoformat(),
                    "allocated_units": take_units,
                    "unit_cost": str(unit_cost),
                    "lot_cogs": str(lot_cogs),
                    "reservation_id": reservation_id,
                    "cost_lot_id": cost_lot.lot_id if cost_lot else None,
                }
            )

            remaining_needed -= take_units
            if remaining_needed == 0:
                break

        allocated_total = requested_qty - remaining_needed

        if remaining_needed > 0:
            if allocated_total == 0:
                raise ValidationError(
                    {
                        "detail": f"No eligible FEFO batches available for product '{product.brand_name}'.",
                        "error_code": "ERR_NO_ELIGIBLE_BATCHES",
                    }
                )
            else:
                # If partial allocation occurred under reserve=True, roll back created reservations
                if should_reserve:
                    for alloc in allocations:
                        resv_id = alloc.get("reservation_id")
                        if resv_id:
                            StockReservation.objects.filter(reservation_id=resv_id).delete()
                            BatchStock.objects.filter(batch_id=alloc["batch_id"]).update(
                                reserved_quantity_base_units=BatchStock.objects.filter(batch_id=alloc["batch_id"]).values_list("reserved_quantity_base_units", flat=True)[0] - alloc["allocated_units"]
                            )
                raise ValidationError(
                    {
                        "detail": f"Insufficient dispensable stock for '{product.brand_name}'. Requested {requested_qty}, available {allocated_total}.",
                        "error_code": "ERR_INSUFFICIENT_STOCK",
                        "requested_quantity_base_units": requested_qty,
                        "available_quantity_base_units": allocated_total,
                    }
                )

        # Audit log
        AuditLogEntry.objects.create(
            entry_id=uuid.uuid4(),
            tenant_id=tenant.tenant_id,
            branch_id=branch.branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "cashier@pharmacy"),
            actor_role=getattr(user, "role", "CASHIER"),
            action="FEFO_ALLOCATION_EXECUTED",
            resource_type="BatchStock",
            resource_id=product.product_id,
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            status=AuditActionStatus.SUCCESS,
            ip_address="127.0.0.1",
            after_state={
                "product_id": product.product_id,
                "requested_quantity_base_units": requested_qty,
                "allocated_quantity_base_units": allocated_total,
                "total_cogs": str(total_cogs),
                "is_reserved": should_reserve,
                "allocations_count": len(allocations),
            },
        )

        return {
            "product_id": product.product_id,
            "product_brand_name": product.brand_name,
            "requested_quantity_base_units": requested_qty,
            "allocated_quantity_base_units": allocated_total,
            "is_fully_allocated": True,
            "total_cogs": str(total_cogs),
            "allocations": allocations,
        }

    @classmethod
    @transaction.atomic
    def commit_reservation(cls, user: Any, reservation_id: str) -> Dict[str, Any]:
        """
        Commits an active stock reservation upon POS sale checkout completion.
        Deducts both batch quantity and reserved quantity atomically.
        """
        cls._assert_dispense_role(user)

        resv = StockReservation.objects.select_for_update().filter(reservation_id=reservation_id).first()
        if not resv:
            raise NotFound({"detail": f"Reservation '{reservation_id}' not found.", "error_code": "ERR_RESERVATION_NOT_FOUND"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(resv.tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        cls._assert_tenant_active(resv.tenant)

        if resv.status == StockReservationStatus.COMMITTED:
            raise ValidationError(
                {"detail": "Reservation is already committed.", "error_code": "ERR_RESERVATION_ALREADY_COMMITTED"}
            )
        if resv.status in (StockReservationStatus.RELEASED, StockReservationStatus.EXPIRED):
            raise ValidationError(
                {"detail": f"Cannot commit {resv.status} reservation.", "error_code": "ERR_RESERVATION_INACTIVE"}
            )

        batch = BatchStock.objects.select_for_update().get(batch_id=resv.batch_id)

        # Deduct actual stock and reserved stock atomically
        batch.quantity_base_units = max(0, batch.quantity_base_units - resv.quantity_base_units)
        batch.reserved_quantity_base_units = max(0, batch.reserved_quantity_base_units - resv.quantity_base_units)
        if batch.quantity_base_units == 0:
            batch.status = BatchStatus.DEPLETED
        batch.save(update_fields=["quantity_base_units", "reserved_quantity_base_units", "status", "updated_at"])

        resv.status = StockReservationStatus.COMMITTED
        resv.save(update_fields=["status", "updated_at"])

        AuditLogEntry.objects.create(
            entry_id=uuid.uuid4(),
            tenant_id=resv.tenant_id,
            branch_id=resv.branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "pos@pharmacy"),
            actor_role=getattr(user, "role", "CASHIER"),
            action="STOCK_RESERVATION_COMMITTED",
            resource_type="StockReservation",
            resource_id=resv.reservation_id,
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            status=AuditActionStatus.SUCCESS,
            ip_address="127.0.0.1",
            after_state={
                "reservation_id": resv.reservation_id,
                "batch_id": batch.batch_id,
                "committed_units": resv.quantity_base_units,
                "remaining_batch_stock": batch.quantity_base_units,
            },
        )

        return {
            "reservation_id": resv.reservation_id,
            "status": resv.status,
            "batch_id": batch.batch_id,
            "committed_quantity_base_units": resv.quantity_base_units,
            "remaining_batch_quantity_base_units": batch.quantity_base_units,
        }

    @classmethod
    @transaction.atomic
    def release_reservation(cls, user: Any, reservation_id: str, reason: str = "Cart Abandoned") -> Dict[str, Any]:
        """
        Releases an active stock reservation (e.g. cart cleared or checkout cancelled).
        Decrements batch reserved units so inventory is immediately available to other cashiers.
        """
        cls._assert_dispense_role(user)

        resv = StockReservation.objects.select_for_update().filter(reservation_id=reservation_id).first()
        if not resv:
            raise NotFound({"detail": f"Reservation '{reservation_id}' not found.", "error_code": "ERR_RESERVATION_NOT_FOUND"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(resv.tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        cls._assert_tenant_active(resv.tenant)

        if resv.status != StockReservationStatus.ACTIVE:
            raise ValidationError(
                {"detail": f"Cannot release reservation in {resv.status} status.", "error_code": "ERR_RESERVATION_NOT_ACTIVE"}
            )

        batch = BatchStock.objects.select_for_update().get(batch_id=resv.batch_id)

        # Release reserved units
        batch.reserved_quantity_base_units = max(0, batch.reserved_quantity_base_units - resv.quantity_base_units)
        batch.save(update_fields=["reserved_quantity_base_units", "updated_at"])

        resv.status = StockReservationStatus.RELEASED
        resv.save(update_fields=["status", "updated_at"])

        AuditLogEntry.objects.create(
            entry_id=uuid.uuid4(),
            tenant_id=resv.tenant_id,
            branch_id=resv.branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "pos@pharmacy"),
            actor_role=getattr(user, "role", "CASHIER"),
            action="STOCK_RESERVATION_RELEASED",
            resource_type="StockReservation",
            resource_id=resv.reservation_id,
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            status=AuditActionStatus.SUCCESS,
            ip_address="127.0.0.1",
            after_state={
                "reservation_id": resv.reservation_id,
                "batch_id": batch.batch_id,
                "released_units": resv.quantity_base_units,
                "reason": reason,
            },
        )

        return {
            "reservation_id": resv.reservation_id,
            "status": resv.status,
            "batch_id": batch.batch_id,
            "released_quantity_base_units": resv.quantity_base_units,
            "available_quantity_base_units": batch.available_quantity_base_units,
        }

