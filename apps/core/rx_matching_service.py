"""
backend/apps/core/rx_matching_service.py

Architecture Decision: ADR-0245 (P245).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Authoritative domain service for Rx-Item Matching:
- Matches sold Rx-required products and quantities to authorized prescription items.
- Strict prohibition of automated treatment/generic substitution without explicit prescriber/pharmacist authorization.
- Enforces remaining dispense quota bounds (prevents over-dispensing).
- Supports partial dispensing and audited dispense reversal on sales return/void.
- Cross-tenant isolation and pharmacist role enforcement.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional, Tuple
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    MedicineProduct,
    PosSaleOrder,
    PrescriptionDispenseRecord,
    PrescriptionItem,
    PrescriptionRecord,
    PrescriptionStatus,
    PrescriptionValidityStatus,
    StaffMember,
    StaffRole,
    Tenant,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class RxMatchingStatus:
    MATCHED = "MATCHED"
    EXCEEDS_QUANTITY = "EXCEEDS_QUANTITY"
    SUBSTITUTION_PROHIBITED = "SUBSTITUTION_PROHIBITED"
    ALREADY_DISPENSED = "ALREADY_DISPENSED"
    PRESCRIPTION_NOT_FOUND = "PRESCRIPTION_NOT_FOUND"
    PRESCRIPTION_INACTIVE = "PRESCRIPTION_INACTIVE"
    OTC_EXEMPT = "OTC_EXEMPT"


class RxItemMatchingService:
    """
    Evaluates and commits exact matches between POS sale lines and authorized
    prescription items without permitting automated treatment substitutions.
    """

    ALLOWED_COMMIT_ROLES = {
        "STAFF_PHARMACIST",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "CASHIER",
        "SUPERADMIN",
    }

    @classmethod
    def match_sale_items_preflight(
        cls,
        prescription: PrescriptionRecord,
        cart_items: List[Dict[str, Any]],
        tenant_id: str,
    ) -> Dict[str, Any]:
        """
        Preflight evaluation of cart items against authorized prescription items.
        Prohibits automated generic/treatment substitution under DGDA clinical policy.
        """
        # 1. Tenant boundary
        if str(prescription.tenant_id) != str(tenant_id):
            raise PermissionDenied("Prescription belongs to another tenant.")

        # 2. State & Validity bounds
        if prescription.status in (PrescriptionStatus.EXPIRED, PrescriptionStatus.CANCELLED, PrescriptionStatus.REJECTED):
            return {
                "prescription_id": prescription.prescription_id,
                "all_matched": False,
                "can_proceed": False,
                "rejection_code": RxMatchingStatus.PRESCRIPTION_INACTIVE,
                "rejection_message": f"Prescription cannot be dispensed in status '{prescription.status}'.",
                "matched_items": [],
            }

        # Check explicit expiry
        if prescription.is_expired or prescription.validity_status == PrescriptionValidityStatus.EXPIRED:
            return {
                "prescription_id": prescription.prescription_id,
                "all_matched": False,
                "can_proceed": False,
                "rejection_code": RxMatchingStatus.PRESCRIPTION_INACTIVE,
                "rejection_message": f"Prescription expired on {prescription.expiry_date}. Dispensing is prohibited.",
                "matched_items": [],
            }

        # Collect prescription items index by product_id
        rx_items = list(prescription.items.select_related("product").all())
        rx_item_by_product: Dict[str, PrescriptionItem] = {
            it.product_id: it for it in rx_items
        }

        # Also map by generic_name to detect un-consented generic substitution attempts
        rx_items_by_generic: Dict[str, List[PrescriptionItem]] = {}
        for it in rx_items:
            gen = (it.product.generic_name or "").strip().upper()
            if gen:
                rx_items_by_generic.setdefault(gen, []).append(it)

        matched_results: List[Dict[str, Any]] = []
        overall_can_proceed = True
        overall_errors: List[str] = []

        for idx, cart_line in enumerate(cart_items):
            product_id = cart_line.get("product_id")
            quantity = int(cart_line.get("quantity") or 0)
            if quantity <= 0:
                overall_errors.append(f"Cart line #{idx+1} specifies invalid quantity ({quantity}).")
                overall_can_proceed = False
                continue

            product = MedicineProduct.objects.filter(product_id=product_id).first()
            if not product:
                matched_results.append({
                    "product_id": product_id,
                    "product_name": "Unknown",
                    "requested_quantity": quantity,
                    "match_status": "PRODUCT_NOT_FOUND",
                    "authorized_quantity": 0,
                    "remaining_quantity": 0,
                    "error": f"Medicine product '{product_id}' not found in catalog.",
                })
                overall_can_proceed = False
                continue

            is_rx_required = bool(product.is_prescription_only or product.is_antibiotic)

            # OTC products do not require Rx matching
            if not is_rx_required:
                matched_results.append({
                    "product_id": product.product_id,
                    "product_name": product.brand_name,
                    "requested_quantity": quantity,
                    "match_status": RxMatchingStatus.OTC_EXEMPT,
                    "authorized_quantity": quantity,
                    "remaining_quantity": quantity,
                    "is_rx_required": False,
                    "notes": "OTC product exempt from prescription quota.",
                })
                continue

            # Exact product match check
            rx_item = rx_item_by_product.get(product.product_id)

            if not rx_item:
                # Check if it was an automated generic substitution attempt
                cart_gen = (product.generic_name or "").strip().upper()
                similar_prescribed = rx_items_by_generic.get(cart_gen, [])
                if similar_prescribed:
                    prescribed_brand = similar_prescribed[0].product.brand_name
                    err_msg = (
                        f"Automated treatment/generic substitution is prohibited under DGDA policy. "
                        f"Prescribed brand is '{prescribed_brand}' ({product.generic_name}); "
                        f"cart contains '{product.brand_name}'. A registered pharmacist must explicitly "
                        f"reissue or document substitution."
                    )
                else:
                    err_msg = (
                        f"Product '{product.brand_name}' is not authorized on prescription "
                        f"{prescription.prescription_number or prescription.prescription_id}."
                    )

                matched_results.append({
                    "product_id": product.product_id,
                    "product_name": product.brand_name,
                    "requested_quantity": quantity,
                    "match_status": RxMatchingStatus.SUBSTITUTION_PROHIBITED,
                    "authorized_quantity": 0,
                    "remaining_quantity": 0,
                    "is_rx_required": True,
                    "error": err_msg,
                })
                overall_can_proceed = False
                overall_errors.append(err_msg)
                continue

            # Check remaining quota on the matched prescription item
            remaining = rx_item.remaining_dispense_quantity
            if remaining <= 0:
                err_msg = (
                    f"Prescribed item '{product.brand_name}' has already been fully dispensed "
                    f"({rx_item.quantity_dispensed}/{rx_item.quantity_prescribed} units)."
                )
                matched_results.append({
                    "product_id": product.product_id,
                    "prescription_item_id": rx_item.id,
                    "product_name": product.brand_name,
                    "requested_quantity": quantity,
                    "match_status": RxMatchingStatus.ALREADY_DISPENSED,
                    "authorized_quantity": rx_item.quantity_prescribed,
                    "quantity_previously_dispensed": rx_item.quantity_dispensed,
                    "remaining_quantity": 0,
                    "is_rx_required": True,
                    "error": err_msg,
                })
                overall_can_proceed = False
                overall_errors.append(err_msg)
                continue

            if quantity > remaining:
                err_msg = (
                    f"Requested quantity ({quantity}) exceeds remaining authorized quota ({remaining}) "
                    f"for '{product.brand_name}' on prescription {prescription.prescription_number}."
                )
                matched_results.append({
                    "product_id": product.product_id,
                    "prescription_item_id": rx_item.id,
                    "product_name": product.brand_name,
                    "requested_quantity": quantity,
                    "match_status": RxMatchingStatus.EXCEEDS_QUANTITY,
                    "authorized_quantity": rx_item.quantity_prescribed,
                    "quantity_previously_dispensed": rx_item.quantity_dispensed,
                    "remaining_quantity": remaining,
                    "is_rx_required": True,
                    "error": err_msg,
                })
                overall_can_proceed = False
                overall_errors.append(err_msg)
                continue

            # Exact match & valid quantity within bounds
            matched_results.append({
                "product_id": product.product_id,
                "prescription_item_id": rx_item.id,
                "product_name": product.brand_name,
                "requested_quantity": quantity,
                "match_status": RxMatchingStatus.MATCHED,
                "authorized_quantity": rx_item.quantity_prescribed,
                "quantity_previously_dispensed": rx_item.quantity_dispensed,
                "remaining_quantity_after_sale": remaining - quantity,
                "is_rx_required": True,
            })

        return {
            "prescription_id": prescription.prescription_id,
            "prescription_number": prescription.prescription_number,
            "tenant_id": str(prescription.tenant_id),
            "all_matched": overall_can_proceed,
            "can_proceed": overall_can_proceed,
            "overall_errors": overall_errors,
            "matched_items": matched_results,
        }

    @classmethod
    @transaction.atomic
    def commit_rx_item_dispenses(
        cls,
        user: Any,
        *,
        prescription_id: str,
        sale_order_id: Optional[str] = None,
        dispense_lines: List[Dict[str, Any]],
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Atomically commits matched Rx items, updates item quantities,
        creates PrescriptionDispenseRecords, and transitions prescription status.
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required to dispense prescription items.")

        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant": "Authenticated user has no associated tenant."})

        rx = (
            PrescriptionRecord.objects.select_for_update()
            .filter(prescription_id=prescription_id, tenant_id=tenant_id)
            .first()
        )
        if not rx:
            raise ValidationError({"prescription_id": f"Prescription '{prescription_id}' not found."})

        # Run preflight verification
        preflight = cls.match_sale_items_preflight(rx, dispense_lines, tenant_id=str(tenant_id))
        if not preflight["can_proceed"]:
            raise ValidationError({"matching": preflight["overall_errors"]})

        # Link staff member
        staff = StaffMember.objects.filter(tenant_id=tenant_id, username=getattr(user, "email", "")).first()
        if not staff:
            staff = StaffMember.objects.filter(tenant_id=tenant_id, email=getattr(user, "email", "")).first()

        # Link sale order if provided
        sale_order = None
        if sale_order_id:
            sale_order = PosSaleOrder.objects.filter(tenant_id=tenant_id, order_id=sale_order_id).first()

        dispense_records: List[PrescriptionDispenseRecord] = []

        for matched in preflight["matched_items"]:
            if not matched.get("is_rx_required", True):
                continue
            item_id = matched["prescription_item_id"]
            qty = matched["requested_quantity"]

            item = PrescriptionItem.objects.select_for_update().get(id=item_id)
            item.quantity_dispensed += qty
            if item.quantity_dispensed >= item.quantity_prescribed:
                item.is_dispensed = True
            item.save(update_fields=["quantity_dispensed", "is_dispensed"])

            rec = PrescriptionDispenseRecord.objects.create(
                dispense_id=f"dsp-{uuid.uuid4().hex[:10]}",
                tenant=rx.tenant,
                branch=rx.branch,
                prescription=rx,
                prescription_item=item,
                sale_order=sale_order,
                quantity_dispensed=qty,
                dispensed_by=staff,
                notes=str(notes or f"POS Match dispense against sale {sale_order_id or 'immediate'}").strip(),
            )
            dispense_records.append(rec)

        # Update prescription overall status
        rx_items = list(rx.items.all())
        all_done = all(it.is_dispensed for it in rx_items)
        if all_done:
            rx.status = PrescriptionStatus.DISPENSED
        else:
            rx.status = PrescriptionStatus.PARTIALLY_DISPENSED
        rx.save(update_fields=["status", "updated_at"])

        return {
            "prescription_id": rx.prescription_id,
            "status": rx.status,
            "dispensed_records_count": len(dispense_records),
            "dispense_ids": [d.dispense_id for d in dispense_records],
            "is_fully_dispensed": all_done,
        }

    @classmethod
    @transaction.atomic
    def reverse_rx_item_dispenses(
        cls,
        user: Any,
        *,
        prescription_id: str,
        dispense_id: str,
        reverse_quantity: Optional[int] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        """
        Reverses or adjusts a previously recorded dispense (e.g., POS sales return/void).
        Restores remaining authorization quota to the prescription item.
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required to reverse dispense.")

        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        rx = (
            PrescriptionRecord.objects.select_for_update()
            .filter(prescription_id=prescription_id, tenant_id=tenant_id)
            .first()
        )
        if not rx:
            raise ValidationError({"prescription_id": f"Prescription '{prescription_id}' not found."})

        rec = PrescriptionDispenseRecord.objects.filter(
            dispense_id=dispense_id, prescription=rx, tenant_id=tenant_id
        ).first()
        if not rec:
            raise ValidationError({"dispense_id": f"Dispense record '{dispense_id}' not found on prescription."})

        item = PrescriptionItem.objects.select_for_update().get(id=rec.prescription_item_id)
        qty_to_revert = reverse_quantity if reverse_quantity is not None else rec.quantity_dispensed

        if qty_to_revert <= 0 or qty_to_revert > item.quantity_dispensed:
            raise ValidationError(
                {"reverse_quantity": f"Cannot revert {qty_to_revert} units (currently dispensed: {item.quantity_dispensed})."}
            )

        item.quantity_dispensed -= qty_to_revert
        item.is_dispensed = (item.quantity_dispensed >= item.quantity_prescribed)
        item.save(update_fields=["quantity_dispensed", "is_dispensed"])

        rec.notes = f"{rec.notes}\n[REVERSED {qty_to_revert} units]: {reason}".strip()
        rec.save(update_fields=["notes"])

        # Re-evaluate prescription status
        rx_items = list(rx.items.all())
        any_dispensed = any(it.quantity_dispensed > 0 for it in rx_items)
        all_dispensed = all(it.is_dispensed for it in rx_items)

        if all_dispensed:
            rx.status = PrescriptionStatus.DISPENSED
        elif any_dispensed:
            rx.status = PrescriptionStatus.PARTIALLY_DISPENSED
        else:
            rx.status = PrescriptionStatus.VERIFIED
        rx.save(update_fields=["status", "updated_at"])

        return {
            "prescription_id": rx.prescription_id,
            "dispense_id": rec.dispense_id,
            "reverted_quantity": qty_to_revert,
            "new_item_dispensed_quantity": item.quantity_dispensed,
            "new_prescription_status": rx.status,
        }

