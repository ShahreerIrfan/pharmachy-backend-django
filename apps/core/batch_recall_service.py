"""
backend/apps/core/batch_recall_service.py

Architecture Decision: ADR-0256 (P256).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Service layer for Batch Recall Notification, Stock Quarantine, Reservation Eviction,
Real-time POS Dispensing Lock, and Historical Sales Lookup without rewriting history.
"""

from decimal import Decimal
import datetime
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchRecallNotification,
    BatchStatus,
    BatchStock,
    Branch,
    PosSaleAllocation,
    RecallClass,
    RecallNoticeStatus,
    RecallSource,
    SaleLotAllocation,
    StockReservation,
    StockReservationStatus,
    Tenant,
)
from apps.core.pos_domain_service import PosDomainService


class BatchRecallService:
    """
    Domain service for DGDA and manufacturer batch recalls.
    Enforces immediate automated stock quarantine, reservation eviction, POS dispensing locks,
    and historical sales impact audit without altering immutable sales history.
    """

    ALLOWED_READ_ROLES = ["SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST", "CASHIER"]
    ALLOWED_MANAGE_ROLES = ["SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST"]

    @classmethod
    def serialize_recall(cls, record: BatchRecallNotification, user: Any = None) -> Dict[str, Any]:
        """
        Serializes BatchRecallNotification with audit metadata.
        """
        return {
            "recall_id": record.recall_id,
            "tenant_id": str(record.tenant_id),
            "branch_id": str(record.branch_id) if record.branch_id else None,
            "branch_name": record.branch.name if record.branch else "All Branches",
            "product_id": str(record.product_id) if record.product_id else None,
            "product_name": record.product_name,
            "batch_number": record.batch_number,
            "recall_class": record.recall_class,
            "recall_class_display": record.get_recall_class_display(),
            "recall_source": record.recall_source,
            "recall_source_display": record.get_recall_source_display(),
            "status": record.status,
            "status_display": record.get_status_display(),
            "title": record.title,
            "recall_reason": record.recall_reason,
            "dgda_order_reference": record.dgda_order_reference,
            "effective_date": record.effective_date.isoformat() if record.effective_date else None,
            "is_dispensing_locked": record.is_dispensing_locked(),
            "published_by_id": str(record.published_by_id) if record.published_by_id else None,
            "published_at": record.published_at.isoformat() if record.published_at else None,
            "lifted_by_id": str(record.lifted_by_id) if record.lifted_by_id else None,
            "lifted_at": record.lifted_at.isoformat() if record.lifted_at else None,
            "lifting_reason": record.lifting_reason,
            "quarantined_units_count": str(record.quarantined_units_count),
            "affected_reservations_evicted": record.affected_reservations_evicted,
            "historical_invoices_impacted": record.historical_invoices_impacted,
            "sha256_hash": record.sha256_hash,
            "is_archived": record.is_archived,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        }

    @classmethod
    def list_recalls(
        cls,
        user: Any,
        branch_id: Optional[str] = None,
        batch_number: Optional[str] = None,
        status_filter: Optional[str] = None,
        recall_class: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Lists recall notices for tenant.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )

        qs = BatchRecallNotification.objects.filter(tenant=tenant).select_related("branch", "product")
        if branch_id:
            qs = qs.filter(branch_id=str(branch_id).strip())
        if batch_number:
            qs = qs.filter(batch_number__iexact=str(batch_number).strip())
        if status_filter:
            qs = qs.filter(status=str(status_filter).strip().upper())
        if recall_class:
            qs = qs.filter(recall_class=str(recall_class).strip().upper())

        return [cls.serialize_recall(rec, user=user) for rec in qs]

    @classmethod
    def get_recall(cls, user: Any, recall_id: str) -> Dict[str, Any]:
        """
        Retrieves single recall record.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )

        record = BatchRecallNotification.objects.filter(
            recall_id=str(recall_id).strip(),
            tenant=tenant,
        ).select_related("branch", "product").first()

        if not record:
            raise NotFound({"recall_id": f"Batch recall notice '{recall_id}' not found."})

        return cls.serialize_recall(record, user=user)

    @classmethod
    @transaction.atomic
    def publish_recall(cls, user: Any, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Publishes a new Batch Recall Notice and automates propagation across inventory:
        1. Quarantines active batch stock.
        2. Evicts open stock reservations.
        3. Scans historical sales impact without rewriting history.
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials required.")

        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in cls.ALLOWED_MANAGE_ROLES:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_RECALL_MANAGE_UNAUTHORIZED",
                    "message_en": "Only credentialed Pharmacists, Branch Managers, and Owners can publish recall notices.",
                    "message_bn": "শুধুমাত্র ফার্মাসিস্ট, ব্রাঞ্চ ম্যানেজার ও ফার্মেসি মালিক রিকল বিজ্ঞপ্তি প্রকাশ করতে পারেন।",
                }
            )

        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=True, allowed_roles=cls.ALLOWED_MANAGE_ROLES
        )

        # 1. Batch Number Validation
        batch_no = str(data.get("batch_number") or "").strip().upper()
        if len(batch_no) < 2:
            raise ValidationError({"batch_number": "batch_number (minimum 2 characters) is required."})

        # 2. Product Name Validation
        product_name = str(data.get("product_name") or "").strip()
        if not product_name:
            raise ValidationError({"product_name": "product_name is required."})

        # 3. Recall Reason Validation
        recall_reason = str(data.get("recall_reason") or "").strip()
        if not recall_reason:
            raise ValidationError({"recall_reason": "recall_reason is required."})

        # 4. Recall Class Validation
        raw_class = data.get("recall_class", RecallClass.CLASS_II)
        recall_class = str(raw_class).strip().upper()
        if recall_class not in RecallClass.values:
            raise ValidationError({
                "recall_class": f"Invalid recall_class '{recall_class}'. Must be one of: {list(RecallClass.values)}"
            })

        # 5. Recall Source Validation
        raw_source = data.get("recall_source", RecallSource.DGDA_MANDATORY)
        recall_source = str(raw_source).strip().upper()
        if recall_source not in RecallSource.values:
            raise ValidationError({
                "recall_source": f"Invalid recall_source '{recall_source}'. Must be one of: {list(RecallSource.values)}"
            })

        # 6. Branch Scoping
        branch_id = data.get("branch_id")
        branch = None
        if branch_id:
            branch = Branch.objects.filter(branch_id=str(branch_id).strip(), tenant=tenant).first()
            if not branch:
                raise ValidationError({"branch_id": f"Branch '{branch_id}' does not exist for this tenant."})

        # 7. Effective Date Validation
        eff_date_raw = data.get("effective_date")
        if eff_date_raw:
            try:
                effective_date = datetime.date.fromisoformat(str(eff_date_raw).strip())
            except ValueError:
                raise ValidationError({"effective_date": "Invalid effective_date format. Expected YYYY-MM-DD."})
        else:
            effective_date = timezone.localdate() if hasattr(timezone, "localdate") else datetime.date.today()

        title = str(data.get("title") or f"Recall Alert: {product_name} Batch {batch_no}").strip()
        dgda_order_ref = str(data.get("dgda_order_reference") or "").strip()

        # Step 1: Active Stock Quarantine
        batches_qs = BatchStock.objects.filter(
            tenant=tenant,
            batch_number__iexact=batch_no,
        )
        if branch:
            batches_qs = batches_qs.filter(branch=branch)

        total_quarantined_qty = Decimal("0.00")
        for b in batches_qs:
            total_quarantined_qty += b.quantity_base_units
            b.status = BatchStatus.RECALLED
            b.is_quarantined = True
            b.recall_reason = recall_reason
            b.save(update_fields=["status", "is_quarantined", "recall_reason", "updated_at"])

        # Step 2: Open Reservation Eviction
        reservations_qs = StockReservation.objects.filter(
            tenant=tenant,
            batch__batch_number__iexact=batch_no,
            status=StockReservationStatus.ACTIVE,
        )
        if branch:
            reservations_qs = reservations_qs.filter(branch=branch)

        evicted_count = 0
        for res in reservations_qs:
            res.status = StockReservationStatus.RELEASED
            res.save(update_fields=["status", "updated_at"])
            # Adjust reserved quantity on batch
            if res.batch:
                res.batch.reserved_quantity_base_units = max(
                    Decimal("0.00"),
                    res.batch.reserved_quantity_base_units - res.quantity_base_units,
                )
                res.batch.save(update_fields=["reserved_quantity_base_units", "updated_at"])
            evicted_count += 1

        # Step 3: Historical Sales Impact Scan (WITHOUT altering past records)
        allocations_qs = SaleLotAllocation.objects.filter(
            tenant=tenant,
            batch__batch_number__iexact=batch_no,
        )
        if branch:
            allocations_qs = allocations_qs.filter(branch=branch)
        historical_count = allocations_qs.values("invoice_id").distinct().count()

        # Create Recall Notification Record
        new_id = f"rcl-{uuid.uuid4().hex[:12]}"
        record = BatchRecallNotification.objects.create(
            recall_id=new_id,
            tenant=tenant,
            branch=branch,
            product_name=product_name,
            batch_number=batch_no,
            recall_class=recall_class,
            recall_source=recall_source,
            status=RecallNoticeStatus.QUARANTINED_AND_ACTIONED,
            title=title,
            recall_reason=recall_reason,
            dgda_order_reference=dgda_order_ref,
            effective_date=effective_date,
            published_by=user,
            quarantined_units_count=total_quarantined_qty,
            affected_reservations_evicted=evicted_count,
            historical_invoices_impacted=historical_count,
        )

        return cls.serialize_recall(record, user=user)

    @classmethod
    def get_historical_sales_impact(cls, user: Any, recall_id: str) -> Dict[str, Any]:
        """
        Queries historical sales and patient contact records impacted by recalled batch
        WITHOUT mutating or rewriting historical financial or dispensing records.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )

        record = BatchRecallNotification.objects.filter(
            recall_id=str(recall_id).strip(),
            tenant=tenant,
        ).first()

        if not record:
            raise NotFound({"recall_id": f"Batch recall notice '{recall_id}' not found."})

        allocations = SaleLotAllocation.objects.filter(
            tenant=tenant,
            batch__batch_number__iexact=record.batch_number,
        ).select_related("invoice", "branch")

        if record.branch:
            allocations = allocations.filter(branch=record.branch)

        impacted_sales = []
        total_sold_units = Decimal("0.00")
        total_sold_bdt = Decimal("0.00")

        for alloc in allocations:
            inv = alloc.invoice
            total_sold_units += Decimal(str(alloc.quantity_base_units))
            total_sold_bdt += alloc.total_selling_price

            impacted_sales.append({
                "allocation_id": alloc.allocation_id,
                "invoice_number": inv.invoice_number if inv else "POS-SALE",
                "invoice_date": inv.created_at.isoformat() if inv and inv.created_at else alloc.created_at.isoformat(),
                "branch_id": str(alloc.branch_id),
                "branch_name": alloc.branch.name if alloc.branch else "",
                "customer_name": getattr(inv, "customer_name", "Walk-in Patient"),
                "customer_phone": getattr(inv, "customer_phone", "N/A"),
                "prescription_number": getattr(inv, "prescription_number", ""),
                "quantity_sold": f"{Decimal(str(alloc.quantity_base_units)):.2f}",
                "total_price_bdt": f"{alloc.total_selling_price:.2f}",
            })

        return {
            "recall_id": record.recall_id,
            "product_name": record.product_name,
            "batch_number": record.batch_number,
            "recall_class": record.recall_class,
            "historical_audit_note": "Historical sales records are immutable; data extracted for patient outreach and recall returns only.",
            "total_impacted_invoices": len(impacted_sales),
            "total_units_sold_historically": f"{total_sold_units:.2f}",
            "total_value_sold_bdt": f"{total_sold_bdt:.2f}",
            "impacted_sales": impacted_sales,
        }

    @classmethod
    def check_dispensing_gate(
        cls,
        tenant_id: str,
        batch_number: str,
        branch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Real-time POS scanning / cart gate verifying whether a batch is under active recall.
        Returns is_dispensing_allowed=False with clinical blocking alert if recalled.
        """
        batch_no = str(batch_number or "").strip().upper()
        if not batch_no:
            return {"is_dispensing_allowed": True}

        qs = BatchRecallNotification.objects.filter(
            tenant_id=str(tenant_id).strip(),
            batch_number__iexact=batch_no,
            is_archived=False,
            status__in=[RecallNoticeStatus.PUBLISHED, RecallNoticeStatus.QUARANTINED_AND_ACTIONED],
        )

        if branch_id:
            from django.db.models import Q
            qs = qs.filter(Q(branch_id=str(branch_id).strip()) | Q(branch__isnull=True))

        active_recall = qs.first()
        if active_recall:
            return {
                "is_dispensing_allowed": False,
                "error_code": "ERR_BATCH_RECALLED_DISPENSING_BLOCKED",
                "message_en": f"Batch '{batch_no}' of {active_recall.product_name} is under active {active_recall.recall_class} recall! Dispensing is strictly prohibited.",
                "message_bn": f"{active_recall.product_name}-এর ব্যাচ '{batch_no}' সক্রিয় রিকলের অধীনে রয়েছে! এটি বিতরণ করা সম্পূর্ণ নিষিদ্ধ।",
                "recall_id": active_recall.recall_id,
                "recall_class": active_recall.recall_class,
                "recall_reason": active_recall.recall_reason,
            }

        return {"is_dispensing_allowed": True}

    @classmethod
    @transaction.atomic
    def lift_recall(cls, user: Any, recall_id: str, reason: str = "") -> Dict[str, Any]:
        """
        Lifts or rescinds a recall notice with audited reversal.
        Requires Pharmacist, Branch Manager, or Owner role.
        """
        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in cls.ALLOWED_MANAGE_ROLES:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_RECALL_MANAGE_UNAUTHORIZED",
                    "message_en": "Only Pharmacists, Branch Managers, and Owners can lift a recall.",
                    "message_bn": "শুধুমাত্র ফার্মাসিস্ট, ব্রাঞ্চ ম্যানেজার ও ফার্মেসি মালিক রিকল প্রত্যাহার করতে পারেন।",
                }
            )

        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=True, allowed_roles=cls.ALLOWED_MANAGE_ROLES
        )

        record = BatchRecallNotification.objects.filter(
            recall_id=str(recall_id).strip(),
            tenant=tenant,
        ).first()

        if not record:
            raise NotFound({"recall_id": f"Batch recall notice '{recall_id}' not found."})

        record.status = RecallNoticeStatus.LIFTED
        record.lifted_by = user
        record.lifted_at = timezone.now()
        record.lifting_reason = str(reason or "Administrative recall reversal").strip()
        record.save()

        # Restore BatchStock to ACTIVE if unexpired
        today = timezone.localdate() if hasattr(timezone, "localdate") else datetime.date.today()
        batches = BatchStock.objects.filter(
            tenant=tenant,
            batch_number__iexact=record.batch_number,
            status=BatchStatus.RECALLED,
        )
        if record.branch:
            batches = batches.filter(branch=record.branch)

        for b in batches:
            if b.expiry_date and b.expiry_date < today:
                b.status = BatchStatus.EXPIRED
            else:
                b.status = BatchStatus.ACTIVE
                b.is_quarantined = False
            b.save(update_fields=["status", "is_quarantined", "updated_at"])

        return cls.serialize_recall(record, user=user)

