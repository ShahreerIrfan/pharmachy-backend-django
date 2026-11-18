"""
backend/apps/core/customer_credit_eligibility_service.py
Architecture Decision: ADR-0264 (P264).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service for:
- Configuring tenant and branch-level customer credit eligibility policies & exposure ceilings.
- Multi-factor eligibility evaluations:
  1. Customer credit facility approval state (APPROVED vs UNAPPROVED/SUSPENDED/PENDING).
  2. Customer overdue aging enforcement (blocking credit if overdue receivables exist).
  3. Individual customer credit limit checks (current_due + additional_due <= credit_limit).
  4. Branch-level cumulative credit exposure ceiling enforcement.
  5. Restricted medicine credit controls (narcotics/controlled and antibiotic dispensing restrictions).
- Role-based authorization gates (Owner/Manager/Finance for policy configuration).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    CustomerCreditEligibilityPolicy,
    CustomerDueRecord,
    DueStatus,
    MedicineProduct,
    PosCustomer,
    PosCustomerCreditStatus,
)
from apps.identity.models import UserRole

MONEY_ZERO = Decimal("0.00")
CREDIT_ADMIN_ROLES = {
    UserRole.SUPERADMIN,
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
}


def q2(val: Any) -> Decimal:
    """Quantize to 2 decimal places safely."""
    if val is None:
        return MONEY_ZERO
    try:
        return Decimal(str(val)).quantize(Decimal("0.01"))
    except Exception:
        return MONEY_ZERO


class CustomerCreditEligibilityService:
    """Evaluates and enforces customer credit eligibility and branch exposure limits."""

    @classmethod
    def get_effective_policy(
        cls,
        tenant: Any,
        branch: Optional[Any] = None,
    ) -> CustomerCreditEligibilityPolicy:
        """
        Resolves the effective credit policy for the branch/tenant.
        Falls back to tenant-level policy or creates default if none exists.
        """
        tenant_id = getattr(tenant, "tenant_id", tenant)
        branch_id = getattr(branch, "branch_id", branch) if branch else None

        # 1. Branch-specific policy
        if branch_id:
            policy = CustomerCreditEligibilityPolicy.objects.filter(
                tenant_id=tenant_id,
                branch_id=branch_id,
                is_active=True,
            ).first()
            if policy:
                return policy

        # 2. Tenant-wide default policy
        policy = CustomerCreditEligibilityPolicy.objects.filter(
            tenant_id=tenant_id,
            branch__isnull=True,
            is_active=True,
        ).first()
        if policy:
            return policy

        # 3. Create default tenant policy
        policy, _ = CustomerCreditEligibilityPolicy.objects.get_or_create(
            tenant_id=tenant_id,
            branch=None,
            defaults={
                "max_customer_credit_limit_bdt": Decimal("10000.00"),
                "branch_credit_ceiling_bdt": Decimal("200000.00"),
                "block_credit_if_overdue": True,
                "allow_credit_for_narcotics": False,
                "allow_credit_for_antibiotics": True,
                "is_active": True,
            },
        )
        return policy

    @classmethod
    @transaction.atomic
    def configure_policy(
        cls,
        user: Any,
        tenant: Any,
        data: Dict[str, Any],
        branch: Optional[Any] = None,
    ) -> CustomerCreditEligibilityPolicy:
        """
        Creates or updates a credit policy for tenant or branch.
        Requires Owner, Manager, or Finance role.
        """
        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in CREDIT_ADMIN_ROLES:
            raise PermissionDenied({
                "error_code": "ERR_PERMISSION_DENIED",
                "message_en": "Only Pharmacy Owner, Branch Manager, or Finance Officer can configure credit policies.",
                "message_bn": "শুধুমাত্র ফার্মেসি মালিক, শাখা ব্যবস্থাপক বা ফাইন্যান্স অফিসার ক্রেডিট পলিসি পরিবর্তন করতে পারেন।",
            })

        tenant_id = getattr(tenant, "tenant_id", tenant)
        branch_id = getattr(branch, "branch_id", branch) if branch else None

        policy = CustomerCreditEligibilityPolicy.objects.select_for_update().filter(
            tenant_id=tenant_id,
            branch_id=branch_id,
        ).first()

        if not policy:
            policy = CustomerCreditEligibilityPolicy(
                tenant_id=tenant_id,
                branch_id=branch_id,
            )

        if "max_customer_credit_limit_bdt" in data:
            val = q2(data["max_customer_credit_limit_bdt"])
            if val < MONEY_ZERO:
                raise ValidationError({"max_customer_credit_limit_bdt": "Limit cannot be negative."})
            policy.max_customer_credit_limit_bdt = val

        if "branch_credit_ceiling_bdt" in data:
            val = q2(data["branch_credit_ceiling_bdt"])
            if val < MONEY_ZERO:
                raise ValidationError({"branch_credit_ceiling_bdt": "Ceiling cannot be negative."})
            policy.branch_credit_ceiling_bdt = val

        if "block_credit_if_overdue" in data:
            policy.block_credit_if_overdue = bool(data["block_credit_if_overdue"])

        if "allow_credit_for_narcotics" in data:
            policy.allow_credit_for_narcotics = bool(data["allow_credit_for_narcotics"])

        if "allow_credit_for_antibiotics" in data:
            policy.allow_credit_for_antibiotics = bool(data["allow_credit_for_antibiotics"])

        if "is_active" in data:
            policy.is_active = bool(data["is_active"])

        policy.updated_by = user
        policy.save()
        return policy

    @classmethod
    def calculate_branch_credit_exposure(cls, tenant: Any, branch: Optional[Any] = None) -> Decimal:
        """Calculates total outstanding due balances for branch or tenant."""
        tenant_id = getattr(tenant, "tenant_id", tenant)
        branch_id = getattr(branch, "branch_id", branch) if branch else None

        qs = CustomerDueRecord.objects.filter(
            tenant_id=tenant_id,
            status__in=[DueStatus.OUTSTANDING, DueStatus.PARTIAL],
        )
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        total = qs.aggregate(s=Sum("remaining_due_bdt"))["s"]
        return q2(total) if total is not None else MONEY_ZERO

    @classmethod
    def check_customer_overdue(cls, customer: PosCustomer) -> Tuple[bool, Decimal]:
        """
        Determines whether customer has overdue unpaid dues based on due_date
        or credit_terms_days aging.
        """
        now = timezone.now()
        today = now.date()
        terms_days = customer.credit_terms_days or 30

        unpaid_dues = CustomerDueRecord.objects.filter(
            tenant=customer.tenant,
            customer=customer,
            status__in=[DueStatus.OUTSTANDING, DueStatus.PARTIAL],
            remaining_due_bdt__gt=MONEY_ZERO,
        )

        overdue_total = MONEY_ZERO
        has_overdue = False

        for due in unpaid_dues:
            is_due_overdue = False
            if due.due_date and due.due_date < today:
                is_due_overdue = True
            elif due.created_at:
                age_days = (now - due.created_at).days
                if age_days > terms_days:
                    is_due_overdue = True

            if is_due_overdue:
                has_overdue = True
                overdue_total += due.remaining_due_bdt

        return has_overdue, q2(overdue_total)

    @classmethod
    def evaluate_credit_eligibility(
        cls,
        user: Any,
        tenant: Any,
        customer_id: str,
        *,
        branch: Optional[Any] = None,
        additional_due_bdt: Decimal = MONEY_ZERO,
        cart_lines: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Comprehensive evaluation of customer credit eligibility across all dimensions:
        - Customer existence and status
        - Individual credit limit and available balance
        - Overdue aging blockage
        - Branch exposure ceiling
        - Restricted product restrictions (narcotics/antibiotics)
        """
        tenant_id = getattr(tenant, "tenant_id", tenant)
        branch_obj = None
        if branch:
            branch_id = getattr(branch, "branch_id", branch)
            branch_obj = Branch.objects.filter(tenant_id=tenant_id, branch_id=branch_id).first()
            if not branch_obj:
                raise ValidationError({"branch": f"Branch '{branch_id}' does not exist for tenant."})

        # Fetch customer
        try:
            customer = PosCustomer.objects.select_related("primary_branch").get(
                tenant_id=tenant_id,
                customer_id=customer_id,
            )
        except PosCustomer.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_CUSTOMER_NOT_FOUND",
                "customer": f"Customer '{customer_id}' not found in active tenant.",
            })

        policy = cls.get_effective_policy(tenant, branch=branch_obj)
        additional_due = q2(additional_due_bdt)
        rejections: List[Dict[str, str]] = []

        # 1. Active & Archived check
        if customer.is_archived or not customer.is_active:
            rejections.append({
                "code": "ERR_CUSTOMER_INACTIVE_OR_ARCHIVED",
                "message_en": f"Customer '{customer.full_name}' is inactive or archived.",
                "message_bn": f"গ্রাহক '{customer.full_name}' নিষ্ক্রিয় বা আর্কাইভ করা হয়েছে।",
            })

        # 2. Suspension check
        if customer.credit_status == PosCustomerCreditStatus.SUSPENDED:
            reason = f": {customer.credit_suspended_reason}" if customer.credit_suspended_reason else ""
            rejections.append({
                "code": "ERR_CUSTOMER_CREDIT_SUSPENDED",
                "message_en": f"Customer credit facility is suspended{reason}.",
                "message_bn": f"গ্রাহকের ক্রেডিট সুবিধা স্থগিত করা হয়েছে{reason}।",
            })

        # 3. Approval status check
        if not customer.is_credit_approved or customer.credit_status != PosCustomerCreditStatus.APPROVED:
            rejections.append({
                "code": "ERR_CUSTOMER_CREDIT_NOT_APPROVED",
                "message_en": f"Customer '{customer.full_name}' is not approved for credit/due sales.",
                "message_bn": f"গ্রাহক '{customer.full_name}' ক্রেডিট বিক্রয়ের জন্য অনুমোদিত নন।",
            })

        # 4. Overdue aging check
        has_overdue, overdue_amount = cls.check_customer_overdue(customer)
        if policy.block_credit_if_overdue and has_overdue:
            rejections.append({
                "code": "ERR_CUSTOMER_HAS_OVERDUE_DUES",
                "message_en": f"Customer has overdue unpaid dues totaling {overdue_amount:.2f} BDT past terms.",
                "message_bn": f"গ্রাহকের {overdue_amount:.2f} টাকা বকেয়া নির্ধারিত সময়সীমা অতিক্রম করেছে।",
            })

        # 5. Customer individual credit limit check
        projected_customer_due = q2(customer.current_due_bdt + additional_due)
        available_credit = max(MONEY_ZERO, q2(customer.credit_limit_bdt - customer.current_due_bdt))

        if customer.credit_limit_bdt <= MONEY_ZERO or projected_customer_due > customer.credit_limit_bdt:
            rejections.append({
                "code": "ERR_CUSTOMER_CREDIT_LIMIT_EXCEEDED",
                "message_en": (
                    f"Projected due {projected_customer_due:.2f} BDT exceeds approved limit {customer.credit_limit_bdt:.2f} BDT "
                    f"(available {available_credit:.2f} BDT)."
                ),
                "message_bn": (
                    f"প্রত্যাশিত বকেয়া {projected_customer_due:.2f} টাকা অনুমোদিত সীমা {customer.credit_limit_bdt:.2f} টাকা অতিক্রম করেছে।"
                ),
            })

        # 6. Branch exposure ceiling check
        current_branch_exposure = cls.calculate_branch_credit_exposure(tenant, branch=branch_obj)
        projected_branch_exposure = q2(current_branch_exposure + additional_due)

        if projected_branch_exposure > policy.branch_credit_ceiling_bdt:
            rejections.append({
                "code": "ERR_BRANCH_CREDIT_CEILING_EXCEEDED",
                "message_en": (
                    f"Branch credit exposure would reach {projected_branch_exposure:.2f} BDT, exceeding branch ceiling {policy.branch_credit_ceiling_bdt:.2f} BDT."
                ),
                "message_bn": (
                    f"শাখার মোট ক্রেডিট এক্সপোজার {projected_branch_exposure:.2f} টাকা হবে, যা সিলিং {policy.branch_credit_ceiling_bdt:.2f} টাকা অতিক্রম করবে।"
                ),
            })

        # 7. Restricted medicines check
        restricted_items_found = []
        if cart_lines:
            product_ids = [line.get("product_id") or line.get("product") for line in cart_lines if line.get("product_id") or line.get("product")]
            if product_ids:
                products = {p.product_id: p for p in MedicineProduct.objects.filter(product_id__in=product_ids)}
                for line in cart_lines:
                    pid = line.get("product_id") or line.get("product")
                    prod = products.get(pid)
                    if not prod:
                        continue
                    if getattr(prod, "is_narcotic", False) and not policy.allow_credit_for_narcotics:
                        restricted_items_found.append({
                            "product_id": prod.product_id,
                            "product_name": prod.brand_name,
                            "restriction_type": "NARCOTIC_RESTRICTED",
                            "reason": "Narcotic/controlled medicine cannot be dispensed on credit.",
                        })
                    if getattr(prod, "is_antibiotic", False) and not policy.allow_credit_for_antibiotics:
                        restricted_items_found.append({
                            "product_id": prod.product_id,
                            "product_name": prod.brand_name,
                            "restriction_type": "ANTIBIOTIC_RESTRICTED",
                            "reason": "Antibiotic medicine cannot be dispensed on credit under branch policy.",
                        })

        if restricted_items_found:
            rejections.append({
                "code": "ERR_RESTRICTED_MEDICINE_CREDIT_FORBIDDEN",
                "message_en": f"Sale contains {len(restricted_items_found)} restricted item(s) forbidden for credit sales.",
                "message_bn": f"বিক্রয়ে {len(restricted_items_found)}টি নিয়ন্ত্রিত ঔষধ রয়েছে যা বাকিতে বিক্রয় নিষিদ্ধ।",
            })

        is_eligible = len(rejections) == 0

        return {
            "is_eligible": is_eligible,
            "customer_id": customer.customer_id,
            "full_name": customer.full_name,
            "phone": customer.phone,
            "credit_status": customer.credit_status,
            "credit_terms_days": customer.credit_terms_days,
            "credit_limit_bdt": f"{customer.credit_limit_bdt:.2f}",
            "current_due_bdt": f"{customer.current_due_bdt:.2f}",
            "available_credit_bdt": f"{available_credit:.2f}",
            "additional_due_requested_bdt": f"{additional_due:.2f}",
            "projected_due_bdt": f"{projected_customer_due:.2f}",
            "has_overdue_dues": has_overdue,
            "overdue_amount_bdt": f"{overdue_amount:.2f}",
            "branch_id": branch_obj.branch_id if branch_obj else None,
            "branch_credit_ceiling_bdt": f"{policy.branch_credit_ceiling_bdt:.2f}",
            "current_branch_exposure_bdt": f"{current_branch_exposure:.2f}",
            "projected_branch_exposure_bdt": f"{projected_branch_exposure:.2f}",
            "policy": {
                "policy_id": policy.policy_id,
                "max_customer_credit_limit_bdt": f"{policy.max_customer_credit_limit_bdt:.2f}",
                "branch_credit_ceiling_bdt": f"{policy.branch_credit_ceiling_bdt:.2f}",
                "block_credit_if_overdue": policy.block_credit_if_overdue,
                "allow_credit_for_narcotics": policy.allow_credit_for_narcotics,
                "allow_credit_for_antibiotics": policy.allow_credit_for_antibiotics,
            },
            "restricted_items": restricted_items_found,
            "rejections": rejections,
        }

    @classmethod
    def assert_sale_credit_eligible(
        cls,
        user: Any,
        tenant: Any,
        customer_id: str,
        *,
        branch: Optional[Any] = None,
        additional_due_bdt: Decimal = MONEY_ZERO,
        cart_lines: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Hard enforcement assertion for checkout.
        Raises ValidationError if customer is not credit eligible.
        """
        eval_result = cls.evaluate_credit_eligibility(
            user,
            tenant,
            customer_id,
            branch=branch,
            additional_due_bdt=additional_due_bdt,
            cart_lines=cart_lines,
        )
        if not eval_result["is_eligible"]:
            first_err = eval_result["rejections"][0]
            raise ValidationError({
                "error_code": first_err["code"],
                "detail": first_err["message_en"],
                "detail_bn": first_err["message_bn"],
                "rejections": eval_result["rejections"],
            })
        return eval_result
