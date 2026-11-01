# Generated for P100 Subscription E2E and Support Exceptions (ADR-0100)

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0032_dunning_notification'),
    ]

    operations = [
        migrations.CreateModel(
            name='SubscriptionSupportOverride',
            fields=[
                ('override_id', models.CharField(max_length=64, primary_key=True, serialize=False)),
                (
                    'override_type',
                    models.CharField(
                        choices=[
                            ('GRACE_EXTENSION', 'Grace Extension'),
                            ('EMERGENCY_UNBLOCK', 'Emergency Unblock'),
                            ('QUOTA_OVERRIDE', 'Quota Override'),
                            ('REACTIVATION', 'Reactivation'),
                        ],
                        db_index=True,
                        default='GRACE_EXTENSION',
                        max_length=32,
                    ),
                ),
                (
                    'status',
                    models.CharField(
                        choices=[
                            ('ACTIVE', 'Active'),
                            ('EXPIRED', 'Expired'),
                            ('REVOKED', 'Revoked'),
                        ],
                        db_index=True,
                        default='ACTIVE',
                        max_length=16,
                    ),
                ),
                ('extension_days', models.PositiveIntegerField(blank=True, default=7, null=True)),
                ('quota_bonus', models.PositiveIntegerField(blank=True, default=0, null=True)),
                ('justification', models.TextField()),
                ('ticket_reference', models.CharField(db_index=True, max_length=64)),
                ('granted_by_user_id', models.CharField(max_length=64)),
                ('granted_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('revoked_by_user_id', models.CharField(blank=True, default='', max_length=64)),
                ('revocation_reason', models.TextField(blank=True, default='')),
                ('metadata_json', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'subscription',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='subscription_overrides',
                        to='core.subscriptionrecord',
                    ),
                ),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='subscription_overrides',
                        to='core.tenant',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Subscription Support Override',
                'verbose_name_plural': 'Subscription Support Overrides',
                'ordering': ['-granted_at'],
                'indexes': [
                    models.Index(fields=['tenant', 'status'], name='core_subscr_tenant__23bb01_idx'),
                    models.Index(fields=['tenant', 'override_type', 'status'], name='core_subscr_tenant__48dc02_idx'),
                    models.Index(fields=['expires_at', 'status'], name='core_subscr_expires_77dd03_idx'),
                ],
            },
        ),
    ]
