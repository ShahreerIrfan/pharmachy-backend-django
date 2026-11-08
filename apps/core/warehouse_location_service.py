"""
backend/apps/core/warehouse_location_service.py
Architecture Decision: ADR-0202 (P202).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Domain Business Service for Warehouse, Bin Locations, Cold-Chain Fridges,
Quarantine Zones and Branch Controlled-Location Access Rules.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchLocationAssignment,
    BatchStatus,
    BatchStock,
    Branch,
    ControlledLocationAccessRule,
    MedicineProduct,
    StorageCondition,
    StorageLocation,
    StorageLocationType,
    Tenant,
    Warehouse,
    WarehouseType,
)

logger = logging.getLogger(__name__)


class WarehouseLocationService:
    """
    Core business service governing multi-tenant branch warehouse facilities,
    storage locations, cold-chain temperature safety, quarantine compliance,
    and controlled access rules (e.g. Narcotics vaults, quarantine holding).
    """

    _WAREHOUSE_ADMIN_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "PLATFORM_SUPER_ADMIN",
        "PROCUREMENT_OFFICER",
        "TENANT_OWNER",
    }

    _INVENTORY_STAFF_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "PLATFORM_SUPER_ADMIN",
        "STAFF_PHARMACIST",
        "PHARMACIST",
        "PROCUREMENT_OFFICER",
        "INVENTORY_CLERK",
        "TENANT_OWNER",
    }

    _SUPERVISOR_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "PLATFORM_SUPER_ADMIN",
        "TENANT_OWNER",
    }

    @classmethod
    def assert_warehouse_admin(cls, actor_user: Any) -> None:
        """Enforces that only pharmacy managers, owners, or admins configure warehouses."""
        if not actor_user:
            return
        role = getattr(actor_user, "role", None)
        role_str = str(role).upper() if role else ""
        if role_str == "CASHIER" or (role_str and role_str not in cls._WAREHOUSE_ADMIN_ROLES):
            raise PermissionDenied(
                "Cashiers and unauthorized personnel cannot configure branch warehouses (ERR_WAREHOUSE_CONFIG_DENIED)."
            )

    @classmethod
    def assert_inventory_staff(cls, actor_user: Any) -> None:
        """Enforces that only qualified staff can view or allocate warehouse bins."""
        if not actor_user:
            return
        role = getattr(actor_user, "role", None)
        role_str = str(role).upper() if role else ""
        if role_str == "CASHIER" or (role_str and role_str not in cls._INVENTORY_STAFF_ROLES):
            raise PermissionDenied(
                "Cashiers cannot allocate or move stock between storage locations (ERR_STAFF_ROLE_DENIED)."
            )

    @classmethod
    @transaction.atomic
    def create_warehouse(
        cls,
        tenant_id: str,
        branch_id: str,
        code: str,
        name: str,
        name_bn: str = "",
        warehouse_type: str = WarehouseType.MAIN_DISPENSARY,
        is_cold_storage: bool = False,
        address: str = "",
        actor_user: Any = None,
    ) -> Warehouse:
        """Creates a tenant branch-scoped warehouse or storage facility."""
        cls.assert_warehouse_admin(actor_user)

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound(f"Tenant '{tenant_id}' not found.")

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
        if not branch:
            raise NotFound(f"Branch '{branch_id}' not found under tenant '{tenant_id}'.")

        normalized_code = code.strip().upper()
        if not normalized_code:
            raise ValidationError({"code": "Warehouse code cannot be empty."})

        if Warehouse.objects.filter(tenant=tenant, branch=branch, code=normalized_code).exists():
            raise ValidationError(
                {"code": f"Warehouse code '{normalized_code}' already exists for this branch."}
            )

        warehouse = Warehouse.objects.create(
            warehouse_id=f"wh-{uuid.uuid4().hex[:10]}",
            tenant=tenant,
            branch=branch,
            name=name.strip(),
            name_bn=name_bn.strip(),
            code=normalized_code,
            warehouse_type=warehouse_type,
            is_cold_storage=is_cold_storage,
            address=address.strip(),
        )
        return warehouse

    @classmethod
    @transaction.atomic
    def create_storage_location(
        cls,
        tenant_id: str,
        branch_id: str,
        warehouse_id: str,
        code: str,
        name: str,
        name_bn: str = "",
        location_type: str = StorageLocationType.SHELF,
        parent_location_id: Optional[str] = None,
        capacity_pack_limit: int = 0,
        temperature_monitored: bool = False,
        min_temp_celsius: Optional[Decimal] = None,
        max_temp_celsius: Optional[Decimal] = None,
        is_controlled_access: bool = False,
        allowed_roles: Optional[List[str]] = None,
        requires_dual_authorization: bool = False,
        actor_user: Any = None,
    ) -> StorageLocation:
        """
        Creates a storage location (Shelf, Bin, Fridge, Freezer, Quarantine Zone, Narcotics Vault).
        Automatically configures temperature ranges and controlled-location access rules.
        """
        cls.assert_warehouse_admin(actor_user)

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound(f"Tenant '{tenant_id}' not found.")

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
        if not branch:
            raise NotFound(f"Branch '{branch_id}' not found under tenant '{tenant_id}'.")

        warehouse = Warehouse.objects.filter(
            tenant=tenant, branch=branch, warehouse_id=warehouse_id
        ).first()
        if not warehouse:
            raise NotFound(f"Warehouse '{warehouse_id}' not found in this branch.")

        parent_loc = None
        if parent_location_id:
            parent_loc = StorageLocation.objects.filter(
                tenant=tenant, branch=branch, warehouse=warehouse, location_id=parent_location_id
            ).first()
            if not parent_loc:
                raise NotFound(f"Parent location '{parent_location_id}' not found in warehouse.")

        normalized_code = code.strip().upper()
        if not normalized_code:
            raise ValidationError({"code": "Location code cannot be empty."})

        if StorageLocation.objects.filter(
            tenant=tenant, branch=branch, warehouse=warehouse, code=normalized_code
        ).exists():
            raise ValidationError(
                {"code": f"Storage location '{normalized_code}' already exists in warehouse '{warehouse.code}'."}
            )

        if capacity_pack_limit < 0:
            raise ValidationError({"capacity_pack_limit": "Capacity pack limit cannot be negative."})

        # Cold chain temperature defaults
        if location_type == StorageLocationType.FRIDGE:
            temperature_monitored = True
            if min_temp_celsius is None:
                min_temp_celsius = Decimal("2.00")
            if max_temp_celsius is None:
                max_temp_celsius = Decimal("8.00")
        elif location_type == StorageLocationType.FREEZER:
            temperature_monitored = True
            if min_temp_celsius is None:
                min_temp_celsius = Decimal("-25.00")
            if max_temp_celsius is None:
                max_temp_celsius = Decimal("-15.00")

        if min_temp_celsius is not None and max_temp_celsius is not None:
            if min_temp_celsius > max_temp_celsius:
                raise ValidationError(
                    {"min_temp_celsius": "Minimum temperature cannot exceed maximum temperature."}
                )

        # Narcotics vault or quarantine zone default to controlled access
        if location_type in [
            StorageLocationType.NARCOTICS_VAULT,
            StorageLocationType.QUARANTINE_ZONE,
        ]:
            is_controlled_access = True

        location = StorageLocation.objects.create(
            location_id=f"loc-{uuid.uuid4().hex[:10]}",
            tenant=tenant,
            branch=branch,
            warehouse=warehouse,
            parent_location=parent_loc,
            location_type=location_type,
            code=normalized_code,
            name=name.strip(),
            name_bn=name_bn.strip(),
            capacity_pack_limit=capacity_pack_limit,
            current_pack_count=0,
            temperature_monitored=temperature_monitored,
            min_temp_celsius=min_temp_celsius,
            max_temp_celsius=max_temp_celsius,
            is_controlled_access=is_controlled_access,
        )

        # Configure access rules if controlled access is active
        if is_controlled_access:
            roles = allowed_roles or ["BRANCH_MANAGER", "PHARMACY_OWNER", "STAFF_PHARMACIST", "PHARMACIST"]
            for r in roles:
                ControlledLocationAccessRule.objects.get_or_create(
                    tenant=tenant,
                    branch=branch,
                    location=location,
                    allowed_role=r.strip().upper(),
                    defaults={"requires_dual_authorization": requires_dual_authorization},
                )

        return location

    @classmethod
    def verify_location_access_permission(
        cls,
        location: StorageLocation,
        actor_user: Any,
        supervisor_user: Any = None,
    ) -> None:
        """
        Enforces controlled-location access rules:
        - Rejects unauthorized roles (e.g. Cashiers).
        - Verifies dual authorization for secure vaults/quarantine if mandated.
        """
        if not location.is_controlled_access:
            return

        if not actor_user:
            raise PermissionDenied(
                "Controlled location requires authenticated staff role (ERR_CONTROLLED_LOCATION_ACCESS_DENIED)."
            )

        actor_role = getattr(actor_user, "role", None)
        actor_role_str = str(actor_role).upper() if actor_role else ""

        # Fetch configured rules
        rules = list(
            ControlledLocationAccessRule.objects.filter(
                tenant=location.tenant,
                branch=location.branch,
                location=location,
                is_active=True,
            )
        )

        allowed_roles = {r.allowed_role for r in rules}
        if not allowed_roles:
            # Fallback safe default
            allowed_roles = {"BRANCH_MANAGER", "PHARMACY_OWNER", "STAFF_PHARMACIST", "PHARMACIST", "TENANT_OWNER"}

        if actor_role_str not in allowed_roles:
            raise PermissionDenied(
                f"Role '{actor_role_str}' is not authorized to access controlled location '{location.code}' "
                f"(ERR_CONTROLLED_LOCATION_ACCESS_DENIED)."
            )

        # Check dual authorization requirement
        dual_required = any(r.requires_dual_authorization for r in rules)
        if dual_required:
            if not supervisor_user or not getattr(supervisor_user, "is_authenticated", False):
                raise ValidationError(
                    {"dual_authorization": "Controlled location requires dual authorization sign-off by a manager or owner (ERR_DUAL_AUTH_REQUIRED)."}
                )

            sup_role = getattr(supervisor_user, "role", None)
            sup_role_str = str(sup_role).upper() if sup_role else ""
            if sup_role_str not in cls._SUPERVISOR_ROLES:
                raise ValidationError(
                    {"dual_authorization": f"Supervisor sign-off requires Manager or Owner role, got '{sup_role_str}' (ERR_DUAL_AUTH_INVALID_ROLE)."}
                )

            if getattr(supervisor_user, "pk", None) == getattr(actor_user, "pk", None):
                raise ValidationError(
                    {"dual_authorization": "Dual authorization sign-off cannot be provided by the same actor (ERR_DUAL_AUTH_SELF_SIGN)."}
                )

    @classmethod
    @transaction.atomic
    def assign_batch_stock_location(
        cls,
        tenant_id: str,
        branch_id: str,
        batch_id: str,
        location_id: str,
        quantity: int,
        actor_user: Any = None,
        supervisor_user: Any = None,
        notes: str = "",
    ) -> BatchLocationAssignment:
        """
        Assigns physical batch stock into a designated shelf, bin, fridge or quarantine location.
        Validates cross-tenant boundaries, capacity limits, temperature requirements,
        quarantine segregation, and controlled access governance.
        """
        cls.assert_inventory_staff(actor_user)

        if quantity <= 0:
            raise ValidationError({"quantity": "Assignment quantity must be greater than zero."})

        # Fetch Batch
        batch = (
            BatchStock.objects.select_for_update()
            .filter(tenant_id=tenant_id, branch_id=branch_id, batch_id=batch_id)
            .first()
        )
        if not batch:
            raise NotFound(
                f"BatchStock '{batch_id}' not found for tenant '{tenant_id}' and branch '{branch_id}'."
            )

        # Fetch Storage Location
        location = (
            StorageLocation.objects.select_for_update()
            .filter(tenant_id=tenant_id, branch_id=branch_id, location_id=location_id)
            .first()
        )
        if not location:
            raise NotFound(
                f"StorageLocation '{location_id}' not found for tenant '{tenant_id}' and branch '{branch_id}'."
            )

        if not location.is_active:
            raise ValidationError({"location": f"Storage location '{location.code}' is inactive."})

        # 1. Enforce Controlled-Location Access Rules
        cls.verify_location_access_permission(location, actor_user, supervisor_user)

        # 2. Temperature & Cold-Chain Storage Compatibility Check
        is_cold_batch = (
            batch.storage_condition in [StorageCondition.COLD_CHAIN, StorageCondition.FREEZER]
            or getattr(batch.product, "is_cold_chain_required", False)
        )
        if is_cold_batch:
            if location.location_type not in [
                StorageLocationType.FRIDGE,
                StorageLocationType.FREEZER,
            ]:
                raise ValidationError(
                    {"location": "Cold-chain products must be stored in a certified FRIDGE or FREEZER location (ERR_COLD_CHAIN_TEMPERATURE_MISMATCH)."}
                )

        if (
            batch.storage_condition == StorageCondition.DO_NOT_FREEZE
            and location.location_type == StorageLocationType.FREEZER
        ):
            raise ValidationError(
                {"location": "Medicines labeled DO NOT FREEZE cannot be stored in deep freezer (ERR_FREEZE_PROHIBITED)."}
            )

        # 3. Quarantine Segregation Check
        is_batch_quarantined = (
            batch.is_quarantined
            or batch.status == BatchStatus.QUARANTINED
        )
        if is_batch_quarantined:
            if location.location_type != StorageLocationType.QUARANTINE_ZONE:
                raise ValidationError(
                    {"location": "Quarantined batches cannot be placed in active dispensing storage; must be stored in a dedicated QUARANTINE_ZONE (ERR_QUARANTINE_RESTRICTION)."}
                )
        else:
            if location.location_type == StorageLocationType.QUARANTINE_ZONE:
                raise ValidationError(
                    {"location": "Active commercial batches cannot be placed in quarantine zone without an active quarantine transition (ERR_ACTIVE_STOCK_IN_QUARANTINE)."}
                )

        # 4. Capacity Limit Enforcement
        if location.capacity_pack_limit > 0:
            if location.current_pack_count + quantity > location.capacity_pack_limit:
                raise ValidationError(
                    {"capacity": f"Storage location capacity exceeded. Limit: {location.capacity_pack_limit}, current: {location.current_pack_count}, requested: {quantity} (ERR_CAPACITY_EXCEEDED)."}
                )

        # 5. Stock Conservation Check
        if quantity > batch.quantity_base_units:
            raise ValidationError(
                {"quantity": f"Cannot assign {quantity} units. Batch only has {batch.quantity_base_units} units in total."}
            )

        # 6. Create / Update Assignment
        assignment, created = BatchLocationAssignment.objects.get_or_create(
            tenant=batch.tenant,
            branch=batch.branch,
            batch=batch,
            location=location,
            defaults={
                "quantity": quantity,
                "assigned_by": actor_user if getattr(actor_user, "is_authenticated", False) else None,
                "notes": notes,
            },
        )
        if not created:
            assignment.quantity += quantity
            assignment.notes = f"{assignment.notes}; {notes}".strip("; ")
            assignment.save(update_fields=["quantity", "notes"])

        # Update location pack count and batch cached location code
        location.current_pack_count += quantity
        location.save(update_fields=["current_pack_count", "updated_at"])

        batch.storage_location = location.code
        batch.save(update_fields=["storage_location", "updated_at"])

        logger.info(
            "Batch %s assigned to location %s (Qty: %d) by %s",
            batch.batch_number,
            location.code,
            quantity,
            actor_user,
        )
        return assignment

    @classmethod
    @transaction.atomic
    def transfer_batch_location(
        cls,
        tenant_id: str,
        branch_id: str,
        batch_id: str,
        source_location_id: str,
        target_location_id: str,
        quantity: int,
        actor_user: Any = None,
        supervisor_user: Any = None,
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Transfers batch stock from a source location to a target location.
        Enforces both source and target access rules, capacity, cold chain, and quarantine limits.
        """
        cls.assert_inventory_staff(actor_user)

        if quantity <= 0:
            raise ValidationError({"quantity": "Transfer quantity must be greater than zero."})

        if source_location_id == target_location_id:
            raise ValidationError({"target_location": "Source and target storage locations must be different."})

        # Fetch source assignment
        source_assign = (
            BatchLocationAssignment.objects.select_for_update()
            .filter(
                tenant_id=tenant_id,
                branch_id=branch_id,
                batch_id=batch_id,
                location_id=source_location_id,
            )
            .first()
        )
        if not source_assign or source_assign.quantity < quantity:
            avail = source_assign.quantity if source_assign else 0
            raise ValidationError(
                {"quantity": f"Insufficient stock at source location. Available: {avail}, requested: {quantity}."}
            )

        source_loc = source_assign.location
        cls.verify_location_access_permission(source_loc, actor_user, supervisor_user)

        # Assign to target location (validates target rules, capacity, cold chain)
        target_assign = cls.assign_batch_stock_location(
            tenant_id=tenant_id,
            branch_id=branch_id,
            batch_id=batch_id,
            location_id=target_location_id,
            quantity=quantity,
            actor_user=actor_user,
            supervisor_user=supervisor_user,
            notes=f"Transferred from {source_loc.code}: {notes}".strip(),
        )

        # Deduct from source
        source_assign.quantity -= quantity
        source_assign.save(update_fields=["quantity"])

        source_loc.current_pack_count = max(0, source_loc.current_pack_count - quantity)
        source_loc.save(update_fields=["current_pack_count", "updated_at"])

        return {
            "batch_id": batch_id,
            "source_location": source_loc.code,
            "source_remaining": source_assign.quantity,
            "target_location": target_assign.location.code,
            "target_quantity": target_assign.quantity,
            "transferred_quantity": quantity,
        }

