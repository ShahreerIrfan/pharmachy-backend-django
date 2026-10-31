"""
backend/tests/test_p039_issue_templates_and_dod.py
Architecture Decision: ADR-0039
Backend Parity Acceptance Test Suite for Issue Templates and Definition of Done.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.test import Client, TestCase

from apps.quality_governance.engine import DefinitionOfDoneEngine
from apps.quality_governance.models import (
    IssueCategory,
    IssueSeverity,
    QualityIssueTicket,
)


class BackendTestP039IssueTemplatesAndDoD(TestCase):
    """Verifies acceptance criteria for P039 and ADR-0039 in backend context."""

    def setUp(self):
        self.client = Client()
        self.tenant_a = "tenant-dhaka-central"
        self.tenant_b = "tenant-chittagong-care"

        self.issue_a = QualityIssueTicket.objects.create(
            tenant_id=self.tenant_a,
            title="Cold-chain sensor alert timeout",
            category=IssueCategory.BUG,
            severity=IssueSeverity.HIGH,
            reproduction_steps="1. Unplug sensor simulator. 2. Observe alert.",
            expected_behavior="Alert triggers in 3s",
            observed_behavior="Alert delays 45s",
        )

    def test_valid_scenario_create_issue_ticket(self):
        res = self.client.post(
            "/api/v2/quality/issues/",
            {
                "title": "Antibiotic dispensing modal latency",
                "category": "BUG",
                "severity": "CRITICAL_CLINICAL_SAFETY",
                "reproduction_steps": "1. Scan item. 2. Open BMDC modal. 3. Observe 3s render lag.",
                "expected_behavior": "Modal renders under 200ms",
                "observed_behavior": "Modal lag is 3s",
            },
            content_type="application/json",
            HTTP_X_TENANT_ID=self.tenant_a,
        )
        self.assertEqual(res.status_code, 201)

    def test_invalid_input_bug_missing_repro_rejected(self):
        res = self.client.post(
            "/api/v2/quality/issues/",
            {
                "title": "Broken checkout",
                "category": "BUG",
                "reproduction_steps": "none",
                "expected_behavior": "work",
                "observed_behavior": "fail",
            },
            content_type="application/json",
            HTTP_X_TENANT_ID=self.tenant_a,
        )
        self.assertEqual(res.status_code, 400)

    def test_unauthorized_cashier_blocked_from_dod_signoff(self):
        res = self.client.post(
            "/api/v2/quality/dod-audits/evaluate/",
            {
                "prompt_id": "P039",
                "feature_title": "Test",
                "tenant_id": self.tenant_a,
                "test_command": "pytest",
                "tests_passed": 5,
            },
            content_type="application/json",
            HTTP_X_TENANT_ID=self.tenant_a,
            HTTP_X_USER_ROLE="CASHIER",
        )
        self.assertEqual(res.status_code, 403)

    def test_cross_tenant_access_forbidden(self):
        res = self.client.get(
            f"/api/v2/quality/issues/{self.issue_a.issue_id}/",
            HTTP_X_TENANT_ID=self.tenant_b,
        )
        self.assertEqual(res.status_code, 403)

    def test_source_specific_clinical_flaw_blocks_dod(self):
        res = DefinitionOfDoneEngine.evaluate(
            {
                "has_critical_clinical_safety_flaw": True,
                "tests_passed": 50,
                "tests_failed": 0,
                "traceability_pass": True,
                "security_phi_pass": True,
                "decimal_money_pass": True,
                "fefo_stock_pass": True,
                "migration_safety_pass": True,
                "multiplatform_ui_pass": True,
                "test_evidence_pass": True,
            }
        )
        self.assertEqual(res.status, "BLOCKED")
        self.assertFalse(res.is_approved)


if __name__ == "__main__":
    unittest.main()
