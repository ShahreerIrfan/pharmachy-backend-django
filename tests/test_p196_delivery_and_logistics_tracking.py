"""
backend/tests/test_p196_delivery_and_logistics_tracking.py
Architecture Decision: ADR-0196 (P196).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration test suite verifying Delivery and Logistics Tracking:
- Shipment lifecycle stages (DISPATCHED, IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED, RECEIVING_CONFIRMED).
- Carrier/vehicle consignment details, driver contacts, and delivery ETAs.
- In-transit cold-chain temperature monitoring and excursion breach alerts.
- Branch receiving confirmation, package counts, discrepancy reporting, and event timelines.
- RBAC (Cashier blocked) and cross-tenant access boundaries.
"""

import datetime
from decimal import Decimal
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    CarrierType,
    MedicineProduct,
    ProcurementShipment,
    PurchaseOrder,
    PurchaseOrderStatus,
    ShipmentDiscrepancyType,
    ShipmentStage,
    Supplier,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestDeliveryAndLogisticsTrackingIntegration:
    @classmethod
    def setup_class(cls):
        cls.client = APIClient()

    def setup_method(self):
        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id="tenant-log-001",
            name="Dhaka Central Pharmacy",
            subdomain="dhaka-central-log",
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id="tenant-log-002",
            name="Chittagong Care Pharmacy",
            subdomain="ctg-care-log",
        )

        # 2. Branches
        self.branch_a = Branch.objects.create(
            branch_id="br-log-001",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
        )

        # 3. Users
        self.manager = PharmacyUser.objects.create_user(
            email="manager@dhaka-central.com",
            phone_number="+8801712000001",
            full_name="Procurement Manager",
            password="StrongPassword123!",
            tenant_id=self.tenant_a.tenant_id,
            role=UserRole.BRANCH_MANAGER,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email="cashier@dhaka-central.com",
            phone_number="+8801712000002",
            full_name="Front Cashier",
            password="StrongPassword123!",
            tenant_id=self.tenant_a.tenant_id,
            role=UserRole.CASHIER,
        )
        self.manager_b = PharmacyUser.objects.create_user(
            email="manager@ctg-care.com",
            phone_number="+8801712000003",
            full_name="CTG Manager",
            password="StrongPassword123!",
            tenant_id=self.tenant_b.tenant_id,
            role=UserRole.BRANCH_MANAGER,
        )

        # 4. Supplier
        self.supplier = Supplier.objects.create(
            supplier_id="sup-sq-log",
            tenant=self.tenant_a,
            name="Square Pharmaceuticals Logistics Division",
            code="SQUARE-LOG",
            lead_time_days=2,
            payment_terms_days=30,
            is_active=True,
        )

        # 5. Purchase Order
        self.po = PurchaseOrder.objects.create(
            po_id="po-log-001",
            po_number="PO-202611-001",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.supplier,
            status=PurchaseOrderStatus.ORDERED,
            subtotal_amount=Decimal("50000.0000"),
            total_amount=Decimal("50000.0000"),
            expected_delivery_date=datetime.date(2026, 11, 10),
            is_active=True,
        )

    def test_01_create_shipment_with_carrier_and_vehicle_metadata(self):
        """Creates an outbound procurement shipment with carrier, vehicle, driver, and ETA."""
        self.client.force_authenticate(user=self.manager)
        eta = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)).isoformat()

        payload = {
            "purchase_order_id": self.po.po_id,
            "carrier_type": "SUPPLIER_FLEET",
            "carrier_name": "Square Dedicated Reefer Fleet",
            "consignment_number": "SQ-WB-998811",
            "vehicle_registration_number": "Dhaka Metro-U-14-5542",
            "driver_name": "Rafiqul Islam",
            "driver_phone": "+8801719888777",
            "estimated_delivery_at": eta,
            "total_packages_dispatched": 25,
            "requires_cold_chain": True,
            "transit_temp_celsius": "4.50",
            "notes": "Insulin and vaccine replenishment",
        }

        resp = self.client.post("/api/v2/procurement/shipments/", data=payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()

        assert data["tracking_number"].startswith("SHP-")
        assert data["stage"] == "DISPATCHED"
        assert data["carrier_name"] == "Square Dedicated Reefer Fleet"
        assert data["total_packages_dispatched"] == 25
        assert data["requires_cold_chain"] is True
        assert Decimal(data["transit_temp_celsius"]) == Decimal("4.50")
        assert data["cold_chain_breached"] is False
        assert len(data["events"]) == 1
        assert data["events"][0]["stage"] == "DISPATCHED"

    def test_02_update_shipment_stages_and_timeline(self):
        """Transitions shipment through in-transit checkpoints to delivered status."""
        self.client.force_authenticate(user=self.manager)
        eta = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).isoformat()
        create_resp = self.client.post(
            "/api/v2/procurement/shipments/",
            data={
                "purchase_order_id": self.po.po_id,
                "carrier_name": "SA Paribahan Cargo",
                "estimated_delivery_at": eta,
                "total_packages_dispatched": 10,
            },
            format="json",
        )
        shp_id = create_resp.json()["shipment_id"]

        # 1. Update to IN_TRANSIT
        resp1 = self.client.post(
            f"/api/v2/procurement/shipments/{shp_id}/update-stage/",
            data={"stage": "IN_TRANSIT", "location_name": "Tejgaon Distribution Hub", "notes": "Loaded on truck"},
            format="json",
        )
        assert resp1.status_code == status.HTTP_200_OK
        assert resp1.json()["stage"] == "IN_TRANSIT"

        # 2. Update to OUT_FOR_DELIVERY
        resp2 = self.client.post(
            f"/api/v2/procurement/shipments/{shp_id}/update-stage/",
            data={"stage": "OUT_FOR_DELIVERY", "location_name": "Dhanmondi Area Dispatch", "notes": "Approaching pharmacy"},
            format="json",
        )
        assert resp2.status_code == status.HTTP_200_OK
        assert resp2.json()["stage"] == "OUT_FOR_DELIVERY"

        # 3. Update to DELIVERED
        resp3 = self.client.post(
            f"/api/v2/procurement/shipments/{shp_id}/update-stage/",
            data={"stage": "DELIVERED", "location_name": "Dhanmondi Branch Gate", "notes": "Gate drop-off verified"},
            format="json",
        )
        assert resp3.status_code == status.HTTP_200_OK
        data3 = resp3.json()
        assert data3["stage"] == "DELIVERED"
        assert data3["actual_delivery_at"] is not None
        assert len(data3["events"]) == 4

    def test_03_in_transit_cold_chain_breach_detection(self):
        """Alerts when cold-chain cargo exceeds 8°C during transit."""
        self.client.force_authenticate(user=self.manager)
        eta = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).isoformat()
        create_resp = self.client.post(
            "/api/v2/procurement/shipments/",
            data={
                "purchase_order_id": self.po.po_id,
                "carrier_name": "Reefer Express",
                "estimated_delivery_at": eta,
                "requires_cold_chain": True,
                "transit_temp_celsius": "5.00",
            },
            format="json",
        )
        shp_id = create_resp.json()["shipment_id"]

        # Temperature spike to 12.5°C during transit
        resp = self.client.post(
            f"/api/v2/procurement/shipments/{shp_id}/update-stage/",
            data={
                "stage": "IN_TRANSIT",
                "location_name": "Highway Sensor Log",
                "transit_temp_celsius": "12.50",
                "notes": "Cooling compressor malfunction",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert Decimal(data["transit_temp_celsius"]) == Decimal("12.50")
        assert data["cold_chain_breached"] is True

    def test_04_confirm_branch_receiving_no_discrepancy(self):
        """Branch confirms receipt of 100% intact packages."""
        self.client.force_authenticate(user=self.manager)
        eta = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).isoformat()
        create_resp = self.client.post(
            "/api/v2/procurement/shipments/",
            data={
                "purchase_order_id": self.po.po_id,
                "carrier_name": "Square Fleet",
                "estimated_delivery_at": eta,
                "total_packages_dispatched": 20,
            },
            format="json",
        )
        shp_id = create_resp.json()["shipment_id"]

        confirm_payload = {
            "total_packages_received": 20,
            "damaged_packages": 0,
            "missing_packages": 0,
            "discrepancy_notes": "All cartons in perfect sealed condition",
        }
        resp = self.client.post(
            f"/api/v2/procurement/shipments/{shp_id}/confirm-receiving/",
            data=confirm_payload,
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["stage"] == "RECEIVING_CONFIRMED"
        assert data["total_packages_received"] == 20
        assert data["damaged_packages"] == 0
        assert data["missing_packages"] == 0
        assert data["discrepancy_type"] == "NONE"
        assert data["confirmed_by"] is not None
        assert data["confirmed_at"] is not None

    def test_05_confirm_branch_receiving_with_discrepancy(self):
        """Documents shortage and transit damages during receiving verification."""
        self.client.force_authenticate(user=self.manager)
        eta = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).isoformat()
        create_resp = self.client.post(
            "/api/v2/procurement/shipments/",
            data={
                "purchase_order_id": self.po.po_id,
                "carrier_name": "Square Fleet",
                "estimated_delivery_at": eta,
                "total_packages_dispatched": 50,
            },
            format="json",
        )
        shp_id = create_resp.json()["shipment_id"]

        confirm_payload = {
            "total_packages_received": 45,
            "damaged_packages": 2,
            "missing_packages": 5,
            "discrepancy_notes": "2 boxes crushed during transit, 5 boxes missing from pallet",
        }
        resp = self.client.post(
            f"/api/v2/procurement/shipments/{shp_id}/confirm-receiving/",
            data=confirm_payload,
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["stage"] == "RECEIVING_CONFIRMED"
        assert data["total_packages_received"] == 45
        assert data["damaged_packages"] == 2
        assert data["missing_packages"] == 5
        assert data["discrepancy_type"] in ("DAMAGED", "SHORTAGE")

    def test_06_cannot_modify_already_confirmed_shipment(self):
        """Prevents tampering or stage updates on already confirmed receipts."""
        self.client.force_authenticate(user=self.manager)
        eta = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).isoformat()
        create_resp = self.client.post(
            "/api/v2/procurement/shipments/",
            data={
                "purchase_order_id": self.po.po_id,
                "carrier_name": "Square Fleet",
                "estimated_delivery_at": eta,
                "total_packages_dispatched": 10,
            },
            format="json",
        )
        shp_id = create_resp.json()["shipment_id"]

        # Confirm receiving
        self.client.post(
            f"/api/v2/procurement/shipments/{shp_id}/confirm-receiving/",
            data={"total_packages_received": 10},
            format="json",
        )

        # Attempt to re-update stage fails
        resp = self.client.post(
            f"/api/v2/procurement/shipments/{shp_id}/update-stage/",
            data={"stage": "IN_TRANSIT"},
            format="json",
        )
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert "ERR_SHIPMENT_ALREADY_CONFIRMED" in str(resp.json())

    def test_07_rbac_cashier_denied(self):
        """Cashier role is barred from logistics creation and receiving confirmation."""
        self.client.force_authenticate(user=self.cashier)

        resp = self.client.post(
            "/api/v2/procurement/shipments/",
            data={
                "purchase_order_id": self.po.po_id,
                "carrier_name": "Unauthorized Attempt",
                "estimated_delivery_at": "2026-11-12T12:00:00Z",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.json().get("error_code") in ("ERR_STAFF_ROLE_DENIED", "ERR_PERMISSION_DENIED")

    def test_08_cross_tenant_isolation(self):
        """Tenant B cannot access or update Tenant A's shipment data."""
        self.client.force_authenticate(user=self.manager)
        eta = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).isoformat()
        create_resp = self.client.post(
            "/api/v2/procurement/shipments/",
            data={
                "purchase_order_id": self.po.po_id,
                "carrier_name": "Square Fleet",
                "estimated_delivery_at": eta,
                "total_packages_dispatched": 10,
            },
            format="json",
        )
        shp_id = create_resp.json()["shipment_id"]

        # Authenticate as Tenant B
        self.client.force_authenticate(user=self.manager_b)
        resp = self.client.get(f"/api/v2/procurement/shipments/{shp_id}/")
        assert resp.status_code == status.HTTP_404_NOT_FOUND
