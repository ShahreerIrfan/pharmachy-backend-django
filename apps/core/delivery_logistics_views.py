"""
backend/apps/core/delivery_logistics_views.py
Architecture Decision: ADR-0196 (P196).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

REST API ViewSet for Delivery and Logistics Tracking:
- Manages shipment lifecycles, carrier/vehicle metadata, and live ETAs.
- Checkpoint event logging and in-transit temperature tracking.
- Branch receiving confirmations and discrepancy logging.
"""

from __future__ import annotations

import datetime
import logging
from decimal import Decimal
from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

from apps.core.delivery_logistics_service import DeliveryLogisticsService

logger = logging.getLogger(__name__)


class DeliveryLogisticsViewSet(viewsets.ViewSet):
    """
    ViewSet for tracking procurement shipments, carrier logistics, and receiving confirmations.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_tenant_id(self, request: Request) -> str:
        tenant_id = getattr(request, "tenant_id", None)
        if not tenant_id and hasattr(request.user, "tenant_id"):
            tenant_id = str(request.user.tenant_id)
        if not tenant_id:
            raise ValidationError(
                {
                    "error_code": "ERR_TENANT_REQUIRED",
                    "detail": "Tenant identification is required for logistics tracking.",
                }
            )
        return str(tenant_id)

    def create(self, request: Request) -> Response:
        """
        Creates a new procurement shipment.
        """
        tenant_id = self._get_tenant_id(request)
        po_id = request.data.get("purchase_order_id")
        carrier_name = request.data.get("carrier_name")
        eta_raw = request.data.get("estimated_delivery_at")

        if not po_id:
            raise ValidationError({"error_code": "ERR_PO_REQUIRED", "detail": "purchase_order_id is required."})
        if not carrier_name:
            raise ValidationError({"error_code": "ERR_CARRIER_REQUIRED", "detail": "carrier_name is required."})
        if not eta_raw:
            raise ValidationError({"error_code": "ERR_ETA_REQUIRED", "detail": "estimated_delivery_at is required."})

        try:
            if isinstance(eta_raw, str):
                estimated_delivery_at = datetime.datetime.fromisoformat(eta_raw.replace("Z", "+00:00"))
            else:
                estimated_delivery_at = eta_raw
        except ValueError:
            raise ValidationError({"error_code": "ERR_INVALID_DATE_FORMAT", "detail": "Invalid ISO format for estimated_delivery_at."})

        temp_raw = request.data.get("transit_temp_celsius")
        transit_temp = Decimal(str(temp_raw)) if temp_raw is not None else None

        shipment = DeliveryLogisticsService.create_shipment(
            tenant_id=tenant_id,
            actor_user=request.user,
            purchase_order_id=po_id,
            carrier_name=carrier_name,
            estimated_delivery_at=estimated_delivery_at,
            carrier_type=request.data.get("carrier_type", "SUPPLIER_FLEET"),
            consignment_number=request.data.get("consignment_number", ""),
            vehicle_registration_number=request.data.get("vehicle_registration_number", ""),
            driver_name=request.data.get("driver_name", ""),
            driver_phone=request.data.get("driver_phone", ""),
            total_packages_dispatched=int(request.data.get("total_packages_dispatched", 1) or 1),
            requires_cold_chain=bool(request.data.get("requires_cold_chain", False)),
            transit_temp_celsius=transit_temp,
            notes=request.data.get("notes", ""),
        )
        return Response(shipment, status=status.HTTP_201_CREATED)

    def list(self, request: Request) -> Response:
        """
        Lists tracked shipments with optional filters.
        """
        tenant_id = self._get_tenant_id(request)
        stage_filter = request.query_params.get("stage")
        po_filter = request.query_params.get("purchase_order_id")
        branch_filter = request.query_params.get("branch_id")

        shipments = DeliveryLogisticsService.list_shipments(
            tenant_id=tenant_id,
            actor_user=request.user,
            stage=stage_filter,
            purchase_order_id=po_filter,
            branch_id=branch_filter,
        )
        return Response({"count": len(shipments), "results": shipments}, status=status.HTTP_200_OK)

    def retrieve(self, request: Request, pk: str = None) -> Response:
        """
        Retrieves a single shipment by ID with checkpoint events.
        """
        tenant_id = self._get_tenant_id(request)
        shipment = DeliveryLogisticsService.get_shipment(
            tenant_id=tenant_id,
            actor_user=request.user,
            shipment_id=pk,
        )
        return Response(shipment, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="update-stage")
    def update_stage(self, request: Request, pk: str = None) -> Response:
        """
        Transitions shipment to a new stage and logs location event.
        """
        tenant_id = self._get_tenant_id(request)
        new_stage = request.data.get("stage")
        if not new_stage:
            raise ValidationError({"error_code": "ERR_STAGE_REQUIRED", "detail": "Field 'stage' is required."})

        location_name = request.data.get("location_name", "")
        notes = request.data.get("notes", "")

        temp_raw = request.data.get("transit_temp_celsius")
        transit_temp = Decimal(str(temp_raw)) if temp_raw is not None else None

        updated = DeliveryLogisticsService.update_shipment_stage(
            tenant_id=tenant_id,
            actor_user=request.user,
            shipment_id=pk,
            new_stage=new_stage,
            location_name=location_name,
            transit_temp_celsius=transit_temp,
            notes=notes,
        )
        return Response(updated, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="confirm-receiving")
    def confirm_receiving(self, request: Request, pk: str = None) -> Response:
        """
        Confirms branch receiving, verifies package counts, and records discrepancies.
        """
        tenant_id = self._get_tenant_id(request)
        received_pkgs_raw = request.data.get("total_packages_received")
        if received_pkgs_raw is None:
            raise ValidationError(
                {
                    "error_code": "ERR_RECEIVED_PACKAGES_REQUIRED",
                    "detail": "Field 'total_packages_received' is required.",
                }
            )

        recv_pkgs = int(received_pkgs_raw)
        dmg_pkgs = int(request.data.get("damaged_packages", 0) or 0)
        miss_pkgs = int(request.data.get("missing_packages", 0) or 0)

        temp_raw = request.data.get("transit_temp_celsius")
        transit_temp = Decimal(str(temp_raw)) if temp_raw is not None else None
        discrepancy_notes = request.data.get("discrepancy_notes", "")

        confirmed = DeliveryLogisticsService.confirm_branch_receiving(
            tenant_id=tenant_id,
            actor_user=request.user,
            shipment_id=pk,
            total_packages_received=recv_pkgs,
            damaged_packages=dmg_pkgs,
            missing_packages=miss_pkgs,
            transit_temp_celsius=transit_temp,
            discrepancy_notes=discrepancy_notes,
        )
        return Response(confirmed, status=status.HTTP_200_OK)
