"""
backend/apps/core/regulated_stock_entry_service.py
Architecture Decision: ADR-0154 (P154).
Domain service for Regulated Stock Entry, Expiry, Manufacturing Date, and Lot Controls.
Enforces Bangladesh DGDA compliance, quarantine safeguards, and multi-tenant RBAC.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    RegulatedStockEntry,
    StorageCondition,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.regulated_stock_entry_service")


class RegulatedStockEntryService:
    """
    Core domain service managing regulated stock intake with manufacturing date,
    expiration date, lot reference, and storage policy governance.
    """

    LOT_PATTERN = re.compile(r"^[A-Za-z0-9\-/_.]+$")
    ALLOWED_CONDITIONS = [c.value for c in StorageCondition]

    @classmethod
    def _resolve_tenant(cls, user: PharmacyUser) -> Optional[Tenant]:
        tenant_id = getattr(user, "tenant_id", None)
        if tenant_id:
            return Tenant.objects.filter(tenant_id=tenant_id).first()
        return None

    @classmethod
    def _resolve_branch(cls, user: PharmacyUser, tenant: Optional[Tenant]) -> Optional[Branch]:
        branch_id = getattr(user, "branch_id", None)
        if branch_id and tenant:
            return Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
        return None

    @classmethod
    def check_permissions(cls, user: PharmacyUser, required_action: str = "read") -> None:
        """Enforces multi-tenant RBAC policies for regulated stock intake."""
        if user.is_superuser:
            return

        if required_action in ["write", "delete"]:
            if getattr(user, "role", None) in [UserRole.CASHIER]:
                raise PermissionDenied({
                    "error_code": "ERR_ROLE_UNAUTHORIZED",
                    "detail": "Cashier role has read-only access to stock entry records.",
                })

        tenant: Optional[Tenant] = cls._resolve_tenant(user)
        if tenant:
            if not tenant.is_active or tenant.status in [
                TenantLifecycleStatus.SUSPENDED,
                TenantLifecycleStatus.ARCHIVED,
            ]:
                raise PermissionDenied({
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": f"Tenant '{tenant.name}' subscription is suspended or terminated.",
                })

    @classmethod
    def get_stock_entry_schema(cls) -> Dict[str, Any]:
        """Returns dynamic schema and regulatory constraints for stock intake."""
        return {
            "schema_version": "1.0",
            "editor_type": "REGULATED_STOCK_ENTRY_FORM",
            "current_date": timezone.now().date().isoformat(),
            "supported_storage_conditions": [
                {
                    "code": c.value,
                    "label_en": c.label,
                    "label_bn": cls._translate_storage_condition(c.value),
                }
                for c in StorageCondition
            ],
            "default_quarantine_lead_days": 30,
            "min_quantity_received": 1,
            "min_unit_cost_bdt": 0.0001,
            "lot_reference_pattern": "^[A-Za-z0-9\\-_./]+$",
        }

    @classmethod
    def validate_intake_payload(cls, payload: Dict[str, Any], current_dt: Optional[date] = None) -> Dict[str, Any]:
        """
        Validates manufacturing date, expiration date, lot reference, and DGDA constraints.
        """
        if current_dt is None:
            current_dt = timezone.now().date()

        # 1. Product ID
        product_id = payload.get("product_id")
        if not product_id or not str(product_id).strip():
            raise ValidationError({
                "error_code": "ERR_MISSING_PRODUCT_ID",
                "detail": "product_id is mandatory.",
                "field": "product_id",
            })
        try:
            product = MedicineProduct.objects.get(pk=str(product_id).strip())
        except MedicineProduct.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_PRODUCT_NOT_FOUND",
                "detail": f"Product '{product_id}' does not exist.",
                "field": "product_id",
            })

        # 2. Lot Reference
        lot_reference = payload.get("lot_reference", "").strip()
        if not lot_reference:
            raise ValidationError({
                "error_code": "ERR_MISSING_LOT_REFERENCE",
                "detail": "lot_reference (batch / lot number) is required.",
                "field": "lot_reference",
            })
        if len(lot_reference) < 2 or len(lot_reference) > 64:
            raise ValidationError({
                "error_code": "ERR_INVALID_LOT_LENGTH",
                "detail": "lot_reference must be between 2 and 64 characters.",
                "field": "lot_reference",
            })
        if not cls.LOT_PATTERN.match(lot_reference):
            raise ValidationError({
                "error_code": "ERR_INVALID_LOT_FORMAT",
                "detail": "lot_reference contains invalid characters. Allowed: alphanumeric, hyphens, underscores, slashes, periods.",
                "field": "lot_reference",
            })

        # 3. Manufacturing Date
        mfg_raw = payload.get("manufacturing_date")
        if not mfg_raw:
            raise ValidationError({
                "error_code": "ERR_MISSING_MANUFACTURING_DATE",
                "detail": "manufacturing_date is mandatory.",
                "field": "manufacturing_date",
            })
        try:
            mfg_date = date.fromisoformat(str(mfg_raw))
        except ValueError:
            raise ValidationError({
                "error_code": "ERR_INVALID_DATE_FORMAT",
                "detail": "manufacturing_date must be in ISO format (YYYY-MM-DD).",
                "field": "manufacturing_date",
            })

        if mfg_date > current_dt:
            raise ValidationError({
                "error_code": "ERR_FUTURE_MANUFACTURING_DATE",
                "detail": f"manufacturing_date ({mfg_date}) cannot be in the future (current: {current_dt}).",
                "field": "manufacturing_date",
            })

        # 4. Expiration Date
        exp_raw = payload.get("expiry_date")
        if not exp_raw:
            raise ValidationError({
                "error_code": "ERR_MISSING_EXPIRY_DATE",
                "detail": "expiry_date is mandatory.",
                "field": "expiry_date",
            })
        try:
            exp_date = date.fromisoformat(str(exp_raw))
        except ValueError:
            raise ValidationError({
                "error_code": "ERR_INVALID_DATE_FORMAT",
                "detail": "expiry_date must be in ISO format (YYYY-MM-DD).",
                "field": "expiry_date",
            })

        if exp_date <= mfg_date:
            raise ValidationError({
                "error_code": "ERR_EXPIRY_BEFORE_MANUFACTURING",
                "detail": f"expiry_date ({exp_date}) must be strictly after manufacturing_date ({mfg_date}).",
                "field": "expiry_date",
            })

        is_quarantined = bool(payload.get("is_quarantined", False))
        quarantine_lead_days = int(payload.get("quarantine_lead_days", 30))
        if quarantine_lead_days < 0 or quarantine_lead_days > 365:
            raise ValidationError({
                "error_code": "ERR_INVALID_QUARANTINE_DAYS",
                "detail": "quarantine_lead_days must be between 0 and 365 days.",
                "field": "quarantine_lead_days",
            })

        # Check if already expired for normal stock intake
        days_until_expiry = (exp_date - current_dt).days
        if days_until_expiry <= 0 and not is_quarantined:
            raise ValidationError({
                "error_code": "ERR_STOCK_ALREADY_EXPIRED",
                "detail": f"Cannot enter expired medicine ({exp_date}) into active dispensing stock. Must be quarantined.",
                "field": "expiry_date",
            })

        # Automatic quarantine alert if within lead days
        if days_until_expiry <= quarantine_lead_days and not is_quarantined:
            is_quarantined = True

        # 5. Quantity Received
        try:
            qty = int(payload.get("quantity_received", 1))
            if qty < 1:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_QUANTITY",
                "detail": "quantity_received must be a positive integer (>= 1).",
                "field": "quantity_received",
            })

        # 6. Unit Cost
        try:
            cost = Decimal(str(payload.get("unit_cost_bdt", "0.00")))
            if cost <= Decimal("0"):
                raise ValueError()
        except (InvalidOperation, ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_UNIT_COST",
                "detail": "unit_cost_bdt must be greater than 0.",
                "field": "unit_cost_bdt",
            })

        # 7. Storage Condition
        storage_cond = payload.get("storage_condition", StorageCondition.ROOM_TEMPERATURE)
        if storage_cond not in cls.ALLOWED_CONDITIONS:
            raise ValidationError({
                "error_code": "ERR_INVALID_STORAGE_CONDITION",
                "detail": f"Invalid storage_condition: '{storage_cond}'. Allowed: {cls.ALLOWED_CONDITIONS}",
                "field": "storage_condition",
            })

        # 8. Reconstitution Shelf Life
        reconstitute_days = payload.get("reconstitution_shelf_life_days")
        if reconstitute_days is not None:
            try:
                rec_int = int(reconstitute_days)
                if rec_int < 1 or rec_int > 365:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValidationError({
                    "error_code": "ERR_INVALID_RECONSTITUTION_DAYS",
                    "detail": "reconstitution_shelf_life_days must be between 1 and 365.",
                    "field": "reconstitution_shelf_life_days",
                })
        else:
            rec_int = None

        shelf_life_days = (exp_date - mfg_date).days

        return {
            "product_id": str(product.product_id),
            "product_brand_name": product.brand_name,
            "lot_reference": lot_reference,
            "manufacturing_date": mfg_date.isoformat(),
            "expiry_date": exp_date.isoformat(),
            "quantity_received": qty,
            "unit_cost_bdt": float(cost),
            "storage_condition": storage_cond,
            "quarantine_lead_days": quarantine_lead_days,
            "is_quarantined": is_quarantined,
            "reconstitution_shelf_life_days": rec_int,
            "shelf_life_days": shelf_life_days,
            "supplier_reference": str(payload.get("supplier_reference", "")).strip(),
            "entry_notes": str(payload.get("entry_notes", "")).strip(),
            "is_active": bool(payload.get("is_active", True)),
        }

    @classmethod
    @transaction.atomic
    def create_stock_entry(
        cls,
        payload: Dict[str, Any],
        user: PharmacyUser,
    ) -> Dict[str, Any]:
        """
        Creates an audited regulated stock intake record and synchronizes BatchStock inventory.
        """
        cls.check_permissions(user, required_action="write")
        tenant: Optional[Tenant] = cls._resolve_tenant(user)
        if not tenant:
            raise ValidationError({
                "error_code": "ERR_TENANT_REQUIRED",
                "detail": "Active tenant context required for stock intake.",
            })

        branch: Optional[Branch] = cls._resolve_branch(user, tenant)
        if not branch:
            branch = Branch.objects.filter(tenant=tenant).first()
            if not branch:
                raise ValidationError({
                    "error_code": "ERR_BRANCH_REQUIRED",
                    "detail": "Active branch required for stock intake.",
                })

        validated = cls.validate_intake_payload(payload)
        product = MedicineProduct.objects.get(pk=validated["product_id"])

        entry_id = payload.get("entry_id") or f"stk-ent-{uuid.uuid4().hex[:12]}"

        # Synchronize or Create BatchStock
        batch_number = validated["lot_reference"]
        batch = BatchStock.objects.filter(
            tenant=tenant,
            branch=branch,
            product=product,
            batch_number=batch_number,
        ).first()

        mfg_date = date.fromisoformat(validated["manufacturing_date"])
        exp_date = date.fromisoformat(validated["expiry_date"])
        unit_cost = Decimal(str(validated["unit_cost_bdt"]))

        if batch:
            batch.quantity_base_units += validated["quantity_received"]
            batch.manufacturing_date = mfg_date
            batch.expiry_date = exp_date
            batch.lot_reference = batch_number
            batch.storage_condition = validated["storage_condition"]
            batch.quarantine_lead_days = validated["quarantine_lead_days"]
            batch.is_quarantined = validated["is_quarantined"]
            batch.reconstitution_shelf_life_days = validated["reconstitution_shelf_life_days"]
            if validated["is_quarantined"]:
                batch.status = BatchStatus.QUARANTINED
            batch.save()
        else:
            batch_id = f"batch-{uuid.uuid4().hex[:12]}"
            batch_status = BatchStatus.QUARANTINED if validated["is_quarantined"] else BatchStatus.ACTIVE
            batch = BatchStock.objects.create(
                batch_id=batch_id,
                tenant=tenant,
                branch=branch,
                product=product,
                batch_number=batch_number,
                lot_reference=batch_number,
                quantity_base_units=validated["quantity_received"],
                expiry_date=exp_date,
                purchase_date=timezone.now().date(),
                cost_per_unit=unit_cost,
                status=batch_status,
                manufacturing_date=mfg_date,
                storage_condition=validated["storage_condition"],
                quarantine_lead_days=validated["quarantine_lead_days"],
                is_quarantined=validated["is_quarantined"],
                reconstitution_shelf_life_days=validated["reconstitution_shelf_life_days"],
            )

        stock_entry = RegulatedStockEntry.objects.create(
            entry_id=entry_id,
            tenant=tenant,
            branch=branch,
            product=product,
            batch=batch,
            lot_reference=validated["lot_reference"],
            manufacturing_date=mfg_date,
            expiry_date=exp_date,
            quantity_received=validated["quantity_received"],
            unit_cost_bdt=unit_cost,
            storage_condition=validated["storage_condition"],
            quarantine_lead_days=validated["quarantine_lead_days"],
            is_quarantined=validated["is_quarantined"],
            reconstitution_shelf_life_days=validated["reconstitution_shelf_life_days"],
            supplier_reference=validated["supplier_reference"],
            entry_notes=validated["entry_notes"],
            is_active=validated["is_active"],
        )

        AuditLogEntry.objects.create(
            tenant_id=tenant.tenant_id,
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="REGULATED_STOCK_ENTRY_RECORDED",
            resource_type="RegulatedStockEntry",
            resource_id=entry_id,
            metadata_json={
                "product_id": str(product.product_id),
                "lot_reference": validated["lot_reference"],
                "quantity_received": validated["quantity_received"],
                "expiry_date": validated["expiry_date"],
                "is_quarantined": validated["is_quarantined"],
            },
            status=AuditActionStatus.SUCCESS,
        )

        return cls._serialize_entry(stock_entry)

    @classmethod
    @transaction.atomic
    def update_stock_entry(
        cls,
        entry_id: str,
        payload: Dict[str, Any],
        user: PharmacyUser,
    ) -> Dict[str, Any]:
        """Updates storage condition, quarantine flag, or internal notes."""
        cls.check_permissions(user, required_action="write")
        try:
            entry = RegulatedStockEntry.objects.select_related("tenant", "batch").get(pk=entry_id)
        except RegulatedStockEntry.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_ENTRY_NOT_FOUND",
                "detail": f"RegulatedStockEntry '{entry_id}' not found.",
            })

        user_tenant_id = getattr(user, "tenant_id", None)
        if entry.tenant and user_tenant_id and str(entry.tenant.tenant_id) != str(user_tenant_id) and not user.is_superuser:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                "detail": "Cross-tenant stock modification prohibited.",
            })

        if "storage_condition" in payload:
            cond = payload["storage_condition"]
            if cond not in cls.ALLOWED_CONDITIONS:
                raise ValidationError({
                    "error_code": "ERR_INVALID_STORAGE_CONDITION",
                    "detail": f"Invalid storage_condition: '{cond}'.",
                })
            entry.storage_condition = cond
            if entry.batch:
                entry.batch.storage_condition = cond
                entry.batch.save(update_fields=["storage_condition"])

        if "is_quarantined" in payload:
            is_q = bool(payload["is_quarantined"])
            entry.is_quarantined = is_q
            if entry.batch:
                entry.batch.is_quarantined = is_q
                entry.batch.status = BatchStatus.QUARANTINED if is_q else BatchStatus.ACTIVE
                entry.batch.save(update_fields=["is_quarantined", "status"])

        if "entry_notes" in payload:
            entry.entry_notes = str(payload["entry_notes"]).strip()

        if "supplier_reference" in payload:
            entry.supplier_reference = str(payload["supplier_reference"]).strip()

        entry.save()

        AuditLogEntry.objects.create(
            tenant_id=entry.tenant.tenant_id if entry.tenant else "PLATFORM",
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="REGULATED_STOCK_ENTRY_UPDATED",
            resource_type="RegulatedStockEntry",
            resource_id=entry_id,
            metadata_json={"storage_condition": entry.storage_condition, "is_quarantined": entry.is_quarantined},
            status=AuditActionStatus.SUCCESS,
        )

        return cls._serialize_entry(entry)

    @classmethod
    def get_stock_entry(cls, entry_id: str, user: PharmacyUser) -> Dict[str, Any]:
        """Retrieves details of a specific stock entry record."""
        cls.check_permissions(user, required_action="read")
        try:
            entry = RegulatedStockEntry.objects.select_related("product", "tenant", "branch", "batch").get(pk=entry_id)
        except RegulatedStockEntry.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_ENTRY_NOT_FOUND",
                "detail": f"RegulatedStockEntry '{entry_id}' not found.",
            })

        user_tenant_id = getattr(user, "tenant_id", None)
        if entry.tenant and user_tenant_id and str(entry.tenant.tenant_id) != str(user_tenant_id) and not user.is_superuser:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                "detail": "Cross-tenant stock access prohibited.",
            })

        return cls._serialize_entry(entry)

    @classmethod
    def list_stock_entries(
        cls,
        user: PharmacyUser,
        product_id: Optional[str] = None,
        is_quarantined: Optional[bool] = None,
        lot_reference: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists stock entry records scoped to the tenant."""
        cls.check_permissions(user, required_action="read")
        qs = RegulatedStockEntry.objects.select_related("product", "tenant", "branch", "batch").filter(is_active=True)

        user_tenant: Optional[Tenant] = cls._resolve_tenant(user)
        if not user.is_superuser:
            if user_tenant:
                qs = qs.filter(tenant=user_tenant)
            else:
                qs = qs.none()

        if product_id:
            qs = qs.filter(product_id=product_id)

        if is_quarantined is not None:
            qs = qs.filter(is_quarantined=is_quarantined)

        if lot_reference:
            qs = qs.filter(lot_reference__icontains=lot_reference)

        return [cls._serialize_entry(e) for e in qs]

    @classmethod
    @transaction.atomic
    def archive_stock_entry(cls, entry_id: str, user: PharmacyUser, hard_delete: bool = False) -> Dict[str, Any]:
        """Archives or soft-deletes a stock entry record."""
        cls.check_permissions(user, required_action="delete")
        try:
            entry = RegulatedStockEntry.objects.get(pk=entry_id)
        except RegulatedStockEntry.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_ENTRY_NOT_FOUND",
                "detail": f"RegulatedStockEntry '{entry_id}' not found.",
            })

        user_tenant_id = getattr(user, "tenant_id", None)
        if entry.tenant and user_tenant_id and str(entry.tenant.tenant_id) != str(user_tenant_id) and not user.is_superuser:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                "detail": "Cross-tenant stock deletion prohibited.",
            })

        if hard_delete:
            entry.delete()
            status_msg = "DELETED"
        else:
            entry.is_active = False
            entry.save(update_fields=["is_active", "updated_at"])
            status_msg = "ARCHIVED"

        AuditLogEntry.objects.create(
            tenant_id=entry.tenant.tenant_id if entry.tenant else "PLATFORM",
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="REGULATED_STOCK_ENTRY_REMOVED",
            resource_type="RegulatedStockEntry",
            resource_id=entry_id,
            metadata_json={"status": status_msg},
            status=AuditActionStatus.SUCCESS,
        )

        return {"status": status_msg, "entry_id": entry_id}

    @classmethod
    def _serialize_entry(cls, entry: RegulatedStockEntry) -> Dict[str, Any]:
        """Converts model instance to JSON dictionary."""
        return {
            "entry_id": str(entry.entry_id),
            "tenant_id": str(entry.tenant.tenant_id) if entry.tenant else None,
            "branch_id": str(entry.branch.branch_id) if entry.branch else None,
            "product_id": str(entry.product_id),
            "product_brand_name": entry.product.brand_name if entry.product else "",
            "batch_id": str(entry.batch.batch_id) if entry.batch else None,
            "lot_reference": entry.lot_reference,
            "manufacturing_date": entry.manufacturing_date.isoformat(),
            "expiry_date": entry.expiry_date.isoformat(),
            "quantity_received": entry.quantity_received,
            "unit_cost_bdt": float(entry.unit_cost_bdt),
            "storage_condition": entry.storage_condition,
            "storage_condition_bn": cls._translate_storage_condition(entry.storage_condition),
            "quarantine_lead_days": entry.quarantine_lead_days,
            "is_quarantined": entry.is_quarantined,
            "reconstitution_shelf_life_days": entry.reconstitution_shelf_life_days,
            "shelf_life_days": entry.shelf_life_days,
            "is_expired": entry.is_expired,
            "supplier_reference": entry.supplier_reference,
            "entry_notes": entry.entry_notes,
            "is_active": entry.is_active,
            "created_at": entry.created_at.isoformat(),
            "updated_at": entry.updated_at.isoformat(),
        }

    @staticmethod
    def _translate_storage_condition(code: str) -> str:
        translations = {
            "ROOM_TEMPERATURE_15_25C": "স্বাভাবিক তাপমাত্রা (১৫°সে - ২৫°সে)",
            "COOL_8_15C": "শীতল তাপমাত্রা (৮°সে - ১৫°সে)",
            "COLD_CHAIN_2_8C": "কোল্ড চেইন রেফ্রিজারেশন (২°সে - ৮°সে)",
            "FREEZER_MINUS_20C": "ডিপ ফ্রিজার (-২০°সে)",
            "DO_NOT_FREEZE": "ফ্রিজ করবেন না",
            "PROTECT_FROM_LIGHT": "সরাসরি আলো থেকে সুরক্ষিত রাখুন",
        }
        return translations.get(code, code)
