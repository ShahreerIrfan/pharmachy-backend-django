# Generated for P267 - Customer statements

from decimal import Decimal
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0173_customerdueinvoiceallocation'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CustomerStatementAudit',
            fields=[
                ('statement_id', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('period_start', models.DateField(blank=True, null=True)),
                ('period_end', models.DateField(blank=True, null=True)),
                ('opening_balance_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('total_charges_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('total_payments_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('closing_balance_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('aging_current_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('aging_31_60_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('aging_61_90_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('aging_91_plus_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('is_parity_reconciled', models.BooleanField(default=True)),
                ('generated_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('print_count', models.PositiveIntegerField(default=1)),
                ('last_printed_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='customer_statement_audits', to='core.branch')),
                ('customer', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='statement_audits', to='core.poscustomer')),
                ('generated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='generated_customer_statements', to=settings.AUTH_USER_MODEL)),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='customer_statement_audits', to='core.tenant')),
            ],
            options={
                'verbose_name': 'Customer Statement Audit',
                'verbose_name_plural': 'Customer Statement Audits',
                'ordering': ['-generated_at', '-statement_id'],
            },
        ),
        migrations.AddIndex(
            model_name='customerstatementaudit',
            index=models.Index(fields=['tenant', 'customer', '-generated_at'], name='core_custom_tenant__3c3cd1_idx'),
        ),
        migrations.AddIndex(
            model_name='customerstatementaudit',
            index=models.Index(fields=['tenant', 'branch', '-generated_at'], name='core_custom_tenant__a7e01d_idx'),
        ),
    ]
