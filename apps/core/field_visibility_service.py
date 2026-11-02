"""
Field Visibility and Sensitive Data Masking Service (P110 / ADR-0110).
Enforces fine-grained field-level visibility across 5 sensitive domains:
1. Purchase Costs (cost_visibility)
2. Profits (profit_visibility)
3. Staff Salaries & Payroll (salary_visibility)
4. Clinical Prescriptions (prescription_visibility)
5. Patient Health Information / PII (patient_pii_visibility)

Also guarantees export-level masking so unauthorized users cannot exfiltrate
sensitive commercial margins, employee compensation, or patient PII via downloads.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    BranchStaffMembership,
    CustomRoleDefinition,
    FieldVisibilityAuditEvent,
    StaffMember,
    StaffMembership,
    StaffSalaryRecord,
    Tenant,
)
from apps.core.postgres_service import CrossTenantOperationError

logger = logging.getLogger("pharmacy.field_visibility")


class FieldVisibilityService:
    """Authoritative evaluation, masking engine, and secure export generator."""

    VISIBILITY_KEYS: Set[str] = {
        "cost_visibility",
        "profit_visibility",
        "salary_visibility",
        "prescription_visibility",
        "patient_pii_visibility",
    }

    ACTION_TO_KEYS_MAP: Dict[str, Set[str]] = {
        "cost_visibility": {"inventory:view_cost", "action:cost_visibility", "cost_visibility"},
        "profit_visibility": {"finance:executive_dashboard", "finance:profit_visibility", "action:profit_visibility", "profit_visibility"},
        "salary_visibility": {"hr:view_salary", "hr:salary_visibility", "action:salary_visibility", "salary_visibility"},
        "prescription_visibility": {"rx:verify", "dispense:rx_antibiotics", "action:rx_review", "action:prescription_visibility", "prescription_visibility"},
        "patient_pii_visibility": {"patient:view_pii", "action:patient_pii_visibility", "patient_pii_visibility"},
    }

    # -------------------------------------------------------------------------
    # 1. PERMISSION RESOLUTION
    # -------------------------------------------------------------------------

    @classmethod
    def resolve_field_visibility_profile(
        cls,
        user: Any,
        tenant_id: Optional[str] = None,
        branch_id: Optional[str] = None,
    ) -> Dict[str, bool]:
        """
        Resolves caller's boolean flags for the 5 field visibility keys.
        Inspects Superadmin/Owner overrides, Custom Roles, and Canonical Defaults.
        """
        if not user or not getattr(user, "is_authenticated", False):
            return {k: False for k in cls.VISIBILITY_KEYS}

        role_str = str(getattr(user, "role", "")).upper()

        # 1. Superadmin & Pharmacy Owner have unrestricted visibility on all 5 keys
        if role_str in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR", "PHARMACY_OWNER", "TENANT_OWNER"]:
            return {k: True for k in cls.VISIBILITY_KEYS}

        user_tid = getattr(user, "tenant_id", None) or tenant_id
        if hasattr(user_tid, "tenant_id"):
            user_tid = user_tid.tenant_id
        user_tid = str(user_tid) if user_tid else None

        user_ident = getattr(user, "username", getattr(user, "email", ""))

        # 2. Check Custom Role via BranchStaffMembership or StaffMembership
        custom_role: Optional[CustomRoleDefinition] = None
        if user_tid:
            if branch_id:
                staff = StaffMember.objects.filter(tenant_id=user_tid).filter(username=user_ident).first()
                if staff:
                    bsm = BranchStaffMembership.objects.filter(
                        tenant_id=user_tid,
                        staff_member=staff,
                        branch_id=branch_id,
                        is_active=True,
                    ).first()
                    if bsm:
                        role_str = bsm.role.upper()
                        if bsm.custom_role_code:
                            custom_role = CustomRoleDefinition.objects.filter(
                                tenant_id=user_tid,
                                role_code__iexact=bsm.custom_role_code,
                                is_archived=False,
                            ).first()

            if not custom_role:
                membership = StaffMembership.objects.filter(
                    tenant_id=user_tid,
                    staff_member__username=user_ident,
                    is_active=True,
                ).first()
                if membership and membership.role:
                    custom_role = CustomRoleDefinition.objects.filter(
                        tenant_id=user_tid,
                        role_code__iexact=membership.role,
                        is_archived=False,
                    ).first()
                if not custom_role and role_str:
                    custom_role = CustomRoleDefinition.objects.filter(
                        tenant_id=user_tid,
                        role_code__iexact=role_str,
                        is_archived=False,
                    ).first()

        if custom_role:
            granted_keys = set(custom_role.permission_keys)
            profile = {}
            for v_key, mapped_perms in cls.ACTION_TO_KEYS_MAP.items():
                profile[v_key] = bool(granted_keys.intersection(mapped_perms))
            return profile

        # 3. Canonical System Role Matrix (ADR-0110)
        canonical_matrix: Dict[str, Dict[str, bool]] = {
            "BRANCH_MANAGER": {
                "cost_visibility": True,
                "profit_visibility": False,
                "salary_visibility": False,
                "prescription_visibility": True,
                "patient_pii_visibility": True,
            },
            "FINANCE_OFFICER": {
                "cost_visibility": True,
                "profit_visibility": True,
                "salary_visibility": True,
                "prescription_visibility": False,
                "patient_pii_visibility": False,
            },
            "STAFF_PHARMACIST": {
                "cost_visibility": False,
                "profit_visibility": False,
                "salary_visibility": False,
                "prescription_visibility": True,
                "patient_pii_visibility": True,
            },
            "PHARMACIST": {
                "cost_visibility": False,
                "profit_visibility": False,
                "salary_visibility": False,
                "prescription_visibility": True,
                "patient_pii_visibility": True,
            },
            "CASHIER": {
                "cost_visibility": False,
                "profit_visibility": False,
                "salary_visibility": False,
                "prescription_visibility": False,
                "patient_pii_visibility": False,
            },
            "INVENTORY_CLERK": {
                "cost_visibility": True,
                "profit_visibility": False,
                "salary_visibility": False,
                "prescription_visibility": False,
                "patient_pii_visibility": False,
            },
            "AUDITOR": {
                "cost_visibility": True,
                "profit_visibility": True,
                "salary_visibility": False,
                "prescription_visibility": True,
                "patient_pii_visibility": True,
            },
        }

        return canonical_matrix.get(role_str, {k: False for k in cls.VISIBILITY_KEYS})

    # -------------------------------------------------------------------------
    # 2. STRING AND PII MASKING UTILITIES
    # -------------------------------------------------------------------------

    @classmethod
    def mask_phone_number(cls, phone: Optional[str]) -> str:
        """Transforms '01712345678' into '017****5678'."""
        if not phone:
            return "***"
        val = str(phone).strip()
        if len(val) >= 11:
            return f"{val[:3]}****{val[-4:]}"
        elif len(val) >= 6:
            return f"{val[:2]}****{val[-2:]}"
        return "***REDACTED***"

    @classmethod
    def mask_person_name(cls, name: Optional[str]) -> str:
        """Transforms 'Rahim Uddin' into 'R**** U****'."""
        if not name:
            return "***"
        parts = str(name).strip().split()
        masked_parts = []
        for p in parts:
            if len(p) > 1:
                masked_parts.append(f"{p[0]}****")
            else:
                masked_parts.append(p)
        return " ".join(masked_parts) if masked_parts else "***"

    # -------------------------------------------------------------------------
    # 3. DOMAIN-SPECIFIC MASKING ENGINES
    # -------------------------------------------------------------------------

    @classmethod
    def mask_medicine_record(cls, record: Dict[str, Any], perms: Dict[str, bool]) -> Dict[str, Any]:
        """Masks purchase cost fields if cost_visibility is false."""
        res = dict(record)
        if not perms.get("cost_visibility", False):
            for field_name in [
                "cost_unit",
                "cost_price",
                "cost_per_unit",
                "purchase_price",
                "unit_cost_bdt",
                "supplier_rate",
                "batch_cost",
                "cogs_bdt",
            ]:
                if field_name in res:
                    res[field_name] = None
            res["_cost_masked"] = True
        else:
            res["_cost_masked"] = False
        return res

    @classmethod
    def mask_invoice_record(cls, record: Dict[str, Any], perms: Dict[str, bool]) -> Dict[str, Any]:
        """
        Masks:
        - Customer Phone & Name if patient_pii_visibility is false
        - COGS and Unit Costs if cost_visibility is false
        - Gross Profit, Net Profit, Margins if profit_visibility is false
        """
        res = dict(record)

        # 1. Patient PII Masking
        if not perms.get("patient_pii_visibility", False):
            if "customer_phone" in res and res["customer_phone"]:
                res["customer_phone"] = cls.mask_phone_number(res["customer_phone"])
            if "customer_name" in res and res["customer_name"]:
                res["customer_name"] = cls.mask_person_name(res["customer_name"])
            if "patient_phone" in res and res["patient_phone"]:
                res["patient_phone"] = cls.mask_phone_number(res["patient_phone"])
            if "patient_name" in res and res["patient_name"]:
                res["patient_name"] = cls.mask_person_name(res["patient_name"])
            res["_pii_masked"] = True
        else:
            res["_pii_masked"] = False

        # 2. Cost Masking
        if not perms.get("cost_visibility", False):
            for cost_k in ["cogs_amount", "total_cogs_bdt", "unit_cost", "cost_unit"]:
                if cost_k in res:
                    res[cost_k] = None
            res["_cost_masked"] = True
        else:
            res["_cost_masked"] = False

        # 3. Profit Masking
        if not perms.get("profit_visibility", False):
            for prof_k in [
                "gross_profit",
                "net_profit",
                "margin_percent",
                "margin_amount",
                "profit_bdt",
                "gross_margin_bdt",
            ]:
                if prof_k in res:
                    res[prof_k] = None
            res["_profit_masked"] = True
        else:
            res["_profit_masked"] = False

        return res

    @classmethod
    def mask_salary_record(cls, record: Dict[str, Any], perms: Dict[str, bool]) -> Dict[str, Any]:
        """Masks compensation figures and bank details if salary_visibility is false."""
        res = dict(record)
        if not perms.get("salary_visibility", False):
            for sal_k in [
                "base_salary_bdt",
                "hourly_rate_bdt",
                "allowances_bdt",
                "total_compensation_bdt",
                "salary_amount",
            ]:
                if sal_k in res:
                    res[sal_k] = None
            if "bank_account_no" in res and res["bank_account_no"]:
                res["bank_account_no"] = "***REDACTED***"
            if "bank_name" in res and res["bank_name"]:
                res["bank_name"] = "***REDACTED***"
            res["_salary_masked"] = True
        else:
            res["_salary_masked"] = False
        return res

    @classmethod
    def mask_prescription_record(cls, record: Dict[str, Any], perms: Dict[str, bool]) -> Dict[str, Any]:
        """
        Masks clinical diagnosis, doctor notes, and prescription image if prescription_visibility is false.
        Also masks patient PII if patient_pii_visibility is false.
        """
        res = dict(record)

        if not perms.get("prescription_visibility", False):
            if "diagnosis" in res and res["diagnosis"]:
                res["diagnosis"] = "[CLINICAL_DIAGNOSIS_REDACTED]"
            if "doctor_notes" in res and res["doctor_notes"]:
                res["doctor_notes"] = "[CONFIDENTIAL_CLINICAL_NOTES]"
            if "notes" in res and res["notes"]:
                res["notes"] = "[CONFIDENTIAL_CLINICAL_NOTES]"
            if "prescription_image_url" in res and res["prescription_image_url"]:
                res["prescription_image_url"] = "[RESTRICTED_ACCESS]"
            res["_prescription_masked"] = True
        else:
            res["_prescription_masked"] = False

        if not perms.get("patient_pii_visibility", False):
            if "patient_name" in res and res["patient_name"]:
                res["patient_name"] = cls.mask_person_name(res["patient_name"])
            if "patient_phone" in res and res["patient_phone"]:
                res["patient_phone"] = cls.mask_phone_number(res["patient_phone"])
            if "patient_token" in res and res["patient_token"]:
                # partially mask token
                val = str(res["patient_token"])
                if len(val) > 4:
                    res["patient_token"] = f"{val[:2]}****{val[-2:]}"
            res["_pii_masked"] = True
        else:
            res["_pii_masked"] = False

        return res

    @classmethod
    def mask_dataset(
        cls,
        dataset: List[Dict[str, Any]],
        perms: Dict[str, bool],
        dataset_type: str = "AUTO",
    ) -> List[Dict[str, Any]]:
        """Masks an entire list of records according to its dataset_type."""
        if not dataset:
            return []

        clean_type = dataset_type.upper().strip()
        results = []

        for item in dataset:
            if not isinstance(item, dict):
                results.append(item)
                continue

            # Auto-detect dataset type if requested
            eff_type = clean_type
            if eff_type == "AUTO":
                if "base_salary_bdt" in item or "hourly_rate_bdt" in item or "salary_id" in item:
                    eff_type = "SALARY"
                elif "diagnosis" in item or "doctor_bmdc_no" in item or "prescription_id" in item:
                    eff_type = "PRESCRIPTION"
                elif "invoice_id" in item or "gross_profit" in item or "total_amount" in item:
                    eff_type = "INVOICE"
                elif "mrp_unit" in item or "brand_name" in item or "cost_unit" in item:
                    eff_type = "MEDICINE"
                else:
                    eff_type = "GENERIC"

            if eff_type == "MEDICINE":
                results.append(cls.mask_medicine_record(item, perms))
            elif eff_type == "INVOICE":
                results.append(cls.mask_invoice_record(item, perms))
            elif eff_type == "SALARY":
                results.append(cls.mask_salary_record(item, perms))
            elif eff_type == "PRESCRIPTION":
                results.append(cls.mask_prescription_record(item, perms))
            else:
                # Generic fallback: run all 4 maskers in sequence
                m = cls.mask_medicine_record(item, perms)
                m = cls.mask_invoice_record(m, perms)
                m = cls.mask_salary_record(m, perms)
                m = cls.mask_prescription_record(m, perms)
                results.append(m)

        return results

    # -------------------------------------------------------------------------
    # 4. SECURE DATA EXPORT ENGINE
    # -------------------------------------------------------------------------

    @classmethod
    def generate_masked_export(
        cls,
        dataset: List[Dict[str, Any]],
        perms: Dict[str, bool],
        dataset_type: str,
        export_format: str = "CSV",
        actor_user: Any = None,
        tenant_id: Optional[str] = None,
        ip_address: str = "",
    ) -> Tuple[str, str, str]:
        """
        Executes field-masking on the dataset and serializes into CSV or JSON.
        Records an immutable FieldVisibilityAuditEvent.
        Returns: (content_string, content_type, filename)
        """
        masked_data = cls.mask_dataset(dataset, perms=perms, dataset_type=dataset_type)
        fmt = export_format.upper().strip()

        actor_name = getattr(actor_user, "username", getattr(actor_user, "email", "system"))
        actor_role = str(getattr(actor_user, "role", "UNKNOWN")).upper()
        now_str = timezone.now().strftime("%Y%m%d_%H%M%S")

        # Record audit event if tenant is given
        if tenant_id:
            try:
                tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
                if tenant:
                    FieldVisibilityAuditEvent.objects.create(
                        event_id=f"vis-audit-{uuid.uuid4().hex[:12]}",
                        tenant=tenant,
                        action="DATASET_EXPORT",
                        field_category=dataset_type.upper(),
                        actor_username=actor_name,
                        actor_role=actor_role,
                        details={
                            "dataset_type": dataset_type,
                            "format": fmt,
                            "record_count": len(dataset),
                            "permissions_applied": perms,
                        },
                        ip_address=ip_address,
                    )
            except Exception as e:
                logger.warning("Could not record FieldVisibilityAuditEvent: %s", e)

        if fmt == "JSON":
            content = json.dumps(masked_data, indent=2, default=str)
            filename = f"export_{dataset_type.lower()}_{now_str}.json"
            return content, "application/json", filename

        # Default CSV Generation
        output = io.StringIO()
        if masked_data:
            fieldnames = list(masked_data[0].keys())
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            for row in masked_data:
                # Convert None to empty or string representation
                clean_row = {}
                for k, v in row.items():
                    if v is None:
                        clean_row[k] = ""
                    else:
                        clean_row[k] = str(v)
                writer.writerow(clean_row)
        else:
            output.write("No records found\n")

        filename = f"export_{dataset_type.lower()}_{now_str}.csv"
        return output.getvalue(), "text/csv", filename

    # -------------------------------------------------------------------------
    # 5. SALARY MANAGEMENT WITH ACCESS GATING
    # -------------------------------------------------------------------------

    @classmethod
    def create_or_update_salary(
        cls,
        actor_user: Any,
        tenant_id: str,
        staff_user_id: str,
        base_salary_bdt: Decimal,
        hourly_rate_bdt: Decimal = Decimal("0.00"),
        allowances_bdt: Decimal = Decimal("0.00"),
        bank_name: str = "",
        bank_account_no: str = "",
        branch_id: Optional[str] = None,
    ) -> StaffSalaryRecord:
        """
        Creates or updates StaffSalaryRecord.
        Requires actor to possess salary_visibility permission!
        """
        perms = cls.resolve_field_visibility_profile(actor_user, tenant_id=tenant_id, branch_id=branch_id)
        if not perms.get("salary_visibility", False):
            role_str = str(getattr(actor_user, "role", "UNKNOWN")).upper()
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERMISSION_DENIED",
                    "role": role_str,
                    "message_en": f"Access denied: role '{role_str}' is not authorized to create or modify staff compensation.",
                    "message_bn": f"অননুমোদিত অ্যাকশন: '{role_str}' রোলের ব্যবহারকারীর বেতন ও ক্ষতিপূরণ তথ্য পরিবর্তন করার অনুমতি নেই।",
                }
            )

        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise CrossTenantOperationError(f"Tenant '{tenant_id}' does not exist.")

        # Guard: Check tenant subscription
        tenant_status = str(getattr(tenant, "status", getattr(tenant, "lifecycle_status", ""))).upper()
        if tenant_status in ["SUSPENDED", "ARCHIVED", "TERMINATED"]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "message_en": "Tenant subscription is inactive or suspended. Payroll operations blocked.",
                    "message_bn": "টেন্যান্ট সাবস্ক্রিপশন স্থগিত রয়েছে। পেরোল অপারেশন সম্পন্ন করা সম্ভব নয়।",
                }
            )

        branch = None
        if branch_id:
            branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()

        from apps.identity.models import PharmacyUser
        staff = PharmacyUser.objects.filter(user_id=staff_user_id).first()
        if not staff:
            raise ValidationError({"error_code": "STAFF_NOT_FOUND", "message": f"Staff user '{staff_user_id}' not found."})

        actor_name = getattr(actor_user, "username", getattr(actor_user, "email", "system"))

        with transaction.atomic():
            record, created = StaffSalaryRecord.objects.get_or_create(
                tenant=tenant,
                staff_member=staff,
                defaults={
                    "salary_id": f"sal-{uuid.uuid4().hex[:12]}",
                    "branch": branch,
                    "base_salary_bdt": base_salary_bdt,
                    "hourly_rate_bdt": hourly_rate_bdt,
                    "allowances_bdt": allowances_bdt,
                    "bank_name": bank_name,
                    "bank_account_no": bank_account_no,
                    "created_by_username": actor_name,
                    "updated_by_username": actor_name,
                },
            )
            if not created:
                record.base_salary_bdt = base_salary_bdt
                record.hourly_rate_bdt = hourly_rate_bdt
                record.allowances_bdt = allowances_bdt
                if bank_name:
                    record.bank_name = bank_name
                if bank_account_no:
                    record.bank_account_no = bank_account_no
                if branch:
                    record.branch = branch
                record.updated_by_username = actor_name
                record.save()

            FieldVisibilityAuditEvent.objects.create(
                event_id=f"vis-audit-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                action="SALARY_MODIFIED",
                field_category="SALARY",
                actor_username=actor_name,
                actor_role=str(getattr(actor_user, "role", "UNKNOWN")).upper(),
                details={
                    "salary_id": record.salary_id,
                    "staff_user_id": staff_user_id,
                    "base_salary_bdt": str(base_salary_bdt),
                    "created": created,
                },
            )

        return record
