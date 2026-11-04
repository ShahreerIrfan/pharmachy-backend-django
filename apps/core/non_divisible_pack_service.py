"""
backend/apps/core/non_divisible_pack_service.py
Architecture Decision: ADR-0165 (P165).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Service layer for Non-Divisible Pack Restrictions, Sterile Container Guardrails,
and Approved Clinical Splitting Protocols.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    InvoiceStatus,
    MedicineProduct,
    NonDivisibleContainerType,
    NonDivisiblePackPolicy,
    PaymentMethod,
    SaleInvoice,
    SaleInvoiceItem,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.non_divisible_pack_service")


class NonDivisiblePackService:
    """Enforces packaging integrity for sterile delivery devices and regulated non-divisible packs."""

    CONTAINER_FORM_MAPPING: Dict[str, str] = {
        "INJECTION": NonDivisibleContainerType.STERILE_VIAL_BOX,
        "INSULIN": NonDivisibleContainerType.INSULIN_PEN,
        "INHALER": NonDivisibleContainerType.INHALER_CANISTER,
        "DROPS": NonDivisibleContainerType.EYE_DROPS_BOTTLE,
        "TOPICAL": NonDivisibleContainerType.TOPICAL_TUBE,
        "DEVICE": NonDivisibleContainerType.DIAGNOSTIC_KIT,
    }

    @classmethod
    def get_effective_policy(
        cls,
        tenant_id: str,
        product_id: str,
        branch_id: Optional[str] = None,
    ) -> Optional[NonDivisiblePackPolicy]:
        """
        Retrieves the effective NonDivisiblePackPolicy.
        Prefers branch-specific override, then tenant-wide policy.
        If none exists in DB, automatically provisions a safe non-divisible policy
        for delivery forms (Insulin, Inhaler, Drops, Topical, Device, Injection).
        """
        # 1. Branch-specific override
        if branch_id:
            policy = NonDivisiblePackPolicy.objects.filter(
                tenant_id=tenant_id,
                branch_id=branch_id,
                product_id=product_id,
                is_active=True,
            ).first()
            if policy:
                return policy

        # 2. Tenant-level configuration
        policy = NonDivisiblePackPolicy.objects.filter(
            tenant_id=tenant_id,
            branch__isnull=True,
            product_id=product_id,
            is_active=True,
        ).first()
        if policy:
            return policy

        # 3. Dynamic fallback for regulated delivery forms
        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            return None

        form_upper = product.dosage_form.strip().upper() if product.dosage_form else ""
        if form_upper in cls.CONTAINER_FORM_MAPPING:
            tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
            if not tenant:
                return None

            container_type = cls.CONTAINER_FORM_MAPPING[form_upper]
            return NonDivisiblePackPolicy.objects.create(
                policy_id=f"ndpp-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                branch=None,
                product=product,
                container_type=container_type,
                is_splitting_allowed=False,
                require_pharmacist_approval=True,
                approved_break_factor=1,
                sub_unit_label="PIECE",
                prohibition_reason=(
                    f"Sealed {container_type} sterile delivery integrity must not be broken or opened."
                ),
                exemption_protocols=[],
                is_active=True,
            )

        return None

    @classmethod
    def create_or_update_policy(
        cls,
        user: PharmacyUser,
        data: Dict[str, Any],
        policy_id: Optional[str] = None,
    ) -> NonDivisiblePackPolicy:
        """Creates or updates a non-divisible pack policy."""
        # 1. RBAC check
        if getattr(user, "role", None) in [UserRole.CASHIER]:
            raise PermissionDenied({
                "error_code": "ERR_ROLE_UNAUTHORIZED",
                "detail": "Cashier role is not authorized to configure non-divisible pack policies.",
            })

        # 2. Tenant context check
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({
                "error_code": "ERR_TENANT_REQUIRED",
                "detail": "Active tenant context is required.",
            })

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant or not tenant.is_active or tenant.status in [
            TenantLifecycleStatus.SUSPENDED,
            TenantLifecycleStatus.ARCHIVED,
        ]:
            raise PermissionDenied({
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": f"Tenant '{tenant_id}' is inactive or suspended.",
            })

        # 3. Resolve Product
        product_id = data.get("product_id")
        if not product_id and not policy_id:
            raise ValidationError({
                "error_code": "ERR_PRODUCT_REQUIRED",
                "detail": "product_id is required.",
            })

        product = None
        if product_id:
            product = MedicineProduct.objects.filter(product_id=product_id).first()
            if not product:
                raise NotFound({
                    "error_code": "ERR_PRODUCT_NOT_FOUND",
                    "detail": f"Product '{product_id}' was not found.",
                })

        # 4. Resolve Branch if provided
        branch = None
        branch_id = data.get("branch_id")
        if branch_id:
            branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
            if not branch:
                raise ValidationError({
                    "error_code": "ERR_BRANCH_NOT_FOUND",
                    "detail": f"Branch '{branch_id}' not found for tenant '{tenant_id}'.",
                })

        container_type = data.get("container_type", NonDivisibleContainerType.SEALED_BOX)
        is_splitting_allowed = bool(data.get("is_splitting_allowed", False))
        require_pharmacist_approval = bool(data.get("require_pharmacist_approval", True))
        approved_break_factor = int(data.get("approved_break_factor", 1))
        if approved_break_factor < 1:
            raise ValidationError({
                "error_code": "ERR_INVALID_BREAK_FACTOR",
                "detail": "approved_break_factor must be at least 1.",
            })

        sub_unit_label = str(data.get("sub_unit_label", "PIECE")).strip().upper()
        prohibition_reason = str(data.get("prohibition_reason", "")).strip()
        if not is_splitting_allowed and not prohibition_reason:
            prohibition_reason = "Sealed sterile container integrity cannot be broken without approved prescription protocol."

        exemption_protocols = data.get("exemption_protocols", [])
        if not isinstance(exemption_protocols, list):
            exemption_protocols = [str(exemption_protocols)]

        with transaction.atomic():
            if policy_id:
                policy = NonDivisiblePackPolicy.objects.select_for_update().filter(
                    policy_id=policy_id,
                    tenant=tenant,
                ).first()
                if not policy:
                    raise NotFound({
                        "error_code": "ERR_POLICY_NOT_FOUND",
                        "detail": f"Non-divisible pack policy '{policy_id}' was not found.",
                    })
                before_state = {
                    "is_splitting_allowed": policy.is_splitting_allowed,
                    "container_type": policy.container_type,
                    "approved_break_factor": policy.approved_break_factor,
                }
            else:
                policy = NonDivisiblePackPolicy.objects.select_for_update().filter(
                    tenant=tenant,
                    branch=branch,
                    product=product,
                ).first()
                if not policy:
                    policy = NonDivisiblePackPolicy(
                        policy_id=f"ndpp-{uuid.uuid4().hex[:12]}",
                        tenant=tenant,
                        branch=branch,
                        product=product,
                    )
                before_state = {}

            policy.container_type = container_type
            policy.is_splitting_allowed = is_splitting_allowed
            policy.require_pharmacist_approval = require_pharmacist_approval
            policy.approved_break_factor = approved_break_factor
            policy.sub_unit_label = sub_unit_label
            policy.prohibition_reason = prohibition_reason
            policy.exemption_protocols = exemption_protocols
            policy.is_active = bool(data.get("is_active", True))
            policy.save()

            AuditLogEntry.objects.create(
                entry_id=uuid.uuid4(),
                tenant_id=tenant_id,
                branch_id=branch.branch_id if branch else None,
                user_id=getattr(user, "user_id", str(user)),
                actor_email=getattr(user, "email", "system@pos"),
                actor_role=getattr(user, "role", "ADMIN"),
                action="NON_DIVISIBLE_POLICY_SAVED",
                resource_type="NonDivisiblePackPolicy",
                resource_id=policy.policy_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "product_id": policy.product_id,
                    "container_type": policy.container_type,
                    "is_splitting_allowed": policy.is_splitting_allowed,
                    "approved_break_factor": policy.approved_break_factor,
                },
                before_state=before_state,
                after_state={
                    "is_splitting_allowed": policy.is_splitting_allowed,
                    "container_type": policy.container_type,
                    "approved_break_factor": policy.approved_break_factor,
                },
            )

        return policy

    @classmethod
    def validate_dispense_request(
        cls,
        tenant_id: str,
        product_id: str,
        branch_id: Optional[str] = None,
        requested_quantity: Decimal = Decimal("1.0000"),
        is_split_request: bool = False,
        exemption_protocol: Optional[str] = None,
        user: Optional[PharmacyUser] = None,
    ) -> Dict[str, Any]:
        """
        Validates dispense or sale request against non-divisible pack restrictions.
        Guards against:
        1. Fractional quantities of indivisible containers (e.g. 0.5 insulin pen).
        2. Splitting sealed packs without approved rules (ERR_NON_DIVISIBLE_CONTAINER_SPLIT_PROHIBITED).
        3. Unauthorized splitting without required Pharmacist sign-off (ERR_ROLE_UNAUTHORIZED).
        4. Missing required exemption protocols.
        """
        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise NotFound({
                "error_code": "ERR_PRODUCT_NOT_FOUND",
                "detail": f"Product '{product_id}' was not found.",
            })

        if requested_quantity <= Decimal("0"):
            raise ValidationError({
                "error_code": "ERR_INVALID_QUANTITY",
                "detail": "Requested quantity must be strictly positive.",
            })

        policy = cls.get_effective_policy(tenant_id, product_id, branch_id)

        # If no policy exists for product, standard whole-unit dispensing applies
        if not policy:
            return {
                "is_allowed": True,
                "product_id": product_id,
                "brand_name": product.brand_name,
                "container_type": "STANDARD_PACK",
                "is_splitting_allowed": True,
                "approved_break_factor": 1,
                "sub_unit_label": "PIECE",
                "requested_quantity": str(requested_quantity),
            }

        # Check 1: Fractional quantity guardrail on non-divisible delivery container
        if requested_quantity % Decimal("1") != Decimal("0"):
            raise ValidationError({
                "error_code": "ERR_NON_DIVISIBLE_CONTAINER_SPLIT_PROHIBITED",
                "detail": (
                    f"Fractional dispensing of non-divisible container '{product.brand_name}' "
                    f"({policy.container_type}) is strictly prohibited: {policy.prohibition_reason}"
                ),
                "product_id": product_id,
                "container_type": policy.container_type,
                "is_splitting_allowed": False,
            })

        # Check 2: Splitting request (attempting to break sealed pack into sub-units)
        if is_split_request:
            if not policy.is_splitting_allowed:
                raise ValidationError({
                    "error_code": "ERR_NON_DIVISIBLE_CONTAINER_SPLIT_PROHIBITED",
                    "detail": (
                        f"Breaking sealed container '{product.brand_name}' ({policy.container_type}) "
                        f"is strictly prohibited: {policy.prohibition_reason}"
                    ),
                    "product_id": product_id,
                    "container_type": policy.container_type,
                    "is_splitting_allowed": False,
                    "prohibition_reason": policy.prohibition_reason,
                })

            # Check clinical exemption protocol
            if policy.exemption_protocols:
                if not exemption_protocol or exemption_protocol not in policy.exemption_protocols:
                    raise ValidationError({
                        "error_code": "ERR_INVALID_EXEMPTION_PROTOCOL",
                        "detail": (
                            f"Approved clinical exemption protocol required to split container "
                            f"'{product.brand_name}'. Permitted protocols: {policy.exemption_protocols}"
                        ),
                        "permitted_protocols": policy.exemption_protocols,
                    })

            # Check Pharmacist/Manager approval role
            if policy.require_pharmacist_approval and user:
                user_role = getattr(user, "role", None)
                if user_role in [UserRole.CASHIER]:
                    raise PermissionDenied({
                        "error_code": "ERR_ROLE_UNAUTHORIZED",
                        "detail": (
                            "Cashier role is not authorized to approve splitting a non-divisible regulated container. "
                            "Licensed Pharmacist or Manager sign-off is required."
                        ),
                    })

        return {
            "is_allowed": True,
            "product_id": product_id,
            "brand_name": product.brand_name,
            "container_type": policy.container_type,
            "is_splitting_allowed": policy.is_splitting_allowed,
            "approved_break_factor": policy.approved_break_factor,
            "sub_unit_label": policy.sub_unit_label,
            "requested_quantity": str(requested_quantity),
            "is_split_request": is_split_request,
            "exemption_protocol": exemption_protocol,
        }

    @classmethod
    def execute_checkout(
        cls,
        tenant_id: str,
        branch_id: str,
        product_id: str,
        requested_quantity: Decimal,
        is_split_request: bool = False,
        exemption_protocol: Optional[str] = None,
        batch_id: Optional[str] = None,
        customer_name: str = "Walk-in Customer",
        customer_phone: str = "",
        payment_method: str = PaymentMethod.CASH,
        user: Optional[PharmacyUser] = None,
    ) -> Dict[str, Any]:
        """
        Executes atomic checkout for non-divisible packs.
        Enforces container integrity, FEFO batch selection, and integer base-piece deductions.
        """
        tenant = Tenant.objects.filter(tenant_id=tenant_id, is_active=True).first()
        if not tenant:
            raise PermissionDenied({
                "error_code": "ERR_TENANT_NOT_FOUND",
                "detail": f"Tenant '{tenant_id}' is inactive or does not exist.",
            })

        if not tenant.is_checkout_allowed():
            raise PermissionDenied({
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": f"Checkout rejected: Tenant '{tenant_id}' is in status '{tenant.status}'.",
            })

        branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant, is_active=True).first()
        if not branch:
            raise ValidationError({
                "error_code": "ERR_BRANCH_NOT_FOUND",
                "detail": f"Branch '{branch_id}' does not belong to tenant '{tenant_id}'.",
            })

        # 1. Pre-flight validation
        validation = cls.validate_dispense_request(
            tenant_id=tenant_id,
            product_id=product_id,
            branch_id=branch_id,
            requested_quantity=requested_quantity,
            is_split_request=is_split_request,
            exemption_protocol=exemption_protocol,
            user=user,
        )

        product = MedicineProduct.objects.get(product_id=product_id)
        unit_mrp = product.mrp_unit or Decimal("0.0000")
        total_amount = (unit_mrp * requested_quantity).quantize(Decimal("0.01"))
        total_base_units = int(requested_quantity)
        today = timezone.now().date()

        # 2. Atomic transaction with row locking
        with transaction.atomic():
            line_allocations = []

            if batch_id:
                batch = BatchStock.objects.select_for_update().filter(
                    batch_id=batch_id,
                    tenant=tenant,
                    branch=branch,
                    product=product,
                ).first()

                if not batch:
                    raise NotFound({
                        "error_code": "ERR_BATCH_NOT_FOUND",
                        "detail": f"Batch '{batch_id}' was not found in branch '{branch_id}'.",
                    })

                if batch.is_expired or batch.status == BatchStatus.EXPIRED or batch.expiry_date <= today:
                    raise ValidationError({
                        "error_code": "ERR_EXPIRED_BATCH",
                        "detail": f"DGDA violation: Batch '{batch.batch_number}' is expired.",
                    })

                if batch.is_quarantined or batch.status != BatchStatus.ACTIVE:
                    raise ValidationError({
                        "error_code": "ERR_BATCH_NOT_ACTIVE",
                        "detail": f"Batch '{batch.batch_number}' is quarantined or inactive.",
                    })

                if batch.quantity_base_units < total_base_units:
                    raise ValidationError({
                        "error_code": "ERR_INSUFFICIENT_STOCK",
                        "detail": (
                            f"Insufficient stock for batch '{batch.batch_number}'. "
                            f"Available: {batch.quantity_base_units}, Requested: {total_base_units}."
                        ),
                    })

                batch.quantity_base_units -= total_base_units
                batch.save(update_fields=["quantity_base_units"])
                line_allocations.append({
                    "batch": batch,
                    "quantity": total_base_units,
                    "unit_price": unit_mrp,
                    "subtotal": total_amount,
                })

            else:
                candidate_batches = list(
                    BatchStock.objects.select_for_update().filter(
                        tenant=tenant,
                        branch=branch,
                        product=product,
                        status=BatchStatus.ACTIVE,
                        is_quarantined=False,
                    ).order_by("expiry_date", "batch_number")
                )

                valid_batches = [b for b in candidate_batches if not b.is_expired and b.expiry_date > today]
                total_unexpired = sum(b.quantity_base_units for b in valid_batches)

                if total_unexpired < total_base_units:
                    raise ValidationError({
                        "error_code": "ERR_INSUFFICIENT_STOCK",
                        "detail": (
                            f"Insufficient stock in branch for '{product.brand_name}'. "
                            f"Available: {total_unexpired}, Requested: {total_base_units}."
                        ),
                    })

                remaining = total_base_units
                for b in valid_batches:
                    if remaining <= 0:
                        break
                    deduct = min(remaining, b.quantity_base_units)
                    b.quantity_base_units -= deduct
                    b.save(update_fields=["quantity_base_units"])

                    subtotal = (Decimal(str(deduct)) * unit_mrp).quantize(Decimal("0.01"))
                    line_allocations.append({
                        "batch": b,
                        "quantity": deduct,
                        "unit_price": unit_mrp,
                        "subtotal": subtotal,
                    })
                    remaining -= deduct

            # Create SaleInvoice
            invoice_id = f"inv_{uuid.uuid4().hex[:16]}"
            invoice_num = f"INV-{today.strftime('%Y%m%d')}-{invoice_id[-6:].upper()}"

            invoice = SaleInvoice.objects.create(
                invoice_id=invoice_id,
                tenant=tenant,
                branch=branch,
                invoice_number=invoice_num,
                customer_name=customer_name,
                customer_phone=customer_phone,
                total_amount=total_amount,
                tax_amount=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                net_amount=total_amount,
                payment_method=payment_method,
                status=InvoiceStatus.FINALIZED,
            )

            for alloc in line_allocations:
                SaleInvoiceItem.objects.create(
                    invoice=invoice,
                    batch=alloc["batch"],
                    product=product,
                    quantity=alloc["quantity"],
                    unit_price=alloc["unit_price"],
                    subtotal=alloc["subtotal"],
                )

            AuditLogEntry.objects.create(
                entry_id=uuid.uuid4(),
                tenant_id=tenant_id,
                branch_id=branch_id,
                user_id=getattr(user, "user_id", str(user)),
                actor_email=getattr(user, "email", "pos@pharmacy"),
                actor_role=getattr(user, "role", "CASHIER"),
                action="NON_DIVISIBLE_PACK_CHECKOUT",
                resource_type="SaleInvoice",
                resource_id=invoice.invoice_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "product_id": product_id,
                    "container_type": validation["container_type"],
                    "requested_quantity": str(requested_quantity),
                    "total_base_units": total_base_units,
                    "is_split_request": is_split_request,
                    "exemption_protocol": exemption_protocol,
                },
            )

        return {
            "invoice_id": invoice.invoice_id,
            "invoice_number": invoice.invoice_number,
            "total_amount": str(invoice.total_amount),
            "net_amount": str(invoice.net_amount),
            "status": invoice.status,
            "validation": validation,
            "allocations": [
                {
                    "batch_id": a["batch"].batch_id,
                    "batch_number": a["batch"].batch_number,
                    "expiry_date": a["batch"].expiry_date.isoformat(),
                    "quantity": a["quantity"],
                    "subtotal": str(a["subtotal"]),
                }
                for a in line_allocations
            ],
        }

    @classmethod
    def reverse_sale(
        cls,
        tenant_id: str,
        invoice_id: str,
        reason: str = "Customer Return / Dispensing Correction",
        user: Optional[PharmacyUser] = None,
    ) -> Dict[str, Any]:
        """Reverses a non-divisible pack sale, re-crediting integer units back to BatchStock."""
        with transaction.atomic():
            invoice = SaleInvoice.objects.select_for_update().filter(
                invoice_id=invoice_id,
                tenant_id=tenant_id,
            ).first()

            if not invoice:
                raise NotFound({
                    "error_code": "ERR_INVOICE_NOT_FOUND",
                    "detail": f"Sale invoice '{invoice_id}' was not found.",
                })

            if invoice.status == InvoiceStatus.REVERSED:
                raise ValidationError({
                    "error_code": "ERR_ALREADY_CANCELLED",
                    "detail": f"Invoice '{invoice.invoice_number}' is already reversed.",
                })

            items = list(invoice.items.select_related("batch").all())
            restored_units = 0

            for item in items:
                batch = BatchStock.objects.select_for_update().filter(batch_id=item.batch_id).first()
                if batch:
                    batch.quantity_base_units += item.quantity
                    batch.save(update_fields=["quantity_base_units"])
                    restored_units += item.quantity

            invoice.status = InvoiceStatus.REVERSED
            invoice.save(update_fields=["status"])

            AuditLogEntry.objects.create(
                entry_id=uuid.uuid4(),
                tenant_id=tenant_id,
                branch_id=invoice.branch_id if invoice.branch else None,
                user_id=getattr(user, "user_id", str(user)),
                actor_email=getattr(user, "email", "manager@pharmacy"),
                actor_role=getattr(user, "role", "BRANCH_MANAGER"),
                action="NON_DIVISIBLE_PACK_SALE_REVERSED",
                resource_type="SaleInvoice",
                resource_id=invoice.invoice_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "reason": reason,
                    "restored_base_units": restored_units,
                    "invoice_number": invoice.invoice_number,
                },
            )

        return {
            "invoice_id": invoice.invoice_id,
            "invoice_number": invoice.invoice_number,
            "status": invoice.status,
            "restored_base_units": restored_units,
            "message": "Non-divisible pack sale reversed and inventory restored successfully.",
        }
