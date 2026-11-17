"""
backend/apps/core/storage_policy_service.py
Architecture Decision: ADR-0213 (P213).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Domain Service for Storage Policy Enforcement:
- Prevents assignment of cold-chain medicines to ambient or incompatible storage zones.
- Validates location capabilities (Fridge, Freezer, Cold Room vs Ambient Shelf/Bin).
- Enforces branch-level storage zone policies with audit logging.
- Atomic batch relocation with concurrency locking (select_for_update).
- RBAC: Cashiers strictly denied from relocating or modifying storage assignments.
- Multi-tenant and branch boundaries enforced.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchStatus,
    BatchStock,
    BatchStorageAssignmentLog,
    Branch,
    MedicineProduct,
    StorageLocation,
    StorageLocationType,
    StorageZonePolicy,
    Tenant,
    Warehouse,
    WarehouseType,
)
from apps.identity.models import UserRole


class StoragePolicyService:
    """
    Business service enforcing storage zone policies, cold-chain safety rules,
    and audited batch relocation workflows.
    """

    ALLOWED_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "SUPERADMIN",
        "STAFF_PHARMACIST",
        "PROCUREMENT_OFFICER",
        "INVENTORY_CLERK",
    }

    @classmethod
    def assert_inventory_role(cls, user: Any) -> None:
        """
        Ensures user has inventory/clinical privileges.
        Cashiers are prohibited from modifying storage assignments.
        """
        role = getattr(user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str == "CASHIER" or role_str not in cls.ALLOWED_ROLES:
            raise PermissionDenied(
                "Cashiers cannot assign or relocate medicine stock. Access is restricted to Managers, Owners, and Pharmacists."
            )

    @classmethod
    def is_cold_chain_capable_location(cls, storage_location: StorageLocation) -> bool:
        """
        Determines if a storage location is capable of maintaining cold-chain temperatures (2°C - 8°C or freezing).
        """
        # Direct fridge / freezer location type
        if storage_location.location_type in [StorageLocationType.FRIDGE, StorageLocationType.FREEZER]:
            return True

        # Cold storage warehouse or cold room
        wh = getattr(storage_location, "warehouse", None)
        if wh:
            if getattr(wh, "is_cold_storage", False):
                return True
            if getattr(wh, "warehouse_type", None) == WarehouseType.COLD_ROOM:
                return True

        return False

    @classmethod
    def validate_storage_assignment(
        cls,
        product: MedicineProduct,
        storage_location: StorageLocation,
        policy: Optional[StorageZonePolicy] = None,
        override_justification: str = "",
    ) -> Tuple[bool, str]:
        """
        Validates whether a product can be safely assigned to a storage location.
        Returns (is_compliant, explanation_message).
        """
        requires_cold = getattr(product, "requires_cold_chain", False)
        # Also treat product as cold-chain if explicitly bounded to 2°C - 8°C
        if not requires_cold:
            if product.min_temp_celsius is not None and product.max_temp_celsius is not None:
                if product.max_temp_celsius <= Decimal("10.00"):
                    requires_cold = True

        is_capable = cls.is_cold_chain_capable_location(storage_location)

        if requires_cold and not is_capable:
            # Policy allows override only if explicit clinical justification provided
            if policy and policy.allow_ambient_override and len(override_justification.strip()) >= 15:
                return True, f"Overridden by policy: {override_justification.strip()}"

            err_msg = (
                f"Storage policy violation: Medicine '{product.brand_name}' requires cold-chain "
                f"temperature control (2.00°C - 8.00°C) and cannot be assigned to ambient storage "
                f"location '{storage_location.name}' [{storage_location.code}] ({storage_location.location_type})."
            )
            return False, err_msg

        # Prevent storing ambient products in deep freezers
        if not requires_cold and storage_location.location_type == StorageLocationType.FREEZER:
            err_msg = (
                f"Storage policy violation: Ambient product '{product.brand_name}' cannot be stored "
                f"in Deep Freezer '{storage_location.name}'."
            )
            return False, err_msg

        return True, "Storage assignment is compliant."

    @classmethod
    @transaction.atomic
    def assign_or_relocate_batch(
        cls,
        tenant: Tenant,
        branch: Branch,
        batch: BatchStock,
        target_location: StorageLocation,
        actor_user: Any,
        override_justification: str = "",
    ) -> BatchStorageAssignmentLog:
        """
        Atomically validates storage policy and updates batch location with audit logging.
        """
        cls.assert_inventory_role(actor_user)

        # Tenant and branch validation
        if batch.tenant_id != tenant.tenant_id:
            raise NotFound("Batch does not belong to this tenant.")
        if target_location.tenant_id != tenant.tenant_id:
            raise NotFound("Target storage location does not belong to this tenant.")

        if batch.branch_id != branch.branch_id:
            raise ValidationError({"branch": "Batch does not belong to the specified branch."})
        if target_location.branch_id != branch.branch_id:
            raise ValidationError({"storage_location": "Target storage location belongs to a different branch."})

        # Fetch or get default policy
        policy = StorageZonePolicy.objects.filter(tenant=tenant, branch=branch, is_active=True).first()

        is_compliant, reason = cls.validate_storage_assignment(
            product=batch.product,
            storage_location=target_location,
            policy=policy,
            override_justification=override_justification,
        )

        if not is_compliant:
            raise ValidationError({"storage_location": reason})

        # Concurrency safe update
        locked_batch = BatchStock.objects.select_for_update().get(pk=batch.pk)
        old_location = locked_batch.storage_location
        locked_batch.storage_location = target_location.code
        locked_batch.save(update_fields=["storage_location", "updated_at"])

        log_entry = BatchStorageAssignmentLog.objects.create(
            tenant=tenant,
            branch=branch,
            batch=locked_batch,
            product=locked_batch.product,
            from_location_code=old_location or "",
            to_location_code=target_location.code,
            quantity_units=locked_batch.quantity_base_units,
            is_cold_chain_product=getattr(locked_batch.product, "requires_cold_chain", False),
            is_compliant=True,
            justification=override_justification,
            assigned_by=actor_user,
        )

        return log_entry

    @classmethod
    def audit_branch_storage_compliance(
        cls,
        tenant: Tenant,
        branch: Branch,
    ) -> Dict[str, Any]:
        """
        Audits all active batches in a branch against storage zone policy and returns compliance stats.
        """
        batches = (
            BatchStock.objects.filter(
                tenant=tenant,
                branch=branch,
                status=BatchStatus.ACTIVE,
            )
            .select_related("product")
            .all()
        )

        # Pre-cache storage locations by code for this branch
        locations_by_code = {
            loc.code: loc
            for loc in StorageLocation.objects.filter(tenant=tenant, branch=branch).select_related("warehouse")
        }

        policy = StorageZonePolicy.objects.filter(tenant=tenant, branch=branch, is_active=True).first()

        total = 0
        compliant = 0
        violations = []

        for b in batches:
            total += 1
            loc = locations_by_code.get(b.storage_location)
            if not loc:
                # Unmapped location code
                if getattr(b.product, "requires_cold_chain", False):
                    violations.append({
                        "batch_id": b.batch_id,
                        "batch_number": b.batch_number,
                        "product_name": b.product.brand_name,
                        "storage_location": b.storage_location,
                        "reason": f"Cold chain product stored in unverified location code '{b.storage_location}'.",
                    })
                else:
                    compliant += 1
                continue

            is_comp, reason = cls.validate_storage_assignment(b.product, loc, policy)
            if is_comp:
                compliant += 1
            else:
                violations.append({
                    "batch_id": b.batch_id,
                    "batch_number": b.batch_number,
                    "product_name": b.product.brand_name,
                    "storage_location": loc.code,
                    "location_name": loc.name,
                    "location_type": loc.location_type,
                    "reason": reason,
                })

        return {
            "branch_id": branch.branch_id,
            "branch_name": branch.name,
            "total_active_batches": total,
            "compliant_batches_count": compliant,
            "violation_batches_count": len(violations),
            "compliance_percentage": (compliant / total * 100.0) if total > 0 else 100.0,
            "violations": violations,
        }

