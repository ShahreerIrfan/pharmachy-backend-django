# Generated for P265 - Receivable ledger

from decimal import Decimal
import django.core.validators
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0171_customercrediteligibilitypolicy'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CustomerReceivableLedgerEntry',
            fields=[
                ('entry_id', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('entry_type', models.CharField(choices=[('DUE_SALE', 'Credit Due Sale'), ('PAYMENT_COLLECTION', 'Payment Collection'), ('SALES_RETURN_CREDIT', 'Sales Return Credit'), ('ADJUSTMENT', 'Audit Adjustment'), ('REVERSAL', 'Compensating Reversal'), ('WRITE_OFF', 'Bad Debt Write-off')], db_index=True, max_length=32)),
                ('reference_number', models.CharField(db_index=True, help_text='Order number, invoice number, collection receipt ID, or return number.', max_length=128)),
                ('debit_amount_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), help_text='Increases customer receivable asset (credit due sales, debit adjustments).', max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))])),
                ('credit_amount_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), help_text='Decreases customer receivable asset (repayments, return credits, write-offs).', max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))])),
                ('balance_after_bdt', models.DecimalField(decimal_places=2, help_text='Running customer receivable balance owed after this posting.', max_digits=14)),
                ('currency', models.CharField(default='BDT', max_length=8)),
                ('narration', models.CharField(blank=True, default='', max_length=500)),
                ('idempotency_key', models.CharField(blank=True, db_index=True, default='', max_length=128)),
                ('posted_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='customer_receivable_ledger_entries', to='core.branch')),
                ('customer', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='receivable_ledger_entries', to='core.poscustomer')),
                ('posted_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='customer_receivable_postings', to=settings.AUTH_USER_MODEL)),
                ('source_collection', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='receivable_postings', to='core.customerduecollection')),
                ('source_due_record', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='receivable_postings', to='core.customerduerecord')),
                ('source_invoice', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='receivable_postings', to='core.saleinvoice')),
                ('source_return', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='receivable_postings', to='core.possalereturn')),
                ('source_sale', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='receivable_postings', to='core.possaleorder')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='customer_receivable_ledger_entries', to='core.tenant')),
            ],
            options={
                'verbose_name': 'Customer Receivable Ledger Entry',
                'verbose_name_plural': 'Customer Receivable Ledger Entries',
                'ordering': ['-posted_at'],
            },
        ),
        migrations.AddIndex(
            model_name='customerreceivableledgerentry',
            index=models.Index(fields=['tenant', 'customer', '-posted_at'], name='core_custom_tenant__c1e099_idx'),
        ),
        migrations.AddIndex(
            model_name='customerreceivableledgerentry',
            index=models.Index(fields=['tenant', 'reference_number'], name='core_custom_tenant__d92410_idx'),
        ),
        migrations.AddIndex(
            model_name='customerreceivableledgerentry',
            index=models.Index(fields=['tenant', 'entry_type', '-posted_at'], name='core_custom_tenant__23ff3a_idx'),
        ),
        migrations.AddConstraint(
            model_name='customerreceivableledgerentry',
            constraint=models.CheckConstraint(condition=models.Q(('debit_amount_bdt__gte', Decimal('0.00'))), name='chk_cust_ar_debit_nonneg'),
        ),
        migrations.AddConstraint(
            model_name='customerreceivableledgerentry',
            constraint=models.CheckConstraint(condition=models.Q(('credit_amount_bdt__gte', Decimal('0.00'))), name='chk_cust_ar_credit_nonneg'),
        ),
    ]
