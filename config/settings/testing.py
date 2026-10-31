"""
config.settings.testing
Architecture Decision: ADR-0041
Testing environment profile with in-memory SQLite and fast hashing for automated CI.
"""

from __future__ import annotations

from config.settings.base import *  # noqa: F401, F403

DJANGO_ENV = "testing"
DEBUG = False
SECRET_KEY = "django-insecure-test-environment-key-for-fast-unit-testing"
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1", "*"]
CORS_ALLOW_ALL_ORIGINS = True

# Ultra-fast in-memory database for isolated unit and contract tests
DB_ENGINE = "sqlite"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Accelerated password hasher for sub-second test execution
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Eager asynchronous task execution during testing
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Fast In-Memory Cache
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-pharmacy-cache",
    }
}

# Transport security relaxed for mock test clients
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_HSTS_SECONDS = 0
IS_HARDENED = False

# Disable throttling during automated unit and integration tests
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_THROTTLE_CLASSES": [],
}
