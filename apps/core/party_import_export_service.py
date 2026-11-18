"""
backend/apps/core/party_import_export_service.py
Architecture Decision: ADR-0278 (P278).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service governing Party (Customer & Supplier) Import/Export:
- Import preview with schema validation, BD phone normalization, and duplicate detection
- Scoped permissions and privacy review (PII masking for non-owners)
- Conflict resolution strategies (SKIP vs UPDATE) preserving existing financial ledger links
- Strict multi-tenant isolation and immutable batch audit logging
"""

from __future__ import annotations

import logging
import re
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.customer_search_merge_service import normalize_bd_phone
from apps.core.models import (
    Branch,
    PosCustomer,
    Supplier,
    SupplierRegion,
    SupplierStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

PARTY_IMPORT_EXPORT_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

UNMASKED_PRIVACY_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

STAFF_DENIED_ROLES = {
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    "CASHIER",
    "STAFF_PHARMACIST",
    "INVENTORY_CLERK",
    "DISPENSER",
}


def mask_phone(phone: str) -> str:
    """Masks phone number (e.g. +8801712345678 -> 017****5678)."""
    p = str(phone or "").strip()
    if len(p) >= 11:
        return f"{p[:3]}****{p[-4:]}"
    return "***"


def mask_email(email: str) -> str:
    """Masks email address (e.g. rafiq@example.com -> r***@example.com)."""
    e = str(email or "").strip()
    if "@" in e:
        parts = e.split("@", 1)
        name, domain = parts[0], parts[1]
        masked_name = f"{name[0]}***" if len(name) > 1 else "***"
        return f"{masked_name}@{domain}"
    return "***"


def mask_bank_account(acc_no: str) -> str:
    """Masks bank account number (e.g. 1234567890 -> ****7890)."""
    a = str(acc_no or "").strip()
    if len(a) > 4:
        return f"****{a[-4:]}"
    return "****"


class PartyImportExportService:
    """
    Authoritative domain service for Customer and Supplier Import/Export.
    """

    @classmethod
    def assert_authorized_actor(cls, actor_user: Any) -> None:
        """Enforces role-based permissions for party import/export."""
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_UNAUTHENTICATED",
                    "detail": "Authentication required for party import/export.",
                }
            )
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in PARTY_IMPORT_EXPORT_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and staff pharmacist are not authorized to import or export party records.",
                    "message_en": "Cashier and staff pharmacist are not authorized to import or export party records.",
                    "message_bn": "ক্যাশিয়ার বা স্টাফ ফার্মাসিস্ট কাস্টমার বা সাপ্লায়ার ইমপোর্ট/এক্সপোর্ট করার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def can_view_unmasked_privacy(cls, actor_user: Any) -> bool:
        """Determines if the actor has permission to view unmasked PII and sensitive financial data."""
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            return False
        if getattr(actor_user, "is_superuser", False):
            return True
        role = getattr(actor_user, "role", None)
        return role in UNMASKED_PRIVACY_ROLES

    @classmethod
    def assert_tenant_active(cls, tenant_id: str) -> Tenant:
        """Validates tenant existence and ensures subscription is active."""
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' not found."})
        if (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant account subscription is suspended. Party import/export blocked.",
                    "message_en": "Tenant account subscription is suspended. Party import/export blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। পার্টি ইমপোর্ট/এক্সপোর্ট বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    # -------------------------------------------------------------------------
    # EXPORT
    # -------------------------------------------------------------------------

    @classmethod
    def export_parties(
        cls,
        *,
        actor_user: Any,
        tenant_id: str,
        party_type: str,
        include_sensitive: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Exports tenant-scoped customer or supplier records with role-based privacy masking.
        """
        cls.assert_authorized_actor(actor_user)
        cls.assert_tenant_active(tenant_id)

        clean_type = str(party_type or "CUSTOMER").strip().upper()
        allow_unmasked = cls.can_view_unmasked_privacy(actor_user) and include_sensitive

        if clean_type == "CUSTOMER":
            qs = PosCustomer.objects.filter(tenant_id=tenant_id, is_archived=False).select_related("primary_branch")
            records = []
            for c in qs.order_by("full_name"):
                records.append(
                    {
                        "party_type": "CUSTOMER",
                        "customer_id": c.customer_id,
                        "full_name": c.full_name,
                        "phone": c.phone if allow_unmasked else mask_phone(c.phone),
                        "email": c.email if allow_unmasked else mask_email(c.email),
                        "address": c.address if allow_unmasked else ("[REDACTED]" if c.address else ""),
                        "loyalty_code": c.loyalty_code,
                        "credit_limit_bdt": str(c.credit_limit_bdt) if allow_unmasked else "[RESTRICTED]",
                        "current_due_bdt": str(c.current_due_bdt) if allow_unmasked else "[RESTRICTED]",
                        "credit_status": c.credit_status,
                        "is_credit_approved": c.is_credit_approved,
                        "credit_terms_days": c.credit_terms_days,
                        "is_active": c.is_active,
                        "branch_name": c.primary_branch.name if c.primary_branch else "",
                    }
                )
            return records

        elif clean_type in ("SUPPLIER", "VENDOR"):
            qs = Supplier.objects.filter(tenant_id=tenant_id, is_active=True).select_related("tenant")
            records = []
            for s in qs.order_by("name"):
                records.append(
                    {
                        "party_type": "SUPPLIER",
                        "supplier_id": s.supplier_id,
                        "name": s.name,
                        "code": s.code,
                        "contact_person": s.contact_person,
                        "phone": s.phone if allow_unmasked else mask_phone(s.phone),
                        "email": s.email if allow_unmasked else mask_email(s.email),
                        "address": s.address if allow_unmasked else ("[REDACTED]" if s.address else ""),
                        "dl_number": s.dl_number,
                        "tin_number": s.tin_number if allow_unmasked else mask_bank_account(s.tin_number),
                        "bin_number": s.bin_number if allow_unmasked else mask_bank_account(s.bin_number),
                        "trade_license_number": s.trade_license_number,
                        "region": s.region,
                        "district": s.district,
                        "status": s.status,
                        "payment_terms_days": s.payment_terms_days,
                        "credit_limit": str(s.credit_limit) if allow_unmasked else "[RESTRICTED]",
                        "current_balance": str(s.current_balance) if allow_unmasked else "[RESTRICTED]",
                        "bank_name": s.bank_name,
                        "bank_account_no": s.bank_account_no if allow_unmasked else mask_bank_account(s.bank_account_no),
                    }
                )
            return records
        else:
            raise ValidationError({"error_code": "ERR_INVALID_PARTY_TYPE", "detail": f"Unknown party type '{party_type}'."})

    # -------------------------------------------------------------------------
    # IMPORT PREVIEW & DEDUPLICATION
    # -------------------------------------------------------------------------

    @classmethod
    def preview_import(
        cls,
        *,
        actor_user: Any,
        tenant_id: str,
        party_type: str,
        records: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Parses incoming batch records, validates schemas, checks BD phone formats,
        and identifies duplicate entries against existing tenant records.
        """
        cls.assert_authorized_actor(actor_user)
        cls.assert_tenant_active(tenant_id)

        clean_type = str(party_type or "CUSTOMER").strip().upper()
        if not records or not isinstance(records, list):
            raise ValidationError({"error_code": "ERR_EMPTY_IMPORT_PAYLOAD", "detail": "Import batch records list is required."})

        valid_count = 0
        duplicate_count = 0
        invalid_count = 0
        preview_items: List[Dict[str, Any]] = []

        if clean_type == "CUSTOMER":
            # Preload existing phones in tenant for fast lookup
            existing_customers = PosCustomer.objects.filter(tenant_id=tenant_id)
            phone_to_customer: Dict[str, PosCustomer] = {}
            for ec in existing_customers:
                norm = normalize_bd_phone(ec.phone)
                for var in norm["variants"]:
                    if var:
                        phone_to_customer[var] = ec

            for idx, r in enumerate(records, 1):
                name = str(r.get("full_name") or r.get("name") or "").strip()
                raw_phone = str(r.get("phone") or "").strip()
                norm_phone = normalize_bd_phone(raw_phone)

                errors = []
                if not name:
                    errors.append("Customer name (full_name) is required.")
                if not raw_phone:
                    errors.append("Phone number is required.")
                elif not norm_phone["is_valid_bd"] and len(norm_phone["digits"]) < 7:
                    errors.append(f"Invalid phone number '{raw_phone}'.")

                is_dup = False
                dup_match_id = None
                dup_match_name = None

                if not errors:
                    # Check duplicate
                    for var in norm_phone["variants"]:
                        if var and var in phone_to_customer:
                            is_dup = True
                            dup_match_id = phone_to_customer[var].customer_id
                            dup_match_name = phone_to_customer[var].full_name
                            break

                if errors:
                    invalid_count += 1
                elif is_dup:
                    duplicate_count += 1
                else:
                    valid_count += 1

                preview_items.append(
                    {
                        "row_number": idx,
                        "full_name": name,
                        "phone": norm_phone["local_11"] or raw_phone,
                        "email": str(r.get("email") or "").strip(),
                        "address": str(r.get("address") or "").strip(),
                        "credit_limit_bdt": str(r.get("credit_limit_bdt") or "0.00"),
                        "is_valid": len(errors) == 0,
                        "is_duplicate": is_dup,
                        "duplicate_match_id": dup_match_id,
                        "duplicate_match_name": dup_match_name,
                        "errors": errors,
                    }
                )

        elif clean_type in ("SUPPLIER", "VENDOR"):
            existing_suppliers = Supplier.objects.filter(tenant_id=tenant_id)
            code_to_supplier: Dict[str, Supplier] = {}
            phone_to_supplier: Dict[str, Supplier] = {}
            name_to_supplier: Dict[str, Supplier] = {}

            for es in existing_suppliers:
                if es.code:
                    code_to_supplier[es.code.lower().strip()] = es
                if es.name:
                    name_to_supplier[es.name.lower().strip()] = es
                if es.phone:
                    norm = normalize_bd_phone(es.phone)
                    for var in norm["variants"]:
                        if var:
                            phone_to_supplier[var] = es

            for idx, r in enumerate(records, 1):
                name = str(r.get("name") or "").strip()
                code = str(r.get("code") or "").strip()
                raw_phone = str(r.get("phone") or "").strip()
                norm_phone = normalize_bd_phone(raw_phone)

                errors = []
                if not name:
                    errors.append("Supplier name is required.")

                is_dup = False
                dup_match_id = None
                dup_match_name = None

                if not errors:
                    if code and code.lower() in code_to_supplier:
                        is_dup = True
                        dup_match_id = code_to_supplier[code.lower()].supplier_id
                        dup_match_name = code_to_supplier[code.lower()].name
                    elif name.lower() in name_to_supplier:
                        is_dup = True
                        dup_match_id = name_to_supplier[name.lower()].supplier_id
                        dup_match_name = name_to_supplier[name.lower()].name
                    elif raw_phone:
                        for var in norm_phone["variants"]:
                            if var and var in phone_to_supplier:
                                is_dup = True
                                dup_match_id = phone_to_supplier[var].supplier_id
                                dup_match_name = phone_to_supplier[var].name
                                break

                if errors:
                    invalid_count += 1
                elif is_dup:
                    duplicate_count += 1
                else:
                    valid_count += 1

                preview_items.append(
                    {
                        "row_number": idx,
                        "name": name,
                        "code": code,
                        "contact_person": str(r.get("contact_person") or "").strip(),
                        "phone": norm_phone["local_11"] or raw_phone,
                        "email": str(r.get("email") or "").strip(),
                        "dl_number": str(r.get("dl_number") or "").strip(),
                        "trade_license_number": str(r.get("trade_license_number") or "").strip(),
                        "is_valid": len(errors) == 0,
                        "is_duplicate": is_dup,
                        "duplicate_match_id": dup_match_id,
                        "duplicate_match_name": dup_match_name,
                        "errors": errors,
                    }
                )
        else:
            raise ValidationError({"error_code": "ERR_INVALID_PARTY_TYPE", "detail": f"Unknown party type '{party_type}'."})

        return {
            "party_type": clean_type,
            "total_records": len(records),
            "valid_count": valid_count,
            "duplicate_count": duplicate_count,
            "invalid_count": invalid_count,
            "preview_items": preview_items,
        }

    # -------------------------------------------------------------------------
    # IMPORT COMMIT
    # -------------------------------------------------------------------------

    @classmethod
    @transaction.atomic
    def commit_import(
        cls,
        *,
        actor_user: Any,
        tenant_id: str,
        party_type: str,
        records: List[Dict[str, Any]],
        duplicate_resolution: str = "SKIP",
    ) -> Dict[str, Any]:
        """
        Executes bulk import of validated records into database.
        Supports duplicate_resolution: 'SKIP' (ignore duplicates) or 'UPDATE' (update existing).
        """
        cls.assert_authorized_actor(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_type = str(party_type or "CUSTOMER").strip().upper()
        resolution = str(duplicate_resolution or "SKIP").strip().upper()
        if resolution not in ("SKIP", "UPDATE"):
            resolution = "SKIP"

        created_count = 0
        updated_count = 0
        skipped_count = 0
        default_branch = Branch.objects.filter(tenant=tenant).first()

        if clean_type == "CUSTOMER":
            for r in records:
                name = str(r.get("full_name") or r.get("name") or "").strip()
                raw_phone = str(r.get("phone") or "").strip()
                if not name or not raw_phone:
                    skipped_count += 1
                    continue

                norm_phone = normalize_bd_phone(raw_phone)
                phone_str = norm_phone["e164"] if norm_phone["is_valid_bd"] else raw_phone

                # Check if existing customer matches
                existing = None
                for var in norm_phone["variants"]:
                    if var:
                        existing = PosCustomer.objects.filter(tenant=tenant, phone__icontains=var).first()
                        if existing:
                            break

                if existing:
                    if resolution == "UPDATE":
                        existing.full_name = name
                        if r.get("email"):
                            existing.email = str(r.get("email")).strip()
                        if r.get("address"):
                            existing.address = str(r.get("address")).strip()
                        existing.save()
                        updated_count += 1
                    else:
                        skipped_count += 1
                else:
                    cust_id = f"cust-{uuid.uuid4().hex[:10]}"
                    credit_limit = Decimal("0.00")
                    if r.get("credit_limit_bdt"):
                        try:
                            credit_limit = Decimal(str(r.get("credit_limit_bdt")))
                        except Exception:
                            credit_limit = Decimal("0.00")

                    PosCustomer.objects.create(
                        customer_id=cust_id,
                        tenant=tenant,
                        primary_branch=default_branch,
                        full_name=name,
                        phone=phone_str,
                        email=str(r.get("email") or "").strip(),
                        address=str(r.get("address") or "").strip(),
                        loyalty_code=str(r.get("loyalty_code") or "").strip(),
                        credit_limit_bdt=credit_limit,
                    )
                    created_count += 1

        elif clean_type in ("SUPPLIER", "VENDOR"):
            for r in records:
                name = str(r.get("name") or "").strip()
                code = str(r.get("code") or "").strip()
                if not name:
                    skipped_count += 1
                    continue

                existing = None
                if code:
                    existing = Supplier.objects.filter(tenant=tenant, code__iexact=code).first()
                if not existing:
                    existing = Supplier.objects.filter(tenant=tenant, name__iexact=name).first()

                if existing:
                    if resolution == "UPDATE":
                        existing.contact_person = str(r.get("contact_person") or existing.contact_person).strip()
                        if r.get("phone"):
                            existing.phone = str(r.get("phone")).strip()
                        if r.get("email"):
                            existing.email = str(r.get("email")).strip()
                        if r.get("address"):
                            existing.address = str(r.get("address")).strip()
                        if r.get("dl_number"):
                            existing.dl_number = str(r.get("dl_number")).strip()
                        existing.save()
                        updated_count += 1
                    else:
                        skipped_count += 1
                else:
                    sup_id = f"sup-{uuid.uuid4().hex[:10]}"
                    Supplier.objects.create(
                        supplier_id=sup_id,
                        tenant=tenant,
                        name=name,
                        code=code or f"SUP-{uuid.uuid4().hex[:6].upper()}",
                        contact_person=str(r.get("contact_person") or "").strip(),
                        phone=str(r.get("phone") or "").strip(),
                        email=str(r.get("email") or "").strip(),
                        address=str(r.get("address") or "").strip(),
                        dl_number=str(r.get("dl_number") or "").strip(),
                        tin_number=str(r.get("tin_number") or "").strip(),
                        bin_number=str(r.get("bin_number") or "").strip(),
                        trade_license_number=str(r.get("trade_license_number") or "").strip(),
                    )
                    created_count += 1
        else:
            raise ValidationError({"error_code": "ERR_INVALID_PARTY_TYPE", "detail": f"Unknown party type '{party_type}'."})

        # Audit log
        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="PARTY_IMPORT_COMMIT",
            resource_type=f"PartyImport:{clean_type}",
            resource_id=f"batch-{uuid.uuid4().hex[:8]}",
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "party_type": clean_type,
                "created_count": created_count,
                "updated_count": updated_count,
                "skipped_count": skipped_count,
                "duplicate_resolution": resolution,
            },
        )

        return {
            "success": True,
            "party_type": clean_type,
            "created_count": created_count,
            "updated_count": updated_count,
            "skipped_count": skipped_count,
            "duplicate_resolution": resolution,
            "message_en": f"Import completed: {created_count} created, {updated_count} updated, {skipped_count} skipped.",
            "message_bn": f"ইমপোর্ট সম্পন্ন: {created_count}টি নতুন তৈরি, {updated_count}টি হালনাগাদ, {skipped_count}টি স্কিপ করা হয়েছে।",
        }
