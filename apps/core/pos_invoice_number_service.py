"""
Architecture Decision: ADR-0231 (P231).
POS Collision-Resistant Invoice Number Policy and Sequential Tracker Service.
Guarantees branch, register, and offline collision-free invoice identifiers.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import (
    Branch,
    PosInvoiceNumberPolicy,
    PosInvoiceSequenceResetFrequency,
    PosInvoiceSequenceTracker,
    PosSaleOrder,
    SaleInvoice,
    Tenant,
)


class PosInvoiceNumberService:
    """Service enforcing tenant & branch collision-resistant POS invoice identifiers."""

    @classmethod
    def sanitize_slug(cls, val: str, default: str = "MAIN", max_len: int = 8) -> str:
        if not val:
            return default
        cleaned = re.sub(r"[^A-Za-z0-9]", "", str(val)).upper()
        if not cleaned:
            return default
        return cleaned[:max_len]

    @classmethod
    def resolve_period_key(cls, reset_frequency: str, dt: Optional[datetime] = None) -> str:
        target_dt = dt or timezone.now()
        freq = str(reset_frequency or PosInvoiceSequenceResetFrequency.DAILY).upper()
        if freq == PosInvoiceSequenceResetFrequency.DAILY:
            return target_dt.strftime("%Y%m%d")
        elif freq == PosInvoiceSequenceResetFrequency.MONTHLY:
            return target_dt.strftime("%Y%m")
        elif freq == PosInvoiceSequenceResetFrequency.YEARLY:
            return target_dt.strftime("%Y")
        elif freq == PosInvoiceSequenceResetFrequency.NEVER:
            return "ALL"
        return target_dt.strftime("%Y%m%d")

    @classmethod
    def get_or_create_policy(
        cls, tenant: Tenant, branch: Optional[Branch] = None
    ) -> PosInvoiceNumberPolicy:
        if branch:
            branch_policy = PosInvoiceNumberPolicy.objects.filter(
                tenant=tenant, branch=branch, is_active=True
            ).first()
            if branch_policy:
                return branch_policy

        tenant_policy = PosInvoiceNumberPolicy.objects.filter(
            tenant=tenant, branch__isnull=True, is_active=True
        ).first()
        if tenant_policy:
            return tenant_policy

        # Fallback to creating a canonical default policy
        policy = PosInvoiceNumberPolicy.objects.create(
            policy_id=f"pol-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            prefix="INV",
            pattern="{prefix}-{branch}-{device}-{period}-{seq}",
            reset_frequency=PosInvoiceSequenceResetFrequency.DAILY,
            padding_digits=4,
            offline_prefix="OFF",
            allow_offline_preallocation=True,
            is_active=True,
        )
        return policy

    @classmethod
    def generate_next_invoice_number_in_tx(
        cls,
        tenant: Tenant,
        branch: Branch,
        device_namespace: str = "ONLINE",
        timestamp: Optional[datetime] = None,
        reserve: bool = True,
    ) -> Dict[str, Any]:
        """
        Allocates the next monotonic collision-resistant invoice identifier inside a transaction.
        Uses SELECT FOR UPDATE on PosInvoiceSequenceTracker.
        """
        policy = cls.get_or_create_policy(tenant, branch)
        target_dt = timestamp or timezone.now()
        period_key = cls.resolve_period_key(policy.reset_frequency, target_dt)

        # Extract branch code
        branch_code = cls.sanitize_slug(
            getattr(branch, "branch_code", None) or getattr(branch, "name", None) or "BR01",
            default="BR01",
            max_len=6,
        )
        dev_code = cls.sanitize_slug(device_namespace or "ONLINE", default="ONLINE", max_len=8)

        # Atomic lock on tracker row
        tracker = (
            PosInvoiceSequenceTracker.objects.select_for_update()
            .filter(
                tenant=tenant,
                branch=branch,
                device_namespace=dev_code,
                period_key=period_key,
            )
            .first()
        )

        if not tracker:
            tracker, _ = PosInvoiceSequenceTracker.objects.get_or_create(
                tenant=tenant,
                branch=branch,
                device_namespace=dev_code,
                period_key=period_key,
                defaults={
                    "tracker_id": f"trkr-{uuid.uuid4().hex[:12]}",
                    "last_sequence": 0,
                },
            )
            # Re-lock after creation
            tracker = (
                PosInvoiceSequenceTracker.objects.select_for_update()
                .filter(pk=tracker.pk)
                .first()
            )

        next_sequence = tracker.last_sequence + 1
        if reserve:
            tracker.last_sequence = next_sequence
            tracker.save(update_fields=["last_sequence", "updated_at"])

        seq_str = str(next_sequence).zfill(policy.padding_digits)
        pattern = policy.pattern or "{prefix}-{branch}-{device}-{period}-{seq}"
        formatted_invoice_number = pattern.format(
            prefix=policy.prefix,
            branch=branch_code,
            device=dev_code,
            period=period_key,
            seq=seq_str,
        )

        return {
            "invoice_number": formatted_invoice_number,
            "policy_id": policy.policy_id,
            "sequence": next_sequence,
            "period_key": period_key,
            "branch_code": branch_code,
            "device_code": dev_code,
            "is_reserved": reserve,
        }

    @classmethod
    def preview_next_invoice_number(
        cls,
        tenant: Tenant,
        branch: Branch,
        device_namespace: str = "ONLINE",
        timestamp: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        with transaction.atomic():
            return cls.generate_next_invoice_number_in_tx(
                tenant=tenant,
                branch=branch,
                device_namespace=device_namespace,
                timestamp=timestamp,
                reserve=False,
            )

    @classmethod
    def validate_and_register_offline_invoice(
        cls,
        tenant: Tenant,
        branch: Branch,
        invoice_number: str,
        offline_device_namespace: str,
        offline_local_sequence: Optional[int],
    ) -> None:
        """
        Validates an offline submitted invoice number against collision invariants.
        """
        inv_str = str(invoice_number or "").strip()
        if not inv_str:
            raise ValidationError({"invoice_number": "Invoice number cannot be blank."})

        # Check existing SaleInvoice under this tenant
        if SaleInvoice.objects.filter(tenant=tenant, invoice_number=inv_str).exists():
            raise ValidationError(
                {
                    "invoice_number": (
                        f"Invoice number '{inv_str}' has already been finalized under this tenant. "
                        "Offline replay or duplicate checkout detected."
                    )
                }
            )

        # Check existing PosSaleOrder with same order_number
        if PosSaleOrder.objects.filter(tenant=tenant, order_number=inv_str).exists():
            raise ValidationError(
                {
                    "invoice_number": (
                        f"Order/Invoice number '{inv_str}' already exists in POS sales. "
                        "Replay rejected."
                    )
                }
            )

        # Check offline device sequence uniqueness
        dev_ns = str(offline_device_namespace or "").strip()
        if dev_ns and offline_local_sequence is not None:
            clash = PosSaleOrder.objects.filter(
                tenant=tenant,
                offline_device_namespace=dev_ns,
                offline_local_sequence=offline_local_sequence,
            ).exists()
            if clash:
                raise ValidationError(
                    {
                        "offline_local_sequence": (
                            f"Device sequence {offline_local_sequence} for namespace '{dev_ns}' "
                            "has already been synchronized. Duplicate offline transaction rejected."
                        )
                    }
                )

