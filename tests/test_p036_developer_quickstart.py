"""
tests/test_p036_developer_quickstart.py
Architecture Decision: ADR-0036
Phase 02: Repository, Toolchain & Engineering Automation

Backend Test Suite for Developer Quickstart Orchestration, Multi-Client Preflight,
Zero-PHI Seed Automation, and RBAC Security Boundaries.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Add project root and backend to sys.path
_CANDIDATE = Path(__file__).resolve().parent.parent
_ROOT_DIR = _CANDIDATE.parent if (_CANDIDATE.parent / "backend").exists() else _CANDIDATE
_BACKEND_DIR = _ROOT_DIR / "backend"
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
if str(_ROOT_DIR / "shared") not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR / "shared"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

try:
    django.setup()
except RuntimeError:
    pass

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.developer_toolchain.engine import (
    DeveloperQuickstartEngine,
    QuickstartValidationError,
)
from apps.developer_toolchain.models import DeveloperBootstrapAudit


class DeveloperQuickstartTestSuite(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.engine = DeveloperQuickstartEngine(root_dir=_ROOT_DIR)
        DeveloperBootstrapAudit.objects.all().delete()

    def tearDown(self):
        DeveloperBootstrapAudit.objects.all().delete()

    def test_valid_scenario_quickstart_engine_execution(self):
        """Valid Scenario: Verifies all 6 mandatory pillars pass and produce valid structured preflight report."""
        report = self.engine.execute_all(dry_run=True)

        self.assertEqual(report["status"], "DRY_RUN")
        self.assertTrue(report["all_passed"])
        self.assertIn("timestamp", report)

        env = report["environment"]
        self.assertTrue(env["python_version"].startswith("3.13"))
        self.assertTrue(env["django_version"].startswith("6."))
        self.assertIn("database_engine", env)

        pillars = report["pillars"]
        required_pillars = {"migration", "seeding", "api_docs", "desktop", "mobile", "tests"}
        self.assertEqual(set(pillars.keys()), required_pillars)

        for p_name, p_data in pillars.items():
            self.assertIn(p_data["status"], {"SUCCESS", "DRY_RUN"}, f"Pillar '{p_name}' failed.")

    def test_invalid_tenant_specification_rejected(self):
        """Invalid Inputs: Requesting seed bootstrap for an unknown tenant raises QuickstartValidationError."""
        with self.assertRaises(QuickstartValidationError) as ctx:
            self.engine.seed_fixtures(dry_run=True, tenant_id="tenant-unauthorized-rogue")

        self.assertIn("Invalid or unauthorized tenant", str(ctx.exception))

    def test_invalid_api_trigger_action_rejected(self):
        """Invalid Action: Triggering an unsupported action via REST API returns HTTP 400 Bad Request."""
        response = self.client.post(
            "/api/v2/developer/quickstart/trigger/",
            {"action": "DROP_DATABASE_ILLEGAL"},
            format="json",
            HTTP_X_USER_ROLE="ADMIN",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("action", response.data)

    def test_unauthorized_cashier_role_rejected(self):
        """RBAC Boundary: Cashier and Dispenser roles are blocked from accessing developer quickstart (HTTP 403)."""
        response_cashier = self.client.get(
            "/api/v2/developer/quickstart/",
            HTTP_X_USER_ROLE="CASHIER",
        )
        self.assertEqual(response_cashier.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Access denied", str(response_cashier.data))

        response_dispenser = self.client.post(
            "/api/v2/developer/quickstart/trigger/",
            {"action": "VERIFY_ALL"},
            format="json",
            HTTP_X_USER_ROLE="DISPENSER",
        )
        self.assertEqual(response_dispenser.status_code, status.HTTP_403_FORBIDDEN)

    def test_cross_tenant_trigger_strictly_blocked(self):
        """Cross-Tenant Boundary: Pharmacy owner from tenant A cannot trigger quickstart for tenant B (HTTP 403)."""
        response = self.client.post(
            "/api/v2/developer/quickstart/trigger/",
            {"action": "SEED", "tenant_id": "tenant-chittagong-care"},
            format="json",
            HTTP_X_TENANT_ID="tenant-dhaka-central",
            HTTP_X_USER_ROLE="PHARMACY_OWNER",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Cross-tenant access forbidden", str(response.data))

    def test_source_specific_rule_six_pillars_verification(self):
        """Source-Specific Rule: Verifies individual pillar methods execute with correct metadata."""
        mig = self.engine.run_migrations(dry_run=True)
        self.assertEqual(mig["status"], "DRY_RUN")

        seed = self.engine.seed_fixtures(dry_run=True, tenant_id="tenant-dhaka-central")
        self.assertEqual(seed["status"], "DRY_RUN")
        self.assertEqual(seed["target_tenant"], "tenant-dhaka-central")

        docs = self.engine.verify_api_docs()
        self.assertEqual(docs["status"], "SUCCESS")
        self.assertEqual(docs["swagger_ui_route"], "/api/docs/")

        desktop = self.engine.verify_desktop_workspace(dry_run=True)
        self.assertEqual(desktop["status"], "SUCCESS")
        self.assertEqual(desktop["toolchain"], "Gradle Wrapper")

        mobile = self.engine.verify_mobile_workspace(dry_run=True)
        self.assertEqual(mobile["status"], "SUCCESS")
        self.assertEqual(mobile["package_name"], "pharmacy_mobile_manager")

        tests = self.engine.run_fast_tests(dry_run=True)
        self.assertEqual(tests["status"], "DRY_RUN")
        self.assertGreater(tests["suites_count"], 0)

    def test_quickstart_rest_endpoints(self):
        """REST API: Validates GET diagnostics, POST trigger, and audit retrieval."""
        get_res = self.client.get(
            "/api/v2/developer/quickstart/",
            HTTP_X_USER_ROLE="ADMIN",
        )
        self.assertEqual(get_res.status_code, status.HTTP_200_OK)
        self.assertTrue(get_res.data["all_passed"])

        post_res = self.client.post(
            "/api/v2/developer/quickstart/trigger/",
            {"action": "DRY_RUN", "dry_run": True},
            format="json",
            HTTP_X_USER_ROLE="ADMIN",
        )
        self.assertEqual(post_res.status_code, status.HTTP_200_OK)
        self.assertEqual(post_res.data["status"], "DRY_RUN")

        audit_res = self.client.get(
            "/api/v2/developer/quickstart/audits/",
            HTTP_X_USER_ROLE="ADMIN",
        )
        self.assertEqual(audit_res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(audit_res.data), 1)
        self.assertEqual(audit_res.data[0]["operator_role"], "ADMIN")

    def test_cli_tool_execution(self):
        """CLI Tool: quickstart.py and shared/tools/quickstart.py execute with exit code 0."""
        root_script = _ROOT_DIR / "quickstart.py"
        self.assertTrue(root_script.exists())
        proc1 = subprocess.run(
            [sys.executable, str(root_script), "--dry-run"],
            capture_output=True,
            text=True,
            cwd=str(_ROOT_DIR),
        )
        self.assertEqual(proc1.returncode, 0, f"Root quickstart failed: {proc1.stderr}")
        self.assertIn("RESULT: SUCCESS", proc1.stdout)

        shared_tool = _ROOT_DIR / "shared" / "tools" / "quickstart.py"
        self.assertTrue(shared_tool.exists())
        proc2 = subprocess.run(
            [sys.executable, str(shared_tool), "--verify-all", "--dry-run"],
            capture_output=True,
            text=True,
            cwd=str(_ROOT_DIR),
        )
        self.assertEqual(proc2.returncode, 0, f"Shared quickstart failed: {proc2.stderr}")
        self.assertIn("RESULT: SUCCESS", proc2.stdout)

    def test_adr_0036_decision_record_parity(self):
        """Documentation: Ensures ADR-0036 decision record exists and contains required specifications."""
        adr_path = (
            _ROOT_DIR
            / "shared"
            / "docs"
            / "architecture"
            / "decisions"
            / "ADR-0036-developer-quickstart-orchestration.md"
        )
        self.assertTrue(adr_path.exists(), "ADR-0036 documentation file must exist.")

        content = adr_path.read_text(encoding="utf-8")
        self.assertIn("Status", content)
        self.assertIn("Accepted", content)
        self.assertIn("Six Mandatory Quickstart Pillars", content)
        self.assertIn("Security & Tenant Scoping Boundaries", content)
        self.assertIn("One-Command Developer Onboarding", content)
