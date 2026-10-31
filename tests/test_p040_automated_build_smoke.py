"""
backend/tests/test_p040_automated_build_smoke.py
Architecture Decision: ADR-0040
Backend DRF and RBAC Acceptance Tests for Build Smoke Pipeline ViewSets and Models.
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

from apps.build_pipeline.models import (
    BuildSmokeExecutionRecord,
    SmokePipelineStatus,
    SmokeStageName,
    SmokeStageStatus,
)


class BackendTestP040AutomatedBuildSmoke(TestCase):
    """
    Acceptance test suite for DRF endpoints and RBAC boundaries of ADR-0040.
    """

    def setUp(self):
        self.client = Client()
        self.tenant_a = "tenant-gulshan-pharma"
        self.tenant_b = "tenant-uttara-med"

        self.record_a = BuildSmokeExecutionRecord.objects.create(
            tenant_id=self.tenant_a,
            triggered_by="CI_SUITE",
            git_commit_hash="abcdef0123456789abcdef0123456789abcdef01",
            git_branch="master",
            is_clean_checkout=True,
            status=SmokePipelineStatus.SUCCESS,
            total_duration_seconds=0.450,
            stages=[
                {
                    "stage_name": SmokeStageName.WORKSPACE_INSPECTION,
                    "status": SmokeStageStatus.PASSED,
                    "duration_ms": 10,
                    "error_message": None,
                }
            ],
        )

    def test_valid_scenario_trigger_pipeline(self):
        """Verifies authorized DevOps/Admin user can trigger build smoke pipeline."""
        res = self.client.post(
            "/api/v2/pipeline/smoke/",
            {
                "tenant_id": self.tenant_a,
                "triggered_by": "DEV_ADMIN",
                "git_commit_hash": "112233445566778899aabbccddeeff0011223344",
                "git_branch": "release-v2.0.0",
            },
            content_type="application/json",
            HTTP_X_TENANT_ID=self.tenant_a,
            HTTP_X_USER_ROLE="ADMIN",
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["status"], "SUCCESS")
        self.assertEqual(data["git_branch"], "release-v2.0.0")
        self.assertEqual(len(data["stages"]), 6)

    def test_invalid_input_malformed_git_hash_rejected(self):
        """Verifies malformed git_commit_hash returns HTTP 400 Bad Request."""
        res = self.client.post(
            "/api/v2/pipeline/smoke/",
            {
                "tenant_id": self.tenant_a,
                "git_commit_hash": "not-valid-hex!@#$",
            },
            content_type="application/json",
            HTTP_X_TENANT_ID=self.tenant_a,
            HTTP_X_USER_ROLE="ADMIN",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("git_commit_hash", res.json())

    def test_invalid_input_unknown_stage_name_rejected(self):
        """Verifies invalid fail_at_stage parameter returns HTTP 400 Bad Request."""
        res = self.client.post(
            "/api/v2/pipeline/smoke/",
            {
                "tenant_id": self.tenant_a,
                "fail_at_stage": "NON_EXISTENT_SMOKE_STAGE",
            },
            content_type="application/json",
            HTTP_X_TENANT_ID=self.tenant_a,
            HTTP_X_USER_ROLE="ADMIN",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("fail_at_stage", res.json())

    def test_unauthorized_cashier_role_forbidden(self):
        """Verifies Cashier role is rejected with HTTP 403 Forbidden."""
        res = self.client.post(
            "/api/v2/pipeline/smoke/",
            {"tenant_id": self.tenant_a},
            content_type="application/json",
            HTTP_X_TENANT_ID=self.tenant_a,
            HTTP_X_USER_ROLE="CASHIER",
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("Cashiers are not authorized", res.json().get("detail", ""))

    def test_cross_tenant_access_forbidden(self):
        """Verifies Tenant B cannot view or retrieve Tenant A's smoke pipeline record."""
        # Tenant B tries to retrieve Tenant A's record directly
        res = self.client.get(
            f"/api/v2/pipeline/smoke/{self.record_a.pipeline_id}/",
            HTTP_X_TENANT_ID=self.tenant_b,
            HTTP_X_USER_ROLE="ADMIN",
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("Cross-tenant access forbidden", res.json().get("detail", ""))

    def test_latest_endpoint_retrieves_most_recent_run(self):
        """Verifies /api/v2/pipeline/smoke/latest/ endpoint returns tenant's newest run."""
        res = self.client.get(
            "/api/v2/pipeline/smoke/latest/",
            HTTP_X_TENANT_ID=self.tenant_a,
            HTTP_X_USER_ROLE="ADMIN",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["pipeline_id"], str(self.record_a.pipeline_id))
        self.assertEqual(data["tenant_id"], self.tenant_a)


if __name__ == "__main__":
    unittest.main()
