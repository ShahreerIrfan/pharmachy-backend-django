# Generated for P098 Platform MRR and Billing Reports (ADR-0098)

from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0030_device_seat_management'),
    ]

    operations = [
        migrations.CreateModel(
            name='PlatformBillingReportSnapshot',
            fields=[
                ('report_id', models.CharField(max_length=64, primary_key=True, serialize=False)),
                (
                    'report_type',
                    models.CharField(
                        choices=[
                            ('FULL_SUMMARY', 'Full Platform Summary'),
                            ('MRR_CHURN', 'MRR & Churn Report'),
                            ('COLLECTIONS', 'Verified Collections Report'),
                            ('OUTSTANDING_INVOICES', 'Outstanding Invoices & Aging Report'),
                            ('SUBSCRIPTION_AUDIT', 'Subscription Audit Trail Report'),
                        ],
                        db_index=True,
                        default='FULL_SUMMARY',
                        max_length=32,
                    ),
                ),
                (
                    'period',
                    models.CharField(
                        choices=[
                            ('CURRENT', 'Current Month / Period'),
                            ('MONTHLY', 'Monthly'),
                            ('QUARTERLY', 'Quarterly'),
                            ('ANNUAL', 'Annual'),
                            ('CUSTOM', 'Custom Date Range'),
                        ],
                        db_index=True,
                        default='CURRENT',
                        max_length=16,
                    ),
                ),
                ('start_date', models.DateField()),
                ('end_date', models.DateField()),
                ('currency', models.CharField(default='BDT', max_length=8)),
                ('total_mrr_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('total_arr_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('arpu_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('total_collected_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('total_outstanding_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('churn_rate_pct', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=6)),
                ('active_subscriptions_count', models.PositiveIntegerField(default=0)),
                ('churned_subscriptions_count', models.PositiveIntegerField(default=0)),
                ('outstanding_invoices_count', models.PositiveIntegerField(default=0)),
                ('mrr_breakdown_json', models.JSONField(blank=True, default=dict)),
                ('churn_details_json', models.JSONField(blank=True, default=dict)),
                ('collections_breakdown_json', models.JSONField(blank=True, default=dict)),
                ('aging_brackets_json', models.JSONField(blank=True, default=dict)),
                ('audit_events_sample_json', models.JSONField(blank=True, default=list)),
                ('generated_by_user_id', models.CharField(blank=True, default='', max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'Platform Billing Report Snapshot',
                'verbose_name_plural': 'Platform Billing Report Snapshots',
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['report_type', 'created_at'], name='core_platfo_report__f23349_idx'),
                    models.Index(fields=['period', 'start_date', 'end_date'], name='core_platfo_period_d288a7_idx'),
                ],
            },
        ),
    ]
