"""
apps.secrets_vault.scanner
Architecture Decision: ADR-0037
Secret Exposure Scanner for Continuous CI/CD Guardrails and Source Auditing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List


class SecretExposureViolation(Exception):
    """Raised when an unmasked private key, API secret, or high-entropy credential is detected."""

    pass


class SecretExposureScanner:
    """Scans code buffers, environment files, and configuration strings for exposed secrets."""

    PATTERNS: Dict[str, re.Pattern[str]] = {
        "AWS Access Key": re.compile(r"\b(?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b"),
        "Private Key Header": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
        "Stripe/Gateway Live Key": re.compile(r"\b(?:sk|rk)_live_[0-9a-zA-Z]{24,}\b"),
        "Mobile Financial Secret": re.compile(r"\b(?:bkash|nagad)_(?:sec|key)_[0-9a-zA-Z]{16,}\b"),
        "GitHub Personal Access Token": re.compile(r"\bghp_[0-9a-zA-Z]{36}\b"),
    }

    @classmethod
    def scan_string(cls, content: str, source_name: str = "") -> List[str]:
        """Scans a text buffer and returns a list of detected pattern violation descriptions."""
        violations: List[str] = []
        if not content:
            return violations

        for name, pattern in cls.PATTERNS.items():
            if pattern.search(content):
                prefix = f"[{source_name}] " if source_name else ""
                violations.append(f"{prefix}Exposed secret pattern detected: {name}")

        return violations

    @classmethod
    def scan_file(cls, file_path: Path) -> List[str]:
        """Reads a file from disk and scans its content."""
        if not file_path.exists() or not file_path.is_file():
            return []
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            return cls.scan_string(content, source_name=file_path.name)
        except Exception:
            return []

    @classmethod
    def assert_no_exposure(cls, content: str, source_name: str = "") -> None:
        """Raises SecretExposureViolation if any secret exposure patterns are found."""
        violations = cls.scan_string(content, source_name=source_name)
        if violations:
            raise SecretExposureViolation("\n".join(violations))
