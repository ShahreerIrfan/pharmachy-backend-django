"""
backend/apps/core/supplier_organization_service.py
Architecture Decision: ADR-0181 (P181).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Service layer governing tenant-scoped supplier organizations with legal identity,
DGDA drug licensing, NBR e-TIN / 13-digit BIN tax references, payment terms,
credit limits, accounts payable (AP) ledger postings, and proof of origin verification.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    PreferredCommunicationMethod,
    Supplier,
    SupplierContactPerson,
    SupplierContactPersonRole,
    SupplierLedgerEntry,
    SupplierPostingType,
    SupplierRegion,
    SupplierStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

ALLOWED_PROCUREMENT_ROLES = {
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    UserRole.STAFF_PHARMACIST,
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "STAFF_PHARMACIST",
    "OWNER_ADMIN",
    "ADMIN",
    "PROCUREMENT_OFFICER",
    "AUDITOR",
}


class SupplierOrganizationService:
    """
    Authoritative service governing supplier organizations, legal credentials,
    accounts payable ledger tracking, and authentic origin verification.
    """

    @classmethod
    def _assert_procurement_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if role in [UserRole.CASHIER, "CASHIER"] or (role not in ALLOWED_PROCUREMENT_ROLES and not getattr(actor_user, "is_superuser", False)):
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and unauthorized staff are not permitted to manage supplier organizations.",
                    "message_en": "Cashier and unauthorized staff are not permitted to manage supplier organizations.",
                    "message_bn": "ক্যাশিয়ার বা অননুমোদিত কর্মী সরবরাহকারী প্রতিষ্ঠান ব্যবস্থাপনার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant_id: str) -> Tenant:
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound(
                {
                    "error_code": "ERR_TENANT_NOT_FOUND",
                    "detail": f"Tenant '{tenant_id}' not found.",
                }
            )
        if (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant account subscription is suspended. Procurement access blocked.",
                    "message_en": "Tenant account subscription is suspended. Procurement access blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। সরবরাহকারী মডিউল বন্ধ।",
                }
            )
        return tenant

    @classmethod
    def serialize_supplier(cls, s: Supplier) -> Dict[str, Any]:
        """Serializes a SupplierOrganization conforming to standard JSON contract schema."""
        return {
            "supplier_id": s.supplier_id,
            "tenant_id": str(s.tenant_id),
            "name": s.name,
            "name_bn": s.name_bn or "",
            "legal_name": s.legal_name or "",
            "code": s.code or "",
            "contact_person": s.contact_person or "",
            "phone": s.phone or "",
            "email": s.email or "",
            "address": s.address or "",
            "region": s.region,
            "district": s.district or "",
            "dl_number": s.dl_number or "",
            "tax_id": s.tax_id or "",
            "tin_number": s.tin_number or "",
            "bin_number": s.bin_number or "",
            "trade_license_number": s.trade_license_number or "",
            "status": s.status,
            "payment_terms_days": s.payment_terms_days,
            "credit_limit": str(s.credit_limit),
            "current_balance": str(s.current_balance),
            "bank_name": s.bank_name or "",
            "bank_account_name": s.bank_account_name or "",
            "bank_account_no": s.bank_account_no or "",
            "bank_branch": s.bank_branch or "",
            "bank_routing_number": s.bank_routing_number or "",
            "lead_time_days": s.lead_time_days,
            "proof_of_origin_verified": s.proof_of_origin_verified,
            "proof_of_origin_notes": s.proof_of_origin_notes or "",
            "notes": s.notes or "",
            "is_active": s.is_active,
            "merged_into_id": s.merged_into_id,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }

    @classmethod
    def serialize_ledger_entry(cls, entry: SupplierLedgerEntry) -> Dict[str, Any]:
        """Serializes an immutable accounts payable ledger posting."""
        return {
            "posting_id": entry.posting_id,
            "tenant_id": str(entry.tenant_id),
            "supplier_id": str(entry.supplier_id),
            "supplier_name": entry.supplier.name if entry.supplier else "",
            "branch_id": str(entry.branch_id) if entry.branch_id else None,
            "posting_type": entry.posting_type,
            "reference_number": entry.reference_number,
            "debit_amount": str(entry.debit_amount),
            "credit_amount": str(entry.credit_amount),
            "balance_after": str(entry.balance_after),
            "currency": entry.currency,
            "notes": entry.notes or "",
            "posted_by_id": str(entry.posted_by_id) if entry.posted_by_id else None,
            "posted_at": entry.posted_at.isoformat() if entry.posted_at else None,
        }

    @classmethod
    def list_suppliers(
        cls,
        tenant_id: str,
        search: Optional[str] = None,
        region: Optional[str] = None,
        status: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        cls._assert_tenant_active(tenant_id)
        qs = Supplier.objects.filter(tenant_id=tenant_id)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if status:
            qs = qs.filter(status=status.upper())
        if region:
            qs = qs.filter(region=region.upper())
        if search:
            q = search.strip()
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(name_bn__icontains=q)
                | Q(legal_name__icontains=q)
                | Q(code__icontains=q)
                | Q(contact_person__icontains=q)
                | Q(phone__icontains=q)
                | Q(dl_number__icontains=q)
                | Q(bin_number__icontains=q)
                | Q(tin_number__icontains=q)
            )
        return [cls.serialize_supplier(s) for s in qs.order_by("name")]

    @classmethod
    def get_supplier(cls, tenant_id: str, supplier_id: str) -> Dict[str, Any]:
        cls._assert_tenant_active(tenant_id)
        s = Supplier.objects.filter(supplier_id=supplier_id).first()
        if not s:
            raise NotFound(
                {
                    "error_code": "ERR_SUPPLIER_NOT_FOUND",
                    "detail": f"Supplier '{supplier_id}' not found.",
                }
            )
        if str(s.tenant_id) != str(tenant_id):
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "detail": "Cross-tenant supplier access is strictly forbidden.",
                }
            )
        return cls.serialize_supplier(s)

    @classmethod
    def create_supplier(cls, tenant_id: str, actor_user: Any, data: Dict[str, Any]) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        # Cross-tenant check for actor
        user_tenant_id = getattr(actor_user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(actor_user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "detail": "Cross-tenant supplier registration is strictly forbidden.",
                }
            )

        name = data.get("name", "").strip()
        if not name:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_SUPPLIER_NAME",
                    "detail": "Supplier name is required.",
                }
            )

        if Supplier.objects.filter(tenant=tenant, name__iexact=name, is_active=True).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_SUPPLIER_NAME",
                    "detail": f"Active supplier with name '{name}' already exists for this pharmacy.",
                }
            )

        # Credit limit parsing
        credit_limit_raw = data.get("credit_limit", "0.0000")
        try:
            credit_limit = Decimal(str(credit_limit_raw))
            if credit_limit < Decimal("0.0000"):
                raise ValidationError({"error_code": "ERR_INVALID_CREDIT_LIMIT", "detail": "Credit limit cannot be negative."})
        except Exception as e:
            if isinstance(e, ValidationError):
                raise
            raise ValidationError({"error_code": "ERR_INVALID_CREDIT_LIMIT", "detail": "Invalid credit limit value."})

        # Payment terms
        payment_terms_days = int(data.get("payment_terms_days", 30))
        if payment_terms_days < 0:
            raise ValidationError({"error_code": "ERR_INVALID_PAYMENT_TERMS", "detail": "Payment terms cannot be negative."})

        # Region validation
        region = data.get("region", SupplierRegion.DHAKA).upper()
        if region not in SupplierRegion.values:
            region = SupplierRegion.OTHER

        # Status validation
        status_val = data.get("status", SupplierStatus.ACTIVE).upper()
        if status_val not in SupplierStatus.values:
            status_val = SupplierStatus.ACTIVE

        supplier_id = data.get("supplier_id") or f"sup-{uuid.uuid4().hex[:12]}"
        if not supplier_id.startswith("sup-"):
            supplier_id = f"sup-{supplier_id}"

        supplier = Supplier.objects.create(
            supplier_id=supplier_id,
            tenant=tenant,
            name=name,
            name_bn=data.get("name_bn", "").strip(),
            legal_name=data.get("legal_name", "").strip() or name,
            code=data.get("code", "").strip(),
            contact_person=data.get("contact_person", "").strip(),
            phone=data.get("phone", "").strip(),
            email=data.get("email", "").strip(),
            address=data.get("address", "").strip(),
            region=region,
            district=data.get("district", "").strip(),
            dl_number=data.get("dl_number", "").strip(),
            tax_id=data.get("tax_id", "").strip(),
            tin_number=data.get("tin_number", "").strip(),
            bin_number=data.get("bin_number", "").strip(),
            trade_license_number=data.get("trade_license_number", "").strip(),
            status=status_val,
            payment_terms_days=payment_terms_days,
            credit_limit=credit_limit,
            current_balance=Decimal("0.0000"),
            bank_name=data.get("bank_name", "").strip(),
            bank_account_name=data.get("bank_account_name", "").strip(),
            bank_account_no=data.get("bank_account_no", "").strip(),
            bank_branch=data.get("bank_branch", "").strip(),
            bank_routing_number=data.get("bank_routing_number", "").strip(),
            lead_time_days=int(data.get("lead_time_days", 2)),
            proof_of_origin_verified=bool(data.get("proof_of_origin_verified", False)),
            proof_of_origin_notes=data.get("proof_of_origin_notes", "").strip(),
            notes=data.get("notes", "").strip(),
            is_active=(status_val == SupplierStatus.ACTIVE),
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SUPPLIER_ORGANIZATION_CREATED",
            resource_type="SupplierOrganization",
            resource_id=supplier.supplier_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "supplier_id": supplier.supplier_id,
                "name": supplier.name,
                "bin_number": supplier.bin_number,
                "credit_limit": str(supplier.credit_limit),
            },
        )

        return cls.serialize_supplier(supplier)

    @classmethod
    def update_supplier(
        cls,
        tenant_id: str,
        actor_user: Any,
        supplier_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        supplier = Supplier.objects.filter(supplier_id=supplier_id).first()
        if not supplier:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})
        if str(supplier.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant supplier update forbidden."})

        # Name update check
        if "name" in data:
            new_name = data["name"].strip()
            if not new_name:
                raise ValidationError({"error_code": "ERR_MISSING_SUPPLIER_NAME", "detail": "Supplier name cannot be empty."})
            if (
                Supplier.objects.filter(tenant=tenant, name__iexact=new_name, is_active=True)
                .exclude(supplier_id=supplier.supplier_id)
                .exists()
            ):
                raise ValidationError({"error_code": "ERR_DUPLICATE_SUPPLIER_NAME", "detail": f"Name '{new_name}' in use."})
            supplier.name = new_name

        if "legal_name" in data:
            supplier.legal_name = str(data["legal_name"]).strip()
        if "name_bn" in data:
            supplier.name_bn = str(data["name_bn"]).strip()
        if "contact_person" in data:
            supplier.contact_person = str(data["contact_person"]).strip()
        if "phone" in data:
            supplier.phone = str(data["phone"]).strip()
        if "email" in data:
            supplier.email = str(data["email"]).strip()
        if "address" in data:
            supplier.address = str(data["address"]).strip()
        if "district" in data:
            supplier.district = str(data["district"]).strip()
        if "region" in data:
            reg = str(data["region"]).upper()
            if reg in SupplierRegion.values:
                supplier.region = reg
        if "dl_number" in data:
            supplier.dl_number = str(data["dl_number"]).strip()
        if "tin_number" in data:
            supplier.tin_number = str(data["tin_number"]).strip()
        if "bin_number" in data:
            supplier.bin_number = str(data["bin_number"]).strip()
        if "trade_license_number" in data:
            supplier.trade_license_number = str(data["trade_license_number"]).strip()
        if "payment_terms_days" in data:
            supplier.payment_terms_days = max(0, int(data["payment_terms_days"]))
        if "credit_limit" in data:
            supplier.credit_limit = Decimal(str(data["credit_limit"]))
        if "bank_name" in data:
            supplier.bank_name = str(data["bank_name"]).strip()
        if "bank_account_name" in data:
            supplier.bank_account_name = str(data["bank_account_name"]).strip()
        if "bank_account_no" in data:
            supplier.bank_account_no = str(data["bank_account_no"]).strip()
        if "bank_branch" in data:
            supplier.bank_branch = str(data["bank_branch"]).strip()
        if "bank_routing_number" in data:
            supplier.bank_routing_number = str(data["bank_routing_number"]).strip()
        if "lead_time_days" in data:
            supplier.lead_time_days = max(0, int(data["lead_time_days"]))
        if "notes" in data:
            supplier.notes = str(data["notes"]).strip()
        if "status" in data:
            st = str(data["status"]).upper()
            if st in SupplierStatus.values:
                supplier.status = st
                supplier.is_active = (st == SupplierStatus.ACTIVE)

        supplier.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SUPPLIER_ORGANIZATION_UPDATED",
            resource_type="SupplierOrganization",
            resource_id=supplier.supplier_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"supplier_id": supplier.supplier_id, "updated_fields": list(data.keys())},
        )

        return cls.serialize_supplier(supplier)

    @classmethod
    def archive_or_delete_supplier(cls, tenant_id: str, actor_user: Any, supplier_id: str) -> Dict[str, Any]:
        """Soft-deletes / archives a supplier organization, guarded against outstanding AP balance."""
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        supplier = Supplier.objects.filter(supplier_id=supplier_id).first()
        if not supplier:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})
        if str(supplier.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant supplier update forbidden."})

        # Guard: Outstanding balance
        if supplier.current_balance > Decimal("0.0000"):
            raise ValidationError(
                {
                    "error_code": "ERR_SUPPLIER_OUTSTANDING_BALANCE",
                    "detail": f"Cannot archive supplier with outstanding accounts payable balance of {supplier.current_balance} BDT.",
                    "message_en": f"Cannot archive supplier with outstanding accounts payable balance of {supplier.current_balance} BDT.",
                    "message_bn": f"বকেয়া {supplier.current_balance} টাকা পরিশোধ না করে সরবরাহকারী আর্কাইভ করা যাবে না।",
                }
            )

        supplier.status = SupplierStatus.INACTIVE
        supplier.is_active = False
        supplier.save(update_fields=["status", "is_active", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SUPPLIER_ORGANIZATION_ARCHIVED",
            resource_type="SupplierOrganization",
            resource_id=supplier.supplier_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"supplier_id": supplier.supplier_id, "action": "ARCHIVED"},
        )

        return cls.serialize_supplier(supplier)

    @classmethod
    def post_ap_ledger_entry(
        cls,
        tenant_id: str,
        actor_user: Any,
        supplier_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Posts an atomic, immutable accounts payable ledger entry with exact Decimal(14, 4) precision.
        Updates supplier's current_balance.
        """
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        posting_type = data.get("posting_type", SupplierPostingType.GRN_INVOICE).upper()
        if posting_type not in SupplierPostingType.values:
            raise ValidationError({"error_code": "ERR_INVALID_POSTING_TYPE", "detail": f"Invalid posting type '{posting_type}'."})

        ref_number = data.get("reference_number", "").strip()
        if not ref_number:
            raise ValidationError({"error_code": "ERR_MISSING_REFERENCE_NUMBER", "detail": "Reference number (PO/GRN/Voucher) is required."})

        debit = Decimal(str(data.get("debit_amount", "0.0000")))
        credit = Decimal(str(data.get("credit_amount", "0.0000")))

        if debit < Decimal("0.0000") or credit < Decimal("0.0000"):
            raise ValidationError({"error_code": "ERR_INVALID_AMOUNT", "detail": "Debit and credit amounts cannot be negative."})

        branch_id = data.get("branch_id")
        branch = None
        if branch_id:
            branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()

        with transaction.atomic():
            supplier = Supplier.objects.select_for_update().filter(supplier_id=supplier_id).first()
            if not supplier:
                raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})
            if str(supplier.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant supplier posting forbidden."})

            if supplier.status == SupplierStatus.BLACKLISTED:
                raise PermissionDenied({"error_code": "ERR_SUPPLIER_BLACKLISTED", "detail": "Transactions barred for blacklisted suppliers."})

            # Calculate balance change:
            # Credit increases payable liability owed to supplier (e.g. GRN receipt of inventory).
            # Debit decreases payable liability (e.g. payment voucher or purchase return debit note).
            balance_delta = credit - debit
            new_balance = supplier.current_balance + balance_delta

            posting_id = f"sup-post-{uuid.uuid4().hex[:12]}"
            entry = SupplierLedgerEntry.objects.create(
                posting_id=posting_id,
                tenant=tenant,
                supplier=supplier,
                branch=branch,
                posting_type=posting_type,
                reference_number=ref_number,
                debit_amount=debit,
                credit_amount=credit,
                balance_after=new_balance,
                currency="BDT",
                notes=data.get("notes", "").strip(),
                posted_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            )

            supplier.current_balance = new_balance
            supplier.save(update_fields=["current_balance", "updated_at"])

            AuditLogEntry.objects.create(
                request_id=f"req-{uuid.uuid4().hex[:8]}",
                tenant_id=str(tenant.tenant_id),
                user_id=getattr(actor_user, "user_id", str(actor_user)),
                actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
                actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
                action="SUPPLIER_LEDGER_POSTED",
                resource_type="SupplierLedgerEntry",
                resource_id=entry.posting_id,
                ip_address="127.0.0.1",
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "supplier_id": supplier.supplier_id,
                    "posting_type": posting_type,
                    "reference_number": ref_number,
                    "balance_after": str(new_balance),
                },
            )

        return cls.serialize_ledger_entry(entry)

    @classmethod
    def list_ledger_entries(cls, tenant_id: str, supplier_id: str) -> List[Dict[str, Any]]:
        cls._assert_tenant_active(tenant_id)
        supplier = Supplier.objects.filter(supplier_id=supplier_id, tenant_id=tenant_id).first()
        if not supplier:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})
        entries = SupplierLedgerEntry.objects.filter(tenant_id=tenant_id, supplier=supplier).order_by("-posted_at")
        return [cls.serialize_ledger_entry(e) for e in entries]

    @classmethod
    def verify_proof_of_origin(
        cls,
        tenant_id: str,
        actor_user: Any,
        supplier_id: str,
        notes: str = "",
    ) -> Dict[str, Any]:
        """Certifies that supplier's manufacturer distribution authorization has been inspected."""
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        supplier = Supplier.objects.filter(supplier_id=supplier_id).first()
        if not supplier:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})
        if str(supplier.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant supplier update forbidden."})

        supplier.proof_of_origin_verified = True
        supplier.proof_of_origin_notes = notes.strip()
        supplier.save(update_fields=["proof_of_origin_verified", "proof_of_origin_notes", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SUPPLIER_ORIGIN_VERIFIED",
            resource_type="SupplierOrganization",
            resource_id=supplier.supplier_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"supplier_id": supplier.supplier_id, "verified": True},
        )

        return cls.serialize_supplier(supplier)

    # =========================================================================
    # SUPPLIER CONTACT PERSONS (P182)
    # =========================================================================

    @classmethod
    def serialize_contact_person(cls, c: SupplierContactPerson) -> Dict[str, Any]:
        """Serializes a SupplierContactPerson conforming to standard JSON contract schema."""
        return {
            "contact_id": c.contact_id,
            "tenant_id": str(c.tenant_id),
            "supplier_id": str(c.supplier_id),
            "supplier_name": c.supplier.name if c.supplier else "",
            "name": c.name,
            "name_bn": c.name_bn or "",
            "designation": c.designation or "",
            "role": c.role,
            "role_display": c.get_role_display(),
            "phone": c.phone,
            "alternate_phone": c.alternate_phone or "",
            "email": c.email or "",
            "preferred_communication_method": c.preferred_communication_method,
            "preferred_communication_method_display": c.get_preferred_communication_method_display(),
            "is_primary": c.is_primary,
            "is_active": c.is_active,
            "notes": c.notes or "",
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }

    @classmethod
    def list_supplier_contacts(
        cls,
        tenant_id: str,
        supplier_id: str,
        role: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        cls._assert_tenant_active(tenant_id)
        supplier = Supplier.objects.filter(supplier_id=supplier_id).first()
        if not supplier:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})
        if str(supplier.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant supplier contact access forbidden."})

        qs = SupplierContactPerson.objects.filter(tenant_id=tenant_id, supplier=supplier)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if role:
            qs = qs.filter(role=role.upper())

        return [cls.serialize_contact_person(c) for c in qs.order_by("-is_primary", "name")]

    @classmethod
    def get_supplier_contact(
        cls,
        tenant_id: str,
        supplier_id: str,
        contact_id: str,
    ) -> Dict[str, Any]:
        cls._assert_tenant_active(tenant_id)
        contact = SupplierContactPerson.objects.filter(contact_id=contact_id).first()
        if not contact or str(contact.supplier_id) != str(supplier_id):
            raise NotFound({"error_code": "ERR_CONTACT_NOT_FOUND", "detail": f"Contact '{contact_id}' not found for supplier."})
        if str(contact.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant contact access forbidden."})
        return cls.serialize_contact_person(contact)

    @classmethod
    def create_supplier_contact(
        cls,
        tenant_id: str,
        actor_user: Any,
        supplier_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        supplier = Supplier.objects.filter(supplier_id=supplier_id).first()
        if not supplier:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})
        if str(supplier.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant supplier contact creation forbidden."})

        name = data.get("name", "").strip()
        if len(name) < 2:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_CONTACT_NAME",
                    "detail": "Contact person name is required (minimum 2 characters).",
                }
            )

        phone = data.get("phone", "").strip()
        if not phone:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_CONTACT_PHONE",
                    "detail": "Contact person phone number is required.",
                }
            )

        role = data.get("role", SupplierContactPersonRole.SALES_REPRESENTATIVE).upper()
        if role not in SupplierContactPersonRole.values:
            role = SupplierContactPersonRole.OTHER

        pref_comm = data.get("preferred_communication_method", PreferredCommunicationMethod.PHONE).upper()
        if pref_comm not in PreferredCommunicationMethod.values:
            pref_comm = PreferredCommunicationMethod.PHONE

        is_primary = bool(data.get("is_primary", False))

        with transaction.atomic():
            if is_primary:
                # Atomic primary reassignment: unset is_primary on existing contacts for this supplier
                SupplierContactPerson.objects.filter(
                    tenant=tenant,
                    supplier=supplier,
                    is_primary=True,
                ).update(is_primary=False)

            contact_id = data.get("contact_id") or f"sup-cnt-{uuid.uuid4().hex[:12]}"
            if not contact_id.startswith("sup-cnt-"):
                contact_id = f"sup-cnt-{contact_id}"

            contact = SupplierContactPerson.objects.create(
                contact_id=contact_id,
                tenant=tenant,
                supplier=supplier,
                name=name,
                name_bn=data.get("name_bn", "").strip(),
                designation=data.get("designation", "").strip(),
                role=role,
                phone=phone,
                alternate_phone=data.get("alternate_phone", "").strip(),
                email=data.get("email", "").strip(),
                preferred_communication_method=pref_comm,
                is_primary=is_primary,
                is_active=bool(data.get("is_active", True)),
                notes=data.get("notes", "").strip(),
            )

            AuditLogEntry.objects.create(
                request_id=f"req-{uuid.uuid4().hex[:8]}",
                tenant_id=str(tenant.tenant_id),
                user_id=getattr(actor_user, "user_id", str(actor_user)),
                actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
                actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
                action="SUPPLIER_CONTACT_CREATED",
                resource_type="SupplierContactPerson",
                resource_id=contact.contact_id,
                ip_address="127.0.0.1",
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "supplier_id": supplier.supplier_id,
                    "contact_id": contact.contact_id,
                    "name": contact.name,
                    "role": contact.role,
                    "is_primary": contact.is_primary,
                },
            )

        return cls.serialize_contact_person(contact)

    @classmethod
    def update_supplier_contact(
        cls,
        tenant_id: str,
        actor_user: Any,
        supplier_id: str,
        contact_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        contact = SupplierContactPerson.objects.filter(contact_id=contact_id).first()
        if not contact or str(contact.supplier_id) != str(supplier_id):
            raise NotFound({"error_code": "ERR_CONTACT_NOT_FOUND", "detail": f"Contact '{contact_id}' not found for supplier."})
        if str(contact.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant contact update forbidden."})

        if "name" in data:
            new_name = str(data["name"]).strip()
            if len(new_name) < 2:
                raise ValidationError({"error_code": "ERR_MISSING_CONTACT_NAME", "detail": "Name must be at least 2 characters."})
            contact.name = new_name

        if "name_bn" in data:
            contact.name_bn = str(data["name_bn"]).strip()
        if "designation" in data:
            contact.designation = str(data["designation"]).strip()
        if "role" in data:
            r = str(data["role"]).upper()
            if r in SupplierContactPersonRole.values:
                contact.role = r
        if "phone" in data:
            p = str(data["phone"]).strip()
            if not p:
                raise ValidationError({"error_code": "ERR_MISSING_CONTACT_PHONE", "detail": "Phone cannot be empty."})
            contact.phone = p
        if "alternate_phone" in data:
            contact.alternate_phone = str(data["alternate_phone"]).strip()
        if "email" in data:
            contact.email = str(data["email"]).strip()
        if "preferred_communication_method" in data:
            m = str(data["preferred_communication_method"]).upper()
            if m in PreferredCommunicationMethod.values:
                contact.preferred_communication_method = m
        if "notes" in data:
            contact.notes = str(data["notes"]).strip()
        if "is_active" in data:
            contact.is_active = bool(data["is_active"])

        with transaction.atomic():
            if data.get("is_primary") is True and not contact.is_primary:
                # Reassign primary contact for this supplier
                SupplierContactPerson.objects.filter(
                    tenant=tenant,
                    supplier=contact.supplier,
                    is_primary=True,
                ).exclude(contact_id=contact.contact_id).update(is_primary=False)
                contact.is_primary = True
            elif "is_primary" in data:
                contact.is_primary = bool(data["is_primary"])

            contact.save()

            AuditLogEntry.objects.create(
                request_id=f"req-{uuid.uuid4().hex[:8]}",
                tenant_id=str(tenant.tenant_id),
                user_id=getattr(actor_user, "user_id", str(actor_user)),
                actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
                actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
                action="SUPPLIER_CONTACT_UPDATED",
                resource_type="SupplierContactPerson",
                resource_id=contact.contact_id,
                ip_address="127.0.0.1",
                status=AuditActionStatus.SUCCESS,
                metadata_json={"contact_id": contact.contact_id, "updated_fields": list(data.keys())},
            )

        return cls.serialize_contact_person(contact)

    @classmethod
    def archive_supplier_contact(
        cls,
        tenant_id: str,
        actor_user: Any,
        supplier_id: str,
        contact_id: str,
    ) -> Dict[str, Any]:
        """Soft-deletes / archives a supplier contact person."""
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        contact = SupplierContactPerson.objects.filter(contact_id=contact_id).first()
        if not contact or str(contact.supplier_id) != str(supplier_id):
            raise NotFound({"error_code": "ERR_CONTACT_NOT_FOUND", "detail": f"Contact '{contact_id}' not found for supplier."})
        if str(contact.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant contact archival forbidden."})

        contact.is_active = False
        contact.is_primary = False
        contact.save(update_fields=["is_active", "is_primary", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SUPPLIER_CONTACT_ARCHIVED",
            resource_type="SupplierContactPerson",
            resource_id=contact.contact_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"contact_id": contact.contact_id, "action": "ARCHIVED"},
        )

        return cls.serialize_contact_person(contact)

