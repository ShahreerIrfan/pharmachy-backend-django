# Generated for P264 - Customer credit eligibility

from decimal import Decimal
import django.core.validators
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0170_poscustomer_privacy_preferences'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CustomerCreditEligibilityPolicy',
            fields=[
                ('policy_id', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('max_customer_credit_limit_bdt', models.DecimalField(decimal_places=2, default=Decimal('10000.00'), help_text='Upper ceiling on individual customer credit limit allowed to be granted.', max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))])),
                ('branch_credit_ceiling_bdt', models.DecimalField(decimal_places=2, default=Decimal('200000.00'), help_text='Total cumulative outstanding credit exposure ceiling for the branch.', max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))])),
                ('block_credit_if_overdue', models.BooleanField(default=True, help_text='If True, customers with dues past due_date or credit terms are blocked from new credit.')),
                ('allow_credit_for_narcotics', models.BooleanField(default=False, help_text='Hard safety control: if False, sales containing narcotic/controlled drugs cannot use DUE credit.')),
                ('allow_credit_for_antibiotics', models.BooleanField(default=True, help_text='Hard safety control: if False, sales containing antibiotic drugs cannot use DUE credit.')),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='customer_credit_policies', to='core.branch')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='customer_credit_policies', to='core.tenant')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='updated_credit_policies', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-updated_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='customercrediteligibilitypolicy',
            constraint=models.UniqueConstraint(fields=('tenant', 'branch'), name='uq_credit_policy_tenant_branch'),
        ),
    ]
