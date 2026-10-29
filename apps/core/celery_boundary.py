"""
apps/core/celery_boundary.py
Architecture Decision: ADR-0002, ADR-0003 & ADR-0031
Phase 02: Repository, Toolchain & Engineering Automation

Architectural Boundary Guard for Asynchronous Redis Workers vs. Synchronous Core:
1. Core stock allocation, inventory ledger updates, financial invoices, and prescription
   validation MUST remain strictly synchronous and transactional (ACID).
2. Asynchronous Celery/Redis workers are strictly bounded to:
   - NOTIFICATION: SMS, Email, low stock alerts, cashier close summaries.
   - REMINDER: DGDA Drug license expiry, PCB renewals, near-expiry batch quarantine.
   - DERIVED_ANALYTICS: Daily sales rollups, velocity aggregation, mobile KPI cache warming.
3. Any attempt to enqueue core stock/ledger operations raises AsyncBoundaryViolationError.
4. Transactional post-commit dispatch pattern guarantees zero phantom jobs on DB rollback.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.core.models import (
    AsyncJobRecord,
    Branch,
    JobCategory,
    JobStatus,
    Tenant,
)

logger = logging.getLogger("pharmacy.celery_boundary")


class AsyncBoundaryViolationError(Exception):
    """Raised when an operation from the synchronous transactional core is scheduled asynchronously."""


class InvalidJobPayloadError(ValueError):
    """Raised when an asynchronous job payload lacks mandatory parameters."""


class CrossTenantJobError(PermissionError):
    """Raised when an async job attempts to access or mutate resources outside its tenant scope."""


class ExpiredSubscriptionJobError(PermissionError):
    """Raised when an inactive or expired tenant attempts to invoke background jobs."""


class AsyncJobBoundaryEnforcer:
    """
    Canonical authority and gatekeeper for Celery/Redis asynchronous job dispatch.
    Architecture Decision: ADR-0031.
    """

    PERMITTED_CATEGORIES = {
        JobCategory.NOTIFICATION,
        JobCategory.REMINDER,
        JobCategory.DERIVED_ANALYTICS,
    }

    PROHIBITED_CORE_ACTIONS = {
        "CORE_STOCK_MUTATION",
        "CORE_LEDGER_POSTING",
        "CORE_SALE_FINALIZATION",
        "CORE_PRESCRIPTION_DISPENSING",
        "INVENTORY_LEDGER_ENTRY",
        "BATCH_STOCK_DEDUCTION",
        "DOUBLE_ENTRY_JOURNAL_POST",
    }

    TASK_PAYLOAD_REQUIREMENTS = {
        "send_sale_notification_task": {"invoice_id", "recipient_phone"},
        "check_and_dispatch_license_expiry_reminders_task": set(),
        "aggregate_daily_sales_analytics_task": {"date_str"},
    }

    @classmethod
    def validate_boundary(cls, category: str, task_name: str) -> None:
        """Enforces that core stock and ledger mutations cannot be pushed to background queues."""
        upper_cat = str(category).upper()
        upper_task = str(task_name).upper()

        # Check for explicitly prohibited core operations
        for prohibited in cls.PROHIBITED_CORE_ACTIONS:
            if prohibited in upper_cat or prohibited in upper_task:
                raise AsyncBoundaryViolationError(
                    f"Architectural boundary violation (ADR-0031): '{category} / {task_name}' is part of "
                    f"the synchronous transactional core and cannot be executed via Redis/Celery. "
                    f"All stock mutations and ledger postings must be committed synchronously in transaction.atomic()."
                )

        if category not in cls.PERMITTED_CATEGORIES:
            raise AsyncBoundaryViolationError(
                f"Disallowed job category '{category}'. Only {list(cls.PERMITTED_CATEGORIES)} are permitted "
                f"for asynchronous Redis worker processing (ADR-0031)."
            )

    @classmethod
    def validate_payload(cls, task_name: str, payload: dict[str, Any]) -> None:
        """Validates that the payload contains required parameters for the specific task."""
        required_keys = cls.TASK_PAYLOAD_REQUIREMENTS.get(task_name)
        if required_keys:
            missing = required_keys - set(payload.keys())
            if missing:
                raise InvalidJobPayloadError(
                    f"Invalid job payload for '{task_name}': Missing required parameter(s): {sorted(missing)}"
                )

    @classmethod
    def enqueue_job(
        cls,
        category: JobCategory | str,
        task_name: str,
        tenant_id: str,
        payload: dict[str, Any],
        branch_id: str | None = None,
        execute_inline: bool = False,
    ) -> AsyncJobRecord:
        """
        Validates, records, and dispatches an asynchronous job to the Redis/Celery queue.
        Architecture Decision: ADR-0031.
        """
        # 1. Enforce architectural category boundary
        cls.validate_boundary(category, task_name)

        # 2. Validate tenant & subscription status
        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist as err:
            raise CrossTenantJobError(f"Cannot dispatch async job: Tenant '{tenant_id}' does not exist.") from err

        if not tenant.is_active:
            raise ExpiredSubscriptionJobError(
                f"Tenant '{tenant_id}' subscription is inactive or expired. Background jobs are suspended."
            )

        # 3. Branch validation if provided
        branch = None
        if branch_id:
            try:
                branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
            except Branch.DoesNotExist as err:
                raise CrossTenantJobError(f"Branch '{branch_id}' does not belong to tenant '{tenant_id}'.") from err

        # 4. Payload validation
        cls.validate_payload(task_name, payload)

        # 5. Persist AsyncJobRecord (PENDING status)
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        job_record = AsyncJobRecord.objects.create(
            job_id=job_id,
            tenant=tenant,
            branch=branch,
            category=category,
            task_name=task_name,
            status=JobStatus.PENDING,
            payload=payload,
            scheduled_at=timezone.now(),
        )

        # 6. Dispatch or execute
        if execute_inline:
            cls.execute_job_direct(job_record)
            job_record.refresh_from_db()
        else:
            cls._dispatch_to_celery(job_record)

        return job_record

    @classmethod
    def dispatch_post_commit_job(
        cls,
        category: JobCategory | str,
        task_name: str,
        tenant_id: str,
        payload: dict[str, Any],
        branch_id: str | None = None,
    ) -> None:
        """
        Registers an async job to be enqueued ONLY AFTER the current DB transaction commits.
        Guarantees zero phantom jobs if the transaction rolls back (ADR-0031).
        """
        cls.validate_boundary(category, task_name)

        def _on_commit_callback():
            cls.enqueue_job(
                category=category,
                task_name=task_name,
                tenant_id=tenant_id,
                payload=payload,
                branch_id=branch_id,
            )

        transaction.on_commit(_on_commit_callback)

    @classmethod
    def _dispatch_to_celery(cls, job_record: AsyncJobRecord) -> None:
        """Dispatches job to Celery worker via shared task."""
        from apps.core import tasks

        task_func = getattr(tasks, job_record.task_name, None)
        if task_func and hasattr(task_func, "delay"):
            task_func.delay(job_record.job_id)
        else:
            # Fallback to direct synchronous execution if Celery worker is offline
            cls.execute_job_direct(job_record)

    @classmethod
    def execute_job_direct(cls, job_record: AsyncJobRecord) -> None:
        """Executes the task logic directly (for eager execution or testing)."""
        from apps.core import tasks

        task_func = getattr(tasks, job_record.task_name, None)
        if callable(task_func):
            task_func(job_record.job_id)
        else:
            job_record.status = JobStatus.FAILED
            job_record.error_message = f"Task function '{job_record.task_name}' not implemented in apps.core.tasks."
            job_record.completed_at = timezone.now()
            job_record.save()

    @classmethod
    def check_redis_status(cls) -> dict[str, Any]:
        """Probes Redis connectivity and cache operations."""
        import os
        import time

        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        status = {
            "configured_url": redis_url,
            "connected": False,
            "ping_latency_ms": None,
            "cache_backend": cache.__class__.__name__,
            "error": None,
        }

        try:
            import redis

            client = redis.Redis.from_url(redis_url, socket_timeout=2)
            start_t = time.perf_counter()
            ping_ok = client.ping()
            latency = (time.perf_counter() - start_t) * 1000.0

            status["connected"] = bool(ping_ok)
            status["ping_latency_ms"] = round(latency, 2)
        except Exception as err:
            status["error"] = str(err)
            status["connected"] = False

        return status
