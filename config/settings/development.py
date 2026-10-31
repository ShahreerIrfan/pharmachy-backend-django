"""
config.settings.development
Architecture Decision: ADR-0041
Development environment profile with local SQLite support and developer tooling.
"""

from __future__ import annotations

import os

from config.settings.base import *  # noqa: F401, F403

DJANGO_ENV = "development"
DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() in ("true", "1", "yes")
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-dev-key-pharmacy-saas-2026-bangladesh")
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")
CORS_ALLOW_ALL_ORIGINS = DEBUG

# Database Configuration (SQLite default for local dev, PostgreSQL optional)
DB_ENGINE = os.environ.get("DB_ENGINE", "sqlite")
if DB_ENGINE == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "pharmacy_erp_db"),
            "USER": os.environ.get("POSTGRES_USER", "pharmacy_user"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "pharmacy_secure_pass"),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",  # noqa: F405
        }
    }

# Development Transport Defaults (Relaxed for localhost/HTTP testing)
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_HSTS_SECONDS = 0
IS_HARDENED = False
