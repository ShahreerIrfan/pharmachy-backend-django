"""
backend/apps/core/pos_delivery_service.py
Architecture Decision: ADR-0226 (P226).
Phase 12: POS, Sales Orders, Checkout & Returns.

Canonical Sales Preorder & Delivery Fulfillment Service:
- Creates, updates, and soft-archives branch-scoped PosDeliveryOrder records linked 1:1 with PosSaleOrder.
- Coordinates FEFO BatchStock reservations cleanly via PosHeldCartService without phantom or duplicate locks.
- Enforces pharmacist Rx verification (ERR_RX_VERIFICATION_REQUIRED) before packing/dispatching Rx items.
- Enforces FieldVisibilityService patient_pii_visibility masking, audited operational address reveal,
  post-completion/cancellation redaction, and strictly PII-free PosDeliveryAudit logging.
- Supports Prepaid auto-finalization on dispatch and Decimal-exact COD collection + finalization on delivery.
- Supports failed attempt tracking, return-to-branch stock restoration/reversal, and optimistic version checks.
"""

from __future__ import annotations

import datetime
import logging
import re
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import MethodNotAllowed, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.field_visibility_service import FieldVisibilityService
from apps.core.models import (
    BatchStatus,
    MedicineProduct,
    MedicineVariant,
    PaymentMethod,
    PosAllocationStatus,
    PosCustomer,
    PosDeliveryAudit,
    PosDeliveryFulfillmentState,
    PosDeliveryMethod,
    PosDeliveryOrder,
    PosOrderFulfillmentMode,
    PosPackagingUnit,
    PosPaymentAttemptStatus,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleOrderLine,
    PosSaleSourceChannel,
    StaffMember,
)
from apps.core.pos_cart_pricing_service import PosCartPricingService, parse_strict_decimal, q2
from apps.core.pos_cart_unit_service import PosCartUnitSelectionService
from apps.core.pos_domain_service import PosDomainService
from apps.core.pos_held_cart_service import PosHeldCartService, _HoldCartAbort

logger = logging.getLogger("pharmacy.core.pos_delivery")


class DeliveryDomainError(ValidationError):
    """ValidationError preserving native JSON types for structured delivery error details."""

    def __init__(self, payload: Dict[str, Any]):
        self.raw_detail = payload
        super().__init__(payload)


class _DeliveryAbort(Exception):
    """Signals atomic rollback while preserving rejected PosDeliveryAudit entries outside rollback."""

    def __init__(self, audit_kwargs: Dict[str, Any], exc: Exception):
        super().__init__(str(exc))
        self.audit_kwargs = audit_kwargs
        self.exc = exc


