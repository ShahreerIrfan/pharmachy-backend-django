"""
backend/apps/core/branch_batch_ownership_service.py
Architecture Decision: ADR-0175 (P175).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Authoritative Service for Per-Branch Batch Ownership & Isolation:
1. Strict branch boundary enforcement for batch queries and inventory lookups.
2. Prevention of cross-branch stock mixing in all POS dispense operations.
3. Branch-bound RBAC lockout (Cashiers/Pharmacists strictly barred from cross-branch reads/writes).
4. Multi-branch owner views with segregated per-branch allocation breakdowns.
5. Audited, conservation-safe inter-branch stock transfers.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db import models, transaction
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
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.stock_availability_service import StockAvailabilityService
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

BRANCH_BOUND_ROLES = {
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    UserRole.BRANCH_MANAGER,
    UserRole.INVENTORY_CLERK,
    "CASHIER",
    "STAFF_PHARMACIST",
    "BRANCH_MANAGER",
    "INVENTORY_CLERK",
}

MULTI_BRANCH_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    UserRole.AUDITOR,
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "AUDITOR",
    "OWNER_ADMIN",
    "ADMIN",
}


class BranchBatchOwnershipService:
    """Authoritative service ensuring batch and physical stock ownership remain branch-scoped."""

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
    def resolve_effective_branch(
        cls,
        user: Any,
        tenant: Tenant,
        requested_branch_id: Optional[str] = None,
    ) -> Optional[Branch]:
        """
        Determines the authorized branch context.
        Branch-bound users can ONLY access their assigned branch.
        Multi-branch users can access a requested branch or None (for all-branch reporting).
        """
        user_role = getattr(user, "role", None)
        user_branch_id = getattr(user, "branch_id", None)

        if user_role in BRANCH_BOUND_ROLES:
            if not user_branch_id:
                raise PermissionDenied(
                    {
                        "detail": "User has a branch-bound role but no branch is assigned.",
                        "error_code": "ERR_UNASSIGNED_BRANCH",
                    }
                )
            if requested_branch_id and str(requested_branch_id) != str(user_branch_id):
                AuditLogEntry.objects.create(
                    request_id=f"req-{uuid.uuid4().hex[:12]}",
                    tenant_id=tenant.tenant_id,
                    branch_id=str(user_branch_id),
                    user_id=str(getattr(user, "user_id", getattr(user, "id", user))),
                    actor_email=str(getattr(user, "email", "")),
                    actor_role=str(user_role),
                    action="CROSS_BRANCH_ACCESS_BLOCKED",
                    resource_type="Branch",
                    resource_id=str(requested_branch_id),
                    status=AuditActionStatus.DENIED,
                    metadata_json={
                        "requested_branch_id": requested_branch_id,
                        "assigned_branch_id": user_branch_id,
                    },
                )
                raise PermissionDenied(
                    {
                        "detail": (
                            f"Cross-branch access forbidden. User is only authorized for branch '{user_branch_id}'."
                        ),
                        "error_code": "ERR_CROSS_BRANCH_ACCESS",
                    }
                )
            target_branch_id = user_branch_id
        else:
            target_branch_id = requested_branch_id

        if target_branch_id:
            try:
                branch = Branch.objects.get(tenant=tenant, branch_id=target_branch_id)
                return branch
            except Branch.DoesNotExist:
                raise NotFound(
                    {
                        "detail": f"Branch '{target_branch_id}' not found for tenant '{tenant.tenant_id}'.",
                        "error_code": "ERR_BRANCH_NOT_FOUND",
                    }
                )
        return None

    @classmethod
    def list_branch_batches(
        cls,
        user: Any,
        tenant_id: str,
        branch_id: Optional[str] = None,
        product_id: Optional[str] = None,
        status_filter: Optional[str] = None,
        search: Optional[str] = None,
        dispensable_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """Lists batches with strictly isolated branch ownership."""
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})

        cls._assert_tenant_active(tenant)

        # Cross-tenant check
        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        branch = cls.resolve_effective_branch(user, tenant, requested_branch_id=branch_id)

        qs = BatchStock.objects.select_related("product", "branch", "recall_notice").filter(
            tenant=tenant,
            is_active=True,
        )

        if branch:
            qs = qs.filter(branch=branch)
        if product_id:
            qs = qs.filter(product__product_id=product_id)
        if status_filter:
            qs = qs.filter(status=status_filter.upper())
        if search:
            qs = qs.filter(
                models.Q(batch_number__icontains=search)
                | models.Q(product__brand_name__icontains=search)
                | models.Q(product__generic_name__icontains=search)
            )

        qs = qs.order_by("expiry_date", "batch_number")

        results = []
        for b in qs:
            if dispensable_only and not b.is_dispensable:
                continue
            results.append(cls._serialize_branch_batch(b))

        return results

    @classmethod
    def get_branch_batch(
        cls,
        user: Any,
        tenant_id: str,
        batch_id: str,
        branch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Retrieves a single batch, strictly validating branch ownership."""
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})

        cls._assert_tenant_active(tenant)

        try:
            batch = BatchStock.objects.select_related("product", "branch", "recall_notice").get(
                tenant=tenant, batch_id=batch_id
            )
        except BatchStock.DoesNotExist:
            raise NotFound(
                {
                    "detail": f"Batch '{batch_id}' not found.",
                    "error_code": "ERR_BATCH_NOT_FOUND",
                }
            )

        user_role = getattr(user, "role", None)
        user_branch_id = getattr(user, "branch_id", None)

        if user_role in BRANCH_BOUND_ROLES:
            if str(batch.branch_id) != str(user_branch_id):
                AuditLogEntry.objects.create(
                    request_id=f"req-{uuid.uuid4().hex[:12]}",
                    tenant_id=tenant.tenant_id,
                    branch_id=str(user_branch_id),
                    user_id=str(getattr(user, "user_id", getattr(user, "id", user))),
                    actor_email=str(getattr(user, "email", "")),
                    actor_role=str(user_role),
                    action="CROSS_BRANCH_ACCESS_BLOCKED",
                    resource_type="BatchStock",
                    resource_id=batch_id,
                    status=AuditActionStatus.DENIED,
                    metadata_json={
                        "target_batch_branch": batch.branch_id,
                        "user_branch": user_branch_id,
                    },
                )
                raise PermissionDenied(
                    {
                        "detail": (
                            f"Cross-branch access forbidden. Batch '{batch_id}' belongs to branch '{batch.branch.name}', "
                            f"while user is assigned to '{user_branch_id}'."
                        ),
                        "error_code": "ERR_CROSS_BRANCH_ACCESS",
                    }
                )

        if branch_id and str(batch.branch_id) != str(branch_id):
            raise PermissionDenied(
                {
                    "detail": f"Batch '{batch_id}' belongs to branch '{batch.branch_id}', not requested branch '{branch_id}'.",
                    "error_code": "ERR_CROSS_BRANCH_MISMATCH",
                }
            )

        return cls._serialize_branch_batch(batch)

    @classmethod
    def validate_branch_dispense(
        cls,
        user: Any,
        tenant_id: str,
        branch_id: str,
        items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Pre-flight POS checkout validation ensuring all items belong to the active branch,
        are dispensable under DGDA regulations, and have sufficient unreserved branch stock.
        """
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})

        cls._assert_tenant_active(tenant)
        branch = cls.resolve_effective_branch(user, tenant, requested_branch_id=branch_id)
        if not branch:
            raise ValidationError({"detail": "branch_id is required.", "error_code": "ERR_BRANCH_REQUIRED"})

        if not items:
            raise ValidationError(
                {"detail": "Items list cannot be empty.", "error_code": "ERR_EMPTY_DISPENSE_ITEMS"}
            )

        validated_items = []
        for idx, item in enumerate(items):
            batch_id = item.get("batch_id")
            requested_qty = item.get("quantity", 0)

            if not batch_id:
                raise ValidationError(
                    {
                        "detail": f"Item at index {idx} is missing batch_id.",
                        "error_code": "ERR_MISSING_BATCH_ID",
                    }
                )

            try:
                qty = int(requested_qty)
                if qty <= 0:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValidationError(
                    {
                        "detail": f"Item '{batch_id}' has invalid quantity '{requested_qty}'. Must be positive integer.",
                        "error_code": "ERR_INVALID_DISPENSE_QUANTITY",
                    }
                )

            try:
                batch = BatchStock.objects.select_related("product", "branch").get(
                    tenant=tenant, batch_id=batch_id
                )
            except BatchStock.DoesNotExist:
                raise NotFound(
                    {
                        "detail": f"Batch '{batch_id}' not found.",
                        "error_code": "ERR_BATCH_NOT_FOUND",
                    }
                )

            # Strict branch scoping check
            if str(batch.branch_id) != str(branch.branch_id):
                AuditLogEntry.objects.create(
                    request_id=f"req-{uuid.uuid4().hex[:12]}",
                    tenant_id=tenant.tenant_id,
                    branch_id=branch.branch_id,
                    user_id=str(getattr(user, "user_id", getattr(user, "id", user))),
                    actor_email=str(getattr(user, "email", "")),
                    actor_role=str(getattr(user, "role", "CASHIER")),
                    action="CROSS_BRANCH_DISPENSE_BLOCKED",
                    resource_type="BatchStock",
                    resource_id=batch_id,
                    status=AuditActionStatus.DENIED,
                    metadata_json={
                        "item_batch_branch": batch.branch_id,
                        "checkout_branch": branch.branch_id,
                    },
                )
                raise ValidationError(
                    {
                        "detail": (
                            f"Batch '{batch_id}' belongs to branch '{batch.branch.name}' ({batch.branch_id}), "
                            f"not active checkout branch '{branch.name}' ({branch.branch_id}). "
                            "Cross-branch stock mixing in POS sales is strictly barred."
                        ),
                        "error_code": "ERR_CROSS_BRANCH_DISPENSE_BLOCKED",
                    }
                )

            # Dispensability checks
            if not batch.is_dispensable:
                reasons = []
                if batch.is_expired:
                    reasons.append("Batch is expired under DGDA regulations.")
                if batch.is_quarantined:
                    reasons.append("Batch is in quarantine disposition.")
                if batch.is_recalled:
                    reasons.append(f"Batch is recalled: {batch.recall_reason or 'DGDA Statutory Notice'}.")
                if batch.available_quantity_base_units <= 0:
                    reasons.append("No unreserved stock available.")
                raise ValidationError(
                    {
                        "detail": f"Batch '{batch_id}' is not dispensable: {'; '.join(reasons)}",
                        "error_code": "ERR_BATCH_NOT_DISPENSABLE",
                    }
                )

            if batch.available_quantity_base_units < qty:
                raise ValidationError(
                    {
                        "detail": (
                            f"Insufficient unreserved stock for batch '{batch_id}'. "
                            f"Requested {qty} units, but only {batch.available_quantity_base_units} are available."
                        ),
                        "error_code": "ERR_INSUFFICIENT_UNRESERVED_STOCK",
                    }
                )

            validated_items.append(
                {
                    "batch_id": batch.batch_id,
                    "batch_number": batch.batch_number,
                    "product_id": batch.product.product_id,
                    "product_name": batch.product.brand_name,
                    "branch_id": branch.branch_id,
                    "branch_name": branch.name,
                    "requested_quantity": qty,
                    "unit_cost_bdt": str(batch.cost_per_unit),
                    "expiry_date": str(batch.expiry_date),
                }
            )

        return {
            "valid": True,
            "branch_id": branch.branch_id,
            "branch_name": branch.name,
            "item_count": len(validated_items),
            "items": validated_items,
        }

    @classmethod
    def get_medicine_per_branch_summary(
        cls,
        user: Any,
        tenant_id: str,
        product_id: str,
    ) -> Dict[str, Any]:
        """
        Returns segregated per-branch inventory breakdown for a medicine across authorized branches,
        strictly preventing blended stock totals or cross-branch confusion.
        """
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})

        cls._assert_tenant_active(tenant)

        try:
            product = MedicineProduct.objects.get(product_id=product_id)
        except MedicineProduct.DoesNotExist:
            raise NotFound({"detail": "Product not found.", "error_code": "ERR_PRODUCT_NOT_FOUND"})

        user_role = getattr(user, "role", None)
        user_branch_id = getattr(user, "branch_id", None)

        if user_role in BRANCH_BOUND_ROLES:
            branches = Branch.objects.filter(tenant=tenant, branch_id=user_branch_id)
        else:
            branches = Branch.objects.filter(tenant=tenant, is_active=True).order_by("name")

        branch_breakdowns = []
        for br in branches:
            batches = BatchStock.objects.filter(
                tenant=tenant, branch=br, product=product, is_active=True
            ).order_by("expiry_date")

            total_on_hand = sum(b.quantity_base_units for b in batches)
            total_reserved = sum(b.reserved_quantity_base_units for b in batches)
            total_available = sum(b.available_quantity_base_units for b in batches)

            batch_list = [cls._serialize_branch_batch(b) for b in batches]

            branch_breakdowns.append(
                {
                    "branch_id": br.branch_id,
                    "branch_name": br.name,
                    "quantity_on_hand": total_on_hand,
                    "quantity_reserved": total_reserved,
                    "quantity_available": total_available,
                    "batch_count": len(batch_list),
                    "batches": batch_list,
                }
            )

        return {
            "product_id": product.product_id,
            "product_name": product.brand_name,
            "generic_name": product.generic_name,
            "branch_count": len(branch_breakdowns),
            "branch_breakdowns": branch_breakdowns,
        }

    @classmethod
    def transfer_stock_between_branches(
        cls,
        user: Any,
        tenant_id: str,
        source_branch_id: str,
        target_branch_id: str,
        batch_id: str,
        quantity: int,
        reason: str,
    ) -> Dict[str, Any]:
        """
        Audited inter-branch stock transfer. Stock cannot be virtually reassigned;
        it requires an audited dispatch deducting source batch balance and an
        inward receipt creating/updating target branch batch balance.
        """
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})

        cls._assert_tenant_active(tenant)

        user_role = getattr(user, "role", None)
        if user_role not in MULTI_BRANCH_ROLES and user_role not in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
            raise PermissionDenied(
                {
                    "detail": "Only Branch Managers or Pharmacy Owners can authorize inter-branch stock transfers.",
                    "error_code": "ERR_PERMISSION_DENIED",
                }
            )

        if str(source_branch_id) == str(target_branch_id):
            raise ValidationError(
                {
                    "detail": "Source and target branch cannot be the same.",
                    "error_code": "ERR_SAME_BRANCH_TRANSFER",
                }
            )

        try:
            qty = int(quantity)
            if qty <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValidationError(
                {"detail": "Transfer quantity must be a positive integer.", "error_code": "ERR_INVALID_TRANSFER_QUANTITY"}
            )

        cleaned_reason = (reason or "").strip()
        if len(cleaned_reason) < 10:
            raise ValidationError(
                {
                    "detail": "A reasoned audit explanation (minimum 10 characters) is mandatory.",
                    "error_code": "ERR_AUDIT_REASON_REQUIRED",
                }
            )

        with transaction.atomic():
            try:
                src_branch = Branch.objects.get(tenant=tenant, branch_id=source_branch_id)
                dst_branch = Branch.objects.get(tenant=tenant, branch_id=target_branch_id)
            except Branch.DoesNotExist as exc:
                raise NotFound({"detail": str(exc), "error_code": "ERR_BRANCH_NOT_FOUND"})

            try:
                src_batch = BatchStock.objects.select_for_update().get(
                    tenant=tenant, branch=src_branch, batch_id=batch_id
                )
            except BatchStock.DoesNotExist:
                raise NotFound(
                    {
                        "detail": f"Source batch '{batch_id}' not found in source branch '{source_branch_id}'.",
                        "error_code": "ERR_SOURCE_BATCH_NOT_FOUND",
                    }
                )

            # Verify unreserved stock in source branch
            if src_batch.available_quantity_base_units < qty:
                raise ValidationError(
                    {
                        "detail": (
                            f"Insufficient unreserved stock in source branch '{src_branch.name}'. "
                            f"Requested transfer of {qty} units, but only {src_batch.available_quantity_base_units} are unreserved."
                        ),
                        "error_code": "ERR_INSUFFICIENT_UNRESERVED_STOCK",
                    }
                )

            # Deduct from source batch
            src_batch.quantity_base_units -= qty
            src_batch.save(update_fields=["quantity_base_units"])

            # Deduct from source cost lot if available
            src_lot = PurchaseCostLot.objects.filter(
                tenant=tenant, branch=src_branch, batch=src_batch, status=PurchaseCostLotStatus.ACTIVE
            ).first()
            if src_lot:
                src_lot.remaining_quantity = max(0, src_lot.remaining_quantity - qty)
                if src_lot.remaining_quantity == 0:
                    src_lot.status = PurchaseCostLotStatus.DEPLETED
                src_lot.save()

            # Find or create target branch batch
            dst_batch, created = BatchStock.objects.select_for_update().get_or_create(
                tenant=tenant,
                branch=dst_branch,
                product=src_batch.product,
                batch_number=src_batch.batch_number,
                defaults={
                    "batch_id": f"bat-{uuid.uuid4().hex[:12]}",
                    "quantity_base_units": 0,
                    "reserved_quantity_base_units": 0,
                    "expiry_date": src_batch.expiry_date,
                    "purchase_date": timezone.now().date(),
                    "cost_per_unit": src_batch.cost_per_unit,
                    "status": BatchStatus.ACTIVE,
                    "manufacturing_date": src_batch.manufacturing_date,
                    "storage_condition": src_batch.storage_condition,
                    "manufacturer_name": src_batch.manufacturer_name,
                },
            )

            dst_batch.quantity_base_units += qty
            dst_batch.save(update_fields=["quantity_base_units"])

            # Create or update target branch cost lot
            dst_lot_id = f"pcl-{uuid.uuid4().hex[:12]}"
            PurchaseCostLot.objects.create(
                lot_id=dst_lot_id,
                tenant=tenant,
                branch=dst_branch,
                product=src_batch.product,
                batch=dst_batch,
                purchase_date=timezone.now().date(),
                unit_cost=src_batch.cost_per_unit,
                initial_quantity=qty,
                remaining_quantity=qty,
                status=PurchaseCostLotStatus.ACTIVE,
                notes=f"Inter-branch transfer from {src_branch.name} ({src_branch.branch_id}): {cleaned_reason}",
            )

            # Recalculate stock availability for BOTH branches
            StockAvailabilityService.recalculate_availability(
                tenant=tenant, branch=src_branch, product=src_batch.product
            )
            StockAvailabilityService.recalculate_availability(
                tenant=tenant, branch=dst_branch, product=src_batch.product
            )

            # Audit logging
            transfer_id = f"trf-{uuid.uuid4().hex[:12]}"
            AuditLogEntry.objects.create(
                request_id=f"req-{uuid.uuid4().hex[:12]}",
                tenant_id=tenant.tenant_id,
                branch_id=src_branch.branch_id,
                user_id=str(getattr(user, "user_id", getattr(user, "id", user))),
                actor_email=str(getattr(user, "email", "")),
                actor_role=str(user_role),
                action="INTER_BRANCH_STOCK_TRANSFER",
                resource_type="BatchStock",
                resource_id=transfer_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "source_branch_id": src_branch.branch_id,
                    "target_branch_id": dst_branch.branch_id,
                    "source_batch_id": src_batch.batch_id,
                    "target_batch_id": dst_batch.batch_id,
                    "quantity": qty,
                    "reason": cleaned_reason,
                },
            )

            return {
                "transfer_id": transfer_id,
                "source_branch_id": src_branch.branch_id,
                "target_branch_id": dst_branch.branch_id,
                "source_batch_id": src_batch.batch_id,
                "target_batch_id": dst_batch.batch_id,
                "product_id": src_batch.product.product_id,
                "quantity": qty,
                "source_remaining_quantity": src_batch.quantity_base_units,
                "target_new_quantity": dst_batch.quantity_base_units,
                "reason": cleaned_reason,
            }

    @classmethod
    def _serialize_branch_batch(cls, b: BatchStock) -> Dict[str, Any]:
        return {
            "batch_id": b.batch_id,
            "tenant_id": b.tenant_id,
            "branch_id": b.branch_id,
            "branch_name": b.branch.name if hasattr(b, "branch") and b.branch else "",
            "product_id": b.product.product_id,
            "product_name": b.product.brand_name,
            "batch_number": b.batch_number,
            "quantity_base_units": b.quantity_base_units,
            "reserved_quantity_base_units": b.reserved_quantity_base_units,
            "available_quantity_base_units": b.available_quantity_base_units,
            "expiry_date": str(b.expiry_date),
            "cost_per_unit": str(b.cost_per_unit),
            "status": b.status,
            "is_expired": b.is_expired,
            "is_quarantined": b.is_quarantined,
            "is_recalled": b.is_recalled,
            "is_dispensable": b.is_dispensable,
        }
