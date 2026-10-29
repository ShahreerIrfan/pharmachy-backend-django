"""
backend/config/celery.py
Architecture Decision: ADR-0021, ADR-0022 & ADR-0031
Phase 02: Repository, Toolchain & Engineering Automation

Celery Application Instance for Bangladesh Pharmacy ERP & POS Cloud SaaS.
Workers are strictly scoped to:
1. NOTIFICATION: SMS / Email / Webhook delivery.
2. REMINDER: DGDA Drug License, PCB registration, and Batch Expiry alerts.
3. DERIVED_ANALYTICS: Periodic daily sales rollups, demand velocity, and Redis cache warming.
Core stock/sale/ledger mutations are strictly synchronous and forbidden from this queue.
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("pharmacy_erp")

# Configure Celery using settings with 'CELERY_' prefix
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks from all installed Django apps (e.g. apps.core.tasks)
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self) -> None:
    """Diagnostic task for verifying Celery worker health and task execution."""
    print(f"Celery Diagnostic Task Request: {self.request!r}")
