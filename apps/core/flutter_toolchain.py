"""
apps/core/flutter_toolchain.py
Architecture Decision: ADR-0007, ADR-0009, ADR-0021 & ADR-0024
Phase 02: Repository, Toolchain & Engineering Automation

Provides automated validation and governance for Flutter mobile manager workspace,
stable SDK constraints, analyzer rules, platform flavors, zero-emoji policy,
monetary decimal precision, and mobile RBAC security boundaries.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class FlutterToolchainError(Exception):
    """Raised when Flutter workspace setup, analyzer rules, or mobile invariants are violated."""

    pass


@dataclass(frozen=True)
class FlutterSdkInfo:
    flutter_version: str
    dart_version: str
    channel: str
    is_supported: bool


class FlutterSdkValidator:
    """Validates Flutter stable SDK and Dart runtime versions."""

    MIN_FLUTTER_VERSION: tuple[int, int, int] = (3, 19, 0)
    MIN_DART_VERSION: tuple[int, int, int] = (3, 3, 0)

    @classmethod
    def parse_version_output(cls, output: str) -> FlutterSdkInfo:
        # e.g. Flutter 3.44.1 • channel stable • https://...
        # Tools • Dart 3.12.1 • DevTools 2.57.0
        f_match = re.search(r"Flutter\s+(\d+)\.(\d+)\.(\d+)", output)
        d_match = re.search(r"Dart\s+(?:SDK version:\s+)?(\d+)\.(\d+)\.(\d+)", output)
        ch_match = re.search(r"channel\s+([a-zA-Z]+)", output)

        if not f_match:
            raise FlutterToolchainError(f"Unable to parse Flutter version from output:\n{output}")

        f_ver = (int(f_match.group(1)), int(f_match.group(2)), int(f_match.group(3)))
        d_ver = (int(d_match.group(1)), int(d_match.group(2)), int(d_match.group(3))) if d_match else (3, 3, 0)
        channel = ch_match.group(1) if ch_match else "stable"

        is_supported = (f_ver >= cls.MIN_FLUTTER_VERSION) and (d_ver >= cls.MIN_DART_VERSION)

        return FlutterSdkInfo(
            flutter_version=f"{f_ver[0]}.{f_ver[1]}.{f_ver[2]}",
            dart_version=f"{d_ver[0]}.{d_ver[1]}.{d_ver[2]}",
            channel=channel,
            is_supported=is_supported,
        )

    @classmethod
    def validate_installed_sdk(cls) -> FlutterSdkInfo:
        import shutil

        cmd = shutil.which("flutter") or shutil.which("flutter.bat") or "flutter"
        try:
            result = subprocess.run([cmd, "--version"], capture_output=True, text=True, shell=True, check=False)
            output = (result.stdout + "\n" + result.stderr).strip()
            info = cls.parse_version_output(output)
            if not info.is_supported:
                raise FlutterToolchainError(
                    f"Unsupported Flutter SDK version {info.flutter_version} (Dart {info.dart_version}). "
                    f"Minimum required is Flutter >= 3.19.0, Dart >= 3.3.0 (ADR-0024)."
                )
            return info
        except FileNotFoundError:
            raise FlutterToolchainError("Flutter binary not found in PATH.")


class FlutterWorkspaceValidator:
    """Validates layered mobile architecture and analyzer rules."""

    MANDATORY_FILES = [
        "pubspec.yaml",
        "analysis_options.yaml",
        "lib/main.dart",
        "lib/core/constants/flavors.dart",
        "lib/core/theme/tokens.dart",
        "lib/models/dashboard_kpi.dart",
        "lib/models/tenant_session.dart",
        "lib/models/stock_alert.dart",
        "lib/services/mobile_security_service.dart",
        "lib/ui/widgets/kpi_card.dart",
        "lib/ui/screens/dashboard_screen.dart",
        "test/unit/dashboard_kpi_test.dart",
        "test/unit/mobile_security_service_test.dart",
        "test/widget/dashboard_screen_test.dart",
        "test/widget_test.dart",
    ]

    @classmethod
    def validate_workspace(cls, workspace_dir: Path | str) -> dict[str, Any]:
        wdir = Path(workspace_dir).resolve()
        missing = []
        for rel in cls.MANDATORY_FILES:
            f = wdir / rel
            if not f.is_file() or f.stat().st_size == 0:
                missing.append(rel)

        if missing:
            raise FlutterToolchainError(
                f"Incomplete Flutter workspace in {wdir}. Missing or empty files: {', '.join(missing)}"
            )

        # Inspect analysis_options.yaml for strict rules
        analysis_opts = (wdir / "analysis_options.yaml").read_text(encoding="utf-8")
        if "strict-casts: true" not in analysis_opts:
            raise FlutterToolchainError("analysis_options.yaml missing strict-casts: true")
        if "prefer_const_constructors" not in analysis_opts:
            raise FlutterToolchainError("analysis_options.yaml missing prefer_const_constructors rule")

        # Inspect pubspec.yaml for name and dependencies
        pubspec = (wdir / "pubspec.yaml").read_text(encoding="utf-8")
        if "name: pharmacy_mobile_manager" not in pubspec:
            raise FlutterToolchainError("pubspec.yaml must define name: pharmacy_mobile_manager")
        if "cupertino_icons" not in pubspec:
            raise FlutterToolchainError("pubspec.yaml missing cupertino_icons dependency (ADR-0009)")

        # Inspect flavors in flavors.dart
        flavors_content = (wdir / "lib/core/constants/flavors.dart").read_text(encoding="utf-8")
        if "enum AppFlavor { dev, staging, prod }" not in flavors_content:
            raise FlutterToolchainError("flavors.dart missing AppFlavor enum (dev, staging, prod)")

        return {
            "status": "VALID",
            "app_name": "pharmacy_mobile_manager",
            "files_checked": len(cls.MANDATORY_FILES),
            "flavors": ["dev", "staging", "prod"],
        }


class FlutterEmojiValidator:
    """Enforces zero-emoji iconography rule across all Dart source files (ADR-0009)."""

    EMOJI_PATTERN = re.compile(
        r"[\U0001F600-\U0001F64F"  # Emoticons
        r"\U0001F300-\U0001F5FF"  # Misc symbols & pictographs
        r"\U0001F680-\U0001F6FF"  # Transport & map
        r"\U0001F1E0-\U0001F1FF"  # Flags
        r"\U00002702-\U000027B0"  # Dingbats
        r"\U0001F900-\U0001F9FF"  # Supplemental symbols
        r"]+",
        flags=re.UNICODE,
    )

    @classmethod
    def scan_directory(cls, dir_path: Path | str) -> list[str]:
        p = Path(dir_path).resolve()
        violations = []
        for file in p.rglob("*.dart"):
            text = file.read_text(encoding="utf-8", errors="ignore")
            matches = cls.EMOJI_PATTERN.findall(text)
            if matches:
                violations.append(
                    f"Dart file {file.name} contains forbidden emoji icon(s): {matches} (ADR-0009 violation)"
                )
        return violations


class FlutterFinancialPrecisionValidator:
    """Verifies that Dart models avoid raw float for money amounts."""

    @classmethod
    def validate_dart_monetary_types(cls, dart_file_path: Path | str) -> None:
        p = Path(dart_file_path).resolve()
        content = p.read_text(encoding="utf-8")
        forbidden = re.findall(
            r"\b(?:double|num)\s+(?:todaySales|price|total|revenue|subtotal)\b", content, re.IGNORECASE
        )
        if forbidden:
            raise FlutterToolchainError(
                f"Financial accuracy violation in {p.name}: forbidden floating-point type for money: {forbidden}. "
                f"Must use exact decimal string or scaled integer (ADR-0001, ADR-0024)."
            )
