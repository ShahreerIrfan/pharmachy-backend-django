"""
backend/apps/core/pos_domain_service.py
Architecture Decision: ADR-0221 (P221).
Phase 12: POS, Sales Orders, Checkout & Returns.

Canonical POS Domain Service governing:
- Multi-tenant customer ledger profiles & credit/due bounds with subscription feature gates.
- Sale order headers & lines with exact Decimal (BDT) financial calculations.
- FEFO stock batch reservations & immutable lot allocations with concurrency locking.
- Multi-tender split payment attempts (Cash, bKash, Nagad, Card, Due) with idempotency.
- Audited lifecycle state transitions (DRAFT -> HELD -> PENDING_RX_REVIEW -> CONFIRMED ->
  PARTIALLY_PAID -> FINALIZED -> REVERSED / VOIDED), bridging cleanly to SaleInvoice,
  SaleLotAllocation, PaymentTransaction, CustomerDueRecord, and StockLedgerEntry.
"""

from __future__ import annotations

import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStock,
    Branch,
    CashierRegisterAssignment,
    InvoiceStatus,
    LoginDeviceRegistration,
    MedicineProduct,
    MedicineVariant,
    PaymentMethod,
    PlanCatalogueRecord,
    PlanTier,
    PosAllocationStatus,
    PosCustomer,
    PosDiscountType,
    PosFefoAllocationStatus,
    PosPackagingUnit,
    PosPaymentAttempt,
    PosPaymentAttemptStatus,
    PosRoundingMode,
    PosSaleLifecycleAudit,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleOrderLine,
    PosSaleSourceChannel,
    PosSyncState,
    PosTaxMode,
    PrescriptionRecord,
    PrescriptionStatus,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    SaleInvoice,
    SaleInvoiceItem,
    SaleLotAllocation,
    SaleLotAllocationStatus,
    StaffMember,
    StockMovementType,
    Tenant,
    TenantAppliedEntitlement,
    TenantLifecycleStatus,
)
from apps.core.stock_ledger_service import StockLedgerService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.core.pos_domain")

MONEY_Q = Decimal("0.01")
COST_Q = Decimal("0.0001")
CASHIER_MAX_DISCOUNT_PCT = Decimal("0.05")  # 5% discount ceiling before Manager/Owner override


def q2(value: Any) -> Decimal:
    return Decimal(str(value or "0.00")).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def q4(value: Any) -> Decimal:
    return Decimal(str(value or "0.0000")).quantize(COST_Q, rounding=ROUND_HALF_UP)


