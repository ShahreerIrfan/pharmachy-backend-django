from decimal import Decimal
import django.core.validators
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0119_purchase_document_generation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SupplierDashboardSnapshot",
            fields=[
                ("snapshot_id", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("snapshot_date", models.DateField(db_index=True, default=django.utils.timezone.now)),
                ("open_orders_count", models.PositiveIntegerField(default=0)),
                ("open_orders_value", models.DecimalField(decimal_places=4, default=Decimal("0.0000"), max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal("0.0000"))])),
                ("overdue_deliveries_count", models.PositiveIntegerField(default=0)),
                ("overdue_deliveries_value", models.DecimalField(decimal_places=4, default=Decimal("0.0000"), max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal("0.0000"))])),
                ("total_due_balance", models.DecimalField(decimal_places=4, default=Decimal("0.0000"), max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal("0.0000"))])),
                ("overdue_due_balance", models.DecimalField(decimal_places=4, default=Decimal("0.0000"), max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal("0.0000"))])),
                ("low_stock_opportunities_count", models.PositiveIntegerField(default=0)),
                ("estimated_reorder_cost", models.DecimalField(decimal_places=4, default=Decimal("0.0000"), max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal("0.0000"))])),
                ("active_suppliers_count", models.PositiveIntegerField(default=0)),
                ("dashboard_metrics", models.JSONField(default=dict, help_text="Detailed JSON payload including open order items, overdue list, due balances by supplier, and sourcing opportunities.")),
                ("immutable_verification_hash", models.CharField(blank=True, default="", help_text="Cryptographic SHA-256 hash of metrics payload.", max_length=64)),
                ("notes", models.TextField(blank=True, default="")),
                ("generated_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("branch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="supplier_dashboard_snapshots", to="core.branch")),
                ("generated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="generated_supplier_snapshots", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="supplier_dashboard_snapshots", to="core.tenant")),
            ],
            options={
                "verbose_name": "Supplier Dashboard Snapshot",
                "verbose_name_plural": "Supplier Dashboard Snapshots",
                "ordering": ["-generated_at"],
            },
        ),
        migrations.AddIndex(
            model_name="supplierdashboardsnapshot",
            index=models.Index(fields=["tenant", "snapshot_date"], name="core_suppli_tenant__7f98ab_idx"),
        ),
        migrations.AddIndex(
            model_name="supplierdashboardsnapshot",
            index=models.Index(fields=["tenant", "branch", "snapshot_date"], name="core_suppli_tenant__8a14bc_idx"),
        ),
    ]

