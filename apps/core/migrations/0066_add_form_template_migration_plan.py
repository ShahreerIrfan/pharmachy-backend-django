# Generated manually for ADR-0139 (P139) Form Template Migration Plan

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0065_add_catalogue_lifecycle_notice'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='FormTemplateMigrationPlan',
            fields=[
                ('plan_id', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('plan_name', models.CharField(max_length=255)),
                ('strategy', models.CharField(choices=[('STRICT', 'Strict Schema Match'), ('COERCE_SAFE', 'Safe Type Coercion and Default Injection'), ('DRY_RUN_ONLY', 'Dry Run Only (No DB Writes)')], db_index=True, default='COERCE_SAFE', max_length=32)),
                ('status', models.CharField(choices=[('DRAFT', 'Draft Plan'), ('VALIDATED', 'Dry Run Validated'), ('EXECUTING', 'Migration Executing'), ('COMPLETED', 'Migration Completed'), ('ROLLED_BACK', 'Migration Rolled Back'), ('FAILED', 'Migration Failed')], db_index=True, default='DRAFT', max_length=32)),
                ('field_mappings', models.JSONField(blank=True, default=dict)),
                ('dry_run_summary', models.JSONField(blank=True, default=dict)),
                ('affected_medicine_count', models.PositiveIntegerField(default=0)),
                ('migrated_medicine_count', models.PositiveIntegerField(default=0)),
                ('historical_dispensing_audited_count', models.PositiveIntegerField(default=0)),
                ('rollback_snapshot_payload', models.JSONField(blank=True, default=dict)),
                ('plan_hash', models.CharField(blank=True, default='', max_length=64)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('executed_at', models.DateTimeField(blank=True, null=True)),
                ('rolled_back_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_form_template_plans', to=settings.AUTH_USER_MODEL)),
                ('executed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='executed_form_template_plans', to=settings.AUTH_USER_MODEL)),
                ('from_template', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='source_migration_plans', to='core.medicineformtemplate')),
                ('tenant', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='form_template_migration_plans', to='core.tenant')),
                ('to_template', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='target_migration_plans', to='core.medicineformtemplate')),
            ],
            options={
                'verbose_name': 'Form Template Migration Plan',
                'verbose_name_plural': 'Form Template Migration Plans',
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['tenant', 'status', '-created_at'], name='core_form_t_tenant__832441_idx'),
                    models.Index(fields=['from_template', 'to_template'], name='core_form_t_from_te_273892_idx'),
                    models.Index(fields=['status', 'is_active'], name='core_form_t_status_473821_idx'),
                ],
            },
        ),
    ]