class PosDeliveryService:
    """Service governing POS preorders, address privacy, stock reservation, dispatch & delivery fulfillment."""

    MIN_RESERVATION_TTL_MINUTES = 1
    MAX_RESERVATION_TTL_MINUTES = 10080  # Up to 7 days for scheduled pharmacy preorders
    DEFAULT_PREORDER_RESERVATION_TTL_MINUTES = 120

    PREORDER_OR_DELIVERY_MODES = {
        PosOrderFulfillmentMode.PREORDER_PICKUP,
        PosOrderFulfillmentMode.HOME_DELIVERY,
    }

    MUTABLE_DELIVERY_STATES = {
        PosDeliveryFulfillmentState.PREORDER_PENDING,
        PosDeliveryFulfillmentState.STOCK_RESERVED,
        PosDeliveryFulfillmentState.PACKED,
    }

    REVEAL_ALLOWED_STATES = {
        PosDeliveryFulfillmentState.PREORDER_PENDING,
        PosDeliveryFulfillmentState.STOCK_RESERVED,
        PosDeliveryFulfillmentState.PACKED,
        PosDeliveryFulfillmentState.DISPATCHED,
        PosDeliveryFulfillmentState.OUT_FOR_DELIVERY,
    }

    REDACT_ALLOWED_STATES = {
        PosDeliveryFulfillmentState.DELIVERED,
        PosDeliveryFulfillmentState.RETURNED_TO_BRANCH,
        PosDeliveryFulfillmentState.CANCELLED,
    }

    ALLOWED_TRANSITIONS = {
        PosDeliveryFulfillmentState.PREORDER_PENDING: {
            PosDeliveryFulfillmentState.STOCK_RESERVED,
            PosDeliveryFulfillmentState.CANCELLED,
        },
        PosDeliveryFulfillmentState.STOCK_RESERVED: {
            PosDeliveryFulfillmentState.PACKED,
            PosDeliveryFulfillmentState.CANCELLED,
        },
        PosDeliveryFulfillmentState.PACKED: {
            PosDeliveryFulfillmentState.DISPATCHED,
            PosDeliveryFulfillmentState.DELIVERED,  # PREORDER_PICKUP branch counter handover
            PosDeliveryFulfillmentState.CANCELLED,
        },
        PosDeliveryFulfillmentState.DISPATCHED: {
            PosDeliveryFulfillmentState.OUT_FOR_DELIVERY,
            PosDeliveryFulfillmentState.DELIVERED,
            PosDeliveryFulfillmentState.FAILED_ATTEMPT,
            PosDeliveryFulfillmentState.RETURNED_TO_BRANCH,
        },
        PosDeliveryFulfillmentState.OUT_FOR_DELIVERY: {
            PosDeliveryFulfillmentState.DELIVERED,
            PosDeliveryFulfillmentState.FAILED_ATTEMPT,
            PosDeliveryFulfillmentState.RETURNED_TO_BRANCH,
        },
        PosDeliveryFulfillmentState.FAILED_ATTEMPT: {
            PosDeliveryFulfillmentState.OUT_FOR_DELIVERY,
            PosDeliveryFulfillmentState.DISPATCHED,
            PosDeliveryFulfillmentState.RETURNED_TO_BRANCH,
        },
        PosDeliveryFulfillmentState.RETURNED_TO_BRANCH: {
            PosDeliveryFulfillmentState.PACKED,
        },
        PosDeliveryFulfillmentState.DELIVERED: set(),
        PosDeliveryFulfillmentState.CANCELLED: set(),
    }

    COD_ALLOWED_TENDERS = {
        PaymentMethod.CASH,
        PaymentMethod.BKASH,
        PaymentMethod.NAGAD,
        PaymentMethod.CARD,
    }

    @classmethod
    def mask_address_line(cls, raw_address: Optional[str]) -> str:
        """Masks house numbers & street tokens while keeping structural shape for counter clarity."""
        clean = str(raw_address or "").strip()
        if not clean:
            return ""
        if clean.startswith("[REDACTED"):
            return clean
        tokens = clean.split()
        masked: List[str] = []
        for tok in tokens:
            if any(ch.isdigit() for ch in tok):
                masked.append(re.sub(r"[A-Za-z0-9]", "*", tok))
            elif len(tok) <= 2:
                masked.append(tok[0] + "*")
            else:
                masked.append(tok[0] + ("*" * min(4, len(tok) - 1)))
        return " ".join(masked)

    @classmethod
    def evaluate_privacy_payload(cls, user: Any, delivery: PosDeliveryOrder) -> Dict[str, Any]:
        """Resolves viewer PII entitlement and returns privacy-safe recipient & address projection."""
        is_redacted = delivery.address_redacted_at is not None
        if is_redacted:
            return {
                "recipient_name": FieldVisibilityService.mask_person_name(delivery.recipient_name),
                "recipient_phone": FieldVisibilityService.mask_phone_number(delivery.recipient_phone),
                "delivery_address_line": "[REDACTED_AFTER_COMPLETION]",
                "delivery_landmark_notes": "",
                "_address_privacy_masked": True,
                "_pii_masked": True,
            }

        perms = FieldVisibilityService.resolve_field_visibility_profile(
            user, tenant_id=delivery.tenant_id, branch_id=delivery.branch_id
        )
        can_view_pii = bool(perms.get("patient_pii_visibility", False))
        if can_view_pii:
            return {
                "recipient_name": delivery.recipient_name,
                "recipient_phone": delivery.recipient_phone,
                "delivery_address_line": delivery.delivery_address_line,
                "delivery_landmark_notes": delivery.delivery_landmark_notes,
                "_address_privacy_masked": False,
                "_pii_masked": False,
            }

        return {
            "recipient_name": FieldVisibilityService.mask_person_name(delivery.recipient_name),
            "recipient_phone": FieldVisibilityService.mask_phone_number(delivery.recipient_phone),
            "delivery_address_line": cls.mask_address_line(delivery.delivery_address_line),
            "delivery_landmark_notes": "",
            "_address_privacy_masked": True,
            "_pii_masked": True,
        }

    @classmethod
    def parse_strict_version(cls, raw_value: Any) -> Optional[int]:
        return PosHeldCartService.parse_strict_expected_version(raw_value)

    @classmethod
    def parse_strict_reservation_ttl(cls, raw_value: Any, *, default_minutes: int) -> int:
        if raw_value is None or raw_value == "":
            return default_minutes
        if isinstance(raw_value, bool) or isinstance(raw_value, float):
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_INVALID_HOLD_TTL",
                    "reservation_ttl_minutes": "reservation_ttl_minutes must be an integer.",
                    "message_en": "Reservation TTL must be a whole number of minutes (1..10080).",
                    "message_bn": "রিজার্ভেশনের মেয়াদ ১ থেকে ১০০৮০ মিনিটের মধ্যে পূর্ণসংখ্যা হতে হবে।",
                }
            )
        try:
            clean = str(raw_value).strip()
            if not re.fullmatch(r"-?\d+", clean):
                raise ValueError("non-int")
            parsed = int(clean)
        except Exception:
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_INVALID_HOLD_TTL",
                    "reservation_ttl_minutes": "reservation_ttl_minutes must be a valid integer.",
                    "message_en": "Invalid reservation TTL value.",
                    "message_bn": "রিজার্ভেশনের মেয়াদ সঠিক নয়।",
                }
            )
        if parsed < cls.MIN_RESERVATION_TTL_MINUTES or parsed > cls.MAX_RESERVATION_TTL_MINUTES:
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_INVALID_HOLD_TTL",
                    "reservation_ttl_minutes": (
                        f"reservation_ttl_minutes must be between {cls.MIN_RESERVATION_TTL_MINUTES} "
                        f"and {cls.MAX_RESERVATION_TTL_MINUTES}."
                    ),
                    "message_en": "Preorder reservation TTL must be between 1 and 10080 minutes (7 days).",
                    "message_bn": "প্রি-অর্ডার রিজার্ভেশনের মেয়াদ ১ থেকে ১০০৮০ মিনিটের মধ্যে হতে হবে।",
                }
            )
        return parsed

    @classmethod
    def _parse_optional_iso_datetime(cls, raw_val: Any, field_name: str) -> Optional[datetime.datetime]:
        if raw_val is None or raw_val == "":
            return None
        if isinstance(raw_val, datetime.datetime):
            dt = raw_val
        else:
            text = str(raw_val).strip()
            try:
                dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                raise DeliveryDomainError(
                    {
                        "error_code": "ERR_INVALID_DELIVERY_WINDOW",
                        field_name: f"'{field_name}' must be a valid ISO-8601 datetime string.",
                        "message_en": f"Invalid datetime format for '{field_name}'.",
                        "message_bn": f"'{field_name}'-এর তারিখ ও সময় ফরম্যাট সঠিক নয়।",
                    }
                )
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        return dt

    @classmethod
    def _record_delivery_audit(
        cls,
        *,
        tenant: Any,
        branch: Any,
        delivery: Optional[PosDeliveryOrder],
        sale: Optional[PosSaleOrder],
        action: str,
        user: Any,
        from_state: str = "",
        to_state: str = "",
        reason: str = "",
        was_rejected: bool = False,
        rejection_code: str = "",
        rejection_message: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PosDeliveryAudit:
        """Records PII-free immutable delivery audit entry + mirrors into central AuditLogEntry."""
        actor_role = str(getattr(user, "role", "SYSTEM") or "SYSTEM").upper()
        safe_meta = dict(metadata or {})
        # Scrub accidental PII keys if passed into metadata
        for forbidden_key in (
            "delivery_address_line",
            "recipient_phone",
            "recipient_name",
            "delivery_landmark_notes",
        ):
            safe_meta.pop(forbidden_key, None)

        audit = PosDeliveryAudit.objects.create(
            tenant=tenant,
            branch=branch,
            delivery=delivery,
            sale=sale,
            action=action[:40],
            from_state=from_state[:32],
            to_state=to_state[:32],
            reason=str(reason or "")[:255],
            was_rejected=bool(was_rejected),
            rejection_code=str(rejection_code or "")[:64],
            rejection_message=str(rejection_message or "")[:255],
            actor_user=user if getattr(user, "is_authenticated", False) else None,
            actor_role=actor_role[:48],
            metadata_json=safe_meta,
        )
        AuditLogEntry.objects.create(
            tenant_id=tenant.tenant_id if hasattr(tenant, "tenant_id") else str(tenant),
            branch_id=branch.branch_id if hasattr(branch, "branch_id") else str(branch),
            user_id=str(getattr(user, "user_id", "") or getattr(user, "pk", "")),
            actor_email=str(getattr(user, "email", "")),
            actor_role=actor_role,
            request_id=f"dlv-{uuid.uuid4().hex[:10]}",
            action=f"POS_DELIVERY_{action}",
            resource_type="POS_DELIVERY_ORDER",
            resource_id=delivery.delivery_id if delivery else (sale.sale_id if sale else ""),
            status=AuditActionStatus.DENIED if was_rejected else AuditActionStatus.SUCCESS,
            before_state={"fulfillment_state": from_state},
            after_state={"fulfillment_state": to_state, "rejection_code": rejection_code},
            metadata_json=safe_meta,
        )
        return audit

    @classmethod
    def _assert_version_match(cls, delivery: PosDeliveryOrder, expected_version: Optional[int], user: Any, action_tag: str) -> None:
        if expected_version is not None and int(delivery.version) != expected_version:
            raise _DeliveryAbort(
                audit_kwargs={
                    "tenant": delivery.tenant,
                    "branch": delivery.branch,
                    "delivery": delivery,
                    "sale": delivery.sale,
                    "action": action_tag,
                    "from_state": delivery.fulfillment_state,
                    "to_state": delivery.fulfillment_state,
                    "user": user,
                    "was_rejected": True,
                    "rejection_code": "ERR_CART_VERSION_CONFLICT",
                    "rejection_message": f"Expected v{expected_version}, actual v{delivery.version}.",
                },
                exc=DeliveryDomainError(
                    {
                        "error_code": "ERR_CART_VERSION_CONFLICT",
                        "expected_version": expected_version,
                        "actual_version": int(delivery.version),
                        "detail": f"Delivery order changed concurrently (expected v{expected_version}, actual v{delivery.version}).",
                        "message_en": "Delivery order changed elsewhere. Refresh before updating.",
                        "message_bn": "ডেলিভারি অর্ডারটি অন্য কোথাও থেকে পরিবর্তিত হয়েছে। অনুগ্রহ করে রিফ্রেশ করুন।",
                    }
                ),
            )

    @classmethod
    def _recompute_cod_expected(cls, sale: PosSaleOrder) -> Decimal:
        settled = q2(q2(sale.paid_amount_bdt) + q2(sale.due_amount_bdt))
        return max(Decimal("0.00"), q2(q2(sale.grand_total_bdt) - settled))

    @classmethod
    def _create_embedded_sale_order(
        cls,
        *,
        user: Any,
        tenant: Any,
        branch: Any,
        data: Dict[str, Any],
        fulfillment_mode: str,
        delivery_fee_bdt: Decimal,
        customer: Optional[PosCustomer],
    ) -> PosSaleOrder:
        lines_payload = data.get("lines") or []
        if not isinstance(lines_payload, list) or not lines_payload:
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_EMPTY_CART",
                    "lines": "Provide an existing 'sale_id' with items or a non-empty 'lines' array.",
                    "message_en": "Add at least one medicine item before creating a preorder or delivery.",
                    "message_bn": "প্রি-অর্ডার বা ডেলিভারি তৈরির আগে কমপক্ষে একটি ওষুধ যোগ করুন।",
                }
            )

        source_channel = str(data.get("source_channel") or PosSaleSourceChannel.DESKTOP_POS_ONLINE).upper()
        if source_channel not in PosSaleSourceChannel.values:
            source_channel = PosSaleSourceChannel.DESKTOP_POS_ONLINE

        sale = PosSaleOrder.objects.create(
            sale_id=f"sale-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            cashier_user=user,
            customer=customer,
            customer_name=str(data.get("recipient_name") or (customer.full_name if customer else "Walk-in Customer"))[:255],
            customer_phone=str(data.get("recipient_phone") or (customer.phone if customer else ""))[:32],
            source_channel=source_channel,
            lifecycle_state=PosSaleLifecycleState.DRAFT,
            fulfillment_mode=fulfillment_mode,
            delivery_fee_bdt=delivery_fee_bdt,
            notes=str(data.get("notes") or "")[:500],
            created_by=user,
            updated_by=user,
        )

        for idx, raw_line in enumerate(lines_payload, start=1):
            if not isinstance(raw_line, dict):
                raise DeliveryDomainError({"lines": "Each line entry must be a JSON object."})
            pid = str(raw_line.get("product_id") or raw_line.get("product") or "").strip()
            product = MedicineProduct.objects.filter(product_id=pid).first()
            if not product:
                raise DeliveryDomainError(
                    {
                        "error_code": "ERR_PRODUCT_NOT_FOUND",
                        "product_id": f"Product '{pid}' not found or inactive.",
                        "message_en": f"Medicine '{pid}' not found.",
                        "message_bn": f"ওষুধ '{pid}' পাওয়া যায়নি।",
                    }
                )
            variant = None
            vid = str(raw_line.get("variant_id") or raw_line.get("variant") or "").strip()
            if vid:
                variant = MedicineVariant.objects.filter(pk=vid, product=product).first()

            unit = str(raw_line.get("packaging_unit") or PosPackagingUnit.TABLET).strip().upper()
            if unit not in PosPackagingUnit.values:
                raise DeliveryDomainError({"packaging_unit": f"Unsupported packaging_unit '{unit}'."})

            raw_qty = raw_line.get("quantity_units", 1)
            if isinstance(raw_qty, bool) or isinstance(raw_qty, float):
                raise DeliveryDomainError({"quantity_units": "quantity_units must be a positive integer."})
            try:
                qty = int(raw_qty)
            except (TypeError, ValueError):
                raise DeliveryDomainError({"quantity_units": "quantity_units must be a positive integer."})
            if qty < 1:
                raise DeliveryDomainError({"quantity_units": "quantity_units must be >= 1."})

            pkg_ctx = PosCartUnitSelectionService.resolve_packaging_context(tenant, branch, product, variant=variant)
            eval_res = PosCartUnitSelectionService.evaluate_unit_selection(
                tenant=tenant,
                branch=branch,
                product=product,
                variant=variant,
                packaging_unit=unit,
                quantity_units=qty,
                actor_user=user,
                pkg_ctx=pkg_ctx,
            )
            conv_factor = int(eval_res["conversion_factor"])
            unit_price = eval_res["unit_price_bdt"]
            line_sub = q2(Decimal(qty) * q2(unit_price))
            PosSaleOrderLine.objects.create(
                line_id=f"sline-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                branch=branch,
                sale=sale,
                line_number=idx,
                product=product,
                variant=variant,
                packaging_unit=unit,
                quantity_units=qty,
                conversion_factor=conv_factor,
                base_units_total=qty * conv_factor,
                unit_conversion_source=str(eval_res.get("conversion_source") or "")[:48],
                dosage_form_snapshot=str(eval_res.get("dosage_form_snapshot") or "")[:64],
                container_policy_applied=str(eval_res.get("container_policy_applied") or "")[:48],
                unit_price_bdt=unit_price,
                line_subtotal_bdt=line_sub,
                line_total_bdt=line_sub,
                requires_rx_verification=bool(
                    product.is_prescription_only or product.is_narcotic or product.is_antibiotic
                ),
            )

        sale, _ = PosCartPricingService.recompute_sale_totals(sale, user, allow_empty=False)
        sale.refresh_from_db()
        return sale

    @classmethod
    def create_delivery_order(
        cls, user: Any, data: Optional[Dict[str, Any]] = None
    ) -> Tuple[PosDeliveryOrder, bool]:
        data = data or {}
        try:
            delivery, created, pids = cls._create_delivery_order_atomic(user, data)
            if pids:
                PosHeldCartService._refresh_read_model_safe(user, delivery.branch_id, pids)
            return delivery, created
        except _DeliveryAbort as abort:
            cls._record_delivery_audit(**abort.audit_kwargs)
            raise abort.exc

    @classmethod
    @transaction.atomic
    def _create_delivery_order_atomic(
        cls, user: Any, data: Dict[str, Any]
    ) -> Tuple[PosDeliveryOrder, bool, List[str]]:
        branch_id = str(data.get("branch") or data.get("branch_id") or "").strip() or None
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
        )
        if not branch:
            branch = tenant.branches.filter(is_active=True).order_by("created_at").first()
        if not branch:
            raise DeliveryDomainError({"branch": "Active branch is required for preorder or delivery orders."})

        idem_key = str(data.get("idempotency_key") or "").strip()[:128]
        if idem_key:
            existing = (
                PosDeliveryOrder.objects.filter(tenant=tenant, idempotency_key=idem_key)
                .select_related("sale", "branch", "rider_staff")
                .first()
            )
            if existing:
                return existing, False, []

        mode = str(data.get("fulfillment_mode") or PosOrderFulfillmentMode.HOME_DELIVERY).strip().upper()
        if mode not in cls.PREORDER_OR_DELIVERY_MODES:
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_INVALID_FULFILLMENT_MODE",
                    "fulfillment_mode": f"fulfillment_mode must be PREORDER_PICKUP or HOME_DELIVERY (got '{mode}').",
                    "message_en": "Fulfillment mode must be PREORDER_PICKUP or HOME_DELIVERY.",
                    "message_bn": "অর্ডারের ধরন PREORDER_PICKUP অথবা HOME_DELIVERY হতে হবে।",
                }
            )

        default_method = (
            PosDeliveryMethod.BRANCH_PICKUP if mode == PosOrderFulfillmentMode.PREORDER_PICKUP else PosDeliveryMethod.OWN_RIDER
        )
        method = str(data.get("delivery_method") or default_method).strip().upper()
        if method not in PosDeliveryMethod.values:
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_INVALID_DELIVERY_METHOD",
                    "delivery_method": f"Invalid delivery_method '{method}'.",
                    "message_en": f"Unsupported delivery method '{method}'.",
                    "message_bn": "ডেলিভারি মাধ্যমটি সঠিক নয়।",
                }
            )

        recipient_name = str(data.get("recipient_name") or "").strip()[:255]
        recipient_phone = str(data.get("recipient_phone") or "").strip()[:32]
        address_line = str(data.get("delivery_address_line") or "").strip()
        area = str(data.get("delivery_area") or "").strip()[:128]
        city = str(data.get("delivery_city") or "Dhaka").strip()[:128]
        landmark = str(data.get("delivery_landmark_notes") or "").strip()

        if not recipient_name or not recipient_phone:
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_RECIPIENT_REQUIRED",
                    "recipient_name": "Recipient name and phone are mandatory for preorders/deliveries.",
                    "message_en": "Recipient name and phone number are required.",
                    "message_bn": "প্রাপকের নাম এবং মোবাইল নম্বর আবশ্যক।",
                }
            )
        if mode == PosOrderFulfillmentMode.HOME_DELIVERY and not address_line:
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_DELIVERY_ADDRESS_REQUIRED",
                    "delivery_address_line": "Street address is mandatory for HOME_DELIVERY orders.",
                    "message_en": "Delivery street address is required for home delivery.",
                    "message_bn": "হোম ডেলিভারির জন্য প্রাপকের ঠিকানা প্রদান আবশ্যক।",
                }
            )

        raw_fee = data.get("delivery_fee_bdt", "0.00")
        delivery_fee_bdt = parse_strict_decimal(
            raw_fee if raw_fee is not None else "0.00",
            "delivery_fee_bdt",
            min_val=Decimal("0.00"),
            error_code="ERR_INVALID_DELIVERY_FEE",
        )
        if mode == PosOrderFulfillmentMode.PREORDER_PICKUP and "delivery_fee_bdt" not in data:
            delivery_fee_bdt = Decimal("0.00")

        win_start = cls._parse_optional_iso_datetime(data.get("scheduled_window_start"), "scheduled_window_start")
        win_end = cls._parse_optional_iso_datetime(data.get("scheduled_window_end"), "scheduled_window_end")
        if win_start and win_end and win_end < win_start:
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_INVALID_DELIVERY_WINDOW",
                    "scheduled_window_end": "scheduled_window_end cannot be earlier than scheduled_window_start.",
                    "message_en": "Delivery time window end must be after start time.",
                    "message_bn": "ডেলিভারির শেষ সময় শুরুর সময়ের আগে হতে পারবে না।",
                }
            )

        customer = None
        cid = str(data.get("customer_id") or data.get("customer") or "").strip()
        if cid:
            customer = PosCustomer.objects.filter(pk=cid, tenant=tenant, is_archived=False).first()
            if not customer:
                raise DeliveryDomainError({"customer_id": "Customer does not belong to this tenant."})

        sale_id = str(data.get("sale_id") or data.get("sale") or "").strip()
        if sale_id:
            sale = PosSaleOrder.objects.select_for_update().filter(pk=sale_id, tenant=tenant).first()
            if not sale:
                raise DeliveryDomainError(
                    {
                        "error_code": "ERR_SALE_NOT_FOUND",
                        "sale_id": "Target POS sale order not found in this tenant.",
                        "message_en": "Referenced sale order not found.",
                        "message_bn": "সংশ্লিষ্ট সেলস অর্ডারটি পাওয়া যায়নি।",
                    }
                )
            if sale.branch_id != branch.branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_CROSS_BRANCH_ACCESS_DENIED",
                        "message_en": "Delivery branch must match linked POS sale order branch.",
                        "message_bn": "ডেলিভারি শাখা ও সেলস অর্ডারের শাখা একই হতে হবে।",
                    }
                )
            if hasattr(sale, "delivery_order") and sale.delivery_order:
                if idem_key and sale.delivery_order.idempotency_key == idem_key:
                    return sale.delivery_order, False, []
                raise DeliveryDomainError(
                    {
                        "error_code": "ERR_DELIVERY_ALREADY_EXISTS",
                        "sale_id": f"Sale '{sale.order_number}' already has a linked delivery order.",
                        "message_en": "A delivery/preorder record already exists for this cart.",
                        "message_bn": "এই কার্টের জন্য ইতোমধ্যেই একটি ডেলিভারি/প্রি-অর্ডার তৈরি রয়েছে।",
                    }
                )
            if sale.lifecycle_state in (PosSaleLifecycleState.VOIDED, PosSaleLifecycleState.REVERSED):
                raise DeliveryDomainError(
                    {
                        "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                        "sale_id": f"Cannot attach delivery to '{sale.lifecycle_state}' sale.",
                        "message_en": f"Cannot create delivery for a '{sale.lifecycle_state}' order.",
                        "message_bn": "বাতিল বা রিভার্সকৃত অর্ডারে ডেলিভারি তৈরি করা যাবে না।",
                    }
                )
            if not sale.lines.exists():
                raise DeliveryDomainError(
                    {
                        "error_code": "ERR_EMPTY_CART",
                        "lines": "Cannot create preorder or delivery for an empty cart.",
                        "message_en": "Add at least one medicine item before creating a preorder/delivery.",
                        "message_bn": "প্রি-অর্ডার বা ডেলিভারির আগে কার্টে অন্তত একটি ওষুধ যোগ করুন।",
                    }
                )
            sale.fulfillment_mode = mode
            sale.delivery_fee_bdt = delivery_fee_bdt
            if customer and not sale.customer_id:
                sale.customer = customer
            sale.updated_by = user
            sale.version += 1
            sale.save(update_fields=["fulfillment_mode", "delivery_fee_bdt", "customer", "updated_by", "version", "updated_at"])
            if sale.lifecycle_state != PosSaleLifecycleState.FINALIZED:
                PosCartPricingService.recompute_sale_totals(sale, user, allow_empty=False)
        else:
            sale = cls._create_embedded_sale_order(
                user=user,
                tenant=tenant,
                branch=branch,
                data=data,
                fulfillment_mode=mode,
                delivery_fee_bdt=delivery_fee_bdt,
                customer=customer,
            )

        rider_staff = None
        rider_staff_id = str(data.get("rider_staff_id") or "").strip()
        if rider_staff_id:
            rider_staff = StaffMember.objects.filter(pk=rider_staff_id, tenant=tenant).first()
            if not rider_staff:
                raise DeliveryDomainError({"rider_staff_id": "Assigned rider staff does not belong to this tenant."})

        reserve_stock = bool(data.get("reserve_stock", True))
        res_ttl = cls.parse_strict_reservation_ttl(
            data.get("reservation_ttl_minutes"),
            default_minutes=cls.DEFAULT_PREORDER_RESERVATION_TTL_MINUTES,
        )

        delivery = PosDeliveryOrder(
            delivery_id=f"delv-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            sale=sale,
            idempotency_key=idem_key,
            fulfillment_mode=mode,
            delivery_method=method,
            fulfillment_state=PosDeliveryFulfillmentState.PREORDER_PENDING,
            scheduled_window_start=win_start,
            scheduled_window_end=win_end,
            recipient_name=recipient_name,
            recipient_phone=recipient_phone,
            delivery_address_line=address_line,
            delivery_area=area,
            delivery_city=city,
            delivery_landmark_notes=landmark,
            privacy_consent_captured=bool(data.get("privacy_consent_captured", True)),
            delivery_fee_bdt=delivery_fee_bdt,
            cod_expected_bdt=cls._recompute_cod_expected(sale),
            courier_partner_name=str(data.get("courier_partner_name") or "")[:128],
            rider_staff=rider_staff,
            rider_name=str(data.get("rider_name") or (rider_staff.full_name if rider_staff else ""))[:128],
            rider_phone=str(data.get("rider_phone") or (rider_staff.phone if rider_staff else ""))[:32],
            created_by=user,
            updated_by=user,
        )
        delivery.save()

        affected_pids: List[str] = []
        if reserve_stock and sale.lifecycle_state != PosSaleLifecycleState.FINALIZED:
            lines = list(sale.lines.select_related("product", "variant", "preferred_batch").order_by("line_number"))
            try:
                _, affected_pids = PosHeldCartService._allocate_fefo_reserved_stock(user, sale, lines)
            except _HoldCartAbort as abort:
                raise _DeliveryAbort(
                    audit_kwargs={
                        "tenant": tenant,
                        "branch": branch,
                        "delivery": None,
                        "sale": sale,
                        "action": "STOCK_RESERVE_REJECTED",
                        "from_state": PosDeliveryFulfillmentState.PREORDER_PENDING,
                        "to_state": PosDeliveryFulfillmentState.PREORDER_PENDING,
                        "user": user,
                        "was_rejected": True,
                        "rejection_code": abort.audit_kwargs.get("rejection_code", "ERR_INSUFFICIENT_DISPENSABLE_STOCK"),
                        "rejection_message": abort.audit_kwargs.get("rejection_message", "Stock reservation failed."),
                    },
                    exc=abort.exc,
                )
            now = timezone.now()
            sale.hold_stock_reserved = True
            sale.hold_reservation_expires_at = now + datetime.timedelta(minutes=res_ttl)
            sale.save(update_fields=["hold_stock_reserved", "hold_reservation_expires_at", "updated_at"])
            delivery.stock_reserved = True
            delivery.reservation_expires_at = sale.hold_reservation_expires_at
            delivery.fulfillment_state = PosDeliveryFulfillmentState.STOCK_RESERVED
            delivery.save(update_fields=["stock_reserved", "reservation_expires_at", "fulfillment_state", "updated_at"])

        cls._record_delivery_audit(
            tenant=tenant,
            branch=branch,
            delivery=delivery,
            sale=sale,
            action="CREATED",
            user=user,
            from_state="",
            to_state=delivery.fulfillment_state,
            reason=f"Created {mode} order ({method})",
            metadata={
                "fulfillment_mode": mode,
                "delivery_method": method,
                "delivery_area": area,
                "delivery_city": city,
                "stock_reserved": delivery.stock_reserved,
                "delivery_fee_bdt": f"{delivery_fee_bdt:.2f}",
                "cod_expected_bdt": f"{delivery.cod_expected_bdt:.2f}",
            },
        )
        return delivery, True, affected_pids

    @classmethod
    def update_delivery_order(
        cls, user: Any, delivery: PosDeliveryOrder, data: Optional[Dict[str, Any]] = None
    ) -> PosDeliveryOrder:
        data = data or {}
        expected_version = cls.parse_strict_version(data.get("expected_version"))
        try:
            return cls._update_delivery_order_atomic(user, delivery.delivery_id, data, expected_version)
        except _DeliveryAbort as abort:
            cls._record_delivery_audit(**abort.audit_kwargs)
            raise abort.exc

    @classmethod
    @transaction.atomic
    def _update_delivery_order_atomic(
        cls, user: Any, delivery_id: str, data: Dict[str, Any], expected_version: Optional[int]
    ) -> PosDeliveryOrder:
        delivery = PosDeliveryOrder.objects.select_for_update().select_related("sale", "branch", "tenant").get(pk=delivery_id)
        PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=delivery.branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
        )
        cls._assert_version_match(delivery, expected_version, user, "UPDATE_REJECTED")

        if delivery.fulfillment_state not in cls.MUTABLE_DELIVERY_STATES:
            raise _DeliveryAbort(
                audit_kwargs={
                    "tenant": delivery.tenant,
                    "branch": delivery.branch,
                    "delivery": delivery,
                    "sale": delivery.sale,
                    "action": "UPDATE_REJECTED",
                    "from_state": delivery.fulfillment_state,
                    "to_state": delivery.fulfillment_state,
                    "user": user,
                    "was_rejected": True,
                    "rejection_code": "ERR_IMMUTABLE_DELIVERY_STATE",
                    "rejection_message": f"Cannot edit delivery in '{delivery.fulfillment_state}' state.",
                },
                exc=DeliveryDomainError(
                    {
                        "error_code": "ERR_IMMUTABLE_DELIVERY_STATE",
                        "detail": (
                            f"Delivery '{delivery.delivery_number}' is in '{delivery.fulfillment_state}' state "
                            "and can no longer be edited; use audited fulfillment or return actions."
                        ),
                        "message_en": f"Delivery in '{delivery.fulfillment_state}' state is locked from direct edits.",
                        "message_bn": f"'{delivery.fulfillment_state}' অবস্থার ডেলিভারি সরাসরি সম্পাদনা করা যাবে না।",
                    }
                ),
            )

        if "delivery_method" in data and data["delivery_method"] is not None:
            method = str(data["delivery_method"]).strip().upper()
            if method not in PosDeliveryMethod.values:
                raise DeliveryDomainError({"delivery_method": f"Invalid delivery_method '{method}'."})
            delivery.delivery_method = method

        if "recipient_name" in data and data["recipient_name"] is not None:
            val = str(data["recipient_name"]).strip()[:255]
            if not val:
                raise DeliveryDomainError({"recipient_name": "Recipient name cannot be blank."})
            delivery.recipient_name = val
        if "recipient_phone" in data and data["recipient_phone"] is not None:
            val = str(data["recipient_phone"]).strip()[:32]
            if not val:
                raise DeliveryDomainError({"recipient_phone": "Recipient phone cannot be blank."})
            delivery.recipient_phone = val
        if "delivery_address_line" in data and data["delivery_address_line"] is not None:
            addr = str(data["delivery_address_line"]).strip()
            if delivery.fulfillment_mode == PosOrderFulfillmentMode.HOME_DELIVERY and not addr:
                raise DeliveryDomainError(
                    {
                        "error_code": "ERR_DELIVERY_ADDRESS_REQUIRED",
                        "delivery_address_line": "Street address is mandatory for HOME_DELIVERY.",
                        "message_en": "Delivery street address cannot be empty for home delivery.",
                        "message_bn": "হোম ডেলিভারির ঠিকানা খালি রাখা যাবে না।",
                    }
                )
            delivery.delivery_address_line = addr
            delivery.address_redacted_at = None
            delivery.address_redacted_by = None
        if "delivery_area" in data and data["delivery_area"] is not None:
            delivery.delivery_area = str(data["delivery_area"]).strip()[:128]
        if "delivery_city" in data and data["delivery_city"] is not None:
            delivery.delivery_city = str(data["delivery_city"]).strip()[:128]
        if "delivery_landmark_notes" in data and data["delivery_landmark_notes"] is not None:
            delivery.delivery_landmark_notes = str(data["delivery_landmark_notes"]).strip()

        if "scheduled_window_start" in data:
            delivery.scheduled_window_start = cls._parse_optional_iso_datetime(
                data.get("scheduled_window_start"), "scheduled_window_start"
            )
        if "scheduled_window_end" in data:
            delivery.scheduled_window_end = cls._parse_optional_iso_datetime(
                data.get("scheduled_window_end"), "scheduled_window_end"
            )
        if (
            delivery.scheduled_window_start
            and delivery.scheduled_window_end
            and delivery.scheduled_window_end < delivery.scheduled_window_start
        ):
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_INVALID_DELIVERY_WINDOW",
                    "scheduled_window_end": "Window end cannot precede window start.",
                    "message_en": "Delivery window end cannot be earlier than start time.",
                    "message_bn": "ডেলিভারির শেষ সময় শুরুর সময়ের আগে হতে পারবে না।",
                }
            )

        if "courier_partner_name" in data and data["courier_partner_name"] is not None:
            delivery.courier_partner_name = str(data["courier_partner_name"]).strip()[:128]
        if "rider_name" in data and data["rider_name"] is not None:
            delivery.rider_name = str(data["rider_name"]).strip()[:128]
        if "rider_phone" in data and data["rider_phone"] is not None:
            delivery.rider_phone = str(data["rider_phone"]).strip()[:32]
        if "rider_staff_id" in data:
            rsid = str(data.get("rider_staff_id") or "").strip()
            if rsid:
                staff = StaffMember.objects.filter(pk=rsid, tenant=delivery.tenant).first()
                if not staff:
                    raise DeliveryDomainError({"rider_staff_id": "Assigned rider staff does not belong to this tenant."})
                delivery.rider_staff = staff
            else:
                delivery.rider_staff = None

        sale = PosSaleOrder.objects.select_for_update().get(pk=delivery.sale_id)
        if "delivery_fee_bdt" in data and data["delivery_fee_bdt"] is not None:
            if sale.lifecycle_state == PosSaleLifecycleState.FINALIZED:
                raise DeliveryDomainError(
                    {
                        "error_code": "ERR_IMMUTABLE_DELIVERY_STATE",
                        "delivery_fee_bdt": "Cannot modify delivery fee after POS sale finalization.",
                        "message_en": "Delivery fee is locked after invoice finalization.",
                        "message_bn": "ইনভয়েস চূড়ান্ত হওয়ার পর ডেলিভারি ফি পরিবর্তন করা যাবে না।",
                    }
                )
            new_fee = parse_strict_decimal(
                data["delivery_fee_bdt"],
                "delivery_fee_bdt",
                min_val=Decimal("0.00"),
                error_code="ERR_INVALID_DELIVERY_FEE",
            )
            delivery.delivery_fee_bdt = new_fee
            sale.delivery_fee_bdt = new_fee
            sale.version += 1
            sale.updated_by = user
            sale.save(update_fields=["delivery_fee_bdt", "version", "updated_by", "updated_at"])
            sale, _ = PosCartPricingService.recompute_sale_totals(sale, user, allow_empty=False)
            sale.refresh_from_db()

        delivery.sale = sale
        delivery.cod_expected_bdt = cls._recompute_cod_expected(sale)
        delivery.version += 1
        delivery.updated_by = user
        delivery.save()

        cls._record_delivery_audit(
            tenant=delivery.tenant,
            branch=delivery.branch,
            delivery=delivery,
            sale=sale,
            action="UPDATED",
            user=user,
            from_state=delivery.fulfillment_state,
            to_state=delivery.fulfillment_state,
            reason="Updated preorder/delivery metadata",
            metadata={
                "delivery_method": delivery.delivery_method,
                "delivery_area": delivery.delivery_area,
                "delivery_fee_bdt": f"{delivery.delivery_fee_bdt:.2f}",
                "cod_expected_bdt": f"{delivery.cod_expected_bdt:.2f}",
            },
        )
        return delivery

    @classmethod
    @transaction.atomic
    def archive_delivery_order(cls, user: Any, delivery: PosDeliveryOrder, reason: str = "") -> PosDeliveryOrder:
        delivery = PosDeliveryOrder.objects.select_for_update().select_related("sale", "branch", "tenant").get(pk=delivery.pk)
        PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=delivery.branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
        )
        allowed_archive_states = {
            PosDeliveryFulfillmentState.PREORDER_PENDING,
            PosDeliveryFulfillmentState.RETURNED_TO_BRANCH,
            PosDeliveryFulfillmentState.CANCELLED,
            PosDeliveryFulfillmentState.DELIVERED,
        }
        if delivery.fulfillment_state not in allowed_archive_states or (
            delivery.stock_reserved and delivery.fulfillment_state == PosDeliveryFulfillmentState.PREORDER_PENDING
        ):
            raise MethodNotAllowed(
                "DELETE",
                detail=(
                    f"Active delivery '{delivery.delivery_number}' in state '{delivery.fulfillment_state}' "
                    "cannot be archived directly; cancel or complete fulfillment first."
                ),
            )
        if not delivery.address_redacted_at and delivery.fulfillment_state in cls.REDACT_ALLOWED_STATES:
            delivery.delivery_address_line = "[REDACTED_ON_ARCHIVE]"
            delivery.delivery_landmark_notes = ""
            delivery.address_redacted_at = timezone.now()
            delivery.address_redacted_by = user
        delivery.is_archived = True
        delivery.version += 1
        delivery.updated_by = user
        delivery.save()

        cls._record_delivery_audit(
            tenant=delivery.tenant,
            branch=delivery.branch,
            delivery=delivery,
            sale=delivery.sale,
            action="ARCHIVED",
            user=user,
            from_state=delivery.fulfillment_state,
            to_state=delivery.fulfillment_state,
            reason=reason or "Delivery order archived safely",
        )
        return delivery

    @classmethod
    def reserve_stock(
        cls, user: Any, delivery: PosDeliveryOrder, data: Optional[Dict[str, Any]] = None
    ) -> PosDeliveryOrder:
        data = data or {}
        expected_version = cls.parse_strict_version(data.get("expected_version"))
        try:
            updated, pids = cls._reserve_stock_atomic(user, delivery.delivery_id, data, expected_version)
            if pids:
                PosHeldCartService._refresh_read_model_safe(user, updated.branch_id, pids)
            return updated
        except _DeliveryAbort as abort:
            cls._record_delivery_audit(**abort.audit_kwargs)
            raise abort.exc

    @classmethod
    @transaction.atomic
    def _reserve_stock_atomic(
        cls, user: Any, delivery_id: str, data: Dict[str, Any], expected_version: Optional[int]
    ) -> Tuple[PosDeliveryOrder, List[str]]:
        delivery = PosDeliveryOrder.objects.select_for_update().select_related("sale", "branch", "tenant").get(pk=delivery_id)
        PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=delivery.branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
        )
        cls._assert_version_match(delivery, expected_version, user, "RESERVE_REJECTED")

        if delivery.fulfillment_state not in (
            PosDeliveryFulfillmentState.PREORDER_PENDING,
            PosDeliveryFulfillmentState.STOCK_RESERVED,
        ):
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "detail": f"Cannot reserve stock in '{delivery.fulfillment_state}' state.",
                    "message_en": f"Cannot reserve stock when order is '{delivery.fulfillment_state}'.",
                    "message_bn": f"'{delivery.fulfillment_state}' অবস্থায় স্টক রিজার্ভ করা যাবে না।",
                }
            )

        sale = PosSaleOrder.objects.select_for_update().get(pk=delivery.sale_id)
        lines = list(sale.lines.select_related("product", "variant", "preferred_batch").order_by("line_number"))
        if not lines:
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_EMPTY_CART",
                    "message_en": "Cannot reserve stock for an empty order.",
                    "message_bn": "খালি অর্ডারের জন্য স্টক রিজার্ভ করা যাবে না।",
                }
            )

        res_ttl = cls.parse_strict_reservation_ttl(
            data.get("reservation_ttl_minutes"),
            default_minutes=cls.DEFAULT_PREORDER_RESERVATION_TTL_MINUTES,
        )
        try:
            reserved_units, pids = PosHeldCartService._allocate_fefo_reserved_stock(user, sale, lines)
        except _HoldCartAbort as abort:
            raise _DeliveryAbort(
                audit_kwargs={
                    "tenant": delivery.tenant,
                    "branch": delivery.branch,
                    "delivery": delivery,
                    "sale": sale,
                    "action": "STOCK_RESERVE_REJECTED",
                    "from_state": delivery.fulfillment_state,
                    "to_state": delivery.fulfillment_state,
                    "user": user,
                    "was_rejected": True,
                    "rejection_code": abort.audit_kwargs.get("rejection_code", "ERR_INSUFFICIENT_DISPENSABLE_STOCK"),
                    "rejection_message": abort.audit_kwargs.get("rejection_message", "Stock reservation failed."),
                },
                exc=abort.exc,
            )

        now = timezone.now()
        old_state = delivery.fulfillment_state
        sale.hold_stock_reserved = True
        sale.hold_reservation_expires_at = now + datetime.timedelta(minutes=res_ttl)
        sale.save(update_fields=["hold_stock_reserved", "hold_reservation_expires_at", "updated_at"])

        delivery.stock_reserved = True
        delivery.reservation_expires_at = sale.hold_reservation_expires_at
        delivery.fulfillment_state = PosDeliveryFulfillmentState.STOCK_RESERVED
        delivery.version += 1
        delivery.updated_by = user
        delivery.save()

        cls._record_delivery_audit(
            tenant=delivery.tenant,
            branch=delivery.branch,
            delivery=delivery,
            sale=sale,
            action="STOCK_RESERVED",
            user=user,
            from_state=old_state,
            to_state=delivery.fulfillment_state,
            reason=f"Reserved {reserved_units} FEFO base units ({res_ttl}m TTL)",
            metadata={"reserved_base_units": reserved_units, "reservation_ttl_minutes": res_ttl},
        )
        return delivery, pids

    @classmethod
    def pack_delivery(
        cls, user: Any, delivery: PosDeliveryOrder, data: Optional[Dict[str, Any]] = None
    ) -> PosDeliveryOrder:
        data = data or {}
        expected_version = cls.parse_strict_version(data.get("expected_version"))
        try:
            updated, pids = cls._pack_delivery_atomic(user, delivery.delivery_id, data, expected_version)
            if pids:
                PosHeldCartService._refresh_read_model_safe(user, updated.branch_id, pids)
            return updated
        except _DeliveryAbort as abort:
            cls._record_delivery_audit(**abort.audit_kwargs)
            raise abort.exc

    @classmethod
    @transaction.atomic
    def _pack_delivery_atomic(
        cls, user: Any, delivery_id: str, data: Dict[str, Any], expected_version: Optional[int]
    ) -> Tuple[PosDeliveryOrder, List[str]]:
        delivery = PosDeliveryOrder.objects.select_for_update().select_related("sale", "branch", "tenant").get(pk=delivery_id)
        PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=delivery.branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
        )
        cls._assert_version_match(delivery, expected_version, user, "PACK_REJECTED")

        allowed_sources = {
            PosDeliveryFulfillmentState.STOCK_RESERVED,
            PosDeliveryFulfillmentState.RETURNED_TO_BRANCH,
        }
        if delivery.fulfillment_state not in allowed_sources:
            raise _DeliveryAbort(
                audit_kwargs={
                    "tenant": delivery.tenant,
                    "branch": delivery.branch,
                    "delivery": delivery,
                    "sale": delivery.sale,
                    "action": "PACK_REJECTED",
                    "from_state": delivery.fulfillment_state,
                    "to_state": delivery.fulfillment_state,
                    "user": user,
                    "was_rejected": True,
                    "rejection_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "rejection_message": f"Cannot pack delivery in '{delivery.fulfillment_state}' state.",
                },
                exc=DeliveryDomainError(
                    {
                        "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                        "detail": (
                            f"Delivery must have reserved stock (STOCK_RESERVED) before packing "
                            f"(current: '{delivery.fulfillment_state}')."
                        ),
                        "message_en": "Reserve stock cleanly before packing this delivery.",
                        "message_bn": "প্যাক করার আগে অবশ্যই স্টক রিজার্ভ থাকতে হবে।",
                    }
                ),
            )

        sale = PosSaleOrder.objects.select_for_update().get(pk=delivery.sale_id)
        from apps.core.pos_rx_safety_service import PosRxSafetyService

        rx_sum = PosRxSafetyService.evaluate_and_sync_sale_rx_gate(sale, actor_user=user, mutate=True)
        if rx_sum["has_blocked_recall_or_ban"] or not rx_sum["can_finalize_online"]:
            raise _DeliveryAbort(
                audit_kwargs={
                    "tenant": delivery.tenant,
                    "branch": delivery.branch,
                    "delivery": delivery,
                    "sale": sale,
                    "action": "PACK_REJECTED",
                    "from_state": delivery.fulfillment_state,
                    "to_state": delivery.fulfillment_state,
                    "user": user,
                    "was_rejected": True,
                    "rejection_code": "ERR_REGULATED_PRODUCT_BLOCKED" if rx_sum["has_blocked_recall_or_ban"] else "ERR_RX_VERIFICATION_REQUIRED",
                    "rejection_message": "Pharmacist Rx verification or regulatory safety clearance required before packing.",
                },
                exc=DeliveryDomainError(
                    {
                        "error_code": "ERR_REGULATED_PRODUCT_BLOCKED" if rx_sum["has_blocked_recall_or_ban"] else "ERR_RX_VERIFICATION_REQUIRED",
                        "detail": "Prescription verification by a registered pharmacist is required before packing/dispatching.",
                        "message_en": "Pharmacist prescription verification is required before packing Rx/antibiotic items.",
                        "message_bn": "প্রেসক্রিপশন বা অ্যান্টিবায়োটিক ওষুধ প্যাক করার আগে ফার্মাসিস্টের যাচাইকরণ আবশ্যক।",
                    }
                ),
            )

        pids: List[str] = []
        if sale.lifecycle_state != PosSaleLifecycleState.FINALIZED:
            reserved_allocs = list(sale.allocations.filter(status=PosAllocationStatus.RESERVED).select_related("batch"))
            if not reserved_allocs:
                lines = list(sale.lines.select_related("product", "variant", "preferred_batch").order_by("line_number"))
                _, pids = PosHeldCartService._allocate_fefo_reserved_stock(user, sale, lines)
            else:
                for alloc in reserved_allocs:
                    b = alloc.batch
                    if b.is_expired or b.status != BatchStatus.ACTIVE or b.is_quarantined or b.is_recalled:
                        raise _DeliveryAbort(
                            audit_kwargs={
                                "tenant": delivery.tenant,
                                "branch": delivery.branch,
                                "delivery": delivery,
                                "sale": sale,
                                "action": "PACK_REJECTED",
                                "from_state": delivery.fulfillment_state,
                                "to_state": delivery.fulfillment_state,
                                "user": user,
                                "was_rejected": True,
                                "rejection_code": "ERR_UNSAFE_BATCH_BLOCKED",
                                "rejection_message": f"Reserved batch '{b.batch_number}' is unsafe.",
                            },
                            exc=DeliveryDomainError(
                                {
                                    "error_code": "ERR_UNSAFE_BATCH_BLOCKED",
                                    "detail": f"Reserved batch '{b.batch_number}' became expired/quarantined/recalled.",
                                    "message_en": f"Batch '{b.batch_number}' cannot be packed due to expiry/quarantine/recall.",
                                    "message_bn": f"ব্যাচ '{b.batch_number}' মেয়াদোত্তীর্ণ বা কোয়ারেন্টাইনে থাকায় প্যাক করা যাবে না।",
                                }
                            ),
                        )

        now = timezone.now()
        old_state = delivery.fulfillment_state
        delivery.fulfillment_state = PosDeliveryFulfillmentState.PACKED
        delivery.packed_at = now
        delivery.packed_by = user
        delivery.stock_reserved = sale.lifecycle_state != PosSaleLifecycleState.FINALIZED
        delivery.cod_expected_bdt = cls._recompute_cod_expected(sale)
        delivery.version += 1
        delivery.updated_by = user
        delivery.save()

        cls._record_delivery_audit(
            tenant=delivery.tenant,
            branch=delivery.branch,
            delivery=delivery,
            sale=sale,
            action="PACKED",
            user=user,
            from_state=old_state,
            to_state=delivery.fulfillment_state,
            reason=str(data.get("note") or "Order packed and verified cleanly"),
        )
        return delivery, pids

    @classmethod
    def dispatch_delivery(
        cls, user: Any, delivery: PosDeliveryOrder, data: Optional[Dict[str, Any]] = None
    ) -> PosDeliveryOrder:
        data = data or {}
        expected_version = cls.parse_strict_version(data.get("expected_version"))
        try:
            updated, pids = cls._dispatch_delivery_atomic(user, delivery.delivery_id, data, expected_version)
            if pids:
                PosHeldCartService._refresh_read_model_safe(user, updated.branch_id, pids)
            return updated
        except _DeliveryAbort as abort:
            cls._record_delivery_audit(**abort.audit_kwargs)
            raise abort.exc

    @classmethod
    @transaction.atomic
    def _dispatch_delivery_atomic(
        cls, user: Any, delivery_id: str, data: Dict[str, Any], expected_version: Optional[int]
    ) -> Tuple[PosDeliveryOrder, List[str]]:
        delivery = PosDeliveryOrder.objects.select_for_update().select_related("sale", "branch", "tenant").get(pk=delivery_id)
        PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=delivery.branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
        )
        cls._assert_version_match(delivery, expected_version, user, "DISPATCH_REJECTED")

        if delivery.fulfillment_state not in (
            PosDeliveryFulfillmentState.PACKED,
            PosDeliveryFulfillmentState.FAILED_ATTEMPT,
        ):
            raise _DeliveryAbort(
                audit_kwargs={
                    "tenant": delivery.tenant,
                    "branch": delivery.branch,
                    "delivery": delivery,
                    "sale": delivery.sale,
                    "action": "DISPATCH_REJECTED",
                    "from_state": delivery.fulfillment_state,
                    "to_state": delivery.fulfillment_state,
                    "user": user,
                    "was_rejected": True,
                    "rejection_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "rejection_message": f"Cannot dispatch from '{delivery.fulfillment_state}' state.",
                },
                exc=DeliveryDomainError(
                    {
                        "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                        "detail": f"Delivery must be PACKED or FAILED_ATTEMPT before dispatch (got '{delivery.fulfillment_state}').",
                        "message_en": "Order must be packed before dispatching to rider/courier.",
                        "message_bn": "ডিসপ্যাচ করার আগে অর্ডারটি প্যাক অবস্থায় থাকতে হবে।",
                    }
                ),
            )

        if "rider_staff_id" in data and data["rider_staff_id"]:
            staff = StaffMember.objects.filter(pk=str(data["rider_staff_id"]).strip(), tenant=delivery.tenant).first()
            if not staff:
                raise DeliveryDomainError({"rider_staff_id": "Assigned rider staff does not belong to this tenant."})
            delivery.rider_staff = staff
            if not str(data.get("rider_name") or "").strip():
                delivery.rider_name = staff.full_name[:128]
            if not str(data.get("rider_phone") or "").strip():
                delivery.rider_phone = staff.phone[:32]
        if "rider_name" in data and data["rider_name"] is not None:
            delivery.rider_name = str(data["rider_name"]).strip()[:128]
        if "rider_phone" in data and data["rider_phone"] is not None:
            delivery.rider_phone = str(data["rider_phone"]).strip()[:32]
        if "courier_partner_name" in data and data["courier_partner_name"] is not None:
            delivery.courier_partner_name = str(data["courier_partner_name"]).strip()[:128]
        if "courier_reference" in data and data["courier_reference"] is not None:
            delivery.courier_reference = str(data["courier_reference"]).strip()[:128]

        if delivery.fulfillment_mode == PosOrderFulfillmentMode.HOME_DELIVERY:
            if not (delivery.rider_staff_id or delivery.rider_name or delivery.courier_partner_name):
                raise DeliveryDomainError(
                    {
                        "error_code": "ERR_DISPATCH_ASSIGNEE_REQUIRED",
                        "detail": "Home delivery dispatch requires a rider_name, rider_staff_id, or courier_partner_name.",
                        "message_en": "Assign a delivery rider or courier partner before dispatching.",
                        "message_bn": "ডিসপ্যাচ করার আগে রাইডার বা কুরিয়ারের নাম নির্ধারণ করুন।",
                    }
                )

        sale = PosSaleOrder.objects.select_for_update().get(pk=delivery.sale_id)
        from apps.core.pos_rx_safety_service import PosRxSafetyService

        rx_sum = PosRxSafetyService.evaluate_and_sync_sale_rx_gate(sale, actor_user=user, mutate=True)
        if rx_sum["has_blocked_recall_or_ban"] or not rx_sum["can_finalize_online"]:
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_REGULATED_PRODUCT_BLOCKED" if rx_sum["has_blocked_recall_or_ban"] else "ERR_RX_VERIFICATION_REQUIRED",
                    "detail": "Rx verification required before dispatch.",
                    "message_en": "Pharmacist verification is required before dispatching Rx orders.",
                    "message_bn": "ডিসপ্যাচ করার আগে ফার্মাসিস্টের প্রেসক্রিপশন যাচাই আবশ্যক।",
                }
            )

        now = timezone.now()
        pids: List[str] = []
        # Prepaid / due-settled orders finalize inventory & invoice immediately at dispatch handover
        if sale.lifecycle_state != PosSaleLifecycleState.FINALIZED:
            PosDomainService._sync_sale_payment_totals(sale)
            settled_sum = q2(sale.paid_amount_bdt + sale.due_amount_bdt)
            if settled_sum == q2(sale.grand_total_bdt) and q2(sale.grand_total_bdt) > Decimal("0.00"):
                sale = PosDomainService.finalize_sale(user, sale)
                delivery.stock_reserved = False
                pids = list(sale.lines.values_list("product_id", flat=True))
            else:
                # COD order stays RESERVED in BatchStock during transit so stock cannot double-sell
                delivery.stock_reserved = True
                sale.hold_reservation_expires_at = now + datetime.timedelta(hours=24)
                sale.save(update_fields=["hold_reservation_expires_at", "updated_at"])
                delivery.reservation_expires_at = sale.hold_reservation_expires_at

        old_state = delivery.fulfillment_state
        delivery.sale = sale
        delivery.fulfillment_state = PosDeliveryFulfillmentState.DISPATCHED
        delivery.dispatched_at = delivery.dispatched_at or now
        delivery.dispatched_by = delivery.dispatched_by or user
        delivery.cod_expected_bdt = cls._recompute_cod_expected(sale)
        delivery.version += 1
        delivery.updated_by = user
        delivery.save()

        cls._record_delivery_audit(
            tenant=delivery.tenant,
            branch=delivery.branch,
            delivery=delivery,
            sale=sale,
            action="DISPATCHED",
            user=user,
            from_state=old_state,
            to_state=delivery.fulfillment_state,
            reason=str(data.get("note") or f"Dispatched via {delivery.delivery_method}"),
            metadata={
                "finalized_at_dispatch": sale.lifecycle_state == PosSaleLifecycleState.FINALIZED,
                "cod_expected_bdt": f"{delivery.cod_expected_bdt:.2f}",
                "courier_partner_name": delivery.courier_partner_name,
            },
        )
        return delivery, pids

    @classmethod
    @transaction.atomic
    def mark_out_for_delivery(
        cls, user: Any, delivery: PosDeliveryOrder, data: Optional[Dict[str, Any]] = None
    ) -> PosDeliveryOrder:
        data = data or {}
        delivery = PosDeliveryOrder.objects.select_for_update().select_related("sale", "branch", "tenant").get(pk=delivery.pk)
        PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=delivery.branch_id, require_write=True, allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES
        )
        expected_version = cls.parse_strict_version(data.get("expected_version"))
        if expected_version is not None and int(delivery.version) != expected_version:
            raise DeliveryDomainError({"error_code": "ERR_CART_VERSION_CONFLICT", "message_en": "Version conflict."})

        if delivery.fulfillment_state not in (
            PosDeliveryFulfillmentState.DISPATCHED,
            PosDeliveryFulfillmentState.FAILED_ATTEMPT,
        ):
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "detail": f"Cannot transition to OUT_FOR_DELIVERY from '{delivery.fulfillment_state}'.",
                    "message_en": "Dispatch order before marking out for delivery.",
                    "message_bn": "আউট ফর ডেলিভারি করার আগে অর্ডারটি ডিসপ্যাচ থাকতে হবে।",
                }
            )
        old_state = delivery.fulfillment_state
        delivery.fulfillment_state = PosDeliveryFulfillmentState.OUT_FOR_DELIVERY
        delivery.version += 1
        delivery.updated_by = user
        delivery.save()
        cls._record_delivery_audit(
            tenant=delivery.tenant,
            branch=delivery.branch,
            delivery=delivery,
            sale=delivery.sale,
            action="OUT_FOR_DELIVERY",
            user=user,
            from_state=old_state,
            to_state=delivery.fulfillment_state,
            reason=str(data.get("note") or "Courier out for doorstep delivery"),
        )
        return delivery

    @classmethod
    def complete_delivery(
        cls, user: Any, delivery: PosDeliveryOrder, data: Optional[Dict[str, Any]] = None
    ) -> PosDeliveryOrder:
        data = data or {}
        expected_version = cls.parse_strict_version(data.get("expected_version"))
        try:
            updated, pids = cls._complete_delivery_atomic(user, delivery.delivery_id, data, expected_version)
            if pids:
                PosHeldCartService._refresh_read_model_safe(user, updated.branch_id, pids)
            return updated
        except _DeliveryAbort as abort:
            cls._record_delivery_audit(**abort.audit_kwargs)
            raise abort.exc

    @classmethod
    @transaction.atomic
    def _complete_delivery_atomic(
        cls, user: Any, delivery_id: str, data: Dict[str, Any], expected_version: Optional[int]
    ) -> Tuple[PosDeliveryOrder, List[str]]:
        delivery = PosDeliveryOrder.objects.select_for_update().select_related("sale", "branch", "tenant").get(pk=delivery_id)
        PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=delivery.branch_id, require_write=True, allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES
        )
        cls._assert_version_match(delivery, expected_version, user, "DELIVER_REJECTED")

        allowed = {PosDeliveryFulfillmentState.DISPATCHED, PosDeliveryFulfillmentState.OUT_FOR_DELIVERY}
        if delivery.fulfillment_mode == PosOrderFulfillmentMode.PREORDER_PICKUP:
            allowed.add(PosDeliveryFulfillmentState.PACKED)

        if delivery.fulfillment_state not in allowed:
            raise _DeliveryAbort(
                audit_kwargs={
                    "tenant": delivery.tenant,
                    "branch": delivery.branch,
                    "delivery": delivery,
                    "sale": delivery.sale,
                    "action": "DELIVER_REJECTED",
                    "from_state": delivery.fulfillment_state,
                    "to_state": delivery.fulfillment_state,
                    "user": user,
                    "was_rejected": True,
                    "rejection_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "rejection_message": f"Cannot complete delivery from '{delivery.fulfillment_state}' state.",
                },
                exc=DeliveryDomainError(
                    {
                        "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                        "detail": f"Cannot mark delivery DELIVERED from '{delivery.fulfillment_state}'.",
                        "message_en": "Dispatch order (or pack pickup order) before completing delivery.",
                        "message_bn": "ডেলিভারি সম্পন্ন করার আগে অর্ডারটি ডিসপ্যাচ অবস্থায় থাকতে হবে।",
                    }
                ),
            )

        pod_ref = str(data.get("proof_of_delivery_ref") or "").strip()[:128]
        if not pod_ref:
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_POD_REQUIRED",
                    "proof_of_delivery_ref": "Proof of delivery reference/OTP/signature is required.",
                    "message_en": "Proof of delivery reference (OTP, receipt no., or signature ref) is required.",
                    "message_bn": "ডেলিভারির প্রমাণ (OTP বা রিসিভ রেফারেন্স) প্রদান করা আবশ্যক।",
                }
            )

        sale = PosSaleOrder.objects.select_for_update().get(pk=delivery.sale_id)
        pids: List[str] = []
        if sale.lifecycle_state != PosSaleLifecycleState.FINALIZED:
            PosDomainService._sync_sale_payment_totals(sale)
            remaining_due = cls._recompute_cod_expected(sale)
            if remaining_due > Decimal("0.00"):
                raw_cod = data.get("cod_collected_bdt")
                if raw_cod is None or str(raw_cod).strip() == "":
                    raise DeliveryDomainError(
                        {
                            "error_code": "ERR_COD_COLLECTION_REQUIRED",
                            "cod_collected_bdt": f"Exact COD collection of {remaining_due:.2f} BDT is required.",
                            "message_en": f"Enter COD payment collected ({remaining_due:.2f} BDT remaining).",
                            "message_bn": f"বকেয়া COD মূল্য ({remaining_due:.2f} টাকা) সংগ্রহ করে নিশ্চিত করুন।",
                        }
                    )
                collected = parse_strict_decimal(
                    raw_cod, "cod_collected_bdt", min_val=Decimal("0.01"), error_code="ERR_COD_COLLECTION_REQUIRED"
                )
                tender = str(data.get("cod_tender_type") or data.get("cod_payment_method") or PaymentMethod.CASH).strip().upper()
                if tender not in cls.COD_ALLOWED_TENDERS:
                    raise DeliveryDomainError({"cod_tender_type": f"Unsupported COD tender '{tender}'."})
                if collected < remaining_due:
                    raise DeliveryDomainError(
                        {
                            "error_code": "ERR_INSUFFICIENT_COD_AMOUNT",
                            "cod_collected_bdt": f"Collected {collected:.2f} BDT is less than required {remaining_due:.2f} BDT.",
                            "message_en": f"Collected amount ({collected:.2f} BDT) is less than due ({remaining_due:.2f} BDT).",
                            "message_bn": f"সংগৃহীত অর্থ ({collected:.2f} টাকা) পাওনা ({remaining_due:.2f} টাকা)-এর চেয়ে কম।",
                        }
                    )
                if tender != PaymentMethod.CASH and collected != remaining_due:
                    raise DeliveryDomainError(
                        {
                            "error_code": "ERR_NON_CASH_OVERPAYMENT",
                            "cod_collected_bdt": f"Non-cash tender '{tender}' must exactly match remaining {remaining_due:.2f} BDT.",
                            "message_en": f"Digital payment must exactly match {remaining_due:.2f} BDT.",
                            "message_bn": f"ডিজিটাল পেমেন্ট ঠিক {remaining_due:.2f} টাকার সমান হতে হবে।",
                        }
                    )
                cod_idem = str(data.get("payment_idempotency_key") or f"cod-{delivery.delivery_id}-{delivery.version}")[:128]
                PosDomainService.record_payment_attempt(
                    user,
                    sale,
                    {
                        "tender_type": tender,
                        "amount_bdt": f"{collected:.2f}",
                        "status": PosPaymentAttemptStatus.CAPTURED,
                        "idempotency_key": cod_idem,
                        "payer_reference": pod_ref,
                    },
                )
                delivery.cod_collected_bdt = remaining_due
            sale = PosDomainService.finalize_sale(user, sale)
            pids = list(sale.lines.values_list("product_id", flat=True))

        now = timezone.now()
        old_state = delivery.fulfillment_state
        delivery.sale = sale
        delivery.fulfillment_state = PosDeliveryFulfillmentState.DELIVERED
        delivery.delivered_at = now
        delivery.delivered_by = user
        delivery.proof_of_delivery_ref = pod_ref
        delivery.proof_recipient_relation = str(data.get("proof_recipient_relation") or "SELF")[:64]
        delivery.stock_reserved = False
        delivery.cod_expected_bdt = Decimal("0.00")
        if bool(data.get("redact_address_after_delivery", False)):
            delivery.delivery_address_line = "[REDACTED_AFTER_COMPLETION]"
            delivery.delivery_landmark_notes = ""
            delivery.address_redacted_at = now
            delivery.address_redacted_by = user
        delivery.version += 1
        delivery.updated_by = user
        delivery.save()

        cls._record_delivery_audit(
            tenant=delivery.tenant,
            branch=delivery.branch,
            delivery=delivery,
            sale=sale,
            action="DELIVERED",
            user=user,
            from_state=old_state,
            to_state=delivery.fulfillment_state,
            reason=f"Delivered with POD {pod_ref}",
            metadata={
                "proof_of_delivery_ref": pod_ref,
                "cod_collected_bdt": f"{delivery.cod_collected_bdt:.2f}",
                "linked_invoice_id": sale.linked_invoice_id or "",
            },
        )
        return delivery, pids

    @classmethod
    @transaction.atomic
    def fail_delivery_attempt(
        cls, user: Any, delivery: PosDeliveryOrder, data: Optional[Dict[str, Any]] = None
    ) -> PosDeliveryOrder:
        data = data or {}
        delivery = PosDeliveryOrder.objects.select_for_update().select_related("sale", "branch", "tenant").get(pk=delivery.pk)
        PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=delivery.branch_id, require_write=True, allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES
        )
        if delivery.fulfillment_state not in (
            PosDeliveryFulfillmentState.DISPATCHED,
            PosDeliveryFulfillmentState.OUT_FOR_DELIVERY,
        ):
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "message_en": f"Cannot record failed attempt in '{delivery.fulfillment_state}' state.",
                    "message_bn": f"'{delivery.fulfillment_state}' অবস্থায় ব্যর্থ ডেলিভারি রেকর্ড করা যাবে না।",
                }
            )
        reason = str(data.get("reason") or "").strip()[:255]
        if not reason:
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_REASON_REQUIRED",
                    "reason": "Failure reason is required.",
                    "message_en": "Provide a reason for the failed delivery attempt.",
                    "message_bn": "ডেলিভারি ব্যর্থ হওয়ার কারণ উল্লেখ করুন।",
                }
            )
        old_state = delivery.fulfillment_state
        delivery.fulfillment_state = PosDeliveryFulfillmentState.FAILED_ATTEMPT
        delivery.failed_attempt_count += 1
        delivery.last_failure_reason = reason
        delivery.version += 1
        delivery.updated_by = user
        delivery.save()
        cls._record_delivery_audit(
            tenant=delivery.tenant,
            branch=delivery.branch,
            delivery=delivery,
            sale=delivery.sale,
            action="FAILED_ATTEMPT",
            user=user,
            from_state=old_state,
            to_state=delivery.fulfillment_state,
            reason=reason,
            metadata={"failed_attempt_count": delivery.failed_attempt_count},
        )
        return delivery

    @classmethod
    def return_to_branch(
        cls, user: Any, delivery: PosDeliveryOrder, data: Optional[Dict[str, Any]] = None
    ) -> PosDeliveryOrder:
        data = data or {}
        expected_version = cls.parse_strict_version(data.get("expected_version"))
        updated, pids = cls._return_to_branch_atomic(user, delivery.delivery_id, data, expected_version)
        if pids:
            PosHeldCartService._refresh_read_model_safe(user, updated.branch_id, pids)
        return updated

    @classmethod
    @transaction.atomic
    def _return_to_branch_atomic(
        cls, user: Any, delivery_id: str, data: Dict[str, Any], expected_version: Optional[int]
    ) -> Tuple[PosDeliveryOrder, List[str]]:
        delivery = PosDeliveryOrder.objects.select_for_update().select_related("sale", "branch", "tenant").get(pk=delivery_id)
        PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=delivery.branch_id, require_write=True, allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES
        )
        cls._assert_version_match(delivery, expected_version, user, "RETURN_REJECTED")
        if delivery.fulfillment_state not in (
            PosDeliveryFulfillmentState.DISPATCHED,
            PosDeliveryFulfillmentState.OUT_FOR_DELIVERY,
            PosDeliveryFulfillmentState.FAILED_ATTEMPT,
        ):
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "message_en": f"Cannot return to branch from '{delivery.fulfillment_state}' state.",
                    "message_bn": f"'{delivery.fulfillment_state}' অবস্থায় শাখায় ফেরত পাঠানো যাবে না।",
                }
            )
        reason = str(data.get("reason") or "Undeliverable order returned to branch").strip()[:255]
        sale = PosSaleOrder.objects.select_for_update().get(pk=delivery.sale_id)
        pids: List[str] = list(sale.lines.values_list("product_id", flat=True))
        if sale.lifecycle_state == PosSaleLifecycleState.FINALIZED:
            # Elevated roles reverse finalized invoice/stock cleanly; cashiers returning unfinalized COD release reservations
            role = str(getattr(user, "role", "")).upper()
            if role not in PosDomainService.POS_ELEVATED_ROLES:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Returning a prepaid/finalized delivery requires Manager or Pharmacist approval.",
                        "message_bn": "চূড়ান্ত হওয়া ডেলিভারি রিটার্ন করতে ম্যানেজার বা ফার্মাসিস্টের অনুমোদন প্রয়োজন।",
                    }
                )
            sale = PosDomainService.reverse_finalized_sale(user, sale, f"Delivery returned to branch: {reason}")
        else:
            PosHeldCartService._release_sale_reservations(
                sale, target_status=PosAllocationStatus.RELEASED, only_if_expired_or_unsafe=False
            )
            sale.hold_stock_reserved = False
            sale.save(update_fields=["hold_stock_reserved", "updated_at"])
        sale.refresh_from_db()

        old_state = delivery.fulfillment_state
        delivery.sale = sale
        delivery.fulfillment_state = PosDeliveryFulfillmentState.RETURNED_TO_BRANCH
        delivery.stock_reserved = False
        delivery.last_failure_reason = reason
        delivery.version += 1
        delivery.updated_by = user
        delivery.save()

        cls._record_delivery_audit(
            tenant=delivery.tenant,
            branch=delivery.branch,
            delivery=delivery,
            sale=sale,
            action="RETURNED_TO_BRANCH",
            user=user,
            from_state=old_state,
            to_state=delivery.fulfillment_state,
            reason=reason,
        )
        return delivery, pids

    @classmethod
    def cancel_delivery(
        cls, user: Any, delivery: PosDeliveryOrder, data: Optional[Dict[str, Any]] = None
    ) -> PosDeliveryOrder:
        data = data or {}
        expected_version = cls.parse_strict_version(data.get("expected_version"))
        updated, pids = cls._cancel_delivery_atomic(user, delivery.delivery_id, data, expected_version)
        if pids:
            PosHeldCartService._refresh_read_model_safe(user, updated.branch_id, pids)
        return updated

    @classmethod
    @transaction.atomic
    def _cancel_delivery_atomic(
        cls, user: Any, delivery_id: str, data: Dict[str, Any], expected_version: Optional[int]
    ) -> Tuple[PosDeliveryOrder, List[str]]:
        delivery = PosDeliveryOrder.objects.select_for_update().select_related("sale", "branch", "tenant").get(pk=delivery_id)
        PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=delivery.branch_id, require_write=True, allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES
        )
        cls._assert_version_match(delivery, expected_version, user, "CANCEL_REJECTED")
        if delivery.fulfillment_state not in cls.MUTABLE_DELIVERY_STATES:
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "detail": f"Cannot cancel delivery in '{delivery.fulfillment_state}' state.",
                    "message_en": f"Orders in '{delivery.fulfillment_state}' state cannot be cancelled directly; use return-to-branch.",
                    "message_bn": f"'{delivery.fulfillment_state}' অবস্থার অর্ডার সরাসরি বাতিল করা যাবে না।",
                }
            )
        reason = str(data.get("reason") or "Cancelled by customer/operator").strip()[:255]
        sale = PosSaleOrder.objects.select_for_update().get(pk=delivery.sale_id)
        pids = list(sale.lines.values_list("product_id", flat=True))
        if sale.lifecycle_state == PosSaleLifecycleState.FINALIZED:
            sale = PosDomainService.reverse_finalized_sale(user, sale, f"Delivery cancelled: {reason}")
        else:
            PosHeldCartService._release_sale_reservations(
                sale, target_status=PosAllocationStatus.RELEASED, only_if_expired_or_unsafe=False
            )
            sale.hold_stock_reserved = False
            sale.fulfillment_mode = PosOrderFulfillmentMode.COUNTER_WALKIN
            sale.save(update_fields=["hold_stock_reserved", "fulfillment_mode", "updated_at"])
            if bool(data.get("void_sale", True)):
                sale = PosDomainService.void_pre_final_sale(user, sale, reason)
        sale.refresh_from_db()

        now = timezone.now()
        old_state = delivery.fulfillment_state
        delivery.sale = sale
        delivery.fulfillment_state = PosDeliveryFulfillmentState.CANCELLED
        delivery.stock_reserved = False
        delivery.cancelled_at = now
        delivery.cancel_reason = reason
        if bool(data.get("redact_address", False)):
            delivery.delivery_address_line = "[REDACTED_ON_CANCEL]"
            delivery.delivery_landmark_notes = ""
            delivery.address_redacted_at = now
            delivery.address_redacted_by = user
        delivery.version += 1
        delivery.updated_by = user
        delivery.save()

        cls._record_delivery_audit(
            tenant=delivery.tenant,
            branch=delivery.branch,
            delivery=delivery,
            sale=sale,
            action="CANCELLED",
            user=user,
            from_state=old_state,
            to_state=delivery.fulfillment_state,
            reason=reason,
        )
        return delivery, pids

    @classmethod
    def reveal_address(
        cls, user: Any, delivery: PosDeliveryOrder, data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Audited operational access to full recipient street address during active pre-terminal fulfillment."""
        data = data or {}
        try:
            return cls._reveal_address_atomic(user, delivery.delivery_id, data)
        except _DeliveryAbort as abort:
            cls._record_delivery_audit(**abort.audit_kwargs)
            raise abort.exc

    @classmethod
    @transaction.atomic
    def _reveal_address_atomic(
        cls, user: Any, delivery_id: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        delivery = PosDeliveryOrder.objects.select_for_update().select_related("sale", "branch", "tenant").get(pk=delivery_id)
        PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=delivery.branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
        )
        reason = str(data.get("reason") or "").strip()[:255]
        if len(reason) < 3:
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_REASON_REQUIRED",
                    "reason": "A documented operational reason (>= 3 chars) is mandatory to reveal customer address.",
                    "message_en": "Operational reason is mandatory to view unmasked delivery address.",
                    "message_bn": "সম্পূর্ণ ডেলিভারি ঠিকানা দেখার জন্য উপযুক্ত কারণ উল্লেখ করা বাধ্যতামূলক।",
                }
            )
        if delivery.is_archived or delivery.address_redacted_at or delivery.fulfillment_state not in cls.REVEAL_ALLOWED_STATES:
            raise _DeliveryAbort(
                audit_kwargs={
                    "tenant": delivery.tenant,
                    "branch": delivery.branch,
                    "delivery": delivery,
                    "sale": delivery.sale,
                    "action": "ADDRESS_REVEAL_DENIED",
                    "from_state": delivery.fulfillment_state,
                    "to_state": delivery.fulfillment_state,
                    "user": user,
                    "reason": reason,
                    "was_rejected": True,
                    "rejection_code": "ERR_ADDRESS_PRIVACY_LOCKED",
                    "rejection_message": "Address privacy locked after terminal/redacted state.",
                },
                exc=DeliveryDomainError(
                    {
                        "error_code": "ERR_ADDRESS_PRIVACY_LOCKED",
                        "detail": "Full recipient address is locked after delivery completion, cancellation, or redaction.",
                        "message_en": "Address privacy is locked for completed, cancelled, or redacted deliveries.",
                        "message_bn": "সম্পন্ন, বাতিল বা গোপনকৃত ডেলিভারির পূর্ণ ঠিকানা আর দেখা যাবে না।",
                    }
                ),
            )

        audit = cls._record_delivery_audit(
            tenant=delivery.tenant,
            branch=delivery.branch,
            delivery=delivery,
            sale=delivery.sale,
            action="ADDRESS_REVEALED",
            user=user,
            from_state=delivery.fulfillment_state,
            to_state=delivery.fulfillment_state,
            reason=reason,
            metadata={"delivery_area": delivery.delivery_area, "delivery_city": delivery.delivery_city},
        )
        return {
            "delivery_id": delivery.delivery_id,
            "delivery_number": delivery.delivery_number,
            "recipient_name": delivery.recipient_name,
            "recipient_phone": delivery.recipient_phone,
            "delivery_address_line": delivery.delivery_address_line,
            "delivery_area": delivery.delivery_area,
            "delivery_city": delivery.delivery_city,
            "delivery_landmark_notes": delivery.delivery_landmark_notes,
            "_address_privacy_masked": False,
            "audit_id": audit.delivery_audit_id,
            "message_en": "Operational dispatch address revealed and audited.",
            "message_bn": "ডিসপ্যাচের জন্য পূর্ণ ঠিকানা প্রদর্শিত ও অডিটলগে নথিভুক্ত হয়েছে।",
        }

    @classmethod
    @transaction.atomic
    def redact_address(
        cls, user: Any, delivery: PosDeliveryOrder, data: Optional[Dict[str, Any]] = None
    ) -> PosDeliveryOrder:
        data = data or {}
        delivery = PosDeliveryOrder.objects.select_for_update().select_related("sale", "branch", "tenant").get(pk=delivery.pk)
        PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=delivery.branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
        )
        if delivery.fulfillment_state not in cls.REDACT_ALLOWED_STATES:
            raise DeliveryDomainError(
                {
                    "error_code": "ERR_DELIVERY_ACTIVE_CANNOT_REDACT",
                    "detail": f"Cannot redact address while delivery is active in '{delivery.fulfillment_state}' state.",
                    "message_en": "Active in-transit delivery addresses cannot be redacted yet.",
                    "message_bn": "সক্রিয় ডেলিভারি অবস্থায় ঠিকানা মুছে ফেলা যাবে না।",
                }
            )
        now = timezone.now()
        delivery.delivery_address_line = "[REDACTED_AFTER_COMPLETION]"
        delivery.delivery_landmark_notes = ""
        delivery.address_redacted_at = now
        delivery.address_redacted_by = user
        delivery.version += 1
        delivery.updated_by = user
        delivery.save()
        cls._record_delivery_audit(
            tenant=delivery.tenant,
            branch=delivery.branch,
            delivery=delivery,
            sale=delivery.sale,
            action="ADDRESS_REDACTED",
            user=user,
            from_state=delivery.fulfillment_state,
            to_state=delivery.fulfillment_state,
            reason=str(data.get("reason") or "Manual PII redaction after fulfillment"),
        )
        return delivery
