"""
backend/apps/core/product_metadata_review_service.py
Architecture Decision: ADR-0133 (P133).
Service for Pharmacist/Editor Review of Medicine Metadata Changes,
Proposed Diff Computation, Dual-Control Approvals, and Master Catalog Publication.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    MedicineCurationStatus,
    MedicineProduct,
    MedicineReviewStatus,
    ProductMetadataReview,
    ReviewChangeType,
)
from apps.identity.models import PharmacyUser

logger = logging.getLogger("pharmacy.core.product_metadata_review")


class ProductMetadataReviewService:
    """
    Core business service governing medicine metadata peer review, diff tracking,
    pharmacist sign-off, and regulatory publication.
    """

    ALLOWED_EDITABLE_FIELDS = {
        "brand_name",
        "brand_name_bn",
        "generic_name",
        "generic_name_bn",
        "dosage_form",
        "strength",
        "strength_numeric",
        "strength_unit",
        "manufacturer",
        "route",
        "barcode",
        "mrp_unit",
        "cost_unit",
        "requires_cold_chain",
        "min_temp_celsius",
        "max_temp_celsius",
        "dgda_dar_number",
        "is_prescription_only",
        "is_narcotic",
        "is_antibiotic",
        "curation_notes",
    }

    @classmethod
    def serialize_review(cls, review: ProductMetadataReview) -> Dict[str, Any]:
        """Converts ProductMetadataReview to JSON Schema compliant dictionary."""
        submitter_email = review.submitted_by.email if review.submitted_by else None
        reviewer_email = review.reviewed_by.email if review.reviewed_by else None
        return {
            "review_id": review.review_id,
            "product_id": review.product_id,
            "product_brand_name": review.product.brand_name if review.product else "",
            "change_type": review.change_type,
            "status": review.status,
            "proposed_diff": review.proposed_diff,
            "submitter_email": submitter_email,
            "submitted_at": review.submitted_at.isoformat() if review.submitted_at else None,
            "reviewer_email": reviewer_email,
            "reviewed_at": review.reviewed_at.isoformat() if review.reviewed_at else None,
            "review_notes": review.review_notes or "",
            "review_hash": review.review_hash,
            "created_at": review.created_at.isoformat() if review.created_at else None,
        }

    @classmethod
    @transaction.atomic
    def propose_review(
        cls,
        product_id: str,
        change_type: str,
        proposed_changes: Dict[str, Any],
        actor: PharmacyUser,
        submission_notes: str = "",
    ) -> Dict[str, Any]:
        """
        Creates a new metadata review proposal, computes field diffs, and sets product state.
        """
        clean_pid = str(product_id or "").strip()
        product = MedicineProduct.objects.filter(product_id=clean_pid).first()
        if not product:
            raise ValidationError({"product_id": f"Medicine product '{clean_pid}' not found."})

        clean_type = str(change_type or "").strip().upper()
        if clean_type not in ReviewChangeType.values:
            raise ValidationError(
                {"change_type": f"Invalid change_type '{clean_type}'. Must be one of {ReviewChangeType.values}."}
            )

        if not isinstance(proposed_changes, dict) or not proposed_changes:
            raise ValidationError({"proposed_changes": "proposed_changes dictionary must contain at least one field."})

        # Compute field diffs
        proposed_diff: Dict[str, Dict[str, Any]] = {}
        for field, new_val in proposed_changes.items():
            if field not in cls.ALLOWED_EDITABLE_FIELDS:
                continue
            old_val = getattr(product, field, None)
            # Serialize for diff readability
            old_repr = str(old_val) if isinstance(old_val, Decimal) else old_val
            new_repr = str(new_val) if isinstance(new_val, Decimal) else new_val
            proposed_diff[field] = {
                "old_value": old_repr,
                "new_value": new_repr,
            }

        if not proposed_diff:
            raise ValidationError(
                {"proposed_changes": "No supported or modifiable metadata fields provided in proposal."}
            )

        review = ProductMetadataReview.objects.create(
            review_id=f"rev-{uuid.uuid4().hex[:12]}",
            product=product,
            change_type=clean_type,
            status=MedicineReviewStatus.PENDING_REVIEW,
            proposed_diff=proposed_diff,
            submitted_by=actor,
            submission_notes=str(submission_notes or "").strip(),
        )

        # Transition product to UNDER_REVIEW if it was in DRAFT
        if product.curation_status == MedicineCurationStatus.DRAFT:
            product.curation_status = MedicineCurationStatus.UNDER_REVIEW
            product.save(update_fields=["curation_status"])

        AuditLogEntry.objects.create(
            action="PRODUCT_METADATA_REVIEW_SUBMITTED",
            resource_type="MEDICINE_REVIEW",
            resource_id=review.review_id,
            tenant_id="platform",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "product_id": product.product_id,
                "change_type": review.change_type,
                "altered_fields": list(proposed_diff.keys()),
            },
        )

        return cls.serialize_review(review)

    @classmethod
    @transaction.atomic
    def decide_review(
        cls,
        review_id: str,
        decision: str,
        actor: PharmacyUser,
        review_notes: str,
    ) -> Dict[str, Any]:
        """
        Executes pharmacist review decision: APPROVE (and publishes changes), REJECT, or REQUEST_CHANGES.
        Enforces Four-Eyes Principle / Dual Control.
        """
        review = (
            ProductMetadataReview.objects.select_related("product", "submitted_by")
            .filter(review_id=str(review_id).strip())
            .first()
        )
        if not review:
            raise ValidationError({"review_id": f"Review record '{review_id}' not found."})

        if review.status != MedicineReviewStatus.PENDING_REVIEW:
            raise ValidationError(
                {"status": f"ERR_REVIEW_ALREADY_DECIDED: Review '{review_id}' has already been {review.status}."}
            )

        # Four-Eyes Principle Enforcement: Submitter cannot approve their own proposal
        is_superuser = getattr(actor, "is_superuser", False) or str(getattr(actor, "role", "")).upper() == "SUPERADMIN"
        actor_pk = getattr(actor, "pk", None) or getattr(actor, "user_id", None)
        if review.submitted_by_id and actor_pk and str(review.submitted_by_id) == str(actor_pk) and not is_superuser:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_FOUR_EYES_PRINCIPLE_VIOLATION",
                    "message_en": (
                        "Submitter cannot approve their own medicine metadata review proposal. "
                        "Independent pharmacist review sign-off is required."
                    ),
                    "message_bn": "প্রস্তাবকারী নিজে নিজের রিভিউ অনুমোদন করতে পারবেন না। ভিন্ন ফার্মাসিস্ট দ্বারা অনুমোদন আবশ্যক।",
                }
            )

        clean_decision = str(decision or "").strip().upper()
        if clean_decision not in ("APPROVE", "REJECT", "REQUEST_CHANGES"):
            raise ValidationError(
                {"decision": f"Invalid decision '{clean_decision}'. Must be APPROVE, REJECT, or REQUEST_CHANGES."}
            )

        clean_notes = str(review_notes or "").strip()
        if len(clean_notes) < 5:
            raise ValidationError({"review_notes": "Review justification notes must be at least 5 characters."})

        now = timezone.now()
        product = review.product

        if clean_decision == "APPROVE":
            # Apply proposed changes atomically to MedicineProduct
            for field, diff in review.proposed_diff.items():
                new_val = diff["new_value"]
                # Type cast decimals if needed
                if field in ("mrp_unit", "cost_unit", "strength_numeric", "min_temp_celsius", "max_temp_celsius"):
                    if new_val is not None:
                        new_val = Decimal(str(new_val))
                setattr(product, field, new_val)

            product.curation_status = MedicineCurationStatus.APPROVED
            product.reviewed_by = actor
            product.reviewed_at = now
            product.version_number += 1
            product.immutable_version_hash = product.compute_version_hash()
            product.save()

            review.status = MedicineReviewStatus.APPROVED
            review.reviewed_by = actor
            review.reviewed_at = now
            review.review_notes = clean_notes
            review.review_hash = review.compute_review_hash()
            review.save()

            AuditLogEntry.objects.create(
                action="PRODUCT_METADATA_REVIEW_APPROVED",
                resource_type="MEDICINE_REVIEW",
                resource_id=review.review_id,
                tenant_id="platform",
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "actor": actor.email if actor else "system",
                    "product_id": product.product_id,
                    "new_product_version": product.version_number,
                    "applied_fields": list(review.proposed_diff.keys()),
                },
            )

        elif clean_decision == "REJECT":
            review.status = MedicineReviewStatus.REJECTED
            review.reviewed_by = actor
            review.reviewed_at = now
            review.review_notes = clean_notes
            review.review_hash = review.compute_review_hash()
            review.save()

            AuditLogEntry.objects.create(
                action="PRODUCT_METADATA_REVIEW_REJECTED",
                resource_type="MEDICINE_REVIEW",
                resource_id=review.review_id,
                tenant_id="platform",
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "actor": actor.email if actor else "system",
                    "product_id": product.product_id,
                    "rejection_reason": clean_notes,
                },
            )

        elif clean_decision == "REQUEST_CHANGES":
            review.status = MedicineReviewStatus.CHANGES_REQUESTED
            review.reviewed_by = actor
            review.reviewed_at = now
            review.review_notes = clean_notes
            review.review_hash = review.compute_review_hash()
            review.save()

        return cls.serialize_review(review)

    @classmethod
    def list_reviews(
        cls,
        product_id: Optional[str] = None,
        status_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Queries and lists review records."""
        qs = ProductMetadataReview.objects.select_related("product", "submitted_by", "reviewed_by").all()
        if product_id:
            qs = qs.filter(product_id=str(product_id).strip())
        if status_filter:
            qs = qs.filter(status=str(status_filter).strip().upper())

        return [cls.serialize_review(r) for r in qs]
