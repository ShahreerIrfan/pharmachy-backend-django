"""
config.settings.production
Architecture Decision: ADR-0041
Hardened production profile with PostgreSQL 16, strict transport security, and fail-fast secrets validation.
"""

from __future__ import annotations

import os
from typing import List

from django.core.exceptions import ImproperlyConfigured

from config.settings.base import *  # noqa: F401, F403

DJANGO_ENV = "production"
DEBUG = False

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
raw_hosts = os.environ.get("DJANGO_ALLOWED_HOSTS", "")
ALLOWED_HOSTS: List[str] = [h.strip() for h in raw_hosts.split(",") if h.strip()]
CORS_ALLOW_ALL_ORIGINS = False


class ProductionSecretsValidator:
    """
    Guarantees that production deployments fail fast if essential secrets,
    canonical PostgreSQL database credentials, or transport constraints are violated.
    """

    @classmethod
    def validate(cls, secret_key: str, postgres_password: str, allowed_hosts: List[str]) -> None:
        if not secret_key or secret_key.startswith("django-insecure-"):
            raise ImproperlyConfigured(
                "ADR-0041 Violation: Production DJANGO_SECRET_KEY must be configured with a cryptographically "
                "secure secret and cannot begin with 'django-insecure-'."
            )

        if not postgres_password or postgres_password == "pharmacy_secure_pass":
            raise ImproperlyConfigured(
                "ADR-0041 Violation: Production POSTGRES_PASSWORD must be configured and cannot use the "
                "development default 'pharmacy_secure_pass'."
            )

        if not allowed_hosts:
            raise ImproperlyConfigured("ADR-0041 Violation: Production DJANGO_ALLOWED_HOSTS must be non-empty.")

        if "*" in allowed_hosts or any(h == "*" for h in allowed_hosts):
            raise ImproperlyConfigured(
                "ADR-0041 Violation: Production DJANGO_ALLOWED_HOSTS cannot contain wildcard '*'."
            )


# Canonical Production Database: PostgreSQL 16
DB_ENGINE = "postgres"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "pharmacy_erp_db"),
        "USER": os.environ.get("POSTGRES_USER", "pharmacy_user"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "postgres"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 600,
    }
}

# Hardened Transport & Security Headers
SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "True").lower() in ("true", "1")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
X_FRAME_OPTIONS = "DENY"
IS_HARDENED = True

# Execute immediate fail-fast validation when active in production
if os.environ.get("DJANGO_ENV", "").lower() == "production" and os.environ.get(
    "SKIP_PRODUCTION_SECRETS_VALIDATION", "False"
).lower() not in ("true", "1"):
    ProductionSecretsValidator.validate(
        secret_key=SECRET_KEY,
        postgres_password=DATABASES["default"]["PASSWORD"],
        allowed_hosts=ALLOWED_HOSTS,
    )
