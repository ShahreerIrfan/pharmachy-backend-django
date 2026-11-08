from decimal import Decimal
import django.core.validators
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0118_supply_chain_traceability"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PurchaseDocument",
            fields=[
                ("document_id", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("document_type", models.CharField(choices=[("PURCHASE_ORDER", "Purchase Order"), ("GOODS_RECEIPT_NOTE", "Goods Receipt Note"), ("PURCHASE_RETURN", "Purchase Return Voucher"), ("SUPPLIER_STATEMENT", "Supplier Statement of Account")], db_index=True, max_length=32)),
                ("document_number", models.CharField(db_index=True, help_text="Canonical document sequence number (e.g. DOC-PO-202611-0001)", max_length=64)),
                ("version", models.PositiveIntegerField(default=1, help_text="Monotonically increasing version number for document regenerations")),
                ("is_latest", models.BooleanField(db_index=True, default=True, help_text="True if this document is the current active version")),
                ("source_id", models.CharField(db_index=True, help_text="ID of the underlying entity (po_id, grn_id, return_id, or supplier_id)", max_length=64)),
                ("source_reference", models.CharField(db_index=True, help_text="Human readable reference of underlying entity (e.g. PO-001, GRN-001)", max_length=64)),
                ("supplier_name", models.CharField(blank=True, default="", max_length=255)),
                ("document_format", models.CharField(choices=[("A4_PDF", "A4 PDF Document"), ("THERMAL_80MM", "80mm Thermal Print"), ("JSON_DATA", "JSON Data Feed")], default="A4_PDF", max_length=32)),
                ("title", models.CharField(max_length=255)),
                ("title_bn", models.CharField(blank=True, default="", max_length=255)),
                ("total_amount", models.DecimalField(decimal_places=4, default=Decimal("0.0000"), max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal("0.0000"))])),
                ("currency", models.CharField(default="BDT", max_length=8)),
                ("document_payload", models.JSONField(default=dict, help_text="Full immutable snapshot of all headers, lines, calculations, and terms")),
                ("immutable_verification_hash", models.CharField(blank=True, default="", help_text="Cryptographic SHA-256 hash of document payload for tamper detection", max_length=64)),
                ("pdf_file_path", models.CharField(blank=True, default="", help_text="Local path or storage key of generated PDF", max_length=512)),
                ("pdf_size_bytes", models.PositiveIntegerField(default=0, help_text="Byte length of the generated PDF file")),
                ("status", models.CharField(choices=[("DRAFT", "Draft"), ("FINAL", "Final Published"), ("SUPERSEDED", "Superseded by Newer Version"), ("VOID", "Voided")], db_index=True, default="FINAL", max_length=32)),
                ("notes", models.TextField(blank=True, default="")),
                ("generated_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("branch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="purchase_documents", to="core.branch")),
                ("generated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="generated_purchase_documents", to=settings.AUTH_USER_MODEL)),
                ("supplier", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="purchase_documents", to="core.supplier")),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="purchase_documents", to="core.tenant")),
            ],
            options={
                "verbose_name": "Purchase Document",
                "verbose_name_plural": "Purchase Documents",
                "ordering": ["-generated_at", "-version"],
            },
        ),
        migrations.AddIndex(
            model_name="purchasedocument",
            index=models.Index(fields=["tenant", "document_type", "source_id"], name="core_purcha_tenant__0d34fa_idx"),
        ),
        migrations.AddIndex(
            model_name="purchasedocument",
            index=models.Index(fields=["tenant", "document_type", "is_latest"], name="core_purcha_tenant__fa9921_idx"),
        ),
        migrations.AddIndex(
            model_name="purchasedocument",
            index=models.Index(fields=["tenant", "supplier", "document_type"], name="core_purcha_tenant__109283_idx"),
        ),
        migrations.AddIndex(
            model_name="purchasedocument",
            index=models.Index(fields=["tenant", "document_number"], name="core_purcha_tenant__7532a1_idx"),
        ),
        migrations.AlterUniqueTogether(
            name="purchasedocument",
            unique_together={("tenant", "document_type", "source_id", "version")},
        ),
    ]

