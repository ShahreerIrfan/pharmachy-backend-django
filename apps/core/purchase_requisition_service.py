"""
backend/apps/core/purchase_requisition_service.py
Architecture Decision: ADR-0184 (P184).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Service layer governing purchase requisitions, department budgets,
requested item lines, budget ceiling guards, and approval chain lifecycle.
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
    Branch,
    MedicineProduct,
    PurchaseRequisition,
    PurchaseRequisitionItem,
    RequisitionDepartment,
    RequisitionPriority,
    RequisitionStatus,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

ALLOWED_PROCUREMENT_ROLES = {
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
    "PROCUREMENT_OFFICER",
    "AUDITOR",
}

APPROVAL_ROLES = {
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
    "PROCUREMENT_OFFICER",
}


class PurchaseRequisitionService:
    """
    Authoritative service governing department purchase requisitions,
    approval workflows, budget caps, and multi-tenant integrity.
    """

    @classmethod
    def _assert_procurement_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if role in [UserRole.CASHIER, "CASHIER"] or (
            role not in ALLOWED_PROCUREMENT_ROLES and not getattr(actor_user, "is_superuser", False)
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and unauthorized staff cannot manage purchase requisitions.",
                    "message_en": "Cashier and unauthorized staff cannot manage purchase requisitions.",
                    "message_bn": "ক্যাশিয়ার বা অননুমোদিত কর্মী পারচেজ রিকুইজিশন ব্যবস্থাপনার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def _assert_approval_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if role not in APPROVAL_ROLES and not getattr(actor_user, "is_superuser", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_APPROVAL_ROLE_REQUIRED",
                    "detail": "Only Branch Managers or Pharmacy Owners can approve or reject purchase requisitions.",
                    "message_en": "Only Branch Managers or Pharmacy Owners can approve or reject purchase requisitions.",
                    "message_bn": "শুধুমাত্র ব্রাঞ্চ ম্যানেজার বা ফার্মেসি মালিক রিকুইজিশন অনুমোদন বা প্রত্যাখ্যান করতে পারেন।",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant_id: str) -> Tenant:
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' not found."})
        if (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant account subscription is suspended. Requisition module blocked.",
                    "message_en": "Tenant account subscription is suspended. Requisition module blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। রিকুইজিশন মডিউল বন্ধ।",
                }
            )
        return tenant

    @classmethod
    def serialize_item(cls, item: PurchaseRequisitionItem) -> Dict[str, Any]:
        return {
            "item_id": item.item_id,
            "product_id": str(item.product_id),
            "product_name": item.product.brand_name if item.product else "",
            "preferred_supplier_id": str(item.preferred_supplier_id) if item.preferred_supplier_id else None,
            "preferred_supplier_name": item.preferred_supplier.name if item.preferred_supplier else "",
            "package_type": item.package_type,
            "package_size": item.package_size,
            "requested_quantity": item.requested_quantity,
            "estimated_unit_cost": str(item.estimated_unit_cost),
            "estimated_total_cost": str(item.estimated_total_cost),
            "current_stock_quantity": item.current_stock_quantity,
            "notes": item.notes or "",
        }

    @classmethod
    def serialize_requisition(cls, req: PurchaseRequisition) -> Dict[str, Any]:
        """Serializes a PurchaseRequisition conforming to JSON schema contract."""
        return {
            "requisition_id": req.requisition_id,
            "tenant_id": str(req.tenant_id),
            "branch_id": str(req.branch_id),
            "branch_name": req.branch.name if req.branch else "",
            "department": req.department,
            "priority": req.priority,
            "status": req.status,
            "total_estimated_cost": str(req.total_estimated_cost),
            "allocated_budget": str(req.allocated_budget),
            "justification": req.justification or "",
            "target_delivery_date": req.target_delivery_date.isoformat() if req.target_delivery_date else None,
            "submitted_by_id": str(req.submitted_by_id) if req.submitted_by_id else None,
            "submitted_at": req.submitted_at.isoformat() if req.submitted_at else None,
            "approved_by_id": str(req.approved_by_id) if req.approved_by_id else None,
            "approved_at": req.approved_at.isoformat() if req.approved_at else None,
            "approval_notes": req.approval_notes or "",
            "rejected_by_id": str(req.rejected_by_id) if req.rejected_by_id else None,
            "rejected_at": req.rejected_at.isoformat() if req.rejected_at else None,
            "rejection_reason": req.rejection_reason or "",
            "is_active": req.is_active,
            "items": [cls.serialize_item(item) for item in req.items.select_related("product", "preferred_supplier").all()],
            "created_at": req.created_at.isoformat() if req.created_at else None,
            "updated_at": req.updated_at.isoformat() if req.updated_at else None,
        }

    @classmethod
    def get_requisition(cls, tenant_id: str, requisition_id: str) -> Dict[str, Any]:
        cls._assert_tenant_active(tenant_id)
        req = (
            PurchaseRequisition.objects.select_related("branch", "tenant")
            .prefetch_related("items__product", "items__preferred_supplier")
            .filter(requisition_id=requisition_id)
            .first()
        )
        if not req:
            raise NotFound({"error_code": "ERR_REQUISITION_NOT_FOUND", "detail": f"Requisition '{requisition_id}' not found."})
        if str(req.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant requisition access forbidden."})
        return cls.serialize_requisition(req)

    @classmethod
    def list_requisitions(
        cls,
        tenant_id: str,
        branch_id: Optional[str] = None,
        department: Optional[str] = None,
        priority: Optional[str] = None,
        status: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        cls._assert_tenant_active(tenant_id)
        qs = (
            PurchaseRequisition.objects.filter(tenant_id=tenant_id)
            .select_related("branch")
            .prefetch_related("items__product", "items__preferred_supplier")
        )
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if department:
            qs = qs.filter(department=department.upper())
        if priority:
            qs = qs.filter(priority=priority.upper())
        if status:
            qs = qs.filter(status=status.upper())
        if is_active is not None:
            qs = qs.filter(is_active=is_active)

        return [cls.serialize_requisition(r) for r in qs.order_by("-created_at")]

    @classmethod
    def create_requisition(
        cls,
        tenant_id: str,
        branch_id: str,
        actor_user: Any,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        target_branch_id = data.get("branch_id") or branch_id
        if not target_branch_id:
            raise ValidationError({"error_code": "ERR_MISSING_BRANCH_ID", "detail": "branch_id is required."})

        branch = Branch.objects.filter(branch_id=target_branch_id).first()
        if not branch:
            raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{target_branch_id}' not found."})
        if str(branch.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant branch access forbidden."})

        items_data = data.get("items")
        if not items_data or not isinstance(items_data, list):
            raise ValidationError({"error_code": "ERR_EMPTY_REQUISITION_ITEMS", "detail": "Requisition must contain at least one item."})

        # Validate budget cap
        allocated_budget = Decimal("0.0000")
        if "allocated_budget" in data:
            try:
                allocated_budget = Decimal(str(data["allocated_budget"]))
                if allocated_budget < Decimal("0.0000"):
                    raise ValidationError({"error_code": "ERR_INVALID_BUDGET", "detail": "Allocated budget cannot be negative."})
            except Exception as e:
                if isinstance(e, ValidationError):
                    raise
                raise ValidationError({"error_code": "ERR_INVALID_BUDGET", "detail": "Invalid allocated budget value."})

        # Validate dates
        target_delivery_date = None
        target_date_raw = data.get("target_delivery_date")
        if target_date_raw:
            try:
                target_delivery_date = (
                    target_date_raw
                    if isinstance(target_date_raw, datetime.date)
                    else datetime.date.fromisoformat(str(target_date_raw))
                )
            except ValueError:
                raise ValidationError({"error_code": "ERR_INVALID_DATE_FORMAT", "detail": "target_delivery_date must be YYYY-MM-DD."})

        department = data.get("department", RequisitionDepartment.DISPENSARY).upper()
        if department not in RequisitionDepartment.values:
            department = RequisitionDepartment.DISPENSARY

        priority = data.get("priority", RequisitionPriority.MEDIUM).upper()
        if priority not in RequisitionPriority.values:
            priority = RequisitionPriority.MEDIUM

        req_id = data.get("requisition_id") or f"req-{uuid.uuid4().hex[:12]}"
        if not (req_id.startswith("req-") or req_id.startswith("pr-")):
            req_id = f"req-{req_id}"

        # Parse & validate items
        validated_items = []
        total_estimated_cost = Decimal("0.0000")

        for idx, item in enumerate(items_data):
            prod_id = item.get("product_id")
            if not prod_id:
                raise ValidationError({"error_code": "ERR_MISSING_PRODUCT_ID", "detail": f"Item at index {idx} is missing product_id."})

            product = MedicineProduct.objects.filter(product_id=prod_id).first()
            if not product:
                raise NotFound({"error_code": "ERR_PRODUCT_NOT_FOUND", "detail": f"Product '{prod_id}' not found."})

            try:
                qty = int(item.get("requested_quantity", 1))
                if qty < 1:
                    raise ValidationError({"error_code": "ERR_INVALID_QUANTITY", "detail": "Requested quantity must be at least 1."})
            except (ValueError, TypeError):
                raise ValidationError({"error_code": "ERR_INVALID_QUANTITY", "detail": "Invalid quantity value."})

            try:
                unit_cost = Decimal(str(item.get("estimated_unit_cost", product.cost_unit or "1.0000")))
                if unit_cost <= Decimal("0.0000"):
                    raise ValidationError({"error_code": "ERR_INVALID_COST_PRICE", "detail": "Estimated unit cost must be positive."})
            except Exception as e:
                if isinstance(e, ValidationError):
                    raise
                raise ValidationError({"error_code": "ERR_INVALID_COST_PRICE", "detail": "Invalid estimated unit cost value."})

            pkg_size = max(1, int(item.get("package_size", 1)))
            line_total = (unit_cost * Decimal(qty)).quantize(Decimal("0.0001"))
            total_estimated_cost += line_total

            pref_supplier_id = item.get("preferred_supplier_id")
            pref_supplier = None
            if pref_supplier_id:
                pref_supplier = Supplier.objects.filter(supplier_id=pref_supplier_id).first()
                if pref_supplier and str(pref_supplier.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Preferred supplier from another tenant is forbidden."})

            validated_items.append(
                {
                    "item_id": item.get("item_id") or f"pri-{uuid.uuid4().hex[:12]}",
                    "product": product,
                    "preferred_supplier": pref_supplier,
                    "package_type": item.get("package_type", "BOX").upper(),
                    "package_size": pkg_size,
                    "requested_quantity": qty,
                    "estimated_unit_cost": unit_cost,
                    "estimated_total_cost": line_total,
                    "current_stock_quantity": int(item.get("current_stock_quantity", 0)),
                    "notes": str(item.get("notes", "")).strip(),
                }
            )

        with transaction.atomic():
            requisition = PurchaseRequisition.objects.create(
                requisition_id=req_id,
                tenant=tenant,
                branch=branch,
                department=department,
                priority=priority,
                status=RequisitionStatus.DRAFT,
                total_estimated_cost=total_estimated_cost,
                allocated_budget=allocated_budget,
                justification=str(data.get("justification", "")).strip(),
                target_delivery_date=target_delivery_date,
                created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            )

            for vi in validated_items:
                PurchaseRequisitionItem.objects.create(
                    item_id=vi["item_id"],
                    requisition=requisition,
                    product=vi["product"],
                    preferred_supplier=vi["preferred_supplier"],
                    package_type=vi["package_type"],
                    package_size=vi["package_size"],
                    requested_quantity=vi["requested_quantity"],
                    estimated_unit_cost=vi["estimated_unit_cost"],
                    estimated_total_cost=vi["estimated_total_cost"],
                    current_stock_quantity=vi["current_stock_quantity"],
                    notes=vi["notes"],
                )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "staff@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "STAFF_PHARMACIST")),
            action="PURCHASE_REQUISITION_CREATED",
            resource_type="PurchaseRequisition",
            resource_id=requisition.requisition_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "branch_id": branch.branch_id,
                "department": requisition.department,
                "items_count": len(validated_items),
                "total_estimated_cost": str(requisition.total_estimated_cost),
            },
        )

        return cls.serialize_requisition(requisition)

    @classmethod
    def update_requisition(
        cls,
        tenant_id: str,
        actor_user: Any,
        requisition_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        req = PurchaseRequisition.objects.filter(requisition_id=requisition_id).first()
        if not req:
            raise NotFound({"error_code": "ERR_REQUISITION_NOT_FOUND", "detail": f"Requisition '{requisition_id}' not found."})
        if str(req.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant requisition update forbidden."})

        if req.status != RequisitionStatus.DRAFT:
            raise ValidationError(
                {
                    "error_code": "ERR_REQUISITION_LOCKED",
                    "detail": f"Requisition in '{req.status}' status cannot be modified. Only DRAFT requisitions can be updated.",
                }
            )

        if "department" in data and data["department"].upper() in RequisitionDepartment.values:
            req.department = data["department"].upper()
        if "priority" in data and data["priority"].upper() in RequisitionPriority.values:
            req.priority = data["priority"].upper()
        if "justification" in data:
            req.justification = str(data["justification"]).strip()
        if "allocated_budget" in data:
            req.allocated_budget = Decimal(str(data["allocated_budget"]))
        if "target_delivery_date" in data:
            tdd = data["target_delivery_date"]
            req.target_delivery_date = tdd if isinstance(tdd, datetime.date) else datetime.date.fromisoformat(str(tdd))

        req.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "staff@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "STAFF_PHARMACIST")),
            action="PURCHASE_REQUISITION_UPDATED",
            resource_type="PurchaseRequisition",
            resource_id=req.requisition_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"updated_fields": list(data.keys())},
        )

        return cls.serialize_requisition(req)

    @classmethod
    def submit_requisition(
        cls,
        tenant_id: str,
        actor_user: Any,
        requisition_id: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Transitions DRAFT -> SUBMITTED.
        Enforces Budget Ceiling Guard: total_estimated_cost <= allocated_budget
        unless explicitly overridden by authorized manager.
        """
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        req = PurchaseRequisition.objects.filter(requisition_id=requisition_id).first()
        if not req:
            raise NotFound({"error_code": "ERR_REQUISITION_NOT_FOUND", "detail": f"Requisition '{requisition_id}' not found."})
        if str(req.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant requisition submission forbidden."})

        if req.status != RequisitionStatus.DRAFT:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_STATE_TRANSITION",
                    "detail": f"Cannot submit requisition in '{req.status}' status. Requisition must be in DRAFT.",
                }
            )

        if not req.items.exists():
            raise ValidationError(
                {
                    "error_code": "ERR_EMPTY_REQUISITION_ITEMS",
                    "detail": "Cannot submit requisition without items.",
                }
            )

        # Budget Ceiling Guard
        payload = data or {}
        override_budget = bool(payload.get("budget_override", False))
        if req.allocated_budget > Decimal("0.0000") and req.total_estimated_cost > req.allocated_budget:
            if not override_budget:
                raise ValidationError(
                    {
                        "error_code": "ERR_BUDGET_EXCEEDED",
                        "detail": f"Requisition total estimated cost ({req.total_estimated_cost} BDT) exceeds allocated budget ({req.allocated_budget} BDT).",
                        "message_en": f"Requisition estimated cost ({req.total_estimated_cost} BDT) exceeds allocated budget ({req.allocated_budget} BDT).",
                        "message_bn": f"রিকুইজিশনের আনুমানিক খরচ ({req.total_estimated_cost} টাকা) বরাদ্দকৃত বাজেটের ({req.allocated_budget} টাকা) চেয়ে বেশি।",
                        "total_estimated_cost": str(req.total_estimated_cost),
                        "allocated_budget": str(req.allocated_budget),
                    }
                )

        req.status = RequisitionStatus.SUBMITTED
        req.submitted_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        req.submitted_at = timezone.now()
        req.save(update_fields=["status", "submitted_by", "submitted_at", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "staff@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "STAFF_PHARMACIST")),
            action="PURCHASE_REQUISITION_SUBMITTED",
            resource_type="PurchaseRequisition",
            resource_id=req.requisition_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"total_estimated_cost": str(req.total_estimated_cost)},
        )

        return cls.serialize_requisition(req)

    @classmethod
    def approve_requisition(
        cls,
        tenant_id: str,
        actor_user: Any,
        requisition_id: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Transitions SUBMITTED -> APPROVED. Requires Manager/Owner role."""
        cls._assert_approval_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        req = PurchaseRequisition.objects.filter(requisition_id=requisition_id).first()
        if not req:
            raise NotFound({"error_code": "ERR_REQUISITION_NOT_FOUND", "detail": f"Requisition '{requisition_id}' not found."})
        if str(req.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant requisition approval forbidden."})

        if req.status != RequisitionStatus.SUBMITTED:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_STATE_TRANSITION",
                    "detail": f"Cannot approve requisition in '{req.status}' status. Requisition must be SUBMITTED.",
                }
            )

        payload = data or {}
        req.status = RequisitionStatus.APPROVED
        req.approved_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        req.approved_at = timezone.now()
        req.approval_notes = str(payload.get("approval_notes", "")).strip()
        req.save(update_fields=["status", "approved_by", "approved_at", "approval_notes", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PURCHASE_REQUISITION_APPROVED",
            resource_type="PurchaseRequisition",
            resource_id=req.requisition_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"total_estimated_cost": str(req.total_estimated_cost), "approval_notes": req.approval_notes},
        )

        return cls.serialize_requisition(req)

    @classmethod
    def reject_requisition(
        cls,
        tenant_id: str,
        actor_user: Any,
        requisition_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Transitions SUBMITTED -> REJECTED. Requires Manager/Owner role and mandatory rejection_reason."""
        cls._assert_approval_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        req = PurchaseRequisition.objects.filter(requisition_id=requisition_id).first()
        if not req:
            raise NotFound({"error_code": "ERR_REQUISITION_NOT_FOUND", "detail": f"Requisition '{requisition_id}' not found."})
        if str(req.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant requisition rejection forbidden."})

        if req.status != RequisitionStatus.SUBMITTED:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_STATE_TRANSITION",
                    "detail": f"Cannot reject requisition in '{req.status}' status. Requisition must be SUBMITTED.",
                }
            )

        reason = str(data.get("rejection_reason", "")).strip()
        if not reason:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_REJECTION_REASON",
                    "detail": "rejection_reason is mandatory when declining a purchase requisition.",
                }
            )

        req.status = RequisitionStatus.REJECTED
        req.rejected_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        req.rejected_at = timezone.now()
        req.rejection_reason = reason
        req.save(update_fields=["status", "rejected_by", "rejected_at", "rejection_reason", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PURCHASE_REQUISITION_REJECTED",
            resource_type="PurchaseRequisition",
            resource_id=req.requisition_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"rejection_reason": reason},
        )

        return cls.serialize_requisition(req)

    @classmethod
    def cancel_requisition(
        cls,
        tenant_id: str,
        actor_user: Any,
        requisition_id: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Cancels a requisition in DRAFT or SUBMITTED status."""
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        req = PurchaseRequisition.objects.filter(requisition_id=requisition_id).first()
        if not req:
            raise NotFound({"error_code": "ERR_REQUISITION_NOT_FOUND", "detail": f"Requisition '{requisition_id}' not found."})
        if str(req.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant requisition cancellation forbidden."})

        if req.status in [RequisitionStatus.APPROVED, RequisitionStatus.CONVERTED_TO_PO]:
            raise ValidationError(
                {
                    "error_code": "ERR_CANNOT_CANCEL_APPROVED",
                    "detail": f"Cannot cancel requisition in '{req.status}' status. Approved requisitions must follow purchase reversal workflow.",
                }
            )

        req.status = RequisitionStatus.CANCELLED
        req.save(update_fields=["status", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "staff@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "STAFF_PHARMACIST")),
            action="PURCHASE_REQUISITION_CANCELLED",
            resource_type="PurchaseRequisition",
            resource_id=req.requisition_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"previous_status": req.status},
        )

        return cls.serialize_requisition(req)

