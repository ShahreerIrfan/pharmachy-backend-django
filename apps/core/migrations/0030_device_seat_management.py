# Generated for P096 Device Seats Management (ADR-0096)

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0029_tenant_quota_usage_counter'),
    ]

    operations = [
        migrations.CreateModel(
            name='DeviceSeat',
            fields=[
                ('device_id', models.CharField(max_length=128, primary_key=True, serialize=False)),
                ('device_name', models.CharField(max_length=255)),
                (
                    'device_type',
                    models.CharField(
                        choices=[
                            ('DESKTOP_POS', 'Desktop POS Workstation'),
                            ('TABLET_POS', 'Tablet POS Terminal'),
                            ('MOBILE_SCANNER', 'Mobile Barcode Scanner'),
                            ('COUNTER_TERMINAL', 'Counter Billing Terminal'),
                        ],
                        db_index=True,
                        default='DESKTOP_POS',
                        max_length=64,
                    ),
                ),
                (
                    'status',
                    models.CharField(
                        choices=[
                            ('ACTIVE', 'Active Enrolled Workstation'),
                            ('REVOKED', 'Revoked / Decommissioned'),
                            ('SUSPENDED', 'Suspended'),
                            ('DECOMMISSIONED', 'Permanently Decommissioned'),
                        ],
                        db_index=True,
                        default='ACTIVE',
                        max_length=32,
                    ),
                ),
                ('mac_address', models.CharField(blank=True, default='', max_length=64)),
                ('ip_address', models.CharField(blank=True, default='', max_length=64)),
                ('hardware_fingerprint', models.CharField(blank=True, default='', max_length=256)),
                ('enrolled_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('enrolled_by', models.CharField(blank=True, default='', max_length=128)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('revoked_by', models.CharField(blank=True, default='', max_length=128)),
                ('revocation_reason', models.TextField(blank=True, default='')),
                ('last_heartbeat_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'branch',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='device_seats',
                        to='core.branch',
                    ),
                ),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='device_seats',
                        to='core.tenant',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Device Seat',
                'verbose_name_plural': 'Device Seats',
                'ordering': ['-enrolled_at'],
                'indexes': [
                    models.Index(fields=['tenant', 'status'], name='core_device_tenant__3d8b1e_idx'),
                    models.Index(fields=['branch', 'status'], name='core_device_branch__4f1c9a_idx'),
                    models.Index(fields=['tenant', 'branch'], name='core_device_tenant__9b52a0_idx'),
                ],
            },
        ),
    ]

