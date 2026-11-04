"""
backend/apps/core/loose_tablet_sales_service.py
Architecture Decision: ADR-0164 (P164).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Service layer for Loose Tablet Sales, Partial Strip Dispensing,
Exact Base-Piece Stock Deduction, and Moisture/Clinical Prohibition Guardrails.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from django.db import models, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    InvoiceStatus,
    LooseTabletSalesConfig,
    MedicineProduct,
    PaymentMethod,
    SaleInvoice,
    SaleInvoiceItem,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.loose_tablet_sales_service")


class LooseTabletSalesService:
    """Coordinates partial strip sales and enforces blister-integrity & base-piece stock conservation."""

    @classmethod
    def get_effective_config(
        cls,
        tenant_id: str,
        product_id: str,
        branch_id: Optional[str] = None,
    ) -> LooseTabletSalesConfig:
        """
        Retrieves the effective LooseTabletSalesConfig.
        Prefers branch-specific override, then tenant-wide config.
        If no config exists in DB, creates a default config (allowing loose sales with default strip size 10).
        """
        # 1. Branch-specific override
        if branch_id:
            config = LooseTabletSalesConfig.objects.filter(
                tenant_id=tenant_id,
                branch_id=branch_id,
                product_id=product_id,
                is_active=True,
            ).first()
            if config:
                return config

        # 2. Tenant-level configuration
        config = LooseTabletSalesConfig.objects.filter(
            tenant_id=tenant_id,
            branch__isnull=True,
            product_id=product_id,
            is_active=True,
        ).first()
        if config:
            return config

        # 3. Dynamic default config fallback
        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise NotFound({
                "error_code": "ERR_PRODUCT_NOT_FOUND",
                "detail": f"Product '{product_id}' not found.",
            })

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({
                "error_code": "ERR_TENANT_NOT_FOUND",
                "detail": f"Tenant '{tenant_id}' not found.",
            })

        strip_mrp = (product.mrp_unit * Decimal("10.0000")).quantize(Decimal("0.0001"))
        return LooseTabletSalesConfig.objects.create(
            config_id=f"ltsc-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=None,
            product=product,
            allow_loose_sales=True,
            min_loose_dispense_units=1,
            max_loose_dispense_units=100,
            strip_size_pieces=10,
            strip_mrp=strip_mrp,
            loose_unit_mrp=product.mrp_unit,
            apply_loose_unit_surcharge=False,
            loose_surcharge_percentage=Decimal("0.00"),
            prohibition_reason="",
            is_active=True,
        )

    @classmethod
    def create_or_update_config(
        cls,
        user: PharmacyUser,
        data: Dict[str, Any],
        config_id: Optional[str] = None,
    ) -> LooseTabletSalesConfig:
        """Creates or updates a loose tablet sales configuration policy."""
        # 1. RBAC check: Cashier cannot modify loose sales configuration
        if getattr(user, "role", None) in [UserRole.CASHIER]:
            raise PermissionDenied({
                "error_code": "ERR_ROLE_UNAUTHORIZED",
                "detail": "Cashier role is not authorized to configure loose tablet sales policies.",
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
        if not product_id and not config_id:
            raise ValidationError({
                "error_code": "ERR_PRODUCT_REQUIRED",
                "detail": "Product ID is required for loose sales configuration.",
            })

        product = None
        if product_id:
            product = MedicineProduct.objects.filter(product_id=product_id).first()
            if not product:
                raise NotFound({
                    "error_code": "ERR_PRODUCT_NOT_FOUND",
                    "detail": f"Medicine product '{product_id}' was not found.",
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

        strip_size_pieces = int(data.get("strip_size_pieces", 10))
        if strip_size_pieces < 1:
            raise ValidationError({
                "error_code": "ERR_INVALID_STRIP_SIZE",
                "detail": "Strip size must be at least 1 base unit.",
            })

        allow_loose_sales = bool(data.get("allow_loose_sales", True))
        prohibition_reason = str(data.get("prohibition_reason", "")).strip()
        if not allow_loose_sales and not prohibition_reason:
            prohibition_reason = "Loose tablet sales prohibited for this product (clinical/moisture sensitivity guardrail)."

        strip_mrp = Decimal(str(data.get("strip_mrp", "0.0000")))
        loose_unit_mrp = Decimal(str(data.get("loose_unit_mrp", "0.0000")))
        if strip_mrp < Decimal("0.0000") or loose_unit_mrp < Decimal("0.0000"):
            raise ValidationError({
                "error_code": "ERR_NEGATIVE_PRICE",
                "detail": "Prices cannot be negative.",
            })

        min_units = int(data.get("min_loose_dispense_units", 1))
        max_units = int(data.get("max_loose_dispense_units", 100))
        if min_units > max_units:
            raise ValidationError({
                "error_code": "ERR_INVALID_LIMITS",
                "detail": "Minimum loose units cannot exceed maximum loose units.",
            })

        with transaction.atomic():
            if config_id:
                config = LooseTabletSalesConfig.objects.select_for_update().filter(
                    config_id=config_id,
                    tenant=tenant,
                ).first()
                if not config:
                    raise NotFound({
                        "error_code": "ERR_CONFIG_NOT_FOUND",
                        "detail": f"Loose sales config '{config_id}' was not found.",
                    })
                before_state = {
                    "allow_loose_sales": config.allow_loose_sales,
                    "strip_mrp": str(config.strip_mrp),
                    "loose_unit_mrp": str(config.loose_unit_mrp),
                }
            else:
                config = LooseTabletSalesConfig.objects.select_for_update().filter(
                    tenant=tenant,
                    branch=branch,
                    product=product,
                ).first()
                if not config:
                    config = LooseTabletSalesConfig(
                        config_id=f"ltsc-{uuid.uuid4().hex[:12]}",
                        tenant=tenant,
                        branch=branch,
                        product=product,
                    )
                before_state = {}

            config.allow_loose_sales = allow_loose_sales
            config.min_loose_dispense_units = min_units
            config.max_loose_dispense_units = max_units
            config.strip_size_pieces = strip_size_pieces
            config.strip_mrp = strip_mrp
            config.loose_unit_mrp = loose_unit_mrp
            config.apply_loose_unit_surcharge = bool(data.get("apply_loose_unit_surcharge", False))
            config.loose_surcharge_percentage = Decimal(str(data.get("loose_surcharge_percentage", "0.00")))
            config.prohibition_reason = prohibition_reason
            config.is_active = bool(data.get("is_active", True))
            config.save()

            AuditLogEntry.objects.create(
                entry_id=uuid.uuid4(),
                tenant_id=tenant_id,
                branch_id=branch.branch_id if branch else None,
                user_id=getattr(user, "user_id", str(user)),
                actor_email=getattr(user, "email", "system@pos"),
                actor_role=getattr(user, "role", "ADMIN"),
                action="LOOSE_SALES_CONFIG_SAVED",
                resource_type="LooseTabletSalesConfig",
                resource_id=config.config_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "product_id": config.product_id,
                    "allow_loose_sales": config.allow_loose_sales,
                    "strip_size_pieces": config.strip_size_pieces,
                },
                before_state=before_state,
                after_state={
                    "allow_loose_sales": config.allow_loose_sales,
                    "strip_mrp": str(config.strip_mrp),
                    "loose_unit_mrp": str(config.loose_unit_mrp),
                },
            )

        return config

    @classmethod
    def calculate_quote(
        cls,
        tenant_id: str,
        product_id: str,
        branch_id: Optional[str] = None,
        strips: int = 0,
        loose_pieces: int = 0,
        total_pieces: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Calculates quote for dispensing strips and/or loose tablets.
        Enforces loose sales prohibition guardrail (ERR_LOOSE_TABLET_SALES_PROHIBITED).
        """
        config = cls.get_effective_config(tenant_id, product_id, branch_id)

        # Quantity normalization
        if total_pieces is not None:
            if total_pieces <= 0:
                raise ValidationError({
                    "error_code": "ERR_INVALID_QUANTITY",
                    "detail": "Requested total pieces must be greater than zero.",
                })
            normalized_strips = total_pieces // config.strip_size_pieces
            normalized_loose_pieces = total_pieces % config.strip_size_pieces
            total_base_units = total_pieces
        else:
            if strips < 0 or loose_pieces < 0:
                raise ValidationError({
                    "error_code": "ERR_NEGATIVE_QUANTITY",
                    "detail": "Strips and loose pieces cannot be negative.",
                })
            normalized_strips = strips + (loose_pieces // config.strip_size_pieces)
            normalized_loose_pieces = loose_pieces % config.strip_size_pieces
            total_base_units = (normalized_strips * config.strip_size_pieces) + normalized_loose_pieces
            if total_base_units <= 0:
                raise ValidationError({
                    "error_code": "ERR_INVALID_QUANTITY",
                    "detail": "Requested quantity must be greater than zero.",
                })

        # Guardrail: Check loose tablet sales prohibition
        if not config.allow_loose_sales and normalized_loose_pieces > 0:
            raise ValidationError({
                "error_code": "ERR_LOOSE_TABLET_SALES_PROHIBITED",
                "detail": (
                    f"Loose tablet sales prohibited for {config.product.brand_name}: "
                    f"{config.prohibition_reason}"
                ),
                "product_id": product_id,
                "brand_name": config.product.brand_name,
                "allow_loose_sales": False,
                "prohibition_reason": config.prohibition_reason,
            })

        # Limits validation
        if normalized_loose_pieces > 0 and normalized_loose_pieces < config.min_loose_dispense_units:
            raise ValidationError({
                "error_code": "ERR_BELOW_MIN_LOOSE_DISPENSE",
                "detail": (
                    f"Requested loose quantity ({normalized_loose_pieces}) is below minimum allowed "
                    f"({config.min_loose_dispense_units} units)."
                ),
            })

        # Pricing computation
        strip_total = (Decimal(str(normalized_strips)) * config.strip_mrp).quantize(Decimal("0.0001"))
        base_loose_mrp = config.loose_unit_mrp

        surcharge_amount = Decimal("0.0000")
        effective_loose_mrp = base_loose_mrp
        if config.apply_loose_unit_surcharge and config.loose_surcharge_percentage > Decimal("0.00"):
            surcharge_factor = Decimal("1.00") + (config.loose_surcharge_percentage / Decimal("100.00"))
            effective_loose_mrp = (base_loose_mrp * surcharge_factor).quantize(Decimal("0.0001"))
            surcharge_amount = (
                (effective_loose_mrp - base_loose_mrp) * Decimal(str(normalized_loose_pieces))
            ).quantize(Decimal("0.0001"))

        loose_total = (Decimal(str(normalized_loose_pieces)) * effective_loose_mrp).quantize(Decimal("0.0001"))
        gross_total = (strip_total + loose_total).quantize(Decimal("0.01"))

        # Inventory check
        stock_query = BatchStock.objects.filter(
            tenant_id=tenant_id,
            product_id=product_id,
            status=BatchStatus.ACTIVE,
            is_quarantined=False,
            expiry_date__gt=timezone.now().date(),
        )
        if branch_id:
            stock_query = stock_query.filter(branch_id=branch_id)
        available_stock_base_units = stock_query.aggregate(
            total=models.Sum("quantity_base_units")
        )["total"] or 0

        is_stock_sufficient = available_stock_base_units >= total_base_units

        return {
            "product_id": product_id,
            "brand_name": config.product.brand_name,
            "allow_loose_sales": config.allow_loose_sales,
            "prohibition_reason": config.prohibition_reason,
            "strip_size_pieces": config.strip_size_pieces,
            "normalized_strips": normalized_strips,
            "normalized_loose_pieces": normalized_loose_pieces,
            "total_base_units": total_base_units,
            "strip_mrp": str(config.strip_mrp),
            "loose_unit_mrp": str(effective_loose_mrp),
            "strip_total": str(strip_total.quantize(Decimal("0.01"))),
            "loose_total": str(loose_total.quantize(Decimal("0.01"))),
            "surcharge_amount": str(surcharge_amount.quantize(Decimal("0.01"))),
            "gross_total": str(gross_total),
            "final_quote_amount": str(gross_total),
            "available_stock_base_units": available_stock_base_units,
            "is_stock_sufficient": is_stock_sufficient,
        }

    @classmethod
    def execute_checkout(
        cls,
        tenant_id: str,
        branch_id: str,
        product_id: str,
        strips: int = 0,
        loose_pieces: int = 0,
        total_pieces: Optional[int] = None,
        batch_id: Optional[str] = None,
        customer_name: str = "Walk-in Customer",
        customer_phone: str = "",
        payment_method: str = PaymentMethod.CASH,
        user: Optional[PharmacyUser] = None,
    ) -> Dict[str, Any]:
        """
        Executes retail dispensing of strips and loose tablets.
        Guarantees:
        - Atomic transaction with row-level locking (`select_for_update()`).
        - Integer base-piece conservation in BatchStock.
        - DGDA compliance: Rejects expired batches (ERR_EXPIRED_BATCH).
        - Blocks prohibited loose tablets (ERR_LOOSE_TABLET_SALES_PROHIBITED).
        - Prevents overselling (ERR_INSUFFICIENT_STOCK).
        """
        # 1. Validate tenant & branch
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

        # 2. Quote and guardrail check
        quote = cls.calculate_quote(
            tenant_id=tenant_id,
            product_id=product_id,
            branch_id=branch_id,
            strips=strips,
            loose_pieces=loose_pieces,
            total_pieces=total_pieces,
        )

        total_base_units = quote["total_base_units"]
        final_amount = Decimal(quote["final_quote_amount"])
        today = timezone.now().date()

        # 3. Synchronous atomic stock deduction with row-level locks
        with transaction.atomic():
            line_allocations = []

            if batch_id:
                # Specific batch specified
                batch = BatchStock.objects.select_for_update().filter(
                    batch_id=batch_id,
                    tenant=tenant,
                    branch=branch,
                    product_id=product_id,
                ).first()

                if not batch:
                    raise NotFound({
                        "error_code": "ERR_BATCH_NOT_FOUND",
                        "detail": f"Batch '{batch_id}' was not found in branch '{branch_id}'.",
                    })

                # Expired batch block
                if batch.is_expired or batch.status == BatchStatus.EXPIRED or batch.expiry_date <= today:
                    raise ValidationError({
                        "error_code": "ERR_EXPIRED_BATCH",
                        "detail": (
                            f"DGDA compliance violation: Batch '{batch.batch_number}' expired on {batch.expiry_date}. "
                            f"Dispensing of expired pharmaceuticals is strictly prohibited."
                        ),
                    })

                # Quarantine check
                if batch.is_quarantined or batch.status != BatchStatus.ACTIVE:
                    raise ValidationError({
                        "error_code": "ERR_BATCH_NOT_ACTIVE",
                        "detail": f"Batch '{batch.batch_number}' is quarantined or not active.",
                    })

                # Stock check
                if batch.quantity_base_units < total_base_units:
                    raise ValidationError({
                        "error_code": "ERR_INSUFFICIENT_STOCK",
                        "detail": (
                            f"Insufficient stock in batch '{batch.batch_number}'. "
                            f"Available: {batch.quantity_base_units}, Requested: {total_base_units}."
                        ),
                    })

                batch.quantity_base_units -= total_base_units
                batch.save(update_fields=["quantity_base_units"])
                line_allocations.append({
                    "batch": batch,
                    "quantity": total_base_units,
                    "unit_price": (final_amount / Decimal(str(total_base_units))).quantize(Decimal("0.0001")),
                    "subtotal": final_amount,
                })

            else:
                # Automatic FEFO allocation across unexpired batches
                candidate_batches = list(
                    BatchStock.objects.select_for_update().filter(
                        tenant=tenant,
                        branch=branch,
                        product_id=product_id,
                        status=BatchStatus.ACTIVE,
                        is_quarantined=False,
                    ).order_by("expiry_date", "batch_number")
                )

                # Filter valid unexpired
                valid_batches = [b for b in candidate_batches if not b.is_expired and b.expiry_date > today]
                total_unexpired_stock = sum(b.quantity_base_units for b in valid_batches)

                if total_unexpired_stock < total_base_units:
                    raise ValidationError({
                        "error_code": "ERR_INSUFFICIENT_STOCK",
                        "detail": (
                            f"Insufficient unexpired stock in branch. Available: {total_unexpired_stock}, "
                            f"Requested: {total_base_units}."
                        ),
                    })

                remaining_to_deduct = total_base_units
                effective_unit_price = (final_amount / Decimal(str(total_base_units))).quantize(Decimal("0.0001"))

                for batch in valid_batches:
                    if remaining_to_deduct <= 0:
                        break
                    deduct_qty = min(remaining_to_deduct, batch.quantity_base_units)
                    batch.quantity_base_units -= deduct_qty
                    batch.save(update_fields=["quantity_base_units"])

                    subtotal = (Decimal(str(deduct_qty)) * effective_unit_price).quantize(Decimal("0.01"))
                    line_allocations.append({
                        "batch": batch,
                        "quantity": deduct_qty,
                        "unit_price": effective_unit_price,
                        "subtotal": subtotal,
                    })
                    remaining_to_deduct -= deduct_qty

            # Create SaleInvoice
            invoice_id = f"inv_{uuid.uuid4().hex[:16]}"
            invoice_num = f"INV-{timezone.now().strftime('%Y%m%d')}-{invoice_id[-6:].upper()}"

            invoice = SaleInvoice.objects.create(
                invoice_id=invoice_id,
                tenant=tenant,
                branch=branch,
                invoice_number=invoice_num,
                customer_name=customer_name,
                customer_phone=customer_phone,
                total_amount=final_amount,
                tax_amount=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                net_amount=final_amount,
                payment_method=payment_method,
                status=InvoiceStatus.FINALIZED,
            )

            product = MedicineProduct.objects.get(product_id=product_id)
            for alloc in line_allocations:
                SaleInvoiceItem.objects.create(
                    invoice=invoice,
                    batch=alloc["batch"],
                    product=product,
                    quantity=alloc["quantity"],
                    unit_price=alloc["unit_price"],
                    subtotal=alloc["subtotal"],
                )

            # Audit log
            AuditLogEntry.objects.create(
                entry_id=uuid.uuid4(),
                tenant_id=tenant_id,
                branch_id=branch_id,
                user_id=getattr(user, "user_id", str(user)),
                actor_email=getattr(user, "email", "pos@pharmacy"),
                actor_role=getattr(user, "role", "CASHIER"),
                action="LOOSE_TABLET_CHECKOUT",
                resource_type="SaleInvoice",
                resource_id=invoice.invoice_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "product_id": product_id,
                    "normalized_strips": quote["normalized_strips"],
                    "normalized_loose_pieces": quote["normalized_loose_pieces"],
                    "total_base_units": total_base_units,
                    "final_amount": str(final_amount),
                    "batches_allocated": [a["batch"].batch_number for a in line_allocations],
                },
            )

        return {
            "invoice_id": invoice.invoice_id,
            "invoice_number": invoice.invoice_number,
            "total_amount": str(invoice.total_amount),
            "net_amount": str(invoice.net_amount),
            "status": invoice.status,
            "quote": quote,
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
        """
        Reverses or voids a loose tablet sale invoice.
        Re-credits exact integer base units back into corresponding BatchStock rows.
        """
        with transaction.atomic():
            invoice = SaleInvoice.objects.select_for_update().filter(
                invoice_id=invoice_id,
                tenant_id=tenant_id,
            ).first()

            if not invoice:
                raise NotFound({
                    "error_code": "ERR_INVOICE_NOT_FOUND",
                    "detail": f"Sale invoice '{invoice_id}' was not found for tenant '{tenant_id}'.",
                })

            if invoice.status == InvoiceStatus.REVERSED:
                raise ValidationError({
                    "error_code": "ERR_ALREADY_CANCELLED",
                    "detail": f"Invoice '{invoice.invoice_number}' is already cancelled.",
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
                actor_role=getattr(user, "role", "MANAGER"),
                action="LOOSE_TABLET_SALE_REVERSED",
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
            "message": "Loose tablet sale reversed and base units restocked successfully.",
        }
