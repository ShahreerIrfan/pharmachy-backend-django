"""
apps.file_storage.antivirus
Architecture Decision: ADR-0051
Antivirus and Content Inspection Policy Engine for Bangladesh Pharmacy SaaS.
Inspects file magic headers, scans for malicious payloads/executables, and enforces size/type policies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ScanResult:
    is_clean: bool
    scan_status: str  # "CLEAN", "INFECTED", "QUARANTINED", "FAILED"
    threat_name: str
    quarantine_reason: str
    detected_mime: str
    scan_provider: str = "DGDA-Malware-Sentry-v2"


class AntivirusPolicyEngine:
    """
    Validates file integrity, enforces MIME whitelist, inspects binary magic headers,
    and quarantines known malware signatures and executable binary payloads.
    """

    ALLOWED_MIMES = {
        "image/jpeg",
        "image/png",
        "image/webp",
        "application/pdf",
    }

    CATEGORY_MAX_SIZES = {
        "PRESCRIPTION_IMAGE": 10 * 1024 * 1024,  # 10 MB
        "SALES_INVOICE_PDF": 10 * 1024 * 1024,  # 10 MB
        "PURCHASE_ORDER": 10 * 1024 * 1024,  # 10 MB
        "DGDA_COMPLIANCE": 15 * 1024 * 1024,  # 15 MB
        "GENERAL_ATTACHMENT": 5 * 1024 * 1024,  # 5 MB
    }

    # Standard EICAR Antivirus Test Signature
    EICAR_SIGNATURE = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

    # Executable binary header markers
    EXECUTABLE_MAGIC_HEADERS = [
        (b"MZ", "Win32/PE.Executable"),
        (b"\x7fELF", "Linux/ELF.Binary"),
        (b"\xca\xfe\xba\xbe", "Java/ClassBytecode"),
        (b"\xfe\xed\xfa\xce", "Mach-O/Binary32"),
        (b"\xce\xfa\xed\xfe", "Mach-O/Binary32Rev"),
        (b"\xfe\xed\xfa\xcf", "Mach-O/Binary64"),
        (b"\xcf\xfa\xed\xfe", "Mach-O/Binary64Rev"),
    ]

    # Suspicious script patterns
    SCRIPT_PATTERNS = [
        (re.compile(rb"<\?php", re.IGNORECASE), "Script/PHP.WebShell"),
        (re.compile(rb"<script[\s>]", re.IGNORECASE), "Script/HTML.XSS.Payload"),
        (re.compile(rb"^#!\s*/bin/(sh|bash)", re.MULTILINE), "Script/Unix.Shell"),
        (re.compile(rb"^#!\s*/usr/bin/env", re.MULTILINE), "Script/Env.Interpreter"),
    ]

    @classmethod
    def detect_magic_mime(cls, content: bytes) -> str | None:
        """Inspects binary magic bytes to determine real file MIME type."""
        if content.startswith(b"%PDF-"):
            return "application/pdf"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WEBP":
            return "image/webp"
        return None

    @classmethod
    def scan_file(cls, filename: str, content: bytes, category: str, declared_mime: str) -> ScanResult:
        """
        Executes comprehensive multi-vector antivirus and content policy scanning.
        """
        file_size = len(content)

        # 1. Size Limit Check
        max_size = cls.CATEGORY_MAX_SIZES.get(category, 10 * 1024 * 1024)
        if file_size > max_size:
            return ScanResult(
                is_clean=False,
                scan_status="QUARANTINED",
                threat_name="Policy/OversizedPayload",
                quarantine_reason=f"File exceeds maximum allowed size {max_size} bytes for category {category}.",
                detected_mime=declared_mime,
            )

        # 2. Declared MIME whitelist check
        if declared_mime not in cls.ALLOWED_MIMES:
            return ScanResult(
                is_clean=False,
                scan_status="QUARANTINED",
                threat_name="Policy/DisallowedMimeType",
                quarantine_reason=f"Declared MIME type '{declared_mime}' is not in approved clinical whitelist.",
                detected_mime=declared_mime,
            )

        # 3. Known Malware Signature Check (EICAR)
        if cls.EICAR_SIGNATURE in content:
            return ScanResult(
                is_clean=False,
                scan_status="INFECTED",
                threat_name="Virus:EICAR_Standard_Test_Payload",
                quarantine_reason="EICAR test malware signature detected. File isolated immediately.",
                detected_mime=declared_mime,
            )

        # 4. Executable Binary Headers Check
        for magic, threat in cls.EXECUTABLE_MAGIC_HEADERS:
            if content.startswith(magic):
                return ScanResult(
                    is_clean=False,
                    scan_status="QUARANTINED",
                    threat_name=threat,
                    quarantine_reason=f"Executable binary header detected ({threat}). Executables forbidden.",
                    detected_mime="application/x-dosexec",
                )

        # 5. Magic Byte Inspection vs Declared MIME
        magic_mime = cls.detect_magic_mime(content)
        if magic_mime is None:
            return ScanResult(
                is_clean=False,
                scan_status="QUARANTINED",
                threat_name="Suspicious/UnknownBinaryHeader",
                quarantine_reason="Binary content header does not match any allowed file formats (PDF, JPEG, PNG, WebP).",
                detected_mime="application/octet-stream",
            )

        # If declared MIME claims image/pdf but magic bytes mismatch
        if magic_mime != declared_mime:
            # Allow common JPEG variants image/jpg vs image/jpeg
            if not (declared_mime in ("image/jpeg", "image/jpg") and magic_mime == "image/jpeg"):
                return ScanResult(
                    is_clean=False,
                    scan_status="QUARANTINED",
                    threat_name="Spoofed/MimeTypeMismatch",
                    quarantine_reason=f"Declared MIME '{declared_mime}' does not match inspected magic bytes '{magic_mime}'.",
                    detected_mime=magic_mime,
                )

        # 6. Script Pattern Scanning (prevent embedded XSS or PHP shells in images/docs)
        for pattern, threat in cls.SCRIPT_PATTERNS:
            if pattern.search(content):
                return ScanResult(
                    is_clean=False,
                    scan_status="INFECTED",
                    threat_name=threat,
                    quarantine_reason=f"Executable script pattern detected ({threat}). Active code forbidden.",
                    detected_mime=magic_mime,
                )

        # Passed all inspection vectors
        return ScanResult(
            is_clean=True,
            scan_status="CLEAN",
            threat_name="NONE",
            quarantine_reason="",
            detected_mime=magic_mime,
        )
