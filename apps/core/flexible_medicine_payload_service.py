"""
backend/apps/core/flexible_medicine_payload_service.py
Architecture Decision: ADR-0159 (P159).
Typed, Versioned Dynamic Medicine Payloads with Strict Field Incompatibility and Unknown Field Rejection.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    MedicineProduct,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.flexible_payload_service")


class FlexibleMedicinePayloadService:
    """
    Coordinates typed, versioned dynamic medicine API payloads.
    Strictly validates permitted fields, detects and rejects incompatible dosage-form parameters,
    and disallows arbitrary unknown attributes with field-level error codes.
    """

    SUPPORTED_SCHEMA_VERSIONS: Set[str] = {"2026.01", "v2"}

    SUPPORTED_DOSAGE_FORMS: Set[str] = {
        "TABLET_CAPSULE",
        "INHALER",
        "DROPS",
        "TOPICAL_CREAM",
        "SACHET_POWDER",
        "SUPPOSITORY",
        "DEVICE_CONSUMABLE",
        "SYRUP_SUSPENSION",
        "INJECTION_INFUSION",
    }

    COMMON_ATTRIBUTES: Set[str] = {
        "brand_name",
        "brand_name_bn",
        "generic_name",
        "generic_name_bn",
        "manufacturer",
        "dar_number",
        "cost_unit",
        "mrp_unit",
        "vat_percentage",
        "is_prescription_only",
        "is_antibiotic",
        "is_narcotic",
        "requires_cold_chain",
        "storage_condition",
        "storage_temperature_celsius_min",
        "storage_temperature_celsius_max",
    }

    MANDATORY_COMMON_ATTRIBUTES: Set[str] = {
        "brand_name",
        "generic_name",
        "dar_number",
        "cost_unit",
        "mrp_unit",
    }

    FORM_SPECIFIC_ATTRIBUTES: Dict[str, Set[str]] = {
        "TABLET_CAPSULE": {
            "pieces_per_strip",
            "strips_per_pack",
            "total_pieces_per_pack",
            "capsule_shell_type",
            "is_delayed_release",
        },
        "INHALER": {
            "dose_per_actuation",
            "actuations_per_device",
            "canister_type",
        },
        "DROPS": {
            "volume_ml",
            "drop_category",
            "is_preservative_free",
        },
        "TOPICAL_CREAM": {
            "tube_size_grams",
            "application_site",
            "vehicle_base",
        },
        "SACHET_POWDER": {
            "grams_per_sachet",
            "sachets_per_pack",
            "reconstitution_guidance",
        },
        "SUPPOSITORY": {
            "piece_count",
            "suppository_base_type",
            "administration_route",
        },
        "DEVICE_CONSUMABLE": {
            "device_type",
            "is_sterile",
            "gauge_size",
        },
        "SYRUP_SUSPENSION": {
            "bottle_size_ml",
            "is_dry_powder",
            "reconstituted_expiry_days",
        },
        "INJECTION_INFUSION": {
            "vial_volume_ml",
            "is_lyophilized",
            "diluent_required",
        },
    }

    MANDATORY_FORM_ATTRIBUTES: Dict[str, Set[str]] = {
        "INHALER": {"dose_per_actuation", "actuations_per_device"},
        "DROPS": {"volume_ml", "drop_category"},
        "TOPICAL_CREAM": {"tube_size_grams", "application_site"},
        "SACHET_POWDER": {"grams_per_sachet", "sachets_per_pack"},
        "SUPPOSITORY": {"piece_count", "administration_route"},
        "DEVICE_CONSUMABLE": {"device_type"},
    }

    @classmethod
    def _get_all_recognized_form_attributes(cls) -> Set[str]:
        all_attrs = set()
        for s in cls.FORM_SPECIFIC_ATTRIBUTES.values():
            all_attrs.update(s)
        return all_attrs

    @classmethod
    def _resolve_tenant(cls, user: PharmacyUser) -> Optional[Tenant]:
        tenant_id = getattr(user, "tenant_id", None)
        if tenant_id:
            return Tenant.objects.filter(tenant_id=tenant_id).first()
        return None

    @classmethod
    def check_permissions(cls, user: PharmacyUser, required_action: str = "read") -> None:
        """Enforces multi-tenant RBAC policies for flexible medicine payloads."""
        if user.is_superuser:
            return

        if required_action in ["write", "submit"]:
            if getattr(user, "role", None) in [UserRole.CASHIER]:
                raise PermissionDenied({
                    "error_code": "ERR_ROLE_UNAUTHORIZED",
                    "detail": "Cashier role has read-only access to master catalog payloads.",
                })

        tenant: Optional[Tenant] = cls._resolve_tenant(user)
        if tenant:
            if not tenant.is_active or tenant.status in [
                TenantLifecycleStatus.SUSPENDED,
                TenantLifecycleStatus.ARCHIVED,
            ]:
                raise PermissionDenied({
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": f"Tenant '{tenant.name}' subscription is suspended or terminated.",
                })

    @classmethod
    def validate_flexible_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates typed, versioned dynamic payload.
        Returns validation report with field-level error codes.
        """
        errors: List[Dict[str, Any]] = []

        # 1. Version check
        schema_version = payload.get("schema_version")
        if not schema_version or str(schema_version).strip() not in cls.SUPPORTED_SCHEMA_VERSIONS:
            errors.append({
                "field": "schema_version",
                "error_code": "ERR_PAYLOAD_VERSION_UNSUPPORTED",
                "message_en": f"Schema version '{schema_version}' is unsupported. Supported versions: {sorted(list(cls.SUPPORTED_SCHEMA_VERSIONS))}.",
                "message_bn": f"স্কিমা ভার্সন '{schema_version}' সমর্থিত নয়।",
            })
            return {
                "is_valid": False,
                "error_count": len(errors),
                "errors": errors,
                "validated_data": None,
            }

        # 2. Dosage form check
        dosage_form = payload.get("dosage_form_family")
        if not dosage_form or str(dosage_form).upper() not in cls.SUPPORTED_DOSAGE_FORMS:
            errors.append({
                "field": "dosage_form_family",
                "error_code": "ERR_INVALID_DOSAGE_FORM",
                "message_en": f"Dosage form family '{dosage_form}' is invalid or unsupported.",
                "message_bn": f"ডোজ ফর্ম '{dosage_form}' সঠিক নয়।",
            })
            return {
                "is_valid": False,
                "error_count": len(errors),
                "errors": errors,
                "validated_data": None,
            }

        dosage_form = str(dosage_form).upper()
        attributes = payload.get("attributes")
        if not isinstance(attributes, dict):
            errors.append({
                "field": "attributes",
                "error_code": "ERR_INVALID_TYPE",
                "message_en": "Payload attributes must be a non-null object.",
                "message_bn": "পেলোড অ্যাট্রিবিউট অবজেক্ট হওয়া আবশ্যক।",
            })
            return {
                "is_valid": False,
                "error_count": len(errors),
                "errors": errors,
                "validated_data": None,
            }

        allowed_form_attrs = cls.FORM_SPECIFIC_ATTRIBUTES.get(dosage_form, set())
        all_recognized_form_attrs = cls._get_all_recognized_form_attributes()

        # 3. Check for Unknown and Incompatible fields
        for key in attributes.keys():
            if key in cls.COMMON_ATTRIBUTES:
                continue
            elif key in allowed_form_attrs:
                continue
            elif key in all_recognized_form_attrs:
                # Key belongs to another form family!
                errors.append({
                    "field": key,
                    "error_code": "ERR_INCOMPATIBLE_FIELD",
                    "message_en": f"Field '{key}' is incompatible with dosage form family '{dosage_form}'.",
                    "message_bn": f"'{key}' ফিল্ডটি '{dosage_form}' ডোজ ফর্মের সাথে অসঙ্গতিপূর্ণ।",
                })
            else:
                # Key is completely unknown!
                errors.append({
                    "field": key,
                    "error_code": "ERR_UNKNOWN_FIELD",
                    "message_en": f"Unknown field '{key}' is not permitted in medicine payload schema.",
                    "message_bn": f"অজানা ফিল্ড '{key}' অনুমোদিত নয়।",
                })

        # 4. Mandatory Common Attributes
        for m_field in cls.MANDATORY_COMMON_ATTRIBUTES:
            val = attributes.get(m_field)
            if val is None or str(val).strip() == "":
                errors.append({
                    "field": m_field,
                    "error_code": "ERR_MANDATORY_FIELD_MISSING",
                    "message_en": f"Mandatory attribute '{m_field}' is missing.",
                    "message_bn": f"বাধ্যতামূলক ফিল্ড '{m_field}' অনুপস্থিত।",
                })

        # 5. Mandatory Form-Specific Attributes
        required_form_attrs = cls.MANDATORY_FORM_ATTRIBUTES.get(dosage_form, set())
        for req_field in required_form_attrs:
            val = attributes.get(req_field)
            if val is None or str(val).strip() == "":
                errors.append({
                    "field": req_field,
                    "error_code": "ERR_MANDATORY_FIELD_MISSING",
                    "message_en": f"Attribute '{req_field}' is mandatory for '{dosage_form}'.",
                    "message_bn": f"'{dosage_form}' এর জন্য '{req_field}' ফিল্ডটি আবশ্যক।",
                })

        # 6. Pricing Rules
        cost_val = attributes.get("cost_unit")
        mrp_val = attributes.get("mrp_unit")

        cost_dec: Optional[Decimal] = None
        mrp_dec: Optional[Decimal] = None

        if cost_val is not None and str(cost_val).strip() != "":
            try:
                cost_dec = Decimal(str(cost_val))
                if cost_dec < 0:
                    errors.append({
                        "field": "cost_unit",
                        "error_code": "ERR_NEGATIVE_COST",
                        "message_en": "Unit cost price cannot be negative.",
                        "message_bn": "ক্রয়মূল্য ঋণাত্মক হতে পারে না।",
                    })
            except (InvalidOperation, ValueError):
                errors.append({
                    "field": "cost_unit",
                    "error_code": "ERR_INVALID_TYPE",
                    "message_en": "Cost price must be a valid decimal number.",
                    "message_bn": "ক্রয়মূল্য সঠিক সংখ্যা হতে হবে।",
                })

        if mrp_val is not None and str(mrp_val).strip() != "":
            try:
                mrp_dec = Decimal(str(mrp_val))
                if mrp_dec <= 0:
                    errors.append({
                        "field": "mrp_unit",
                        "error_code": "ERR_INVALID_MRP",
                        "message_en": "MRP must be strictly positive.",
                        "message_bn": "খুচরা মূল্য অবশ্যই শূন্যের বেশি হতে হবে।",
                    })
                elif cost_dec is not None and mrp_dec < cost_dec:
                    errors.append({
                        "field": "mrp_unit",
                        "error_code": "ERR_MRP_BELOW_COST",
                        "message_en": f"MRP ({mrp_dec}) cannot be less than purchase cost ({cost_dec}).",
                        "message_bn": f"খুচরা মূল্য ({mrp_dec}) ক্রয়মূল্যের ({cost_dec}) চেয়ে কম হতে পারে না।",
                    })
            except (InvalidOperation, ValueError):
                errors.append({
                    "field": "mrp_unit",
                    "error_code": "ERR_INVALID_TYPE",
                    "message_en": "MRP must be a valid decimal number.",
                    "message_bn": "খুচরা মূল্য সঠিক সংখ্যা হতে হবে।",
                })

        # 7. Packaging Multiplier Check
        pieces = attributes.get("pieces_per_strip")
        strips = attributes.get("strips_per_pack")
        total = attributes.get("total_pieces_per_pack")
        if pieces is not None and strips is not None and total is not None:
            try:
                p_i = int(pieces)
                s_i = int(strips)
                t_i = int(total)
                if p_i > 0 and s_i > 0 and t_i != (p_i * s_i):
                    errors.append({
                        "field": "total_pieces_per_pack",
                        "error_code": "ERR_DIMENSION_MISMATCH",
                        "message_en": f"Total pieces ({t_i}) does not equal pieces ({p_i}) * strips ({s_i}) = {p_i * s_i}.",
                        "message_bn": "প্যাকে মোট পিস ও স্ট্রিপের হিসাবের গরমিল।",
                    })
            except (ValueError, TypeError):
                errors.append({
                    "field": "total_pieces_per_pack",
                    "error_code": "ERR_INVALID_TYPE",
                    "message_en": "Packaging dimensions must be valid integers.",
                    "message_bn": "প্যাকেজিং সংখ্যা অবশ্যই পূর্ণসংখ্যা হতে হবে।",
                })

        # 8. Temperature Invariants
        min_t = attributes.get("storage_temperature_celsius_min")
        max_t = attributes.get("storage_temperature_celsius_max")
        if min_t is not None and max_t is not None:
            try:
                min_dec = Decimal(str(min_t))
                max_dec = Decimal(str(max_t))
                if min_dec > max_dec:
                    errors.append({
                        "field": "storage_temperature_celsius_min",
                        "error_code": "ERR_INVALID_TEMPERATURE_RANGE",
                        "message_en": f"Minimum storage temperature ({min_dec}°C) cannot exceed maximum ({max_dec}°C).",
                        "message_bn": "সর্বনিম্ন তাপমাত্রা সর্বোচ্চ তাপমাত্রার চেয়ে বেশি হতে পারে না।",
                    })
            except Exception:
                pass

        is_valid = len(errors) == 0
        validated_data = payload if is_valid else None

        return {
            "schema_version": schema_version,
            "dosage_form_family": dosage_form,
            "is_valid": is_valid,
            "error_count": len(errors),
            "errors": errors,
            "validated_data": validated_data,
        }

    @classmethod
    @transaction.atomic
    def submit_flexible_payload(
        cls,
        payload: Dict[str, Any],
        user: PharmacyUser,
    ) -> Dict[str, Any]:
        """
        Validates and persists medicine monograph into master catalog.
        Requires authorized role (Superadmin, Owner, Manager, Staff Pharmacist).
        """
        cls.check_permissions(user, required_action="submit")
        tenant: Optional[Tenant] = cls._resolve_tenant(user)

        validation = cls.validate_flexible_payload(payload)
        if not validation["is_valid"]:
            raise ValidationError({
                "error_code": "ERR_VALIDATION_FAILED",
                "detail": "Payload validation failed.",
                "errors": validation["errors"],
            })

        attrs = payload["attributes"]
        dosage_form = payload["dosage_form_family"]

        product_id = f"med-{uuid.uuid4().hex[:12]}"

        # Separate common attributes for MedicineProduct model
        brand_name = attrs.get("brand_name", "")
        generic_name = attrs.get("generic_name", "")
        dar_number = attrs.get("dar_number", "")
        cost_unit = Decimal(str(attrs.get("cost_unit", "0.00")))
        mrp_unit = Decimal(str(attrs.get("mrp_unit", "0.00")))

        # Formulation specific attributes stored in source_metadata
        form_specifics = {k: v for k, v in attrs.items() if k not in cls.COMMON_ATTRIBUTES}

        product = MedicineProduct.objects.create(
            product_id=product_id,
            brand_name=brand_name,
            brand_name_bn=attrs.get("brand_name_bn", ""),
            generic_name=generic_name,
            generic_name_bn=attrs.get("generic_name_bn", ""),
            dosage_form=dosage_form,
            manufacturer=attrs.get("manufacturer", ""),
            dgda_dar_number=dar_number,
            cost_unit=cost_unit,
            mrp_unit=mrp_unit,
            is_prescription_only=attrs.get("is_prescription_only", False),
            is_antibiotic=attrs.get("is_antibiotic", False),
            is_narcotic=attrs.get("is_narcotic", False),
            requires_cold_chain=attrs.get("requires_cold_chain", False),
            source_metadata={
                "schema_version": payload["schema_version"],
                "form_specific_parameters": form_specifics,
                "created_by_tenant": getattr(tenant, "tenant_id", "GLOBAL"),
            },
        )

        AuditLogEntry.objects.create(
            entry_id=uuid.uuid4(),
            tenant_id=getattr(tenant, "tenant_id", "GLOBAL"),
            user_id=str(user.pk),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="SUBMIT_FLEXIBLE_MEDICINE_PAYLOAD",
            resource_type="MedicineProduct",
            resource_id=product.product_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "brand_name": brand_name,
                "dosage_form_family": dosage_form,
                "schema_version": payload["schema_version"],
            },
        )

        return {
            "product_id": product.product_id,
            "brand_name": product.brand_name,
            "generic_name": product.generic_name,
            "dosage_form": product.dosage_form,
            "dgda_dar_number": product.dgda_dar_number,
            "cost_unit": str(product.cost_unit),
            "mrp_unit": str(product.mrp_unit),
            "schema_version": payload["schema_version"],
            "status": "CREATED",
        }

    @classmethod
    def get_dosage_form_specification(cls, dosage_form_family: str) -> Dict[str, Any]:
        """Returns introspectable field specifications for a dosage form family."""
        form = dosage_form_family.upper()
        if form not in cls.SUPPORTED_DOSAGE_FORMS:
            raise ValidationError({
                "error_code": "ERR_INVALID_DOSAGE_FORM",
                "detail": f"Dosage form '{dosage_form_family}' is not supported.",
            })

        allowed_specific = cls.FORM_SPECIFIC_ATTRIBUTES.get(form, set())
        mandatory_specific = cls.MANDATORY_FORM_ATTRIBUTES.get(form, set())
        all_recognized = cls._get_all_recognized_form_attributes()
        incompatible = all_recognized - allowed_specific

        return {
            "dosage_form_family": form,
            "common_attributes": sorted(list(cls.COMMON_ATTRIBUTES)),
            "mandatory_common_attributes": sorted(list(cls.MANDATORY_COMMON_ATTRIBUTES)),
            "allowed_form_specific_attributes": sorted(list(allowed_specific)),
            "mandatory_form_specific_attributes": sorted(list(mandatory_specific)),
            "incompatible_form_specific_attributes": sorted(list(incompatible)),
        }

