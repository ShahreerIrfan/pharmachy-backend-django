"""
backend/apps/core/dynamic_field_group_service.py
Architecture Decision: ADR-0152 (P152).
Dynamic Field Group Layout, Accessible Keyboard Flow & Inline Error Service.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    DynamicFieldGroupLayout,
    FieldGroupCode,
    LayoutDisplayDensity,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.dynamic_field_group_service")


class DynamicFieldGroupService:
    """
    Coordinates dynamic field group layouts, tab sequence ordering,
    keyboard accelerator mnemonics, accessible bilingual labels, and inline error feedback.
    """

    ALLOWED_GROUP_CODES = [c.value for c in FieldGroupCode]
    ALLOWED_DENSITIES = [c.value for c in LayoutDisplayDensity]

    @classmethod
    def _resolve_tenant(cls, user: PharmacyUser) -> Optional[Tenant]:
        tenant_id = getattr(user, "tenant_id", None)
        if tenant_id:
            return Tenant.objects.filter(tenant_id=tenant_id).first()
        return None

    @classmethod
    def check_permissions(cls, user: PharmacyUser, required_action: str = "read") -> None:
        """Enforces multi-tenant RBAC policies for dynamic field group configurations."""
        if user.is_superuser:
            return

        if required_action in ["write", "delete"]:
            if getattr(user, "role", None) in [UserRole.CASHIER]:
                raise PermissionDenied({
                    "error_code": "ERR_ROLE_UNAUTHORIZED",
                    "detail": "Cashier role has read-only access to layout configuration.",
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
    def get_canonical_groups(cls, dosage_form_family: str = "ALL") -> List[Dict[str, Any]]:
        """Returns platform canonical field groups with keyboard mnemonics and accessible metadata."""
        form_upper = dosage_form_family.upper()

        groups: List[Dict[str, Any]] = [
            {
                "group_code": FieldGroupCode.GENERAL_IDENTIFICATION.value,
                "display_order": 1,
                "title_en": "General Product Identification",
                "title_bn": "সাধারণ পণ্যের পরিচিতি",
                "is_collapsible": False,
                "default_collapsed": False,
                "keyboard_shortcut_mnemonic": "G",
                "display_density": LayoutDisplayDensity.COMFORTABLE.value,
                "visibility_condition": {},
                "fields": [
                    {
                        "field_name": "brand_name",
                        "tab_index": 1,
                        "label_en": "Brand Name",
                        "label_bn": "ব্র্যান্ডের নাম",
                        "control_type": "TEXT_INPUT",
                        "is_mandatory": True,
                        "desktop_grid_width": "HALF",
                        "keyboard_mnemonic": "B",
                        "aria_label": "Enter commercial medicine brand name",
                    },
                    {
                        "field_name": "generic_name",
                        "tab_index": 2,
                        "label_en": "Generic / Chemical Name",
                        "label_bn": "জেনেরিক বা রাসায়নিক নাম",
                        "control_type": "TEXT_INPUT",
                        "is_mandatory": True,
                        "desktop_grid_width": "HALF",
                        "keyboard_mnemonic": "N",
                        "aria_label": "Enter generic chemical compound name",
                    },
                    {
                        "field_name": "dosage_form",
                        "tab_index": 3,
                        "label_en": "Dosage Form / Item Type",
                        "label_bn": "ডোজ ফর্ম বা আইটেম টাইপ",
                        "control_type": "SELECT_DROPDOWN",
                        "is_mandatory": True,
                        "desktop_grid_width": "HALF",
                        "keyboard_mnemonic": "D",
                        "aria_label": "Select pharmaceutical dosage form",
                    },
                    {
                        "field_name": "manufacturer",
                        "tab_index": 4,
                        "label_en": "Manufacturer / Supplier",
                        "label_bn": "প্রস্তুতকারক বা কোম্পানি",
                        "control_type": "TEXT_INPUT",
                        "is_mandatory": False,
                        "desktop_grid_width": "HALF",
                        "keyboard_mnemonic": "M",
                        "aria_label": "Enter manufacturing pharmaceutical company",
                    },
                ],
            },
            {
                "group_code": FieldGroupCode.FORM_SPECIFIC_PARAMETERS.value,
                "display_order": 2,
                "title_en": "Formulation & Clinical Parameters",
                "title_bn": "ডোজ ফর্ম সংক্রান্ত বিশেষ পরিমাপ",
                "is_collapsible": True,
                "default_collapsed": False,
                "keyboard_shortcut_mnemonic": "F",
                "display_density": LayoutDisplayDensity.COMFORTABLE.value,
                "visibility_condition": {
                    "field": "dosage_form",
                    "operator": "IN",
                    "value": [
                        "INHALER",
                        "DROPS",
                        "CREAM_OINTMENT",
                        "SACHET",
                        "SUPPOSITORY",
                        "NEEDLE_OR_SYRINGE",
                        "DIAGNOSTIC_TEST_STRIP",
                    ],
                },
                "fields": cls._get_form_specific_fields(form_upper),
            },
            {
                "group_code": FieldGroupCode.PACKAGING_DIMENSIONS.value,
                "display_order": 3,
                "title_en": "Packaging & Unit Multipliers",
                "title_bn": "প্যাকেজিং ও স্ট্রিপ হিসাব",
                "is_collapsible": True,
                "default_collapsed": False,
                "keyboard_shortcut_mnemonic": "P",
                "display_density": LayoutDisplayDensity.COMFORTABLE.value,
                "visibility_condition": {},
                "fields": [
                    {
                        "field_name": "pieces_per_strip",
                        "tab_index": 10,
                        "label_en": "Pieces per Strip",
                        "label_bn": "স্ট্রিপে পিস সংখ্যা",
                        "control_type": "NUMERIC_INPUT",
                        "is_mandatory": False,
                        "desktop_grid_width": "THIRD",
                        "keyboard_mnemonic": "1",
                        "aria_label": "Number of units per individual strip",
                    },
                    {
                        "field_name": "strips_per_pack",
                        "tab_index": 11,
                        "label_en": "Strips per Pack / Box",
                        "label_bn": "প্যাকে স্ট্রিপ সংখ্যা",
                        "control_type": "NUMERIC_INPUT",
                        "is_mandatory": False,
                        "desktop_grid_width": "THIRD",
                        "keyboard_mnemonic": "2",
                        "aria_label": "Number of strips per outer carton box",
                    },
                    {
                        "field_name": "total_pieces_per_pack",
                        "tab_index": 12,
                        "label_en": "Total Pieces per Pack",
                        "label_bn": "প্যাক প্রতি মোট পিস",
                        "control_type": "NUMERIC_INPUT",
                        "is_mandatory": False,
                        "desktop_grid_width": "THIRD",
                        "keyboard_mnemonic": "3",
                        "aria_label": "Calculated total pieces per box",
                    },
                ],
            },
            {
                "group_code": FieldGroupCode.STORAGE_AND_COLD_CHAIN.value,
                "display_order": 4,
                "title_en": "Storage & Cold-Chain Controls",
                "title_bn": "সংরক্ষণ ও তাপমাত্রা নিয়ন্ত্রণ",
                "is_collapsible": True,
                "default_collapsed": False,
                "keyboard_shortcut_mnemonic": "S",
                "display_density": LayoutDisplayDensity.COMFORTABLE.value,
                "visibility_condition": {},
                "fields": [
                    {
                        "field_name": "storage_condition",
                        "tab_index": 20,
                        "label_en": "Storage Policy",
                        "label_bn": "সংরক্ষণের শর্ত",
                        "control_type": "SELECT_DROPDOWN",
                        "is_mandatory": True,
                        "desktop_grid_width": "HALF",
                        "keyboard_mnemonic": "O",
                        "aria_label": "Select thermal storage requirement",
                    },
                    {
                        "field_name": "requires_refrigeration",
                        "tab_index": 21,
                        "label_en": "Requires Cold Storage (2-8°C)",
                        "label_bn": "ফ্রিজ বা কোল্ড-চেইন আবশ্যক",
                        "control_type": "CHECKBOX_SWITCH",
                        "is_mandatory": False,
                        "desktop_grid_width": "HALF",
                        "keyboard_mnemonic": "C",
                        "aria_label": "Toggle cold-chain refrigeration requirement",
                    },
                    {
                        "field_name": "storage_temperature_celsius_min",
                        "tab_index": 22,
                        "label_en": "Min Temp (°C)",
                        "label_bn": "সর্বনিম্ন তাপমাত্রা (°সে)",
                        "control_type": "NUMERIC_INPUT",
                        "is_mandatory": False,
                        "desktop_grid_width": "QUARTER",
                        "keyboard_mnemonic": "L",
                        "aria_label": "Minimum permitted storage temperature",
                    },
                    {
                        "field_name": "storage_temperature_celsius_max",
                        "tab_index": 23,
                        "label_en": "Max Temp (°C)",
                        "label_bn": "সর্বোচ্চ তাপমাত্রা (°সে)",
                        "control_type": "NUMERIC_INPUT",
                        "is_mandatory": False,
                        "desktop_grid_width": "QUARTER",
                        "keyboard_mnemonic": "H",
                        "aria_label": "Maximum permitted storage temperature",
                    },
                ],
            },
            {
                "group_code": FieldGroupCode.REGULATORY_AND_DISPENSING.value,
                "display_order": 5,
                "title_en": "Regulatory & Prescription Schedule",
                "title_bn": "নিয়ন্ত্রক ও প্রেসক্রিপশন শিডিউল",
                "is_collapsible": True,
                "default_collapsed": True,
                "keyboard_shortcut_mnemonic": "R",
                "display_density": LayoutDisplayDensity.COMFORTABLE.value,
                "visibility_condition": {},
                "fields": [
                    {
                        "field_name": "prescription_required",
                        "tab_index": 30,
                        "label_en": "Prescription Only (POM)",
                        "label_bn": "প্রেসক্রিপশন আবশ্যক",
                        "control_type": "CHECKBOX_SWITCH",
                        "is_mandatory": False,
                        "desktop_grid_width": "HALF",
                        "keyboard_mnemonic": "X",
                        "aria_label": "Toggle prescription requirement",
                    },
                    {
                        "field_name": "is_antibiotic",
                        "tab_index": 31,
                        "label_en": "Antibiotic Schedule Compliance",
                        "label_bn": "অ্যান্টিবায়োটিক শিডিউল নজরদারি",
                        "control_type": "CHECKBOX_SWITCH",
                        "is_mandatory": False,
                        "desktop_grid_width": "HALF",
                        "keyboard_mnemonic": "A",
                        "aria_label": "Toggle antibiotic regulatory reporting",
                    },
                ],
            },
            {
                "group_code": FieldGroupCode.FINANCIAL_PRICING.value,
                "display_order": 6,
                "title_en": "Financial Pricing & MRP",
                "title_bn": "মূল্য ও কর নির্ধারণ",
                "is_collapsible": True,
                "default_collapsed": False,
                "keyboard_shortcut_mnemonic": "M",
                "display_density": LayoutDisplayDensity.COMFORTABLE.value,
                "visibility_condition": {},
                "fields": [
                    {
                        "field_name": "cost_unit",
                        "tab_index": 40,
                        "label_en": "Unit Cost Price (BDT)",
                        "label_bn": "ক্রয়মূল্য (টাকা)",
                        "control_type": "NUMERIC_INPUT",
                        "is_mandatory": True,
                        "desktop_grid_width": "THIRD",
                        "keyboard_mnemonic": "K",
                        "aria_label": "Enter acquisition purchase cost",
                    },
                    {
                        "field_name": "mrp_unit",
                        "tab_index": 41,
                        "label_en": "Maximum Retail Price (MRP)",
                        "label_bn": "খুচরা বিক্রয়মূল্য (এমআরপি)",
                        "control_type": "NUMERIC_INPUT",
                        "is_mandatory": True,
                        "desktop_grid_width": "THIRD",
                        "keyboard_mnemonic": "U",
                        "aria_label": "Enter maximum retail price",
                    },
                    {
                        "field_name": "vat_percentage",
                        "tab_index": 42,
                        "label_en": "VAT (%)",
                        "label_bn": "ভ্যাট হার (%)",
                        "control_type": "NUMERIC_INPUT",
                        "is_mandatory": False,
                        "desktop_grid_width": "THIRD",
                        "keyboard_mnemonic": "V",
                        "aria_label": "Value added tax percentage",
                    },
                ],
            },
        ]

        return groups

    @classmethod
    def _get_form_specific_fields(cls, dosage_form: str) -> List[Dict[str, Any]]:
        """Returns specialized formulation fields depending on dosage form."""
        if dosage_form == "INHALER":
            return [
                {
                    "field_name": "dose_per_actuation",
                    "tab_index": 5,
                    "label_en": "Dose per Actuation",
                    "label_bn": "প্রতি পাফে মাত্রা",
                    "control_type": "TEXT_INPUT",
                    "is_mandatory": True,
                    "desktop_grid_width": "HALF",
                    "keyboard_mnemonic": "E",
                    "aria_label": "Inhaler dose per actuation",
                },
                {
                    "field_name": "actuations_per_device",
                    "tab_index": 6,
                    "label_en": "Actuations per Inhaler",
                    "label_bn": "মোট পাফ সংখ্যা",
                    "control_type": "NUMERIC_INPUT",
                    "is_mandatory": True,
                    "desktop_grid_width": "HALF",
                    "keyboard_mnemonic": "T",
                    "aria_label": "Total actuation count per canister",
                },
            ]
        elif dosage_form in ["NEEDLE_OR_SYRINGE", "NEEDLE"]:
            return [
                {
                    "field_name": "needle_gauge",
                    "tab_index": 5,
                    "label_en": "Needle Gauge",
                    "label_bn": "সূঁচের গজ",
                    "control_type": "SELECT_DROPDOWN",
                    "is_mandatory": True,
                    "desktop_grid_width": "HALF",
                    "keyboard_mnemonic": "G",
                    "aria_label": "Needle thickness gauge",
                },
                {
                    "field_name": "needle_length_mm",
                    "tab_index": 6,
                    "label_en": "Needle Length (mm)",
                    "label_bn": "সূঁচের দৈর্ঘ্য (মিমি)",
                    "control_type": "NUMERIC_INPUT",
                    "is_mandatory": True,
                    "desktop_grid_width": "HALF",
                    "keyboard_mnemonic": "L",
                    "aria_label": "Needle length in millimeters",
                },
            ]
        elif dosage_form in ["DIAGNOSTIC_TEST_STRIP", "STRIP"]:
            return [
                {
                    "field_name": "meter_compatibility",
                    "tab_index": 5,
                    "label_en": "Meter Compatibility",
                    "label_bn": "প্রযোজ্য মিটারের নাম",
                    "control_type": "TEXT_INPUT",
                    "is_mandatory": True,
                    "desktop_grid_width": "HALF",
                    "keyboard_mnemonic": "C",
                    "aria_label": "Target glucometer compatibility",
                },
                {
                    "field_name": "strip_count_per_pack",
                    "tab_index": 6,
                    "label_en": "Strips per Pack",
                    "label_bn": "প্যাকে স্ট্রিপ সংখ্যা",
                    "control_type": "NUMERIC_INPUT",
                    "is_mandatory": True,
                    "desktop_grid_width": "HALF",
                    "keyboard_mnemonic": "N",
                    "aria_label": "Diagnostic strips count per vial",
                },
            ]
        elif dosage_form == "SUPPOSITORY":
            return [
                {
                    "field_name": "suppository_base_type",
                    "tab_index": 5,
                    "label_en": "Suppository Vehicle Base",
                    "label_bn": "সাপোজিটরি বেস",
                    "control_type": "SELECT_DROPDOWN",
                    "is_mandatory": True,
                    "desktop_grid_width": "HALF",
                    "keyboard_mnemonic": "V",
                    "aria_label": "Suppository base type",
                },
                {
                    "field_name": "administration_route",
                    "tab_index": 6,
                    "label_en": "Administration Route",
                    "label_bn": "প্রয়োগ রুট",
                    "control_type": "SELECT_DROPDOWN",
                    "is_mandatory": True,
                    "desktop_grid_width": "HALF",
                    "keyboard_mnemonic": "R",
                    "aria_label": "Anatomical administration route",
                },
            ]
        # Default placeholder fields for generic formulation
        return [
            {
                "field_name": "active_ingredient_concentration",
                "tab_index": 5,
                "label_en": "Active Strength / Concentration",
                "label_bn": "কার্যকর উপাদান মাত্রা",
                "control_type": "TEXT_INPUT",
                "is_mandatory": False,
                "desktop_grid_width": "FULL",
                "keyboard_mnemonic": "S",
                "aria_label": "Active ingredient strength concentration",
            }
        ]

    @classmethod
    def evaluate_group_visibility(cls, visibility_condition: Dict[str, Any], form_values: Dict[str, Any]) -> bool:
        """
        Evaluates whether a field group is visible according to conditional rules.
        Empty visibility condition implies unconditional visibility.
        """
        if not visibility_condition or not isinstance(visibility_condition, dict):
            return True

        field = visibility_condition.get("field")
        operator = visibility_condition.get("operator", "EQUALS").upper()
        expected = visibility_condition.get("value")

        if not field:
            return True

        val = form_values.get(field)

        if operator == "EQUALS":
            if isinstance(expected, bool):
                return bool(val) == expected
            return str(val or "").upper() == str(expected or "").upper()
        elif operator == "NOT_EQUALS":
            return str(val or "").upper() != str(expected or "").upper()
        elif operator == "IN":
            if isinstance(expected, list):
                allowed = [str(x).upper() for x in expected]
                return str(val or "").upper() in allowed
            return False
        elif operator == "NOT_IN":
            if isinstance(expected, list):
                disallowed = [str(x).upper() for x in expected]
                return str(val or "").upper() not in disallowed
            return True

        return True

    @classmethod
    def resolve_layout(
        cls,
        user: PharmacyUser,
        dosage_form_family: str = "ALL",
        form_values: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Resolves dynamic field groups combining system canonicals and tenant overrides.
        Calculates tab index traversal, shortcut mnemonics, and conditional group visibility.
        """
        cls.check_permissions(user, required_action="read")
        tenant: Optional[Tenant] = cls._resolve_tenant(user)
        values = form_values or {}

        canonical_groups = cls.get_canonical_groups(dosage_form_family=dosage_form_family)

        # Check tenant database overrides
        overrides = {}
        if tenant:
            db_layouts = DynamicFieldGroupLayout.objects.filter(
                Q(tenant=tenant) | Q(tenant__isnull=True),
                dosage_form_family__in=["ALL", dosage_form_family],
                is_active=True,
            )
            for l in db_layouts:
                overrides[l.group_code] = l

        resolved_groups = []
        for g in canonical_groups:
            code = g["group_code"]
            override: Optional[DynamicFieldGroupLayout] = overrides.get(code)
            if override:
                group_data = {
                    "layout_id": str(override.layout_id),
                    "group_code": override.group_code,
                    "display_order": override.display_order,
                    "title_en": override.title_en,
                    "title_bn": override.title_bn,
                    "is_collapsible": override.is_collapsible,
                    "default_collapsed": override.default_collapsed,
                    "keyboard_shortcut_mnemonic": override.keyboard_shortcut_mnemonic or g.get("keyboard_shortcut_mnemonic", ""),
                    "display_density": override.display_density,
                    "visibility_condition": override.visibility_condition or g.get("visibility_condition", {}),
                    "fields": override.fields_config if override.fields_config else g.get("fields", []),
                }
            else:
                group_data = dict(g)
                group_data["layout_id"] = f"canonical-{code.lower()}"

            # Calculate visibility
            is_visible = cls.evaluate_group_visibility(group_data.get("visibility_condition", {}), values)
            group_data["is_visible"] = is_visible
            resolved_groups.append(group_data)

        # Sort by display_order
        resolved_groups.sort(key=lambda x: x["display_order"])

        return {
            "dosage_form_family": dosage_form_family,
            "tenant_id": getattr(tenant, "tenant_id", None),
            "groups_count": len(resolved_groups),
            "visible_groups_count": sum(1 for g in resolved_groups if g["is_visible"]),
            "groups": resolved_groups,
        }

    @classmethod
    def validate_form_data_with_inline_errors(
        cls,
        dosage_form_family: str,
        form_values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Validates form inputs against field groups and aggregates inline errors.
        Provides field-level and group-level error mappings for responsive UI display.
        """
        canonical_groups = cls.get_canonical_groups(dosage_form_family=dosage_form_family)
        inline_errors: List[Dict[str, Any]] = []

        for group in canonical_groups:
            group_code = group["group_code"]
            is_visible = cls.evaluate_group_visibility(group.get("visibility_condition", {}), form_values)
            if not is_visible:
                continue

            for field in group.get("fields", []):
                fname = field["field_name"]
                is_mandatory = field.get("is_mandatory", False)
                val = form_values.get(fname)

                if is_mandatory and (val is None or str(val).strip() == ""):
                    inline_errors.append({
                        "group_code": group_code,
                        "field_name": fname,
                        "error_code": "ERR_MANDATORY_FIELD_MISSING",
                        "message_en": f"Field '{field['label_en']}' is mandatory.",
                        "message_bn": f"'{field['label_bn']}' ফিল্ডটি পূরণ করা আবশ্যক।",
                        "tab_index": field.get("tab_index", 1),
                    })

        # Dimension consistency check
        pieces = form_values.get("pieces_per_strip")
        strips = form_values.get("strips_per_pack")
        total = form_values.get("total_pieces_per_pack")
        if pieces is not None and strips is not None and total is not None:
            try:
                p_int = int(pieces)
                s_int = int(strips)
                t_int = int(total)
                if p_int > 0 and s_int > 0 and t_int != p_int * s_int:
                    inline_errors.append({
                        "group_code": FieldGroupCode.PACKAGING_DIMENSIONS.value,
                        "field_name": "total_pieces_per_pack",
                        "error_code": "ERR_DIMENSION_MISMATCH",
                        "message_en": f"Total pieces ({t_int}) must equal pieces per strip ({p_int}) * strips per pack ({s_int}) = {p_int * s_int}.",
                        "message_bn": f"প্যাকের মোট পিস ({t_int}) অবশ্যই স্ট্রিপ প্রতি পিস ({p_int}) ও প্যাকে স্ট্রিপের ({s_int}) গুণফল ({p_int * s_int}) হতে হবে।",
                        "tab_index": 12,
                    })
            except (ValueError, TypeError):
                pass

        # Temperature range check
        t_min = form_values.get("storage_temperature_celsius_min")
        t_max = form_values.get("storage_temperature_celsius_max")
        if t_min is not None and t_max is not None:
            try:
                min_dec = Decimal(str(t_min))
                max_dec = Decimal(str(t_max))
                if min_dec > max_dec:
                    inline_errors.append({
                        "group_code": FieldGroupCode.STORAGE_AND_COLD_CHAIN.value,
                        "field_name": "storage_temperature_celsius_min",
                        "error_code": "ERR_INVALID_TEMPERATURE_RANGE",
                        "message_en": f"Minimum temperature ({min_dec}°C) cannot exceed maximum temperature ({max_dec}°C).",
                        "message_bn": f"সর্বনিম্ন তাপমাত্রা ({min_dec}°সে) সর্বোচ্চ তাপমাত্রার ({max_dec}°সে) চেয়ে বেশি হতে পারে না।",
                        "tab_index": 22,
                    })
            except Exception:
                pass

        return {
            "is_valid": len(inline_errors) == 0,
            "error_count": len(inline_errors),
            "inline_errors": inline_errors,
        }

    @classmethod
    @transaction.atomic
    def create_or_update_layout(
        cls,
        payload: Dict[str, Any],
        user: PharmacyUser,
        target_layout_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Creates or updates a custom dynamic field group layout for tenant/branch."""
        cls.check_permissions(user, required_action="write")
        tenant: Optional[Tenant] = cls._resolve_tenant(user)

        layout: Optional[DynamicFieldGroupLayout] = None
        if target_layout_id:
            try:
                layout = DynamicFieldGroupLayout.objects.get(pk=target_layout_id)
            except DynamicFieldGroupLayout.DoesNotExist:
                raise ValidationError({
                    "error_code": "ERR_LAYOUT_NOT_FOUND",
                    "detail": f"DynamicFieldGroupLayout '{target_layout_id}' not found.",
                })
            user_tenant_id = getattr(user, "tenant_id", None)
            if layout.tenant_id and user_tenant_id and str(layout.tenant_id) != str(user_tenant_id) and not user.is_superuser:
                raise PermissionDenied({
                    "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                    "detail": "Cross-tenant modification prohibited.",
                })

        group_code = payload.get("group_code", getattr(layout, "group_code", None))
        if group_code not in cls.ALLOWED_GROUP_CODES:
            raise ValidationError({
                "error_code": "ERR_INVALID_GROUP_CODE",
                "detail": f"Invalid group_code: '{group_code}'. Allowed: {cls.ALLOWED_GROUP_CODES}",
            })

        title_en = str(payload.get("title_en", getattr(layout, "title_en", ""))).strip()
        title_bn = str(payload.get("title_bn", getattr(layout, "title_bn", ""))).strip()
        if len(title_en) < 2 or len(title_bn) < 2:
            raise ValidationError({
                "error_code": "ERR_INVALID_TITLE",
                "detail": "title_en and title_bn must be at least 2 characters.",
            })

        display_order = int(payload.get("display_order", getattr(layout, "display_order", 1)))
        if display_order < 1:
            raise ValidationError({
                "error_code": "ERR_INVALID_DISPLAY_ORDER",
                "detail": "display_order must be 1 or greater.",
            })

        created = False
        if not layout:
            dosage_family = payload.get("dosage_form_family", "ALL")
            q = Q(group_code=group_code, dosage_form_family=dosage_family)
            if tenant:
                q &= Q(tenant=tenant)
            else:
                q &= Q(tenant__isnull=True)
            layout = DynamicFieldGroupLayout.objects.filter(q).first()
            if not layout:
                layout = DynamicFieldGroupLayout(
                    tenant=tenant,
                    group_code=group_code,
                    dosage_form_family=dosage_family,
                )
                created = True

        layout.display_order = display_order
        layout.title_en = title_en
        layout.title_bn = title_bn
        if "is_collapsible" in payload:
            layout.is_collapsible = bool(payload["is_collapsible"])
        if "default_collapsed" in payload:
            layout.default_collapsed = bool(payload["default_collapsed"])
        if "keyboard_shortcut_mnemonic" in payload:
            layout.keyboard_shortcut_mnemonic = str(payload["keyboard_shortcut_mnemonic"]).strip()
        if "display_density" in payload:
            layout.display_density = payload["display_density"]
        if "fields" in payload:
            layout.fields_config = payload["fields"]
        if "visibility_condition" in payload:
            layout.visibility_condition = payload["visibility_condition"]
        if "is_active" in payload:
            layout.is_active = bool(payload["is_active"])

        layout.full_clean()
        layout.save()

        action = "LAYOUT_GROUP_CREATED" if created else "LAYOUT_GROUP_UPDATED"
        AuditLogEntry.objects.create(
            tenant_id=getattr(tenant, "tenant_id", "PLATFORM") if tenant else "PLATFORM",
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action=action,
            resource_type="DynamicFieldGroupLayout",
            resource_id=str(layout.layout_id),
            metadata_json={
                "group_code": layout.group_code,
                "dosage_form_family": layout.dosage_form_family,
                "display_order": layout.display_order,
            },
            status=AuditActionStatus.SUCCESS,
        )

        return {
            "layout_id": str(layout.layout_id),
            "tenant_id": getattr(tenant, "tenant_id", None),
            "group_code": layout.group_code,
            "dosage_form_family": layout.dosage_form_family,
            "display_order": layout.display_order,
            "title_en": layout.title_en,
            "title_bn": layout.title_bn,
            "keyboard_shortcut_mnemonic": layout.keyboard_shortcut_mnemonic,
            "display_density": layout.display_density,
            "is_collapsible": layout.is_collapsible,
            "default_collapsed": layout.default_collapsed,
            "is_active": layout.is_active,
        }

    @classmethod
    @transaction.atomic
    def delete_or_archive_layout(cls, layout_id: str, user: PharmacyUser, hard_delete: bool = False) -> Dict[str, Any]:
        """Archives or deletes a custom dynamic field group layout."""
        cls.check_permissions(user, required_action="delete")
        try:
            layout = DynamicFieldGroupLayout.objects.get(pk=layout_id)
        except DynamicFieldGroupLayout.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_LAYOUT_NOT_FOUND",
                "detail": f"DynamicFieldGroupLayout '{layout_id}' not found.",
            })

        user_tenant_id = getattr(user, "tenant_id", None)
        tenant = cls._resolve_tenant(user)
        if layout.tenant_id and user_tenant_id and str(layout.tenant_id) != str(user_tenant_id) and not user.is_superuser:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                "detail": "Cross-tenant deletion prohibited.",
            })

        if hard_delete:
            layout.delete()
            status_msg = "DELETED"
        else:
            layout.is_active = False
            layout.save(update_fields=["is_active", "updated_at"])
            status_msg = "ARCHIVED"

        AuditLogEntry.objects.create(
            tenant_id=getattr(tenant, "tenant_id", "PLATFORM") if tenant else "PLATFORM",
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="LAYOUT_GROUP_REMOVED",
            resource_type="DynamicFieldGroupLayout",
            resource_id=str(layout_id),
            metadata_json={"status": status_msg},
            status=AuditActionStatus.SUCCESS,
        )

        return {"status": status_msg, "layout_id": layout_id}
