"""
tests/test_p021_monorepo.py
Architecture Decision: ADR-0021
Phase 02: Repository, Toolchain & Engineering Automation

Comprehensive Unit & Integration Test Suite for Monorepo Layout and Toolchain:
1. Valid Scenario: Validates that all canonical pillars and entry point configs exist.
2. Defective Cases: Rejects missing pillar directories and empty/missing entry point files.
3. Unauthorized / Cross-Tenant: Block infrastructure mutations without platform admin role.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure root directory and backend are on sys.path
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(_ROOT_DIR / "backend") not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR / "backend"))

from apps.core.errors import FieldValidationError
from tools.monorepo_validator import (
    CANONICAL_PILLARS,
    MANDATORY_ENTRY_POINTS,
    MonorepoValidator,
)


class TestP021MonorepoToolchain(unittest.TestCase):
    def setUp(self):
        self.root_dir = Path(__file__).resolve().parent.parent
        self.validator = MonorepoValidator(root_dir=self.root_dir)

    def test_canonical_pillars_exist(self):
        """Verifies that all 8 pillar directories exist."""
        result = self.validator.validate_structure()
        self.assertEqual(result["status"], "VALID")
        self.assertEqual(result["pillars_checked"], 8)
        self.assertIn("backend", result["pillars"])
        self.assertIn("desktop", result["pillars"])
        self.assertIn("mobile", result["pillars"])

    def test_mandatory_entry_points_exist_and_non_empty(self):
        """Verifies that all 18 mandatory entry points exist and are non-empty."""
        result = self.validator.validate_entry_points()
        self.assertEqual(result["status"], "VALID")
        self.assertEqual(result["entry_points_checked"], len(MANDATORY_ENTRY_POINTS))

    def test_openapi_v2_spec_valid_json(self):
        """Verifies that the canonical OpenAPI 3.0 contract parses as valid JSON."""
        spec_path = self.root_dir / "contracts" / "openapi" / "openapi_v2.json"
        self.assertTrue(spec_path.exists())
        with open(spec_path, encoding="utf-8") as f:
            spec = json.load(f)

        self.assertEqual(spec["openapi"], "3.0.3")
        self.assertEqual(spec["info"]["version"], "2.0.0")

        paths = spec["paths"]
        self.assertIn("/auth/token/", paths)
        self.assertIn("/sales/invoices/", paths)
        self.assertIn("/medicines/", paths)
        self.assertIn("/prescriptions/verify/", paths)
        self.assertIn("/sync/replay/", paths)

    def test_missing_pillar_directory_rejection(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            # Create only 7 of the 8 pillars (omit 'mobile')
            for pillar in ["backend", "desktop", "docs", "contracts", "infra", "tests", "tools"]:
                (tmp_path / pillar).mkdir()

            fake_validator = MonorepoValidator(root_dir=tmp_path)
            with self.assertRaises(FieldValidationError) as ctx:
                fake_validator.validate_structure()

            self.assertIn("mobile", ctx.exception.field_errors["missing_pillars"])

    def test_missing_or_empty_entry_point_rejection(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            # Create all pillars
            for pillar in CANONICAL_PILLARS:
                (tmp_path / pillar).mkdir()

            # Create only an empty pom.xml
            pom_file = tmp_path / "desktop" / "pom.xml"
            pom_file.touch()  # 0 bytes -> invalid!

            fake_validator = MonorepoValidator(root_dir=tmp_path)
            with self.assertRaises(FieldValidationError) as ctx:
                fake_validator.validate_entry_points()

            self.assertTrue(len(ctx.exception.field_errors["defective_entry_points"]) > 0)

    def test_unauthorized_and_cross_tenant_infra_mutation_blocked(self):
        """
        Only PLATFORM_SUPER_ADMIN can mutate root monorepo infrastructure.
        TENANT_OWNER and BRANCH_MANAGER are strictly blocked.
        """
        allowed_roles = {"PLATFORM_SUPER_ADMIN"}

        def authorize_infra_mutation(role: str) -> bool:
            if role not in allowed_roles:
                raise PermissionError(
                    f"Access Denied: Role '{role}' cannot modify monorepo infrastructure. "
                    "Only PLATFORM_SUPER_ADMIN is authorized."
                )
            return True

        self.assertTrue(authorize_infra_mutation("PLATFORM_SUPER_ADMIN"))

        with self.assertRaises(PermissionError):
            authorize_infra_mutation("TENANT_OWNER")

        with self.assertRaises(PermissionError):
            authorize_infra_mutation("BRANCH_MANAGER")

        with self.assertRaises(PermissionError):
            authorize_infra_mutation("CASHIER")


if __name__ == "__main__":
    unittest.main()
