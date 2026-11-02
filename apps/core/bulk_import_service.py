"""
backend/apps/core/bulk_import_service.py
Architecture Decision: ADR-0132 (P132).
Core service for Medicine Catalog Bulk Import Wizard:
CSV Parsing, Dynamic Field Mapping, Duplicate Policies, Atomic Commit & Rollback.
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    ImportDuplicatePolicy,
    ImportSessionStatus,
    MedicineCurationStatus,
    MedicineImportSession,
    MedicineProduct,
)
from apps.identity.models import PharmacyUser

logger = logging.getLogger("pharmacy.core.bulk_import")


class MedicineBulkImportService:
    """
    Transactional service governing the multi-stage bulk import workflow for medicine catalogs.
    """

    CANONICAL_FIELDS = {
        "brand_name": {"required": True, "type": "string", "label_en": "Brand Name"},
        "generic_name": {"required": True, "type": "string", "label_en": "Generic Name"},
        "dosage_form": {"required": True, "type": "string", "label_en": "Dosage Form"},
        "strength": {"required": True, "type": "string", "label_en": "Strength"},
        "manufacturer": {"required": True, "type": "string", "label_en": "Manufacturer"},
        "mrp_unit": {"required": True, "type": "decimal", "label_en": "MRP Unit Price (BDT)"},
        "cost_unit": {"required": False, "type": "decimal", "label_en": "Cost Unit Price (BDT)"},
        "barcode": {"required": False, "type": "string", "label_en": "Barcode / GTIN"},
        "route": {"required": False, "type": "string", "label_en": "Administration Route"},
        "is_prescription_only": {"required": False, "type": "boolean", "label_en": "Prescription Only"},
    }

    HEADER_SYNONYMS = {
        "brand_name": ["brand", "brand_name", "trade_name", "item_name", "medicine_name", "product_name", "name"],
        "generic_name": ["generic", "generic_name", "molecule", "inn", "composition", "active_ingredient"],
        "dosage_form": ["form", "dosage_form", "dosage", "type", "form_type"],
        "strength": ["strength", "power", "dose", "potency"],
        "manufacturer": ["manufacturer", "company", "maker", "pharma", "producer"],
        "mrp_unit": ["mrp", "mrp_unit", "price", "retail_price", "unit_price", "selling_price", "sale_price"],
        "cost_unit": ["cost", "cost_unit", "trade_price", "tp", "purchase_price", "buying_price"],
        "barcode": ["barcode", "upc", "ean", "gtin", "code"],
        "route": ["route", "administration_route"],
        "is_prescription_only": ["rx", "pom", "prescription_only", "is_prescription"],
    }

    @classmethod
    def serialize_session(cls, session: MedicineImportSession) -> Dict[str, Any]:
        """Converts session to JSON schema compliant dictionary."""
        return {
            "session_id": session.session_id,
            "tenant_id": session.tenant_id,
            "status": session.status,
            "file_name": session.file_name,
            "file_format": session.file_format,
            "total_rows": session.total_rows,
            "valid_rows": session.valid_rows,
            "invalid_rows": session.invalid_rows,
            "duplicate_rows": session.duplicate_rows,
            "duplicate_policy": session.duplicate_policy,
            "raw_headers": session.raw_headers,
            "preview_rows": session.preview_rows,
            "field_mapping": session.field_mapping,
            "validation_errors": session.validation_errors,
            "created_count": len(session.created_record_ids),
            "updated_count": len(session.updated_record_ids),
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "committed_at": session.committed_at.isoformat() if session.committed_at else None,
            "rolled_back_at": session.rolled_back_at.isoformat() if session.rolled_back_at else None,
        }

    @classmethod
    def auto_suggest_mapping(cls, raw_headers: List[str]) -> Dict[str, str]:
        """Auto-detects matching canonical fields from raw CSV headers."""
        mapping: Dict[str, str] = {}
        for header in raw_headers:
            clean = header.strip().lower().replace(" ", "_").replace("-", "_")
            for canonical, synonyms in cls.HEADER_SYNONYMS.items():
                if clean in synonyms or clean == canonical:
                    mapping[header] = canonical
                    break
        return mapping

    @classmethod
    def create_session_from_csv(
        cls,
        file_name: str,
        csv_content: str,
        actor: PharmacyUser,
        target_tenant_id: str = "platform",
        duplicate_policy: str = "SKIP",
    ) -> MedicineImportSession:
        """
        Parses uploaded CSV text, creates MedicineImportSession, and generates preview data.
        """
        clean_content = str(csv_content or "").strip()
        if not clean_content:
            raise ValidationError({"csv_content": "CSV content cannot be empty."})

        # Detect delimiter
        sample = clean_content[:2048]
        delimiter = ","
        if ";" in sample and sample.count(";") > sample.count(","):
            delimiter = ";"
        elif "\t" in sample and sample.count("\t") > sample.count(","):
            delimiter = "\t"

        reader = csv.reader(io.StringIO(clean_content), delimiter=delimiter)
        try:
            raw_headers = next(reader)
        except StopIteration:
            raise ValidationError({"csv_content": "CSV content has no rows or headers."})

        raw_headers = [h.strip() for h in raw_headers if h.strip()]
        if not raw_headers:
            raise ValidationError({"csv_content": "No valid column headers found in CSV."})

        parsed_rows: List[Dict[str, str]] = []
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue  # skip completely blank rows
            row_dict = {}
            for idx, header in enumerate(raw_headers):
                val = row[idx].strip() if idx < len(row) else ""
                row_dict[header] = val
            parsed_rows.append(row_dict)

        if not parsed_rows:
            raise ValidationError({"csv_content": "CSV contains headers but no data rows."})

        initial_mapping = cls.auto_suggest_mapping(raw_headers)
        preview_rows = parsed_rows[:5]

        session = MedicineImportSession.objects.create(
            session_id=f"imp-{uuid.uuid4().hex[:12]}",
            tenant_id=target_tenant_id,
            status=ImportSessionStatus.UPLOADED,
            file_name=file_name.strip() or "import.csv",
            file_format="CSV",
            total_rows=len(parsed_rows),
            raw_headers=raw_headers,
            preview_rows=preview_rows,
            raw_rows_data=parsed_rows,
            field_mapping=initial_mapping,
            duplicate_policy=duplicate_policy if duplicate_policy in ImportDuplicatePolicy.values else ImportDuplicatePolicy.SKIP,
            initiated_by=actor,
        )

        AuditLogEntry.objects.create(
            action="MEDICINE_IMPORT_SESSION_CREATED",
            resource_type="MEDICINE_IMPORT",
            resource_id=session.session_id,
            tenant_id=target_tenant_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "file_name": session.file_name,
                "total_rows": session.total_rows,
                "headers": raw_headers,
            },
        )

        # Run initial pre-flight validation if all required fields mapped
        required_targets = {k for k, v in cls.CANONICAL_FIELDS.items() if v["required"]}
        mapped_targets = set(initial_mapping.values())
        if required_targets.issubset(mapped_targets):
            cls.apply_mapping_and_validate(session, initial_mapping, session.duplicate_policy)

        return session

    @classmethod
    def apply_mapping_and_validate(
        cls,
        session: MedicineImportSession,
        field_mapping: Dict[str, str],
        duplicate_policy: Optional[str] = None,
    ) -> MedicineImportSession:
        """
        Applies column mapping and performs dry-run pre-flight validation across all rows.
        """
        if session.status in (ImportSessionStatus.COMMITTED, ImportSessionStatus.ROLLED_BACK):
            raise ValidationError(
                {"status": f"Cannot modify mapping on an import session that is already {session.status}."}
            )

        if duplicate_policy and duplicate_policy in ImportDuplicatePolicy.values:
            session.duplicate_policy = duplicate_policy

        session.field_mapping = field_mapping
        session.status = ImportSessionStatus.VALIDATING

        raw_rows = session.raw_rows_data or []
        errors: List[Dict[str, Any]] = []
        valid_count = 0
        duplicate_count = 0

        # In-file duplicate tracking
        seen_barcodes = set()
        seen_triplets = set()

        # Database existing barcodes and triplets
        existing_barcodes = set(
            MedicineProduct.objects.exclude(barcode="").values_list("barcode", flat=True)
        )
        existing_triplets = set(
            MedicineProduct.objects.values_list("brand_name", "dosage_form", "strength")
        )

        for idx, row in enumerate(raw_rows, start=1):
            mapped_row: Dict[str, Any] = {}
            row_has_error = False

            # Extract mapped fields
            for src_col, target_field in field_mapping.items():
                if target_field in cls.CANONICAL_FIELDS:
                    mapped_row[target_field] = row.get(src_col, "").strip()

            # 1. Required field checks
            for field, spec in cls.CANONICAL_FIELDS.items():
                if spec["required"]:
                    val = mapped_row.get(field, "")
                    if not val:
                        errors.append(
                            {
                                "row_index": idx,
                                "field": field,
                                "error_message": f"Mandatory field '{spec['label_en']}' is missing or blank.",
                            }
                        )
                        row_has_error = True

            # 2. Decimal validation for prices
            mrp_raw = mapped_row.get("mrp_unit", "")
            if mrp_raw:
                try:
                    mrp_dec = Decimal(str(mrp_raw).replace(",", ""))
                    if mrp_dec <= Decimal("0.00"):
                        errors.append(
                            {
                                "row_index": idx,
                                "field": "mrp_unit",
                                "error_message": "MRP must be greater than zero BDT.",
                            }
                        )
                        row_has_error = True
                except (InvalidOperation, ValueError):
                    errors.append(
                        {
                            "row_index": idx,
                            "field": "mrp_unit",
                            "error_message": f"Invalid price format '{mrp_raw}'. Must be a valid decimal number.",
                        }
                    )
                    row_has_error = True

            cost_raw = mapped_row.get("cost_unit", "")
            if cost_raw:
                try:
                    cost_dec = Decimal(str(cost_raw).replace(",", ""))
                    if cost_dec < Decimal("0.00"):
                        errors.append(
                            {
                                "row_index": idx,
                                "field": "cost_unit",
                                "error_message": "Cost unit cannot be negative.",
                            }
                        )
                        row_has_error = True
                except (InvalidOperation, ValueError):
                    errors.append(
                        {
                            "row_index": idx,
                            "field": "cost_unit",
                            "error_message": f"Invalid cost format '{cost_raw}'.",
                        }
                    )
                    row_has_error = True

            # 3. Duplicate checks
            barcode = mapped_row.get("barcode", "").strip()
            brand = mapped_row.get("brand_name", "").strip().lower()
            form = mapped_row.get("dosage_form", "").strip().lower()
            strength = mapped_row.get("strength", "").strip().lower()
            triplet = (brand, form, strength)

            is_duplicate = False
            # Check in-file duplicate
            if barcode and barcode in seen_barcodes:
                is_duplicate = True
            elif triplet in seen_triplets and brand:
                is_duplicate = True

            # Check database duplicate
            if not is_duplicate:
                if barcode and barcode in existing_barcodes:
                    is_duplicate = True
                elif (mapped_row.get("brand_name", ""), mapped_row.get("dosage_form", ""), mapped_row.get("strength", "")) in existing_triplets:
                    is_duplicate = True

            if barcode:
                seen_barcodes.add(barcode)
            if brand:
                seen_triplets.add(triplet)

            if is_duplicate:
                duplicate_count += 1
                if session.duplicate_policy == ImportDuplicatePolicy.FAIL_ON_DUPLICATE:
                    errors.append(
                        {
                            "row_index": idx,
                            "field": "barcode" if barcode else "brand_name",
                            "error_message": f"Duplicate drug detected for '{mapped_row.get('brand_name')}' under FAIL_ON_DUPLICATE policy.",
                        }
                    )
                    row_has_error = True

            if not row_has_error:
                valid_count += 1

        session.valid_rows = valid_count
        session.invalid_rows = len(raw_rows) - valid_count
        session.duplicate_rows = duplicate_count
        session.validation_errors = errors
        session.status = ImportSessionStatus.VALIDATED
        session.save()
        return session

    @classmethod
    @transaction.atomic
    def commit_import(
        cls,
        session: MedicineImportSession,
        actor: PharmacyUser,
    ) -> Dict[str, Any]:
        """
        Commits valid imported records into MedicineProduct in an atomic transaction.
        Records created product IDs for rollback safety.
        """
        if session.status == ImportSessionStatus.COMMITTED:
            raise ValidationError({"status": "ERR_IMPORT_ALREADY_COMMITTED: Import session has already been committed."})
        if session.status == ImportSessionStatus.ROLLED_BACK:
            raise ValidationError({"status": "ERR_IMPORT_ALREADY_ROLLED_BACK: Cannot commit a rolled-back import session."})

        # Ensure validation was run
        if session.status != ImportSessionStatus.VALIDATED:
            cls.apply_mapping_and_validate(session, session.field_mapping, session.duplicate_policy)

        if session.valid_rows == 0:
            raise ValidationError(
                {"status": f"Cannot commit import session with 0 valid rows ({session.invalid_rows} invalid rows)."}
            )

        raw_rows = session.raw_rows_data or []
        mapping = session.field_mapping or {}
        policy = session.duplicate_policy

        created_ids = []
        updated_ids = []

        existing_products_by_barcode = {
            p.barcode: p for p in MedicineProduct.objects.exclude(barcode="").all()
        }

        for row in raw_rows:
            mapped_row = {
                target: row.get(src, "").strip()
                for src, target in mapping.items()
                if target in cls.CANONICAL_FIELDS
            }

            brand = mapped_row.get("brand_name", "")
            generic = mapped_row.get("generic_name", "")
            form = mapped_row.get("dosage_form", "")
            strength = mapped_row.get("strength", "")
            mrp_str = mapped_row.get("mrp_unit", "")

            if not brand or not generic or not form or not strength or not mrp_str:
                continue  # skip invalid rows

            try:
                mrp_dec = Decimal(str(mrp_str).replace(",", ""))
                if mrp_dec <= Decimal("0.00"):
                    continue
            except Exception:
                continue

            cost_str = mapped_row.get("cost_unit", "")
            cost_dec = Decimal("0.00")
            if cost_str:
                try:
                    cost_dec = Decimal(str(cost_str).replace(",", ""))
                except Exception:
                    cost_dec = mrp_dec * Decimal("0.80")
            else:
                cost_dec = mrp_dec * Decimal("0.80")

            barcode = mapped_row.get("barcode", "").strip()
            manufacturer = mapped_row.get("manufacturer", "") or "Generic Pharma Ltd."
            route = mapped_row.get("route", "") or "Oral"

            # Check existing duplicate
            existing_product = None
            if barcode and barcode in existing_products_by_barcode:
                existing_product = existing_products_by_barcode[barcode]
            else:
                existing_product = MedicineProduct.objects.filter(
                    brand_name__iexact=brand,
                    dosage_form__iexact=form,
                    strength__iexact=strength,
                ).first()

            if existing_product:
                if policy == ImportDuplicatePolicy.SKIP:
                    continue
                elif policy == ImportDuplicatePolicy.FAIL_ON_DUPLICATE:
                    raise ValidationError(
                        {"duplicate": f"FAIL_ON_DUPLICATE policy violated by product '{brand}'."}
                    )
                elif policy == ImportDuplicatePolicy.UPDATE_OVERWRITE:
                    existing_product.mrp_unit = mrp_dec
                    existing_product.cost_unit = cost_dec
                    existing_product.manufacturer = manufacturer
                    existing_product.route = route
                    existing_product.version_number += 1
                    existing_product.immutable_version_hash = existing_product.compute_version_hash()
                    existing_product.save()
                    updated_ids.append(existing_product.product_id)
                    continue

            # Create new product
            new_pid = f"prod-{uuid.uuid4().hex[:12]}"
            new_product = MedicineProduct.objects.create(
                product_id=new_pid,
                brand_name=brand,
                generic_name=generic,
                dosage_form=form,
                strength=strength,
                manufacturer=manufacturer,
                route=route,
                barcode=barcode,
                mrp_unit=mrp_dec,
                cost_unit=cost_dec,
                curation_status=MedicineCurationStatus.APPROVED,
                curated_by=actor,
            )
            created_ids.append(new_product.product_id)
            if barcode:
                existing_products_by_barcode[barcode] = new_product

        session.created_record_ids = created_ids
        session.updated_record_ids = updated_ids
        session.status = ImportSessionStatus.COMMITTED
        session.committed_at = timezone.now()
        session.save()

        AuditLogEntry.objects.create(
            action="MEDICINE_IMPORT_COMMITTED",
            resource_type="MEDICINE_IMPORT",
            resource_id=session.session_id,
            tenant_id=session.tenant_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "created_count": len(created_ids),
                "updated_count": len(updated_ids),
                "total_rows": session.total_rows,
            },
        )

        return cls.serialize_session(session)

    @classmethod
    @transaction.atomic
    def rollback_import(
        cls,
        session: MedicineImportSession,
        actor: PharmacyUser,
    ) -> Dict[str, Any]:
        """
        Reverses an imported batch, deleting newly created products and recording audit trail.
        """
        if session.status == ImportSessionStatus.ROLLED_BACK:
            raise ValidationError(
                {"status": "ERR_IMPORT_ALREADY_ROLLED_BACK: This import session has already been rolled back."}
            )
        if session.status != ImportSessionStatus.COMMITTED:
            raise ValidationError(
                {"status": f"Cannot rollback an import session that is in '{session.status}' state."}
            )

        created_ids = session.created_record_ids or []
        deleted_count = 0
        if created_ids:
            deleted_count, _ = MedicineProduct.objects.filter(product_id__in=created_ids).delete()

        session.status = ImportSessionStatus.ROLLED_BACK
        session.rolled_back_at = timezone.now()
        session.save()

        AuditLogEntry.objects.create(
            action="MEDICINE_IMPORT_ROLLED_BACK",
            resource_type="MEDICINE_IMPORT",
            resource_id=session.session_id,
            tenant_id=session.tenant_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "deleted_products_count": deleted_count,
            },
        )

        return {
            "session_id": session.session_id,
            "status": session.status,
            "deleted_products_count": deleted_count,
            "rolled_back_at": session.rolled_back_at.isoformat(),
        }
