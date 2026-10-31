"""
Django Settings for Bangladesh Multi-Tenant Pharmacy ERP & POS Cloud Authority API
Architecture Decision: ADR-0004, ADR-0021 & ADR-0022
Canonical Database: PostgreSQL 16
Development / Offline Fallback: SQLite
"""

import os
import sys
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BASE_DIR.parent

# Ensure both BASE_DIR (standalone repository) and ROOT_DIR are on Python path
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Load local environment files if present
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
    load_dotenv(ROOT_DIR / ".env")
except ImportError:
    pass

# ADR-0022: Enforce Python runtime version pinning (>=3.13.0, <3.14.0)
from apps.core.env_isolation import EnvironmentConfigValidator, PythonRuntimeValidator

PythonRuntimeValidator.validate_runtime()

if os.environ.get("ENV_VALIDATE_STRICT", "False").lower() in ("true", "1"):
    EnvironmentConfigValidator.validate()

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-dev-key-pharmacy-saas-2026-bangladesh")
DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() in ("true", "1", "yes")
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

# Application Definition
INSTALLED_APPS = [
    # Django Core
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-Party
    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",
    "drf_spectacular",
    # SaaS Core & Modular Monolith Bounded Contexts
    "apps.core",
    "apps.audit_logging",
    "apps.developer_toolchain",
    "apps.secrets_vault",
    "apps.release_management",
]

MIDDLEWARE = [
    "apps.audit_logging.middleware.StructuredLoggingMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Database Configuration (PostgreSQL canonical, SQLite fallback for local test/dev)
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
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Localization & Bangladesh Settings
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Dhaka"
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Django REST Framework
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# OpenAPI Documentation
SPECTACULAR_SETTINGS = {
    "TITLE": "Bangladesh Multi-Tenant Pharmacy ERP & POS Cloud API",
    "DESCRIPTION": "OpenAPI 3.0 specification for cloud authority, POS sync, inventory, and clinical compliance.",
    "VERSION": "2.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# CORS Configuration
CORS_ALLOW_ALL_ORIGINS = DEBUG

# Redis & Celery Asynchronous Infrastructure (ADR-0031)
# Scoped strictly to: NOTIFICATION, REMINDER, and DERIVED_ANALYTICS.
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60
CELERY_TASK_ALWAYS_EAGER = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "False").lower() in ("true", "1")
CELERY_TASK_EAGER_PROPAGATES = True

# Cache Layer (ADR-0031: Derived analytics & Mobile Manager KPI Fast-Cache)
USE_REDIS_CACHE = os.environ.get("USE_REDIS_CACHE", "True").lower() in ("true", "1")
if USE_REDIS_CACHE:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": os.environ.get("REDIS_CACHE_URL", "redis://localhost:6379/1"),
            "TIMEOUT": 300,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "pharmacy-saas-cache",
        }
    }

# Structured JSON Logging Configuration (ADR-0035)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured_json": {
            "()": "apps.audit_logging.formatters.StructuredJsonFormatter",
            "service_name": "pharmacy-saas-cloud-api",
            "environment": os.environ.get("DJANGO_ENV", "development"),
        },
        "simple": {
            "format": "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured_json" if os.environ.get("LOG_JSON", "True").lower() in ("true", "1") else "simple",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.environ.get("LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "apps.audit_logging": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
