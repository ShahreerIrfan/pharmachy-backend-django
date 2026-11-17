"""
backend/apps/core/barcode_inventory_service.py
Architecture Decision: ADR-0218 (P218).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Authoritative domain service executing barcode-driven inventory workflows:
- Receive: Inward scanning, cold-chain checks, and staging.
- Put-Away: Batch barcode + location barcode with storage policy enforcement.
- Move: Relocation between bins/shelves with physical verification.
- Count: Physical cycle counts with discrepancy calculation (no silent adjustments).
- Verify: POS / station scanning with FEFO lot priority, expiry, and quarantine status.
- Bilingual operator prompts in English and Bengali.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BarcodeInventoryAuditLog,
    BarcodeWorkflowType,
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    StorageCondition,
    StorageZonePolicy,
    Tenant,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

COLD_CHAIN_CONDITIONS = {
    StorageCondition.COLD_CHAIN,
    StorageCondition.FREEZER,
    StorageCondition.COOL,
}

ALLOWED_BARCODE_ROLES = {
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    UserRole.STAFF_PHARMACIST,
    UserRole.CASHIER,
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "STAFF_PHARMACIST",
    "PROCUREMENT_OFFICER",
    "INVENTORY_CLERK",
    "CASHIER",
    "CHIEF_PHARMACIST",
}


class BarcodeInventoryService:
    """
    Unified service handling Receive, Put-Away, Move, Count, and Verify barcode workflows.
    """

    @classmethod
    def assert_barcode_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in ALLOWED_BARCODE_ROLES:
            raise PermissionDenied(
                {
                    "detail": "User lacks authorization for barcode inventory workflows.",
                    "error_code": "ERR_BARCODE_PERMISSION_DENIED",
                }
            )

    @classmethod
    def resolve_barcode(
        cls,
        tenant: Tenant,
        branch: Branch,
        barcode: str,
    ) -> Tuple[Optional[MedicineProduct], Optional[BatchStock]]:
        """
        Resolves a scanned barcode string against BatchStock numbers, primary Product barcodes,
        or registered BarcodeAlternateIdentifiers.
        """
        clean_code = str(barcode or "").strip()
        if not clean_code:
            return None, None

        # 1. Match Batch Number directly in this branch
        batch = (
            BatchStock.objects.filter(tenant=tenant, branch=branch, batch_number__iexact=clean_code)
            .select_related("product")
            .first()
        )
        if batch:
            return batch.product, batch

        # 2. Match Medicine Product Primary Barcode
        product = MedicineProduct.objects.filter(barcode__iexact=clean_code).first()
        if product:
            # Pick first available FEFO batch for this product in branch
            batch = (
                BatchStock.objects.filter(tenant=tenant, branch=branch, product=product, quantity_base_units__gt=0)
                .order_by("expiry_date")
                .first()
            )
            return product, batch

        # 3. Match Alternate Barcodes
        try:
            from apps.core.models import BarcodeAlternateIdentifier
            alt = (
                BarcodeAlternateIdentifier.objects.filter(
                    tenant_id_str=tenant.tenant_id,
                    barcode_value__iexact=clean_code,
                    is_active=True,
                )
                .select_related("product")
                .first()
            )
            if alt and alt.product:
                batch = (
                    BatchStock.objects.filter(tenant=tenant, branch=branch, product=alt.product, quantity_base_units__gt=0)
                    .order_by("expiry_date")
                    .first()
                )
                return alt.product, batch
        except Exception:
            pass

        return None, None

    @classmethod
    def log_scan_audit(
        cls,
        tenant: Tenant,
        branch: Branch,
        workflow_type: str,
        barcode: str,
        product: Optional[MedicineProduct],
        batch: Optional[BatchStock],
        source_loc: str = "",
        dest_loc: str = "",
        expected_qty: int = 0,
        scanned_qty: int = 0,
        prompt_en: str = "",
        prompt_bn: str = "",
        is_success: bool = True,
        error_code: str = "",
        user: Any = None,
    ) -> BarcodeInventoryAuditLog:
        return BarcodeInventoryAuditLog.objects.create(
            audit_id=f"bcaudit-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            workflow_type=workflow_type,
            product=product,
            batch=batch,
            scanned_barcode=barcode,
            source_location=source_loc,
            destination_location=dest_loc,
            expected_quantity=expected_qty,
            scanned_quantity=scanned_qty,
            discrepancy=scanned_qty - expected_qty,
            operator_prompt_en=prompt_en,
            operator_prompt_bn=prompt_bn,
            is_success=is_success,
            error_code=error_code,
            scanned_by=user if hasattr(user, "pk") and user.is_authenticated else None,
        )

    @classmethod
    def execute_receive(
        cls,
        tenant: Tenant,
        branch: Branch,
        barcode: str,
        user: Any = None,
    ) -> Dict[str, Any]:
        """
        Receive workflow: Scans inward product or batch and guides operator to appropriate storage staging.
        """
        product, batch = cls.resolve_barcode(tenant, branch, barcode)
        if not product:
            prompt_en = f"Unrecognized barcode '{barcode}'. Please register or scan valid medicine barcode."
            prompt_bn = f"অপরিচিত বারকোড '{barcode}'। অনুগ্রহ করে সঠিক মেডিসিন বারকোড স্ক্যান করুন।"
            cls.log_scan_audit(tenant, branch, BarcodeWorkflowType.RECEIVE, barcode, None, None, prompt_en=prompt_en, prompt_bn=prompt_bn, is_success=False, error_code="ERR_BARCODE_NOT_FOUND", user=user)
            raise NotFound({"detail": prompt_en, "prompt_bn": prompt_bn, "error_code": "ERR_BARCODE_NOT_FOUND"})

        is_cold_chain = (
            getattr(product, "requires_cold_chain", False)
            or getattr(product, "is_cold_chain", False)
            or (batch and batch.storage_condition in COLD_CHAIN_CONDITIONS)
        )

        if is_cold_chain:
            prompt_en = f"Cold-Chain item '{product.brand_name}' received. Immediately transfer to refrigerated storage (2°C - 8°C)."
            prompt_bn = f"কোল্ড-চেইন আইটেম '{product.brand_name}' গৃহীত হয়েছে। অবিলম্বে রেফ্রিজারেটরে (২°-৮° সে.) স্থানান্তর করুন।"
        else:
            prompt_en = f"Item '{product.brand_name}' received. Scan destination storage shelf or bin barcode."
            prompt_bn = f"আইটেম '{product.brand_name}' গৃহীত হয়েছে। স্টোরেজ সেলফ বা বিন বারকোড স্ক্যান করুন।"

        cls.log_scan_audit(
            tenant, branch, BarcodeWorkflowType.RECEIVE, barcode, product, batch,
            prompt_en=prompt_en, prompt_bn=prompt_bn, is_success=True, user=user,
        )

        return {
            "workflow_type": BarcodeWorkflowType.RECEIVE,
            "product_id": product.product_id,
            "product_name": product.brand_name,
            "batch_number": batch.batch_number if batch else "",
            "is_cold_chain": is_cold_chain,
            "operator_prompt_en": prompt_en,
            "operator_prompt_bn": prompt_bn,
        }

    @classmethod
    @transaction.atomic
    def execute_put_away(
        cls,
        tenant: Tenant,
        branch: Branch,
        batch_number_or_id: str,
        destination_location: str,
        user: Any = None,
    ) -> Dict[str, Any]:
        """
        Put-Away workflow: Scans batch barcode and destination shelf barcode with storage policy enforcement.
        """
        dest_loc = str(destination_location or "").strip().upper()
        if not dest_loc:
            raise ValidationError({"destination_location": "Destination storage location barcode is required."})

        # Locate batch
        batch = (
            BatchStock.objects.filter(tenant=tenant, branch=branch)
            .filter(batch_number__iexact=batch_number_or_id)
            .select_related("product")
            .first()
        )
        if not batch:
            batch = (
                BatchStock.objects.filter(tenant=tenant, branch=branch, batch_id=batch_number_or_id)
                .select_related("product")
                .first()
            )

        if not batch:
            raise NotFound({"detail": f"Batch '{batch_number_or_id}' not found in this branch.", "error_code": "ERR_BATCH_NOT_FOUND"})

        product = batch.product
        is_cold_chain = (
            getattr(product, "requires_cold_chain", False)
            or getattr(product, "is_cold_chain", False)
            or (batch.storage_condition in COLD_CHAIN_CONDITIONS)
        )

        # Enforce Cold Chain storage policy if enforced in branch
        policy = StorageZonePolicy.objects.filter(tenant=tenant, branch=branch, is_active=True).first()
        is_fridge_loc = ("FRIDGE" in dest_loc or "COLD" in dest_loc or "CHILL" in dest_loc or "FREEZER" in dest_loc)

        if is_cold_chain and policy and policy.enforce_cold_chain and not is_fridge_loc:
            prompt_en = f"Storage policy violation: Cold-chain medicine '{product.brand_name}' cannot be placed in ambient zone '{dest_loc}'. Scan refrigerator location."
            prompt_bn = f"স্টোরেজ নীতি লঙ্ঘন: কোল্ড-চেইন ওষুধ '{product.brand_name}' সাধারণ স্থান '{dest_loc}'-এ রাখা যাবে না। রেফ্রিজারেটর লোকেশন স্ক্যান করুন।"
            cls.log_scan_audit(
                tenant, branch, BarcodeWorkflowType.PUT_AWAY, batch.batch_number, product, batch,
                dest_loc=dest_loc, prompt_en=prompt_en, prompt_bn=prompt_bn, is_success=False,
                error_code="ERR_STORAGE_POLICY_VIOLATION", user=user,
            )
            raise ValidationError({"detail": prompt_en, "prompt_bn": prompt_bn, "error_code": "ERR_STORAGE_POLICY_VIOLATION"})

        # Update batch storage location
        from_loc = batch.storage_location
        batch.storage_location = dest_loc
        batch.save(update_fields=["storage_location"])

        prompt_en = f"Batch '{batch.batch_number}' placed in '{dest_loc}' successfully."
        prompt_bn = f"ব্যাচ '{batch.batch_number}' সফলভাবে '{dest_loc}' লোকেশনে সংরক্ষণ করা হয়েছে।"

        cls.log_scan_audit(
            tenant, branch, BarcodeWorkflowType.PUT_AWAY, batch.batch_number, product, batch,
            source_loc=from_loc, dest_loc=dest_loc, prompt_en=prompt_en, prompt_bn=prompt_bn,
            is_success=True, user=user,
        )

        return {
            "workflow_type": BarcodeWorkflowType.PUT_AWAY,
            "product_name": product.brand_name,
            "batch_number": batch.batch_number,
            "from_location": from_loc,
            "destination_location": dest_loc,
            "operator_prompt_en": prompt_en,
            "operator_prompt_bn": prompt_bn,
        }

    @classmethod
    @transaction.atomic
    def execute_move(
        cls,
        tenant: Tenant,
        branch: Branch,
        batch_number: str,
        from_location: str,
        to_location: str,
        user: Any = None,
    ) -> Dict[str, Any]:
        """
        Move workflow: Internal relocation between shelves/bins.
        """
        dest_loc = str(to_location or "").strip().upper()
        if not dest_loc:
            raise ValidationError({"to_location": "Target location barcode is required."})

        batch = (
            BatchStock.objects.filter(tenant=tenant, branch=branch, batch_number__iexact=batch_number)
            .select_related("product")
            .first()
        )
        if not batch:
            raise NotFound({"detail": f"Batch '{batch_number}' not found.", "error_code": "ERR_BATCH_NOT_FOUND"})

        batch.storage_location = dest_loc
        batch.save(update_fields=["storage_location"])

        prompt_en = f"Relocation complete: Batch '{batch.batch_number}' moved from '{from_location}' to '{dest_loc}'."
        prompt_bn = f"স্থানান্তর সম্পন্ন: ব্যাচ '{batch.batch_number}' '{from_location}' থেকে '{dest_loc}'-এ স্থানান্তরিত হয়েছে।"

        cls.log_scan_audit(
            tenant, branch, BarcodeWorkflowType.MOVE, batch.batch_number, batch.product, batch,
            source_loc=from_location, dest_loc=dest_loc, prompt_en=prompt_en, prompt_bn=prompt_bn,
            is_success=True, user=user,
        )

        return {
            "workflow_type": BarcodeWorkflowType.MOVE,
            "batch_number": batch.batch_number,
            "source_location": from_location,
            "destination_location": dest_loc,
            "operator_prompt_en": prompt_en,
            "operator_prompt_bn": prompt_bn,
        }

    @classmethod
    def execute_count(
        cls,
        tenant: Tenant,
        branch: Branch,
        barcode: str,
        location: str,
        counted_quantity: int,
        user: Any = None,
    ) -> Dict[str, Any]:
        """
        Count workflow: Physical inventory counting and discrepancy audit without silent adjustments.
        """
        product, batch = cls.resolve_barcode(tenant, branch, barcode)
        if not product or not batch:
            raise NotFound({"detail": f"No active inventory found for barcode '{barcode}'.", "error_code": "ERR_BATCH_NOT_FOUND"})

        expected_qty = batch.quantity_base_units
        discrepancy = counted_quantity - expected_qty

        if discrepancy == 0:
            prompt_en = f"Count matched: {counted_quantity} units verified for batch '{batch.batch_number}' (Zero discrepancy)."
            prompt_bn = f"গণনা মিলেছে: ব্যাচ '{batch.batch_number}'-এর জন্য {counted_quantity} ইউনিট নিশ্চিত (কোনো পার্থক্য নেই)।"
        else:
            prompt_en = f"Discrepancy detected: Counted {counted_quantity} vs System {expected_qty} ({discrepancy:+d} units). Logged for review."
            prompt_bn = f"পার্থক্য শনাক্ত: গণনা {counted_quantity} বনাম সিস্টেম {expected_qty} ({discrepancy:+d} ইউনিট)। পর্যালোচনার জন্য রেকর্ড করা হয়েছে।"

        cls.log_scan_audit(
            tenant, branch, BarcodeWorkflowType.COUNT, barcode, product, batch,
            source_loc=location, expected_qty=expected_qty, scanned_qty=counted_quantity,
            prompt_en=prompt_en, prompt_bn=prompt_bn, is_success=True, user=user,
        )

        return {
            "workflow_type": BarcodeWorkflowType.COUNT,
            "product_name": product.brand_name,
            "batch_number": batch.batch_number,
            "location": location,
            "expected_quantity": expected_qty,
            "counted_quantity": counted_quantity,
            "discrepancy": discrepancy,
            "operator_prompt_en": prompt_en,
            "operator_prompt_bn": prompt_bn,
        }

    @classmethod
    def execute_verify(
        cls,
        tenant: Tenant,
        branch: Branch,
        barcode: str,
        user: Any = None,
    ) -> Dict[str, Any]:
        """
        Verify workflow: Scans barcode to display product info, active batches, FEFO priority, and quarantine alerts.
        """
        product, batch = cls.resolve_barcode(tenant, branch, barcode)
        if not product:
            prompt_en = f"Unregistered item barcode '{barcode}'."
            prompt_bn = f"অনিবন্ধিত আইটেম বারকোড '{barcode}'।"
            cls.log_scan_audit(tenant, branch, BarcodeWorkflowType.VERIFY, barcode, None, None, prompt_en=prompt_en, prompt_bn=prompt_bn, is_success=False, error_code="ERR_NOT_FOUND", user=user)
            raise NotFound({"detail": prompt_en, "prompt_bn": prompt_bn, "error_code": "ERR_NOT_FOUND"})

        today = timezone.localdate()
        is_expired = batch.expiry_date <= today if batch else False
        is_quarantined = (batch.is_quarantined or batch.status == BatchStatus.QUARANTINED) if batch else False

        if is_expired:
            prompt_en = f"EXPIRED LOT ALERT: Batch '{batch.batch_number}' expired on {batch.expiry_date}. DO NOT DISPENSE."
            prompt_bn = f"মেয়াদোত্তীর্ণ ব্যাচ সতর্কতা: ব্যাচ '{batch.batch_number}'-এর মেয়াদ {batch.expiry_date} তারিখে শেষ। বিক্রি করবেন না।"
        elif is_quarantined:
            prompt_en = f"QUARANTINE ALERT: Batch '{batch.batch_number}' is held in quarantine ({batch.recall_reason or 'Quality Hold'})."
            prompt_bn = f"কোয়ারেন্টাইন সতর্কতা: ব্যাচ '{batch.batch_number}' কোয়ারেন্টাইনে রয়েছে।"
        else:
            prompt_en = f"Verified: {product.brand_name} (Batch: {batch.batch_number if batch else 'N/A'}, Qty: {batch.quantity_base_units if batch else 0})."
            prompt_bn = f"যাচাইকৃত: {product.brand_name} (ব্যাচ: {batch.batch_number if batch else 'N/A'}, পরিমাণ: {batch.quantity_base_units if batch else 0})।"

        cls.log_scan_audit(
            tenant, branch, BarcodeWorkflowType.VERIFY, barcode, product, batch,
            prompt_en=prompt_en, prompt_bn=prompt_bn, is_success=True, user=user,
        )

        return {
            "workflow_type": BarcodeWorkflowType.VERIFY,
            "product_id": product.product_id,
            "product_name": product.brand_name,
            "batch_number": batch.batch_number if batch else "",
            "quantity_base_units": batch.quantity_base_units if batch else 0,
            "expiry_date": batch.expiry_date.isoformat() if batch else "",
            "is_expired": is_expired,
            "is_quarantined": is_quarantined,
            "storage_location": batch.storage_location if batch else "",
            "operator_prompt_en": prompt_en,
            "operator_prompt_bn": prompt_bn,
        }
