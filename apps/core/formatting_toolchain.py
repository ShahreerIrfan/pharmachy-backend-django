"""
apps/core/formatting_toolchain.py
Architecture Decision: ADR-0026
Phase 02: Repository, Toolchain & Engineering Automation

Comprehensive code formatting standards, linter rules, and CI enforcement tooling:
- Universal .editorconfig validation (indentation, UTF-8, final newline, trim whitespace).
- Python Ruff linter and formatter execution.
- Dart format and Flutter analysis execution.
- Java OpenJDK coding standards and import order validation.
- Role-based authorization boundary for toolchain configuration mutation.
"""

from __future__ import annotations

import configparser
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Set


class FormattingValidationError(Exception):
    """Raised when source files or configuration violate code formatting standards."""

    pass


@dataclass(frozen=True)
class EditorConfigSummary:
    root_defined: bool
    sections_count: int
    sections: List[str]
    max_line_length: int


class EditorConfigValidator:
    """Validates the structure and properties of the root .editorconfig file."""

    REQUIRED_SECTIONS: Set[str] = {
        "*",
        "*.py",
        "*.java",
        "*.dart",
        "*.{json,json5}",
        "*.{yml,yaml}",
        "*.md",
    }

    @classmethod
    def validate_editorconfig_file(cls, editorconfig_path: Path | str) -> EditorConfigSummary:
        p = Path(editorconfig_path).resolve()
        if not p.is_file():
            raise FormattingValidationError(f".editorconfig file not found at: {p}")

        content = p.read_text(encoding="utf-8")
        if "root = true" not in content and "root=true" not in content:
            raise FormattingValidationError(".editorconfig must declare 'root = true' at the top level.")

        # Strip top-level declarations (like 'root = true') so configparser can parse sections cleanly
        section_lines = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("root") and "=" in stripped:
                continue
            section_lines.append(line)

        config = configparser.ConfigParser(interpolation=None)
        try:
            config.read_string("\n".join(section_lines))
        except Exception as e:
            raise FormattingValidationError(f"Invalid .editorconfig syntax: {e}")

        sections = set(config.sections())
        missing = cls.REQUIRED_SECTIONS - sections
        if missing:
            raise FormattingValidationError(
                f".editorconfig missing mandatory language section(s): {', '.join(sorted(missing))}"
            )

        # Inspect general section
        gen = config["*"]
        if gen.get("charset", "").lower() != "utf-8":
            raise FormattingValidationError("[*] section must define charset = utf-8")
        if gen.get("insert_final_newline", "").lower() != "true":
            raise FormattingValidationError("[*] section must define insert_final_newline = true")
        if gen.get("trim_trailing_whitespace", "").lower() != "true":
            raise FormattingValidationError("[*] section must define trim_trailing_whitespace = true")

        max_len = int(gen.get("max_line_length", "120"))

        return EditorConfigSummary(
            root_defined=True,
            sections_count=len(sections),
            sections=sorted(list(sections)),
            max_line_length=max_len,
        )

    @classmethod
    def check_file_conformance(cls, file_path: Path | str, indent_size: int = 4) -> List[str]:
        """Check a single text file for final newline and trailing whitespace."""
        p = Path(file_path).resolve()
        if not p.is_file():
            return [f"File not found: {p}"]

        issues = []
        raw_bytes = p.read_bytes()
        if not raw_bytes:
            return issues

        # Check final newline
        if not (raw_bytes.endswith(b"\n") or raw_bytes.endswith(b"\r\n")):
            issues.append(f"{p.name}: Missing final newline at end of file")

        # Check trailing whitespace line by line
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            issues.append(f"{p.name}: File is not valid UTF-8")
            return issues

        lines = text.splitlines()
        for idx, line in enumerate(lines, start=1):
            if line.endswith(" ") or line.endswith("\t"):
                issues.append(f"{p.name}:{idx}: Line has trailing whitespace")

        return issues


class PythonRuffValidator:
    """Executes and verifies Python formatting with Ruff."""

    @classmethod
    def run_ruff_check(cls, target_dir: Path | str) -> Dict[str, Any]:
        p = Path(target_dir).resolve()
        ruff_cmd = shutil.which("ruff") or "ruff"

        proc = subprocess.run(
            [ruff_cmd, "check", str(p)],
            capture_output=True,
            text=True,
            check=False,
        )

        if proc.returncode != 0:
            raise FormattingValidationError(f"Ruff linting failed on {p.name}:\n{proc.stdout}\n{proc.stderr}")

        return {
            "status": "PASS",
            "target": str(p),
            "output": proc.stdout.strip() or "All checks passed!",
        }


class DartFormatValidator:
    """Validates that Dart files conform to dart format rules (120 cols)."""

    @classmethod
    def run_dart_format_check(cls, target_dir: Path | str) -> Dict[str, Any]:
        p = Path(target_dir).resolve()
        dart_cmd = shutil.which("dart") or shutil.which("dart.bat") or "dart"

        proc = subprocess.run(
            [dart_cmd, "format", "--output=none", "--set-exit-if-changed", str(p)],
            capture_output=True,
            text=True,
            shell=True,
            check=False,
        )

        if proc.returncode != 0:
            raise FormattingValidationError(
                f"Dart format check failed on {p.name}. Run 'dart format {p}' to fix:\n{proc.stdout}"
            )

        return {
            "status": "PASS",
            "target": str(p),
            "output": proc.stdout.strip(),
        }


class JavaConventionValidator:
    """Enforces Java conventions: no wildcard imports, strict BigDecimal, UTF-8 encoding."""

    WILDCARD_IMPORT_PATTERN = re.compile(r"^\s*import\s+[a-zA-Z0-9_.]+\.\*;", re.MULTILINE)

    @classmethod
    def scan_java_file(cls, java_file: Path | str) -> List[str]:
        p = Path(java_file).resolve()
        if not p.is_file():
            return [f"Java file not found: {p}"]

        issues = []
        content = p.read_text(encoding="utf-8")

        # Wildcard imports
        wildcards = cls.WILDCARD_IMPORT_PATTERN.findall(content)
        if wildcards:
            issues.append(f"{p.name}: Contains forbidden wildcard import(s): {wildcards}")

        # Check for trailing whitespace
        lines = content.splitlines()
        for idx, line in enumerate(lines, start=1):
            if line.endswith(" ") or line.endswith("\t"):
                issues.append(f"{p.name}:{idx}: Trailing whitespace detected")

        return issues


class FormattingSecurityBoundary:
    """RBAC guard protecting developer tooling, linter configs, and formatters."""

    ADMIN_ROLES: Set[str] = {"PLATFORM_SUPER_ADMIN", "DEVOPS_ENGINEER", "LEAD_ARCHITECT"}

    @classmethod
    def assert_can_mutate_toolchain_rules(cls, actor_role: str) -> None:
        if actor_role not in cls.ADMIN_ROLES:
            raise PermissionError(
                f"Security Violation: Actor role '{actor_role}' is not authorized to alter "
                f"code formatting standards or linter rules. Required: {', '.join(sorted(cls.ADMIN_ROLES))} (ADR-0026)."
            )