class PosDomainService:
    """
    Transactional service implementing POS customers, sale orders, allocations,
    payment attempts, and audited lifecycle state transitions.
    """

    TENANT_WIDE_ROLES = {
        UserRole.SUPERADMIN,
        UserRole.PHARMACY_OWNER,
        UserRole.FINANCE_OFFICER,
        UserRole.AUDITOR,
        "SUPERADMIN",
        "PHARMACY_OWNER",
        "FINANCE_OFFICER",
        "AUDITOR",
    }

    POS_READ_ROLES = {
        UserRole.SUPERADMIN,
        UserRole.PHARMACY_OWNER,
        UserRole.BRANCH_MANAGER,
        UserRole.STAFF_PHARMACIST,
        UserRole.CASHIER,
        UserRole.FINANCE_OFFICER,
        UserRole.AUDITOR,
        "SUPERADMIN",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "STAFF_PHARMACIST",
        "CASHIER",
        "FINANCE_OFFICER",
        "AUDITOR",
    }

    POS_SALE_WRITE_ROLES = {
        UserRole.SUPERADMIN,
        UserRole.PHARMACY_OWNER,
        UserRole.BRANCH_MANAGER,
        UserRole.STAFF_PHARMACIST,
        UserRole.CASHIER,
        "SUPERADMIN",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "STAFF_PHARMACIST",
        "CASHIER",
    }

    POS_ELEVATED_ROLES = {
        UserRole.SUPERADMIN,
        UserRole.PHARMACY_OWNER,
        UserRole.BRANCH_MANAGER,
        UserRole.STAFF_PHARMACIST,
        "SUPERADMIN",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "STAFF_PHARMACIST",
    }

    POS_CUSTOMER_ADMIN_ROLES = {
        UserRole.SUPERADMIN,
        UserRole.PHARMACY_OWNER,
        UserRole.BRANCH_MANAGER,
        UserRole.FINANCE_OFFICER,
        "SUPERADMIN",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "FINANCE_OFFICER",
    }

    COST_EXEMPT_ROLES = {
        UserRole.SUPERADMIN,
        UserRole.PHARMACY_OWNER,
        UserRole.BRANCH_MANAGER,
        UserRole.FINANCE_OFFICER,
        UserRole.AUDITOR,
        "SUPERADMIN",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "FINANCE_OFFICER",
        "AUDITOR",
    }

    MUTABLE_SALE_STATES = {
        PosSaleLifecycleState.DRAFT,
        PosSaleLifecycleState.HELD,
        PosSaleLifecycleState.PENDING_RX_REVIEW,
    }

    ALLOWED_TRANSITIONS = {
        PosSaleLifecycleState.DRAFT: {
            PosSaleLifecycleState.HELD,
            PosSaleLifecycleState.PENDING_RX_REVIEW,
            PosSaleLifecycleState.CONFIRMED,
            PosSaleLifecycleState.PARTIALLY_PAID,
            PosSaleLifecycleState.FINALIZED,
            PosSaleLifecycleState.VOIDED,
            PosSaleLifecycleState.OFFLINE_PENDING_SYNC,
        },
        PosSaleLifecycleState.HELD: {
            PosSaleLifecycleState.DRAFT,
            PosSaleLifecycleState.PENDING_RX_REVIEW,
            PosSaleLifecycleState.CONFIRMED,
            PosSaleLifecycleState.PARTIALLY_PAID,
            PosSaleLifecycleState.FINALIZED,
            PosSaleLifecycleState.VOIDED,
        },
        PosSaleLifecycleState.PENDING_RX_REVIEW: {
            PosSaleLifecycleState.CONFIRMED,
            PosSaleLifecycleState.PARTIALLY_PAID,
            PosSaleLifecycleState.VOIDED,
        },
        PosSaleLifecycleState.CONFIRMED: {
            PosSaleLifecycleState.PARTIALLY_PAID,
            PosSaleLifecycleState.FINALIZED,
            PosSaleLifecycleState.VOIDED,
        },
        PosSaleLifecycleState.PARTIALLY_PAID: {
            PosSaleLifecycleState.FINALIZED,
            PosSaleLifecycleState.VOIDED,
        },
        PosSaleLifecycleState.FINALIZED: {
            PosSaleLifecycleState.PARTIALLY_RETURNED,
            PosSaleLifecycleState.FULLY_RETURNED,
            PosSaleLifecycleState.REVERSED,
        },
        PosSaleLifecycleState.PARTIALLY_RETURNED: {
            PosSaleLifecycleState.FULLY_RETURNED,
            PosSaleLifecycleState.REVERSED,
        },
        PosSaleLifecycleState.OFFLINE_PENDING_SYNC: {
            PosSaleLifecycleState.FINALIZED,
            PosSaleLifecycleState.SYNC_CONFLICT_REVIEW,
        },
        PosSaleLifecycleState.SYNC_CONFLICT_REVIEW: {
            PosSaleLifecycleState.FINALIZED,
            PosSaleLifecycleState.REVERSED,
            PosSaleLifecycleState.VOIDED,
        },
        PosSaleLifecycleState.FULLY_RETURNED: set(),
        PosSaleLifecycleState.VOIDED: set(),
        PosSaleLifecycleState.REVERSED: set(),
    }

    @classmethod
    def can_view_cost(cls, user: Any) -> bool:
        role = str(getattr(user, "role", "")).upper()
        return role in cls.COST_EXEMPT_ROLES

    @classmethod
    def assert_tenant_active(cls, tenant: Tenant) -> None:
        if not tenant.is_active or tenant.status in (
            TenantLifecycleStatus.SUSPENDED,
            TenantLifecycleStatus.ARCHIVED,
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUBSCRIPTION_SUSPENDED",
                    "message_en": f"Tenant subscription is {tenant.status}. POS operations are locked.",
                    "message_bn": "টেন্যান্ট সাবস্ক্রিপশন স্থগিত বা আর্কাইভড। পিওএস কার্যক্রম বন্ধ আছে।",
                }
            )

    @classmethod
    def has_feature_entitlement(cls, tenant: Tenant, feature_key: str) -> bool:
        ent = TenantAppliedEntitlement.objects.filter(tenant=tenant, is_active=True).first()
        if ent and isinstance(ent.features_snapshot, dict):
            return bool(ent.features_snapshot.get(feature_key, False))
        # Derive from plan catalogue or tier defaults if entitlement row hasn't been materialized yet
        plan_rec = PlanCatalogueRecord.objects.filter(tier=tenant.plan_tier, is_active=True).first()
        if plan_rec and isinstance(plan_rec.features, dict):
            return bool(plan_rec.features.get(feature_key, False))
        if feature_key == "CUSTOMER_LEDGER":
            return str(tenant.plan_tier).upper() in (PlanTier.STANDARD, PlanTier.PRO)
        return True

    @classmethod
    def assert_feature_entitlement(cls, tenant: Tenant, feature_key: str, action_label: str) -> None:
        if not cls.has_feature_entitlement(tenant, feature_key):
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUBSCRIPTION_FEATURE_LOCKED",
                    "feature": feature_key,
                    "message_en": (
                        f"Your current plan ({tenant.plan_tier}) does not include '{feature_key}' "
                        f"required for {action_label}. Upgrade to Standard or Pro."
                    ),
                    "message_bn": (
                        f"আপনার বর্তমান প্ল্যান ({tenant.plan_tier})-এ '{feature_key}' সুবিধাটি নেই। "
                        f"স্ট্যান্ডার্ড বা প্রো প্ল্যানে আপগ্রেড করুন।"
                    ),
                }
            )

    @classmethod
    def resolve_user_tenant_and_branch(
        cls,
        user: Any,
        branch_id: Optional[str] = None,
        require_write: bool = False,
        allowed_roles: Optional[set[str]] = None,
    ) -> tuple[Tenant, Optional[Branch]]:
        if not user or not getattr(user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_AUTH_REQUIRED", "detail": "Authentication required."})

        role = str(getattr(user, "role", "")).upper()
        permitted = allowed_roles or (cls.POS_SALE_WRITE_ROLES if require_write else cls.POS_READ_ROLES)
        if role not in permitted:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": f"Role '{role}' is not authorized for this POS operation.",
                    "message_bn": f"'{role}' রোল-এর জন্য এই পিওএস কাজটি অনুমোদিত নয়।",
                }
            )

        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            raise PermissionDenied({"error_code": "ERR_TENANT_UNBOUND", "detail": "User is not bound to a tenant."})

        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": "Tenant not found."})

        cls.assert_tenant_active(tenant)

        if not branch_id:
            branch_id = getattr(user, "branch_id", None)

        branch_obj: Optional[Branch] = None
        if branch_id:
            branch_obj = Branch.objects.filter(branch_id=branch_id).first()
            if not branch_obj or branch_obj.tenant_id != tenant.tenant_id:
                raise NotFound(
                    {
                        "error_code": "ERR_BRANCH_NOT_FOUND",
                        "message_en": f"Branch '{branch_id}' was not found for your pharmacy tenant.",
                        "message_bn": "নির্ধারিত শাখাটি আপনার টেন্যান্টে খুঁজে পাওয়া যায়নি।",
                    }
                )
            actor_branch_id = getattr(user, "branch_id", None)
            if role not in cls.TENANT_WIDE_ROLES and actor_branch_id and str(actor_branch_id) != str(branch_obj.branch_id):
                raise PermissionDenied(
                    {
                        "error_code": "ERR_CROSS_BRANCH_ACCESS_DENIED",
                        "message_en": (
                            f"Branch-bound role '{role}' cannot access branch '{branch_obj.branch_id}' "
                            f"from assigned branch '{actor_branch_id}'."
                        ),
                        "message_bn": "নিজস্ব শাখার বাইরে পিওএস অপারেশন অনুমোদিত নয়।",
                    }
                )

        return tenant, branch_obj

    @classmethod
    def _record_lifecycle_audit(
        cls,
        sale: PosSaleOrder,
        from_state: str,
        to_state: str,
        action: str,
        user: Any,
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PosSaleLifecycleAudit:
        actor_role = str(getattr(user, "role", "SYSTEM")).upper()
        audit = PosSaleLifecycleAudit.objects.create(
            tenant=sale.tenant,
            branch=sale.branch,
            sale=sale,
            from_state=from_state,
            to_state=to_state,
            action=action,
            actor_user=user if getattr(user, "is_authenticated", False) else None,
            actor_role=actor_role,
            reason=reason,
            metadata_json=metadata or {},
        )
        AuditLogEntry.objects.create(
            tenant_id=sale.tenant_id,
            branch_id=sale.branch_id,
            user_id=str(getattr(user, "user_id", "") or getattr(user, "pk", "")),
            actor_email=str(getattr(user, "email", "")),
            actor_role=actor_role,
            request_id=f"pos-{uuid.uuid4().hex[:10]}",
            action=f"POS_{action}",
            resource_type="POS_SALE_ORDER",
            resource_id=sale.sale_id,
            status=AuditActionStatus.SUCCESS,
            before_state={"lifecycle_state": from_state, "grand_total_bdt": str(sale.grand_total_bdt)},
            after_state={"lifecycle_state": to_state, "grand_total_bdt": str(sale.grand_total_bdt)},
            metadata_json={"order_number": sale.order_number, "reason": reason, **(metadata or {})},
        )
        return audit

    @classmethod
    def compute_line_and_header_totals(
        cls,
        sale: PosSaleOrder,
        actor_user: Any,
        *,
        allow_empty: bool = False,
    ) -> PosSaleOrder:
        """
        Deterministically computes line subtotals, VAT/tax (EXCLUSIVE or INCLUSIVE),
        non-compounding discounts, rounding mode adjustments, and header grand total
        using exact Decimal (ROUND_HALF_UP). Enforces discount ceilings against Cashier role.
        """
        from apps.core.pos_cart_pricing_service import PosCartPricingService

        updated_sale, _ = PosCartPricingService.recompute_sale_totals(
            sale, actor_user, allow_empty=allow_empty
        )
        return updated_sale

    @classmethod
    @transaction.atomic
    def create_sale_order(cls, user: Any, data: Dict[str, Any]) -> tuple[PosSaleOrder, bool]:
        """
        Creates a PosSaleOrder with nested lines idempotently by (tenant, idempotency_key).
        Returns (sale, created_bool).
        """
        branch_id = data.get("branch") or data.get("branch_id")
        tenant, branch = cls.resolve_user_tenant_and_branch(user, branch_id=branch_id, require_write=True)
        if not branch:
            raise ValidationError({"branch": "Target branch is required to create a POS sale order."})

        idem_key = str(data.get("idempotency_key") or "").strip()
        if idem_key:
            existing = (
                PosSaleOrder.objects.filter(tenant=tenant, idempotency_key=idem_key)
                .prefetch_related("lines__product", "allocations", "payment_attempts")
                .first()
            )
            if existing:
                return existing, False

        off_ns = str(data.get("offline_device_namespace") or "").strip()
        off_seq = data.get("offline_local_sequence")
        if off_ns and off_seq is not None:
            existing_off = (
                PosSaleOrder.objects.filter(
                    tenant=tenant,
                    offline_device_namespace=off_ns,
                    offline_local_sequence=off_seq,
                )
                .prefetch_related("lines__product", "allocations", "payment_attempts")
                .first()
            )
            if existing_off:
                return existing_off, False

        lines_data = data.get("lines") or []
        if not isinstance(lines_data, list) or len(lines_data) == 0:
            raise ValidationError({"lines": "At least one line item is required."})

        customer = None
        customer_id = data.get("customer") or data.get("customer_id")
        if customer_id:
            customer = PosCustomer.objects.filter(customer_id=customer_id, tenant=tenant, is_archived=False).first()
            if not customer:
                raise ValidationError({"customer": f"Customer '{customer_id}' was not found in this tenant."})

        register_assignment = None
        reg_id = data.get("register_assignment") or data.get("register_assignment_id")
        if reg_id:
            register_assignment = CashierRegisterAssignment.objects.filter(
                assignment_id=reg_id, tenant=tenant, branch=branch
            ).first()
            if not register_assignment:
                raise ValidationError({"register_assignment": "Register assignment not found in this branch."})

        device = None
        device_reg_id = data.get("device") or data.get("device_id")
        if device_reg_id:
            device = LoginDeviceRegistration.objects.filter(
                registration_id=device_reg_id, tenant=tenant, branch=branch
            ).first()
            if not device:
                raise ValidationError({"device": "Device registration not found in this branch."})

        prescription = None
        rx_id = data.get("prescription") or data.get("prescription_id")
        if rx_id:
            prescription = PrescriptionRecord.objects.filter(
                prescription_id=rx_id, tenant=tenant, branch=branch
            ).first()
            if not prescription:
                raise ValidationError({"prescription": "Prescription record not found in this branch."})

        cashier_staff = StaffMember.objects.filter(tenant=tenant, username=getattr(user, "email", "")).first()
        requested_state = str(data.get("lifecycle_state") or PosSaleLifecycleState.DRAFT).upper()
        if requested_state not in (
            PosSaleLifecycleState.DRAFT,
            PosSaleLifecycleState.HELD,
            PosSaleLifecycleState.OFFLINE_PENDING_SYNC,
        ):
            raise ValidationError(
                {"lifecycle_state": f"Initial sale order state must be DRAFT, HELD, or OFFLINE_PENDING_SYNC. Got '{requested_state}'."}
            )

        discount_type = str(data.get("discount_type") or PosDiscountType.NONE).upper()
        if discount_type not in PosDiscountType.values:
            raise ValidationError({"discount_type": f"Invalid discount type '{discount_type}'."})

        tax_mode = str(data.get("tax_mode") or PosTaxMode.EXCLUSIVE).upper()
        if tax_mode not in PosTaxMode.values:
            raise ValidationError({"tax_mode": f"Invalid tax_mode '{tax_mode}'."})

        rounding_mode = str(data.get("rounding_mode") or PosRoundingMode.NONE).upper()
        if rounding_mode not in PosRoundingMode.values:
            raise ValidationError({"rounding_mode": f"Invalid rounding_mode '{rounding_mode}'."})

        raw_hdr_disc = data.get("header_discount_amount_bdt")
        if raw_hdr_disc is None and discount_type in (PosDiscountType.FLAT_BDT, PosDiscountType.PROMO_POLICY):
            raw_hdr_disc = data.get("discount_amount_bdt", "0.00")
        hdr_disc_val = q2(raw_hdr_disc or "0.00")

        order_num = str(data.get("order_number") or "").strip()
        offline_ns = str(data.get("offline_device_namespace") or "").strip()
        offline_seq = data.get("offline_local_sequence")
        from apps.core.pos_invoice_number_service import PosInvoiceNumberService

        if order_num:
            if offline_ns:
                PosInvoiceNumberService.validate_and_register_offline_invoice(
                    tenant=tenant,
                    branch=branch,
                    invoice_number=order_num,
                    offline_device_namespace=offline_ns,
                    offline_local_sequence=offline_seq,
                )
        else:
            dev_slug = offline_ns or (device.device_identifier if device else "ONLINE")
            gen_res = PosInvoiceNumberService.generate_next_invoice_number_in_tx(
                tenant=tenant,
                branch=branch,
                device_namespace=dev_slug,
                reserve=True,
            )
            order_num = gen_res["invoice_number"]

        sale = PosSaleOrder(
            sale_id=f"sale-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            order_number=order_num,
            idempotency_key=idem_key,
            offline_device_namespace=str(data.get("offline_device_namespace") or "").strip(),
            offline_local_sequence=data.get("offline_local_sequence"),
            device=device,
            register_assignment=register_assignment,
            cashier_user=user,
            cashier_staff=cashier_staff,
            customer=customer,
            customer_name=str(data.get("customer_name") or (customer.full_name if customer else "Walk-in Customer")),
            customer_phone=str(data.get("customer_phone") or (customer.phone if customer else "")),
            prescription=prescription,
            source_channel=str(data.get("source_channel") or PosSaleSourceChannel.DESKTOP_POS_ONLINE),
            sync_status=(
                PosSyncState.LOCAL_PENDING_SYNC
                if requested_state == PosSaleLifecycleState.OFFLINE_PENDING_SYNC
                else str(data.get("sync_status") or PosSyncState.CLOUD_CONFIRMED)
            ),
            lifecycle_state=requested_state,
            discount_type=discount_type,
            discount_rate_pct=q2(data.get("discount_rate_pct", "0.00")),
            header_discount_amount_bdt=hdr_disc_val,
            discount_amount_bdt=q2(data.get("discount_amount_bdt", hdr_disc_val)),
            tax_mode=tax_mode,
            tax_rate_pct=q2(data.get("tax_rate_pct", "0.00")),
            rounding_mode=rounding_mode,
            rounding_adjustment_bdt=q2(data.get("rounding_adjustment_bdt", "0.00")),
            discount_reason=str(data.get("discount_reason") or ""),
            hold_reference=str(data.get("hold_reference") or ""),
            held_at=timezone.now() if requested_state == PosSaleLifecycleState.HELD else None,
            notes=str(data.get("notes") or ""),
            created_by=user,
            updated_by=user,
        )
        if prescription and prescription.status in (PrescriptionStatus.VERIFIED, PrescriptionStatus.DISPENSED):
            sale.rx_verified_at = prescription.verified_at or timezone.now()
            sale.rx_verified_by = user

        sale.save()

        for idx, raw_line in enumerate(lines_data, start=1):
            from apps.core.pos_cart_unit_service import PosCartUnitSelectionService

            product_id = raw_line.get("product") or raw_line.get("product_id")
            product = MedicineProduct.objects.filter(product_id=product_id).first()
            if not product:
                raise ValidationError({"lines": f"Line {idx}: product '{product_id}' not found."})

            var = None
            var_id = raw_line.get("variant") or raw_line.get("variant_id")
            if var_id:
                var = MedicineVariant.objects.filter(
                    variant_id=str(var_id).strip(), product=product, is_active=True
                ).first()
                if not var:
                    raise ValidationError({"lines": f"Line {idx}: variant '{var_id}' not found for product."})

            pref_batch = None
            pref_batch_id = raw_line.get("preferred_batch") or raw_line.get("preferred_batch_id")
            if pref_batch_id:
                pref_batch = BatchStock.objects.filter(
                    batch_id=pref_batch_id, tenant=tenant, branch=branch, product=product
                ).first()
                if not pref_batch:
                    raise ValidationError({"lines": f"Line {idx}: preferred batch '{pref_batch_id}' not found in branch."})

            qty_units = PosCartUnitSelectionService.parse_strict_positive_int(
                raw_line.get("quantity_units", 0), "quantity_units"
            )
            norm_form = PosCartUnitSelectionService.normalize_dosage_form(product)
            default_pkg = PosCartUnitSelectionService.FORM_BASE_UNIT.get(norm_form, PosPackagingUnit.TABLET)
            pkg_unit = str(raw_line.get("packaging_unit") or default_pkg).strip().upper()
            if pkg_unit not in PosPackagingUnit.values:
                raise ValidationError({"lines": f"Line {idx}: unsupported packaging_unit '{pkg_unit}'."})

            raw_factor = raw_line.get("conversion_factor")
            evaluated_unit = PosCartUnitSelectionService.evaluate_unit_selection(
                tenant=tenant,
                branch=branch,
                product=product,
                variant=var,
                packaging_unit=pkg_unit,
                requested_conversion_factor=raw_factor,
                quantity_units=qty_units,
                actor_user=user,
                override_reason=str(raw_line.get("override_reason") or raw_line.get("notes") or ""),
            )
            conv_factor = int(evaluated_unit["conversion_factor"])

            unit_price = raw_line.get("unit_price_bdt")
            resolved_price = q2(unit_price) if unit_price is not None else q2(evaluated_unit["unit_price_bdt"])
            if resolved_price <= Decimal("0.00"):
                raise ValidationError({"lines": f"Line {idx}: unit_price_bdt must be greater than zero."})

            unit_cost = q4(
                raw_line.get("unit_cost_bdt")
                if raw_line.get("unit_cost_bdt") is not None
                else (pref_batch.cost_per_unit if pref_batch else product.cost_unit)
            )

            PosSaleOrderLine.objects.create(
                line_id=f"sline-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                branch=branch,
                sale=sale,
                line_number=int(raw_line.get("line_number") or idx),
                product=product,
                variant=var,
                preferred_batch=pref_batch,
                packaging_unit=evaluated_unit["packaging_unit"],
                quantity_units=qty_units,
                conversion_factor=conv_factor,
                base_units_total=qty_units * conv_factor,
                unit_price_bdt=resolved_price,
                discount_type=str(raw_line.get("discount_type") or PosDiscountType.NONE).upper(),
                discount_rate_pct=q2(raw_line.get("discount_rate_pct", "0.00")),
                discount_amount_bdt=q2(raw_line.get("discount_amount_bdt", "0.00")),
                tax_mode=str(raw_line.get("tax_mode") or sale.tax_mode or PosTaxMode.EXCLUSIVE).upper(),
                tax_rate_pct=q2(raw_line.get("tax_rate_pct", "0.00")),
                line_subtotal_bdt=q2(Decimal(qty_units) * resolved_price),
                line_total_bdt=q2(Decimal(qty_units) * resolved_price),
                price_source="EXPLICIT_LINE_PRICE" if unit_price is not None else "CANONICAL_MRP",
                unit_cost_bdt=unit_cost,
                requires_rx_verification=bool(
                    product.is_prescription_only or product.is_narcotic or product.is_antibiotic
                ),
                dosage_form_snapshot=evaluated_unit["dosage_form_snapshot"],
                container_policy_applied=evaluated_unit["container_policy_applied"],
                unit_conversion_source=evaluated_unit["conversion_source"],
                container_override_approved_by=evaluated_unit["override_approved_by"],
                container_override_reason=evaluated_unit["override_reason"],
                notes=str(raw_line.get("notes") or ""),
            )

        cls.compute_line_and_header_totals(sale, user)
        from apps.core.pos_rx_safety_service import PosRxSafetyService

        PosRxSafetyService.evaluate_and_sync_sale_rx_gate(sale, actor_user=user, mutate=True)
        sale.refresh_from_db()

        cls._record_lifecycle_audit(
            sale=sale,
            from_state="",
            to_state=sale.lifecycle_state,
            action="ORDER_CREATED",
            user=user,
            reason=sale.notes or "POS sale order created",
        )
        return sale, True

    @classmethod
    @transaction.atomic
    def update_mutable_sale(cls, user: Any, sale: PosSaleOrder, data: Dict[str, Any]) -> PosSaleOrder:
        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        cls.resolve_user_tenant_and_branch(user, branch_id=sale.branch_id, require_write=True)

        if sale.lifecycle_state not in cls.MUTABLE_SALE_STATES:
            raise ValidationError(
                {
                    "error_code": "ERR_IMMUTABLE_SALE_STATE",
                    "detail": (
                        f"Sale order '{sale.order_number}' is in state '{sale.lifecycle_state}' and cannot be edited. "
                        f"Use audited state transitions or reversal instead."
                    ),
                }
            )

        if "discount_type" in data:
            sale.discount_type = str(data["discount_type"]).upper()
            if sale.discount_type == PosDiscountType.NONE:
                sale.discount_rate_pct = Decimal("0.00")
                sale.header_discount_amount_bdt = Decimal("0.00")
        if "discount_rate_pct" in data:
            sale.discount_rate_pct = q2(data["discount_rate_pct"])
        if "header_discount_amount_bdt" in data:
            sale.header_discount_amount_bdt = q2(data["header_discount_amount_bdt"])
        elif "discount_amount_bdt" in data:
            sale.header_discount_amount_bdt = q2(data["discount_amount_bdt"])
            sale.discount_amount_bdt = q2(data["discount_amount_bdt"])
        if "tax_mode" in data:
            sale.tax_mode = str(data["tax_mode"]).upper()
        if "tax_rate_pct" in data:
            sale.tax_rate_pct = q2(data["tax_rate_pct"])
        if "rounding_mode" in data:
            sale.rounding_mode = str(data["rounding_mode"]).upper()
        if "rounding_adjustment_bdt" in data:
            sale.rounding_adjustment_bdt = q2(data["rounding_adjustment_bdt"])
            if "rounding_mode" not in data and sale.rounding_adjustment_bdt != Decimal("0.00"):
                sale.rounding_mode = PosRoundingMode.MANUAL_ADJ
        if "customer" in data or "customer_id" in data:
            cid = str(data.get("customer") if "customer" in data else data.get("customer_id") or "").strip()
            if cid:
                cust_obj = PosCustomer.objects.filter(
                    customer_id=cid, tenant=sale.tenant, is_archived=False, is_active=True
                ).first()
                if not cust_obj:
                    raise ValidationError({"customer": f"Customer '{cid}' was not found in this tenant."})
                sale.customer = cust_obj
                sale.customer_name = cust_obj.full_name
                sale.customer_phone = cust_obj.phone
            else:
                sale.customer = None
        if "customer_name" in data:
            sale.customer_name = str(data["customer_name"])
        if "customer_phone" in data:
            sale.customer_phone = str(data["customer_phone"])
        if "discount_reason" in data:
            sale.discount_reason = str(data["discount_reason"])
        if "notes" in data:
            sale.notes = str(data["notes"])
        sale.updated_by = user
        sale.version += 1
        sale.save()

        cls.compute_line_and_header_totals(sale, user)
        cls._record_lifecycle_audit(
            sale=sale,
            from_state=sale.lifecycle_state,
            to_state=sale.lifecycle_state,
            action="ORDER_UPDATED",
            user=user,
            reason="Updated mutable draft/held POS order",
        )
        return sale

    @classmethod
    def _release_reserved_allocations(cls, sale: PosSaleOrder) -> None:
        from apps.core.pos_fefo_checkout_service import PosFefoCheckoutService

        PosFefoCheckoutService._release_reserved_allocations_in_tx(sale)

    @classmethod
    def hold_or_resume_sale(
        cls, user: Any, sale: PosSaleOrder, target_state: str, hold_reference: str = ""
    ) -> PosSaleOrder:
        from apps.core.pos_held_cart_service import PosHeldCartService

        target_upper = target_state.upper()
        if target_upper == PosSaleLifecycleState.HELD:
            return PosHeldCartService.hold_cart(
                user,
                sale,
                {"hold_reference": hold_reference} if hold_reference else {},
            )
        if target_upper == PosSaleLifecycleState.DRAFT:
            resumed, _ = PosHeldCartService.resume_cart(user, sale, {})
            return resumed
        raise ValidationError(
            {
                "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                "detail": f"Unsupported target state '{target_upper}' for hold_or_resume_sale.",
            }
        )

    @classmethod
    def verify_rx_sale(
        cls,
        user: Any,
        sale: PosSaleOrder,
        notes: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> PosSaleOrder:
        from apps.core.pos_rx_safety_service import PosRxSafetyService

        return PosRxSafetyService.verify_sale_rx(user, sale, data=data, notes=notes)

    @classmethod
    def allocate_stock(
        cls,
        user: Any,
        sale: PosSaleOrder,
        *,
        batch_overrides: Any = None,
        override_reason: str = "",
        expected_version: Optional[int] = None,
    ) -> PosSaleOrder:
        """
        Reserves batch inventory strictly in deterministic FEFO + FIFO cost-lot order with
        row-level locks. Blocks expired, recalled, quarantined, and unauthorized override lots.
        """
        from apps.core.pos_fefo_checkout_service import PosFefoCheckoutService

        return PosFefoCheckoutService.allocate_sale_fefo(
            user,
            sale,
            batch_overrides=batch_overrides,
            override_reason=override_reason,
            expected_version=expected_version,
            transition_lifecycle=True,
        )

    @classmethod
    def _sync_sale_payment_totals(cls, sale: PosSaleOrder) -> PosSaleOrder:
        from apps.core.pos_payment_service import PosPaymentSplitService

        return PosPaymentSplitService.recalculate_sale_payment_totals(sale)

    @classmethod
    @transaction.atomic
    def record_payment_attempt(cls, user: Any, sale: PosSaleOrder, data: Dict[str, Any]) -> tuple[PosPaymentAttempt, bool]:
        from apps.core.pos_payment_service import PosPaymentSplitService

        return PosPaymentSplitService.record_single_attempt(user, sale, data)

    @classmethod
    def finalize_sale(
        cls,
        user: Any,
        sale: PosSaleOrder,
        *,
        expected_grand_total_bdt: Any = None,
        expected_pricing_hash: Optional[str] = None,
        is_offline_sync: bool = False,
        batch_overrides: Any = None,
        override_reason: str = "",
    ) -> PosSaleOrder:
        """
        Atomically finalizes a POS sale order:
        - Recomputes & verifies authoritative server totals and optional pricing confirmation hash/total.
        - Verifies Rx verification compliance, classification snapshots & offline Rx limitations.
        - Verifies or resolves safe FEFO + FIFO cost-lot allocations with row locks (rejects expired/recalled/quarantined).
        - Verifies exact tender settlement (paid + due == grand_total).
        - Deducts BatchStock via StockLedgerService and records SaleInvoice, SaleInvoiceItem,
          SaleLotAllocation, PaymentTransaction, CustomerDueRecord, and AntibioticDispenseRegisterEntry.
        """
        from apps.core.pos_cart_pricing_service import (
            PosCartPricingService,
            _PricingRejectionAbort,
            parse_strict_decimal,
        )
        from apps.core.pos_fefo_checkout_service import PosFefoCheckoutService, _FefoRejectionAbort
        from apps.core.pos_rx_safety_service import PosRxSafetyService, _RxRejectionAbort

        try:
            return cls._finalize_sale_atomic(
                user,
                sale,
                expected_grand_total_bdt=expected_grand_total_bdt,
                expected_pricing_hash=expected_pricing_hash,
                is_offline_sync=is_offline_sync,
                batch_overrides=batch_overrides,
                override_reason=override_reason,
                pricing_svc=PosCartPricingService,
                rejection_cls=_PricingRejectionAbort,
                decimal_parser=parse_strict_decimal,
            )
        except _PricingRejectionAbort as abort:
            PosCartPricingService._record_pricing_audit(**abort.log_kwargs)
            raise abort.exc
        except _RxRejectionAbort as rx_abort:
            locked_or_curr = PosSaleOrder.objects.filter(pk=rx_abort.sale_id).first()
            if locked_or_curr:
                if rx_abort.mark_offline_blocked:
                    from apps.core.models import PosRxGateStatus

                    locked_or_curr.rx_gate_status = PosRxGateStatus.OFFLINE_BLOCKED
                    locked_or_curr.rx_offline_blocked_reason = "Unverified Rx/antibiotic/narcotic cannot finalize offline."
                    locked_or_curr.save(update_fields=["rx_gate_status", "rx_offline_blocked_reason", "updated_at"])
                PosRxSafetyService._record_rx_audit(sale=locked_or_curr, **rx_abort.audit_kwargs)
            raise rx_abort.exc
        except _FefoRejectionAbort as fefo_abort:
            PosFefoCheckoutService.record_audit_safely(fefo_abort.audit_kwargs)
            raise fefo_abort.exc

    @classmethod
    @transaction.atomic
    def _finalize_sale_atomic(
        cls,
        user: Any,
        sale: PosSaleOrder,
        *,
        expected_grand_total_bdt: Any,
        expected_pricing_hash: Optional[str],
        is_offline_sync: bool,
        batch_overrides: Any,
        override_reason: str,
        pricing_svc: Any,
        rejection_cls: Any,
        decimal_parser: Any,
    ) -> PosSaleOrder:
        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        cls.resolve_user_tenant_and_branch(user, branch_id=sale.branch_id, require_write=True)

        if sale.lifecycle_state == PosSaleLifecycleState.FINALIZED:
            if is_offline_sync:
                return sale
            raise ValidationError(
                {
                    "error_code": "ERR_SALE_ALREADY_FINALIZED",
                    "detail": f"Sale '{sale.order_number}' is already finalized.",
                }
            )
        if sale.lifecycle_state in (PosSaleLifecycleState.VOIDED, PosSaleLifecycleState.REVERSED):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "detail": f"Cannot finalize a {sale.lifecycle_state} sale order.",
                }
            )

        # Recompute canonical totals under row lock before Rx/payment checks
        sale, breakdown = pricing_svc.recompute_sale_totals(sale, user, allow_empty=False)
        server_total = q2(sale.grand_total_bdt)
        server_hash = breakdown["calculated_pricing_hash"]

        if expected_grand_total_bdt is not None and str(expected_grand_total_bdt).strip() != "":
            exp_total = decimal_parser(expected_grand_total_bdt, "expected_grand_total_bdt", min_val=Decimal("0.00"))
            if exp_total != server_total:
                raise rejection_cls(
                    log_kwargs={
                        "sale": sale,
                        "action": "PRICING_REJECTED",
                        "user": user,
                        "breakdown": breakdown,
                        "expected_grand_total_bdt": exp_total,
                        "was_rejected": True,
                        "rejection_code": "ERR_PRICE_CONFIRMATION_MISMATCH",
                        "rejection_message": f"Finalize expected {exp_total}, server {server_total}.",
                    },
                    exc=ValidationError(
                        {
                            "error_code": "ERR_PRICE_CONFIRMATION_MISMATCH",
                            "expected_grand_total_bdt": f"{exp_total:.2f}",
                            "server_grand_total_bdt": f"{server_total:.2f}",
                            "server_pricing_hash": server_hash,
                            "detail": (
                                f"Price confirmation mismatch during checkout: expected {exp_total:.2f} BDT, "
                                f"server total is {server_total:.2f} BDT."
                            ),
                        }
                    ),
                )

        if expected_pricing_hash and str(expected_pricing_hash).strip() != server_hash:
            raise rejection_cls(
                log_kwargs={
                    "sale": sale,
                    "action": "PRICING_REJECTED",
                    "user": user,
                    "breakdown": breakdown,
                    "was_rejected": True,
                    "rejection_code": "ERR_PRICE_CONFIRMATION_MISMATCH",
                    "rejection_message": f"Finalize hash mismatch ({str(expected_pricing_hash)[:16]} != {server_hash[:16]}).",
                },
                exc=ValidationError(
                    {
                        "error_code": "ERR_PRICE_CONFIRMATION_MISMATCH",
                        "expected_pricing_hash": str(expected_pricing_hash),
                        "server_pricing_hash": server_hash,
                        "detail": "Pricing confirmation hash changed before checkout finalization.",
                    }
                ),
            )

        if not sale.pricing_confirmed_at or sale.pricing_confirmation_hash != server_hash:
            sale.pricing_confirmed_at = timezone.now()
            sale.pricing_confirmed_by = user
            sale.pricing_confirmation_hash = server_hash
            sale.save(update_fields=["pricing_confirmed_at", "pricing_confirmed_by", "pricing_confirmation_hash", "updated_at"])

        from apps.core.pos_rx_safety_service import PosRxSafetyService

        PosRxSafetyService.assert_rx_gate_for_finalization(
            user,
            sale,
            is_offline_sync=bool(is_offline_sync),
        )
        sale.refresh_from_db()

        from apps.core.pos_fefo_checkout_service import PosFefoCheckoutService

        # Atomically reconcile, validate safety (expired/recalled/quarantined) & reserve missing/edited FEFO lots
        sale = PosFefoCheckoutService.ensure_and_validate_checkout_allocations_in_tx(
            user,
            sale,
            batch_overrides=batch_overrides,
            override_reason=override_reason,
            is_offline_sync=bool(is_offline_sync),
        )
        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)

        from apps.core.pos_payment_service import PosPaymentSplitService

        captured_attempts = PosPaymentSplitService.assert_payments_ready_for_finalize(sale)
        dominant_tender = captured_attempts[0].tender_type if captured_attempts else PaymentMethod.CASH

        invoice_id = f"inv_{uuid.uuid4().hex[:16]}"
        invoice = SaleInvoice.objects.create(
            invoice_id=invoice_id,
            tenant=sale.tenant,
            branch=sale.branch,
            invoice_number=sale.order_number,
            customer_name=sale.customer_name or "Walk-in Customer",
            customer_phone=sale.customer_phone or "",
            total_amount=sale.subtotal_bdt,
            tax_amount=sale.tax_amount_bdt,
            discount_amount=sale.discount_amount_bdt,
            net_amount=sale.grand_total_bdt,
            payment_method=dominant_tender,
            status=InvoiceStatus.FINALIZED,
        )

        for line in sale.lines.select_related("product").order_by("line_number"):
            line_allocs = list(
                line.allocations.filter(status=PosAllocationStatus.RESERVED).select_related("batch", "cost_lot")
            )
            if not line_allocs:
                raise ValidationError({"allocations": f"Line {line.line_number} has no reserved batch allocation."})

            primary_batch = line_allocs[0].batch
            inv_item = SaleInvoiceItem.objects.create(
                invoice=invoice,
                batch=primary_batch,
                product=line.product,
                quantity=line.base_units_total,
                unit_price=q2(line.line_total_bdt / Decimal(line.base_units_total)),
                subtotal=line.line_total_bdt,
            )
            line.linked_invoice_item = inv_item
            line.save(update_fields=["linked_invoice_item", "updated_at"])

            for alloc in line_allocs:
                batch = BatchStock.objects.select_for_update().get(pk=alloc.batch_id)
                seg_ids = PosFefoCheckoutService._quarantined_segregation_batch_ids(
                    sale.tenant_id, sale.branch_id, [batch.batch_id]
                )
                is_safe, _blk_cat, err_code, blk_reason = PosFefoCheckoutService._classify_batch_safety(
                    batch,
                    today=timezone.now().date(),
                    product_recalled=PosFefoCheckoutService._is_product_globally_recalled(line.product),
                    quarantined_seg_ids=seg_ids,
                )
                if not is_safe:
                    raise ValidationError({"error_code": err_code, "detail": blk_reason})
                batch.reserved_quantity_base_units = max(0, batch.reserved_quantity_base_units - alloc.allocated_base_units)
                batch.save(update_fields=["reserved_quantity_base_units", "updated_at"])

                StockLedgerService.record_stock_movement(
                    tenant=sale.tenant,
                    branch=sale.branch,
                    product=line.product,
                    movement_type=StockMovementType.SALE_DISPENSE,
                    quantity_delta=-alloc.allocated_base_units,
                    source_entity_type="POS_SALE_ORDER",
                    source_entity_id=sale.sale_id,
                    source_reference=sale.order_number,
                    batch=batch,
                    cost_lot=alloc.cost_lot,
                    unit_cost=alloc.unit_cost_bdt,
                    unit_price=q4(line.unit_price_bdt),
                    posted_by=user,
                )

                if alloc.cost_lot_id:
                    lot = PurchaseCostLot.objects.select_for_update().get(pk=alloc.cost_lot_id)
                    lot.remaining_quantity = max(0, lot.remaining_quantity - alloc.allocated_base_units)
                    if lot.remaining_quantity == 0:
                        lot.status = PurchaseCostLotStatus.DEPLETED
                    lot.save(update_fields=["remaining_quantity", "status", "updated_at"])

                eff_unit_sell = q4(line.line_total_bdt / Decimal(line.base_units_total))
                tot_sell = q4(eff_unit_sell * Decimal(alloc.allocated_base_units))
                tot_cost = q4(alloc.unit_cost_bdt * Decimal(alloc.allocated_base_units))
                gp = q4(tot_sell - tot_cost)
                margin = q2((gp / tot_sell) * Decimal("100.00")) if tot_sell > Decimal("0.0000") else Decimal("0.00")

                sla = SaleLotAllocation.objects.create(
                    allocation_id=f"sall-{uuid.uuid4().hex[:12]}",
                    tenant=sale.tenant,
                    branch=sale.branch,
                    invoice=invoice,
                    invoice_item=inv_item,
                    product=line.product,
                    batch=batch,
                    cost_lot=alloc.cost_lot,
                    quantity_base_units=alloc.allocated_base_units,
                    unit_selling_price=eff_unit_sell,
                    unit_cost=alloc.unit_cost_bdt,
                    total_selling_price=tot_sell,
                    total_cost=tot_cost,
                    gross_profit=gp,
                    profit_margin_pct=margin,
                    status=SaleLotAllocationStatus.COMMITTED,
                )
                alloc.sale_lot_allocation = sla
                alloc.status = PosAllocationStatus.COMMITTED
                alloc.save(update_fields=["sale_lot_allocation", "status"])
                PosFefoCheckoutService.record_audit_safely(
                    {
                        "sale": sale,
                        "sale_line": line,
                        "batch": batch,
                        "action": "FEFO_COMMITTED",
                        "allocation_mode": alloc.allocation_mode,
                        "requested_base_units": int(line.base_units_total),
                        "allocated_base_units": int(alloc.allocated_base_units),
                        "batch_number_snapshot": batch.batch_number,
                        "expiry_date_snapshot": alloc.expiry_date_snapshot or batch.expiry_date,
                        "was_rejected": False,
                        "reason": f"Committed FEFO lot {batch.batch_number} for invoice {invoice.invoice_number}",
                        "actor_user": user,
                        "is_offline_sync": bool(is_offline_sync),
                    }
                )

        PosPaymentSplitService.post_finalized_payment_ledger_and_journal_in_tx(
            user=user,
            sale=sale,
            invoice=invoice,
            captured_attempts=captured_attempts,
            is_offline_sync=bool(is_offline_sync),
        )

        if sale.due_amount_bdt > Decimal("0.00"):
            from apps.core.pos_customer_due_service import PosCustomerDueService

            PosCustomerDueService.create_due_record_on_finalize_in_tx(
                user=user,
                sale=sale,
                invoice=invoice,
            )

        if sale.prescription:
            rx = PrescriptionRecord.objects.select_for_update().get(pk=sale.prescription_id)
            rx.status = PrescriptionStatus.DISPENSED
            rx.save(update_fields=["status"])

        old_state = sale.lifecycle_state
        sale.linked_invoice = invoice
        sale.lifecycle_state = PosSaleLifecycleState.FINALIZED
        sale.fefo_allocation_status = PosFefoAllocationStatus.COMMITTED
        sale.finalized_at = timezone.now()
        sale.version += 1
        sale.updated_by = user
        sale.save(
            update_fields=[
                "linked_invoice",
                "lifecycle_state",
                "fefo_allocation_status",
                "finalized_at",
                "version",
                "updated_by",
                "updated_at",
            ]
        )

        PosRxSafetyService.record_antibiotic_register_on_finalize(user, sale, invoice)

        cls._record_lifecycle_audit(
            sale=sale,
            from_state=old_state,
            to_state=PosSaleLifecycleState.FINALIZED,
            action="SALE_FINALIZED",
            user=user,
            reason=f"Finalized POS sale {sale.order_number} -> Invoice {invoice.invoice_number}",
            metadata={"invoice_id": invoice.invoice_id},
        )
        return sale

    @classmethod
    @transaction.atomic
    def void_pre_final_sale(cls, user: Any, sale: PosSaleOrder, reason: str) -> PosSaleOrder:
        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        cls.resolve_user_tenant_and_branch(user, branch_id=sale.branch_id, require_write=True)

        if sale.lifecycle_state in (
            PosSaleLifecycleState.FINALIZED,
            PosSaleLifecycleState.PARTIALLY_RETURNED,
            PosSaleLifecycleState.FULLY_RETURNED,
            PosSaleLifecycleState.REVERSED,
        ):
            raise MethodNotAllowed(
                "DELETE",
                detail="Posted/finalized sales cannot be deleted or voided directly; submit an audited reversal.",
            )

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError({"reason": "A documented reason is strictly required to void a sale order."})

        if sale.lifecycle_state == PosSaleLifecycleState.VOIDED:
            raise ValidationError({"detail": "Sale order is already voided."})

        cls._release_reserved_allocations(sale)
        sale.payment_attempts.filter(
            status__in=[PosPaymentAttemptStatus.INITIATED, PosPaymentAttemptStatus.PENDING, PosPaymentAttemptStatus.AUTHORIZED]
        ).update(status=PosPaymentAttemptStatus.VOIDED)

        old_state = sale.lifecycle_state
        sale.lifecycle_state = PosSaleLifecycleState.VOIDED
        sale.fefo_allocation_status = PosFefoAllocationStatus.RELEASED
        sale.voided_at = timezone.now()
        sale.void_reason = clean_reason
        sale.is_archived = True
        sale.version += 1
        sale.updated_by = user
        sale.save()

        cls._record_lifecycle_audit(
            sale=sale,
            from_state=old_state,
            to_state=PosSaleLifecycleState.VOIDED,
            action="SALE_VOIDED",
            user=user,
            reason=sale.void_reason,
        )
        return sale

    @classmethod
    @transaction.atomic
    def correct_sale_order(
        cls,
        user: Any,
        sale: PosSaleOrder,
        reason: str,
        correction_type: str = "CART_CORRECTION",
        line_id: Optional[str] = None,
        quantity_units: Optional[int] = None,
    ) -> PosSaleOrder:
        """
        Architecture Decision: ADR-0235 (P235).
        Audited correction command with reason and lifecycle audit trail.
        For pre-final orders (DRAFT, HELD, CONFIRMED): adjusts items/quantities safely and recomputes totals.
        For finalized orders: performs an audited reversal and provisions a linked correction replacement cart.
        """
        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError({"reason": "A documented reason is strictly required to correct a sale order."})

        from apps.core.pos_cart_pricing_service import PosCartPricingService

        # Post-final correction path: reverse original and create linked replacement draft cart
        if sale.lifecycle_state in (PosSaleLifecycleState.FINALIZED, PosSaleLifecycleState.PARTIALLY_RETURNED):
            cls.reverse_finalized_sale(user, sale, reason=f"Correction: {clean_reason}")
            corr_sale_id = f"sale-{uuid.uuid4().hex[:12]}"
            corr_order_number = f"{sale.order_number}-CORR"
            corr_sale = PosSaleOrder.objects.create(
                sale_id=corr_sale_id,
                order_number=corr_order_number,
                tenant=sale.tenant,
                branch=sale.branch,
                cashier_user=user,
                customer=sale.customer,
                customer_name=sale.customer_name,
                customer_phone=sale.customer_phone,
                prescription=sale.prescription,
                source_channel=sale.source_channel,
                lifecycle_state=PosSaleLifecycleState.DRAFT,
                discount_type=sale.discount_type,
                discount_rate_pct=sale.discount_rate_pct,
                header_discount_amount_bdt=sale.header_discount_amount_bdt,
                tax_mode=sale.tax_mode,
                tax_rate_pct=sale.tax_rate_pct,
                currency=sale.currency,
                corrected_from_sale=sale,
                correction_reason=clean_reason,
                is_correction=True,
                created_by=user,
                updated_by=user,
            )
            from apps.core.models import PosSaleOrderLine
            for s_line in sale.lines.all():
                if line_id and s_line.line_id == line_id and quantity_units == 0:
                    continue  # line removed in correction
                q_units = quantity_units if (line_id and s_line.line_id == line_id and quantity_units is not None) else s_line.quantity_units
                conversion = s_line.conversion_factor or 1
                base_units = q_units * conversion
                subtotal = (s_line.unit_price_bdt * Decimal(q_units)).quantize(Decimal("0.01"))
                PosSaleOrderLine.objects.create(
                    line_id=f"line-{uuid.uuid4().hex[:12]}",
                    tenant=sale.tenant,
                    branch=sale.branch,
                    sale=corr_sale,
                    line_number=s_line.line_number,
                    product=s_line.product,
                    packaging_unit=s_line.packaging_unit,
                    conversion_factor=conversion,
                    quantity_units=q_units,
                    base_units_total=base_units,
                    unit_price_bdt=s_line.unit_price_bdt,
                    line_subtotal_bdt=subtotal,
                    line_total_bdt=subtotal,
                    requires_rx_verification=s_line.requires_rx_verification,
                )
            corr_sale, _ = PosCartPricingService.recompute_sale_totals(corr_sale, user, allow_empty=True)
            cls._record_lifecycle_audit(
                sale=corr_sale,
                from_state=sale.lifecycle_state,
                to_state=PosSaleLifecycleState.DRAFT,
                action="POSTED_SALE_CORRECTED",
                user=user,
                reason=clean_reason,
                metadata={"original_sale_id": sale.sale_id, "original_order_number": sale.order_number},
            )
            return corr_sale

        # Pre-final cart correction path
        if sale.lifecycle_state == PosSaleLifecycleState.VOIDED:
            raise ValidationError({"detail": "Cannot correct an already voided sale order."})

        cls.resolve_user_tenant_and_branch(user, branch_id=sale.branch_id, require_write=True)
        old_state = sale.lifecycle_state
        if line_id:
            s_line = sale.lines.filter(line_id=line_id).first()
            if not s_line:
                raise ValidationError({"line_id": f"Sale line {line_id} not found."})
            if quantity_units == 0:
                s_line.delete()
            elif quantity_units and quantity_units > 0:
                s_line.quantity_units = quantity_units
                s_line.base_units_total = quantity_units * (s_line.conversion_factor or 1)
                subtotal = (s_line.unit_price_bdt * Decimal(quantity_units)).quantize(Decimal("0.01"))
                s_line.line_subtotal_bdt = subtotal
                s_line.line_total_bdt = subtotal
                s_line.save()

        sale, _ = PosCartPricingService.recompute_sale_totals(sale, user, allow_empty=True)
        sale.correction_reason = clean_reason
        sale.version += 1
        sale.updated_by = user
        sale.save()

        cls._record_lifecycle_audit(
            sale=sale,
            from_state=old_state,
            to_state=sale.lifecycle_state,
            action="CART_CORRECTION",
            user=user,
            reason=clean_reason,
            metadata={"correction_type": correction_type, "line_id": line_id, "quantity_units": quantity_units},
        )
        return sale

    @classmethod
    @transaction.atomic
    def reverse_finalized_sale(cls, user: Any, sale: PosSaleOrder, reason: str) -> PosSaleOrder:
        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        cls.resolve_user_tenant_and_branch(
            user, branch_id=sale.branch_id, require_write=True, allowed_roles=cls.POS_ELEVATED_ROLES
        )

        if not str(reason or "").strip():
            raise ValidationError({"reason": "A documented reason is strictly required to reverse a finalized sale."})

        if sale.lifecycle_state not in (PosSaleLifecycleState.FINALIZED, PosSaleLifecycleState.PARTIALLY_RETURNED):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "detail": f"Only FINALIZED or PARTIALLY_RETURNED sales can be reversed (got '{sale.lifecycle_state}').",
                }
            )

        committed_allocs = list(
            sale.allocations.filter(status__in=[PosAllocationStatus.COMMITTED, PosAllocationStatus.PARTIALLY_RETURNED])
            .select_related("batch", "sale_line__product", "cost_lot", "sale_lot_allocation")
        )
        for alloc in committed_allocs:
            restore_qty = alloc.allocated_base_units - alloc.returned_base_units
            if restore_qty > 0:
                batch = BatchStock.objects.select_for_update().get(pk=alloc.batch_id)
                StockLedgerService.record_stock_movement(
                    tenant=sale.tenant,
                    branch=sale.branch,
                    product=alloc.product,
                    movement_type=StockMovementType.CUSTOMER_RETURN,
                    quantity_delta=restore_qty,
                    source_entity_type="POS_SALE_REVERSAL",
                    source_entity_id=sale.sale_id,
                    source_reference=sale.order_number,
                    batch=batch,
                    cost_lot=alloc.cost_lot,
                    unit_cost=alloc.unit_cost_bdt,
                    unit_price=q4(alloc.sale_line.unit_price_bdt),
                    notes=f"Reversal: {reason}",
                    posted_by=user,
                )
                if alloc.cost_lot_id:
                    lot = PurchaseCostLot.objects.select_for_update().get(pk=alloc.cost_lot_id)
                    lot.remaining_quantity += restore_qty
                    lot.status = PurchaseCostLotStatus.ACTIVE
                    lot.save(update_fields=["remaining_quantity", "status", "updated_at"])

            alloc.returned_base_units = alloc.allocated_base_units
            alloc.status = PosAllocationStatus.RELEASED
            alloc.released_at = timezone.now()
            alloc.save(update_fields=["returned_base_units", "status", "released_at"])

            if alloc.sale_lot_allocation:
                alloc.sale_lot_allocation.status = SaleLotAllocationStatus.REVERSED
                alloc.sale_lot_allocation.is_returned = True
                alloc.sale_lot_allocation.returned_quantity_base_units = alloc.sale_lot_allocation.quantity_base_units
                alloc.sale_lot_allocation.save(
                    update_fields=["status", "is_returned", "returned_quantity_base_units", "updated_at"]
                )

        if sale.linked_invoice:
            sale.linked_invoice.status = InvoiceStatus.REVERSED
            sale.linked_invoice.save(update_fields=["status"])

        from apps.core.pos_fefo_checkout_service import PosFefoCheckoutService
        from apps.core.pos_rx_safety_service import PosRxSafetyService

        PosRxSafetyService.reverse_antibiotic_register_on_reversal(user, sale, reason)
        for rev_alloc in committed_allocs:
            PosFefoCheckoutService.record_audit_safely(
                {
                    "sale": sale,
                    "sale_line": rev_alloc.sale_line,
                    "batch": rev_alloc.batch,
                    "action": "ALLOCATION_REVERSED",
                    "allocation_mode": rev_alloc.allocation_mode,
                    "requested_base_units": int(rev_alloc.allocated_base_units),
                    "allocated_base_units": int(rev_alloc.allocated_base_units),
                    "batch_number_snapshot": rev_alloc.batch.batch_number,
                    "expiry_date_snapshot": rev_alloc.expiry_date_snapshot or rev_alloc.batch.expiry_date,
                    "was_rejected": False,
                    "reason": reason.strip(),
                    "actor_user": user,
                }
            )

        from apps.core.pos_customer_due_service import PosCustomerDueService
        from apps.core.pos_payment_service import PosPaymentSplitService

        PosPaymentSplitService.post_reversal_journal_and_refund_in_tx(user=user, sale=sale, reason=reason)
        PosCustomerDueService.sync_due_on_sale_reversal_in_tx(sale=sale, reason=reason)

        sale.payment_attempts.filter(status=PosPaymentAttemptStatus.CAPTURED).update(
            status=PosPaymentAttemptStatus.REFUNDED
        )

        old_state = sale.lifecycle_state
        sale.lifecycle_state = PosSaleLifecycleState.REVERSED
        sale.fefo_allocation_status = PosFefoAllocationStatus.RELEASED
        sale.refunded_amount_bdt = sale.paid_amount_bdt
        sale.voided_at = timezone.now()
        sale.void_reason = reason.strip()
        sale.version += 1
        sale.updated_by = user
        sale.save()

        cls._record_lifecycle_audit(
            sale=sale,
            from_state=old_state,
            to_state=PosSaleLifecycleState.REVERSED,
            action="SALE_REVERSED",
            user=user,
            reason=reason.strip(),
        )
        return sale
