"""
apps/core/sqlite_bootstrap.py
Architecture Decision: ADR-0004 & ADR-0029
Phase 02: Repository, Toolchain & Engineering Automation

Comprehensive Local SQLite Backend Bootstrap, Deterministic Seeder,
and PostgreSQL Integration Parity Lane:
- Migrations automation with foreign key pragma enforcement.
- Deterministic seeding with realistic Bangladesh DGDA pharmaceutical fixtures.
- FEFO batch inventory, cold-chain bounds, and sales transactions.
- Zero floating-point monetary invariant (strict Decimal everywhere).
- PostgreSQL integration lane probe (detects live PostgreSQL service and concurrency contract).
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, Dict, Tuple

from django.core.management import call_command
from django.db import connection, transaction
from django.utils import timezone

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    InvoiceStatus,
    MedicineProduct,
    PaymentMethod,
    PlanTier,
    SaleInvoice,
    SaleInvoiceItem,
    Tenant,
)


class SQLiteBootstrapError(Exception):
    """Raised when SQLite bootstrap, migrations, or seeding fails."""

    pass


class DeterministicDatabaseSeeder:
    """Populates local SQLite database with consistent, deterministic Bangladesh pharmaceutical fixtures."""

    @classmethod
    @transaction.atomic
    def seed_all(cls) -> Dict[str, int]:
        """Seeds tenants, branches, medicine catalog, batches, and sample invoices."""
        counts = {
            "tenants": cls.seed_tenants(),
            "branches": cls.seed_branches(),
            "medicines": cls.seed_medicines(),
            "batches": cls.seed_batches(),
            "invoices": cls.seed_invoices(),
        }
        return counts

    @classmethod
    def seed_tenants(cls) -> int:
        tenants_data = [
            {
                "tenant_id": "tenant-dhaka-central",
                "name": "Lazz Pharma Dhaka Central",
                "subdomain": "lazz-dhaka",
                "plan_tier": PlanTier.PRO,
                "is_active": True,
            },
            {
                "tenant_id": "tenant-chittagong-care",
                "name": "Tamanna Pharmacy Chittagong",
                "subdomain": "tamanna-ctg",
                "plan_tier": PlanTier.STANDARD,
                "is_active": True,
            },
        ]
        created = 0
        for d in tenants_data:
            _, was_created = Tenant.objects.update_or_create(tenant_id=d["tenant_id"], defaults=d)
            if was_created:
                created += 1
        return len(tenants_data)

    @classmethod
    def seed_branches(cls) -> int:
        branches_data = [
            {
                "branch_id": "branch-dhk-main",
                "tenant_id": "tenant-dhaka-central",
                "name": "Main Dispensary (Kakrail)",
                "license_number": "DGDA-DL-DHK-2024-8891",
                "address": "Kakrail Mor, Dhaka 1000",
                "is_active": True,
            },
            {
                "branch_id": "branch-dhk-dhanmondi",
                "tenant_id": "tenant-dhaka-central",
                "name": "Dhanmondi Model Pharmacy",
                "license_number": "DGDA-DL-DHK-2024-8892",
                "address": "Road 27, Dhanmondi, Dhaka 1209",
                "is_active": True,
            },
            {
                "branch_id": "branch-ctg-main",
                "tenant_id": "tenant-chittagong-care",
                "name": "GEC Circle Branch",
                "license_number": "DGDA-DL-CTG-2024-4412",
                "address": "GEC Circle, Chittagong 4000",
                "is_active": True,
            },
        ]
        for d in branches_data:
            Branch.objects.update_or_create(branch_id=d["branch_id"], defaults=d)
        return len(branches_data)

    @classmethod
    def seed_medicines(cls) -> int:
        medicines_data = [
            {
                "product_id": "PROD-NAPA-500",
                "brand_name": "Napa",
                "generic_name": "Paracetamol",
                "dosage_form": "Tablet",
                "strength": "500mg",
                "manufacturer": "Square Pharmaceuticals Ltd.",
                "mrp_unit": Decimal("1.20"),
                "cost_unit": Decimal("0.95"),
                "requires_cold_chain": False,
            },
            {
                "product_id": "PROD-SECLO-20",
                "brand_name": "Seclo",
                "generic_name": "Omeprazole",
                "dosage_form": "Capsule",
                "strength": "20mg",
                "manufacturer": "Square Pharmaceuticals Ltd.",
                "mrp_unit": Decimal("7.00"),
                "cost_unit": Decimal("5.40"),
                "requires_cold_chain": False,
            },
            {
                "product_id": "PROD-MOXACIL-500",
                "brand_name": "Moxacil",
                "generic_name": "Amoxicillin",
                "dosage_form": "Capsule",
                "strength": "500mg",
                "manufacturer": "Square Pharmaceuticals Ltd.",
                "mrp_unit": Decimal("8.50"),
                "cost_unit": Decimal("6.80"),
                "requires_cold_chain": False,
            },
            {
                "product_id": "PROD-MAXPRO-20",
                "brand_name": "Maxpro",
                "generic_name": "Esomeprazole",
                "dosage_form": "Tablet",
                "strength": "20mg",
                "manufacturer": "Renata Limited",
                "mrp_unit": Decimal("9.00"),
                "cost_unit": Decimal("7.10"),
                "requires_cold_chain": False,
            },
            {
                "product_id": "PROD-AZITHRO-500",
                "brand_name": "Zimax",
                "generic_name": "Azithromycin",
                "dosage_form": "Tablet",
                "strength": "500mg",
                "manufacturer": "Beximco Pharmaceuticals Ltd.",
                "mrp_unit": Decimal("35.00"),
                "cost_unit": Decimal("28.00"),
                "requires_cold_chain": False,
            },
            {
                "product_id": "PROD-LANTUS-100",
                "brand_name": "Lantus Solostar",
                "generic_name": "Insulin Glargine",
                "dosage_form": "Injection",
                "strength": "100 IU/ml",
                "manufacturer": "Sanofi Bangladesh Ltd.",
                "mrp_unit": Decimal("950.00"),
                "cost_unit": Decimal("820.00"),
                "requires_cold_chain": True,
                "min_temp_celsius": Decimal("2.00"),
                "max_temp_celsius": Decimal("8.00"),
            },
        ]
        for d in medicines_data:
            MedicineProduct.objects.update_or_create(product_id=d["product_id"], defaults=d)
        return len(medicines_data)

    @classmethod
    def seed_batches(cls) -> int:
        now_date = timezone.now().date()
        batches_data = [
            # Napa active lots
            {
                "batch_id": "BATCH-NAPA-2027A",
                "tenant_id": "tenant-dhaka-central",
                "branch_id": "branch-dhk-main",
                "product_id": "PROD-NAPA-500",
                "batch_number": "NP27A01",
                "quantity_base_units": 1500,
                "expiry_date": now_date + datetime.timedelta(days=365),
                "purchase_date": now_date - datetime.timedelta(days=30),
                "cost_per_unit": Decimal("0.95"),
                "status": BatchStatus.ACTIVE,
            },
            {
                "batch_id": "BATCH-NAPA-2028B",
                "tenant_id": "tenant-dhaka-central",
                "branch_id": "branch-dhk-main",
                "product_id": "PROD-NAPA-500",
                "batch_number": "NP28B02",
                "quantity_base_units": 2000,
                "expiry_date": now_date + datetime.timedelta(days=730),
                "purchase_date": now_date - datetime.timedelta(days=10),
                "cost_per_unit": Decimal("0.95"),
                "status": BatchStatus.ACTIVE,
            },
            # Seclo active lot
            {
                "batch_id": "BATCH-SECLO-2027",
                "tenant_id": "tenant-dhaka-central",
                "branch_id": "branch-dhk-main",
                "product_id": "PROD-SECLO-20",
                "batch_number": "SC2701",
                "quantity_base_units": 800,
                "expiry_date": now_date + datetime.timedelta(days=400),
                "purchase_date": now_date - datetime.timedelta(days=20),
                "cost_per_unit": Decimal("5.40"),
                "status": BatchStatus.ACTIVE,
            },
            # Moxacil antibiotic lot
            {
                "batch_id": "BATCH-MOX-2027",
                "tenant_id": "tenant-dhaka-central",
                "branch_id": "branch-dhk-main",
                "product_id": "PROD-MOXACIL-500",
                "batch_number": "MX2701",
                "quantity_base_units": 600,
                "expiry_date": now_date + datetime.timedelta(days=300),
                "purchase_date": now_date - datetime.timedelta(days=15),
                "cost_per_unit": Decimal("6.80"),
                "status": BatchStatus.ACTIVE,
            },
            # Cold-Chain Insulin lot
            {
                "batch_id": "BATCH-LANTUS-2026",
                "tenant_id": "tenant-dhaka-central",
                "branch_id": "branch-dhk-main",
                "product_id": "PROD-LANTUS-100",
                "batch_number": "LT2601",
                "quantity_base_units": 50,
                "expiry_date": now_date + datetime.timedelta(days=180),
                "purchase_date": now_date - datetime.timedelta(days=5),
                "cost_per_unit": Decimal("820.00"),
                "status": BatchStatus.ACTIVE,
            },
            # Expired Napa lot for quarantine testing
            {
                "batch_id": "BATCH-NAPA-EXPIRED",
                "tenant_id": "tenant-dhaka-central",
                "branch_id": "branch-dhk-main",
                "product_id": "PROD-NAPA-500",
                "batch_number": "NP23EXP",
                "quantity_base_units": 100,
                "expiry_date": now_date - datetime.timedelta(days=60),  # Expired 60 days ago
                "purchase_date": now_date - datetime.timedelta(days=400),
                "cost_per_unit": Decimal("0.90"),
                "status": BatchStatus.EXPIRED,
            },
        ]
        for d in batches_data:
            BatchStock.objects.update_or_create(batch_id=d["batch_id"], defaults=d)
        return len(batches_data)

    @classmethod
    def seed_invoices(cls) -> int:
        SaleInvoice.objects.exclude(invoice_id="INV-DHK-2026-0001").delete()
        invoice_data = {
            "invoice_id": "INV-DHK-2026-0001",
            "tenant_id": "tenant-dhaka-central",
            "branch_id": "branch-dhk-main",
            "invoice_number": "INV-20261029-001",
            "customer_name": "Rahim Uddin",
            "customer_phone": "01700000000",  # Synthetic dummy phone
            "total_amount": Decimal("154.00"),
            "tax_amount": Decimal("0.00"),
            "discount_amount": Decimal("4.00"),
            "net_amount": Decimal("150.00"),
            "payment_method": PaymentMethod.BKASH,
            "status": InvoiceStatus.FINALIZED,
        }
        invoice, _ = SaleInvoice.objects.update_or_create(invoice_id=invoice_data["invoice_id"], defaults=invoice_data)

        # Invoice Line Items
        # 1. 20 Napa Tablets (20 * 1.20 = 24.00 BDT)
        SaleInvoiceItem.objects.update_or_create(
            invoice=invoice,
            product_id="PROD-NAPA-500",
            defaults={
                "batch_id": "BATCH-NAPA-2027A",
                "quantity": 20,
                "unit_price": Decimal("1.20"),
                "subtotal": Decimal("24.00"),
            },
        )
        # 2. 10 Seclo Capsules (10 * 7.00 = 70.00 BDT)
        SaleInvoiceItem.objects.update_or_create(
            invoice=invoice,
            product_id="PROD-SECLO-20",
            defaults={
                "batch_id": "BATCH-SECLO-2027",
                "quantity": 10,
                "unit_price": Decimal("7.00"),
                "subtotal": Decimal("70.00"),
            },
        )
        # 3. 7 Moxacil Capsules (7 * 8.50 = 59.50 -> rounded 60.00)
        SaleInvoiceItem.objects.update_or_create(
            invoice=invoice,
            product_id="PROD-MOXACIL-500",
            defaults={
                "batch_id": "BATCH-MOX-2027",
                "quantity": 7,
                "unit_price": Decimal("8.50"),
                "subtotal": Decimal("60.00"),
            },
        )

        return 1


class SQLiteBootstrapEngine:
    """Orchestrates database migration, deterministic seeding, and summary statistics."""

    @classmethod
    def enable_foreign_keys_pragma(cls) -> None:
        """Enforces SQLite foreign key constraints."""
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA foreign_keys = ON;")

    @classmethod
    def run_migrations(cls) -> None:
        """Applies all pending Django migrations to the configured database."""
        cls.enable_foreign_keys_pragma()
        call_command("migrate", interactive=False)

    @classmethod
    def seed_development_data(cls) -> Dict[str, int]:
        """Seeds deterministic fixtures and returns record counts."""
        cls.enable_foreign_keys_pragma()
        return DeterministicDatabaseSeeder.seed_all()

    @classmethod
    def bootstrap(cls) -> Dict[str, Any]:
        """Runs full bootstrap: migrations + seed data + statistics."""
        cls.run_migrations()
        counts = cls.seed_development_data()
        stats = cls.get_database_stats()
        return {
            "status": "BOOTSTRAPPED",
            "engine": connection.vendor,
            "seed_counts": counts,
            "statistics": stats,
        }

    @classmethod
    def get_database_stats(cls) -> Dict[str, Any]:
        """Collects database inventory and financial metrics."""
        cls.enable_foreign_keys_pragma()
        total_batches = BatchStock.objects.count()
        active_batches = BatchStock.objects.filter(status=BatchStatus.ACTIVE).count()
        expired_batches = BatchStock.objects.filter(status=BatchStatus.EXPIRED).count()

        # Calculate total stock valuation using strict Decimal arithmetic
        total_valuation = Decimal("0.00")
        for b in BatchStock.objects.all():
            total_valuation += Decimal(str(b.quantity_base_units)) * b.cost_per_unit

        total_sales = Decimal("0.00")
        for inv in SaleInvoice.objects.all():
            total_sales += inv.net_amount

        return {
            "tenants_count": Tenant.objects.count(),
            "branches_count": Branch.objects.count(),
            "medicines_count": MedicineProduct.objects.count(),
            "batches_total": total_batches,
            "batches_active": active_batches,
            "batches_expired": expired_batches,
            "invoices_count": SaleInvoice.objects.count(),
            "total_inventory_valuation_bdt": str(total_valuation),
            "total_sales_volume_bdt": str(total_sales),
        }


# ==============================================================================
# POSTGRESQL PARITY LANE
# ==============================================================================
class PostgreSQLParityLane:
    """Inspects PostgreSQL service availability and validates dialect portability."""

    @classmethod
    def probe_postgres_service(cls) -> Tuple[bool, str]:
        """Probes whether a live PostgreSQL instance is configured and reachable."""
        from django.conf import settings

        db_conf = settings.DATABASES.get("default", {})
        engine = db_conf.get("ENGINE", "")

        if "postgresql" not in engine:
            return (
                False,
                f"Active Django DB_ENGINE is '{connection.vendor}'. "
                f"To run PostgreSQL integration lane, set DB_ENGINE=postgres and ensure PostgreSQL is running.",
            )

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT version();")
                version_info = cursor.fetchone()[0]
                return True, f"PostgreSQL live: {version_info}"
        except Exception as e:
            return False, f"PostgreSQL connection failed: {e}"

    @classmethod
    def evaluate_concurrency_locking_contract(cls) -> Dict[str, Any]:
        """
        Reports concurrency locking behavior based on active engine:
        - SQLite: Locks whole DB on write; select_for_update() is a no-op.
        - PostgreSQL: Emits row-level SELECT ... FOR UPDATE, preventing cashier race conditions.
        """
        vendor = connection.vendor
        if vendor == "postgresql":
            return {
                "vendor": "postgresql",
                "row_level_locking_active": True,
                "concurrency_safe": True,
                "notes": "PostgreSQL provides true row-level locks on BatchStock for multi-terminal checkouts.",
            }
        else:
            return {
                "vendor": "sqlite",
                "row_level_locking_active": False,
                "concurrency_safe": False,
                "notes": (
                    "SQLite does not support row-level locks. select_for_update() operates as a no-op. "
                    "Multi-terminal concurrent checkout race tests must execute in the PostgreSQL CI lane."
                ),
            }
