# Generated for P099 Dunning Notifications (ADR-0099)

from decimal import Decimal
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0031_platform_billing_report_snapshot'),
    ]

    operations = [
        migrations.CreateModel(
            name='DunningNotification',
            fields=[
                ('notification_id', models.CharField(max_length=64, primary_key=True, serialize=False)),
                (
                    'stage',
                    models.CharField(
                        choices=[
                            ('UPCOMING_RENEWAL', 'Upcoming Renewal Notice'),
                            ('PAYMENT_FAILED', 'Payment Failure Alert'),
                            ('GRACE_PERIOD_ACTIVE', 'Grace Period Warning'),
                            ('SUBSCRIPTION_EXPIRED', 'Subscription Expired'),
                            ('ACCOUNT_SUSPENDED', 'Account Suspended'),
                            ('PAYMENT_RECOVERED', 'Payment Recovered & Restored'),
                        ],
                        db_index=True,
                        default='UPCOMING_RENEWAL',
                        max_length=32,
                    ),
                ),
                (
                    'channel',
                    models.CharField(
                        choices=[
                            ('IN_APP', 'In-App Banner / Notification'),
                            ('SMS', 'SMS Message'),
                            ('EMAIL', 'Email Notification'),
                        ],
                        default='IN_APP',
                        max_length=16,
                    ),
                ),
                (
                    'delivery_status',
                    models.CharField(
                        choices=[
                            ('PENDING', 'Pending Delivery'),
                            ('DELIVERED', 'Delivered / Sent'),
                            ('FAILED', 'Delivery Failed'),
                            ('SUPPRESSED', 'Suppressed (De-duplicated)'),
                        ],
                        db_index=True,
                        default='DELIVERED',
                        max_length=16,
                    ),
                ),
                ('dedup_key', models.CharField(db_index=True, max_length=128)),
                ('title_en', models.CharField(max_length=255)),
                ('title_bn', models.CharField(max_length=255)),
                ('body_en', models.TextField()),
                ('body_bn', models.TextField()),
                ('amount_due_bdt', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12)),
                ('recipient_email', models.EmailField(blank=True, default='', max_length=255)),
                ('recipient_phone', models.CharField(blank=True, default='', max_length=64)),
                ('is_acknowledged', models.BooleanField(db_index=True, default=False)),
                ('acknowledged_at', models.DateTimeField(blank=True, null=True)),
                ('acknowledged_by_user_id', models.CharField(blank=True, default='', max_length=64)),
                ('retry_count', models.PositiveIntegerField(default=0)),
                ('max_retries', models.PositiveIntegerField(default=3)),
                ('next_retry_at', models.DateTimeField(blank=True, null=True)),
                ('last_error_message', models.TextField(blank=True, default='')),
                ('metadata_json', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'invoice',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='dunning_notifications',
                        to='core.subscriptionrenewalinvoice',
                    ),
                ),
                (
                    'subscription',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='dunning_notifications',
                        to='core.subscriptionrecord',
                    ),
                ),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='dunning_notifications',
                        to='core.tenant',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Dunning Notification',
                'verbose_name_plural': 'Dunning Notifications',
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['tenant', 'is_acknowledged'], name='core_dunnin_tenant__ec1c1e_idx'),
                    models.Index(fields=['tenant', 'stage'], name='core_dunnin_tenant__70ae9a_idx'),
                    models.Index(fields=['dedup_key', 'delivery_status'], name='core_dunnin_dedup_k_51b9e2_idx'),
                ],
            },
        ),
    ]
