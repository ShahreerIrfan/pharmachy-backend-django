"""
backend/apps/core/customer_loyalty_service.py
Architecture Decision: ADR-0270 (P270).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service for:
1. Optional customer loyalty points accrual and redemption.
2. Subscription plan gating (Standard / Pro tier entitlement via CUSTOMER_LOYALTY).
3. Customer privacy consent respect:
   - Does not award or redeem points if customer has not opted in or has revoked promotional consent.
4. Mandatory DGDA / healthcare regulatory safety exclusions:
   - Prescription medicines, antibiotics, Schedule-G narcotics, and controlled items are
     STRICTLY EXCLUDED from loyalty earning and promotional discounts.
5. Multi-tiered membership (Bronze, Silver, Gold, Platinum).
6. Immutable double-entry points ledger (CustomerLoyaltyLedgerEntry) with SHA-256 integrity hashes.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import math
from typing import Any, Dict, List, Optional
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    CustomerLoyaltyEntryType,
    CustomerLoyaltyLedgerEntry,
    CustomerLoyaltyProgramConfig,
    CustomerLoyaltyTier,
    MedicineProduct,
    PosCustomer,
    PosSaleOrder,
    PosSaleReturn,
    Tenant,
)
from apps.core.pos_domain_service import PosDomainService, q2

MONEY_ZERO = Decimal("0.00")


class CustomerLoyaltyService:
    """Domain service managing customer loyalty points, tiered offers, and safety exclusions."""

    @classmethod
    def get_or_create_config(cls, tenant: Tenant) -> CustomerLoyaltyProgramConfig:
        """Retrieves or creates default tenant loyalty configuration."""
        config, _ = CustomerLoyaltyProgramConfig.objects.get_or_create(
            tenant=tenant,
            defaults={
                "is_enabled": True,
                "earn_rate_bdt_per_point": Decimal("100.00"),
                "redemption_rate_bdt_per_point": Decimal("1.00"),
                "min_points_to_redeem": 10,
                "max_redemption_percent_per_sale": 50,
                "silver_min_spend_bdt": Decimal("5000.00"),
                "gold_min_spend_bdt": Decimal("20000.00"),
                "platinum_min_spend_bdt": Decimal("50000.00"),
                "exclude_prescription_drugs": True,
                "exclude_antibiotics": True,
                "exclude_narcotics": True,
            },
        )
        return config

    @classmethod
    def get_customer_loyalty_profile(
        cls,
        *,
        user: Any,
        customer_id: str,
        branch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Returns customer loyalty balance, current tier, and program parameters."""
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=branch_id, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        PosDomainService.assert_feature_entitlement(tenant, "CUSTOMER_LOYALTY", "customer loyalty program")

        customer = PosCustomer.objects.filter(tenant=tenant, customer_id=customer_id).first()
        if not customer:
            raise NotFound(
                {
                    "error_code": "ERR_CUSTOMER_NOT_FOUND",
                    "customer_id": f"Customer '{customer_id}' was not found in active tenant scope.",
                }
            )

        config = cls.get_or_create_config(tenant)

        # Check privacy preferences
        has_consent = bool(
            customer.is_loyalty_enrolled
            and (customer.privacy_preferences or {}).get("promotional_campaigns", "GRANTED") != "REVOKED"
        )

        redemption_value_bdt = q2(Decimal(customer.loyalty_points_balance) * config.redemption_rate_bdt_per_point)

        return {
            "customer_id": customer.customer_id,
            "full_name": customer.full_name,
            "loyalty_tier": customer.loyalty_tier,
            "points_balance": customer.loyalty_points_balance,
            "redemption_value_bdt": f"{redemption_value_bdt:.2f}",
            "lifetime_points_earned": customer.lifetime_loyalty_points_earned,
            "lifetime_spend_bdt": f"{customer.lifetime_loyalty_spend_bdt:.2f}",
            "is_enrolled": customer.is_loyalty_enrolled,
            "has_promotional_consent": has_consent,
            "program_config": {
                "is_enabled": config.is_enabled,
                "earn_rate_bdt_per_point": f"{config.earn_rate_bdt_per_point:.2f}",
                "redemption_rate_bdt_per_point": f"{config.redemption_rate_bdt_per_point:.2f}",
                "min_points_to_redeem": config.min_points_to_redeem,
                "max_redemption_percent_per_sale": config.max_redemption_percent_per_sale,
                "safety_exclusions": {
                    "prescription_drugs_excluded": config.exclude_prescription_drugs,
                    "antibiotics_excluded": config.exclude_antibiotics,
                    "narcotics_excluded": config.exclude_narcotics,
                },
            },
        }

    @classmethod
    def calculate_spend_breakdown(
        cls,
        *,
        config: CustomerLoyaltyProgramConfig,
        cart_items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Splits cart spend into eligible spend and regulatory excluded spend.
        Prescription medicines and antibiotics cannot earn or redeem loyalty points under DGDA guidelines.
        """
        eligible_spend = MONEY_ZERO
        excluded_spend = MONEY_ZERO
        item_breakdown = []

        for item in cart_items:
            line_total = q2(item.get("line_total_bdt") or item.get("total_amount_bdt", "0.00"))
            is_rx = bool(item.get("is_prescription_required") or item.get("requires_prescription"))
            is_antibiotic = bool(item.get("is_antibiotic"))
            is_narcotic = bool(item.get("is_narcotic") or item.get("is_schedule_g"))

            is_excluded = False
            reasons = []
            if config.exclude_prescription_drugs and is_rx:
                is_excluded = True
                reasons.append("PRESCRIPTION_MEDICINE")
            if config.exclude_antibiotics and is_antibiotic:
                is_excluded = True
                reasons.append("ANTIBIOTIC_MEDICINE")
            if config.exclude_narcotics and is_narcotic:
                is_excluded = True
                reasons.append("SCHEDULE_G_NARCOTIC")

            if is_excluded:
                excluded_spend += line_total
                item_breakdown.append(
                    {
                        "product_name": item.get("product_name", "Item"),
                        "amount_bdt": f"{line_total:.2f}",
                        "is_eligible": False,
                        "exclusion_reasons": reasons,
                    }
                )
            else:
                eligible_spend += line_total
                item_breakdown.append(
                    {
                        "product_name": item.get("product_name", "Item"),
                        "amount_bdt": f"{line_total:.2f}",
                        "is_eligible": True,
                        "exclusion_reasons": [],
                    }
                )

        earn_rate = config.earn_rate_bdt_per_point
        points_earnable = int(math.floor(eligible_spend / earn_rate)) if earn_rate > MONEY_ZERO else 0

        return {
            "eligible_spend_bdt": eligible_spend,
            "excluded_spend_bdt": excluded_spend,
            "points_earnable": points_earnable,
            "items": item_breakdown,
        }

    @classmethod
    @transaction.atomic
    def award_points_for_sale(
        cls,
        *,
        user: Any,
        customer_id: str,
        sale: PosSaleOrder,
        cart_items: List[Dict[str, Any]],
        branch_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Awards loyalty points on an eligible completed sale, strictly respecting
        privacy consent and statutory safety exclusions.
        """
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=branch_id, require_write=True, allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES
        )
        if not PosDomainService.has_feature_entitlement(tenant, "CUSTOMER_LOYALTY"):
            return None

        customer = PosCustomer.objects.select_for_update().filter(tenant=tenant, customer_id=customer_id).first()
        if not customer or not customer.is_loyalty_enrolled:
            return None

        # Check privacy consent
        if (customer.privacy_preferences or {}).get("promotional_campaigns") == "REVOKED":
            return None

        config = cls.get_or_create_config(tenant)
        if not config.is_enabled:
            return None

        breakdown = cls.calculate_spend_breakdown(config=config, cart_items=cart_items)
        points_to_award = breakdown["points_earnable"]
        if points_to_award <= 0:
            return None

        # Update customer balance and spend
        customer.loyalty_points_balance += points_to_award
        customer.lifetime_loyalty_points_earned += points_to_award
        customer.lifetime_loyalty_spend_bdt += breakdown["eligible_spend_bdt"]

        # Evaluate tier advancement
        cls._evaluate_and_update_tier(customer, config)
        customer.save()

        # Record immutable ledger entry
        ref_no = f"EARN-{sale.order_number or sale.sale_id}"
        entry_id = f"lp-{uuid.uuid4().hex[:12]}"
        sha_hash = cls._compute_entry_hash(
            entry_id=entry_id,
            tenant_id=str(tenant.tenant_id),
            customer_id=customer.customer_id,
            entry_type=CustomerLoyaltyEntryType.POINTS_EARNED,
            points_credit=points_to_award,
            balance_after=customer.loyalty_points_balance,
            reference_number=ref_no,
        )

        entry = CustomerLoyaltyLedgerEntry.objects.create(
            entry_id=entry_id,
            tenant=tenant,
            branch=branch or customer.primary_branch,
            customer=customer,
            entry_type=CustomerLoyaltyEntryType.POINTS_EARNED,
            points_debit=0,
            points_credit=points_to_award,
            balance_after=customer.loyalty_points_balance,
            monetary_equivalent_bdt=q2(Decimal(points_to_award) * config.redemption_rate_bdt_per_point),
            eligible_spend_bdt=breakdown["eligible_spend_bdt"],
            excluded_spend_bdt=breakdown["excluded_spend_bdt"],
            source_sale=sale,
            reference_number=ref_no,
            narration=f"Loyalty points earned on sale {sale.order_number}",
            sha256_hash=sha_hash,
        )

        return {
            "entry_id": entry.entry_id,
            "points_awarded": points_to_award,
            "new_balance": customer.loyalty_points_balance,
            "tier": customer.loyalty_tier,
            "eligible_spend_bdt": f"{breakdown['eligible_spend_bdt']:.2f}",
            "excluded_spend_bdt": f"{breakdown['excluded_spend_bdt']:.2f}",
        }

    @classmethod
    @transaction.atomic
    def redeem_loyalty_points(
        cls,
        *,
        user: Any,
        customer_id: str,
        points_to_redeem: int,
        cart_items: List[Dict[str, Any]],
        sale: Optional[PosSaleOrder] = None,
        branch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Redeems points as an authorized discount on eligible cart lines.
        Redemption against restricted medicines is strictly prohibited.
        """
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=branch_id, require_write=True, allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES
        )
        PosDomainService.assert_feature_entitlement(tenant, "CUSTOMER_LOYALTY", "loyalty points redemption")

        if points_to_redeem <= 0:
            raise ValidationError({"points_to_redeem": "Points to redeem must be greater than zero."})

        customer = PosCustomer.objects.select_for_update().filter(tenant=tenant, customer_id=customer_id).first()
        if not customer:
            raise NotFound({"error_code": "ERR_CUSTOMER_NOT_FOUND", "customer_id": "Customer not found."})

        if not customer.is_loyalty_enrolled:
            raise ValidationError(
                {
                    "error_code": "ERR_CUSTOMER_NOT_ENROLLED",
                    "customer_id": "Customer is not enrolled in the loyalty points program.",
                }
            )

        if (customer.privacy_preferences or {}).get("promotional_campaigns") == "REVOKED":
            raise ValidationError(
                {
                    "error_code": "ERR_LOYALTY_CONSENT_REVOKED",
                    "customer_id": "Customer has revoked promotional/loyalty program consent.",
                }
            )

        config = cls.get_or_create_config(tenant)
        if not config.is_enabled:
            raise ValidationError({"error_code": "ERR_LOYALTY_DISABLED", "message": "Loyalty program is disabled."})

        if points_to_redeem < config.min_points_to_redeem:
            raise ValidationError(
                {
                    "error_code": "ERR_MIN_POINTS_THRESHOLD",
                    "points_to_redeem": f"Minimum {config.min_points_to_redeem} points required for redemption.",
                }
            )

        if points_to_redeem > customer.loyalty_points_balance:
            raise ValidationError(
                {
                    "error_code": "ERR_INSUFFICIENT_LOYALTY_POINTS",
                    "points_to_redeem": f"Insufficient points. Available: {customer.loyalty_points_balance}, requested: {points_to_redeem}.",
                }
            )

        # Safety check: Calculate eligible spend from non-restricted items
        breakdown = cls.calculate_spend_breakdown(config=config, cart_items=cart_items)
        eligible_spend = breakdown["eligible_spend_bdt"]
        if eligible_spend <= MONEY_ZERO:
            raise ValidationError(
                {
                    "error_code": "ERR_NO_ELIGIBLE_ITEMS",
                    "message": "Cart contains only restricted prescription/antibiotic items. Loyalty redemption is forbidden.",
                }
            )

        discount_bdt = q2(Decimal(points_to_redeem) * config.redemption_rate_bdt_per_point)
        max_allowed_discount = q2((eligible_spend * Decimal(config.max_redemption_percent_per_sale)) / Decimal("100.00"))

        if discount_bdt > max_allowed_discount:
            max_points = int(math.floor(max_allowed_discount / config.redemption_rate_bdt_per_point))
            raise ValidationError(
                {
                    "error_code": "ERR_MAX_REDEMPTION_EXCEEDED",
                    "discount_bdt": f"Redemption discount {discount_bdt:.2f} BDT exceeds max allowed {max_allowed_discount:.2f} BDT ({config.max_redemption_percent_per_sale}% of eligible spend). Maximum redeemable points: {max_points}.",
                }
            )

        # Deduct points atomically
        customer.loyalty_points_balance -= points_to_redeem
        customer.save(update_fields=["loyalty_points_balance", "updated_at"])

        ref_no = f"REDEEM-{sale.order_number if sale else uuid.uuid4().hex[:8]}"
        entry_id = f"lp-{uuid.uuid4().hex[:12]}"
        sha_hash = cls._compute_entry_hash(
            entry_id=entry_id,
            tenant_id=str(tenant.tenant_id),
            customer_id=customer.customer_id,
            entry_type=CustomerLoyaltyEntryType.POINTS_REDEEMED,
            points_debit=points_to_redeem,
            balance_after=customer.loyalty_points_balance,
            reference_number=ref_no,
        )

        entry = CustomerLoyaltyLedgerEntry.objects.create(
            entry_id=entry_id,
            tenant=tenant,
            branch=branch or customer.primary_branch,
            customer=customer,
            entry_type=CustomerLoyaltyEntryType.POINTS_REDEEMED,
            points_debit=points_to_redeem,
            points_credit=0,
            balance_after=customer.loyalty_points_balance,
            monetary_equivalent_bdt=discount_bdt,
            eligible_spend_bdt=eligible_spend,
            excluded_spend_bdt=breakdown["excluded_spend_bdt"],
            source_sale=sale,
            reference_number=ref_no,
            narration=f"Points redeemed for {discount_bdt:.2f} BDT discount",
            sha256_hash=sha_hash,
        )

        return {
            "entry_id": entry.entry_id,
            "points_redeemed": points_to_redeem,
            "discount_amount_bdt": f"{discount_bdt:.2f}",
            "new_points_balance": customer.loyalty_points_balance,
            "sha256_hash": sha_hash,
        }

    @classmethod
    def _evaluate_and_update_tier(cls, customer: PosCustomer, config: CustomerLoyaltyProgramConfig) -> None:
        """Promotes customer tier based on cumulative eligible spend."""
        spend = customer.lifetime_loyalty_spend_bdt
        if spend >= config.platinum_min_spend_bdt:
            customer.loyalty_tier = CustomerLoyaltyTier.PLATINUM
        elif spend >= config.gold_min_spend_bdt:
            customer.loyalty_tier = CustomerLoyaltyTier.GOLD
        elif spend >= config.silver_min_spend_bdt:
            customer.loyalty_tier = CustomerLoyaltyTier.SILVER
        else:
            customer.loyalty_tier = CustomerLoyaltyTier.BRONZE

    @classmethod
    def _compute_entry_hash(
        cls,
        *,
        entry_id: str,
        tenant_id: str,
        customer_id: str,
        entry_type: str,
        points_credit: int = 0,
        points_debit: int = 0,
        balance_after: int,
        reference_number: str,
    ) -> str:
        payload = {
            "entry_id": entry_id,
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "entry_type": entry_type,
            "points_credit": points_credit,
            "points_debit": points_debit,
            "balance_after": balance_after,
            "reference_number": reference_number,
        }
        canonical_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()

    @classmethod
    def get_customer_loyalty_ledger(
        cls,
        *,
        user: Any,
        customer_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Returns customer points ledger history."""
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )

        entries = (
            CustomerLoyaltyLedgerEntry.objects.filter(tenant=tenant, customer_id=customer_id)
            .order_by("-created_at")[:limit]
        )

        return [
            {
                "entry_id": e.entry_id,
                "entry_type": e.entry_type,
                "points_credit": e.points_credit,
                "points_debit": e.points_debit,
                "balance_after": e.balance_after,
                "monetary_equivalent_bdt": f"{e.monetary_equivalent_bdt:.2f}",
                "reference_number": e.reference_number,
                "narration": e.narration,
                "created_at": e.created_at.isoformat(),
                "sha256_hash": e.sha256_hash,
            }
            for e in entries
        ]
