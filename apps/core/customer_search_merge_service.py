"""
backend/apps/core/customer_search_merge_service.py
Architecture Decision: ADR-0268 (P268).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service for:
1. Normalized Bangladesh phone and alias search for POS customers:
   - Strips country code (+88, 88), spaces, dashes and non-digit characters.
   - Searches active phone and secondary phones list.
   - Searches full names and known aliases/nicknames.
2. Controlled, audited duplicate customer merge:
   - Validates RBAC (only PHARMACY_OWNER, BRANCH_MANAGER, SUPERADMIN).
   - Enforces strict tenant boundaries and prohibits self-merges or re-merging already merged profiles.
   - Consolidates credit status, limits, aliases, and secondary phone numbers.
   - Preserves immutable transaction links (historical sales, receipts, collections, prescriptions).
   - Re-assigns active outstanding dues to survivor (target) customer.
   - Transfers outstanding receivable balance with audited AR ledger ADJUSTMENT entries
     (MERGE-OUT credit on source, MERGE-IN debit on target), ensuring double-entry parity.
   - Records immutable CustomerMergeAudit with cryptographic SHA-256 hash.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.customer_receivable_ledger_service import CustomerReceivableLedgerService
from apps.core.models import (
    Branch,
    CustomerDueRecord,
    CustomerMergeAudit,
    CustomerReceivableEntryType,
    DueStatus,
    PosCustomer,
    Tenant,
)
from apps.core.pos_domain_service import PosDomainService, q2
from apps.identity.models import UserRole

MONEY_ZERO = Decimal("0.00")
CUSTOMER_ADMIN_ROLES = PosDomainService.POS_CUSTOMER_ADMIN_ROLES


def normalize_bd_phone(raw_phone: str) -> Dict[str, Any]:
    """
    Normalizes a Bangladesh telephone number string into canonical forms.
    Returns:
        {
            "raw": str,
            "digits": str,
            "local_11": str (e.g. "01712345678"),
            "e164": str (e.g. "+8801712345678"),
            "is_valid_bd": bool,
            "variants": List[str]
        }
    """
    raw = str(raw_phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return {
            "raw": raw,
            "digits": "",
            "local_11": "",
            "e164": "",
            "is_valid_bd": False,
            "variants": [],
        }

    # Extract 11-digit local BD number
    local_11 = ""
    if digits.startswith("880") and len(digits) == 13:
        local_11 = "0" + digits[3:]
    elif digits.startswith("0") and len(digits) == 11:
        local_11 = digits
    elif digits.startswith("1") and len(digits) == 10:
        local_11 = "0" + digits
    elif len(digits) == 11 and digits.startswith("01"):
        local_11 = digits

    is_valid_bd = bool(local_11 and len(local_11) == 11 and local_11.startswith("01"))
    e164 = f"+88{local_11}" if is_valid_bd else f"+{digits}"

    variants: Set[str] = {raw, digits}
    if is_valid_bd:
        variants.add(local_11)
        variants.add(e164)
        variants.add(f"88{local_11}")
        variants.add(local_11[1:])  # 10 digits without leading 0

    return {
        "raw": raw,
        "digits": digits,
        "local_11": local_11,
        "e164": e164,
        "is_valid_bd": is_valid_bd,
        "variants": sorted(list(variants)),
    }


def serialize_customer_snapshot(cust: PosCustomer) -> Dict[str, Any]:
    """Generates an immutable JSON-serializable snapshot of customer state."""
    return {
        "customer_id": cust.customer_id,
        "tenant_id": str(cust.tenant_id),
        "primary_branch_id": str(cust.primary_branch_id or ""),
        "full_name": cust.full_name,
        "phone": cust.phone,
        "email": cust.email,
        "address": cust.address,
        "loyalty_code": cust.loyalty_code,
        "credit_limit_bdt": f"{cust.credit_limit_bdt:.2f}",
        "current_due_bdt": f"{cust.current_due_bdt:.2f}",
        "is_credit_approved": cust.is_credit_approved,
        "credit_status": cust.credit_status,
        "credit_terms_days": cust.credit_terms_days,
        "aliases": list(cust.aliases or []),
        "secondary_phones": list(cust.secondary_phones or []),
        "is_active": cust.is_active,
        "is_archived": cust.is_archived,
        "merged_into_id": str(cust.merged_into_id or ""),
        "created_at": cust.created_at.isoformat() if cust.created_at else None,
    }


class CustomerSearchMergeService:
    """Domain service for searching POS customers and merging duplicate profiles."""

    @classmethod
    def search_customers(
        cls,
        *,
        user: Any,
        query: str,
        branch_id: Optional[str] = None,
        include_merged: bool = False,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """
        Searches customers within user's tenant using normalized phone, name, aliases,
        or loyalty code.
        """
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=branch_id, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )

        clean_q = str(query or "").strip()
        if not clean_q:
            # Return recent active customers
            qs = PosCustomer.objects.filter(tenant=tenant)
            if not include_merged:
                qs = qs.filter(is_archived=False)
            results = qs.order_by("-updated_at")[:limit]
            return [cls._serialize_search_hit(c, match_type="RECENT") for c in results]

        phone_info = normalize_bd_phone(clean_q)
        q_filter = Q()

        # 1. Phone match conditions
        for v in phone_info["variants"]:
            if len(v) >= 4:
                q_filter |= Q(phone__icontains=v)
                q_filter |= Q(secondary_phones__icontains=v)

        # 2. Name, alias, loyalty code, email matches
        q_filter |= Q(full_name__icontains=clean_q)
        q_filter |= Q(aliases__icontains=clean_q)
        q_filter |= Q(loyalty_code__iexact=clean_q)
        if "@" in clean_q:
            q_filter |= Q(email__icontains=clean_q)

        qs = PosCustomer.objects.filter(tenant=tenant).filter(q_filter)
        if not include_merged:
            qs = qs.filter(is_archived=False)

        # Branch filtering for branch-restricted staff
        role = str(getattr(user, "role", "")).upper()
        actor_branch_id = getattr(user, "branch_id", None)
        if role not in PosDomainService.TENANT_WIDE_ROLES and actor_branch_id:
            qs = qs.filter(Q(primary_branch_id=actor_branch_id) | Q(primary_branch__isnull=True))

        candidates = list(qs[:limit])

        # Rank and serialize results
        scored: List[Dict[str, Any]] = []
        for c in candidates:
            match_type = cls._determine_match_type(c, clean_q, phone_info)
            scored.append(cls._serialize_search_hit(c, match_type=match_type))

        # Sort: EXACT_PHONE first, then ALIAS, NAME, LOYALTY, PARTIAL_PHONE
        rank_order = {"EXACT_PHONE": 1, "ALIAS": 2, "NAME": 3, "LOYALTY": 4, "PARTIAL_PHONE": 5, "RECENT": 6}
        scored.sort(key=lambda x: (rank_order.get(x["match_type"], 99), -Decimal(x["current_due_bdt"])))
        return scored

    @classmethod
    def _determine_match_type(cls, customer: PosCustomer, query: str, phone_info: Dict[str, Any]) -> str:
        q_lower = query.lower()
        # Exact phone
        if phone_info["local_11"] and (customer.phone == phone_info["local_11"] or customer.phone == phone_info["e164"]):
            return "EXACT_PHONE"
        if customer.phone and phone_info["digits"] and phone_info["digits"] in re.sub(r"\D", "", customer.phone):
            return "EXACT_PHONE" if len(phone_info["digits"]) >= 10 else "PARTIAL_PHONE"

        # Secondary phone
        sec_phones = [re.sub(r"\D", "", str(p)) for p in (customer.secondary_phones or [])]
        if phone_info["digits"] and phone_info["digits"] in sec_phones:
            return "EXACT_PHONE"

        # Alias match
        aliases = [str(a).lower() for a in (customer.aliases or [])]
        if any(q_lower in a for a in aliases):
            return "ALIAS"

        # Name match
        if q_lower in customer.full_name.lower():
            return "NAME"

        # Loyalty
        if customer.loyalty_code and q_lower == customer.loyalty_code.lower():
            return "LOYALTY"

        return "PARTIAL_PHONE"

    @classmethod
    def _serialize_search_hit(cls, c: PosCustomer, match_type: str) -> Dict[str, Any]:
        return {
            "customer_id": c.customer_id,
            "tenant_id": str(c.tenant_id),
            "primary_branch_id": str(c.primary_branch_id or ""),
            "primary_branch_name": c.primary_branch.name if c.primary_branch else "",
            "full_name": c.full_name,
            "phone": c.phone,
            "email": c.email,
            "address": c.address,
            "loyalty_code": c.loyalty_code,
            "credit_limit_bdt": f"{c.credit_limit_bdt:.2f}",
            "current_due_bdt": f"{c.current_due_bdt:.2f}",
            "available_credit_bdt": f"{max(MONEY_ZERO, c.credit_limit_bdt - c.current_due_bdt):.2f}",
            "is_credit_approved": c.is_credit_approved,
            "credit_status": c.credit_status,
            "aliases": list(c.aliases or []),
            "secondary_phones": list(c.secondary_phones or []),
            "is_active": c.is_active,
            "is_archived": c.is_archived,
            "is_merged": bool(c.merged_into_id),
            "merged_into_id": str(c.merged_into_id or ""),
            "match_type": match_type,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }

    @classmethod
    @transaction.atomic
    def merge_customers(
        cls,
        *,
        user: Any,
        source_customer_id: str,
        target_customer_id: str,
        merge_reason: str,
        branch_id: Optional[str] = None,
        idempotency_key: str = "",
    ) -> Dict[str, Any]:
        """
        Controlled merge of `source_customer` into `target_customer`:
        1. Validates RBAC (only POS_CUSTOMER_ADMIN_ROLES).
        2. Enforces active tenant ownership and prohibits self-merge or duplicate re-merge.
        3. Consolidates profiles, aliases, and secondary phones.
        4. Re-assigns active outstanding dues (CustomerDueRecord) to target.
        5. Atomically posts AR ledger transfer adjustments:
           - MERGE-OUT on source (credit source_due, balance becomes 0).
           - MERGE-IN on target (debit source_due, balance increases by source_due).
        6. Archives source customer with merged_into=target.
        7. Creates immutable CustomerMergeAudit with SHA256 integrity hash.
        """
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=branch_id,
            require_write=True,
            allowed_roles=CUSTOMER_ADMIN_ROLES,
        )

        clean_source = str(source_customer_id or "").strip()
        clean_target = str(target_customer_id or "").strip()
        clean_reason = str(merge_reason or "").strip()

        if not clean_source or not clean_target:
            raise ValidationError(
                {
                    "error_code": "ERR_MERGE_INPUT_INVALID",
                    "source_customer_id": "source_customer_id is required." if not clean_source else "",
                    "target_customer_id": "target_customer_id is required." if not clean_target else "",
                }
            )

        if clean_source == clean_target:
            raise ValidationError(
                {
                    "error_code": "ERR_SELF_MERGE_FORBIDDEN",
                    "target_customer_id": "Cannot merge a customer into itself.",
                }
            )

        if not clean_reason:
            raise ValidationError(
                {
                    "error_code": "ERR_MERGE_REASON_REQUIRED",
                    "merge_reason": "merge_reason is required explaining the justification for customer profile merge.",
                }
            )

        # Idempotency check if idempotency key passed
        clean_idem = (idempotency_key or "").strip()
        if clean_idem:
            existing_audit = CustomerMergeAudit.objects.filter(
                tenant=tenant,
                source_customer_id=clean_source,
                target_customer_id=clean_target,
            ).first()
            if existing_audit and existing_audit.source_customer.merged_into_id == clean_target:
                return cls._serialize_merge_result(existing_audit, is_idempotent_replay=True)

        # Lock both customer rows
        try:
            source = PosCustomer.objects.select_for_update().get(tenant=tenant, customer_id=clean_source)
        except PosCustomer.DoesNotExist:
            raise NotFound(
                {
                    "error_code": "ERR_SOURCE_CUSTOMER_NOT_FOUND",
                    "source_customer_id": f"Source customer '{clean_source}' was not found in active tenant scope.",
                }
            )

        try:
            target = PosCustomer.objects.select_for_update().get(tenant=tenant, customer_id=clean_target)
        except PosCustomer.DoesNotExist:
            raise NotFound(
                {
                    "error_code": "ERR_TARGET_CUSTOMER_NOT_FOUND",
                    "target_customer_id": f"Target customer '{clean_target}' was not found in active tenant scope.",
                }
            )

        # Ensure source is not already merged
        if source.merged_into_id:
            raise ValidationError(
                {
                    "error_code": "ERR_ALREADY_MERGED",
                    "source_customer_id": f"Source customer '{source.customer_id}' was already merged into '{source.merged_into_id}'.",
                }
            )

        # Take frozen pre-merge snapshots
        source_snap = serialize_customer_snapshot(source)
        target_snap = serialize_customer_snapshot(target)

        transferred_due = q2(source.current_due_bdt)
        merge_id = f"cmerge-{uuid.uuid4().hex[:12]}"

        # 1. Consolidate Aliases
        consolidated_aliases = list(target.aliases or [])
        if source.full_name and source.full_name not in consolidated_aliases and source.full_name != target.full_name:
            consolidated_aliases.append(source.full_name)
        for a in (source.aliases or []):
            if a and a not in consolidated_aliases and a != target.full_name:
                consolidated_aliases.append(a)
        target.aliases = consolidated_aliases

        # 2. Consolidate Secondary Phones
        consolidated_phones = list(target.secondary_phones or [])
        if source.phone and source.phone not in consolidated_phones and source.phone != target.phone:
            consolidated_phones.append(source.phone)
        for p in (source.secondary_phones or []):
            if p and p not in consolidated_phones and p != target.phone:
                consolidated_phones.append(p)
        target.secondary_phones = consolidated_phones

        # 3. Consolidate Credit & Approval Limits
        target.credit_limit_bdt = max(target.credit_limit_bdt, source.credit_limit_bdt)
        if source.is_credit_approved and not target.is_credit_approved:
            target.is_credit_approved = True
            target.credit_status = source.credit_status
            target.credit_approved_by = source.credit_approved_by
            target.credit_approved_at = source.credit_approved_at
            target.credit_terms_days = max(target.credit_terms_days, source.credit_terms_days)

        # 4. Consolidate Notes
        if source.notes:
            merge_note = f"[Merged from {source.customer_id} on {timezone.now().strftime('%Y-%m-%d')}: {source.notes}]"
            target.notes = f"{target.notes}\n{merge_note}".strip() if target.notes else merge_note

        # 5. Re-link active outstanding dues to target
        active_dues = CustomerDueRecord.objects.filter(
            tenant=tenant,
            customer=source,
            status__in=[DueStatus.OUTSTANDING, DueStatus.PARTIAL],
        )
        dues_relinked_count = active_dues.count()
        if dues_relinked_count > 0:
            for due in active_dues:
                due.customer = target
                due.notes = f"{due.notes or ''} [MERGED from {source.customer_id} in {merge_id}]".strip()
                due.save(update_fields=["customer", "notes", "updated_at"])

        # Count immutable historical records that remain referenced for audit
        historical_sales_count = source.pos_sales.count()
        historical_collections_count = source.due_collections.count()
        historical_prescriptions_count = getattr(source, "prescriptions", PosCustomer.objects.none()).count()

        relinked_summary = {
            "active_dues_relinked": dues_relinked_count,
            "historical_sales_preserved": historical_sales_count,
            "historical_collections_preserved": historical_collections_count,
            "prescriptions_preserved": historical_prescriptions_count,
        }

        # 6. Post AR Ledger Transfer Adjustments if balance > 0
        if transferred_due > MONEY_ZERO:
            # Source ledger credit -> balance drops to 0
            CustomerReceivableLedgerService.post_entry_in_tx(
                tenant=tenant,
                customer=source,
                entry_type=CustomerReceivableEntryType.ADJUSTMENT,
                reference_number=f"MERGE-OUT-{merge_id}",
                debit_amount_bdt=MONEY_ZERO,
                credit_amount_bdt=transferred_due,
                branch=branch,
                narration=f"Merged into {target.full_name} ({target.customer_id}). Reason: {clean_reason}",
                posted_by=user,
                skip_customer_balance_update=False,
            )

            # Target ledger debit -> balance increases by transferred_due
            CustomerReceivableLedgerService.post_entry_in_tx(
                tenant=tenant,
                customer=target,
                entry_type=CustomerReceivableEntryType.ADJUSTMENT,
                reference_number=f"MERGE-IN-{merge_id}",
                debit_amount_bdt=transferred_due,
                credit_amount_bdt=MONEY_ZERO,
                branch=branch,
                narration=f"Merged from {source.full_name} ({source.customer_id}). Reason: {clean_reason}",
                posted_by=user,
                skip_customer_balance_update=False,
            )

        # 7. Update source customer: Archive and mark merged
        source.is_archived = True
        source.is_active = False
        source.current_due_bdt = MONEY_ZERO
        source.merged_into = target
        source.merged_at = timezone.now()
        source.merged_by = user
        source.save(
            update_fields=[
                "is_archived",
                "is_active",
                "current_due_bdt",
                "merged_into",
                "merged_at",
                "merged_by",
                "updated_at",
            ]
        )

        # 8. Save updated target customer
        target.save(
            update_fields=[
                "aliases",
                "secondary_phones",
                "credit_limit_bdt",
                "is_credit_approved",
                "credit_status",
                "credit_approved_by",
                "credit_approved_at",
                "credit_terms_days",
                "notes",
                "updated_at",
            ]
        )

        # 9. Compute cryptographic SHA256 integrity hash
        hash_payload = {
            "merge_id": merge_id,
            "tenant_id": str(tenant.tenant_id),
            "source_customer_id": source.customer_id,
            "target_customer_id": target.customer_id,
            "transferred_due_bdt": f"{transferred_due:.2f}",
            "merged_by_id": str(getattr(user, "user_id", user.pk)),
            "reason": clean_reason,
        }
        canonical_str = json.dumps(hash_payload, sort_keys=True, separators=(",", ":"))
        sha_hash = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()

        # 10. Record CustomerMergeAudit
        audit = CustomerMergeAudit.objects.create(
            merge_id=merge_id,
            tenant=tenant,
            branch=branch or target.primary_branch,
            source_customer=source,
            target_customer=target,
            transferred_due_bdt=transferred_due,
            source_snapshot=source_snap,
            target_snapshot=target_snap,
            relinked_records_summary=relinked_summary,
            merge_reason=clean_reason,
            merged_by=user,
            sha256_hash=sha_hash,
        )

        return cls._serialize_merge_result(audit, is_idempotent_replay=False)

    @classmethod
    def _serialize_merge_result(cls, audit: CustomerMergeAudit, is_idempotent_replay: bool = False) -> Dict[str, Any]:
        return {
            "merge_id": audit.merge_id,
            "tenant_id": str(audit.tenant_id),
            "branch_id": str(audit.branch_id or ""),
            "source_customer": {
                "customer_id": audit.source_customer_id,
                "full_name": audit.source_snapshot.get("full_name", ""),
                "phone": audit.source_snapshot.get("phone", ""),
                "is_archived": True,
                "merged_into_id": audit.target_customer_id,
            },
            "target_customer": {
                "customer_id": audit.target_customer_id,
                "full_name": audit.target_snapshot.get("full_name", ""),
                "phone": audit.target_snapshot.get("phone", ""),
                "credit_limit_bdt": f"{audit.target_customer.credit_limit_bdt:.2f}",
                "current_due_bdt": f"{audit.target_customer.current_due_bdt:.2f}",
                "aliases": list(audit.target_customer.aliases or []),
                "secondary_phones": list(audit.target_customer.secondary_phones or []),
            },
            "transferred_due_bdt": f"{audit.transferred_due_bdt:.2f}",
            "relinked_records_summary": audit.relinked_records_summary,
            "merge_reason": audit.merge_reason,
            "merged_by_email": audit.merged_by.email if audit.merged_by else "",
            "merged_at": audit.merged_at.isoformat() if audit.merged_at else None,
            "sha256_hash": audit.sha256_hash,
            "is_idempotent_replay": is_idempotent_replay,
        }

    @classmethod
    def get_merge_history(
        cls,
        *,
        user: Any,
        customer_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Retrieves audited merge events within active tenant scope."""
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=branch_id, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )

        qs = CustomerMergeAudit.objects.filter(tenant=tenant).select_related(
            "source_customer", "target_customer", "merged_by", "branch"
        )
        if customer_id:
            qs = qs.filter(Q(source_customer_id=customer_id) | Q(target_customer_id=customer_id))

        results = qs.order_by("-merged_at")[:limit]
        return [cls._serialize_merge_result(a) for a in results]
