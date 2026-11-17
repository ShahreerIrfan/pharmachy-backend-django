"""
backend/apps/core/walk_in_sales_service.py
Architecture Decision: ADR-0262 (P262).
Phase 14: Customers, Suppliers, Dues & Payments.

Domain service managing anonymous cash walk-in sales with minimal identifiers:
- Zero unnecessary personal data collection for OTC counter purchases.
- Strict prohibition of credit/due sales for anonymous walk-in customers.
- Pseudonymous token generation for walk-in prescription purchases.
- Clean cash clearing ledger reconciliation without AR customer ledger pollution.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    PaymentMethod,
    PosCustomer,
    PosPackagingUnit,
    PosRegulatoryClass,
    PosRxGateStatus,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleOrderLine,
    PosSaleSourceChannel,
    SaleInvoice,
    Tenant,
)
from apps.core.pos_domain_service import PosDomainService

logger = logging.getLogger(__name__)


class WalkInSalesService:
    """
    Service enforcing privacy-first anonymous walk-in retail sales at pharmacy counters.
    """

    DEFAULT_CUSTOMER_NAME = "Walk-in Customer"
    ALLOWED_POS_ROLES = PosDomainService.POS_SALE_WRITE_ROLES

    @classmethod
    def sanitize_customer_identifiers(
        cls,
        customer_name: Optional[str] = None,
        customer_phone: Optional[str] = None,
        is_anonymous: bool = True,
    ) -> Dict[str, str]:
        """
        Normalizes walk-in customer identifiers, eliminating unwanted personal data
        unless explicitly provided for an e-receipt request.
        """
        if is_anonymous:
            return {
                "customer_name": cls.DEFAULT_CUSTOMER_NAME,
                "customer_phone": "",
            }

        name = str(customer_name or "").strip() or cls.DEFAULT_CUSTOMER_NAME
        phone = str(customer_phone or "").strip()
        return {
            "customer_name": name,
            "customer_phone": phone,
        }

    @classmethod
    @transaction.atomic
    def create_walk_in_sale(
        cls,
        user: Any,
        branch_id: Optional[str] = None,
        customer_name: Optional[str] = None,
        customer_phone: Optional[str] = None,
        source_channel: str = PosSaleSourceChannel.DESKTOP_POS_ONLINE,
    ) -> PosSaleOrder:
        """
        Initializes an anonymous cash walk-in sale order without forcing personal identifiers.
        """
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=branch_id,
            require_write=True,
            allowed_roles=cls.ALLOWED_POS_ROLES,
        )

        sanitized = cls.sanitize_customer_identifiers(
            customer_name=customer_name,
            customer_phone=customer_phone,
            is_anonymous=not bool(customer_phone),
        )

        sale_uid = uuid.uuid4().hex[:12]
        order_no = f"SO-WALKIN-{sale_uid[:8].upper()}"

        sale = PosSaleOrder.objects.create(
            sale_id=f"sale-{sale_uid}",
            tenant=tenant,
            branch=branch,
            cashier_user=user,
            customer=None,  # Explicitly unbound to any PosCustomer profile
            customer_name=sanitized["customer_name"],
            customer_phone=sanitized["customer_phone"],
            order_number=order_no,
            source_channel=source_channel,
            lifecycle_state=PosSaleLifecycleState.DRAFT,
            rx_gate_status=PosRxGateStatus.NOT_REQUIRED,
            subtotal_bdt=Decimal("0.00"),
            discount_amount_bdt=Decimal("0.00"),
            tax_amount_bdt=Decimal("0.00"),
            rounding_adjustment_bdt=Decimal("0.00"),
            grand_total_bdt=Decimal("0.00"),
            paid_amount_bdt=Decimal("0.00"),
            due_amount_bdt=Decimal("0.00"),
            created_by=user,
            updated_by=user,
        )

        return sale

    @classmethod
    def validate_tender_eligibility(
        cls,
        sale: PosSaleOrder,
        payment_method: str,
        amount: Decimal,
    ) -> None:
        """
        Validates that anonymous walk-in sales CANNOT use credit/due tender.
        Credit sales strictly require an approved, registered PosCustomer account.
        """
        method_str = str(payment_method).upper().strip()
        if method_str in ("DUE", PaymentMethod.DUE) and not sale.customer_id:
            raise ValidationError(
                {
                    "error_code": "ERR_ANONYMOUS_CREDIT_FORBIDDEN",
                    "message_en": "Credit/due sales are strictly forbidden for anonymous walk-in customers. An approved customer profile must be attached.",
                    "message_bn": "বেনামী ওয়াক-ইন ক্রেতাদের জন্য বাকিতে বিক্রি সম্পূর্ণ নিষিদ্ধ। অনুমোদিত কাস্টমার অ্যাকাউন্ট সংযুক্ত করা আবশ্যক।",
                }
            )

    @classmethod
    def generate_walk_in_receipt_metadata(cls, sale: PosSaleOrder) -> Dict[str, Any]:
        """
        Generates minimal, privacy-compliant receipt metadata for cash counter sales.
        """
        return {
            "order_number": sale.order_number,
            "sale_id": sale.sale_id,
            "branch_id": str(sale.branch_id),
            "branch_name": sale.branch.name if sale.branch else "Branch",
            "customer_type": "ANONYMOUS_WALK_IN" if not sale.customer_id else "REGISTERED",
            "customer_name": sale.customer_name or cls.DEFAULT_CUSTOMER_NAME,
            "grand_total_bdt": f"{sale.grand_total_bdt:.2f}",
            "paid_amount_bdt": f"{sale.paid_amount_bdt:.2f}",
            "change_amount_bdt": f"{max(Decimal('0.00'), sale.paid_amount_bdt - sale.grand_total_bdt):.2f}",
            "is_privacy_preserved": bool(not sale.customer_id and not sale.customer_phone),
            "issued_at": timezone.now().isoformat(),
        }

