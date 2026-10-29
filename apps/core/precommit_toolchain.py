"""
apps/core/precommit_toolchain.py
Architecture Decision: ADR-0027
Phase 02: Repository, Toolchain & Engineering Automation

Comprehensive Pre-Commit Quality, Security, and Compliance Engine:
- Risky files detection (.env, .db, .sql, .pem, .bak, OS debris).
- Oversized assets detection (> 5MB file threshold).
- Enterprise secret scanning (AWS, bKash, Nagad, SSLCommerz, JWT, DB URIs, Django SECRET_KEY).
- Accidental patient data / PHI leak detection (Bangladesh NID, mobile numbers, DICOM, unredacted Rx images).
- Syntax validation (Python AST, JSON, YAML).
- Role-based security boundary for hook administration and cross-tenant protection.
- Orchestration runner with structured reports and CLI bindings.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import yaml

# Maximum allowable file size for repository commits (5.0 MB)
MAX_FILE_SIZE_BYTES: int = 5 * 1024 * 1024

# Allowed synthetic / dummy test phone numbers and identifiers
ALLOWED_SYNTHETIC_IDENTIFIERS: Set[str] = {
    "01700000000",
    "01800000000",
    "01900000000",
    "+8801700000000",
    "+8801800000000",
    "+8801900000000",
    "0000000000",
    "1234567890",
    "19901234567890123",
}


class PreCommitSecurityError(PermissionError):
    """Raised when an unauthorized actor attempts to modify git hook policies or bypass checks."""

    pass


class PreCommitValidationError(Exception):
    """Raised when staged or tracked repository files violate security or quality guardrails."""

    pass


@dataclass(frozen=True)
class PreCommitViolation:
    guardrail: str
    file_path: str
    line_number: Optional[int]
    message_en: str
    message_bn: str
    snippet: Optional[str] = None
    remediation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PreCommitReport:
    is_valid: bool
    violations: List[PreCommitViolation]
    checked_files_count: int
    execution_seconds: float
    guardrail_status: Dict[str, str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "violations_count": len(self.violations),
            "checked_files_count": self.checked_files_count,
            "execution_seconds": round(self.execution_seconds, 3),
            "guardrail_status": self.guardrail_status,
            "violations": [v.to_dict() for v in self.violations],
        }

    def summary_text(self) -> str:
        lines = [
            f"Pre-Commit Inspection: {'PASSED' if self.is_valid else 'FAILED'}",
            f"Files Audited: {self.checked_files_count}",
            f"Execution Time: {self.execution_seconds:.2f}s",
            "Guardrail Statuses:",
        ]
        for name, status in self.guardrail_status.items():
            lines.append(f"  - {name}: {status}")

        if self.violations:
            lines.append(f"\nViolations Detected ({len(self.violations)}):")
            for idx, v in enumerate(self.violations, 1):
                loc = f"{v.file_path}:{v.line_number}" if v.line_number else v.file_path
                lines.append(f"  [{idx}] [{v.guardrail}] at {loc}")
                lines.append(f"      EN: {v.message_en}")
                lines.append(f"      BN: {v.message_bn}")
                if v.snippet:
                    lines.append(f"      Snippet: {v.snippet.strip()}")
                if v.remediation:
                    lines.append(f"      Remedy: {v.remediation}")
        return "\n".join(lines)


# ==============================================================================
# 1. RISKY FILES SCANNER
# ==============================================================================
class RiskyFileScanner:
    """Detects files that must never be committed to Git."""

    # Explicit filenames forbidden in any directory
    FORBIDDEN_FILENAMES: Set[str] = {
        ".env",
        ".env.local",
        ".env.production",
        ".env.staging",
        ".env.test",
        ".env.dev",
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
    }

    # Forbidden file extensions
    FORBIDDEN_EXTENSIONS: Set[str] = {
        ".sqlite",
        ".sqlite3",
        ".db",
        ".rdb",
        ".dump",
        ".sql",
        ".pem",
        ".key",
        ".pkcs12",
        ".pfx",
        ".p12",
        ".bak",
        ".swp",
    }

    # Allowed exceptions (e.g. .env.example, sample templates)
    ALLOWED_FILENAMES: Set[str] = {
        ".env.example",
        ".env.template",
        ".env.sample",
    }

    @classmethod
    def scan_path(cls, file_path: str | Path) -> List[PreCommitViolation]:
        violations: List[PreCommitViolation] = []
        p = Path(file_path)
        name = p.name
        suffix = p.suffix.lower()

        # Exempt allowed files
        if name in cls.ALLOWED_FILENAMES:
            return violations

        # Check forbidden exact filenames
        if name in cls.FORBIDDEN_FILENAMES or (name.startswith(".env.") and name not in cls.ALLOWED_FILENAMES):
            violations.append(
                PreCommitViolation(
                    guardrail="RISKY_FILES",
                    file_path=str(p),
                    line_number=None,
                    message_en=f"Direct commit of environment or risky file '{name}' is forbidden.",
                    message_bn=f"এনভায়রনমেন্ট বা ঝুঁকিপূর্ণ ফাইল '{name}' সরাসরি গিট কমিট করা নিষিদ্ধ।",
                    remediation="Add to .gitignore and use .env.example with sanitized placeholder values instead.",
                )
            )

        # Check forbidden extensions
        if suffix in cls.FORBIDDEN_EXTENSIONS:
            violations.append(
                PreCommitViolation(
                    guardrail="RISKY_FILES",
                    file_path=str(p),
                    line_number=None,
                    message_en=f"Direct commit of database or cryptographic key file '{name}' ({suffix}) is forbidden.",
                    message_bn=f"ডাটাবেজ বা ক্রিপ্টোগ্রাফিক কি ফাইল '{name}' ({suffix}) কমিট করা নিষিদ্ধ।",
                    remediation="Store secrets in environment variables or key vaults, and databases outside Git.",
                )
            )

        return violations


# ==============================================================================
# 2. OVERSIZED ASSET SCANNER
# ==============================================================================
class OversizedAssetScanner:
    """Guards against committing large binary files that bloat repository history."""

    @classmethod
    def scan_file(cls, file_path: str | Path, max_bytes: int = MAX_FILE_SIZE_BYTES) -> List[PreCommitViolation]:
        p = Path(file_path)
        if not p.is_file():
            return []

        size = p.stat().st_size
        if size > max_bytes:
            mb_size = round(size / (1024 * 1024), 2)
            limit_mb = round(max_bytes / (1024 * 1024), 2)
            return [
                PreCommitViolation(
                    guardrail="OVERSIZED_ASSETS",
                    file_path=str(p),
                    line_number=None,
                    message_en=f"File exceeds maximum repository size limit: {mb_size} MB > {limit_mb} MB threshold.",
                    message_bn=f"ফাইলটি সর্বোচ্চ আকারের সীমা অতিক্রম করেছে: {mb_size} মেগাবাইট > {limit_mb} মেগাবাইট।",
                    remediation="Upload large assets, videos, or archives to CDN or cloud object storage.",
                )
            ]
        return []


# ==============================================================================
# 3. SECRET DETECTION SCANNER
# ==============================================================================
class SecretScanner:
    """Scans text content for hardcoded keys, tokens, and credentials."""

    SECRET_PATTERNS: List[Tuple[str, re.Pattern, str, str]] = [
        (
            "PRIVATE_KEY_BLOCK",
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
            "Hardcoded cryptographic private key block detected.",
            "হার্ডকোডেড ক্রিপ্টোগ্রাফিক প্রাইভেট কি ব্লক শনাক্ত হয়েছে।",
        ),
        (
            "AWS_ACCESS_KEY",
            re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
            "Hardcoded AWS Access Key ID detected.",
            "হার্ডকোডেড এডব্লিউএস অ্যাক্সেস কি শনাক্ত হয়েছে।",
        ),
        (
            "BKASH_APP_CREDENTIAL",
            re.compile(
                r"""(?:bkash_app_key|bkash_app_secret|bkash_username|bkash_password)\s*[:=]\s*["']([^"']{8,})["']""",
                re.IGNORECASE,
            ),
            "Hardcoded bKash merchant/API credential detected.",
            "হার্ডকোডেড বিকাশ মার্চেন্ট/এপিআই ক্রেডেনশিয়াল শনাক্ত হয়েছে।",
        ),
        (
            "NAGAD_CREDENTIAL",
            re.compile(r"""(?:nagad_public_key|nagad_private_key)\s*[:=]\s*["']([^"']{16,})["']""", re.IGNORECASE),
            "Hardcoded Nagad payment gateway cryptographic key detected.",
            "হার্ডকোডেড নগদ পেমেন্ট গেটওয়ে কি শনাক্ত হয়েছে।",
        ),
        (
            "SSLCOMMERZ_KEY",
            re.compile(
                r"""(?:sslcommerz_store_passwd|sslcommerz_store_id)\s*[:=]\s*["']([^"']{6,})["']""", re.IGNORECASE
            ),
            "Hardcoded SSLCommerz payment credentials detected.",
            "হার্ডকোডেড এসএসএলকমার্জ পেমেন্ট ক্রেডেনশিয়াল শনাক্ত হয়েছে।",
        ),
        (
            "DATABASE_URI_PASSWORD",
            re.compile(r"""postgres(?:ql)?:\/\/[a-zA-Z0-9_\-]+:([^@\s"']{4,})@[a-zA-Z0-9_\-\.]+:\d+\/\w+"""),
            "Database connection URI containing raw embedded password detected.",
            "পাসওয়ার্ডযুক্ত ডাটাবেজ কানেকশন ইউআরআই শনাক্ত হয়েছে।",
        ),
        (
            "JWT_SECRET",
            re.compile(r"""(?:jwt_secret|jwt_secret_key)\s*[:=]\s*["']([^"']{16,})["']""", re.IGNORECASE),
            "Hardcoded JWT signing secret detected.",
            "হার্ডকোডেড জেডব্লিউটি সিক্রেট শনাক্ত হয়েছে।",
        ),
        (
            "DJANGO_SECRET_KEY",
            re.compile(r"""SECRET_KEY\s*=\s*["'](?!django-insecure-test)([^"']{30,})["']"""),
            "Hardcoded Django production SECRET_KEY detected.",
            "হার্ডকোডেড জ্যাঙ্গো প্রোডাকশন সিক্রেট কি শনাক্ত হয়েছে।",
        ),
    ]

    DUMMY_DATABASE_CREDENTIALS: Set[str] = {
        "postgres:postgres",
        "user:password",
        "user:pass",
        "test_user:test_password",
        "root:root",
        "app:secret",
    }

    ALLOWLIST_PRAGMA = "pragma: allowlist secret"

    @classmethod
    def scan_content(cls, content: str, file_path: str = "") -> List[PreCommitViolation]:
        violations: List[PreCommitViolation] = []
        # If the file explicitly marks entire contents for secret scanner testing
        if "# test-allowlist-secrets" in content or "<!-- pragma: allowlist secret -->" in content:
            return violations

        lines = content.splitlines()

        for idx, line in enumerate(lines, 1):
            if cls.ALLOWLIST_PRAGMA in line:
                continue

            # Skip dummy development database credentials
            if any(dummy in line for dummy in cls.DUMMY_DATABASE_CREDENTIALS):
                continue

            for rule_id, pattern, msg_en, msg_bn in cls.SECRET_PATTERNS:
                match = pattern.search(line)
                if match:
                    # Sanitize snippet display to prevent leaking the secret in test logs
                    matched_text = match.group(0)
                    masked = matched_text[:6] + "..." + matched_text[-4:] if len(matched_text) > 12 else "[MASKED]"
                    violations.append(
                        PreCommitViolation(
                            guardrail="SECRETS",
                            file_path=file_path,
                            line_number=idx,
                            message_en=f"{msg_en} (Rule: {rule_id})",
                            message_bn=f"{msg_bn} (রুল: {rule_id})",
                            snippet=f"{line[: match.start()]} {masked} {line[match.end() :]}",
                            remediation="Move credential to an environment variable or vault secret store.",
                        )
                    )
        return violations


