"""
backend/tests/test_p048_rate_limiting.py
Architecture Decision: ADR-0048
Unit and integration tests for multi-tiered rate limiting, 429 envelopes, and Retry-After headers.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.core.cache import cache
from django.test import Client, TestCase


class TestP048RateLimiting(TestCase):
    """
    Validates Rate Limiting and Throttling under ADR-0048:
    - Policy matrix introspection
    - Sub-threshold requests succeed (200 OK)
    - Invalid input rejected with 422 before throttle exhaustion
    - Rate limit exceeded returns 429 ERR_RATE_LIMIT_EXCEEDED with Retry-After header
    - Sensitive mutation throttling
    - Data export throttling
    - Public invitation throttling
    - Cross-tenant boundaries (403 ERR_CROSS_TENANT_VIOLATION)
    """

    def setUp(self):
        self.client = Client()
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_01_rate_limit_policy_introspection(self):
        """GET /api/v2/rate-limits/policy/ returns active throttle scopes and rates."""
        res = self.client.get("/api/v2/rate-limits/policy/")
        self.assertEqual(res.status_code, 200)
        body = res.json()

        self.assertTrue(body["success"])
        self.assertEqual(body["code"], "RATE_LIMIT_POLICY_RETRIEVED")
        policies = body.get("data", {}).get("policies", [])
        scopes = {p["scope"] for p in policies}

        self.assertIn("anon", scopes)
        self.assertIn("user", scopes)
        self.assertIn("auth_login", scopes)
        self.assertIn("password_reset", scopes)
        self.assertIn("sensitive_mutation", scopes)
        self.assertIn("data_export", scopes)
        self.assertIn("public_invitation", scopes)

    def test_02_sub_threshold_requests_succeed(self):
        """Valid scenario: Sub-threshold requests pass without throttling."""
        res1 = self.client.post("/api/v2/rate-limits/test-auth-throttle/", HTTP_X_DEVICE_ID="dev-unit-01")
        res2 = self.client.post("/api/v2/rate-limits/test-auth-throttle/", HTTP_X_DEVICE_ID="dev-unit-01")

        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res1.json()["code"], "AUTH_THROTTLE_PASS")

    def test_03_invalid_input_case_returns_422(self):
        """Invalid input rejected with 422 ERR_VALIDATION_FAILED without consuming entire bucket."""
        res = self.client.post(
            "/api/v2/conventions/validate-input/",
            data=json.dumps({"item_name": "", "quantity": -1, "unit_price": "-5.00"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 422)
        body = res.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error_code"], "ERR_VALIDATION_FAILED")
        self.assertIn("item_name", body["field_errors"])

    def test_04_rate_limit_exceeded_returns_429_envelope(self):
        """Exceeding 3/min test throttle returns 429 with ERR_RATE_LIMIT_EXCEEDED and Retry-After."""
        device = "dev-unit-throttle"
        self.client.post("/api/v2/rate-limits/test-auth-throttle/", HTTP_X_DEVICE_ID=device)
        self.client.post("/api/v2/rate-limits/test-auth-throttle/", HTTP_X_DEVICE_ID=device)
        self.client.post("/api/v2/rate-limits/test-auth-throttle/", HTTP_X_DEVICE_ID=device)

        # 4th request must be throttled
        res_throttled = self.client.post("/api/v2/rate-limits/test-auth-throttle/", HTTP_X_DEVICE_ID=device)
        self.assertEqual(res_throttled.status_code, 429)

        body = res_throttled.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error_code"], "ERR_RATE_LIMIT_EXCEEDED")
        self.assertEqual(body["code"], "ERR_RATE_LIMIT_EXCEEDED")
        self.assertEqual(body["http_status"], 429)
        self.assertTrue(body["is_retryable"])
        self.assertEqual(body["recovery_action"], "RETRY_AFTER_BACKOFF")
        self.assertIn("Retry-After", res_throttled.headers)
        self.assertIn("wait_seconds", body["details"])
        self.assertIn("অনুরোধের সীমা অতিক্রম করেছে", body["message_bn"])

    def test_05_sensitive_mutation_rate_limit(self):
        """Sensitive mutation endpoint throttled upon exceeding limit."""
        dev = "dev-mut"
        for _ in range(3):
            self.client.post("/api/v2/rate-limits/test-mutation-throttle/", HTTP_X_DEVICE_ID=dev)

        res = self.client.post("/api/v2/rate-limits/test-mutation-throttle/", HTTP_X_DEVICE_ID=dev)
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.json()["error_code"], "ERR_RATE_LIMIT_EXCEEDED")

    def test_06_data_export_rate_limit(self):
        """Bulk data export endpoint throttled upon exceeding limit."""
        dev = "dev-exp"
        for _ in range(3):
            self.client.get("/api/v2/rate-limits/test-export-throttle/", HTTP_X_DEVICE_ID=dev)

        res = self.client.get("/api/v2/rate-limits/test-export-throttle/", HTTP_X_DEVICE_ID=dev)
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.json()["error_code"], "ERR_RATE_LIMIT_EXCEEDED")

    def test_07_public_invitation_rate_limit(self):
        """Public invitation endpoint throttled upon exceeding limit."""
        dev = "dev-inv"
        for _ in range(3):
            self.client.post("/api/v2/rate-limits/test-invitation-throttle/", HTTP_X_DEVICE_ID=dev)

        res = self.client.post("/api/v2/rate-limits/test-invitation-throttle/", HTTP_X_DEVICE_ID=dev)
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.json()["error_code"], "ERR_RATE_LIMIT_EXCEEDED")

    def test_08_cross_tenant_access_returns_403(self):
        """Cross-tenant violation rejected with 403 ERR_CROSS_TENANT_VIOLATION."""
        res = self.client.get(
            "/api/v2/conventions/cross-tenant-access/?caller_tenant_id=tenant-dhaka-central&target_tenant_id=tenant-sylhet-care"
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")
