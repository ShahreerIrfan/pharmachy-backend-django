"""
backend/apps/core/global_medicine_service.py

Architecture Decision: ADR-0070
Centrally Curated Global Medicine Master Catalogue Service.
Manages master product curation lifecycle, DGDA regulatory metadata, immutable SHA-256 version hashes,
and strict platform ownership protection preventing store-level mutations.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import MedicineCurationHistory, MedicineCurationStatus, MedicineProduct
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger(__name__)


class GlobalMedicineCatalogService:
    """
    Central orchestration service for platform-wide medicine catalog curation.
    Strictly enforces platform administration ownership and tamper-evident versioning.
    """

    @classmethod
    def resolve_status_chip(cls, curation_status: str) -> Dict[str, str]:
        """Maps curation lifecycle status to bilingual labels and restrained Apple-inspired UI tones."""
        clean = str(curation_status or "APPROVED").upper().strip()
        mapping = {
            MedicineCurationStatus.DRAFT: {
                "tone": "info",
                "label_en": "Draft",
                "label_bn": "খসড়া",
            },
            MedicineCurationStatus.UNDER_REVIEW: {
                "tone": "warning",
                "label_en": "Under Review",
                "label_bn": "পর্যালোচনাধীন",
            },
            MedicineCurationStatus.APPROVED: {
                "tone": "success",
                "label_en": "Approved & Published",
                "label_bn": "অনুমোদিত ও প্রকাশিত",
            },
            MedicineCurationStatus.DEPRECATED: {
                "tone": "neutral",
                "label_en": "Deprecated",
                "label_bn": "বাতিলযোগ্য",
            },
            MedicineCurationStatus.RECALLED: {
                "tone": "danger",
                "label_en": "DGDA Recalled",
                "label_bn": "ডিজিডিএ প্রত্যাহারকৃত",
            },
        }
        return mapping.get(clean, {"tone": "neutral", "label_en": clean, "label_bn": clean})

    @classmethod
    def _require_platform_operator(cls, user: PharmacyUser):
        """Invariant: Only platform administrators can author or curate the global master catalog."""
        user_role = str(getattr(user, "role", "")).upper().strip()
        if user_role not in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR] and not getattr(
            user, "is_superuser", False
        ):
            raise PermissionDenied(
                detail={
                    "error_code": "PLATFORM_CATALOG_OWNERSHIP_PROTECTED",
                    "message_en": (
                        "Access denied: Global medicine catalog master entries are centrally managed "
                        "by platform administrators. Tenants cannot modify master catalog entries."
                    ),
                    "message_bn": "গ্লোবাল মেডিসিন মাস্টার ক্যাটালগ কেন্দ্রীয়ভাবে সুরক্ষিত। কোনো ফার্মেসি এটি পরিবর্তন বা মুছতে পারবে না।",
                }
            )

    @classmethod
    @transaction.atomic
    def create_master_product(cls, operator: PharmacyUser, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Creates a new master product in the global catalog.
        """
        cls._require_platform_operator(operator)

        brand_name = str(data.get("brand_name") or "").strip()
        generic_name = str(data.get("generic_name") or "").strip()
        dosage_form = str(data.get("dosage_form") or "").strip()
        strength = str(data.get("strength") or "").strip()
        manufacturer = str(data.get("manufacturer") or "").strip()

        if not brand_name:
            raise ValidationError({"brand_name": "Brand name is required."})
        if not generic_name:
            raise ValidationError({"generic_name": "Generic INN name is required."})
        if not dosage_form:
            raise ValidationError({"dosage_form": "Dosage form is required."})
        if not strength:
            raise ValidationError({"strength": "Strength is required."})
        if not manufacturer:
            raise ValidationError({"manufacturer": "Manufacturer name is required."})

        # Monetary validations
        try:
            mrp_unit = Decimal(str(data.get("mrp_unit", "1.00")))
            if mrp_unit < Decimal("0.01"):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValidationError({"mrp_unit": "MRP unit must be a valid positive Decimal >= 0.01 BDT."})

        try:
            cost_unit = Decimal(str(data.get("cost_unit", "0.00")))
            if cost_unit < Decimal("0.00"):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValidationError({"cost_unit": "Cost unit must be a valid Decimal >= 0.00 BDT."})

        # DGDA DAR Number uniqueness check
        dar_number = str(data.get("dgda_dar_number") or "").strip()
        if dar_number and MedicineProduct.objects.filter(dgda_dar_number=dar_number).exists():
            raise ValidationError(
                {"dgda_dar_number": f"Medicine with DGDA DAR number '{dar_number}' already exists in catalog."}
            )

        product_id = str(data.get("product_id") or "").strip() or f"prod-{uuid.uuid4().hex[:12]}"
        initial_status = str(data.get("curation_status") or MedicineCurationStatus.DRAFT).upper().strip()
        if initial_status not in MedicineCurationStatus.values:
            initial_status = MedicineCurationStatus.DRAFT

        product = MedicineProduct(
            product_id=product_id,
            brand_name=brand_name,
            generic_name=generic_name,
            dosage_form=dosage_form,
            strength=strength,
            manufacturer=manufacturer,
            mrp_unit=mrp_unit,
            cost_unit=cost_unit,
            requires_cold_chain=bool(data.get("requires_cold_chain", False)),
            min_temp_celsius=data.get("min_temp_celsius"),
            max_temp_celsius=data.get("max_temp_celsius"),
            dgda_dar_number=dar_number,
            regulatory_source=str(data.get("regulatory_source", "DGDA_BANGLADESH")).strip(),
            curation_status=initial_status,
            version_number=1,
            is_prescription_only=bool(data.get("is_prescription_only", False)),
            is_narcotic=bool(data.get("is_narcotic", False)),
            is_antibiotic=bool(data.get("is_antibiotic", False)),
            curated_by=operator,
            curation_notes=str(data.get("curation_notes") or "").strip(),
        )
        product.immutable_version_hash = product.compute_version_hash()
        product.save()

        MedicineCurationHistory.objects.create(
            history_id=f"cur-{uuid.uuid4().hex[:12]}",
            product=product,
            reviewer=operator,
            action="CREATED",
            old_status="",
            new_status=initial_status,
            version_number=1,
            notes=f"Initial master product entry by {operator.email}",
        )

        AuditLogEntry.objects.create(
            action="GLOBAL_MEDICINE_CREATED",
            resource_type="GLOBAL_MEDICINE_CATALOG",
            resource_id=product.product_id,
            tenant_id="platform-global",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "brand_name": product.brand_name,
                "generic_name": product.generic_name,
                "dgda_dar_number": product.dgda_dar_number,
                "curation_status": product.curation_status,
                "version_number": product.version_number,
            },
        )

        return cls.serialize_product(product)

    @classmethod
    @transaction.atomic
    def approve_master_product(
        cls,
        reviewer: PharmacyUser,
        product_id: str,
        dgda_dar_number: Optional[str] = None,
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Approves and publishes a master medicine record.
        Requires verified DGDA DAR number.
        """
        cls._require_platform_operator(reviewer)
        clean_id = str(product_id or "").strip()
        product = MedicineProduct.objects.filter(product_id=clean_id).first()
        if not product:
            raise ValidationError({"product_id": f"Medicine product '{clean_id}' does not exist."})

        if dgda_dar_number:
            product.dgda_dar_number = str(dgda_dar_number).strip()

        if not product.dgda_dar_number:
            raise ValidationError(
                {"dgda_dar_number": "Formal regulatory approval strictly requires a valid DGDA DAR/DLR number."}
            )

        old_status = product.curation_status
        product.curation_status = MedicineCurationStatus.APPROVED
        product.reviewed_by = reviewer
        product.reviewed_at = timezone.now()
        if notes:
            product.curation_notes = notes
        product.immutable_version_hash = product.compute_version_hash()
        product.save()

        MedicineCurationHistory.objects.create(
            history_id=f"cur-{uuid.uuid4().hex[:12]}",
            product=product,
            reviewer=reviewer,
            action="APPROVED",
            old_status=old_status,
            new_status=MedicineCurationStatus.APPROVED,
            version_number=product.version_number,
            notes=notes or f"Approved and published by {reviewer.email}",
        )

        AuditLogEntry.objects.create(
            action="GLOBAL_MEDICINE_APPROVED",
            resource_type="GLOBAL_MEDICINE_CATALOG",
            resource_id=product.product_id,
            tenant_id="platform-global",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "brand_name": product.brand_name,
                "dgda_dar_number": product.dgda_dar_number,
                "version_number": product.version_number,
                "version_hash": product.immutable_version_hash,
            },
        )

        return cls.serialize_product(product)

    @classmethod
    @transaction.atomic
    def recall_master_product(
        cls,
        reviewer: PharmacyUser,
        product_id: str,
        recall_reason: str,
        regulatory_order_ref: str,
    ) -> Dict[str, Any]:
        """
        Marks a product as RECALLED / Blacklisted per official DGDA regulatory order.
        Immediately bars dispensing and sales across all tenants.
        """
        cls._require_platform_operator(reviewer)
        clean_id = str(product_id or "").strip()
        product = MedicineProduct.objects.filter(product_id=clean_id).first()
        if not product:
            raise ValidationError({"product_id": f"Medicine product '{clean_id}' does not exist."})

        clean_reason = str(recall_reason or "").strip()
        if len(clean_reason) < 10:
            raise ValidationError({"recall_reason": "Recall reason must be at least 10 characters long."})

        clean_ref = str(regulatory_order_ref or "").strip()
        if len(clean_ref) < 5:
            raise ValidationError(
                {"regulatory_order_ref": "Official DGDA Gazette / regulatory order reference is required (>= 5 chars)."}
            )

        old_status = product.curation_status
        product.curation_status = MedicineCurationStatus.RECALLED
        product.curation_notes = f"RECALL ORDER: {clean_ref} - {clean_reason}"
        product.save(update_fields=["curation_status", "curation_notes", "updated_at"])

        MedicineCurationHistory.objects.create(
            history_id=f"cur-{uuid.uuid4().hex[:12]}",
            product=product,
            reviewer=reviewer,
            action="RECALLED",
            old_status=old_status,
            new_status=MedicineCurationStatus.RECALLED,
            version_number=product.version_number,
            notes=product.curation_notes,
        )

        AuditLogEntry.objects.create(
            action="GLOBAL_MEDICINE_RECALLED",
            resource_type="GLOBAL_MEDICINE_CATALOG",
            resource_id=product.product_id,
            tenant_id="platform-global",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "brand_name": product.brand_name,
                "regulatory_order_ref": clean_ref,
                "recall_reason": clean_reason,
            },
        )

        return cls.serialize_product(product)

    @classmethod
    @transaction.atomic
    def deprecate_master_product(
        cls,
        reviewer: PharmacyUser,
        product_id: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        """
        Deprecates an obsolete formulation.
        """
        cls._require_platform_operator(reviewer)
        clean_id = str(product_id or "").strip()
        product = MedicineProduct.objects.filter(product_id=clean_id).first()
        if not product:
            raise ValidationError({"product_id": f"Medicine product '{clean_id}' does not exist."})

        old_status = product.curation_status
        product.curation_status = MedicineCurationStatus.DEPRECATED
        if reason:
            product.curation_notes = f"DEPRECATED: {reason}"
        product.save(update_fields=["curation_status", "curation_notes", "updated_at"])

        MedicineCurationHistory.objects.create(
            history_id=f"cur-{uuid.uuid4().hex[:12]}",
            product=product,
            reviewer=reviewer,
            action="DEPRECATED",
            old_status=old_status,
            new_status=MedicineCurationStatus.DEPRECATED,
            version_number=product.version_number,
            notes=reason or "Formulation deprecated.",
        )

        return cls.serialize_product(product)

    @classmethod
    def get_curation_history(cls, product_id: str) -> List[Dict[str, Any]]:
        """Retrieves full audit timeline of regulatory curation events for a product."""
        clean_id = str(product_id or "").strip()
        history_qs = (
            MedicineCurationHistory.objects.filter(product_id=clean_id)
            .select_related("reviewer")
            .order_by("-timestamp")
        )
        result = []
        for h in history_qs:
            result.append(
                {
                    "history_id": h.history_id,
                    "reviewer_email": h.reviewer.email if h.reviewer else "System",
                    "action": h.action,
                    "old_status": h.old_status,
                    "new_status": h.new_status,
                    "version_number": h.version_number,
                    "notes": h.notes,
                    "timestamp": h.timestamp.isoformat(),
                }
            )
        return result

    @classmethod
    def serialize_product(cls, product: MedicineProduct) -> Dict[str, Any]:
        """Serializes master medicine product conforming to JSON schema."""
        chip = cls.resolve_status_chip(product.curation_status)
        return {
            "product_id": product.product_id,
            "brand_name": product.brand_name,
            "generic_name": product.generic_name,
            "dosage_form": product.dosage_form,
            "strength": product.strength,
            "manufacturer": product.manufacturer,
            "mrp_unit": str(product.mrp_unit),
            "cost_unit": str(product.cost_unit),
            "dgda_dar_number": product.dgda_dar_number or None,
            "regulatory_source": product.regulatory_source,
            "curation_status": product.curation_status,
            "version_number": product.version_number,
            "immutable_version_hash": product.immutable_version_hash or product.compute_version_hash(),
            "is_prescription_only": product.is_prescription_only,
            "is_narcotic": product.is_narcotic,
            "is_antibiotic": product.is_antibiotic,
            "requires_cold_chain": product.requires_cold_chain,
            "min_temp_celsius": str(product.min_temp_celsius) if product.min_temp_celsius is not None else None,
            "max_temp_celsius": str(product.max_temp_celsius) if product.max_temp_celsius is not None else None,
            "reviewed_at": product.reviewed_at.isoformat() if product.reviewed_at else None,
            "curation_notes": product.curation_notes or None,
            "status_chip": chip,
            "created_at": product.created_at.isoformat(),
        }
