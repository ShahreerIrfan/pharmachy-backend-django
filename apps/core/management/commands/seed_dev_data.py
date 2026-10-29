"""
apps/core/management/commands/seed_dev_data.py
Architecture Decision: ADR-0004 & ADR-0029
Django management command to deterministically seed development SQLite database.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.core.sqlite_bootstrap import SQLiteBootstrapEngine


class Command(BaseCommand):
    help = "Seeds deterministic Bangladesh pharmaceutical fixtures into local SQLite database"

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("--- Bootstrapping Local Development Database ---"))
        res = SQLiteBootstrapEngine.bootstrap()
        self.stdout.write(self.style.SUCCESS(f"Successfully bootstrapped: {res['status']} ({res['engine']})"))
        self.stdout.write(str(res["statistics"]))
