"""
backend/tests/test_p047_api_response_conventions.py
Architecture Decision: ADR-0047
Comprehensive unit tests for DRF pagination, filtering, success/error envelopes, machine codes, and request IDs.
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.test import Client, TestCase

from apps.core.models import MedicineProduct


class TestP047ApiResponseConventions(TestCase):
    """
    Validates API Response Conventions under ADR-0047:
    - Paginated envelope with metadata and request correlation
    - Query parameter filtering and full-text search
    - Standard single object retrieval envelope
    - 422 ERR_VALIDATION_FAILED machine error envelope
    - 401 ERR_UNAUTHORIZED authentication failure envelope
    - 403 ERR_CROSS_TENANT_VIOLATION cross-tenant boundary envelope
    - 404 ERR_RESOURCE_NOT_FOUND resource missing envelope
    - Backward-compatible unpaginated list fallback
    """

    def setUp(self):
        self.client = Client()
        # Clean existing test medicines to maintain deterministic counts
        MedicineProduct.objects.all().delete()
        # Seed test medicines
        self.med1 = MedicineProduct.objects.create(
            product_id="TEST-MED-01",
            brand_name="Napa Extra 500mg",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg+65mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("25.00"),
            cost_unit=Decimal("20.00"),
        )
        self.med2 = MedicineProduct.objects.create(
            product_id="TEST-MED-02",
            brand_name="Seclo 20mg",
            generic_name="Omeprazole",
            dosage_form="Capsule",
            strength="20mg",
            manufacturer="Square Pharmaceuticals Ltd.",
            mrp_unit=Decimal("80.00"),
            cost_unit=Decimal("65.00"),
        )
        self.med3 = MedicineProduct.objects.create(
            product_id="TEST-MED-03",
            brand_name="Ace 500mg",
            generic_name="Paracetamol",
            dosage_form="Tablet",
            strength="500mg",
            manufacturer="Square Pharmaceuticals Ltd.",
            mrp_unit=Decimal("12.00"),
            cost_unit=Decimal("10.00"),
        )

    def test_01_valid_paginated_envelope_structure(self):
        """Valid scenario: GET /api/v2/conventions/paginated-medicines/ returns canonical ADR-0047 envelope."""
        res = self.client.get(
            "/api/v2/conventions/paginated-medicines/?page=1&page_size=2",
            HTTP_X_REQUEST_ID="req-test-p047-01",
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()

        self.assertTrue(body["success"])
        self.assertEqual(body["code"], "PAGINATED_RESULTS")
        self.assertIn("req-test-p047-01", body["request_id"])
        self.assertIn("data", body)
        self.assertEqual(len(body["data"]), 2)

        meta = body.get("meta", {}).get("pagination", {})
        self.assertEqual(meta["page"], 1)
        self.assertEqual(meta["page_size"], 2)
        self.assertEqual(meta["total_count"], 3)
        self.assertEqual(meta["total_pages"], 2)
        self.assertTrue(meta["has_next"])
        self.assertFalse(meta["has_previous"])
        self.assertEqual(meta["next_page"], 2)
        self.assertIsNone(meta["previous_page"])

        self.assertIn("message_en", body)
        self.assertIn("message_bn", body)

    def test_02_filtering_and_full_text_search(self):
        """Demonstrates query filtering with keyword search and field ordering."""
        res = self.client.get("/api/v2/conventions/paginated-medicines/?search=Paracetamol&ordering=mrp_unit")
        self.assertEqual(res.status_code, 200)
        body = res.json()

        items = body.get("data", [])
        self.assertEqual(len(items), 2)  # Napa Extra and Ace
        for item in items:
            self.assertIn("Paracetamol", item["generic_name"])

        # Validate sorting by mrp_unit ascending: Ace (12.00) before Napa Extra (25.00)
        self.assertEqual(items[0]["brand_name"], "Ace 500mg")
        self.assertEqual(items[1]["brand_name"], "Napa Extra 500mg")

    def test_03_valid_single_detail_envelope(self):
        """Single resource fetch returns ADR-0047 success envelope with machine code RESOURCE_FETCHED."""
        res = self.client.get(f"/api/v2/conventions/medicine-detail/{self.med1.pk}/")
        self.assertEqual(res.status_code, 200)
        body = res.json()

        self.assertTrue(body["success"])
        self.assertEqual(body["code"], "RESOURCE_FETCHED")
        self.assertEqual(body["data"]["product_id"], self.med1.pk)
        self.assertEqual(body["data"]["brand_name"], "Napa Extra 500mg")
        self.assertEqual(body["data"]["mrp_unit"], "25.00")

    def test_04_invalid_input_case_returns_422_envelope(self):
        """Invalid or incomplete input case: POST /api/v2/conventions/validate-input/ returns 422 ERR_VALIDATION_FAILED."""
        payload = {"item_name": "", "quantity": 0, "unit_price": "-1.00"}
        res = self.client.post(
            "/api/v2/conventions/validate-input/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_REQUEST_ID="req-test-err-422",
        )
        self.assertEqual(res.status_code, 422)
        body = res.json()

        self.assertFalse(body["success"])
        self.assertEqual(body["error_code"], "ERR_VALIDATION_FAILED")
        self.assertEqual(body["code"], "ERR_VALIDATION_FAILED")
        self.assertEqual(body["http_status"], 422)
        self.assertEqual(body["recovery_action"], "CORRECT_INPUT_FIELDS")
        self.assertIn("item_name", body["field_errors"])
        self.assertIn("quantity", body["field_errors"])
        self.assertIn("unit_price", body["field_errors"])
        self.assertIn("req-test-err-422", body["request_id"])

    def test_05_unauthorized_access_returns_401_envelope(self):
        """Unauthorized case: Unauthenticated request returns 401 ERR_UNAUTHORIZED envelope."""
        res = self.client.get("/api/v2/conventions/unauthorized-access/")
        self.assertEqual(res.status_code, 401)
        body = res.json()

        self.assertFalse(body["success"])
        self.assertEqual(body["error_code"], "ERR_UNAUTHORIZED")
        self.assertEqual(body["http_status"], 401)
        self.assertEqual(body["recovery_action"], "REAUTHENTICATE")
        self.assertTrue(len(body["request_id"]) > 0)
        self.assertIn("প্রবেশাধিকার", body["message_bn"])

    def test_06_cross_tenant_access_returns_403_envelope(self):
        """Cross-tenant case: Foreign tenant access returns 403 ERR_CROSS_TENANT_VIOLATION envelope."""
        res = self.client.get(
            "/api/v2/conventions/cross-tenant-access/?caller_tenant_id=tenant-dhaka-central&target_tenant_id=tenant-sylhet-care"
        )
        self.assertEqual(res.status_code, 403)
        body = res.json()

        self.assertFalse(body["success"])
        self.assertEqual(body["error_code"], "ERR_CROSS_TENANT_VIOLATION")
        self.assertEqual(body["http_status"], 403)
        self.assertEqual(body["recovery_action"], "ABORT_SECURITY_INCIDENT")
        self.assertIn("টাস্ক", body["message_bn"]) if "টাস্ক" in body["message_bn"] else self.assertIn(
            "টেন্যান্ট", body["message_bn"]
        )

    def test_07_resource_not_found_returns_404_envelope(self):
        """Missing resource returns 404 ERR_RESOURCE_NOT_FOUND envelope."""
        res = self.client.get("/api/v2/conventions/medicine-detail/NON-EXISTENT-ID/")
        self.assertEqual(res.status_code, 404)
        body = res.json()

        self.assertFalse(body["success"])
        self.assertEqual(body["error_code"], "ERR_RESOURCE_NOT_FOUND")
        self.assertEqual(body["http_status"], 404)

    def test_08_backward_compatibility_unpaginated_list(self):
        """Unpaginated list requests return backward-compatible array without envelope wrapping."""
        res = self.client.get("/api/v2/medicines/")
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.data, list)
        self.assertEqual(len(res.data), 3)

        # But passing ?page=1 activates the standard envelope
        res_paginated = self.client.get("/api/v2/medicines/?page=1&page_size=2")
        self.assertEqual(res_paginated.status_code, 200)
        self.assertTrue(res_paginated.json()["success"])
        self.assertEqual(res_paginated.json()["code"], "PAGINATED_RESULTS")
