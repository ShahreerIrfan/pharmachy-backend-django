"""
backend/apps/core/classification_snapshot_service.py
Architecture Decision: ADR-0130 (P130).
Service for Versioned Controlled Classification Snapshots, Recalls,
Point-in-Time Regulatory Audits, and Master Medicine Synchronization.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import dateparse, timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    ClassificationType,
    MedicineClassificationSnapshot,
    MedicineCurationStatus,
    MedicineProduct,
)
from apps.identity.models import PharmacyUser


class MedicineClassificationSnapshotService:
    """
    Central service managing the regulatory lifecycle of controlled classifications,
    point-in-time snapshot resolution, and recall enforcement.
    """

    @classmethod
    def serialize_snapshot(cls, snapshot: MedicineClassificationSnapshot) -> Dict[str, Any]:
        """Formats snapshot according to JSON Schema contract."""
        reviewer_email = snapshot.reviewed_by.email if snapshot.reviewed_by else None
        return {
            "snapshot_id": snapshot.snapshot_id,
            "product_id": snapshot.product_id,
            "version_number": snapshot.version_number,
            "classification_type": snapshot.classification_type,
            "regulatory_source": snapshot.regulatory_source,
            "effective_from": snapshot.effective_from.isoformat() if snapshot.effective_from else None,
            "effective_to": snapshot.effective_to.isoformat() if snapshot.effective_to else None,
            "is_current": snapshot.is_current,
            "is_dispensing_blocked": snapshot.is_dispensing_blocked,
            "reviewer_email": reviewer_email,
            "reviewed_at": snapshot.reviewed_at.isoformat() if snapshot.reviewed_at else None,
            "review_notes": snapshot.review_notes or "",
            "immutable_hash": snapshot.immutable_hash,
            "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
        }

    @classmethod
    @transaction.atomic
    def create_snapshot(
        cls,
        product_id: str,
        data: Dict[str, Any],
        actor: PharmacyUser,
    ) -> Dict[str, Any]:
        """
        Creates a new regulatory classification snapshot for a medicine,
        superseding any previously active snapshot and synchronizing master flags.
        """
        clean_product_id = str(product_id or "").strip()
        product = MedicineProduct.objects.filter(product_id=clean_product_id).first()
        if not product:
            raise ValidationError({"product_id": f"Global master product '{clean_product_id}' does not exist."})

        # 1. Classification Type Validation
        c_type = str(data.get("classification_type", "")).strip().upper()
        if c_type not in ClassificationType.values:
            raise ValidationError(
                {"classification_type": f"Invalid classification_type '{c_type}'. Must be one of {ClassificationType.values}."}
            )

        # 2. Regulatory Source & Review Notes Validation
        reg_source = str(data.get("regulatory_source", "")).strip()
        if len(reg_source) < 3:
            raise ValidationError({"regulatory_source": "Regulatory source reference must be at least 3 characters."})

        review_notes = str(data.get("review_notes", "")).strip()
        if len(review_notes) < 5:
            raise ValidationError({"review_notes": "Review and justification notes must be at least 5 characters."})

        # 3. Effective From Timestamp
        raw_eff_from = data.get("effective_from")
        if raw_eff_from:
            if isinstance(raw_eff_from, datetime):
                eff_from = raw_eff_from
            else:
                eff_from = dateparse.parse_datetime(str(raw_eff_from))
                if not eff_from:
                    raise ValidationError({"effective_from": "Invalid ISO datetime format for effective_from."})
        else:
            eff_from = timezone.now()

        if timezone.is_naive(eff_from):
            eff_from = timezone.make_aware(eff_from)

        # 4. Monotonic Versioning & Preceding Snapshot Supersession
        prev_current = (
            MedicineClassificationSnapshot.objects.filter(
                product=product,
                is_current=True,
            )
            .order_by("-version_number")
            .first()
        )

        if prev_current:
            prev_current.effective_to = eff_from
            prev_current.is_current = False
            prev_current.save(update_fields=["effective_to", "is_current"])
            next_version = prev_current.version_number + 1
        else:
            highest_version = (
                MedicineClassificationSnapshot.objects.filter(product=product)
                .aggregate(max_v=models.Max("version_number"))["max_v"]
                or 0
            )
            next_version = highest_version + 1

        # 5. Create New Immutable Snapshot
        snapshot = MedicineClassificationSnapshot.objects.create(
            snapshot_id=f"mcs-{uuid.uuid4().hex[:12]}",
            product=product,
            version_number=next_version,
            classification_type=c_type,
            regulatory_source=reg_source,
            effective_from=eff_from,
            effective_to=None,
            is_current=True,
            reviewed_by=actor,
            reviewed_at=timezone.now(),
            review_notes=review_notes,
        )

        # 6. Master Medicine Flag Synchronization
        old_status = product.curation_status
        if c_type == ClassificationType.RECALLED:
            product.curation_status = MedicineCurationStatus.RECALLED
        elif c_type == ClassificationType.BANNED:
            product.curation_status = MedicineCurationStatus.SUSPENDED
        elif c_type == ClassificationType.PRESCRIPTION_ONLY:
            product.is_prescription_only = True
        elif c_type == ClassificationType.NARCOTIC_SCHEDULED:
            product.is_prescription_only = True
            product.is_narcotic = True
        elif c_type in (
            ClassificationType.ANTIMICROBIAL_ACCESS,
            ClassificationType.ANTIMICROBIAL_WATCH,
            ClassificationType.ANTIMICROBIAL_RESERVE,
        ):
            product.is_prescription_only = True
            product.is_antibiotic = True
        elif c_type == ClassificationType.OTC:
            product.is_prescription_only = False
            product.is_narcotic = False
            product.is_antibiotic = False

        product.version_number += 1
        product.immutable_version_hash = product.compute_version_hash()
        product.save()

        # 7. Audit Log Trail
        AuditLogEntry.objects.create(
            action="MEDICINE_CLASSIFICATION_SNAPSHOT_CREATED",
            resource_type="MEDICINE_CLASSIFICATION",
            resource_id=snapshot.snapshot_id,
            tenant_id="platform",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "product_id": product.product_id,
                "version_number": snapshot.version_number,
                "classification_type": snapshot.classification_type,
                "regulatory_source": snapshot.regulatory_source,
                "effective_from": snapshot.effective_from.isoformat(),
                "old_curation_status": old_status,
                "new_curation_status": product.curation_status,
                "is_dispensing_blocked": snapshot.is_dispensing_blocked,
            },
        )

        return cls.serialize_snapshot(snapshot)

    @classmethod
    def get_current_snapshot(cls, product_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves the active classification snapshot for a medicine."""
        snapshot = (
            MedicineClassificationSnapshot.objects.filter(
                product_id=str(product_id).strip(),
                is_current=True,
            )
            .select_related("reviewed_by")
            .first()
        )
        if not snapshot:
            return None
        return cls.serialize_snapshot(snapshot)

    @classmethod
    def get_point_in_time_snapshot(
        cls,
        product_id: str,
        point_in_time: datetime,
    ) -> Optional[Dict[str, Any]]:
        """
        Point-in-Time Resolution: Returns the exact classification snapshot
        that was legally active at the given historical timestamp.
        """
        clean_product_id = str(product_id or "").strip()
        if timezone.is_naive(point_in_time):
            point_in_time = timezone.make_aware(point_in_time)

        snapshot = (
            MedicineClassificationSnapshot.objects.filter(
                product_id=clean_product_id,
                effective_from__lte=point_in_time,
            )
            .filter(
                models.Q(effective_to__isnull=True) | models.Q(effective_to__gt=point_in_time)
            )
            .order_by("-version_number")
            .select_related("reviewed_by")
            .first()
        )
        if not snapshot:
            return None
        return cls.serialize_snapshot(snapshot)

    @classmethod
    def list_snapshots(cls, product_id: str) -> Dict[str, Any]:
        """
        Returns full versioned classification history for a medicine.
        """
        clean_product_id = str(product_id or "").strip()
        product = MedicineProduct.objects.filter(product_id=clean_product_id).first()
        if not product:
            raise ValidationError({"product_id": f"Global master product '{clean_product_id}' does not exist."})

        snapshots_qs = (
            MedicineClassificationSnapshot.objects.filter(product=product)
            .select_related("reviewed_by")
            .order_by("-version_number")
        )

        current_item = None
        serialized_list = []
        for s in snapshots_qs:
            serialized = cls.serialize_snapshot(s)
            serialized_list.append(serialized)
            if s.is_current:
                current_item = serialized

        return {
            "product_id": clean_product_id,
            "total_snapshots": len(serialized_list),
            "current_snapshot": current_item,
            "snapshots": serialized_list,
        }

    @classmethod
    def is_dispensing_blocked(cls, product_id: str) -> Tuple[bool, str]:
        """
        Checks whether the medicine is currently recalled or banned by DGDA.
        """
        current = (
            MedicineClassificationSnapshot.objects.filter(
                product_id=str(product_id).strip(),
                is_current=True,
            )
            .first()
        )
        if not current:
            return False, ""

        if current.classification_type == ClassificationType.RECALLED:
            return True, f"ERR_PRODUCT_RECALLED_SALE_BLOCKED: Medicine '{product_id}' is subject to DGDA Regulatory Safety Recall."
        if current.classification_type == ClassificationType.BANNED:
            return True, f"ERR_PRODUCT_BANNED_SALE_BLOCKED: Medicine '{product_id}' is banned for sale by statutory order."

        return False, ""
