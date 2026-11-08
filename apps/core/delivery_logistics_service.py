"""
backend/apps/core/delivery_logistics_service.py
Architecture Decision: ADR-0196 (P196).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Service layer governing Delivery and Logistics Tracking:
- Tracks shipment stages (DISPATCHED, IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED, RECEIVING_CONFIRMED).
- Vehicle/courier consignment data, driver contacts, and delivery ETAs.
- In-transit cold-chain temperature telemetry and breach alerts.
- Branch receiving confirmation, package counts, discrepancy reporting, and event timelines.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    CarrierType,
    GoodsReceiptNote,
    ProcurementShipment,
    ProcurementShipmentEvent,
    PurchaseOrder,
    ShipmentDiscrepancyType,
    ShipmentStage,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)

logger = logging.getLogger(__name__)


class DeliveryLogisticsService:
    """
    Authoritative service governing procurement logistics, carrier metadata, transit checkpoints,
    and branch receiving confirmations.
    """

    _ALLOWED_PROCUREMENT_ROLES = {
        "BRANCH_MANAGER",
        "PHARMACY_OWNER",
        "SUPERADMIN",
        "STAFF_PHARMACIST",
        "PROCUREMENT_OFFICER",
        "INVENTORY_CLERK",
        "AUDITOR",
    }

    _ALLOWED_RECEIVING_ROLES = {
        "BRANCH_MANAGER",
        "PHARMACY_OWNER",
        "SUPERADMIN",
        "STAFF_PHARMACIST",
        "INVENTORY_CLERK",
        "PROCUREMENT_OFFICER",
    }

    @classmethod
    def _assert_procurement_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str == "CASHIER" or role_str not in cls._ALLOWED_PROCUREMENT_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Staff role does not have permission to access logistics tracking.",
                }
            )

    @classmethod
    def _assert_receiving_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str == "CASHIER" or role_str not in cls._ALLOWED_RECEIVING_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Staff role does not have permission to confirm branch receiving.",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant_id: str) -> Tenant:
        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant {tenant_id} not found."})

        if tenant.status == TenantLifecycleStatus.SUSPENDED:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant subscription is suspended.",
                }
            )
        return tenant

    @classmethod
    def serialize_event(cls, ev: ProcurementShipmentEvent) -> Dict[str, Any]:
        return {
            "event_id": ev.event_id,
            "stage": ev.stage,
            "location_name": ev.location_name,
            "notes": ev.notes,
            "recorded_by": ev.recorded_by,
            "recorded_at": ev.recorded_at.isoformat() if ev.recorded_at else None,
        }

    @classmethod
    def serialize_shipment(cls, shp: ProcurementShipment) -> Dict[str, Any]:
        return {
            "shipment_id": shp.shipment_id,
            "tracking_number": shp.tracking_number,
            "tenant_id": shp.tenant_id,
            "branch_id": shp.branch_id,
            "branch_name": shp.branch.name if shp.branch else "",
            "purchase_order_id": shp.purchase_order_id,
            "purchase_order_number": shp.purchase_order.po_number if shp.purchase_order else "",
            "supplier_id": shp.supplier_id,
            "supplier_name": shp.supplier.name if shp.supplier else "",
            "carrier_type": shp.carrier_type,
            "carrier_name": shp.carrier_name,
            "consignment_number": shp.consignment_number,
            "vehicle_registration_number": shp.vehicle_registration_number,
            "driver_name": shp.driver_name,
            "driver_phone": shp.driver_phone,
            "stage": shp.stage,
            "dispatched_at": shp.dispatched_at.isoformat() if shp.dispatched_at else None,
            "estimated_delivery_at": shp.estimated_delivery_at.isoformat() if shp.estimated_delivery_at else None,
            "actual_delivery_at": shp.actual_delivery_at.isoformat() if shp.actual_delivery_at else None,
            "total_packages_dispatched": shp.total_packages_dispatched,
            "total_packages_received": shp.total_packages_received,
            "damaged_packages": shp.damaged_packages,
            "missing_packages": shp.missing_packages,
            "discrepancy_type": shp.discrepancy_type,
            "discrepancy_notes": shp.discrepancy_notes,
            "requires_cold_chain": shp.requires_cold_chain,
            "transit_temp_celsius": str(shp.transit_temp_celsius) if shp.transit_temp_celsius is not None else None,
            "cold_chain_breached": shp.cold_chain_breached,
            "confirmed_by": str(getattr(shp.confirmed_by, "user_id", shp.confirmed_by_id or "")) if shp.confirmed_by_id else None,
            "confirmed_at": shp.confirmed_at.isoformat() if shp.confirmed_at else None,
            "goods_receipt_note_id": shp.goods_receipt_note_id,
            "notes": shp.notes,
            "is_active": shp.is_active,
            "events": [cls.serialize_event(e) for e in shp.events.all().order_by("recorded_at")],
            "created_at": shp.created_at.isoformat() if shp.created_at else None,
            "updated_at": shp.updated_at.isoformat() if shp.updated_at else None,
        }

    @classmethod
    @transaction.atomic
    def create_shipment(
        cls,
        tenant_id: str,
        actor_user: Any,
        purchase_order_id: str,
        carrier_name: str,
        estimated_delivery_at: datetime.datetime,
        carrier_type: str = CarrierType.SUPPLIER_FLEET,
        consignment_number: str = "",
        vehicle_registration_number: str = "",
        driver_name: str = "",
        driver_phone: str = "",
        total_packages_dispatched: int = 1,
        requires_cold_chain: bool = False,
        transit_temp_celsius: Optional[Decimal] = None,
        dispatched_at: Optional[datetime.datetime] = None,
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Registers a new outbound shipment for a purchase order with carrier/vehicle data.
        """
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            po = PurchaseOrder.objects.get(tenant=tenant, po_id=purchase_order_id, is_active=True)
        except PurchaseOrder.DoesNotExist:
            raise NotFound({"error_code": "ERR_PURCHASE_ORDER_NOT_FOUND", "detail": f"PO {purchase_order_id} not found."})

        if not carrier_name or not carrier_name.strip():
            raise ValidationError({"error_code": "ERR_CARRIER_REQUIRED", "detail": "Carrier name is required."})

        now = timezone.now()
        dispatch_time = dispatched_at or now

        if estimated_delivery_at and estimated_delivery_at < dispatch_time:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_ETA",
                    "detail": "Estimated delivery time cannot be earlier than dispatch time.",
                }
            )

        cold_chain_breached = False
        if requires_cold_chain and transit_temp_celsius is not None:
            # Cold-chain standard 2°C - 8°C
            if transit_temp_celsius > Decimal("8.00") or transit_temp_celsius < Decimal("2.00"):
                cold_chain_breached = True

        shp_id = f"shp-{uuid.uuid4().hex[:12]}"
        tracking_num = f"SHP-{now.strftime('%Y%m')}-{uuid.uuid4().hex[:6].upper()}"

        shipment = ProcurementShipment.objects.create(
            shipment_id=shp_id,
            tracking_number=tracking_num,
            tenant=tenant,
            branch=po.branch or tenant.branches.first(),
            purchase_order=po,
            supplier=po.supplier,
            carrier_type=carrier_type,
            carrier_name=carrier_name.strip(),
            consignment_number=consignment_number.strip(),
            vehicle_registration_number=vehicle_registration_number.strip(),
            driver_name=driver_name.strip(),
            driver_phone=driver_phone.strip(),
            stage=ShipmentStage.DISPATCHED,
            dispatched_at=dispatch_time,
            estimated_delivery_at=estimated_delivery_at,
            total_packages_dispatched=max(1, total_packages_dispatched),
            requires_cold_chain=requires_cold_chain,
            transit_temp_celsius=transit_temp_celsius,
            cold_chain_breached=cold_chain_breached,
            notes=notes,
            is_active=True,
        )

        ProcurementShipmentEvent.objects.create(
            event_id=f"she-{uuid.uuid4().hex[:12]}",
            shipment=shipment,
            stage=ShipmentStage.DISPATCHED,
            location_name=f"{po.supplier.name} Central Depot" if po.supplier else "Supplier Hub",
            notes=f"Shipment dispatched with {shipment.total_packages_dispatched} package(s). Carrier: {carrier_name}",
            recorded_by=str(getattr(actor_user, "username", "procurement")),
            recorded_at=dispatch_time,
        )

        return cls.serialize_shipment(shipment)

    @classmethod
    @transaction.atomic
    def update_shipment_stage(
        cls,
        tenant_id: str,
        actor_user: Any,
        shipment_id: str,
        new_stage: str,
        location_name: str = "",
        transit_temp_celsius: Optional[Decimal] = None,
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Updates shipment progress through logistics stages with checkpoint events.
        """
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            shipment = ProcurementShipment.objects.select_for_update().get(
                tenant=tenant,
                shipment_id=shipment_id,
                is_active=True,
            )
        except ProcurementShipment.DoesNotExist:
            raise NotFound({"error_code": "ERR_SHIPMENT_NOT_FOUND", "detail": f"Shipment {shipment_id} not found."})

        if shipment.stage == ShipmentStage.RECEIVING_CONFIRMED:
            raise ValidationError(
                {
                    "error_code": "ERR_SHIPMENT_ALREADY_CONFIRMED",
                    "detail": "Cannot update stage on a confirmed delivery.",
                }
            )

        now = timezone.now()
        shipment.stage = new_stage

        if transit_temp_celsius is not None:
            shipment.transit_temp_celsius = transit_temp_celsius
            if shipment.requires_cold_chain:
                if transit_temp_celsius > Decimal("8.00") or transit_temp_celsius < Decimal("2.00"):
                    shipment.cold_chain_breached = True

        if new_stage == ShipmentStage.DELIVERED and not shipment.actual_delivery_at:
            shipment.actual_delivery_at = now

        shipment.save(update_fields=["stage", "transit_temp_celsius", "cold_chain_breached", "actual_delivery_at", "updated_at"])

        ProcurementShipmentEvent.objects.create(
            event_id=f"she-{uuid.uuid4().hex[:12]}",
            shipment=shipment,
            stage=new_stage,
            location_name=location_name.strip(),
            notes=notes.strip(),
            recorded_by=str(getattr(actor_user, "username", "logistics")),
            recorded_at=now,
        )

        return cls.serialize_shipment(shipment)

    @classmethod
    @transaction.atomic
    def confirm_branch_receiving(
        cls,
        tenant_id: str,
        actor_user: Any,
        shipment_id: str,
        total_packages_received: int,
        damaged_packages: int = 0,
        missing_packages: int = 0,
        transit_temp_celsius: Optional[Decimal] = None,
        discrepancy_notes: str = "",
    ) -> Dict[str, Any]:
        """
        Branch receiving confirmation documenting discrepancies (shortage, damage, cold chain breach).
        """
        cls._assert_receiving_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            shipment = ProcurementShipment.objects.select_for_update().get(
                tenant=tenant,
                shipment_id=shipment_id,
                is_active=True,
            )
        except ProcurementShipment.DoesNotExist:
            raise NotFound({"error_code": "ERR_SHIPMENT_NOT_FOUND", "detail": f"Shipment {shipment_id} not found."})

        if shipment.stage == ShipmentStage.RECEIVING_CONFIRMED:
            raise ValidationError(
                {
                    "error_code": "ERR_SHIPMENT_ALREADY_CONFIRMED",
                    "detail": "Shipment receiving has already been confirmed.",
                }
            )

        now = timezone.now()
        recv_pkgs = max(0, total_packages_received)
        dmg_pkgs = max(0, damaged_packages)
        miss_pkgs = max(0, missing_packages)

        # Check shortage against dispatched
        if (recv_pkgs + miss_pkgs) < shipment.total_packages_dispatched and miss_pkgs == 0:
            miss_pkgs = shipment.total_packages_dispatched - recv_pkgs

        # Evaluate cold chain
        cold_breached = shipment.cold_chain_breached
        if transit_temp_celsius is not None:
            shipment.transit_temp_celsius = transit_temp_celsius
            if shipment.requires_cold_chain and (transit_temp_celsius > Decimal("8.00") or transit_temp_celsius < Decimal("2.00")):
                cold_breached = True
        shipment.cold_chain_breached = cold_breached

        # Determine discrepancy type
        if cold_breached:
            disc_type = ShipmentDiscrepancyType.TEMPERATURE_BREACH
        elif dmg_pkgs > 0:
            disc_type = ShipmentDiscrepancyType.DAMAGED
        elif miss_pkgs > 0:
            disc_type = ShipmentDiscrepancyType.SHORTAGE
        elif recv_pkgs > shipment.total_packages_dispatched:
            disc_type = ShipmentDiscrepancyType.OVERAGE
        else:
            disc_type = ShipmentDiscrepancyType.NONE

        shipment.stage = ShipmentStage.RECEIVING_CONFIRMED
        shipment.total_packages_received = recv_pkgs
        shipment.damaged_packages = dmg_pkgs
        shipment.missing_packages = miss_pkgs
        shipment.discrepancy_type = disc_type
        shipment.discrepancy_notes = discrepancy_notes.strip()
        shipment.confirmed_by = actor_user if hasattr(actor_user, "user_id") else None
        shipment.confirmed_at = now
        if not shipment.actual_delivery_at:
            shipment.actual_delivery_at = now

        shipment.save(
            update_fields=[
                "stage",
                "total_packages_received",
                "damaged_packages",
                "missing_packages",
                "discrepancy_type",
                "discrepancy_notes",
                "transit_temp_celsius",
                "cold_chain_breached",
                "confirmed_by",
                "confirmed_at",
                "actual_delivery_at",
                "updated_at",
            ]
        )

        ProcurementShipmentEvent.objects.create(
            event_id=f"she-{uuid.uuid4().hex[:12]}",
            shipment=shipment,
            stage=ShipmentStage.RECEIVING_CONFIRMED,
            location_name=f"{shipment.branch.name} Gate" if shipment.branch else "Branch Receiving",
            notes=f"Branch receipt confirmed: {recv_pkgs} received, {dmg_pkgs} damaged, {miss_pkgs} missing. Discrepancy: {disc_type}",
            recorded_by=str(getattr(actor_user, "username", "receiving_clerk")),
            recorded_at=now,
        )

        return cls.serialize_shipment(shipment)

    @classmethod
    def get_shipment(cls, tenant_id: str, actor_user: Any, shipment_id: str) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            shipment = ProcurementShipment.objects.get(
                tenant=tenant,
                shipment_id=shipment_id,
                is_active=True,
            )
        except ProcurementShipment.DoesNotExist:
            raise NotFound({"error_code": "ERR_SHIPMENT_NOT_FOUND", "detail": f"Shipment {shipment_id} not found."})

        return cls.serialize_shipment(shipment)

    @classmethod
    def list_shipments(
        cls,
        tenant_id: str,
        actor_user: Any,
        stage: Optional[str] = None,
        purchase_order_id: Optional[str] = None,
        branch_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        qs = ProcurementShipment.objects.filter(tenant=tenant, is_active=True)
        if stage:
            qs = qs.filter(stage=stage)
        if purchase_order_id:
            qs = qs.filter(purchase_order_id=purchase_order_id)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        return [cls.serialize_shipment(s) for s in qs.order_by("-dispatched_at")[:50]]
