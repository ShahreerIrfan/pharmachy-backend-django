"""
Manufacturer & Supplier Catalogs, Merge Policies & Duplicate Resolution Service.
Architecture Decision: ADR-0127 (P127).
Phase 07: Milestone M04 - Shared Medicine Catalog & Dynamic Packaging Units.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import Manufacturer, Supplier
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.core.manufacturer_supplier")


class ManufacturerSupplierService:
    """
    Service layer governing manufacturer (global shared master) and
    supplier (tenant-scoped) reference entities with merge policies and archival.
    """

    # ==========================================
    # 1. MANUFACTURER (GLOBAL MASTER)
    # ==========================================

    @classmethod
    def serialize_manufacturer(cls, m: Manufacturer) -> Dict[str, Any]:
        return {
            "entity_type": "MANUFACTURER",
            "entity_id": m.manufacturer_id,
            "manufacturer_id": m.manufacturer_id,
            "name": m.name,
            "name_bn": m.name_bn,
            "code": m.code,
            "dgda_license_no": m.dgda_license_no,
            "country": m.country,
            "website": m.website,
            "is_active": m.is_active,
            "merged_into_id": m.merged_into_id,
            "merge_reason": m.merge_reason,
            "merged_at": m.merged_at.isoformat() if m.merged_at else None,
            "created_at": m.created_at.isoformat(),
        }

    @classmethod
    def list_manufacturers(
        cls,
        search: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        qs = Manufacturer.objects.all()
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if search:
            q = search.strip()
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(name_bn__icontains=q)
                | Q(code__icontains=q)
                | Q(dgda_license_no__icontains=q)
            )
        return [cls.serialize_manufacturer(m) for m in qs]

    @classmethod
    def get_manufacturer(cls, manufacturer_id: str) -> Dict[str, Any]:
        m = Manufacturer.objects.filter(manufacturer_id=manufacturer_id).first()
        if not m:
            raise ValidationError(
                {
                    "error_code": "ERR_MANUFACTURER_NOT_FOUND",
                    "message_en": f"Manufacturer '{manufacturer_id}' not found.",
                    "message_bn": f"উত্পাদক প্রতিষ্ঠান '{manufacturer_id}' পাওয়া যায়নি।",
                }
            )
        return cls.serialize_manufacturer(m)

    @classmethod
    def create_manufacturer(cls, actor_user, data: Dict[str, Any]) -> Dict[str, Any]:
        if not getattr(actor_user, "is_superuser", False) and getattr(actor_user, "role", "") != UserRole.SUPERADMIN:
            raise PermissionDenied(
                {
                    "error_code": "PLATFORM_CATALOG_OWNERSHIP_PROTECTED",
                    "message_en": "Pharmaceutical manufacturers are centrally curated by platform administrators.",
                    "message_bn": "ঔষধ প্রস্তুতকারক প্রতিষ্ঠানসমূহ কেন্দ্রীয়ভাবে প্ল্যাটফর্ম অ্যাডমিনিস্ট্রেটর দ্বারা পরিচালিত।",
                }
            )

        name = data.get("name", "").strip()
        if not name:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_MANUFACTURER_NAME",
                    "message_en": "Manufacturer name is required.",
                    "message_bn": "উত্পাদক প্রতিষ্ঠানের নাম আবশ্যক।",
                }
            )

        if Manufacturer.objects.filter(name__iexact=name).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_MANUFACTURER_NAME",
                    "message_en": f"Manufacturer with name '{name}' already exists.",
                    "message_bn": f"উত্পাদক প্রতিষ্ঠান '{name}' ইতিমধ্যেই নিবন্ধিত আছে।",
                }
            )

        code = data.get("code", "").strip().upper() or name.upper()[:12].replace(" ", "_")
        if Manufacturer.objects.filter(code=code).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_MANUFACTURER_CODE",
                    "message_en": f"Manufacturer code '{code}' already exists.",
                    "message_bn": f"উত্পাদক কোড '{code}' ইতিমধ্যেই বিদ্যমান।",
                }
            )

        m_id = data.get("manufacturer_id") or f"mfr-{uuid.uuid4().hex[:12]}"
        m = Manufacturer.objects.create(
            manufacturer_id=m_id,
            name=name,
            name_bn=data.get("name_bn", ""),
            code=code,
            dgda_license_no=data.get("dgda_license_no", ""),
            country=data.get("country", "BD"),
            website=data.get("website", ""),
            is_active=data.get("is_active", True),
        )
        return cls.serialize_manufacturer(m)

    @classmethod
    def merge_manufacturers(
        cls,
        actor_user,
        source_id: str,
        target_id: str,
        reason: str = "Duplicate entity consolidation",
    ) -> Dict[str, Any]:
        """Merges a duplicate source manufacturer into a canonical target survivor."""
        if not getattr(actor_user, "is_superuser", False) and getattr(actor_user, "role", "") != UserRole.SUPERADMIN:
            raise PermissionDenied(
                {
                    "error_code": "PLATFORM_CATALOG_OWNERSHIP_PROTECTED",
                    "message_en": "Manufacturer merging is restricted to platform super administrators.",
                    "message_bn": "উত্পাদক প্রতিষ্ঠান মার্জ করার ক্ষমতা প্ল্যাটফর্ম অ্যাডমিনিস্ট্রেটরের জন্য সংরক্ষিত।",
                }
            )

        if source_id == target_id:
            raise ValidationError(
                {
                    "error_code": "ERR_CANNOT_MERGE_SELF",
                    "message_en": "Cannot merge a manufacturer into itself.",
                    "message_bn": "একই প্রতিষ্ঠানকে নিজের সাথে মার্জ করা যাবে না।",
                }
            )

        source = Manufacturer.objects.filter(manufacturer_id=source_id).first()
        if not source:
            raise ValidationError({"error_code": "ERR_SOURCE_NOT_FOUND", "message_en": "Source manufacturer not found."})

        target = Manufacturer.objects.filter(manufacturer_id=target_id).first()
        if not target:
            raise ValidationError({"error_code": "ERR_TARGET_NOT_FOUND", "message_en": "Target manufacturer not found."})

        if not target.is_active or target.merged_into_id:
            raise ValidationError(
                {
                    "error_code": "ERR_TARGET_ALREADY_MERGED",
                    "message_en": "Target manufacturer is inactive or has already been merged into another entity.",
                    "message_bn": "লক্ষ্য প্রতিষ্ঠানটি নিষ্ক্রিয় বা ইতিপূর্বে মার্জ করা হয়েছে।",
                }
            )

        # Execute merge policy
        source.merged_into = target
        source.is_active = False
        source.merge_reason = reason
        source.merged_at = timezone.now()
        source.save(update_fields=["merged_into", "is_active", "merge_reason", "merged_at", "updated_at"])

        return {
            "entity_type": "MANUFACTURER",
            "source_id": source.manufacturer_id,
            "target_id": target.manufacturer_id,
            "target_name": target.name,
            "is_merged": True,
            "merge_reason": reason,
            "merged_at": source.merged_at.isoformat(),
            "message_en": f"Successfully merged '{source.name}' into '{target.name}'.",
            "message_bn": f"'{source.name}' সফলভাবে '{target.name}'-এ মার্জ সম্পন্ন হয়েছে।",
        }

    # ==========================================
    # 2. SUPPLIER (TENANT-SCOPED)
    # ==========================================

    @classmethod
    def serialize_supplier(cls, s: Supplier) -> Dict[str, Any]:
        return {
            "entity_type": "SUPPLIER",
            "entity_id": s.supplier_id,
            "supplier_id": s.supplier_id,
            "tenant_id": s.tenant_id,
            "name": s.name,
            "name_bn": s.name_bn,
            "code": s.code,
            "contact_person": s.contact_person,
            "phone": s.phone,
            "email": s.email,
            "address": s.address,
            "dl_number": s.dl_number,
            "tax_id": s.tax_id,
            "is_active": s.is_active,
            "merged_into_id": s.merged_into_id,
            "merge_reason": s.merge_reason,
            "merged_at": s.merged_at.isoformat() if s.merged_at else None,
            "created_at": s.created_at.isoformat(),
        }

    @classmethod
    def list_suppliers(
        cls,
        tenant_id: str,
        search: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        qs = Supplier.objects.filter(tenant_id=tenant_id)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if search:
            q = search.strip()
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(name_bn__icontains=q)
                | Q(contact_person__icontains=q)
                | Q(phone__icontains=q)
                | Q(dl_number__icontains=q)
            )
        return [cls.serialize_supplier(s) for s in qs]

    @classmethod
    def get_supplier(cls, tenant_id: str, supplier_id: str) -> Dict[str, Any]:
        s = Supplier.objects.filter(supplier_id=supplier_id).first()
        if not s:
            raise ValidationError(
                {
                    "error_code": "ERR_SUPPLIER_NOT_FOUND",
                    "message_en": f"Supplier '{supplier_id}' not found.",
                    "message_bn": f"সরবরাহকারী প্রতিষ্ঠান '{supplier_id}' পাওয়া যায়নি।",
                }
            )
        if s.tenant_id != tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "message_en": "Cross-tenant supplier access is strictly forbidden.",
                    "message_bn": "অন্য ফার্মেসির সরবরাহকারী তথ্য দেখা বা পরিবর্তন নিষিদ্ধ।",
                }
            )
        return cls.serialize_supplier(s)

    @classmethod
    def create_supplier(cls, tenant_id: str, actor_user, data: Dict[str, Any]) -> Dict[str, Any]:
        role = getattr(actor_user, "role", "")
        if role in [UserRole.CASHIER]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": "Cashier role is not permitted to register new suppliers.",
                    "message_bn": "ক্যাশিয়ার নতুন সরবরাহকারী নিবন্ধনের অনুমতিপ্রাপ্ত নন।",
                }
            )

        name = data.get("name", "").strip()
        if not name:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_SUPPLIER_NAME",
                    "message_en": "Supplier name is required.",
                    "message_bn": "সরবরাহকারী প্রতিষ্ঠানের নাম আবশ্যক।",
                }
            )

        if Supplier.objects.filter(tenant_id=tenant_id, name__iexact=name, is_active=True).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_SUPPLIER_NAME",
                    "message_en": f"Active supplier with name '{name}' already exists for this pharmacy.",
                    "message_bn": f"এই ফার্মেসির জন্য '{name}' নামে সক্রিয় সরবরাহকারী ইতিমধ্যেই বিদ্যমান।",
                }
            )

        s_id = data.get("supplier_id") or f"sup-{uuid.uuid4().hex[:12]}"
        s = Supplier.objects.create(
            supplier_id=s_id,
            tenant_id=tenant_id,
            name=name,
            name_bn=data.get("name_bn", ""),
            code=data.get("code", ""),
            contact_person=data.get("contact_person", ""),
            phone=data.get("phone", ""),
            email=data.get("email", ""),
            address=data.get("address", ""),
            dl_number=data.get("dl_number", ""),
            tax_id=data.get("tax_id", ""),
            is_active=data.get("is_active", True),
        )
        return cls.serialize_supplier(s)

    @classmethod
    def merge_suppliers(
        cls,
        tenant_id: str,
        actor_user,
        source_id: str,
        target_id: str,
        reason: str = "Consolidation of duplicate supplier entries",
    ) -> Dict[str, Any]:
        """Merges duplicate supplier into a canonical survivor within tenant boundary."""
        role = getattr(actor_user, "role", "")
        if role in [UserRole.CASHIER]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": "Cashier role is not permitted to merge suppliers.",
                    "message_bn": "ক্যাশিয়ার সরবরাহকারী মার্জ করার অনুমতিপ্রাপ্ত নন।",
                }
            )

        if source_id == target_id:
            raise ValidationError(
                {
                    "error_code": "ERR_CANNOT_MERGE_SELF",
                    "message_en": "Cannot merge a supplier into itself.",
                    "message_bn": "একই সরবরাহকারীকে নিজের সাথে মার্জ করা যাবে না।",
                }
            )

        source = Supplier.objects.filter(supplier_id=source_id).first()
        if not source:
            raise ValidationError({"error_code": "ERR_SOURCE_NOT_FOUND", "message_en": "Source supplier not found."})

        target = Supplier.objects.filter(supplier_id=target_id).first()
        if not target:
            raise ValidationError({"error_code": "ERR_TARGET_NOT_FOUND", "message_en": "Target supplier not found."})

        # STRICT TENANT ISOLATION CHECK
        if source.tenant_id != tenant_id or target.tenant_id != tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "message_en": "Suppliers belonging to different tenants cannot be merged.",
                    "message_bn": "ভিন্ন ফার্মেসির সরবরাহকারী প্রতিষ্ঠান মার্জ করা সম্পূর্ণ নিষিদ্ধ।",
                }
            )

        if not target.is_active or target.merged_into_id:
            raise ValidationError(
                {
                    "error_code": "ERR_TARGET_ALREADY_MERGED",
                    "message_en": "Target supplier is inactive or already merged into another entity.",
                    "message_bn": "লক্ষ্য সরবরাহকারীটি নিষ্ক্রিয় বা ইতিমধ্যে মার্জ করা হয়েছে।",
                }
            )

        source.merged_into = target
        source.is_active = False
        source.merge_reason = reason
        source.merged_at = timezone.now()
        source.save(update_fields=["merged_into", "is_active", "merge_reason", "merged_at", "updated_at"])

        return {
            "entity_type": "SUPPLIER",
            "source_id": source.supplier_id,
            "target_id": target.supplier_id,
            "target_name": target.name,
            "is_merged": True,
            "merge_reason": reason,
            "merged_at": source.merged_at.isoformat(),
            "message_en": f"Successfully merged '{source.name}' into '{target.name}'.",
            "message_bn": f"'{source.name}' সফলভাবে '{target.name}'-এ মার্জ সম্পন্ন হয়েছে।",
        }

    @classmethod
    def archive_supplier(cls, tenant_id: str, actor_user, supplier_id: str) -> Dict[str, Any]:
        """Soft-deletes / archives a supplier."""
        role = getattr(actor_user, "role", "")
        if role in [UserRole.CASHIER]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": "Cashier role is not permitted to archive suppliers.",
                    "message_bn": "ক্যাশিয়ার সরবরাহকারী নিষ্ক্রিয় করার অনুমতিপ্রাপ্ত নন।",
                }
            )

        s = Supplier.objects.filter(supplier_id=supplier_id).first()
        if not s:
            raise ValidationError({"error_code": "ERR_SUPPLIER_NOT_FOUND", "message_en": "Supplier not found."})

        if s.tenant_id != tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "message_en": "Cross-tenant supplier operation forbidden.",
                    "message_bn": "অন্য ফার্মেসির সরবরাহকারী নিষ্ক্রিয় করা যাবে না।",
                }
            )

        s.is_active = False
        s.save(update_fields=["is_active", "updated_at"])
        return cls.serialize_supplier(s)
