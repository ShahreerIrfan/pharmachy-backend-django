"""
Django Configuration Package for Bangladesh Multi-Tenant Pharmacy ERP & POS SaaS
Architecture Decision: ADR-0021 & ADR-0031
"""

from __future__ import annotations

# Expose celery app so shared_task works predictably
try:
    from config.celery import app as celery_app

    __all__ = ("celery_app",)
except ImportError:
    pass
