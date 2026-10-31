"""
config.settings
Architecture Decision: ADR-0041
Modular Settings Package Root with Dynamic Environment Delegation.
Dispatches to development, testing, or production based on DJANGO_ENV.
"""

from __future__ import annotations

import os

_ENV = os.environ.get("DJANGO_ENV", "development").lower()

if _ENV == "production":
    from config.settings.production import *  # noqa: F401, F403
elif _ENV in ("test", "testing"):
    from config.settings.testing import *  # noqa: F401, F403
else:
    from config.settings.development import *  # noqa: F401, F403


def get_active_environment() -> str:
    """Returns canonical environment name (development, testing, or production)."""
    return globals().get("DJANGO_ENV", "development")


def get_profile_metadata() -> dict:
    """Extracts sanitized environment profile telemetry conforming to modular_settings.schema.json."""
    from django.utils import timezone

    db_conf = globals().get("DATABASES", {}).get("default", {})
    engine_name = "postgresql" if "postgresql" in db_conf.get("ENGINE", "") else "sqlite3"
    db_name = str(db_conf.get("NAME", "unknown"))

    return {
        "environment": get_active_environment(),
        "debug": bool(globals().get("DEBUG", False)),
        "database_engine": engine_name,
        "database_name": db_name,
        "is_hardened": bool(globals().get("IS_HARDENED", False)),
        "ssl_redirect": bool(globals().get("SECURE_SSL_REDIRECT", False)),
        "session_cookie_secure": bool(globals().get("SESSION_COOKIE_SECURE", False)),
        "csrf_cookie_secure": bool(globals().get("CSRF_COOKIE_SECURE", False)),
        "hsts_seconds": int(globals().get("SECURE_HSTS_SECONDS", 0)),
        "allowed_hosts": list(globals().get("ALLOWED_HOSTS", [])),
        "secrets_validated": bool(globals().get("DJANGO_ENV") == "production"),
        "inspected_at": timezone.now().isoformat(),
    }
