"""
backend/apps/core/temperature_safety_review_service.py
Architecture Decision: ADR-0257 (P257).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Domain service managing immutable temperature excursion safety reviews for
cold-chain biologicals, vaccines, and antibiotics before release, return or disposal.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    ColdChainExcursionIncident,
    ExcursionIncidentStatus,
    ExcursionExposureSeverity,
    ExcursionReviewOutcome,
    MedicineProduct,
    StockMovementType,
    TemperatureExcursionSafetyReview,
    Tenant,
)
from apps.core.pos_domain_service import PosDomainService
from apps.core.stock_ledger_service import StockLedgerService


class TemperatureSafetyReviewService:
    """
    Enforces statutory pharmacist oversight on temperature-exposed medicine batches,
    guaranteeing immutable decision recording, stock ledger reconciliation,
    and real-time clinical dispensing gates.
    """

    ALLOWED_READ_ROLES = ["SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST", "CASHIER"]
    ALLOWED_REVIEW_ROLES = ["SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST"]

    @classmethod
    def serialize_review(cls, record: TemperatureExcursionSafetyReview, user: Any = None) -> Dict[str, Any]:
        """
        Serializes TemperatureExcursionSafetyReview with audit integrity validation.
        """
        expected_hash = record.compute_sha256_hash()
        is_tamper_free = record.sha256_audit_hash == expected_hash

        return {
            "review_id": record.review_id,
            "tenant_id": str(record.tenant_id),
            "branch_id": str(record.branch_id),
            "branch_name": record.branch.name if record.branch else "",
            "batch_id": str(record.batch_id),
            "batch_number": record.batch_number,
            "product_id": str(record.product_id) if record.product_id else None,
            "product_name": record.product_name,
            "reviewed_quantity_base_units": f"{record.reviewed_quantity_base_units:.2f}",
            "unit_cost_bdt": f"{record.unit_cost_bdt:.2f}",
            "total_exposed_value_bdt": f"{record.total_exposed_value_bdt:.2f}",
            "min_exposed_temperature": f"{record.min_exposed_temperature:.2f}" if record.min_exposed_temperature is not None else None,
            "max_exposed_temperature": f"{record.max_exposed_temperature:.2f}",
            "exposure_duration_hours": f"{record.exposure_duration_hours:.2f}",
            "severity": record.severity,
            "severity_display": record.get_severity_display(),
            "review_decision": record.review_decision,
            "review_decision_display": record.get_review_decision_display(),
            "clinical_justification": record.clinical_justification,
            "manufacturer_consulted": record.manufacturer_consulted,
            "manufacturer_reference_no": record.manufacturer_reference_no,
            "action_taken_notes": record.action_taken_notes,
            "incident_id": str(record.incident_id) if record.incident_id else None,
            "reviewed_by_id": str(record.reviewed_by_id) if record.reviewed_by_id else None,
            "reviewed_by_name": getattr(record.reviewed_by, "full_name", None) or getattr(record.reviewed_by, "email", "Staff Pharmacist"),
            "reviewed_at": record.reviewed_at.isoformat() if record.reviewed_at else None,
            "sha256_audit_hash": record.sha256_audit_hash,
            "is_tamper_free": is_tamper_free,
            "is_immutable": record.is_immutable,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }

    @classmethod
    def list_reviews(
        cls,
        user: Any,
        branch_id: Optional[str] = None,
        batch_number: Optional[str] = None,
        decision_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Lists temperature safety reviews with multi-tenant filtering.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )

        qs = TemperatureExcursionSafetyReview.objects.filter(tenant=tenant).select_related(
            "branch", "product", "batch", "reviewed_by"
        )
        if branch_id:
            qs = qs.filter(branch_id=str(branch_id).strip())
        if batch_number:
            qs = qs.filter(batch_number__iexact=str(batch_number).strip())
        if decision_filter:
            qs = qs.filter(review_decision=str(decision_filter).strip().upper())

        return [cls.serialize_review(rec, user=user) for rec in qs]

    @classmethod
    def get_review(cls, user: Any, review_id: str) -> Dict[str, Any]:
        """
        Retrieves single temperature safety review record.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )

        record = TemperatureExcursionSafetyReview.objects.filter(
            review_id=str(review_id).strip(),
            tenant=tenant,
        ).select_related("branch", "product", "batch", "reviewed_by").first()

        if not record:
            raise NotFound({"review_id": f"Temperature safety review '{review_id}' not found."})

        return cls.serialize_review(record, user=user)

    @classmethod
    @transaction.atomic
    def record_review(cls, user: Any, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes a formal, immutable temperature excursion safety review:
        1. Validates pharmacist/manager role authorization.
        2. Validates exposed temperature parameters and clinical justification.
        3. Enforces expiry boundaries (expired stock CANNOT be released to active).
        4. Applies stock state transitions and immutable stock ledger entries.
        5. Computes SHA-256 audit digest.
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials required.")

        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in cls.ALLOWED_REVIEW_ROLES:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_PHARMACIST_REVIEW_REQUIRED",
                    "message_en": "Only registered Pharmacists and Branch Managers can execute temperature safety reviews.",
                    "message_bn": "শুধুমাত্র নিবন্ধিত ফার্মাসিস্ট এবং ব্রাঞ্চ ম্যানেজার তাপমাত্রা সুরক্ষা পর্যালোচনা সম্পন্ন করতে পারেন।",
                }
            )

        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=True, allowed_roles=cls.ALLOWED_REVIEW_ROLES
        )

        # 1. Branch Scoping
        branch_id = data.get("branch_id")
        if not branch_id:
            raise ValidationError({"branch_id": "branch_id is required."})
        branch = Branch.objects.filter(branch_id=str(branch_id).strip(), tenant=tenant).first()
        if not branch:
            raise ValidationError({"branch_id": f"Branch '{branch_id}' does not exist for this tenant."})

        # 2. Batch Resolution
        batch_id = data.get("batch_id")
        batch_no = str(data.get("batch_number") or "").strip().upper()
        batch_qs = BatchStock.objects.select_for_update().filter(tenant=tenant, branch=branch)
        if batch_id:
            batch = batch_qs.filter(batch_id=str(batch_id).strip()).first()
        elif batch_no:
            batch = batch_qs.filter(batch_number__iexact=batch_no).first()
        else:
            raise ValidationError({"batch_id": "Either batch_id or batch_number must be provided."})

        if not batch:
            raise ValidationError({"batch": f"Batch stock '{batch_id or batch_no}' not found in specified branch."})

        # 3. Decision Outcome Validation
        raw_decision = data.get("review_decision")
        if not raw_decision:
            raise ValidationError({"review_decision": "review_decision is required."})
        decision = str(raw_decision).strip().upper()
        if decision not in ExcursionReviewOutcome.values:
            raise ValidationError({
                "review_decision": f"Invalid review_decision '{decision}'. Must be one of: {list(ExcursionReviewOutcome.values)}"
            })

        # 4. Temperature & Exposure Parameters
        try:
            max_temp = Decimal(str(data.get("max_exposed_temperature", "0.00")))
        except Exception:
            raise ValidationError({"max_exposed_temperature": "Invalid Decimal format for max_exposed_temperature."})

        min_temp = None
        if data.get("min_exposed_temperature") is not None:
            try:
                min_temp = Decimal(str(data.get("min_exposed_temperature")))
            except Exception:
                raise ValidationError({"min_exposed_temperature": "Invalid Decimal format for min_exposed_temperature."})

        try:
            duration_hrs = Decimal(str(data.get("exposure_duration_hours", "0.00")))
            if duration_hrs < Decimal("0.00"):
                raise ValueError()
        except Exception:
            raise ValidationError({"exposure_duration_hours": "exposure_duration_hours must be a non-negative Decimal."})

        # 5. Quantity & Cost Reconciliation
        reviewed_qty_raw = data.get("reviewed_quantity_base_units")
        if reviewed_qty_raw is not None:
            try:
                reviewed_qty = Decimal(str(reviewed_qty_raw))
                if reviewed_qty <= Decimal("0.00"):
                    raise ValueError()
            except Exception:
                raise ValidationError({"reviewed_quantity_base_units": "reviewed_quantity_base_units must be a positive Decimal."})
        else:
            reviewed_qty = Decimal(str(batch.quantity_base_units))

        unit_cost = getattr(batch, "cost_per_unit", None) or Decimal("0.00")
        total_exposed_value = (unit_cost * reviewed_qty).quantize(Decimal("0.01"))

        # 6. Severity Evaluation
        if duration_hrs >= Decimal("6.00") or max_temp >= Decimal("15.00"):
            severity = ExcursionExposureSeverity.CRITICAL
        elif duration_hrs >= Decimal("2.00") or max_temp >= Decimal("10.00"):
            severity = ExcursionExposureSeverity.MODERATE
        else:
            severity = ExcursionExposureSeverity.MINOR

        # 7. Clinical Justification Validation
        clinical_justification = str(data.get("clinical_justification") or "").strip()
        if decision == ExcursionReviewOutcome.RELEASE_TO_ACTIVE:
            if len(clinical_justification) < 15:
                raise ValidationError({
                    "clinical_justification": (
                        "Clinical justification must be at least 15 characters documenting "
                        "manufacturer stability guidelines or permissible mean kinetic temperature data."
                    )
                })

            # Statutory Rule: Expired batches CAN NEVER be released to active sellable inventory
            today = timezone.localdate() if hasattr(timezone, "localdate") else datetime.date.today()
            if batch.expiry_date and batch.expiry_date <= today:
                raise ValidationError({
                    "review_decision": "Cannot release expired medicine batch to active sellable stock. Immediate condemnation or return required."
                })

        # 8. Incident Linking (Optional)
        incident_id = data.get("incident_id")
        incident = None
        if incident_id:
            incident = ColdChainExcursionIncident.objects.filter(
                id=str(incident_id).strip(), tenant=tenant, branch=branch
            ).first()

        # 9. Stock State Transition & Immutable Ledger Recording
        if decision == ExcursionReviewOutcome.RELEASE_TO_ACTIVE:
            batch.status = BatchStatus.ACTIVE
            batch.is_quarantined = False
            batch.reserved_quantity_base_units = max(Decimal("0.00"), batch.reserved_quantity_base_units - reviewed_qty)
            batch.save(update_fields=["status", "is_quarantined", "reserved_quantity_base_units", "updated_at"])

            if incident:
                incident.status = ExcursionIncidentStatus.RESOLVED
                incident.clinical_justification = clinical_justification
                incident.reviewed_by = user
                incident.reviewed_at = timezone.now()
                incident.save(update_fields=["status", "clinical_justification", "reviewed_by", "reviewed_at", "updated_at"])

        elif decision == ExcursionReviewOutcome.CONDEMN_DISPOSAL:
            batch.status = BatchStatus.ARCHIVED
            batch.is_active = False
            batch.is_quarantined = False
            batch.reserved_quantity_base_units = max(Decimal("0.00"), batch.reserved_quantity_base_units - reviewed_qty)
            batch.save(update_fields=["status", "is_active", "is_quarantined", "reserved_quantity_base_units", "updated_at"])

            # Record immutable stock ledger entry
            if reviewed_qty > Decimal("0.00"):
                StockLedgerService.record_stock_movement(
                    tenant=tenant,
                    branch=branch,
                    product=batch.product,
                    movement_type=StockMovementType.EXPIRY_DISPOSAL,
                    quantity_delta=-int(reviewed_qty),
                    source_entity_type="TemperatureExcursionSafetyReview",
                    source_entity_id=f"REV-{batch.batch_number}",
                    source_reference=f"EXCURSION-DISPOSAL-{batch.batch_number}",
                    batch_number=batch.batch_number,
                    batch=batch,
                    cost_lot=getattr(batch, "purchase_lot", None),
                    unit_cost=unit_cost,
                    storage_location=getattr(batch, "storage_location", "COLD_CHAIN"),
                    notes=f"Temperature excursion condemnation: {clinical_justification}",
                    posted_by=user,
                )

        elif decision == ExcursionReviewOutcome.RETURN_TO_SUPPLIER:
            batch.status = BatchStatus.RECALLED
            batch.is_quarantined = False
            batch.reserved_quantity_base_units = max(Decimal("0.00"), batch.reserved_quantity_base_units - reviewed_qty)
            batch.save(update_fields=["status", "is_quarantined", "reserved_quantity_base_units", "updated_at"])

            # Record immutable stock ledger entry
            if reviewed_qty > Decimal("0.00"):
                StockLedgerService.record_stock_movement(
                    tenant=tenant,
                    branch=branch,
                    product=batch.product,
                    movement_type=StockMovementType.PURCHASE_RETURN,
                    quantity_delta=-int(reviewed_qty),
                    source_entity_type="TemperatureExcursionSafetyReview",
                    source_entity_id=f"REV-{batch.batch_number}",
                    source_reference=f"EXCURSION-RETURN-{batch.batch_number}",
                    batch_number=batch.batch_number,
                    batch=batch,
                    cost_lot=getattr(batch, "purchase_lot", None),
                    unit_cost=unit_cost,
                    storage_location=getattr(batch, "storage_location", "COLD_CHAIN"),
                    notes=f"Temperature excursion return to supplier: {clinical_justification}",
                    posted_by=user,
                )

        # 10. Persist Immutable Review Record
        review_id = f"esr-{uuid.uuid4().hex[:12]}"
        review = TemperatureExcursionSafetyReview.objects.create(
            review_id=review_id,
            tenant=tenant,
            branch=branch,
            batch=batch,
            incident=incident,
            product=batch.product,
            product_name=batch.product.brand_name if batch.product else "Cold-Chain Item",
            batch_number=batch.batch_number,
            reviewed_quantity_base_units=reviewed_qty,
            unit_cost_bdt=unit_cost,
            total_exposed_value_bdt=total_exposed_value,
            min_exposed_temperature=min_temp,
            max_exposed_temperature=max_temp,
            exposure_duration_hours=duration_hrs,
            severity=severity,
            review_decision=decision,
            clinical_justification=clinical_justification,
            manufacturer_consulted=bool(data.get("manufacturer_consulted", False)),
            manufacturer_reference_no=str(data.get("manufacturer_reference_no") or "").strip(),
            action_taken_notes=str(data.get("action_taken_notes") or "").strip(),
            reviewed_by=user,
            reviewed_at=timezone.now(),
            is_immutable=True,
        )

        return cls.serialize_review(review, user=user)

    @classmethod
    def check_batch_temperature_safety(
        cls,
        tenant_id: str,
        batch_number: str,
        branch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Real-time clinical safety pre-check for POS dispensing and Rx antibiotic register.
        Verifies whether a batch is currently quarantined for temperature breach
        or has an unapproved excursion.
        """
        batch_no = str(batch_number or "").strip().upper()
        if not batch_no:
            return {"is_dispensing_allowed": True}

        batch_qs = BatchStock.objects.filter(
            tenant_id=str(tenant_id).strip(),
            batch_number__iexact=batch_no,
        ).select_related("product")
        if branch_id:
            batch_qs = batch_qs.filter(branch_id=str(branch_id).strip())

        batch = batch_qs.first()
        if not batch:
            return {"is_dispensing_allowed": True}

        # Check if batch is quarantined or status is QUARANTINED/RECALLED/ARCHIVED
        if batch.is_quarantined or batch.status in [BatchStatus.QUARANTINED, BatchStatus.RECALLED, BatchStatus.ARCHIVED]:
            latest_review = TemperatureExcursionSafetyReview.objects.filter(
                tenant_id=str(tenant_id).strip(),
                batch_number__iexact=batch_no,
            ).order_by("-reviewed_at").first()

            if not latest_review or latest_review.review_decision != ExcursionReviewOutcome.RELEASE_TO_ACTIVE:
                is_antibiotic = getattr(batch.product, "is_antibiotic", False)
                return {
                    "is_dispensing_allowed": False,
                    "error_code": "ERR_TEMPERATURE_EXCURSION_UNREVIEWED",
                    "message_en": f"Batch '{batch_no}' is quarantined for temperature exposure! Clinical pharmacist review required before dispensing.",
                    "message_bn": f"ব্যাচ '{batch_no}' তাপমাত্রাজনিত বিচ্যুতির কারণে কোয়ারেন্টাইন করা হয়েছে! বিতরণের পূর্বে ফার্মাসিস্টের অনুমোদন আবশ্যক।",
                    "batch_number": batch_no,
                    "batch_status": batch.status,
                    "is_antibiotic": is_antibiotic,
                    "product_name": batch.product.brand_name if batch.product else "",
                }

        return {
            "is_dispensing_allowed": True,
            "batch_number": batch_no,
            "batch_status": batch.status,
        }
