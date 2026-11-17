"""
backend/apps/core/supply_chain_alert_service.py
Architecture Decision: ADR-0214 (P214).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Domain Service for Supply Chain Stock Alerts:
- Evaluates stock levels (Reorder Required, Stockout, Overstock, Negative Availability).
- Evaluates cold-chain storage temperature risks.
- Enforces strict deduplication so repeated triggers increment occurrence count rather than spamming alerts.
- Auto-resolves open alerts when inventory balances or temperatures return to normal safe levels.
- Full lifecycle transitions (Open -> Acknowledged -> Resolved / Dismissed).
- Multi-tenant isolation and RBAC (Cashier access denied).
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    AlertSeverity,
    AlertStatus,
    Branch,
    BranchStockAvailability,
    MedicineProduct,
    StockAlertType,
    StorageLocation,
    SupplyChainStockAlert,
    Tenant,
)
from apps.identity.models import UserRole


class SupplyChainAlertService:
    """
    Business service for managing supply chain alerts with deduplication,
    lifecycle state transitions, and auto-resolution.
    """

    ALLOWED_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "SUPERADMIN",
        "STAFF_PHARMACIST",
        "PROCUREMENT_OFFICER",
        "INVENTORY_CLERK",
    }

    @classmethod
    def assert_alert_management_role(cls, user: Any) -> None:
        """
        Validates that user has operational or managerial role.
        Cashiers are prohibited from acknowledging, resolving, or modifying alerts.
        """
        role = getattr(user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str == "CASHIER" or role_str not in cls.ALLOWED_ROLES:
            raise PermissionDenied(
                "Cashiers cannot acknowledge or resolve supply chain alerts. Access is restricted to Managers, Owners, and Pharmacists."
            )

    @classmethod
    def compute_fingerprint(
        cls,
        tenant_id: str,
        branch_id: str,
        alert_type: str,
        entity_id: str,
    ) -> str:
        raw = f"{tenant_id}:{branch_id}:{alert_type}:{entity_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]

    @classmethod
    @transaction.atomic
    def create_or_deduplicate_alert(
        cls,
        tenant: Tenant,
        branch: Branch,
        alert_type: str,
        severity: str,
        title: str,
        message: str,
        message_bn: str = "",
        product: Optional[MedicineProduct] = None,
        storage_location: Optional[StorageLocation] = None,
        current_quantity: int = 0,
        threshold_quantity: Optional[int] = None,
        current_temperature: Optional[Decimal] = None,
    ) -> Tuple[SupplyChainStockAlert, bool]:
        """
        Creates a new alert or deduplicates into an existing active (OPEN/ACKNOWLEDGED) alert.
        Returns (alert, created: bool).
        """
        entity_id = (
            str(product.product_id)
            if product
            else (str(storage_location.location_id) if storage_location else "general")
        )
        fingerprint = cls.compute_fingerprint(
            tenant.tenant_id,
            branch.branch_id,
            alert_type,
            entity_id,
        )

        existing = (
            SupplyChainStockAlert.objects.select_for_update()
            .filter(
                tenant=tenant,
                branch=branch,
                dedup_fingerprint=fingerprint,
                status__in=[AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED],
            )
            .first()
        )

        if existing:
            existing.occurred_count += 1
            existing.current_quantity = current_quantity
            existing.threshold_quantity = threshold_quantity
            existing.current_temperature = current_temperature
            existing.last_triggered_at = timezone.now()
            existing.save(
                update_fields=[
                    "occurred_count",
                    "current_quantity",
                    "threshold_quantity",
                    "current_temperature",
                    "last_triggered_at",
                ]
            )
            return existing, False

        new_alert = SupplyChainStockAlert.objects.create(
            tenant=tenant,
            branch=branch,
            product=product,
            storage_location=storage_location,
            alert_type=alert_type,
            severity=severity,
            status=AlertStatus.OPEN,
            title=title,
            message=message,
            message_bn=message_bn,
            current_quantity=current_quantity,
            threshold_quantity=threshold_quantity,
            current_temperature=current_temperature,
            dedup_fingerprint=fingerprint,
            occurred_count=1,
        )
        return new_alert, True

    @classmethod
    def evaluate_stock_level(
        cls,
        tenant: Tenant,
        branch: Branch,
        product: MedicineProduct,
        quantity_on_hand: int,
        reorder_level: int = 0,
        max_level: Optional[int] = None,
    ) -> Optional[SupplyChainStockAlert]:
        """
        Evaluates current physical stock and creates/deduplicates reorder, stockout,
        overstock, or negative availability alerts. Auto-resolves if safe.
        """
        # Negative availability anomaly
        if quantity_on_hand < 0:
            alert, _ = cls.create_or_deduplicate_alert(
                tenant=tenant,
                branch=branch,
                product=product,
                alert_type=StockAlertType.NEGATIVE_AVAILABILITY,
                severity=AlertSeverity.CRITICAL,
                title=f"Negative Stock: {product.brand_name}",
                message=f"Negative stock balance detected ({quantity_on_hand} units). Immediate ledger audit required.",
                message_bn=f"নেতিবাচক স্টক অসঙ্গতি শনাক্ত হয়েছে ({quantity_on_hand} ইউনিট)। অবিলম্বে অডিট প্রয়োজন।",
                current_quantity=quantity_on_hand,
            )
            return alert

        # Complete stockout
        if quantity_on_hand == 0:
            alert, _ = cls.create_or_deduplicate_alert(
                tenant=tenant,
                branch=branch,
                product=product,
                alert_type=StockAlertType.STOCKOUT,
                severity=AlertSeverity.CRITICAL,
                title=f"Stockout: {product.brand_name}",
                message=f"Product {product.brand_name} is completely out of stock in {branch.name}.",
                message_bn=f"{product.brand_name} সম্পূর্ণরূপে স্টকআউট হয়ে গেছে।",
                current_quantity=0,
                threshold_quantity=reorder_level,
            )
            return alert

        # Below reorder point
        if reorder_level > 0 and quantity_on_hand <= reorder_level:
            alert, _ = cls.create_or_deduplicate_alert(
                tenant=tenant,
                branch=branch,
                product=product,
                alert_type=StockAlertType.REORDER_REQUIRED,
                severity=AlertSeverity.WARNING,
                title=f"Reorder Level Reached: {product.brand_name}",
                message=f"Current stock ({quantity_on_hand}) is at or below reorder threshold ({reorder_level}). Create purchase order.",
                message_bn=f"স্টক ({quantity_on_hand}) রিঅর্ডার সীমার ({reorder_level}) নিচে নেমেছে। নতুন ক্রয়াদেশ প্রয়োজন।",
                current_quantity=quantity_on_hand,
                threshold_quantity=reorder_level,
            )
            return alert

        # Overstock exceeded
        if max_level is not None and quantity_on_hand > max_level:
            alert, _ = cls.create_or_deduplicate_alert(
                tenant=tenant,
                branch=branch,
                product=product,
                alert_type=StockAlertType.OVERSTOCK,
                severity=AlertSeverity.INFO,
                title=f"Overstock: {product.brand_name}",
                message=f"Current stock ({quantity_on_hand}) exceeds max target level ({max_level}).",
                message_bn=f"স্টক পরিমাণ ({quantity_on_hand}) সর্বোচ্চ সীমার ({max_level}) চেয়ে বেশি।",
                current_quantity=quantity_on_hand,
                threshold_quantity=max_level,
            )
            return alert

        # If stock is safe, auto-resolve any open stockout / reorder alert
        SupplyChainStockAlert.objects.filter(
            tenant=tenant,
            branch=branch,
            product=product,
            alert_type__in=[
                StockAlertType.STOCKOUT,
                StockAlertType.REORDER_REQUIRED,
                StockAlertType.NEGATIVE_AVAILABILITY,
            ],
            status__in=[AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED],
        ).update(
            status=AlertStatus.RESOLVED,
            resolution_notes="Auto-resolved: Stock replenished into safe operating balance.",
            resolved_at=timezone.now(),
        )
        return None

    @classmethod
    def evaluate_temperature_risk(
        cls,
        tenant: Tenant,
        branch: Branch,
        storage_location: StorageLocation,
        recorded_temp: Decimal,
        min_threshold: Decimal = Decimal("2.00"),
        max_threshold: Decimal = Decimal("8.00"),
    ) -> Optional[SupplyChainStockAlert]:
        """
        Evaluates temperature reading against boundaries and emits deduplicated temperature risk alert.
        """
        if recorded_temp < min_threshold or recorded_temp > max_threshold:
            diff = max(min_threshold - recorded_temp, recorded_temp - max_threshold)
            severity = (
                AlertSeverity.EMERGENCY
                if (diff >= Decimal("5.00") or recorded_temp >= Decimal("15.00"))
                else AlertSeverity.CRITICAL
            )

            alert, _ = cls.create_or_deduplicate_alert(
                tenant=tenant,
                branch=branch,
                storage_location=storage_location,
                alert_type=StockAlertType.TEMPERATURE_RISK,
                severity=severity,
                title=f"Temperature Excursion: {storage_location.name}",
                message=(
                    f"Temperature breached safe limits: recorded {recorded_temp}°C "
                    f"(safe range {min_threshold}°C - {max_threshold}°C) in location '{storage_location.name}'."
                ),
                message_bn=(
                    f"তাপমাত্রা সীমা লঙ্ঘিত হয়েছে: {recorded_temp}°সে "
                    f"(নিরাপদ সীমা {min_threshold}°সে - {max_threshold}°সে)।"
                ),
                current_temperature=recorded_temp,
            )
            return alert

        # Auto-resolve temperature alert if normalized
        SupplyChainStockAlert.objects.filter(
            tenant=tenant,
            branch=branch,
            storage_location=storage_location,
            alert_type=StockAlertType.TEMPERATURE_RISK,
            status__in=[AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED],
        ).update(
            status=AlertStatus.RESOLVED,
            resolution_notes="Auto-resolved: Temperature restored to safe compliant range.",
            resolved_at=timezone.now(),
        )
        return None

    @classmethod
    @transaction.atomic
    def acknowledge_alert(
        cls,
        alert: SupplyChainStockAlert,
        actor_user: Any,
        notes: str = "",
    ) -> SupplyChainStockAlert:
        cls.assert_alert_management_role(actor_user)
        if alert.status == AlertStatus.RESOLVED:
            raise ValidationError({"status": "Cannot acknowledge an already resolved alert."})

        alert.status = AlertStatus.ACKNOWLEDGED
        alert.acknowledged_by = actor_user
        alert.acknowledged_at = timezone.now()
        if notes:
            alert.resolution_notes = f"Acknowledge note: {notes}"
        alert.save(update_fields=["status", "acknowledged_by", "acknowledged_at", "resolution_notes"])
        return alert

    @classmethod
    @transaction.atomic
    def resolve_alert(
        cls,
        alert: SupplyChainStockAlert,
        actor_user: Any,
        resolution_notes: str = "",
    ) -> SupplyChainStockAlert:
        cls.assert_alert_management_role(actor_user)
        alert.status = AlertStatus.RESOLVED
        alert.resolved_by = actor_user
        alert.resolved_at = timezone.now()
        alert.resolution_notes = resolution_notes.strip() or "Resolved by authorized staff."
        alert.save(update_fields=["status", "resolved_by", "resolved_at", "resolution_notes"])
        return alert

    @classmethod
    @transaction.atomic
    def dismiss_alert(
        cls,
        alert: SupplyChainStockAlert,
        actor_user: Any,
        notes: str = "",
    ) -> SupplyChainStockAlert:
        cls.assert_alert_management_role(actor_user)
        alert.status = AlertStatus.DISMISSED
        alert.resolved_by = actor_user
        alert.resolved_at = timezone.now()
        alert.resolution_notes = notes.strip() or "Dismissed by staff."
        alert.save(update_fields=["status", "resolved_by", "resolved_at", "resolution_notes"])
        return alert

    @classmethod
    def scan_branch_inventory(
        cls,
        tenant: Tenant,
        branch: Branch,
    ) -> Dict[str, Any]:
        """
        Scans all BranchStockAvailability rows in a branch and evaluates alerts with deduplication.
        """
        availabilities = BranchStockAvailability.objects.filter(
            tenant=tenant,
            branch=branch,
        ).select_related("product")

        evaluated = 0
        generated = 0
        deduplicated = 0

        for a in availabilities:
            evaluated += 1
            alert = cls.evaluate_stock_level(
                tenant=tenant,
                branch=branch,
                product=a.product,
                quantity_on_hand=a.quantity_on_hand,
                reorder_level=a.reorder_level_base_units,
            )
            if alert:
                if alert.occurred_count == 1:
                    generated += 1
                else:
                    deduplicated += 1

        active_count = SupplyChainStockAlert.objects.filter(
            tenant=tenant,
            branch=branch,
            status__in=[AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED],
        ).count()

        return {
            "branch_id": branch.branch_id,
            "evaluated_products_count": evaluated,
            "new_alerts_generated": generated,
            "deduplicated_alerts_count": deduplicated,
            "total_active_alerts": active_count,
        }
