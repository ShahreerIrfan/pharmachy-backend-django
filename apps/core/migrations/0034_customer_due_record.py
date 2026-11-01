# Generated for P101 Owner Scoped Dashboard and Customer Dues (ADR-0101)

from decimal import Decimal

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0033_subscription_support_override'),
    ]

    operations = [
        migrations.AlterField(
            model_name='saleinvoice',
            name='payment_method',
            field=models.CharField(
                choices=[
                    ('CASH', 'Cash'),
                    ('BKASH', 'bKash'),
                    ('NAGAD', 'Nagad'),
                    ('CARD', 'Card'),
                    ('DUE', 'Due / Credit'),
                ],
                default='CASH',
                max_length=32,
            ),
        ),
        migrations.CreateModel(
            name='CustomerDueRecord',
            fields=[
                ('due_id', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('customer_name', models.CharField(max_length=255)),
                ('customer_phone', models.CharField(db_index=True, max_length=32)),
                (
                    'total_due_bdt',
                    models.DecimalField(
                        decimal_places=2,
                        default=Decimal('0.00'),
                        max_digits=12,
                        validators=[django.core.validators.MinValueValidator(Decimal('0.00'))],
                    ),
                ),
                (
                    'paid_amount_bdt',
                    models.DecimalField(
                        decimal_places=2,
                        default=Decimal('0.00'),
                        max_digits=12,
                        validators=[django.core.validators.MinValueValidator(Decimal('0.00'))],
                    ),
                ),
                (
                    'remaining_due_bdt',
                    models.DecimalField(
                        decimal_places=2,
                        default=Decimal('0.00'),
                        max_digits=12,
                        validators=[django.core.validators.MinValueValidator(Decimal('0.00'))],
                    ),
                ),
                (
                    'status',
                    models.CharField(
                        choices=[
                            ('OUTSTANDING', 'Outstanding'),
                            ('PARTIAL', 'Partially Paid'),
                            ('SETTLED', 'Settled'),
                            ('WRITTEN_OFF', 'Written Off'),
                        ],
                        db_index=True,
                        default='OUTSTANDING',
                        max_length=32,
                    ),
                ),
                ('due_date', models.DateField(blank=True, null=True)),
                ('notes', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'branch',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='customer_dues',
                        to='core.branch',
                    ),
                ),
                (
                    'invoice',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='customer_dues',
                        to='core.saleinvoice',
                    ),
                ),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='customer_dues',
                        to='core.tenant',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Customer Due Record',
                'verbose_name_plural': 'Customer Due Records',
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['tenant', 'status'], name='core_custdue_tenant__23bb01_idx'),
                    models.Index(fields=['tenant', 'customer_phone'], name='core_custdue_tenant__48dc02_idx'),
                    models.Index(fields=['branch', 'status'], name='core_custdue_branch__77dd03_idx'),
                ],
            },
        ),
    ]
