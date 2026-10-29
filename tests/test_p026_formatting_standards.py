"""
tests/test_p026_formatting_standards.py
Architecture Decision: ADR-0026
Phase 02: Repository, Toolchain & Engineering Automation

Verification test suite for code formatting standards, linter enforcement,
EditorConfig compliance, import ordering, and toolchain security boundaries.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from apps.core.formatting_toolchain import (
    DartFormatValidator,
    EditorConfigValidator,
    FormattingSecurityBoundary,
    FormattingValidationError,
    JavaConventionValidator,
    PythonRuffValidator,
)


class TestP026FormattingStandards(unittest.TestCase):
    """Verifies all acceptance criteria for P026 and ADR-0026."""

    def setUp(self):
        self.root_dir = Path(__file__).resolve().parent.parent
        if not (self.root_dir / ".editorconfig").exists() and (self.root_dir.parent / ".editorconfig").exists():
            self.root_dir = self.root_dir.parent
        self.editorconfig_path = (
            (self.root_dir / ".editorconfig")
            if (self.root_dir / ".editorconfig").exists()
            else (self.root_dir.parent / ".editorconfig")
        )

    # 1. EditorConfig Structure & Standards
    def test_editorconfig_specification_valid(self):
        """Root .editorconfig must define root = true, 120 max line length, and all language sections."""
        summary = EditorConfigValidator.validate_editorconfig_file(self.editorconfig_path)
        self.assertTrue(summary.root_defined)
        self.assertEqual(summary.max_line_length, 120)
        self.assertIn("*", summary.sections)
        self.assertIn("*.py", summary.sections)
        self.assertIn("*.java", summary.sections)
        self.assertIn("*.dart", summary.sections)
        self.assertIn("*.{json,json5}", summary.sections)
        self.assertIn("*.{yml,yaml}", summary.sections)

    def test_editorconfig_detects_malformed_syntax(self):
        """Missing root = true or invalid syntax must raise FormattingValidationError."""
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".editorconfig", encoding="utf-8") as f:
            f.write("[*]\ncharset = utf-8\n")
            temp_path = f.name

        try:
            with self.assertRaises(FormattingValidationError) as ctx:
                EditorConfigValidator.validate_editorconfig_file(temp_path)
            self.assertIn("root = true", str(ctx.exception))
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def test_editorconfig_detects_trailing_whitespace_and_missing_newline(self):
        """Conformance checker detects trailing whitespace and missing final newline."""
        with tempfile.NamedTemporaryFile("wb", delete=False, suffix=".py") as f:
            # Trailing space on line 1, no final newline
            f.write(b"def foo(): \n    return 42")
            temp_path = f.name

        try:
            issues = EditorConfigValidator.check_file_conformance(temp_path)
            self.assertTrue(any("Missing final newline" in i for i in issues))
            self.assertTrue(any("trailing whitespace" in i for i in issues))
        finally:
            Path(temp_path).unlink(missing_ok=True)

    # 2. Python Ruff Linter & Formatter
    def test_python_ruff_linter_passes_on_codebase(self):
        """Ruff check on root repository must pass with 0 errors."""
        res = PythonRuffValidator.run_ruff_check(self.root_dir)
        self.assertEqual(res["status"], "PASS")

    # 3. Dart Formatting Check
    def test_dart_formatting_passes_on_mobile(self):
        """dart format on mobile-flutter/lib must pass with 0 unformatted files."""
        mobile_lib = (
            (self.root_dir / "mobile" / "lib")
            if (self.root_dir / "mobile" / "lib").exists()
            else (
                (self.root_dir.parent / "mobile" / "lib")
                if (self.root_dir.parent / "mobile" / "lib").exists()
                else (self.root_dir / "mobile-flutter" / "lib")
            )
        )
        res = DartFormatValidator.run_dart_format_check(mobile_lib)
        self.assertEqual(res["status"], "PASS")

    # 4. Java Conventions & Wildcard Import Prevention
    def test_java_conventions_no_wildcard_imports_in_pos(self):
        """All Java source files in desktop-javafx must avoid wildcard imports."""
        desktop_src = (
            (self.root_dir / "desktop" / "src")
            if (self.root_dir / "desktop" / "src").exists()
            else (
                (self.root_dir.parent / "desktop" / "src")
                if (self.root_dir.parent / "desktop" / "src").exists()
                else (self.root_dir / "desktop-javafx" / "src")
            )
        )
        java_files = list(desktop_src.rglob("*.java"))
        self.assertGreaterEqual(len(java_files), 7)

        for jf in java_files:
            issues = JavaConventionValidator.scan_java_file(jf)
            self.assertEqual(issues, [], f"Java conventions violated in {jf.name}: {issues}")

    def test_java_conventions_detects_wildcard_import(self):
        """Java convention scanner catches simulated wildcard import."""
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".java", encoding="utf-8") as f:
            f.write("package com.test;\nimport java.util.*;\npublic class Foo {}\n")
            temp_path = f.name

        try:
            issues = JavaConventionValidator.scan_java_file(temp_path)
            self.assertTrue(any("forbidden wildcard import" in i for i in issues))
        finally:
            Path(temp_path).unlink(missing_ok=True)

    # 5. Role-Based Access Control on Toolchain Configuration
    def test_formatting_security_boundary_blocks_unauthorized_mutation(self):
        """Cashiers, branch managers, and tenant owners cannot modify formatting rules."""
        unauthorized_roles = ["CASHIER", "BRANCH_MANAGER", "PHARMACIST", "TENANT_OWNER"]
        for role in unauthorized_roles:
            with self.subTest(role=role):
                with self.assertRaises(PermissionError) as ctx:
                    FormattingSecurityBoundary.assert_can_mutate_toolchain_rules(role)
                self.assertIn("Security Violation", str(ctx.exception))

    def test_authorized_admins_can_mutate_toolchain_rules(self):
        """Platform Super Admin and DevOps are authorized to update formatting rules."""
        authorized_roles = ["PLATFORM_SUPER_ADMIN", "DEVOPS_ENGINEER", "LEAD_ARCHITECT"]
        for role in authorized_roles:
            with self.subTest(role=role):
                # Should not raise exception
                FormattingSecurityBoundary.assert_can_mutate_toolchain_rules(role)

    # 6. Full CLI Format Check Runner Execution
    def test_format_check_cli_tool_executes_cleanly(self):
        """tools/format_check.py executes and returns exit code 0."""
        target_dir = self.root_dir
        if (
            not (target_dir / "tools" / "format_check.py").exists()
            and (target_dir.parent / "tools" / "format_check.py").exists()
        ):
            target_dir = target_dir.parent
        proc = subprocess.run(
            ["python", "tools/format_check.py"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"format_check.py failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("All multi-language code formatting standards VERIFIED", proc.stdout)


if __name__ == "__main__":
    unittest.main()