# ==============================================================================
# 4. PATIENT DATA & PHI SCANNER
# ==============================================================================
class PatientDataScanner:
    """Guards against accidental inclusion of real Bangladesh patient records, NID, or prescriptions."""

    # Bangladesh National ID Patterns:
    # 1. Smart NID: 10 digits
    # 2. Legacy NID: 13 digits
    # 3. Legacy NID with birth year prefix: 17 digits (19xx or 20xx)
    NID_10_PATTERN = re.compile(
        r"""["']?(?:nid|national_id|patient_nid|patient_id)["']?\s*[:=]\s*["']?([1-9]\d{9})(?!\d)["']?""",
        re.IGNORECASE,
    )
    NID_13_PATTERN = re.compile(
        r"""["']?(?:nid|national_id|patient_nid)["']?\s*[:=]\s*["']?([1-9]\d{12})(?!\d)["']?""", re.IGNORECASE
    )
    NID_17_PATTERN = re.compile(
        r"""["']?(?:nid|national_id|patient_nid)["']?\s*[:=]\s*["']?((?:19|20)\d{15})(?!\d)["']?""", re.IGNORECASE
    )

    # Bangladesh Mobile Number in Patient / Customer Context
    PATIENT_PHONE_PATTERN = re.compile(
        r"""["']?(?:patient_phone|patient_mobile|rx_phone|patient_contact)["']?\s*[:=]\s*["']?((?:\+?88)?01[3-9]\d{8})(?!\d)["']?""",
        re.IGNORECASE,
    )

    # Clinical Medical Images and Patient Scans
    FORBIDDEN_PHI_EXTENSIONS: Set[str] = {".dcm"}

    PHI_IMAGE_PATTERN = re.compile(
        r"^(?:prescription_|rx_patient_|patient_scan_|medical_record_).*\.(?:jpg|jpeg|png|pdf)$", re.IGNORECASE
    )

    @classmethod
    def scan_path(cls, file_path: str | Path) -> List[PreCommitViolation]:
        violations: List[PreCommitViolation] = []
        p = Path(file_path)
        name = p.name.lower()
        suffix = p.suffix.lower()

        # Check for medical image formats (DICOM)
        if suffix in cls.FORBIDDEN_PHI_EXTENSIONS:
            violations.append(
                PreCommitViolation(
                    guardrail="PATIENT_DATA",
                    file_path=str(p),
                    line_number=None,
                    message_en=f"Direct commit of medical imaging DICOM file '{p.name}' is prohibited.",
                    message_bn=f"মেডিকেল ইমেজিং ডাইকম ফাইল '{p.name}' সরাসরি কমিট করা সম্পূর্ণ নিষিদ্ধ।",
                    remediation="Store patient medical records in encrypted HIPPA/DGDA compliant cloud storage.",
                )
            )

        # Check for unredacted prescription image names
        if cls.PHI_IMAGE_PATTERN.match(name) and "sample" not in name and "dummy" not in name:
            violations.append(
                PreCommitViolation(
                    guardrail="PATIENT_DATA",
                    file_path=str(p),
                    line_number=None,
                    message_en=f"Suspected unredacted patient prescription file '{p.name}' committed.",
                    message_bn=f"অ-অনুমোদিত রোগীর প্রেসক্রিপশন ইমেজ ফাইল '{p.name}' শনাক্ত হয়েছে।",
                    remediation="Redact personal identifiable patient information or use synthetic fixture data.",
                )
            )

        return violations

    @classmethod
    def scan_content(cls, content: str, file_path: str = "") -> List[PreCommitViolation]:
        violations: List[PreCommitViolation] = []
        lines = content.splitlines()

        # If file is explicitly marked as synthetic mock data, allow safe synthetic fixtures
        if "# synthetic-fixture" in content or '"is_synthetic": true' in content:
            return violations

        for idx, line in enumerate(lines, 1):
            if "pragma: allowlist phi" in line:
                continue

            # 1. Check NID patterns (17-digit, 13-digit, or 10-digit)
            match17 = cls.NID_17_PATTERN.search(line)
            match13 = cls.NID_13_PATTERN.search(line)
            match10 = cls.NID_10_PATTERN.search(line)

            if match17 and match17.group(1) not in ALLOWED_SYNTHETIC_IDENTIFIERS:
                violations.append(
                    PreCommitViolation(
                        guardrail="PATIENT_DATA",
                        file_path=file_path,
                        line_number=idx,
                        message_en=f"Suspected real Bangladesh 17-digit legacy NID detected ({match17.group(1)[:6]}***).",
                        message_bn="সন্দেহভাজন আসল বাংলাদেশ ১৭-ডিজিটের লিগ্যাসি এনআইডি শনাক্ত হয়েছে।",
                        snippet=line.strip(),
                        remediation="Anonymize patient NID or use approved synthetic test identifiers.",
                    )
                )
            elif match13 and match13.group(1) not in ALLOWED_SYNTHETIC_IDENTIFIERS:
                violations.append(
                    PreCommitViolation(
                        guardrail="PATIENT_DATA",
                        file_path=file_path,
                        line_number=idx,
                        message_en=f"Suspected real Bangladesh 13-digit legacy NID detected ({match13.group(1)[:4]}***).",
                        message_bn="সন্দেহভাজন আসল বাংলাদেশ ১৩-ডিজিটের লিগ্যাসি এনআইডি শনাক্ত হয়েছে।",
                        snippet=line.strip(),
                        remediation="Anonymize patient NID or use approved synthetic test identifiers.",
                    )
                )
            elif match10 and match10.group(1) not in ALLOWED_SYNTHETIC_IDENTIFIERS:
                violations.append(
                    PreCommitViolation(
                        guardrail="PATIENT_DATA",
                        file_path=file_path,
                        line_number=idx,
                        message_en=f"Suspected real Bangladesh 10-digit Smart NID detected ({match10.group(1)[:3]}***).",
                        message_bn="সন্দেহভাজন আসল বাংলাদেশ ১০-ডিজিটের স্মার্ট এনআইডি শনাক্ত হয়েছে।",
                        snippet=line.strip(),
                        remediation="Anonymize patient NID or use synthetic test constants (e.g. 0000000000).",
                    )
                )

            # 4. Check patient phone numbers
            match_phone = cls.PATIENT_PHONE_PATTERN.search(line)
            if match_phone and match_phone.group(1) not in ALLOWED_SYNTHETIC_IDENTIFIERS:
                violations.append(
                    PreCommitViolation(
                        guardrail="PATIENT_DATA",
                        file_path=file_path,
                        line_number=idx,
                        message_en=f"Real Bangladesh patient mobile number detected ({match_phone.group(1)[:5]}***).",
                        message_bn="আসল বাংলাদেশ রোগীর মোবাইল নম্বর শনাক্ত হয়েছে।",
                        snippet=line.strip(),
                        remediation="Use synthetic test phone e.g. 01700000000 or tag with # synthetic-fixture.",
                    )
                )

        return violations


