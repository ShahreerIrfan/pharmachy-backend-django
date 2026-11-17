"""
backend/apps/core/inspection_evidence_service.py

Architecture Decision: ADR-0254 (P254).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Service layer for statutory Inspection Evidence Repository:
- Refrigerator temperature logs (cold-chain compliance 2°C - 8°C)
- Staff Good Pharmacy Practice (GPP) & antibiotic stewardship training proofs
- Expired stock disposal & quarantine destruction manifests
- Pest control, sanitation, calibration, and DGDA inspection memos
- Role-based protected document clearance and SHA-256 integrity verification
- Verifiable DGDA audit package exports
"""

from decimal import Decimal
import datetime
import hashlib
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    InspectionEvidenceAccessLevel,
    InspectionEvidenceRecord,
    InspectionEvidenceType,
    Tenant,
)
from apps.core.pos_domain_service import PosDomainService


class InspectionEvidenceService:
    """
    Statutory Inspection Evidence Repository Domain Service.
    Enforces DGDA Good Pharmacy Practice (2024) compliance documentation standards.
    """

    ALLOWED_READ_ROLES = ["SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST", "CASHIER"]
    ALLOWED_MANAGE_ROLES = ["SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST"]
    AUDIT_EXPORT_ROLES = ["SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER"]

    @classmethod
    def serialize_evidence(cls, record: InspectionEvidenceRecord, user: Any = None) -> Dict[str, Any]:
        """
        Serializes InspectionEvidenceRecord, redacting confidential document URIs
        for non-privileged callers.
        """
        can_view = record.can_view_document(user) if user else False
        document_uri = record.evidence_document_uri if can_view else "[REDACTED_CONFIDENTIAL_EVIDENCE]"

        return {
            "evidence_id": record.evidence_id,
            "tenant_id": str(record.tenant_id),
            "branch_id": str(record.branch_id),
            "branch_name": record.branch.name if record.branch else "",
            "evidence_type": record.evidence_type,
            "evidence_type_display": record.get_evidence_type_display(),
            "title": record.title,
            "description": record.description,
            "log_date": record.log_date.isoformat() if record.log_date else None,
            "evidence_document_uri": document_uri,
            "document_accessible": can_view,
            "file_name": record.file_name,
            "mime_type": record.mime_type,
            "file_size_bytes": record.file_size_bytes,
            "sha256_hash": record.sha256_hash,
            "access_level": record.access_level,
            "metadata": record.metadata or {},
            "is_verified": record.is_verified,
            "verified_by_id": str(record.verified_by_id) if record.verified_by_id else None,
            "verified_at": record.verified_at.isoformat() if record.verified_at else None,
            "verification_notes": record.verification_notes,
            "is_archived": record.is_archived,
            "archived_at": record.archived_at.isoformat() if record.archived_at else None,
            "archival_reason": record.archival_reason,
            "created_by_id": str(record.created_by_id) if record.created_by_id else None,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        }

    @classmethod
    def list_evidence(
        cls,
        user: Any,
        branch_id: Optional[str] = None,
        evidence_type: Optional[str] = None,
        access_level: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        include_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Lists inspection evidence records scoped to caller's tenant and clearance.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )

        qs = InspectionEvidenceRecord.objects.filter(tenant=tenant).select_related("branch")
        if not include_archived:
            qs = qs.filter(is_archived=False)

        user_role = str(getattr(user, "role", "")).upper()
        if user_role in ("BRANCH_MANAGER", "STAFF_PHARMACIST"):
            user_branch_id = getattr(user, "branch_id", None) or (user.branch.branch_id if getattr(user, "branch", None) else None)
            if user_branch_id:
                qs = qs.filter(branch_id=user_branch_id)
        elif branch_id:
            qs = qs.filter(branch_id=str(branch_id).strip())

        if evidence_type:
            qs = qs.filter(evidence_type=str(evidence_type).strip().upper())
        if access_level:
            qs = qs.filter(access_level=str(access_level).strip().upper())

        if start_date:
            try:
                s_date = datetime.date.fromisoformat(str(start_date).strip())
                qs = qs.filter(log_date__gte=s_date)
            except ValueError:
                raise ValidationError({"start_date": "Invalid date format. Expected YYYY-MM-DD."})

        if end_date:
            try:
                e_date = datetime.date.fromisoformat(str(end_date).strip())
                qs = qs.filter(log_date__lte=e_date)
            except ValueError:
                raise ValidationError({"end_date": "Invalid date format. Expected YYYY-MM-DD."})

        # Cashier role can only view PUBLIC records
        if user_role == "CASHIER":
            qs = qs.filter(access_level=InspectionEvidenceAccessLevel.PUBLIC)

        return [cls.serialize_evidence(rec, user=user) for rec in qs]

    @classmethod
    def get_evidence(cls, user: Any, evidence_id: str) -> Dict[str, Any]:
        """
        Retrieves single inspection evidence record.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )

        record = InspectionEvidenceRecord.objects.filter(
            evidence_id=str(evidence_id).strip(),
            tenant=tenant,
        ).select_related("branch").first()

        if not record:
            raise NotFound({"evidence_id": f"Inspection evidence record '{evidence_id}' not found."})

        # Cashier role blocked if not public
        user_role = str(getattr(user, "role", "")).upper()
        if user_role == "CASHIER" and record.access_level != InspectionEvidenceAccessLevel.PUBLIC:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_INSPECTION_EVIDENCE_ACCESS_RESTRICTED",
                    "message_en": "Cashiers are not authorized to view confidential inspection evidence.",
                    "message_bn": "ক্যাশিয়ারদের গোপনীয় পরিদর্শন প্রমাণপত্র দেখার অনুমতি নেই।",
                }
            )

        return cls.serialize_evidence(record, user=user)

    @classmethod
    def get_evidence_document(cls, user: Any, evidence_id: str) -> Dict[str, Any]:
        """
        Returns unredacted evidence document details.
        Strictly enforces clearance rules; raises 403 on insufficient clearance.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.ALLOWED_READ_ROLES
        )

        record = InspectionEvidenceRecord.objects.filter(
            evidence_id=str(evidence_id).strip(),
            tenant=tenant,
        ).select_related("branch").first()

        if not record:
            raise NotFound({"evidence_id": f"Inspection evidence record '{evidence_id}' not found."})

        if not record.can_view_document(user):
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_INSPECTION_EVIDENCE_ACCESS_RESTRICTED",
                    "message_en": "You do not have clearance to view this classified inspection document.",
                    "message_bn": "আপনার এই গোপনীয় পরিদর্শন নথিটি দেখার অনুমতি নেই।",
                }
            )

        return {
            "evidence_id": record.evidence_id,
            "evidence_type": record.evidence_type,
            "title": record.title,
            "evidence_document_uri": record.evidence_document_uri,
            "file_name": record.file_name,
            "mime_type": record.mime_type,
            "file_size_bytes": record.file_size_bytes,
            "sha256_hash": record.sha256_hash,
            "access_level": record.access_level,
        }

    @classmethod
    @transaction.atomic
    def create_or_update_evidence(
        cls,
        user: Any,
        data: Dict[str, Any],
        evidence_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Creates or updates statutory inspection evidence record.
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials required.")

        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in cls.ALLOWED_MANAGE_ROLES:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_INSPECTION_EVIDENCE_MANAGE_UNAUTHORIZED",
                    "message_en": "Only Pharmacy Owners, Managers, and Pharmacists can manage inspection evidence.",
                    "message_bn": "শুধুমাত্র ফার্মেসি মালিক, ম্যানেজার ও ফার্মাসিস্ট পরিদর্শন প্রমাণপত্র পরিচালনা করতে পারবেন।",
                }
            )

        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=True, allowed_roles=cls.ALLOWED_MANAGE_ROLES
        )

        record = None
        if evidence_id:
            record = InspectionEvidenceRecord.objects.filter(
                evidence_id=str(evidence_id).strip(),
                tenant=tenant,
            ).first()
            if not record:
                raise NotFound({"evidence_id": f"Inspection evidence record '{evidence_id}' not found."})

        # 1. Title Validation
        raw_title = data.get("title")
        title = str(raw_title).strip() if raw_title is not None else (record.title if record else "")
        if len(title) < 3:
            raise ValidationError({"title": "Evidence title (minimum 3 characters) is required."})

        # 2. Evidence Type Validation
        raw_ev_type = data.get("evidence_type")
        ev_type = str(raw_ev_type).strip().upper() if raw_ev_type is not None else (record.evidence_type if record else "")
        if ev_type not in InspectionEvidenceType.values:
            raise ValidationError({
                "evidence_type": f"Invalid evidence type '{ev_type}'. Must be one of: {list(InspectionEvidenceType.values)}"
            })

        # Pharmacists cannot upload RESTRICTED DGDA memos unless manager/owner
        raw_access_level = data.get("access_level")
        access_level = str(raw_access_level).strip().upper() if raw_access_level is not None else (record.access_level if record else InspectionEvidenceAccessLevel.CONFIDENTIAL)
        if access_level not in InspectionEvidenceAccessLevel.values:
            raise ValidationError({
                "access_level": f"Invalid access level '{access_level}'. Must be one of: {list(InspectionEvidenceAccessLevel.values)}"
            })
        if access_level == InspectionEvidenceAccessLevel.RESTRICTED and user_role not in ("SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER"):
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_INSPECTION_EVIDENCE_RESTRICTED_CLEARANCE",
                    "message_en": "Only Pharmacy Owners and Branch Managers can create or manage RESTRICTED inspection records.",
                    "message_bn": "শুধুমাত্র মালিক ও ব্রাঞ্চ ম্যানেজার সংরক্ষিত পরিদর্শন রেকর্ড পরিচালনা করতে পারবেন।",
                }
            )

        # 3. Branch Validation
        branch_id = data.get("branch_id") or getattr(user, "branch_id", None)
        branch = None
        if branch_id:
            branch = Branch.objects.filter(branch_id=str(branch_id).strip(), tenant=tenant).first()
            if not branch:
                raise ValidationError({"branch_id": f"Branch '{branch_id}' does not exist for this tenant."})
        elif not record:
            raise ValidationError({"branch_id": "branch_id is required."})

        # 4. Log Date Validation
        log_date_raw = data.get("log_date")
        if log_date_raw:
            try:
                log_date = datetime.date.fromisoformat(str(log_date_raw).strip())
            except ValueError:
                raise ValidationError({"log_date": "Invalid log_date format. Expected YYYY-MM-DD."})
        elif record:
            log_date = record.log_date
        else:
            raise ValidationError({"log_date": "log_date is required (format YYYY-MM-DD)."})

        today = timezone.localdate() if hasattr(timezone, "localdate") else datetime.date.today()
        if log_date > today:
            raise ValidationError({"log_date": "log_date cannot be in the future."})

        # 5. Document URI and Metadata Validation
        raw_doc_uri = data.get("evidence_document_uri")
        doc_uri = str(raw_doc_uri).strip() if raw_doc_uri is not None else (record.evidence_document_uri if record else "")
        if not doc_uri:
            raise ValidationError({"evidence_document_uri": "evidence_document_uri is required."})

        file_name = str(data.get("file_name") or (record.file_name if record else "evidence.pdf")).strip()
        mime_type = str(data.get("mime_type") or (record.mime_type if record else "application/pdf")).strip()
        file_size = int(data.get("file_size_bytes") if data.get("file_size_bytes") is not None else (record.file_size_bytes if record else 0))
        sha256_hash = str(data.get("sha256_hash") or (record.sha256_hash if record else "")).strip().lower()

        # Domain specific metadata checks
        metadata = dict(data.get("metadata") if data.get("metadata") is not None else (record.metadata if record else {}))
        if ev_type == InspectionEvidenceType.REFRIGERATOR_TEMPERATURE_LOG:
            min_temp = metadata.get("min_temp_celsius")
            max_temp = metadata.get("max_temp_celsius")
            if min_temp is not None and max_temp is not None:
                try:
                    min_f = float(min_temp)
                    max_f = float(max_temp)
                    if min_f > max_f:
                        raise ValidationError({"metadata": "min_temp_celsius cannot exceed max_temp_celsius."})
                    metadata["is_cold_chain_excursion"] = min_f < 2.0 or max_f > 8.0
                except (ValueError, TypeError):
                    raise ValidationError({"metadata": "min_temp_celsius and max_temp_celsius must be numbers."})

        elif ev_type == InspectionEvidenceType.EXPIRED_STOCK_DISPOSAL:
            disposal_value = metadata.get("disposal_value_bdt")
            if disposal_value is not None:
                try:
                    dec_val = Decimal(str(disposal_value))
                    if dec_val < Decimal("0.00"):
                        raise ValidationError({"metadata": "disposal_value_bdt cannot be negative."})
                    metadata["disposal_value_bdt"] = str(dec_val)
                except Exception:
                    raise ValidationError({"metadata": "disposal_value_bdt must be a valid non-negative decimal."})

        # Create or update
        if record:
            if branch:
                record.branch = branch
            record.title = title
            record.evidence_type = ev_type
            record.description = str(data.get("description") or record.description).strip()
            record.log_date = log_date
            record.evidence_document_uri = doc_uri
            record.file_name = file_name
            record.mime_type = mime_type
            record.file_size_bytes = file_size
            record.access_level = access_level
            record.metadata = metadata
            if sha256_hash:
                record.sha256_hash = sha256_hash
            if data.get("is_verified") is True:
                record.is_verified = True
                record.verified_by = user
                record.verified_at = timezone.now()
                record.verification_notes = str(data.get("verification_notes") or record.verification_notes).strip()
            record.save()
        else:
            new_id = f"iev-{uuid.uuid4().hex[:12]}"
            if not sha256_hash:
                raw_hash = f"T:{tenant.tenant_id}|B:{branch.branch_id}|TYPE:{ev_type}|DATE:{log_date}|URI:{doc_uri}|TITLE:{title}"
                sha256_hash = hashlib.sha256(raw_hash.encode("utf-8")).hexdigest()

            is_verified = bool(data.get("is_verified", False))
            verified_by = user if is_verified else None
            verified_at = timezone.now() if is_verified else None

            record = InspectionEvidenceRecord.objects.create(
                evidence_id=new_id,
                tenant=tenant,
                branch=branch,
                evidence_type=ev_type,
                title=title,
                description=str(data.get("description") or "").strip(),
                log_date=log_date,
                evidence_document_uri=doc_uri,
                file_name=file_name,
                mime_type=mime_type,
                file_size_bytes=file_size,
                sha256_hash=sha256_hash,
                access_level=access_level,
                metadata=metadata,
                is_verified=is_verified,
                verified_by=verified_by,
                verified_at=verified_at,
                verification_notes=str(data.get("verification_notes") or "").strip(),
                created_by=user,
            )

        return cls.serialize_evidence(record, user=user)

    @classmethod
    @transaction.atomic
    def archive_evidence(cls, user: Any, evidence_id: str, reason: str = "") -> Dict[str, Any]:
        """
        Soft-archives an inspection evidence record with audit tracking.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=True, allowed_roles=cls.ALLOWED_MANAGE_ROLES
        )

        record = InspectionEvidenceRecord.objects.filter(
            evidence_id=str(evidence_id).strip(),
            tenant=tenant,
        ).first()

        if not record:
            raise NotFound({"evidence_id": f"Inspection evidence record '{evidence_id}' not found."})

        record.is_archived = True
        record.archived_at = timezone.now()
        record.archived_by = user
        record.archival_reason = str(reason or "Administrative archival").strip()
        record.save()

        return {"evidence_id": record.evidence_id, "is_archived": True, "archived_at": record.archived_at.isoformat()}

    @classmethod
    def export_audit_package(
        cls,
        user: Any,
        branch_id: str,
        start_date: str,
        end_date: str,
        evidence_types: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Compiles sealed DGDA statutory audit evidence bundle with tamper-evident checksums.
        Restricted to Owners, Managers, and Superadmins.
        """
        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in cls.AUDIT_EXPORT_ROLES:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_AUDIT_PACKAGE_EXPORT_UNAUTHORIZED",
                    "message_en": "Only Pharmacy Owners and Branch Managers can export DGDA audit packages.",
                    "message_bn": "শুধুমাত্র ফার্মেসি মালিক ও ব্রাঞ্চ ম্যানেজার ডিজিডিএ অডিট প্যাকেজ এক্সপোর্ট করতে পারেন।",
                }
            )

        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=cls.AUDIT_EXPORT_ROLES
        )

        branch = Branch.objects.filter(branch_id=str(branch_id).strip(), tenant=tenant).first()
        if not branch:
            raise NotFound({"branch_id": f"Branch '{branch_id}' not found."})

        try:
            s_date = datetime.date.fromisoformat(str(start_date).strip())
            e_date = datetime.date.fromisoformat(str(end_date).strip())
        except ValueError:
            raise ValidationError("Invalid start_date or end_date. Expected YYYY-MM-DD.")

        if s_date > e_date:
            raise ValidationError("start_date cannot be later than end_date.")

        qs = InspectionEvidenceRecord.objects.filter(
            tenant=tenant,
            branch=branch,
            is_archived=False,
            log_date__gte=s_date,
            log_date__lte=e_date,
        ).order_by("log_date")

        if evidence_types:
            clean_types = [str(t).strip().upper() for t in evidence_types]
            qs = qs.filter(evidence_type__in=clean_types)

        records_list = list(qs)
        evidence_items = []
        category_counts: Dict[str, int] = {}
        total_disposal_bdt = Decimal("0.00")
        cold_chain_excursion_count = 0

        for r in records_list:
            category_counts[r.evidence_type] = category_counts.get(r.evidence_type, 0) + 1
            if r.evidence_type == InspectionEvidenceType.REFRIGERATOR_TEMPERATURE_LOG:
                if (r.metadata or {}).get("is_cold_chain_excursion"):
                    cold_chain_excursion_count += 1
            elif r.evidence_type == InspectionEvidenceType.EXPIRED_STOCK_DISPOSAL:
                val = (r.metadata or {}).get("disposal_value_bdt")
                if val:
                    try:
                        total_disposal_bdt += Decimal(str(val))
                    except Exception:
                        pass

            evidence_items.append({
                "evidence_id": r.evidence_id,
                "evidence_type": r.evidence_type,
                "title": r.title,
                "log_date": r.log_date.isoformat(),
                "sha256_hash": r.sha256_hash,
                "is_verified": r.is_verified,
                "file_name": r.file_name,
            })

        # Compute composite manifest checksum
        manifest_raw = f"TENANT:{tenant.tenant_id}|BRANCH:{branch.branch_id}|FROM:{s_date}|TO:{e_date}|COUNT:{len(evidence_items)}"
        for item in evidence_items:
            manifest_raw += f"|{item['evidence_id']}:{item['sha256_hash']}"
        package_manifest_hash = hashlib.sha256(manifest_raw.encode("utf-8")).hexdigest()

        return {
            "package_id": f"dgda-pkg-{uuid.uuid4().hex[:12]}",
            "tenant_id": str(tenant.tenant_id),
            "branch_id": str(branch.branch_id),
            "branch_name": branch.name,
            "audit_period_start": s_date.isoformat(),
            "audit_period_end": e_date.isoformat(),
            "generated_at": timezone.now().isoformat(),
            "generated_by_user": getattr(user, "email", str(user)),
            "package_manifest_sha256": package_manifest_hash,
            "total_records": len(evidence_items),
            "category_distribution": category_counts,
            "cold_chain_excursion_count": cold_chain_excursion_count,
            "total_expired_stock_disposal_bdt": str(total_disposal_bdt),
            "is_inspection_ready": len(evidence_items) > 0,
            "evidence_manifest": evidence_items,
        }

