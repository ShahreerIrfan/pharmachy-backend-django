"""
apps/core/env_isolation.py
Architecture Decision: ADR-0022
Phase 02: Repository, Toolchain & Engineering Automation

Provides runtime version pinning, environment configuration schema validation,
automated secret leak detection, and role-based security boundaries for the
Bangladesh Multi-Tenant Pharmacy ERP & POS Cloud Authority API.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class EnvironmentValidationError(Exception):
    """Raised when environment configuration or Python runtime violates security or stability invariants."""

    pass


@dataclass(frozen=True)
class EnvironmentConfig:
    """Immutable typed configuration object for backend cloud authority."""

    django_secret_key: str
    django_debug: bool
    django_allowed_hosts: list[str]
    db_engine: str
    postgres_db: str | None
    postgres_user: str | None
    postgres_password: str | None
    postgres_host: str | None
    postgres_port: int | None
    redis_url: str
    jwt_signing_key: str
    environment: str = "development"

    def to_dict(self) -> dict[str, Any]:
        """Convert to raw dictionary representation."""
        return {
            "DJANGO_SECRET_KEY": self.django_secret_key,
            "DJANGO_DEBUG": self.django_debug,
            "DJANGO_ALLOWED_HOSTS": self.django_allowed_hosts,
            "DB_ENGINE": self.db_engine,
            "POSTGRES_DB": self.postgres_db,
            "POSTGRES_USER": self.postgres_user,
            "POSTGRES_PASSWORD": self.postgres_password,
            "POSTGRES_HOST": self.postgres_host,
            "POSTGRES_PORT": self.postgres_port,
            "REDIS_URL": self.redis_url,
            "JWT_SIGNING_KEY": self.jwt_signing_key,
            "ENVIRONMENT": self.environment,
        }


class PythonRuntimeValidator:
    """
    Enforces strict Python runtime pinning.
    ADR-0022 requirement: Python >= 3.13.0 and < 3.14.0.
    """

    MIN_VERSION: tuple[int, int, int] = (3, 13, 0)
    MAX_EXCLUSIVE_VERSION: tuple[int, int, int] = (3, 14, 0)

    @classmethod
    def validate_runtime(cls, version_info: tuple[int, int, int] | None = None) -> bool:
        """
        Validate the Python runtime against pinned limits.
        If version_info is None, inspects sys.version_info.
        Raises EnvironmentValidationError if unsupported.
        """
        if version_info is None:
            v = sys.version_info
            current: tuple[int, int, int] = (v.major, v.minor, v.micro)
        else:
            current = (version_info[0], version_info[1], version_info[2])

        min_str = f"{cls.MIN_VERSION[0]}.{cls.MIN_VERSION[1]}.{cls.MIN_VERSION[2]}"
        max_str = f"{cls.MAX_EXCLUSIVE_VERSION[0]}.{cls.MAX_EXCLUSIVE_VERSION[1]}.{cls.MAX_EXCLUSIVE_VERSION[2]}"
        cur_str = f"{current[0]}.{current[1]}.{current[2]}"

        if current < cls.MIN_VERSION:
            raise EnvironmentValidationError(
                f"Unsupported Python runtime version {cur_str}. Minimum required version is {min_str} (ADR-0022)."
            )

        if current >= cls.MAX_EXCLUSIVE_VERSION:
            raise EnvironmentValidationError(
                f"Untested forward Python runtime version {cur_str}. "
                f"Allowed version range is >={min_str}, <{max_str} (ADR-0022)."
            )

        return True


class SecretLeakDetector:
    """
    Scans environment files, templates, and raw strings for leaked secrets,
    private keys, cloud credentials, and insecure defaults in production.
    """

    PATTERNS = {
        "AWS Access Key": re.compile(r"(?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}"),
        "Private Key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
        "Stripe/Payment Live Key": re.compile(r"(?:sk|rk)_live_[0-9a-zA-Z]{24,}"),
        "High Entropy Token": re.compile(r"ghp_[0-9a-zA-Z]{36}"),
    }

    KNOWN_INSECURE_KEYS: set[str] = {
        "django-insecure-dev-key-pharmacy-saas-2026-bangladesh",
        "dev-insecure-key",
        "secret",
        "changeme",
        "password",
        "123456",
    }

    @classmethod
    def scan_content(cls, content: str, filename: str = "") -> list[str]:
        """Scan a text buffer for known secret patterns."""
        violations: list[str] = []

        for name, pattern in cls.PATTERNS.items():
            if pattern.search(content):
                prefix = f"[{filename}] " if filename else ""
                violations.append(f"{prefix}Found potential leaked credential pattern: {name}")

        return violations

    @classmethod
    def assert_no_leaks(cls, content: str, filename: str = "") -> None:
        """Raise EnvironmentValidationError if any secret patterns are detected."""
        violations = cls.scan_content(content, filename)
        if violations:
            raise EnvironmentValidationError("Secret leak scan failed! Detected violations:\n" + "\n".join(violations))

    @classmethod
    def validate_production_key(cls, secret_key: str) -> None:
        """Validate that a production secret key is strong and not a known default."""
        if not secret_key:
            raise EnvironmentValidationError("Production DJANGO_SECRET_KEY must not be empty.")

        if secret_key in cls.KNOWN_INSECURE_KEYS or secret_key.startswith("django-insecure-"):
            raise EnvironmentValidationError(
                "Production environment is using a known insecure default DJANGO_SECRET_KEY. "
                "You must generate a high-entropy secret (ADR-0022)."
            )

        if len(secret_key) < 32:
            raise EnvironmentValidationError(
                f"Production DJANGO_SECRET_KEY is too short ({len(secret_key)} chars). "
                f"Minimum required length is 32 characters."
            )


class EnvironmentConfigValidator:
    """
    Validates environment dictionaries against typed security and schema constraints.
    """

    @classmethod
    def parse_bool(cls, val: Any, default: bool = False) -> bool:
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        s = str(val).strip().lower()
        return s in ("true", "1", "yes", "on")

    @classmethod
    def validate(cls, env: dict[str, str] | None = None) -> EnvironmentConfig:
        """
        Validate environment dictionary (defaults to os.environ).
        Returns typed EnvironmentConfig on success, raises EnvironmentValidationError on failure.
        """
        raw = dict(os.environ) if env is None else dict(env)

        # 1. Environment mode
        env_mode = raw.get("ENVIRONMENT", "development").strip().lower()
        if env_mode not in ("development", "staging", "production", "test"):
            raise EnvironmentValidationError(
                f"Invalid ENVIRONMENT mode: '{env_mode}'. Allowed: development, staging, production, test."
            )

        # 2. Django Debug
        debug_raw = raw.get("DJANGO_DEBUG")
        debug = cls.parse_bool(debug_raw, default=(env_mode == "development"))

        # In production or staging, debug must NEVER be True
        if env_mode in ("production", "staging") and debug:
            raise EnvironmentValidationError(f"DJANGO_DEBUG must be False in {env_mode} environment (ADR-0022).")

        # 3. Secret Key
        secret_key = raw.get("DJANGO_SECRET_KEY", "").strip()
        if not secret_key:
            raise EnvironmentValidationError("Mandatory environment variable DJANGO_SECRET_KEY is missing.")

        if not debug or env_mode == "production":
            SecretLeakDetector.validate_production_key(secret_key)
        else:
            if len(secret_key) < 16:
                raise EnvironmentValidationError("DJANGO_SECRET_KEY must be at least 16 characters even in dev.")

        # 4. Allowed Hosts
        allowed_hosts_str = raw.get("DJANGO_ALLOWED_HOSTS", "* if debug else ''").strip()
        if not allowed_hosts_str:
            if not debug:
                raise EnvironmentValidationError("DJANGO_ALLOWED_HOSTS cannot be empty in production/staging mode.")
            allowed_hosts = ["*"]
        else:
            allowed_hosts = [h.strip() for h in allowed_hosts_str.split(",") if h.strip()]

        if not debug and "*" in allowed_hosts:
            raise EnvironmentValidationError(
                "DJANGO_ALLOWED_HOSTS wildcard '*' is strictly forbidden in non-debug mode (ADR-0022)."
            )

        # 5. Database Engine & Connection Variables
        db_engine = raw.get("DB_ENGINE", "sqlite").strip().lower()
        if db_engine not in ("postgres", "sqlite"):
            raise EnvironmentValidationError(f"Unsupported DB_ENGINE '{db_engine}'. Allowed: postgres, sqlite.")

        pg_db: str | None = None
        pg_user: str | None = None
        pg_password: str | None = None
        pg_host: str | None = None
        pg_port: int | None = None

        if db_engine == "postgres":
            missing_pg = []
            for var in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST", "POSTGRES_PORT"):
                if not raw.get(var):
                    missing_pg.append(var)
            if missing_pg:
                raise EnvironmentValidationError(f"DB_ENGINE=postgres requires variables: {', '.join(missing_pg)}.")

            pg_db = raw["POSTGRES_DB"].strip()
            pg_user = raw["POSTGRES_USER"].strip()
            pg_password = raw["POSTGRES_PASSWORD"].strip()
            pg_host = raw["POSTGRES_HOST"].strip()
            try:
                pg_port = int(raw["POSTGRES_PORT"].strip())
                if not (1 <= pg_port <= 65535):
                    raise ValueError()
            except ValueError:
                raise EnvironmentValidationError(
                    f"POSTGRES_PORT must be an integer between 1 and 65535. Got: {raw.get('POSTGRES_PORT')}"
                )

        # 6. Redis URL
        redis_url = raw.get("REDIS_URL", "").strip()
        if not redis_url:
            raise EnvironmentValidationError("Mandatory environment variable REDIS_URL is missing.")
        if not (redis_url.startswith("redis://") or redis_url.startswith("rediss://")):
            raise EnvironmentValidationError(f"REDIS_URL must begin with redis:// or rediss://. Got: '{redis_url}'")

        # 7. JWT Signing Key
        jwt_key = raw.get("JWT_SIGNING_KEY", "").strip()
        if not jwt_key:
            raise EnvironmentValidationError("Mandatory environment variable JWT_SIGNING_KEY is missing.")
        if len(jwt_key) < 32:
            raise EnvironmentValidationError(
                f"JWT_SIGNING_KEY must be at least 32 characters long. Got: {len(jwt_key)} chars."
            )
        if jwt_key == secret_key:
            raise EnvironmentValidationError(
                "JWT_SIGNING_KEY must be distinct from DJANGO_SECRET_KEY for cryptographic isolation (ADR-0022)."
            )

        return EnvironmentConfig(
            django_secret_key=secret_key,
            django_debug=debug,
            django_allowed_hosts=allowed_hosts,
            db_engine=db_engine,
            postgres_db=pg_db,
            postgres_user=pg_user,
            postgres_password=pg_password,
            postgres_host=pg_host,
            postgres_port=pg_port,
            redis_url=redis_url,
            jwt_signing_key=jwt_key,
            environment=env_mode,
        )


class EnvironmentSecurityBoundary:
    """
    Role-Based Access Control (RBAC) boundary protecting backend environment credentials.
    Only PLATFORM_SUPER_ADMIN and DEVOPS_ENGINEER are permitted to inspect raw secrets.
    """

    ADMIN_ROLES: set[str] = {"PLATFORM_SUPER_ADMIN", "DEVOPS_ENGINEER", "LEAD_ARCHITECT"}

    @classmethod
    def assert_authorized_access(cls, actor_role: str) -> None:
        """Raise PermissionError if actor is not authorized to access environment secrets."""
        if actor_role not in cls.ADMIN_ROLES:
            raise PermissionError(
                f"Security Violation: Actor role '{actor_role}' is not authorized to inspect "
                f"environment credentials. Required: {', '.join(sorted(cls.ADMIN_ROLES))} (ADR-0022)."
            )

    @classmethod
    def export_sanitized_config(cls, actor_role: str, config: EnvironmentConfig) -> dict[str, Any]:
        """
        Return sanitized configuration dictionary with credentials masked.
        Non-administrative roles are completely denied access.
        """
        cls.assert_authorized_access(actor_role)

        data = config.to_dict()
        data["DJANGO_SECRET_KEY"] = "******** [MASKED]"
        data["JWT_SIGNING_KEY"] = "******** [MASKED]"
        if data["POSTGRES_PASSWORD"]:
            data["POSTGRES_PASSWORD"] = "******** [MASKED]"
        return data

    @classmethod
    def export_raw_secrets(cls, actor_role: str, config: EnvironmentConfig) -> dict[str, Any]:
        """
        Return unmasked credentials strictly for super-admin or devops deployment runners.
        """
        cls.assert_authorized_access(actor_role)
        return config.to_dict()


def load_env_file(path: str | Path) -> dict[str, str]:
    """
    Safely parse a .env or .env.example file into a key-value dictionary.
    Ignores comments, empty lines, and strips surrounding quotes.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Environment file not found at: {p.resolve()}")

    content = p.read_text(encoding="utf-8")
    env_vars: dict[str, str] = {}

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'\"")
            env_vars[key] = val

    return env_vars