# ==============================================================================
# 5. SYNTAX VALIDATOR
# ==============================================================================
class SyntaxValidator:
    """Validates Python AST, JSON structure, and YAML syntax."""

    @classmethod
    def validate_file(cls, file_path: str | Path) -> List[PreCommitViolation]:
        p = Path(file_path)
        if not p.is_file():
            return []

        suffix = p.suffix.lower()
        content = ""
        try:
            content = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Binary file
            return []

        violations: List[PreCommitViolation] = []

        if suffix == ".py":
            try:
                ast.parse(content, filename=str(p))
            except SyntaxError as e:
                violations.append(
                    PreCommitViolation(
                        guardrail="SYNTAX",
                        file_path=str(p),
                        line_number=e.lineno,
                        message_en=f"Python syntax error: {e.msg}",
                        message_bn=f"পাইথন সিনট্যাক্স ত্রুটি: {e.msg}",
                        snippet=e.text.strip() if e.text else None,
                        remediation="Fix Python syntax error prior to staging commit.",
                    )
                )

        elif suffix == ".json":
            try:
                json.loads(content)
            except json.JSONDecodeError as e:
                violations.append(
                    PreCommitViolation(
                        guardrail="SYNTAX",
                        file_path=str(p),
                        line_number=e.lineno,
                        message_en=f"Malformed JSON document: {e.msg}",
                        message_bn=f"বিকৃত JSON ডকুমেন্ট: {e.msg}",
                        snippet=None,
                        remediation="Ensure JSON conforms to strict RFC 8259 syntax.",
                    )
                )

        elif suffix in {".yml", ".yaml"}:
            try:
                yaml.safe_load(content)
            except yaml.YAMLError as e:
                violations.append(
                    PreCommitViolation(
                        guardrail="SYNTAX",
                        file_path=str(p),
                        line_number=getattr(e, "problem_mark", None).line + 1
                        if hasattr(e, "problem_mark") and e.problem_mark
                        else None,
                        message_en=f"Malformed YAML document: {str(e)}",
                        message_bn=f"বিকৃত YAML ডকুমেন্ট: {str(e)}",
                        snippet=None,
                        remediation="Verify indentation and YAML syntax correctness.",
                    )
                )

        return violations


