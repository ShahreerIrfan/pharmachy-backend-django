"""
backend/apps/core/catalogue_recall_service.py
Architecture Decision: ADR-0137 (P137).
Service Layer for Discontinued & Recalled Catalogue Governance:
DGDA Safety Recalls, Market Withdrawals, Discontinued Stock Management,
Dispensing Invariant Enforcement, and Historical Reference Integrity.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    CatalogueLifecycleNotice,
    CatalogueLifecycleStatus,
    MedicineCurationStatus,
    MedicineProduct,
    NonMedicineRetailItem,
    RecallScope,
    Tenant,
)
from apps.identity.models import PharmacyUser

logger = logging.getLogger("pharmacy.core.catalogue_recall")


class CatalogueRecallService:
    """
    Business service governing medicine and retail item safety recalls,
    discontinued catalogue transitions, POS checkout dispensing guards,
    and permanent relational preservation of historical invoices and prescriptions.
    """

    @classmethod
    def serialize_notice(cls, record: CatalogueLifecycleNotice) -> Dict[str, Any]:
        """Converts CatalogueLifecycleNotice to JSON Schema compliant dictionary."""
        target_name = ""
        if record.product_id and record.product:
            target_name = record.product.brand_name
        elif record.non_medicine_item_id and record.non_medicine_item:
            target_name = record.non_medicine_item.name

        return {
            "notice_id": record.notice_id,
            "tenant_id": record.tenant_id_str,
            "product_id": record.product_id,
            "non_medicine_item_id": record.non_medicine_item_id,
            "target_entity_type": record.target_entity_type,
            "target_name": target_name,
            "lifecycle_status": record.lifecycle_status,
            "recall_scope": record.recall_scope,
            "affected_batch_numbers": record.affected_batch_numbers or [],
            "regulatory_authority": record.regulatory_authority,
            "notice_number": record.notice_number,
            "reason": record.reason,
            "effective_date": record.effective_date.isoformat() if record.effective_date else None,
            "action_required": record.action_required or "",
            "is_active": record.is_active,
            "is_dispensing_blocked": record.is_dispensing_blocked,
            "version_number": record.version_number,
            "immutable_hash": record.immutable_hash,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        }

    @classmethod
    @transaction.atomic
    def publish_notice(
        cls,
        target_entity_type: str,
        lifecycle_status: str,
        regulatory_authority: str,
        notice_number: str,
        reason: str,
        effective_date: Optional[Any] = None,
        product_id: Optional[str] = None,
        non_medicine_item_id: Optional[str] = None,
        recall_scope: str = RecallScope.NATIONWIDE_ALL_BATCHES,
        affected_batch_numbers: Optional[List[str]] = None,
        action_required: str = "",
        actor: Optional[PharmacyUser] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Publishes an official safety recall, discontinuation, or withdrawal notice."""
        clean_target_type = str(target_entity_type or "").strip().upper()
        if clean_target_type not in ("MEDICINE", "NON_MEDICINE"):
            raise ValidationError(
                {"target_entity_type": "target_entity_type must be either 'MEDICINE' or 'NON_MEDICINE'."}
            )

        clean_status = str(lifecycle_status or "").strip().upper()
        if clean_status not in CatalogueLifecycleStatus.values:
            raise ValidationError(
                {"lifecycle_status": f"Invalid lifecycle_status '{clean_status}'. Must be one of {CatalogueLifecycleStatus.values}."}
            )

        clean_scope = str(recall_scope or RecallScope.NATIONWIDE_ALL_BATCHES).strip().upper()
        if clean_scope not in RecallScope.values:
            raise ValidationError(
                {"recall_scope": f"Invalid recall_scope '{clean_scope}'. Must be one of {RecallScope.values}."}
            )

        clean_authority = str(regulatory_authority or "").strip()
        if len(clean_authority) < 2:
            raise ValidationError({"regulatory_authority": "regulatory_authority must be at least 2 characters."})

        clean_notice_num = str(notice_number or "").strip()
        if len(clean_notice_num) < 3:
            raise ValidationError({"notice_number": "notice_number must be at least 3 characters."})

        clean_reason = str(reason or "").strip()
        if len(clean_reason) < 5:
            raise ValidationError({"reason": "reason must be at least 5 characters explaining the recall rationale."})

        # Resolve target entity
        product = None
        non_medicine_item = None

        if clean_target_type == "MEDICINE":
            if not product_id:
                raise ValidationError({"product_id": "product_id is required for MEDICINE target."})
            product = MedicineProduct.objects.filter(product_id=str(product_id).strip()).first()
            if not product:
                raise ValidationError({"product_id": f"Medicine product '{product_id}' not found."})
        else:
            if not non_medicine_item_id:
                raise ValidationError({"non_medicine_item_id": "non_medicine_item_id is required for NON_MEDICINE target."})
            non_medicine_item = NonMedicineRetailItem.objects.filter(item_id=str(non_medicine_item_id).strip()).first()
            if not non_medicine_item:
                raise ValidationError({"non_medicine_item_id": f"Retail item '{non_medicine_item_id}' not found."})

        # Resolve tenant context
        is_superuser = bool(getattr(actor, "is_superuser", False) or str(getattr(actor, "role", "")).upper() == "SUPERADMIN")
        effective_tenant = tenant_id or (getattr(actor, "tenant_id", None) if not is_superuser else "platform") or "platform"

        tenant_obj = None
        if effective_tenant != "platform":
            tenant_obj = Tenant.objects.filter(tenant_id=effective_tenant).first()

        # Parse effective date
        if isinstance(effective_date, str):
            from django.utils.dateparse import parse_datetime

            parsed_dt = parse_datetime(effective_date)
            eff_dt = parsed_dt or timezone.now()
        elif effective_date:
            eff_dt = effective_date
        else:
            eff_dt = timezone.now()

        batch_list = [str(b).strip() for b in (affected_batch_numbers or []) if str(b).strip()]

        notice_id = f"rec-{uuid.uuid4().hex[:12]}"

        record = CatalogueLifecycleNotice.objects.create(
            notice_id=notice_id,
            tenant=tenant_obj,
            tenant_id_str=effective_tenant,
            product=product,
            non_medicine_item=non_medicine_item,
            target_entity_type=clean_target_type,
            lifecycle_status=clean_status,
            recall_scope=clean_scope,
            affected_batch_numbers=batch_list,
            regulatory_authority=clean_authority,
            notice_number=clean_notice_num,
            reason=clean_reason,
            effective_date=eff_dt,
            action_required=str(action_required or "").strip(),
            is_active=True,
            version_number=1,
            issued_by=actor,
        )

        # Update product curation status if applicable
        if product:
            if clean_status == CatalogueLifecycleStatus.RECALLED:
                product.curation_status = MedicineCurationStatus.RECALLED
                product.save(update_fields=["curation_status"])
            elif clean_status == CatalogueLifecycleStatus.DISCONTINUED:
                product.curation_status = MedicineCurationStatus.DEPRECATED
                product.save(update_fields=["curation_status"])

        AuditLogEntry.objects.create(
            action="CATALOGUE_RECALL_NOTICE_PUBLISHED",
            resource_type="CATALOGUE_RECALL",
            resource_id=record.notice_id,
            tenant_id=effective_tenant,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "notice_number": clean_notice_num,
                "lifecycle_status": clean_status,
                "recall_scope": clean_scope,
                "affected_batches": batch_list,
                "regulatory_authority": clean_authority,
            },
        )

        return cls.serialize_notice(record)

    @classmethod
    def check_recall_status(
        cls,
        product_id: Optional[str] = None,
        non_medicine_item_id: Optional[str] = None,
        batch_number: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        High-speed safety gating endpoint for POS checkout and prescription dispensing.
        Returns whether dispensing is blocked, with authority notice details.
        """
        if not product_id and not non_medicine_item_id:
            return {"blocked": False, "lifecycle_status": "ACTIVE"}

        qs = CatalogueLifecycleNotice.objects.filter(is_active=True)

        if product_id:
            qs = qs.filter(product_id=str(product_id).strip())
        elif non_medicine_item_id:
            qs = qs.filter(non_medicine_item_id=str(non_medicine_item_id).strip())

        if tenant_id:
            qs = qs.filter(Q(tenant_id_str=str(tenant_id).strip()) | Q(tenant_id_str="platform"))

        # Order by latest effective date
        notices = list(qs.order_by("-effective_date", "-version_number"))

        clean_batch = str(batch_number or "").strip()

        for notice in notices:
            if notice.is_dispensing_blocked:
                # Check batch scope
                if notice.recall_scope == RecallScope.SPECIFIC_BATCHES:
                    if clean_batch and notice.affected_batch_numbers:
                        if any(clean_batch.lower() == str(b).lower() for b in notice.affected_batch_numbers):
                            return {
                                "blocked": True,
                                "lifecycle_status": notice.lifecycle_status,
                                "notice_number": notice.notice_number,
                                "reason": notice.reason,
                                "action_required": notice.action_required,
                                "affected_batch_numbers": notice.affected_batch_numbers,
                            }
                    # If specific batch scope but batch didn't match, continue checking other notices
                else:
                    # Nationwide or Tenant Quarantine blocks ALL batches
                    return {
                        "blocked": True,
                        "lifecycle_status": notice.lifecycle_status,
                        "notice_number": notice.notice_number,
                        "reason": notice.reason,
                        "action_required": notice.action_required,
                        "affected_batch_numbers": notice.affected_batch_numbers,
                    }
            elif notice.lifecycle_status == CatalogueLifecycleStatus.DISCONTINUED:
                # Discontinued: Dispensing of existing stock is permitted, but status flagged
                return {
                    "blocked": False,
                    "lifecycle_status": "DISCONTINUED",
                    "notice_number": notice.notice_number,
                    "reason": notice.reason,
                    "action_required": notice.action_required,
                    "affected_batch_numbers": [],
                }

        return {
            "blocked": False,
            "lifecycle_status": "ACTIVE",
            "notice_number": "",
            "reason": "",
            "action_required": "",
            "affected_batch_numbers": [],
        }

    @classmethod
    @transaction.atomic
    def lift_notice(
        cls,
        notice_id: str,
        actor: Optional[PharmacyUser] = None,
        tenant_id: Optional[str] = None,
        resolution_notes: str = "",
    ) -> Dict[str, Any]:
        """Lifts a recall notice, deactivating the dispensing block and restoring approved curation status."""
        record = CatalogueLifecycleNotice.objects.filter(notice_id=str(notice_id).strip()).first()
        if not record:
            raise ValidationError({"notice_id": f"Catalogue lifecycle notice '{notice_id}' not found."})

        is_superuser = bool(getattr(actor, "is_superuser", False) or str(getattr(actor, "role", "")).upper() == "SUPERADMIN")
        effective_tenant = tenant_id or getattr(actor, "tenant_id", "platform")

        if not is_superuser and record.tenant_id_str != "platform" and record.tenant_id_str != effective_tenant:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied(
                detail={
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "message_en": "Cannot lift or modify recall notice belonging to another tenant.",
                    "message_bn": "অন্য ফার্মেসির রিকল নোটিশ পরিবর্তন বা প্রত্যাহার করা নিষিদ্ধ।",
                }
            )

        record.is_active = False
        record.lifecycle_status = CatalogueLifecycleStatus.LIFTED
        record.version_number += 1
        record.action_required = (
            f"RECALL LIFTED: {str(resolution_notes or '').strip()} (Previous: {record.action_required})".strip()
        )
        record.immutable_hash = record.compute_hash()
        record.save()

        # Check if product has other active recalls; if not, restore APPROVED status
        if record.product_id:
            other_active = CatalogueLifecycleNotice.objects.filter(
                product_id=record.product_id,
                is_active=True,
                lifecycle_status__in=[
                    CatalogueLifecycleStatus.RECALLED,
                    CatalogueLifecycleStatus.WITHDRAWN,
                ],
            ).exists()
            if not other_active:
                record.product.curation_status = MedicineCurationStatus.APPROVED
                record.product.save(update_fields=["curation_status"])

        AuditLogEntry.objects.create(
            action="CATALOGUE_RECALL_NOTICE_LIFTED",
            resource_type="CATALOGUE_RECALL",
            resource_id=record.notice_id,
            tenant_id=record.tenant_id_str,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "resolution_notes": str(resolution_notes or "").strip(),
                "notice_number": record.notice_number,
                "version_number": record.version_number,
            },
        )

        return cls.serialize_notice(record)

    @classmethod
    def list_notices(
        cls,
        product_id: Optional[str] = None,
        non_medicine_item_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Queries and lists catalogue lifecycle notices."""
        qs = CatalogueLifecycleNotice.objects.select_related("product", "non_medicine_item").all()

        if product_id:
            qs = qs.filter(product_id=str(product_id).strip())
        if non_medicine_item_id:
            qs = qs.filter(non_medicine_item_id=str(non_medicine_item_id).strip())
        if tenant_id:
            qs = qs.filter(Q(tenant_id_str=str(tenant_id).strip()) | Q(tenant_id_str="platform"))
        if lifecycle_status:
            qs = qs.filter(lifecycle_status=str(lifecycle_status).strip().upper())
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if search:
            q = str(search).strip()
            qs = qs.filter(
                Q(notice_number__icontains=q)
                | Q(reason__icontains=q)
                | Q(regulatory_authority__icontains=q)
                | Q(product__brand_name__icontains=q)
            )

        return [cls.serialize_notice(r) for r in qs.order_by("-effective_date", "-version_number")]
