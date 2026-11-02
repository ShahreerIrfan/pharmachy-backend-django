"""
backend/apps/core/regulatory_source_service.py
Architecture Decision: ADR-0134 (P134).
Service Layer for Legal and Manufacturer Regulatory Source Attachment:
Classification and Storage Rules, Citations, Review Deadlines, and Historical Immutability.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import dateparse, timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    MedicineProduct,
    RegulatoryRuleTarget,
    RegulatorySourceAttachment,
    RegulatorySourceType,
)
from apps.identity.models import PharmacyUser

logger = logging.getLogger("pharmacy.core.regulatory_source")


class RegulatorySourceService:
    """
    Business service governing legal/manufacturer regulatory sources,
    storage rule validation (e.g. cold chain constraints), classification rules,
    and periodic compliance review schedules.
    """

    @classmethod
    def serialize_attachment(cls, attachment: RegulatorySourceAttachment) -> Dict[str, Any]:
        """Converts RegulatorySourceAttachment to JSON Schema compliant dictionary."""
        reviewer_email = attachment.reviewed_by.email if attachment.reviewed_by else None
        return {
            "attachment_id": attachment.attachment_id,
            "product_id": attachment.product_id,
            "product_brand_name": attachment.product.brand_name if attachment.product else "",
            "source_type": attachment.source_type,
            "rule_target": attachment.rule_target,
            "document_reference_number": attachment.document_reference_number,
            "issuing_authority": attachment.issuing_authority,
            "publication_date": attachment.publication_date.isoformat() if attachment.publication_date else None,
            "effective_date": attachment.effective_date.isoformat() if attachment.effective_date else None,
            "review_cycle_months": attachment.review_cycle_months,
            "next_review_due_date": attachment.next_review_due_date.isoformat() if attachment.next_review_due_date else None,
            "source_url": attachment.source_url or "",
            "storage_rule_specs": attachment.storage_rule_specs or {},
            "classification_specs": attachment.classification_specs or {},
            "version_number": attachment.version_number,
            "is_active": attachment.is_active,
            "immutable_source_hash": attachment.immutable_source_hash,
            "reviewer_email": reviewer_email,
            "reviewed_at": attachment.reviewed_at.isoformat() if attachment.reviewed_at else None,
            "review_notes": attachment.review_notes or "",
            "created_at": attachment.created_at.isoformat() if attachment.created_at else None,
        }

    @classmethod
    def _parse_date(cls, val: Any, field_name: str) -> Optional[datetime.date]:
        if not val:
            return None
        if isinstance(val, datetime.date):
            return val
        if isinstance(val, datetime.datetime):
            return val.date()
        parsed = dateparse.parse_date(str(val).strip())
        if not parsed:
            raise ValidationError({field_name: f"Invalid date format for '{field_name}'. Expected YYYY-MM-DD."})
        return parsed

    @classmethod
    @transaction.atomic
    def attach_source(
        cls,
        product_id: str,
        source_type: str,
        rule_target: str,
        document_reference_number: str,
        issuing_authority: str,
        effective_date: Any,
        publication_date: Any = None,
        review_cycle_months: int = 12,
        next_review_due_date: Any = None,
        source_url: str = "",
        storage_rule_specs: Optional[Dict[str, Any]] = None,
        classification_specs: Optional[Dict[str, Any]] = None,
        actor: Optional[PharmacyUser] = None,
        review_notes: str = "",
    ) -> Dict[str, Any]:
        """
        Creates and attaches a new regulatory source document to a medicine product,
        validates storage and classification rules, and synchronizes product attributes.
        """
        clean_pid = str(product_id or "").strip()
        product = MedicineProduct.objects.filter(product_id=clean_pid).first()
        if not product:
            raise ValidationError({"product_id": f"Medicine product '{clean_pid}' not found."})

        clean_source_type = str(source_type or "").strip().upper()
        if clean_source_type not in RegulatorySourceType.values:
            raise ValidationError(
                {"source_type": f"Invalid source_type '{clean_source_type}'. Must be one of {RegulatorySourceType.values}."}
            )

        clean_rule_target = str(rule_target or "").strip().upper()
        if clean_rule_target not in RegulatoryRuleTarget.values:
            raise ValidationError(
                {"rule_target": f"Invalid rule_target '{clean_rule_target}'. Must be one of {RegulatoryRuleTarget.values}."}
            )

        clean_doc_ref = str(document_reference_number or "").strip()
        if not clean_doc_ref:
            raise ValidationError({"document_reference_number": "document_reference_number is required."})

        clean_authority = str(issuing_authority or "").strip()
        if not clean_authority:
            raise ValidationError({"issuing_authority": "issuing_authority is required."})

        parsed_eff_date = cls._parse_date(effective_date, "effective_date")
        if not parsed_eff_date:
            raise ValidationError({"effective_date": "effective_date is required."})

        parsed_pub_date = cls._parse_date(publication_date, "publication_date") if publication_date else None

        cycle_months = int(review_cycle_months or 12)
        if cycle_months < 1:
            raise ValidationError({"review_cycle_months": "review_cycle_months must be at least 1."})

        # Calculate or validate next_review_due_date
        if next_review_due_date:
            parsed_due_date = cls._parse_date(next_review_due_date, "next_review_due_date")
            if parsed_due_date and parsed_due_date < parsed_eff_date:
                raise ValidationError(
                    {"next_review_due_date": "next_review_due_date cannot be earlier than effective_date."}
                )
        else:
            # Approximate by adding months
            days_to_add = int(cycle_months * 30.4375)
            parsed_due_date = parsed_eff_date + datetime.timedelta(days=days_to_add)

        storage_specs = dict(storage_rule_specs or {})
        classification_rule_specs = dict(classification_specs or {})

        # Storage Rule Specifications Validation & Synchronization
        if clean_rule_target in (
            RegulatoryRuleTarget.STORAGE_CONDITION,
            RegulatoryRuleTarget.CLASSIFICATION_AND_STORAGE,
        ):
            requires_cold_chain = bool(storage_specs.get("requires_cold_chain", False))
            if requires_cold_chain:
                min_temp_raw = storage_specs.get("min_temp_celsius")
                max_temp_raw = storage_specs.get("max_temp_celsius")
                if min_temp_raw is None or max_temp_raw is None:
                    raise ValidationError(
                        {"storage_rule_specs": "Cold chain storage rules require both min_temp_celsius and max_temp_celsius."}
                    )
                try:
                    min_temp = Decimal(str(min_temp_raw))
                    max_temp = Decimal(str(max_temp_raw))
                except Exception:
                    raise ValidationError(
                        {"storage_rule_specs": "min_temp_celsius and max_temp_celsius must be valid decimal numbers."}
                    )

                if min_temp >= max_temp:
                    raise ValidationError(
                        {"storage_rule_specs": "Cold chain rule violation: min_temp_celsius must be strictly less than max_temp_celsius."}
                    )

                product.requires_cold_chain = True
                product.min_temp_celsius = min_temp
                product.max_temp_celsius = max_temp
            else:
                product.requires_cold_chain = False

        # Classification Rule Specifications Validation & Synchronization
        if clean_rule_target in (
            RegulatoryRuleTarget.CLASSIFICATION,
            RegulatoryRuleTarget.CLASSIFICATION_AND_STORAGE,
        ):
            c_type = str(classification_rule_specs.get("classification_type", "")).strip().upper()
            if c_type in ("PRESCRIPTION_ONLY", "POM", "RESTRICTED"):
                product.is_prescription_only = True
            elif "is_prescription_only" in classification_rule_specs:
                product.is_prescription_only = bool(classification_rule_specs["is_prescription_only"])

            if c_type in ("ANTIMICROBIAL", "ANTIBIOTIC"):
                product.is_antibiotic = True
            elif "is_antibiotic" in classification_rule_specs:
                product.is_antibiotic = bool(classification_rule_specs["is_antibiotic"])

            if c_type in ("NARCOTIC", "CONTROLLED"):
                product.is_narcotic = True
            elif "is_narcotic" in classification_rule_specs:
                product.is_narcotic = bool(classification_rule_specs["is_narcotic"])

        # Determine version number for deduplication and revisions
        existing_max_ver = (
            RegulatorySourceAttachment.objects.filter(
                product=product,
                document_reference_number=clean_doc_ref,
            ).aggregate(Max("version_number"))["version_number__max"]
            or 0
        )
        version_number = existing_max_ver + 1

        product.regulatory_source = clean_doc_ref
        product.save()

        now = timezone.now()
        attachment = RegulatorySourceAttachment.objects.create(
            attachment_id=f"reg-src-{uuid.uuid4().hex[:12]}",
            product=product,
            source_type=clean_source_type,
            rule_target=clean_rule_target,
            document_reference_number=clean_doc_ref,
            issuing_authority=clean_authority,
            publication_date=parsed_pub_date,
            effective_date=parsed_eff_date,
            review_cycle_months=cycle_months,
            next_review_due_date=parsed_due_date,
            source_url=str(source_url or "").strip(),
            storage_rule_specs=storage_specs,
            classification_specs=classification_rule_specs,
            version_number=version_number,
            is_active=True,
            reviewed_by=actor,
            reviewed_at=now,
            review_notes=str(review_notes or "").strip(),
        )

        AuditLogEntry.objects.create(
            action="REGULATORY_SOURCE_ATTACHED",
            resource_type="REGULATORY_SOURCE",
            resource_id=attachment.attachment_id,
            tenant_id="platform",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "product_id": product.product_id,
                "document_reference_number": clean_doc_ref,
                "source_type": clean_source_type,
                "rule_target": clean_rule_target,
                "version_number": version_number,
                "next_review_due_date": parsed_due_date.isoformat(),
            },
        )

        return cls.serialize_attachment(attachment)

    @classmethod
    @transaction.atomic
    def update_source(
        cls,
        attachment_id: str,
        data: Dict[str, Any],
        actor: Optional[PharmacyUser] = None,
    ) -> Dict[str, Any]:
        """
        Updates an existing regulatory source attachment, or produces an incremented revision
        if structural rules are amended.
        """
        attachment = (
            RegulatorySourceAttachment.objects.select_related("product")
            .filter(attachment_id=str(attachment_id).strip())
            .first()
        )
        if not attachment:
            raise ValidationError({"attachment_id": f"Regulatory source attachment '{attachment_id}' not found."})

        if "review_notes" in data:
            attachment.review_notes = str(data["review_notes"]).strip()
        if "source_url" in data:
            attachment.source_url = str(data["source_url"]).strip()
        if "next_review_due_date" in data:
            parsed = cls._parse_date(data["next_review_due_date"], "next_review_due_date")
            if parsed and parsed < attachment.effective_date:
                raise ValidationError({"next_review_due_date": "next_review_due_date cannot be earlier than effective_date."})
            attachment.next_review_due_date = parsed

        if "storage_rule_specs" in data:
            storage_specs = dict(data["storage_rule_specs"] or {})
            requires_cold_chain = bool(storage_specs.get("requires_cold_chain", False))
            if requires_cold_chain:
                min_t = Decimal(str(storage_specs.get("min_temp_celsius", "2.00")))
                max_t = Decimal(str(storage_specs.get("max_temp_celsius", "8.00")))
                if min_t >= max_t:
                    raise ValidationError(
                        {"storage_rule_specs": "Cold chain rule violation: min_temp_celsius must be strictly less than max_temp_celsius."}
                    )
                attachment.product.requires_cold_chain = True
                attachment.product.min_temp_celsius = min_t
                attachment.product.max_temp_celsius = max_t
            else:
                attachment.product.requires_cold_chain = False

            attachment.product.save(update_fields=["requires_cold_chain", "min_temp_celsius", "max_temp_celsius"])
            attachment.storage_rule_specs = storage_specs

        if "classification_specs" in data:
            class_specs = dict(data["classification_specs"] or {})
            c_type = str(class_specs.get("classification_type", "")).strip().upper()
            if c_type in ("PRESCRIPTION_ONLY", "POM", "RESTRICTED"):
                attachment.product.is_prescription_only = True
            elif "is_prescription_only" in class_specs:
                attachment.product.is_prescription_only = bool(class_specs["is_prescription_only"])

            if c_type in ("ANTIMICROBIAL", "ANTIBIOTIC"):
                attachment.product.is_antibiotic = True
            elif "is_antibiotic" in class_specs:
                attachment.product.is_antibiotic = bool(class_specs["is_antibiotic"])

            if c_type in ("NARCOTIC", "CONTROLLED"):
                attachment.product.is_narcotic = True
            elif "is_narcotic" in class_specs:
                attachment.product.is_narcotic = bool(class_specs["is_narcotic"])
            attachment.product.save(update_fields=["is_prescription_only", "is_antibiotic", "is_narcotic"])
            attachment.classification_specs = class_specs

        attachment.reviewed_by = actor
        attachment.reviewed_at = timezone.now()
        attachment.immutable_source_hash = attachment.compute_source_hash()
        attachment.save()

        return cls.serialize_attachment(attachment)

    @classmethod
    @transaction.atomic
    def archive_source(
        cls,
        attachment_id: str,
        actor: Optional[PharmacyUser] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Soft-archives a regulatory source attachment."""
        attachment = RegulatorySourceAttachment.objects.filter(attachment_id=str(attachment_id).strip()).first()
        if not attachment:
            raise ValidationError({"attachment_id": f"Regulatory source attachment '{attachment_id}' not found."})

        attachment.is_active = False
        attachment.save(update_fields=["is_active"])

        AuditLogEntry.objects.create(
            action="REGULATORY_SOURCE_ARCHIVED",
            resource_type="REGULATORY_SOURCE",
            resource_id=attachment.attachment_id,
            tenant_id="platform",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "product_id": attachment.product_id,
                "document_reference_number": attachment.document_reference_number,
                "reason": str(reason or "").strip(),
            },
        )

        return cls.serialize_attachment(attachment)

    @classmethod
    def list_sources(
        cls,
        product_id: Optional[str] = None,
        rule_target: Optional[str] = None,
        source_type: Optional[str] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Queries and lists regulatory source records with filtering and search."""
        qs = RegulatorySourceAttachment.objects.select_related("product", "reviewed_by").all()

        if product_id:
            qs = qs.filter(product_id=str(product_id).strip())
        if rule_target:
            qs = qs.filter(rule_target=str(rule_target).strip().upper())
        if source_type:
            qs = qs.filter(source_type=str(source_type).strip().upper())
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if search:
            clean_search = str(search).strip()
            qs = qs.filter(
                Q(document_reference_number__icontains=clean_search)
                | Q(issuing_authority__icontains=clean_search)
                | Q(product__brand_name__icontains=clean_search)
            )

        return [cls.serialize_attachment(item) for item in qs]

    @classmethod
    def get_due_reviews(cls, target_date: Optional[Any] = None) -> List[Dict[str, Any]]:
        """
        Retrieves active regulatory source attachments requiring periodic re-review
        on or before target_date (defaults to today).
        """
        cutoff_date = cls._parse_date(target_date, "target_date") if target_date else timezone.now().date()
        qs = (
            RegulatorySourceAttachment.objects.select_related("product", "reviewed_by")
            .filter(is_active=True, next_review_due_date__lte=cutoff_date)
            .order_by("next_review_due_date")
        )
        return [cls.serialize_attachment(item) for item in qs]