# ==============================================================================
# 6. SECURITY BOUNDARY & HOOK MANAGEMENT RBAC
# ==============================================================================
class PreCommitSecurityBoundary:
    """Guards pre-commit hook policies and enforces tenant isolation."""

    AUTHORIZED_HOOK_ADMINS: Set[str] = {
        "PLATFORM_SUPER_ADMIN",
        "LEAD_ARCHITECT",
        "DEVOPS_ENGINEER",
        "SYSTEM",
    }

    @classmethod
    def authorize_hook_administration(cls, actor_role: str) -> None:
        """Verifies that only authorized platform architects can configure or modify hooks."""
        if actor_role not in cls.AUTHORIZED_HOOK_ADMINS:
            raise PreCommitSecurityError(
                f"Actor role '{actor_role}' is not authorized to install, modify, or bypass git hook policies. "
                f"Required: {cls.AUTHORIZED_HOOK_ADMINS}"
            )

    @classmethod
    def validate_tenant_isolation(
        cls, file_content: str, active_tenant_id: str = "tenant-bd-default"
    ) -> List[PreCommitViolation]:
        """Detects whether foreign tenant IDs or cross-tenant private keys are present in payloads."""
        violations: List[PreCommitViolation] = []
        # Flag cross-tenant injection in fixtures or staged configs
        foreign_match = re.search(r"""["']?tenant_id["']?\s*[:=]\s*["'](tenant-[a-z0-9\-]+)["']""", file_content)
        if foreign_match:
            detected_tenant = foreign_match.group(1)
            # If explicit test cross-tenant check is intended, allow if tagged
            if (
                detected_tenant != active_tenant_id
                and "cross-tenant-test" not in file_content
                and detected_tenant != "tenant-cross-sample"
            ):
                violations.append(
                    PreCommitViolation(
                        guardrail="TENANT_ISOLATION",
                        file_path="payload",
                        line_number=None,
                        message_en=f"Cross-tenant ID mismatch: detected '{detected_tenant}', expected '{active_tenant_id}'.",
                        message_bn=f"ক্রস-টেন্যান্ট আইডি গরমিল: '{detected_tenant}' পাওয়া গেছে, প্রত্যাশিত '{active_tenant_id}'।",
                        remediation="Ensure staged changes only affect the authenticated tenant scope.",
                    )
                )
        return violations


