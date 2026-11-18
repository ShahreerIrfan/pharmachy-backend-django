# Generated for P266 - Partial collections

from decimal import Decimal
import django.core.validators
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0172_customerreceivableledgerentry'),
    ]

    operations = [
        migrations.AddField(
            model_name='customerduecollection',
            name='invoice',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='due_collections',
                to='core.saleinvoice',
            ),
        ),
        migrations.CreateModel(
            name='CustomerDueInvoiceAllocation',
            fields=[
                ('allocation_id', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('allocated_amount_bdt', models.DecimalField(
                    decimal_places=2,
                    max_digits=14,
                    validators=[django.core.validators.MinValueValidator(Decimal('0.01'))],
                )),
                ('previous_remaining_due_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('new_remaining_due_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('is_reversed', models.BooleanField(db_index=True, default=False)),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='due_invoice_allocations', to='core.branch')),
                ('collection', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='invoice_allocations', to='core.customerduecollection')),
                ('due_record', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='invoice_allocations', to='core.customerduerecord')),
                ('invoice', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='due_customer_allocations', to='core.saleinvoice')),
                ('sale', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='due_allocations', to='core.possaleorder')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='due_invoice_allocations', to='core.tenant')),
            ],
            options={
                'verbose_name': 'Customer Due Invoice Allocation',
                'verbose_name_plural': 'Customer Due Invoice Allocations',
                'ordering': ['-created_at', '-allocation_id'],
            },
        ),
        migrations.AddIndex(
            model_name='customerdueinvoiceallocation',
            index=models.Index(fields=['tenant', 'branch', '-created_at'], name='core_custom_tenant__a5fa67_idx'),
        ),
        migrations.AddIndex(
            model_name='customerdueinvoiceallocation',
            index=models.Index(fields=['tenant', 'due_record', 'is_reversed'], name='core_custom_tenant__60ba2b_idx'),
        ),
        migrations.AddIndex(
            model_name='customerdueinvoiceallocation',
            index=models.Index(fields=['tenant', 'invoice', 'is_reversed'], name='core_custom_tenant__d64216_idx'),
        ),
        migrations.AddIndex(
            model_name='customerdueinvoiceallocation',
            index=models.Index(fields=['collection', 'due_record'], name='core_custom_collect_47eefa_idx'),
        ),
        migrations.AddConstraint(
            model_name='customerdueinvoiceallocation',
            constraint=models.CheckConstraint(
                condition=models.Q(('allocated_amount_bdt__gt', Decimal('0.00'))),
                name='chk_due_alloc_amt_pos',
            ),
        ),
    ]
