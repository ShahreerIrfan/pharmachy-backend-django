"""
Architecture Decision: ADR-0166 (P166)
Stable Medicine Batch Entity, Expiry Lifecycle & Statutory Recall Integration Service.
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
    CatalogueLifecycleNotice,
    CatalogueLifecycleStatus,
    MedicineProduct,
    RecallScope,
    StorageCondition,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

ALLOWED_MANAGE_ROLES = {
    UserRole.SUPERADMIN,
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.STAFF_PHARMACIST,
    UserRole.INVENTORY_CLERK,
    "SUPERADMIN",
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "STAFF_PHARMACIST",
    "INVENTORY_CLERK",
    "OWNER_ADMIN",
    "ADMIN",
}


class BatchEntityService:
    """
    Manages complete pharmaceutical batch identity lifecycle,
    expiry constraints, DGDA recall links, and dispensability validation.
    """

    @classmethod
    def _assert_manage_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in ALLOWED_MANAGE_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Only Pharmacists, Branch Managers, or Owners may manage batch records.",
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
                    "detail": "Tenant subscription is suspended. Inventory management is locked.",
                    "error_code": "ERR_TENANT_SUSPENDED",
                }
            )

    @classmethod
    def check_recall_for_batch(cls, batch: BatchStock) -> Optional[CatalogueLifecycleNotice]:
        """
        Dynamically finds if any active statutory recall or market withdrawal
        notice covers this batch's product and batch number.
        """
        notices = CatalogueLifecycleNotice.objects.filter(
            product_id=batch.product_id,
            is_active=True,
            lifecycle_status__in=[
                CatalogueLifecycleStatus.RECALLED,
                CatalogueLifecycleStatus.WITHDRAWN,
            ],
        )

        for notice in notices:
            # Check tenant scope
            if (
                notice.tenant_id_str
                and notice.tenant_id_str not in ("platform", "", None)
                and notice.tenant_id_str != str(batch.tenant_id)
            ):
                continue

            if notice.recall_scope in (RecallScope.SPECIFIC_BATCHES, "SPECIFIC_BATCHES", "BATCH_SPECIFIC"):
                affected = notice.affected_batch_numbers or []
                if batch.batch_number in affected:
                    return notice
            else:
                # Nationwide / all batches of this product
                return notice

        return None

    @classmethod
    def serialize_batch(cls, batch: BatchStock) -> Dict[str, Any]:
        """Serializes batch into standardized contract schema."""
        return {
            "batch_id": batch.batch_id,
            "tenant_id": str(batch.tenant_id),
            "branch_id": str(batch.branch_id),
            "product_id": str(batch.product_id),
            "product_brand_name": batch.product.brand_name if batch.product else "",
            "batch_number": batch.batch_number,
            "manufacturer_name": batch.manufacturer_name or (batch.product.manufacturer if batch.product else ""),
            "quantity_base_units": batch.quantity_base_units,
            "cost_per_unit": str(batch.cost_per_unit),
            "manufacturing_date": batch.manufacturing_date.isoformat() if batch.manufacturing_date else None,
            "expiry_date": batch.expiry_date.isoformat() if batch.expiry_date else None,
            "status": batch.status,
            "storage_condition": batch.storage_condition,
            "lot_reference": batch.lot_reference,
            "quarantine_lead_days": batch.quarantine_lead_days,
            "is_quarantined": batch.is_quarantined,
            "recall_notice_id": batch.recall_notice_id,
            "recall_reason": batch.recall_reason or "",
            "is_dispensable": batch.is_dispensable,
            "is_expired": batch.is_expired,
            "is_recalled": batch.is_recalled,
            "is_active": batch.is_active,
            "created_at": batch.created_at.isoformat() if batch.created_at else None,
            "updated_at": batch.updated_at.isoformat() if batch.updated_at else None,
        }

    @classmethod
    def list_batches(
        cls,
        user: Any,
        tenant_id: str,
        branch_id: Optional[str] = None,
        product_id: Optional[str] = None,
        status_filter: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists batches for an authorized tenant/branch with FEFO ordering."""
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})

        # Check tenant isolation
        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        qs = BatchStock.objects.select_related("product", "recall_notice").filter(
            tenant=tenant,
            is_active=True,
        )

        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if product_id:
            qs = qs.filter(product_id=product_id)
        if status_filter:
            qs = qs.filter(status=status_filter)
        if search:
            qs = qs.filter(batch_number__icontains=search)

        # Order by FEFO (nearest expiry first)
        qs = qs.order_by("expiry_date", "batch_number")

        results = []
        for batch in qs:
            # Refresh recall status if not explicitly recalled
            if batch.status != BatchStatus.RECALLED and not batch.recall_notice_id:
                notice = cls.check_recall_for_batch(batch)
                if notice:
                    batch.status = BatchStatus.RECALLED
                    batch.recall_notice = notice
                    batch.recall_reason = notice.reason or "Statutory recall notice"
                    batch.save(update_fields=["status", "recall_notice", "recall_reason"])
            results.append(cls.serialize_batch(batch))

        return results

    @classmethod
    def get_batch(cls, user: Any, tenant_id: str, batch_id: str) -> Dict[str, Any]:
        """Retrieves a single batch with auto-resolved recall status."""
        batch = (
            BatchStock.objects.select_related("product", "recall_notice", "tenant", "branch")
            .filter(batch_id=batch_id, tenant_id=tenant_id)
            .first()
        )
        if not batch:
            raise NotFound({"detail": f"Batch {batch_id} not found.", "error_code": "ERR_BATCH_NOT_FOUND"})

        # Cross tenant check
        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        # Check recall notice
        if batch.status != BatchStatus.RECALLED and not batch.recall_notice_id:
            notice = cls.check_recall_for_batch(batch)
            if notice:
                batch.status = BatchStatus.RECALLED
                batch.recall_notice = notice
                batch.recall_reason = notice.reason or "Statutory recall notice"
                batch.save(update_fields=["status", "recall_notice", "recall_reason"])

        return cls.serialize_batch(batch)

    @classmethod
    @transaction.atomic
    def create_batch(cls, user: Any, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Creates a new medicine batch with strict expiry invariants,
        duplicate checks, and recall detection.
        """
        cls._assert_manage_role(user)

        tenant_id = data.get("tenant_id") or getattr(user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant_id": "tenant_id is required.", "error_code": "ERR_MISSING_TENANT"})

        # Cross-tenant check
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
            raise NotFound({"detail": "Branch not found for tenant.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        product_id = data.get("product_id")
        if not product_id:
            raise ValidationError({"product_id": "product_id is required.", "error_code": "ERR_MISSING_PRODUCT"})
        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise NotFound({"detail": "Medicine product not found.", "error_code": "ERR_PRODUCT_NOT_FOUND"})

        batch_number = (data.get("batch_number") or "").strip()
        if not batch_number:
            raise ValidationError({"batch_number": "batch_number cannot be empty.", "error_code": "ERR_EMPTY_BATCH_NUMBER"})

        # Check duplicate batch number within branch
        if BatchStock.objects.filter(tenant=tenant, branch=branch, product=product, batch_number=batch_number).exists():
            raise ValidationError(
                {
                    "batch_number": f"Batch '{batch_number}' already exists in this branch.",
                    "error_code": "ERR_DUPLICATE_BATCH",
                }
            )

        # Dates validation
        expiry_raw = data.get("expiry_date")
        if not expiry_raw:
            raise ValidationError({"expiry_date": "expiry_date is required.", "error_code": "ERR_MISSING_EXPIRY_DATE"})

        try:
            if isinstance(expiry_raw, datetime.date):
                expiry_date = expiry_raw
            else:
                expiry_date = datetime.date.fromisoformat(str(expiry_raw))
        except ValueError:
            raise ValidationError({"expiry_date": "Invalid date format. Use YYYY-MM-DD.", "error_code": "ERR_INVALID_DATE"})

        mfg_date = None
        mfg_raw = data.get("manufacturing_date")
        if mfg_raw:
            try:
                if isinstance(mfg_raw, datetime.date):
                    mfg_date = mfg_raw
                else:
                    mfg_date = datetime.date.fromisoformat(str(mfg_raw))
            except ValueError:
                raise ValidationError({"manufacturing_date": "Invalid date format. Use YYYY-MM-DD.", "error_code": "ERR_INVALID_DATE"})

            if expiry_date <= mfg_date:
                raise ValidationError(
                    {
                        "expiry_date": "Expiry date must be strictly after manufacturing date.",
                        "error_code": "ERR_EXPIRY_BEFORE_MANUFACTURING",
                    }
                )

        today = timezone.now().date()
        status_input = data.get("status", BatchStatus.ACTIVE)
        is_quarantined = bool(data.get("is_quarantined", False))

        # Check expired on creation
        if expiry_date <= today:
            if status_input == BatchStatus.ACTIVE and not is_quarantined:
                raise ValidationError(
                    {
                        "expiry_date": f"Expiry date ({expiry_date}) is in the past. New batch cannot be active; it must be quarantined.",
                        "error_code": "ERR_BATCH_EXPIRED_ON_CREATION",
                    }
                )
            status_input = BatchStatus.QUARANTINED
            is_quarantined = True

        # Money / Cost
        cost_raw = data.get("cost_per_unit", "0.00")
        try:
            cost_per_unit = Decimal(str(cost_raw))
            if cost_per_unit < Decimal("0.00"):
                raise ValueError()
        except Exception:
            raise ValidationError({"cost_per_unit": "Invalid cost_per_unit; must be non-negative Decimal.", "error_code": "ERR_INVALID_COST"})

        # Quantity
        quantity = int(data.get("quantity_base_units", 0))
        if quantity < 0:
            raise ValidationError({"quantity_base_units": "Quantity cannot be negative.", "error_code": "ERR_INVALID_QUANTITY"})

        batch_id = data.get("batch_id") or f"bat-{uuid.uuid4().hex[:12]}"
        storage_condition = data.get("storage_condition", StorageCondition.ROOM_TEMPERATURE)
        lot_reference = data.get("lot_reference", "")
        manufacturer_name = data.get("manufacturer_name", "") or product.manufacturer

        batch = BatchStock(
            batch_id=batch_id,
            tenant=tenant,
            branch=branch,
            product=product,
            batch_number=batch_number,
            quantity_base_units=quantity,
            expiry_date=expiry_date,
            manufacturing_date=mfg_date,
            purchase_date=data.get("purchase_date") or today,
            cost_per_unit=cost_per_unit,
            status=status_input,
            storage_condition=storage_condition,
            lot_reference=lot_reference,
            quarantine_lead_days=int(data.get("quarantine_lead_days", 30)),
            is_quarantined=is_quarantined,
            manufacturer_name=manufacturer_name,
            is_active=True,
        )

        # Check for matching active recall notices
        notice = cls.check_recall_for_batch(batch)
        if notice:
            batch.status = BatchStatus.RECALLED
            batch.recall_notice = notice
            batch.recall_reason = notice.reason or "Statutory recall notice"

        batch.save()

        # Audit log entry
        AuditLogEntry.objects.create(
            entry_id=uuid.uuid4(),
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "system@batch"),
            actor_role=getattr(user, "role", "PHARMACIST"),
            action="BATCH_CREATED",
            resource_type="BatchStock",
            resource_id=batch.batch_id,
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            status=AuditActionStatus.SUCCESS,
            ip_address="127.0.0.1",
            after_state={
                "batch_id": batch.batch_id,
                "batch_number": batch.batch_number,
                "status": batch.status,
                "expiry_date": batch.expiry_date.isoformat(),
                "quantity": batch.quantity_base_units,
            },
        )

        return cls.serialize_batch(batch)

    @classmethod
    @transaction.atomic
    def update_batch(cls, user: Any, tenant_id: str, batch_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Audited partial update of non-key batch metadata."""
        cls._assert_manage_role(user)

        batch = BatchStock.objects.select_related("tenant", "branch", "product").filter(batch_id=batch_id, tenant_id=tenant_id).first()
        if not batch:
            raise NotFound({"detail": f"Batch {batch_id} not found.", "error_code": "ERR_BATCH_NOT_FOUND"})

        # Cross tenant check
        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        cls._assert_tenant_active(batch.tenant)

        payload_before = cls.serialize_batch(batch)

        if "manufacturer_name" in data:
            batch.manufacturer_name = data["manufacturer_name"]
        if "lot_reference" in data:
            batch.lot_reference = data["lot_reference"]
        if "storage_condition" in data:
            batch.storage_condition = data["storage_condition"]
        if "quarantine_lead_days" in data:
            batch.quarantine_lead_days = int(data["quarantine_lead_days"])
        if "cost_per_unit" in data:
            try:
                batch.cost_per_unit = Decimal(str(data["cost_per_unit"]))
            except Exception:
                raise ValidationError({"cost_per_unit": "Invalid cost_per_unit.", "error_code": "ERR_INVALID_COST"})

        # Date updates
        if "expiry_date" in data or "manufacturing_date" in data:
            new_exp = batch.expiry_date
            new_mfg = batch.manufacturing_date
            if "expiry_date" in data:
                try:
                    new_exp = datetime.date.fromisoformat(str(data["expiry_date"]))
                except ValueError:
                    raise ValidationError({"expiry_date": "Invalid date format.", "error_code": "ERR_INVALID_DATE"})
            if "manufacturing_date" in data and data["manufacturing_date"]:
                try:
                    new_mfg = datetime.date.fromisoformat(str(data["manufacturing_date"]))
                except ValueError:
                    raise ValidationError({"manufacturing_date": "Invalid date format.", "error_code": "ERR_INVALID_DATE"})

            if new_mfg and new_exp <= new_mfg:
                raise ValidationError(
                    {
                        "expiry_date": "Expiry date must be strictly after manufacturing date.",
                        "error_code": "ERR_EXPIRY_BEFORE_MANUFACTURING",
                    }
                )

            batch.expiry_date = new_exp
            batch.manufacturing_date = new_mfg

        batch.save()

        # Audit log entry
        AuditLogEntry.objects.create(
            entry_id=uuid.uuid4(),
            tenant_id=tenant_id,
            branch_id=batch.branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "system@batch"),
            actor_role=getattr(user, "role", "PHARMACIST"),
            action="BATCH_UPDATED",
            resource_type="BatchStock",
            resource_id=batch.batch_id,
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            status=AuditActionStatus.SUCCESS,
            ip_address="127.0.0.1",
            before_state=payload_before,
            after_state=cls.serialize_batch(batch),
        )

        return cls.serialize_batch(batch)

    @classmethod
    @transaction.atomic
    def transition_status(
        cls,
        user: Any,
        tenant_id: str,
        batch_id: str,
        target_status: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        """
        Executes audited lifecycle status transitions on a batch:
        ACTIVE -> QUARANTINED, QUARANTINED -> ACTIVE, RECALLED, ARCHIVED.
        """
        cls._assert_manage_role(user)

        batch = BatchStock.objects.select_related("tenant", "branch", "product").filter(batch_id=batch_id, tenant_id=tenant_id).first()
        if not batch:
            raise NotFound({"detail": f"Batch {batch_id} not found.", "error_code": "ERR_BATCH_NOT_FOUND"})

        # Cross tenant check
        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        cls._assert_tenant_active(batch.tenant)

        if target_status not in BatchStatus.values:
            raise ValidationError(
                {
                    "target_status": f"Invalid status '{target_status}'. Permitted: {list(BatchStatus.values)}",
                    "error_code": "ERR_INVALID_STATUS",
                }
            )

        current_status = batch.status
        today = timezone.now().date()
        payload_before = cls.serialize_batch(batch)

        # Invariant checks
        if current_status == BatchStatus.ARCHIVED:
            raise ValidationError(
                {
                    "detail": "Archived batches are sealed and cannot transition to other statuses.",
                    "error_code": "ERR_BATCH_ALREADY_ARCHIVED",
                }
            )

        if target_status == BatchStatus.ACTIVE:
            # Cannot activate expired batch
            if batch.expiry_date <= today:
                raise ValidationError(
                    {
                        "detail": f"Cannot activate expired batch (Expiry: {batch.expiry_date}).",
                        "error_code": "ERR_CANNOT_ACTIVATE_EXPIRED_BATCH",
                    }
                )
            # Cannot activate recalled batch if recall notice is still active
            notice = cls.check_recall_for_batch(batch)
            if notice:
                raise ValidationError(
                    {
                        "detail": f"Cannot activate batch under active statutory recall ({notice.notice_number}).",
                        "error_code": "ERR_CANNOT_ACTIVATE_RECALLED_BATCH",
                    }
                )
            batch.is_quarantined = False

        elif target_status == BatchStatus.QUARANTINED:
            batch.is_quarantined = True

        elif target_status == BatchStatus.RECALLED:
            batch.recall_reason = reason or "Regulatory recall enforced"

        elif target_status == BatchStatus.ARCHIVED:
            batch.is_active = False

        batch.status = target_status
        batch.save()

        # Audit log entry
        AuditLogEntry.objects.create(
            entry_id=uuid.uuid4(),
            tenant_id=tenant_id,
            branch_id=batch.branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "system@batch"),
            actor_role=getattr(user, "role", "PHARMACIST"),
            action="BATCH_STATUS_TRANSITION",
            resource_type="BatchStock",
            resource_id=batch.batch_id,
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            status=AuditActionStatus.SUCCESS,
            ip_address="127.0.0.1",
            before_state=payload_before,
            after_state=cls.serialize_batch(batch),
        )

        return cls.serialize_batch(batch)

    @classmethod
    def validate_dispense(
        cls,
        user: Any,
        tenant_id: str,
        batch_id: str,
        requested_units: int = 1,
    ) -> Dict[str, Any]:
        """
        Validates whether a batch can be dispensed at POS checkout lane.
        Hard-blocks recalled, expired, quarantined, archived, or depleted batches.
        """
        batch = (
            BatchStock.objects.select_related("product", "recall_notice")
            .filter(batch_id=batch_id, tenant_id=tenant_id)
            .first()
        )
        if not batch:
            return {
                "is_dispensable": False,
                "error_code": "ERR_BATCH_NOT_FOUND",
                "message": f"Batch {batch_id} not found.",
            }

        # Check tenant isolation
        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            return {
                "is_dispensable": False,
                "error_code": "ERR_TENANT_MISMATCH",
                "message": "Cross-tenant access prohibited.",
            }

        # Check statutory recall
        notice = cls.check_recall_for_batch(batch)
        if notice or batch.status == BatchStatus.RECALLED or batch.recall_notice_id:
            reason = batch.recall_reason or (notice.reason if notice else "Statutory recall")
            return {
                "is_dispensable": False,
                "error_code": "ERR_BATCH_RECALLED",
                "message": f"Batch {batch.batch_number} is subject to regulatory recall: {reason}.",
                "batch_id": batch.batch_id,
                "recall_notice_id": batch.recall_notice_id or (notice.notice_id if notice else None),
            }

        # Check expiry
        today = timezone.now().date()
        if batch.expiry_date <= today or batch.status == BatchStatus.EXPIRED:
            return {
                "is_dispensable": False,
                "error_code": "ERR_BATCH_EXPIRED",
                "message": f"Batch {batch.batch_number} expired on {batch.expiry_date}. Dispensing strictly prohibited.",
                "batch_id": batch.batch_id,
            }

        # Check quarantine
        if batch.is_quarantined or batch.status == BatchStatus.QUARANTINED:
            return {
                "is_dispensable": False,
                "error_code": "ERR_BATCH_QUARANTINED",
                "message": f"Batch {batch.batch_number} is quarantined. Clinical clearance required.",
                "batch_id": batch.batch_id,
            }

        # Check archived
        if batch.status == BatchStatus.ARCHIVED or not batch.is_active:
            return {
                "is_dispensable": False,
                "error_code": "ERR_BATCH_ARCHIVED",
                "message": f"Batch {batch.batch_number} is archived.",
                "batch_id": batch.batch_id,
            }

        # Check stock quantity
        if batch.quantity_base_units < requested_units:
            return {
                "is_dispensable": False,
                "error_code": "ERR_INSUFFICIENT_STOCK",
                "message": f"Insufficient stock: requested {requested_units}, available {batch.quantity_base_units}.",
                "batch_id": batch.batch_id,
                "available_units": batch.quantity_base_units,
            }

        return {
            "is_dispensable": True,
            "error_code": None,
            "message": "Batch verified and ready for dispensing.",
            "batch_id": batch.batch_id,
            "batch_number": batch.batch_number,
            "product_brand_name": batch.product.brand_name,
            "expiry_date": batch.expiry_date.isoformat(),
            "cost_per_unit": str(batch.cost_per_unit),
            "available_units": batch.quantity_base_units,
        }