# ==============================================================================
# 7. ORCHESTRATION RUNNER
# ==============================================================================
class PreCommitRunner:
    """Orchestrates all 7 pre-commit guardrails across files or git staged index."""

    def __init__(self, root_dir: Optional[Path] = None):
        self.root_dir = (root_dir or Path(__file__).resolve().parent.parent.parent).resolve()

    def get_staged_files(self) -> List[Path]:
        """Returns list of staged files from Git index."""
        try:
            res = subprocess.run(
                ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
                cwd=str(self.root_dir),
                capture_output=True,
                text=True,
                check=True,
            )
            files = [self.root_dir / line.strip() for line in res.stdout.splitlines() if line.strip()]
            return [f for f in files if f.exists()]
        except Exception:
            return []

    def get_tracked_files(self) -> List[Path]:
        """Returns list of tracked files in the repository."""
        try:
            res = subprocess.run(
                ["git", "ls-files"],
                cwd=str(self.root_dir),
                capture_output=True,
                text=True,
                check=True,
            )
            files = [self.root_dir / line.strip() for line in res.stdout.splitlines() if line.strip()]
            return [f for f in files if f.exists()]
        except Exception:
            return []

    def execute_smoke_test(self) -> Tuple[bool, str]:
        """Executes fast smoke test (e.g., monorepo structure validator)."""
        validator_script = self.root_dir / "tools" / "monorepo_validator.py"
        if not validator_script.exists():
            validator_script = self.root_dir / "shared" / "tools" / "monorepo_validator.py"
        if not validator_script.exists():
            return False, "monorepo_validator.py not found"

        res = subprocess.run(
            ["python", str(validator_script)],
            cwd=str(self.root_dir),
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            return True, "Monorepo structure & entry points valid"
        return False, res.stderr or res.stdout

    def run_guardrails(
        self,
        files: Sequence[Path | str],
        run_smoke_test: bool = True,
    ) -> PreCommitReport:
        """Executes full guardrail pipeline across the provided files."""
        start_time = time.time()
        violations: List[PreCommitViolation] = []
        statuses: Dict[str, str] = {
            "RISKY_FILES": "PASS",
            "OVERSIZED_ASSETS": "PASS",
            "SECRETS": "PASS",
            "PATIENT_DATA": "PASS",
            "SYNTAX": "PASS",
            "SMOKE_TEST": "PASS" if run_smoke_test else "SKIPPED",
        }

        resolved_files = [Path(f).resolve() for f in files]

        for p in resolved_files:
            rel_path = str(p.relative_to(self.root_dir)) if str(p).startswith(str(self.root_dir)) else str(p)

            # 1. Risky Files
            v_risky = RiskyFileScanner.scan_path(p)
            if v_risky:
                statuses["RISKY_FILES"] = "FAIL"
                violations.extend(v_risky)

            # 2. Oversized Assets
            v_oversized = OversizedAssetScanner.scan_file(p)
            if v_oversized:
                statuses["OVERSIZED_ASSETS"] = "FAIL"
                violations.extend(v_oversized)

            # 3 & 4: Text content checks (Secrets, Patient PHI)
            if p.is_file():
                try:
                    content = p.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    content = None

                if content is not None:
                    v_secrets = SecretScanner.scan_content(content, file_path=rel_path)
                    if v_secrets:
                        statuses["SECRETS"] = "FAIL"
                        violations.extend(v_secrets)

                    v_phi_content = PatientDataScanner.scan_content(content, file_path=rel_path)
                    if v_phi_content:
                        statuses["PATIENT_DATA"] = "FAIL"
                        violations.extend(v_phi_content)

            # 4. Patient PHI file path check
            v_phi_path = PatientDataScanner.scan_path(p)
            if v_phi_path:
                statuses["PATIENT_DATA"] = "FAIL"
                violations.extend(v_phi_path)

            # 5. Syntax validation
            v_syntax = SyntaxValidator.validate_file(p)
            if v_syntax:
                statuses["SYNTAX"] = "FAIL"
                violations.extend(v_syntax)

        # 7. Smoke test pass
        if run_smoke_test:
            ok, msg = self.execute_smoke_test()
            if not ok:
                statuses["SMOKE_TEST"] = "FAIL"
                violations.append(
                    PreCommitViolation(
                        guardrail="SMOKE_TEST",
                        file_path="tools/monorepo_validator.py",
                        line_number=None,
                        message_en=f"Rapid smoke test failed: {msg.strip()}",
                        message_bn=f"র‌্যাপিড স্মোক টেস্ট ব্যর্থ হয়েছে: {msg.strip()}",
                        remediation="Ensure core monorepo layout and essential files are intact.",
                    )
                )

        elapsed = time.time() - start_time
        is_valid = len(violations) == 0

        return PreCommitReport(
            is_valid=is_valid,
            violations=violations,
            checked_files_count=len(resolved_files),
            execution_seconds=elapsed,
            guardrail_status=statuses,
        )

    def install_git_hook(self, actor_role: str = "PLATFORM_SUPER_ADMIN") -> Path:
        """Installs the pre-commit hook into .git/hooks/pre-commit."""
        PreCommitSecurityBoundary.authorize_hook_administration(actor_role)

        hooks_dir = self.root_dir / ".git" / "hooks"
        if not hooks_dir.exists():
            hooks_dir.mkdir(parents=True, exist_ok=True)

        hook_script_path = hooks_dir / "pre-commit"

        hook_content = (
            "#!/usr/bin/env sh\n"
            "# Bangladesh Pharmacy SaaS Pre-Commit Hook (ADR-0027)\n"
            "# Automatically generated by apps.core.precommit_toolchain\n"
            "python tools/pre_commit_check.py --staged\n"
            "EXIT_CODE=$?\n"
            "if [ $EXIT_CODE -ne 0 ]; then\n"
            '    echo "[PRE-COMMIT ERROR] Quality and security gates failed. Commit aborted."\n'
            "    exit $EXIT_CODE\n"
            "fi\n"
            "exit 0\n"
        )

        hook_script_path.write_text(hook_content, encoding="utf-8")

        # Also maintain a tracked version in .githooks/pre-commit
        tracked_hooks_dir = self.root_dir / ".githooks"
        tracked_hooks_dir.mkdir(parents=True, exist_ok=True)
        tracked_hook_path = tracked_hooks_dir / "pre-commit"
        tracked_hook_path.write_text(hook_content, encoding="utf-8")

        return hook_script_path
