"""
backend/apps/core/party_data_privacy_service.py
Architecture Decision: ADR-0279 (P279).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service governing Party Data Privacy, Statutory Retention Policies,
Demographic Corrections, Irreversible Anonymization, and Restricted Subject Exports.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.customer_search_merge_service import normalize_bd_phone
from apps.core.models import (
    Branch,
    CustomerDueRecord,
    DueStatus,
    PartyPrivacyActionType,
    PartyPrivacyRetentionAudit,
    PartyPrivacyStatus,
    PosCustomer,
    PrescriptionRecord,
    SaleInvoice,
    Supplier,
    SupplierLedgerEntry,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")

# Roles authorized for privacy assessments and demographic rectifications
PRIVACY_OFFICER_ROLES = {
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

# Roles strictly restricted for clinical patient-adjacent exports
CLINICAL_EXPORT_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

# Counter staff strictly denied privacy endpoints
STAFF_DENIED_ROLES = {
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    "CASHIER",
    "STAFF_PHARMACIST",
    "INVENTORY_CLERK",
    "DISPENSER",
}


def compute_sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class PartyDataPrivacyService:
    """
    Authoritative domain service for Party Data Privacy, Statutory Retention Assessments,
    Audited Demographic Corrections, Anonymization, and Restricted Dossier Exports.
    """

    @classmethod
    def assert_authorized_officer(cls, actor_user: Any) -> None:
        """Enforces role-based authority for privacy operations."""
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_UNAUTHENTICATED",
                    "detail": "Authentication required for party data privacy operations.",
                }
            )
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in PRIVACY_OFFICER_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and staff pharmacist are not authorized to perform party privacy operations.",
                    "message_en": "Cashier and staff pharmacist are not authorized to perform party privacy operations.",
                    "message_bn": "ক্যাশিয়ার বা স্টাফ ফার্মাসিস্ট পার্টি ডাটা প্রাইভেসি বা রিটেনশন পরিচালনা করার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def assert_clinical_export_authorized(cls, actor_user: Any) -> None:
        """Restricts patient-adjacent clinical dossier export to pharmacy owner or superadmin."""
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in CLINICAL_EXPORT_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CLINICAL_EXPORT_DENIED",
                    "detail": "Export of patient-adjacent clinical prescription dossier requires Pharmacy Owner authorization.",
                    "message_en": "Export of patient-adjacent clinical prescription dossier requires Pharmacy Owner authorization.",
                    "message_bn": "রোগীর প্রেসক্রিপশন ও ক্লিনিক্যাল তথ্য এক্সপোর্ট করার জন্য ফার্মেসি ওনার অনুমোদন আবশ্যক।",
                }
            )

    @classmethod
    def assert_tenant_active(cls, tenant_id: str) -> Tenant:
        """Validates tenant existence and subscription status."""
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
                    "detail": "Tenant account subscription is suspended. Party data privacy operations blocked.",
                    "message_en": "Tenant account subscription is suspended. Party data privacy operations blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। ডাটা প্রাইভেসি অপারেশন বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    # -------------------------------------------------------------------------
    # 1. RETENTION POLICY ASSESSMENT
    # -------------------------------------------------------------------------

    @classmethod
    def assess_retention_policy(
        cls,
        *,
        actor_user: Any,
        tenant_id: str,
        party_type: str,
        party_id: str,
    ) -> Dict[str, Any]:
        """
        Assesses statutory retention schedule, active NBR/DGDA conflicts,
        and outstanding receivable/payable balances.
        """
        cls.assert_authorized_officer(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_type = str(party_type or "CUSTOMER").strip().upper()
        now = timezone.now()
        two_years_ago = now - timezone.timedelta(days=730)
        five_years_ago = now - timezone.timedelta(days=1825)

        statutory_hold_active = False
        statutory_authority = ""
        conflicts = []
        can_erase = True

        if clean_type == "CUSTOMER":
            customer = PosCustomer.objects.filter(tenant=tenant, customer_id=party_id).first()
            if not customer:
                # Check cross tenant
                foreign = PosCustomer.objects.filter(customer_id=party_id).first()
                if foreign and str(foreign.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
                raise NotFound({"error_code": "ERR_CUSTOMER_NOT_FOUND", "detail": f"Customer '{party_id}' not found."})

            # Check open due balance
            open_dues = CustomerDueRecord.objects.filter(
                tenant=tenant,
                customer=customer,
                status__in=[DueStatus.OUTSTANDING, DueStatus.PARTIAL],
                remaining_due_bdt__gt=DECIMAL_ZERO,
            )
            total_open_due = open_dues.aggregate(tot=Sum("remaining_due_bdt"))["tot"] or DECIMAL_ZERO

            if total_open_due > DECIMAL_ZERO or customer.current_due_bdt > DECIMAL_ZERO:
                can_erase = False
                statutory_hold_active = True
                statutory_authority = "Active Accounts Receivable Credit Due"
                conflicts.append(
                    f"Active outstanding due balance ({total_open_due} BDT) exists. Erasure or anonymization is forbidden until due settlement."
                )

            # Check statutory prescriptions (2-year DGDA hold)
            rx_count = PrescriptionRecord.objects.filter(
                tenant=tenant,
                customer=customer,
                created_at__gte=two_years_ago,
            ).count()
            if rx_count > 0:
                can_erase = False
                statutory_hold_active = True
                statutory_authority = "DGDA Dispensing Rules (2-Year Clinical Rx Retention)"
                conflicts.append(
                    f"{rx_count} clinical prescription(s) under active DGDA mandatory 2-year retention. Physical erasure prohibited; anonymization only."
                )

            # Check sales invoices (5-year NBR VAT hold)
            inv_count = SaleInvoice.objects.filter(
                tenant=tenant,
                customer_phone=customer.phone,
                created_at__gte=five_years_ago,
            ).count()
            if inv_count > 0:
                can_erase = False
                statutory_hold_active = True
                statutory_authority = "NBR VAT & Tax Regulations (5-Year Ledger Retention)"
                conflicts.append(
                    f"{inv_count} sales invoice(s) under active NBR 5-year tax audit retention."
                )

            retention_status = PartyPrivacyStatus.STATUTORY_HOLD if statutory_hold_active else PartyPrivacyStatus.APPROVED

            audit = PartyPrivacyRetentionAudit.objects.create(
                tenant=tenant,
                party_type="CUSTOMER",
                party_id=customer.customer_id,
                action_type=PartyPrivacyActionType.RETENTION_ASSESSMENT,
                status=retention_status,
                statutory_hold_active=statutory_hold_active,
                statutory_authority=statutory_authority,
                reason="Routine statutory retention assessment",
                performed_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
                sha256_hash=compute_sha256(f"{customer.customer_id}|{statutory_hold_active}|{now.isoformat()}"),
            )

            return {
                "party_type": "CUSTOMER",
                "party_id": customer.customer_id,
                "party_name": customer.full_name,
                "can_physically_erase": can_erase,
                "statutory_hold_active": statutory_hold_active,
                "statutory_authority": statutory_authority,
                "conflicts_detected": conflicts,
                "open_due_balance_bdt": str(total_open_due),
                "assessment_audit_id": audit.audit_id,
                "assessed_at": now.isoformat(),
            }

        elif clean_type in ("SUPPLIER", "VENDOR"):
            supplier = Supplier.objects.filter(tenant=tenant, supplier_id=party_id).first()
            if not supplier:
                foreign = Supplier.objects.filter(supplier_id=party_id).first()
                if foreign and str(foreign.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
                raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{party_id}' not found."})

            if supplier.current_balance != DECIMAL_ZERO:
                can_erase = False
                statutory_hold_active = True
                statutory_authority = "Active Accounts Payable Liability"
                conflicts.append(
                    f"Active payable balance ({supplier.current_balance} BDT) exists. Erasure is forbidden until settlement."
                )

            ledger_count = SupplierLedgerEntry.objects.filter(
                tenant=tenant,
                supplier=supplier,
                posted_at__gte=five_years_ago,
            ).count()
            if ledger_count > 0:
                can_erase = False
                statutory_hold_active = True
                statutory_authority = "NBR Tax & Commercial Code (5-Year Ledger Retention)"
                conflicts.append(f"{ledger_count} ledger transaction(s) under NBR 5-year retention.")

            retention_status = PartyPrivacyStatus.STATUTORY_HOLD if statutory_hold_active else PartyPrivacyStatus.APPROVED

            audit = PartyPrivacyRetentionAudit.objects.create(
                tenant=tenant,
                party_type="SUPPLIER",
                party_id=supplier.supplier_id,
                action_type=PartyPrivacyActionType.RETENTION_ASSESSMENT,
                status=retention_status,
                statutory_hold_active=statutory_hold_active,
                statutory_authority=statutory_authority,
                reason="Routine supplier statutory retention assessment",
                performed_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
                sha256_hash=compute_sha256(f"{supplier.supplier_id}|{statutory_hold_active}|{now.isoformat()}"),
            )

            return {
                "party_type": "SUPPLIER",
                "party_id": supplier.supplier_id,
                "party_name": supplier.name,
                "can_physically_erase": can_erase,
                "statutory_hold_active": statutory_hold_active,
                "statutory_authority": statutory_authority,
                "conflicts_detected": conflicts,
                "current_payable_balance_bdt": str(supplier.current_balance),
                "assessment_audit_id": audit.audit_id,
                "assessed_at": now.isoformat(),
            }
        else:
            raise ValidationError({"error_code": "ERR_INVALID_PARTY_TYPE", "detail": f"Unknown party type '{party_type}'."})

    # -------------------------------------------------------------------------
    # 2. PARTY DATA CORRECTION (RECTIFICATION)
    # -------------------------------------------------------------------------

    @classmethod
    @transaction.atomic
    def correct_party_data(
        cls,
        *,
        actor_user: Any,
        tenant_id: str,
        party_type: str,
        party_id: str,
        corrections: Dict[str, Any],
        reason: str,
    ) -> Dict[str, Any]:
        """
        Executes an audited demographic rectification on customer or supplier data.
        """
        cls.assert_authorized_officer(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_CORRECTION_REASON",
                    "detail": "A mandatory explanation reason is required for party demographic corrections.",
                    "message_en": "A mandatory explanation reason is required for party demographic corrections.",
                    "message_bn": "তথ্য সংশোধনের সুনির্দিষ্ট কারণ উল্লেখ করা বাধ্যতামূলক।",
                }
            )

        clean_type = str(party_type or "CUSTOMER").strip().upper()

        if clean_type == "CUSTOMER":
            customer = PosCustomer.objects.select_for_update().filter(tenant=tenant, customer_id=party_id).first()
            if not customer:
                raise NotFound({"error_code": "ERR_CUSTOMER_NOT_FOUND", "detail": f"Customer '{party_id}' not found."})

            prev_payload = {
                "full_name": customer.full_name,
                "phone": customer.phone,
                "email": customer.email,
                "address": customer.address,
            }

            new_name = str(corrections.get("full_name") or customer.full_name).strip()
            if not new_name:
                raise ValidationError({"error_code": "ERR_INVALID_NAME", "detail": "Customer name cannot be empty."})

            new_phone = str(corrections.get("phone") or customer.phone).strip()
            norm = normalize_bd_phone(new_phone)
            if not norm["is_valid_bd"] and len(norm["digits"]) < 7:
                raise ValidationError({"error_code": "ERR_INVALID_PHONE", "detail": f"Invalid phone number '{new_phone}'."})

            customer.full_name = new_name
            customer.phone = norm["e164"] if norm["is_valid_bd"] else new_phone
            if "email" in corrections:
                customer.email = str(corrections.get("email") or "").strip()
            if "address" in corrections:
                customer.address = str(corrections.get("address") or "").strip()
            customer.save()

            audit = PartyPrivacyRetentionAudit.objects.create(
                tenant=tenant,
                branch=customer.primary_branch,
                party_type="CUSTOMER",
                party_id=customer.customer_id,
                action_type=PartyPrivacyActionType.CORRECTION,
                status=PartyPrivacyStatus.COMPLETED,
                reason=clean_reason,
                previous_payload_masked=prev_payload,
                corrected_payload={
                    "full_name": customer.full_name,
                    "phone": customer.phone,
                    "email": customer.email,
                    "address": customer.address,
                },
                performed_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
                sha256_hash=compute_sha256(f"{customer.customer_id}|{json.dumps(prev_payload)}|{customer.phone}"),
            )

            AuditLogEntry.objects.create(
                request_id=f"req-{uuid.uuid4().hex[:8]}",
                tenant_id=str(tenant.tenant_id),
                user_id=getattr(actor_user, "user_id", str(actor_user)),
                actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
                actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
                action="PARTY_DATA_CORRECTION",
                resource_type="PosCustomer",
                resource_id=customer.customer_id,
                ip_address="127.0.0.1",
                status=AuditActionStatus.SUCCESS,
                metadata_json={"audit_id": audit.audit_id, "reason": clean_reason},
            )

            return {
                "success": True,
                "party_type": "CUSTOMER",
                "customer_id": customer.customer_id,
                "full_name": customer.full_name,
                "phone": customer.phone,
                "email": customer.email,
                "address": customer.address,
                "audit_id": audit.audit_id,
                "message_en": "Customer demographic details successfully rectified and audited.",
                "message_bn": "গ্রাহকের তথ্য সফলভাবে সংশোধন এবং অডিট করা হয়েছে।",
            }

        elif clean_type in ("SUPPLIER", "VENDOR"):
            supplier = Supplier.objects.select_for_update().filter(tenant=tenant, supplier_id=party_id).first()
            if not supplier:
                raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{party_id}' not found."})

            prev_payload = {
                "name": supplier.name,
                "contact_person": supplier.contact_person,
                "phone": supplier.phone,
                "email": supplier.email,
                "address": supplier.address,
            }

            if corrections.get("name"):
                supplier.name = str(corrections.get("name")).strip()
            if corrections.get("contact_person"):
                supplier.contact_person = str(corrections.get("contact_person")).strip()
            if corrections.get("phone"):
                supplier.phone = str(corrections.get("phone")).strip()
            if corrections.get("email"):
                supplier.email = str(corrections.get("email")).strip()
            if corrections.get("address"):
                supplier.address = str(corrections.get("address")).strip()
            supplier.save()

            audit = PartyPrivacyRetentionAudit.objects.create(
                tenant=tenant,
                party_type="SUPPLIER",
                party_id=supplier.supplier_id,
                action_type=PartyPrivacyActionType.CORRECTION,
                status=PartyPrivacyStatus.COMPLETED,
                reason=clean_reason,
                previous_payload_masked=prev_payload,
                corrected_payload={
                    "name": supplier.name,
                    "contact_person": supplier.contact_person,
                    "phone": supplier.phone,
                    "email": supplier.email,
                    "address": supplier.address,
                },
                performed_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
                sha256_hash=compute_sha256(f"{supplier.supplier_id}|{supplier.name}|{supplier.phone}"),
            )

            return {
                "success": True,
                "party_type": "SUPPLIER",
                "supplier_id": supplier.supplier_id,
                "name": supplier.name,
                "contact_person": supplier.contact_person,
                "phone": supplier.phone,
                "email": supplier.email,
                "audit_id": audit.audit_id,
                "message_en": "Supplier organization details rectified and audited.",
                "message_bn": "সরবরাহকারী প্রতিষ্ঠানের তথ্য সংশোধন ও অডিট সম্পন্ন হয়েছে।",
            }
        else:
            raise ValidationError({"error_code": "ERR_INVALID_PARTY_TYPE", "detail": f"Unknown party type '{party_type}'."})

    # -------------------------------------------------------------------------
    # 3. AUDITED IRREVERSIBLE ANONYMIZATION
    # -------------------------------------------------------------------------

    @classmethod
    @transaction.atomic
    def anonymize_party_data(
        cls,
        *,
        actor_user: Any,
        tenant_id: str,
        party_type: str,
        party_id: str,
        reason: str,
    ) -> Dict[str, Any]:
        """
        Executes an audited PII anonymization under statutory retention hold.
        Physical row erasure is blocked if statutory retention applies, but PII is replaced
        with irreversible pseudonymized tokens while keeping financial ledger links intact.
        """
        cls.assert_authorized_officer(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError({"error_code": "ERR_MISSING_ANONYMIZATION_REASON", "detail": "Reason is required for anonymization."})

        assessment = cls.assess_retention_policy(
            actor_user=actor_user,
            tenant_id=tenant_id,
            party_type=party_type,
            party_id=party_id,
        )

        clean_type = str(party_type or "CUSTOMER").strip().upper()

        if clean_type == "CUSTOMER":
            # If open due exists, anonymization is forbidden until settled
            if Decimal(str(assessment.get("open_due_balance_bdt", "0.00"))) > DECIMAL_ZERO:
                raise ValidationError(
                    {
                        "error_code": "ERR_OPEN_DUE_BALANCE_HOLD",
                        "detail": "Cannot anonymize customer with active outstanding receivable due balance. Settle or write off due first.",
                        "message_en": "Cannot anonymize customer with active outstanding receivable due balance.",
                        "message_bn": "বকেয়া বাকি থাকা অবস্থায় কাস্টমার ডাটা অ্যানোনিমাইজ করা নিষিদ্ধ।",
                    }
                )

            customer = PosCustomer.objects.select_for_update().filter(tenant=tenant, customer_id=party_id).first()
            if not customer:
                raise NotFound({"error_code": "ERR_CUSTOMER_NOT_FOUND", "detail": f"Customer '{party_id}' not found."})

            pseudo_id = uuid.uuid4().hex[:8].upper()
            orig_name = customer.full_name
            customer.full_name = f"Anonymized Customer {pseudo_id}"
            customer.phone = f"+8800{uuid.uuid4().hex[:8]}"
            customer.email = ""
            customer.address = "[ANONYMIZED_UNDER_STATUTORY_HOLD]"
            customer.is_active = False
            customer.is_archived = True
            customer.save()

            audit = PartyPrivacyRetentionAudit.objects.create(
                tenant=tenant,
                branch=customer.primary_branch,
                party_type="CUSTOMER",
                party_id=customer.customer_id,
                action_type=PartyPrivacyActionType.ANONYMIZATION,
                status=PartyPrivacyStatus.COMPLETED,
                statutory_hold_active=assessment.get("statutory_hold_active", False),
                statutory_authority=assessment.get("statutory_authority", ""),
                reason=clean_reason,
                previous_payload_masked={"orig_name": orig_name},
                corrected_payload={"pseudo_name": customer.full_name},
                performed_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
                sha256_hash=compute_sha256(f"{customer.customer_id}|ANONYMIZED|{clean_reason}"),
            )

            AuditLogEntry.objects.create(
                request_id=f"req-{uuid.uuid4().hex[:8]}",
                tenant_id=str(tenant.tenant_id),
                user_id=getattr(actor_user, "user_id", str(actor_user)),
                actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
                actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
                action="PARTY_ANONYMIZATION",
                resource_type="PosCustomer",
                resource_id=customer.customer_id,
                ip_address="127.0.0.1",
                status=AuditActionStatus.SUCCESS,
                metadata_json={"audit_id": audit.audit_id, "reason": clean_reason},
            )

            return {
                "success": True,
                "party_type": "CUSTOMER",
                "customer_id": customer.customer_id,
                "anonymized_name": customer.full_name,
                "is_anonymized": True,
                "audit_id": audit.audit_id,
                "message_en": "Customer PII permanently anonymized under statutory retention hold.",
                "message_bn": "গ্রাহকের ব্যক্তিগত তথ্য সংবিধিবদ্ধ নিয়ম মেনে স্থায়ীভাবে অ্যানোনিমাইজ করা হয়েছে।",
            }

        elif clean_type in ("SUPPLIER", "VENDOR"):
            supplier = Supplier.objects.select_for_update().filter(tenant=tenant, supplier_id=party_id).first()
            if not supplier:
                raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{party_id}' not found."})

            if supplier.current_balance != DECIMAL_ZERO:
                raise ValidationError(
                    {
                        "error_code": "ERR_OPEN_PAYABLE_BALANCE_HOLD",
                        "detail": f"Cannot anonymize supplier with outstanding payable balance ({supplier.current_balance} BDT).",
                    }
                )

            pseudo_id = uuid.uuid4().hex[:8].upper()
            supplier.name = f"Anonymized Supplier {pseudo_id}"
            supplier.contact_person = "[ANONYMIZED]"
            supplier.phone = "+880000000000"
            supplier.email = ""
            supplier.bank_account_no = ""
            supplier.is_active = False
            supplier.save()

            audit = PartyPrivacyRetentionAudit.objects.create(
                tenant=tenant,
                party_type="SUPPLIER",
                party_id=supplier.supplier_id,
                action_type=PartyPrivacyActionType.ANONYMIZATION,
                status=PartyPrivacyStatus.COMPLETED,
                statutory_hold_active=assessment.get("statutory_hold_active", False),
                statutory_authority=assessment.get("statutory_authority", ""),
                reason=clean_reason,
                performed_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
                sha256_hash=compute_sha256(f"{supplier.supplier_id}|ANONYMIZED|{clean_reason}"),
            )

            return {
                "success": True,
                "party_type": "SUPPLIER",
                "supplier_id": supplier.supplier_id,
                "anonymized_name": supplier.name,
                "is_anonymized": True,
                "audit_id": audit.audit_id,
                "message_en": "Supplier details permanently anonymized.",
                "message_bn": "সরবরাহকারী প্রতিষ্ঠানের তথ্য স্থায়ীভাবে অ্যানোনিমাইজ করা হয়েছে।",
            }
        else:
            raise ValidationError({"error_code": "ERR_INVALID_PARTY_TYPE", "detail": f"Unknown party type '{party_type}'."})

    # -------------------------------------------------------------------------
    # 4. RESTRICTED SUBJECT DATA DOSSIER EXPORT
    # -------------------------------------------------------------------------

    @classmethod
    def export_restricted_dossier(
        cls,
        *,
        actor_user: Any,
        tenant_id: str,
        party_type: str,
        party_id: str,
        classification: str = "STANDARD",
        reason: str = "",
    ) -> Dict[str, Any]:
        """
        Exports complete subject data bundle.
        RESTRICTED_CLINICAL requires Pharmacy Owner or Superadmin authorization.
        """
        cls.assert_authorized_officer(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_class = str(classification or "STANDARD").strip().upper()
        if clean_class == "RESTRICTED_CLINICAL":
            cls.assert_clinical_export_authorized(actor_user)

        clean_type = str(party_type or "CUSTOMER").strip().upper()

        if clean_type == "CUSTOMER":
            customer = PosCustomer.objects.filter(tenant=tenant, customer_id=party_id).first()
            if not customer:
                raise NotFound({"error_code": "ERR_CUSTOMER_NOT_FOUND", "detail": f"Customer '{party_id}' not found."})

            profile = {
                "customer_id": customer.customer_id,
                "full_name": customer.full_name,
                "phone": customer.phone,
                "email": customer.email,
                "address": customer.address,
                "credit_limit_bdt": str(customer.credit_limit_bdt),
                "current_due_bdt": str(customer.current_due_bdt),
            }

            dues = [
                {
                    "due_id": d.due_id,
                    "total_due_bdt": str(d.total_due_bdt),
                    "remaining_due_bdt": str(d.remaining_due_bdt),
                    "status": d.status,
                }
                for d in CustomerDueRecord.objects.filter(tenant=tenant, customer=customer)
            ]

            clinical_history = []
            if clean_class == "RESTRICTED_CLINICAL":
                clinical_history = [
                    {
                        "prescription_id": rx.prescription_id,
                        "doctor_name": rx.doctor_name,
                        "bmdc_number": getattr(rx, "doctor_bmdc_no", getattr(rx, "bmdc_number", "")),
                        "status": rx.status,
                        "created_at": rx.created_at.isoformat(),
                    }
                    for rx in PrescriptionRecord.objects.filter(tenant=tenant, customer=customer)
                ]

            audit = PartyPrivacyRetentionAudit.objects.create(
                tenant=tenant,
                branch=customer.primary_branch,
                party_type="CUSTOMER",
                party_id=customer.customer_id,
                action_type=PartyPrivacyActionType.RESTRICTED_EXPORT,
                status=PartyPrivacyStatus.COMPLETED,
                reason=reason or f"Restricted export classification: {clean_class}",
                performed_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
                sha256_hash=compute_sha256(f"{customer.customer_id}|{clean_class}|{timezone.now().isoformat()}"),
            )

            return {
                "party_type": "CUSTOMER",
                "classification": clean_class,
                "profile": profile,
                "due_records": dues,
                "clinical_prescriptions": clinical_history,
                "audit_id": audit.audit_id,
                "exported_at": timezone.now().isoformat(),
            }
        else:
            raise ValidationError({"error_code": "ERR_INVALID_PARTY_TYPE", "detail": "Restricted dossier export supported for customers."})
