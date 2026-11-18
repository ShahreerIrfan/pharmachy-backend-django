"""
backend/apps/core/tax_configuration_service.py
Architecture Decision: ADR-0297 (P297).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service governing Tax Configuration & Invoice Presentation:
- Tenant-configured, effective-dated, explicitly reviewed tax rules (ZERO hardcoded statutory rates).
- Mutable DRAFT rules -> audited review_and_activate transition -> immutable REVIEWED_ACTIVE -> soft ARCHIVED.
- Configurable Invoice Tax Presentation Profile (EN/BN headers, registration label/number, breakdown visibility, disclaimers).
- Accurate Decimal inclusive & exclusive tax extraction and immutable InvoiceTaxSnapshot persistence.
- Optional balanced double-entry GL posting (GL 4010 Sales Revenue + GL 2030 VAT/Tax Payable) and audited reversal.
- Multi-tenant isolation, branch scoping, subscription guards, AuditLogEntry, and CSV export.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.double_entry_journal_service import DoubleEntryJournalService, q2
from apps.core.models import (
    Branch,
    ChartOfAccount,
    InvoiceTaxPresentationProfile,
    InvoiceTaxSnapshot,
    InvoiceTaxSnapshotStatus,
    JournalEntry,
    JournalSourceModule,
    TaxAppliesToScope,
    TaxCalculationMode,
    TaxRuleConfiguration,
    TaxRuleStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")
DECIMAL_HUNDRED = Decimal("100.00")

TAX_VIEW_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
    UserRole.AUDITOR,
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "FINANCE_OFFICER",
    "AUDITOR",
    "CASHIER",
    "STAFF_PHARMACIST",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

TAX_MANAGE_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}


def q3(val: Decimal) -> Decimal:
    return val.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


class TaxConfigurationService:
    """
    Domain service governing reviewed tax rules, invoice presentation profiles,
    immutable invoice tax snapshots, and balanced GL tax liability accounting.
    """

    # =========================================================================
    # RBAC & TENANT GUARDS
    # =========================================================================

    @classmethod
    def assert_view_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if role not in TAX_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TAX_VIEW_DENIED",
                    "detail": "Not authorized to view tax rules.",
                    "message_en": "Not authorized to view tax rules.",
                    "message_bn": "ট্যাক্স রুল দেখার অনুমতি আপনার নেই।",
                }
            )

    @classmethod
    def assert_manage_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if role not in TAX_MANAGE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TAX_MANAGE_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Finance Officer can configure, review, or archive tax rules.",
                    "message_en": "Only Pharmacy Owner or Finance Officer can configure, review, or archive tax rules.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা ফিন্যান্স অফিসার ট্যাক্স রুল তৈরি, পর্যালোচনা বা আর্কাইভ করতে পারেন।",
                }
            )

    @classmethod
    def assert_tenant_active(cls, tenant_id: str) -> Tenant:
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' not found."})
        if (
            getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
            or getattr(tenant, "subscription_status", None) == "SUSPENDED"
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant subscription is suspended. Tax configuration access blocked.",
                    "message_en": "Tenant subscription is suspended. Tax configuration access blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। ট্যাক্স কনফিগারেশন বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    # =========================================================================
    # SERIALIZERS
    # =========================================================================

    @classmethod
    def _serialize_rule(cls, rule: TaxRuleConfiguration) -> Dict[str, Any]:
        return {
            "rule_id": rule.rule_id,
            "tenant_id": str(rule.tenant_id),
            "branch_id": str(rule.branch_id) if rule.branch_id else None,
            "branch_name": rule.branch.name if rule.branch else "All Branches",
            "rule_code": rule.rule_code,
            "name": rule.name,
            "name_bn": rule.name_bn,
            "applies_to_scope": rule.applies_to_scope,
            "calculation_mode": rule.calculation_mode,
            "tax_rate_pct": str(q3(rule.tax_rate_pct)),
            "gl_account_code": rule.gl_account.account_code if rule.gl_account else rule.default_gl_code,
            "effective_from": rule.effective_from.isoformat(),
            "effective_to": rule.effective_to.isoformat() if rule.effective_to else None,
            "status": rule.status,
            "review_reference": rule.review_reference,
            "review_notes": rule.review_notes,
            "reviewed_at": rule.reviewed_at.isoformat() if rule.reviewed_at else None,
            "created_at": rule.created_at.isoformat(),
            "updated_at": rule.updated_at.isoformat(),
        }

    @classmethod
    def _serialize_profile(cls, profile: InvoiceTaxPresentationProfile) -> Dict[str, Any]:
        return {
            "profile_id": profile.profile_id,
            "tenant_id": str(profile.tenant_id),
            "branch_id": str(profile.branch_id) if profile.branch_id else None,
            "legal_entity_name": profile.legal_entity_name,
            "tax_registration_label": profile.tax_registration_label,
            "tax_registration_number": profile.tax_registration_number,
            "show_tax_breakdown_lines": profile.show_tax_breakdown_lines,
            "show_zero_tax_lines": profile.show_zero_tax_lines,
            "show_inclusive_tax_note": profile.show_inclusive_tax_note,
            "invoice_header_en": profile.invoice_header_en,
            "invoice_header_bn": profile.invoice_header_bn,
            "tax_disclaimer_en": profile.tax_disclaimer_en,
            "tax_disclaimer_bn": profile.tax_disclaimer_bn,
            "updated_at": profile.updated_at.isoformat(),
        }

    # =========================================================================
    # TAX RULE CRUD & REVIEW LIFECYCLE
    # =========================================================================

    @classmethod
    @transaction.atomic
    def create_tax_rule(
        cls,
        *,
        tenant_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        rule_code = str(data.get("rule_code", "")).strip().upper()
        name = str(data.get("name", "")).strip()
        if not rule_code or not name:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_TAX_RULE_FIELDS",
                    "detail": "rule_code and name are required.",
                    "message_en": "Rule code and name are required.",
                    "message_bn": "ট্যাক্স রুল কোড এবং নাম আবশ্যক।",
                }
            )

        scope = str(data.get("applies_to_scope", TaxAppliesToScope.ALL_ITEMS)).upper().strip()
        if scope not in TaxAppliesToScope.values:
            raise ValidationError({"error_code": "ERR_INVALID_TAX_SCOPE", "detail": f"Invalid applies_to_scope '{scope}'."})

        calc_mode = str(data.get("calculation_mode", TaxCalculationMode.EXCLUSIVE)).upper().strip()
        if calc_mode not in TaxCalculationMode.values:
            raise ValidationError(
                {"error_code": "ERR_INVALID_CALCULATION_MODE", "detail": f"Invalid calculation_mode '{calc_mode}'."}
            )

        rate_raw = data.get("tax_rate_pct")
        if rate_raw is None:
            raise ValidationError({"error_code": "ERR_MISSING_TAX_RATE", "detail": "tax_rate_pct is required."})
        try:
            rate = q3(Decimal(str(rate_raw)))
            if rate < Decimal("0.000") or rate > Decimal("100.000"):
                raise ValueError()
        except Exception:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_TAX_RATE",
                    "detail": "tax_rate_pct must be a decimal between 0.000 and 100.000.",
                }
            )

        eff_from_raw = data.get("effective_from")
        try:
            eff_from = (
                datetime.date.fromisoformat(eff_from_raw)
                if isinstance(eff_from_raw, str)
                else (eff_from_raw or timezone.now().date())
            )
        except Exception:
            raise ValidationError({"error_code": "ERR_INVALID_EFFECTIVE_FROM", "detail": "Invalid effective_from date."})

        eff_to = None
        if data.get("effective_to"):
            try:
                eff_to = (
                    datetime.date.fromisoformat(data["effective_to"])
                    if isinstance(data["effective_to"], str)
                    else data["effective_to"]
                )
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_EFFECTIVE_TO", "detail": "Invalid effective_to date."})
            if eff_to < eff_from:
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_EFFECTIVE_WINDOW",
                        "detail": "effective_to cannot be earlier than effective_from.",
                    }
                )

        # Optional Branch scope (with cross-tenant guard)
        branch = None
        branch_id = data.get("branch_id")
        if branch_id:
            branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
            if not branch:
                foreign = Branch.objects.filter(branch_id=branch_id).first()
                if foreign and str(foreign.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant branch access forbidden."})
                raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        gl_code = str(data.get("gl_account_code", "2030")).strip()
        gl_account = ChartOfAccount.objects.filter(tenant=tenant, account_code=gl_code).first()

        rule = TaxRuleConfiguration.objects.create(
            tenant=tenant,
            branch=branch,
            rule_code=rule_code,
            name=name,
            name_bn=str(data.get("name_bn", "")).strip(),
            applies_to_scope=scope,
            calculation_mode=calc_mode,
            tax_rate_pct=rate,
            gl_account=gl_account,
            default_gl_code=gl_code,
            effective_from=eff_from,
            effective_to=eff_to,
            status=TaxRuleStatus.DRAFT,
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="TAX_RULE_CREATE_DRAFT",
            resource_type="TaxRuleConfiguration",
            resource_id=rule.rule_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"rule_code": rule_code, "rate": str(rate), "scope": scope},
        )

        res = cls._serialize_rule(rule)
        res["message_en"] = f"Tax rule '{rule.rule_code}' created in DRAFT status pending review."
        res["message_bn"] = f"ট্যাক্স রুল '{rule.rule_code}' ড্রাফট অবস্থায় তৈরি হয়েছে; সক্রিয় করতে রিভিউ অনুমোদন প্রয়োজন।"
        return res

    @classmethod
    def list_tax_rules(
        cls,
        *,
        tenant_id: str,
        status_filter: Optional[str] = None,
        branch_id: Optional[str] = None,
        applies_to_scope: Optional[str] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = TaxRuleConfiguration.objects.filter(tenant=tenant).select_related("branch", "gl_account")
        if status_filter:
            qs = qs.filter(status=status_filter.upper().strip())
        if branch_id:
            qs = qs.filter(Q(branch_id=branch_id) | Q(branch__isnull=True))
        if applies_to_scope:
            qs = qs.filter(applies_to_scope=applies_to_scope.upper().strip())

        rules = [cls._serialize_rule(r) for r in qs]
        return {"tenant_id": str(tenant.tenant_id), "count": len(rules), "rules": rules}

    @classmethod
    def get_tax_rule(cls, *, tenant_id: str, rule_id: str, actor_user: Any = None) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        rule = TaxRuleConfiguration.objects.select_related("branch", "gl_account").filter(tenant=tenant, rule_id=rule_id).first()
        if not rule:
            foreign = TaxRuleConfiguration.objects.filter(rule_id=rule_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant tax rule access forbidden."})
            raise NotFound({"error_code": "ERR_TAX_RULE_NOT_FOUND", "detail": f"Tax rule '{rule_id}' not found."})
        return cls._serialize_rule(rule)

    @classmethod
    @transaction.atomic
    def update_tax_rule_draft(
        cls,
        *,
        tenant_id: str,
        rule_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        rule = TaxRuleConfiguration.objects.select_for_update().filter(tenant=tenant, rule_id=rule_id).first()
        if not rule:
            foreign = TaxRuleConfiguration.objects.filter(rule_id=rule_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant tax rule update forbidden."})
            raise NotFound({"error_code": "ERR_TAX_RULE_NOT_FOUND", "detail": f"Tax rule '{rule_id}' not found."})

        if rule.status != TaxRuleStatus.DRAFT:
            raise ValidationError(
                {
                    "error_code": "ERR_TAX_RULE_IMMUTABLE",
                    "detail": "Reviewed/Active or Archived tax rules cannot be edited in place. Create a new effective-dated draft rule.",
                    "message_en": "Reviewed or archived tax rules are immutable. Create a new effective-dated rule instead.",
                    "message_bn": "রিভিউড বা আর্কাইভড ট্যাক্স রুল সরাসরি পরিবর্তন করা যাবে না। নতুন কার্যকর তারিখসহ নতুন রুল তৈরি করুন।",
                }
            )

        if "name" in data and str(data["name"]).strip():
            rule.name = str(data["name"]).strip()
        if "name_bn" in data:
            rule.name_bn = str(data["name_bn"] or "").strip()
        if "tax_rate_pct" in data and data["tax_rate_pct"] is not None:
            try:
                rate = q3(Decimal(str(data["tax_rate_pct"])))
                if rate < Decimal("0.000") or rate > Decimal("100.000"):
                    raise ValueError()
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_TAX_RATE", "detail": "Invalid tax_rate_pct."})
            rule.tax_rate_pct = rate
        if "calculation_mode" in data and data["calculation_mode"]:
            mode = str(data["calculation_mode"]).upper().strip()
            if mode not in TaxCalculationMode.values:
                raise ValidationError({"error_code": "ERR_INVALID_CALCULATION_MODE", "detail": f"Invalid calculation_mode '{mode}'."})
            rule.calculation_mode = mode

        rule.save()
        return cls._serialize_rule(rule)

    @classmethod
    @transaction.atomic
    def review_and_activate_rule(
        cls,
        *,
        tenant_id: str,
        rule_id: str,
        review_reference: str,
        review_notes: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_ref = str(review_reference or "").strip()
        if not clean_ref:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_REVIEW_REFERENCE",
                    "detail": "review_reference (e.g. advisor memo / board review ID) is mandatory to activate a tax rule.",
                    "message_en": "A review reference is required before activating a tax rule.",
                    "message_bn": "ট্যাক্স রুল সক্রিয় করার আগে পর্যালোচনা রেফারেন্স (Review Reference) প্রদান করা আবশ্যক।",
                }
            )

        rule = TaxRuleConfiguration.objects.select_for_update().filter(tenant=tenant, rule_id=rule_id).first()
        if not rule:
            foreign = TaxRuleConfiguration.objects.filter(rule_id=rule_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant tax rule review forbidden."})
            raise NotFound({"error_code": "ERR_TAX_RULE_NOT_FOUND", "detail": f"Tax rule '{rule_id}' not found."})

        if rule.status != TaxRuleStatus.DRAFT:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_RULE_TRANSITION",
                    "detail": f"Only DRAFT rules can be reviewed and activated (current status: {rule.status}).",
                }
            )

        rule.status = TaxRuleStatus.REVIEWED_ACTIVE
        rule.review_reference = clean_ref
        rule.review_notes = str(review_notes or "").strip()
        rule.reviewed_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        rule.reviewed_at = timezone.now()
        rule.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="TAX_RULE_REVIEW_ACTIVATE",
            resource_type="TaxRuleConfiguration",
            resource_id=rule.rule_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"rule_code": rule.rule_code, "review_reference": clean_ref},
        )

        res = cls._serialize_rule(rule)
        res["message_en"] = f"Tax rule '{rule.rule_code}' reviewed and activated."
        res["message_bn"] = f"ট্যাক্স রুল '{rule.rule_code}' পর্যালোচিত হয়ে সক্রিয় করা হয়েছে।"
        return res

    @classmethod
    @transaction.atomic
    def archive_tax_rule(
        cls,
        *,
        tenant_id: str,
        rule_id: str,
        reason: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        rule = TaxRuleConfiguration.objects.select_for_update().filter(tenant=tenant, rule_id=rule_id).first()
        if not rule:
            foreign = TaxRuleConfiguration.objects.filter(rule_id=rule_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant archive forbidden."})
            raise NotFound({"error_code": "ERR_TAX_RULE_NOT_FOUND", "detail": f"Tax rule '{rule_id}' not found."})

        rule.status = TaxRuleStatus.ARCHIVED
        if reason:
            rule.review_notes = f"{rule.review_notes} [Archived: {reason.strip()}]".strip()
        rule.save()

        return {
            "success": True,
            "rule_id": rule.rule_id,
            "status": rule.status,
            "message_en": f"Tax rule '{rule.rule_code}' archived.",
            "message_bn": f"ট্যাক্স রুল '{rule.rule_code}' আর্কাইভ করা হয়েছে।",
        }

    # =========================================================================
    # INVOICE TAX PRESENTATION PROFILE
    # =========================================================================

    @classmethod
    def get_or_default_presentation_profile(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        profile = None
        if branch_id:
            profile = InvoiceTaxPresentationProfile.objects.filter(tenant=tenant, branch_id=branch_id).first()
        if not profile:
            profile = InvoiceTaxPresentationProfile.objects.filter(tenant=tenant, branch__isnull=True).first()
        if profile:
            return cls._serialize_profile(profile)

        return {
            "profile_id": None,
            "tenant_id": str(tenant.tenant_id),
            "branch_id": branch_id,
            "legal_entity_name": tenant.name,
            "tax_registration_label": "BIN / VAT Reg No.",
            "tax_registration_number": "",
            "show_tax_breakdown_lines": True,
            "show_zero_tax_lines": True,
            "show_inclusive_tax_note": True,
            "invoice_header_en": "Tax & Dispensing Invoice",
            "invoice_header_bn": "ট্যাক্স ও বিক্রয় ইনভয়েস",
            "tax_disclaimer_en": "Tax calculated using tenant-reviewed internal tax rules. Verify statutory compliance with your tax advisor.",
            "tax_disclaimer_bn": "ফার্মেসি কর্তৃপক্ষের পর্যালোচিত কর নীতিমালা অনুযায়ী কর গণনা করা হয়েছে।",
        }

    @classmethod
    @transaction.atomic
    def upsert_presentation_profile(
        cls,
        *,
        tenant_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        branch = None
        branch_id = data.get("branch_id")
        if branch_id:
            branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
            if not branch:
                foreign = Branch.objects.filter(branch_id=branch_id).first()
                if foreign and str(foreign.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant branch access forbidden."})
                raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        profile = InvoiceTaxPresentationProfile.objects.filter(tenant=tenant, branch=branch).first()
        if not profile:
            profile = InvoiceTaxPresentationProfile(
                tenant=tenant,
                branch=branch,
                legal_entity_name=str(data.get("legal_entity_name") or tenant.name).strip(),
            )

        for field in (
            "legal_entity_name",
            "tax_registration_label",
            "tax_registration_number",
            "invoice_header_en",
            "invoice_header_bn",
            "tax_disclaimer_en",
            "tax_disclaimer_bn",
        ):
            if field in data and data[field] is not None:
                setattr(profile, field, str(data[field]).strip())

        for bfield in ("show_tax_breakdown_lines", "show_zero_tax_lines", "show_inclusive_tax_note"):
            if bfield in data:
                setattr(profile, bfield, bool(data[bfield]))

        profile.updated_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        profile.save()
        return cls._serialize_profile(profile)

    # =========================================================================
    # INVOICE TAX CALCULATION, SNAPSHOT & BALANCED GL INTEGRATION
    # =========================================================================

    @classmethod
    def _resolve_rule_for_item(
        cls,
        tenant: Tenant,
        branch_id: Optional[str],
        item_scope: str,
        invoice_date: datetime.date,
    ) -> Optional[TaxRuleConfiguration]:
        """
        Resolves the single highest-precedence REVIEWED_ACTIVE rule effective on invoice_date:
        1. Branch-specific + exact item scope
        2. Tenant-wide + exact item scope
        3. Branch-specific + ALL_ITEMS
        4. Tenant-wide + ALL_ITEMS
        Returns None if no reviewed rule exists (zero hardcoded tax assumptions!).
        """
        active_qs = TaxRuleConfiguration.objects.filter(
            tenant=tenant,
            status=TaxRuleStatus.REVIEWED_ACTIVE,
            effective_from__lte=invoice_date,
        ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=invoice_date))

        candidates = list(active_qs.select_related("gl_account"))
        scope_upper = (item_scope or TaxAppliesToScope.ALL_ITEMS).upper().strip()

        def _score(r: TaxRuleConfiguration) -> int:
            if r.branch_id and str(r.branch_id) != str(branch_id or ""):
                return -1
            if r.applies_to_scope not in (scope_upper, TaxAppliesToScope.ALL_ITEMS):
                return -1
            score = 0
            if r.applies_to_scope == scope_upper:
                score += 10
            if r.branch_id:
                score += 5
            return score

        ranked = [(r, _score(r)) for r in candidates]
        ranked = [pair for pair in ranked if pair[1] >= 0]
        if not ranked:
            return None
        ranked.sort(key=lambda pair: (pair[1], pair[0].effective_from), reverse=True)
        return ranked[0][0]

    @classmethod
    @transaction.atomic
    def calculate_invoice_taxes(
        cls,
        *,
        tenant_id: str,
        branch_id: str,
        items: List[Dict[str, Any]],
        invoice_reference: str = "",
        invoice_date: Optional[Any] = None,
        persist_snapshot: bool = False,
        post_gl: bool = False,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
        if not branch:
            foreign = Branch.objects.filter(branch_id=branch_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant branch access forbidden."})
            raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        if not items or not isinstance(items, list):
            raise ValidationError({"error_code": "ERR_EMPTY_INVOICE_ITEMS", "detail": "At least one invoice item is required."})

        inv_date = timezone.now().date()
        if invoice_date:
            inv_date = datetime.date.fromisoformat(invoice_date) if isinstance(invoice_date, str) else invoice_date

        presentation = cls.get_or_default_presentation_profile(
            tenant_id=tenant_id, branch_id=branch_id, actor_user=actor_user
        )

        line_results: List[Dict[str, Any]] = []
        taxable_subtotal = DECIMAL_ZERO
        total_tax = DECIMAL_ZERO
        grand_total = DECIMAL_ZERO
        gl_tax_by_code: Dict[str, Decimal] = {}

        for idx, it in enumerate(items, start=1):
            try:
                qty = Decimal(str(it.get("quantity", "1")))
                unit_price = Decimal(str(it.get("unit_price_bdt", "0.00")))
                discount = Decimal(str(it.get("discount_bdt", "0.00")))
                if qty <= DECIMAL_ZERO or unit_price < DECIMAL_ZERO or discount < DECIMAL_ZERO:
                    raise ValueError()
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_LINE_ITEM", "detail": f"Invalid numeric values on item {idx}."})

            line_gross = q2((qty * unit_price) - discount)
            if line_gross < DECIMAL_ZERO:
                raise ValidationError({"error_code": "ERR_NEGATIVE_LINE_TOTAL", "detail": f"Discount exceeds line gross on item {idx}."})

            item_scope = str(it.get("item_scope", TaxAppliesToScope.MEDICINE)).upper().strip()
            rule = cls._resolve_rule_for_item(tenant, branch_id, item_scope, inv_date)

            if not rule:
                line_taxable = line_gross
                line_tax = DECIMAL_ZERO
                line_total = line_gross
                rule_meta = {
                    "rule_id": None,
                    "rule_code": "UNCONFIGURED_ZERO_TAX",
                    "calculation_mode": "NONE",
                    "tax_rate_pct": "0.000",
                    "gl_account_code": "2030",
                }
            else:
                rate = rule.tax_rate_pct
                if rule.calculation_mode == TaxCalculationMode.INCLUSIVE:
                    divisor = Decimal("1.00000") + (rate / DECIMAL_HUNDRED)
                    line_taxable = q2(line_gross / divisor)
                    line_tax = q2(line_gross - line_taxable)
                    line_total = line_gross
                else:
                    line_taxable = line_gross
                    line_tax = q2(line_gross * (rate / DECIMAL_HUNDRED))
                    line_total = q2(line_taxable + line_tax)

                gl_code = rule.gl_account.account_code if rule.gl_account else rule.default_gl_code
                if line_tax > DECIMAL_ZERO:
                    gl_tax_by_code[gl_code] = q2(gl_tax_by_code.get(gl_code, DECIMAL_ZERO) + line_tax)

                rule_meta = {
                    "rule_id": rule.rule_id,
                    "rule_code": rule.rule_code,
                    "name": rule.name,
                    "review_reference": rule.review_reference,
                    "calculation_mode": rule.calculation_mode,
                    "tax_rate_pct": str(q3(rate)),
                    "gl_account_code": gl_code,
                }

            taxable_subtotal = q2(taxable_subtotal + line_taxable)
            total_tax = q2(total_tax + line_tax)
            grand_total = q2(grand_total + line_total)

            line_results.append(
                {
                    "line_number": idx,
                    "sku": str(it.get("sku", f"ITEM-{idx}")),
                    "description": str(it.get("description", "")),
                    "item_scope": item_scope,
                    "quantity": str(qty),
                    "taxable_base_bdt": str(line_taxable),
                    "tax_amount_bdt": str(line_tax),
                    "line_total_bdt": str(line_total),
                    "applied_rule": rule_meta,
                }
            )

        snapshot_id = None
        posted_journal_id = None
        inv_ref = str(invoice_reference or f"TAXINV-{uuid.uuid4().hex[:8].upper()}").strip()

        if persist_snapshot:
            journal = None
            if post_gl and grand_total > DECIMAL_ZERO:
                lines = [
                    {
                        "account_code": "1010",
                        "debit_amount_bdt": grand_total,
                        "credit_amount_bdt": DECIMAL_ZERO,
                        "description": f"Invoice settlement {inv_ref}",
                    },
                    {
                        "account_code": "4010",
                        "debit_amount_bdt": DECIMAL_ZERO,
                        "credit_amount_bdt": taxable_subtotal,
                        "description": f"Net medicine/retail revenue {inv_ref}",
                    },
                ]
                for gl_code, tax_amt in gl_tax_by_code.items():
                    if tax_amt > DECIMAL_ZERO:
                        lines.append(
                            {
                                "account_code": gl_code,
                                "debit_amount_bdt": DECIMAL_ZERO,
                                "credit_amount_bdt": tax_amt,
                                "description": f"Reviewed tax collected on {inv_ref}",
                            }
                        )
                j_res = DoubleEntryJournalService.post_journal_entry(
                    tenant_id=str(tenant.tenant_id),
                    branch_id=str(branch.branch_id),
                    posting_date=inv_date,
                    source_module=JournalSourceModule.POS_SALE,
                    source_reference=inv_ref,
                    narration=f"Invoice Tax Snapshot GL Posting: {inv_ref}",
                    lines=lines,
                    actor_user=actor_user,
                )
                journal = JournalEntry.objects.get(entry_id=j_res["entry_id"])
                posted_journal_id = str(journal.entry_id)

            snap = InvoiceTaxSnapshot.objects.create(
                tenant=tenant,
                branch=branch,
                invoice_reference=inv_ref,
                invoice_date=inv_date,
                taxable_subtotal_bdt=taxable_subtotal,
                total_tax_bdt=total_tax,
                grand_total_bdt=grand_total,
                applied_rules_json=line_results,
                presentation_json=presentation,
                status=InvoiceTaxSnapshotStatus.FINALIZED,
                journal_entry=journal,
                created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            )
            snapshot_id = snap.snapshot_id

        return {
            "tenant_id": str(tenant.tenant_id),
            "branch_id": str(branch.branch_id),
            "invoice_reference": inv_ref,
            "invoice_date": inv_date.isoformat(),
            "taxable_subtotal_bdt": str(taxable_subtotal),
            "total_tax_bdt": str(total_tax),
            "grand_total_bdt": str(grand_total),
            "lines": line_results,
            "presentation": presentation,
            "snapshot_id": snapshot_id,
            "posted_journal_id": posted_journal_id,
        }

    @classmethod
    @transaction.atomic
    def reverse_tax_snapshot(
        cls,
        *,
        tenant_id: str,
        snapshot_id: str,
        reason: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError({"error_code": "ERR_MISSING_REVERSAL_REASON", "detail": "Reversal reason is mandatory."})

        snap = InvoiceTaxSnapshot.objects.select_for_update().filter(tenant=tenant, snapshot_id=snapshot_id).first()
        if not snap:
            foreign = InvoiceTaxSnapshot.objects.filter(snapshot_id=snapshot_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant reversal forbidden."})
            raise NotFound({"error_code": "ERR_SNAPSHOT_NOT_FOUND", "detail": f"Tax snapshot '{snapshot_id}' not found."})

        if snap.status == InvoiceTaxSnapshotStatus.REVERSED:
            raise ValidationError({"error_code": "ERR_ALREADY_REVERSED", "detail": "Tax snapshot already reversed."})

        reversal_jid = None
        if snap.journal_entry_id:
            rev = DoubleEntryJournalService.reverse_journal_entry(
                tenant_id=str(tenant.tenant_id),
                entry_id=str(snap.journal_entry_id),
                reason=f"Invoice tax snapshot reversal ({snap.invoice_reference}): {clean_reason}",
                actor_user=actor_user,
            )
            reversal_jid = rev["reversal_journal_id"]
            snap.reversal_journal = JournalEntry.objects.get(entry_id=reversal_jid)

        snap.status = InvoiceTaxSnapshotStatus.REVERSED
        snap.reversal_reason = clean_reason
        snap.save()

        return {
            "success": True,
            "snapshot_id": snap.snapshot_id,
            "status": snap.status,
            "reversal_journal_id": reversal_jid,
            "message_en": f"Tax snapshot '{snap.snapshot_id}' reversed with compensating audit event.",
            "message_bn": f"ট্যাক্স স্ন্যাপশট '{snap.snapshot_id}' অডিটেড রিভার্সালসহ বাতিল করা হয়েছে।",
        }

    @classmethod
    def export_tax_rules_csv(cls, *, tenant_id: str, actor_user: Any = None) -> str:
        res = cls.list_tax_rules(tenant_id=tenant_id, actor_user=actor_user)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "Rule ID",
                "Rule Code",
                "Name",
                "Scope",
                "Calculation Mode",
                "Rate (%)",
                "GL Code",
                "Effective From",
                "Effective To",
                "Status",
                "Review Reference",
            ]
        )
        for r in res["rules"]:
            writer.writerow(
                [
                    r["rule_id"],
                    r["rule_code"],
                    r["name"],
                    r["applies_to_scope"],
                    r["calculation_mode"],
                    r["tax_rate_pct"],
                    r["gl_account_code"],
                    r["effective_from"],
                    r["effective_to"] or "",
                    r["status"],
                    r["review_reference"],
                ]
            )
        return buf.getvalue()
