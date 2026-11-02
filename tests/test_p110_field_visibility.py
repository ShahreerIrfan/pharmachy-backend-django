"""
Integration tests for P110: Fine-grained field visibility and sensitive data masking.
Tests cover:
- Cost, profit, salary, prescription, and patient PII masking across endpoints.
- Role-based visibility: Owner, Branch Manager, Finance Officer, Pharmacist, Cashier.
- Custom role overrides with permission keys.
- Secure CSV and JSON export masking preventing data exfiltration.
- Cross-tenant isolation barriers and suspended tenant subscription guards.
"""

import random
import uuid
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    CustomRoleDefinition,
    FieldVisibilityAuditEvent,
    MedicineProduct,
    PlanTier,
    PrescriptionRecord,
    SaleInvoice,
    StaffSalaryRecord,
    Tenant,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole


class FieldVisibilityIntegrationTests(TestCase):
    """End-to-end integration tests for P110 fine-grained field visibility."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Provision Tenant A (Active)
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p110-a-{self.suffix}",
            name=f"Popular Pharma A {self.suffix}",
            subdomain=f"pop-a-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status="ACTIVE",
        )

        # 2. Provision Tenant B (Foreign Tenant)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p110-b-{self.suffix}",
            name=f"Square Pharma B {self.suffix}",
            subdomain=f"sq-b-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status="ACTIVE",
        )

        # 3. Provision Branches
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant_a,
            name=f"Dhanmondi Branch {self.suffix}",
            license_number=f"DGDA-DL-A-{self.suffix}",
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
            is_main_branch=True,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.suffix}",
            tenant=self.tenant_b,
            name=f"Gulshan Branch {self.suffix}",
            license_number=f"DGDA-DL-B-{self.suffix}",
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
        )

        # 4. Provision Users for Tenant A
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@popular.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Pharmacy Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.manager_a = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@popular.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Branch Manager A",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.finance_a = PharmacyUser.objects.create_user(
            email=f"fin_{self.suffix}@popular.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Finance Officer A",
            password="StrongPassword123!",
            role=UserRole.FINANCE_OFFICER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.pharmacist_a = PharmacyUser.objects.create_user(
            email=f"rx_{self.suffix}@popular.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Staff Pharmacist A",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@popular.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Cashier A",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_b = PharmacyUser.objects.create_user(
            email=f"user_b_{self.suffix}@square.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Cashier B",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )

        # 5. Seed Test Data: Medicines
        self.med1 = MedicineProduct.objects.create(
            product_id=f"med-napa-{self.suffix}",
            brand_name=f"Napa Extra {self.suffix}",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg+65mg",
            mrp_unit=Decimal("2.50"),
            cost_unit=Decimal("1.80"),
            manufacturer="Beximco Pharma",
        )

        # 6. Seed Test Data: Invoices
        self.invoice1 = SaleInvoice.objects.create(
            invoice_id=f"inv-01-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice_number=f"INV-{self.suffix}-001",
            customer_name="Tanvir Hossain",
            customer_phone="01700112233",
            total_amount=Decimal("500.00"),
            tax_amount=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            net_amount=Decimal("500.00"),
            payment_method="CASH",
        )

        # 7. Seed Test Data: Prescriptions
        self.rx1 = PrescriptionRecord.objects.create(
            prescription_id=f"rx-01-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            doctor_name="Dr. Mahmudul Hasan",
            doctor_bmdc_no="BMDC-A-98765",
            patient_token="PT-1001",
            patient_age=35,
            patient_gender="MALE",
            diagnosis="Acute Streptococcal Pharyngitis",
            notes="Dispense Azithromycin 500mg once daily for 5 days.",
            prescription_date=date.today(),
        )

        # 8. Seed Test Data: Staff Salary
        self.salary1 = StaffSalaryRecord.objects.create(
            salary_id=f"sal-01-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            staff_member=self.cashier_a,
            base_salary_bdt=Decimal("28000.00"),
            hourly_rate_bdt=Decimal("150.00"),
            allowances_bdt=Decimal("2000.00"),
            bank_name="Islami Bank Bangladesh",
            bank_account_no="20501234567890123",
            created_by_username="owner_a",
            updated_by_username="owner_a",
        )

    def test_01_profile_endpoint_for_owner_and_cashier(self):
        """Owner receives all 5 visibility permissions, Cashier receives all 5 false."""
        # 1. Owner Profile
        self.client.force_authenticate(user=self.owner_a)
        res_owner = self.client.get("/api/v2/field-visibility/")
        self.assertEqual(res_owner.status_code, status.HTTP_200_OK)
        data_owner = res_owner.json()
        perms_owner = data_owner["visibility_permissions"]
        self.assertTrue(perms_owner["cost_visibility"])
        self.assertTrue(perms_owner["profit_visibility"])
        self.assertTrue(perms_owner["salary_visibility"])
        self.assertTrue(perms_owner["prescription_visibility"])
        self.assertTrue(perms_owner["patient_pii_visibility"])

        # 2. Cashier Profile
        self.client.force_authenticate(user=self.cashier_a)
        res_cashier = self.client.get("/api/v2/field-visibility/")
        self.assertEqual(res_cashier.status_code, status.HTTP_200_OK)
        data_cashier = res_cashier.json()
        perms_cashier = data_cashier["visibility_permissions"]
        self.assertFalse(perms_cashier["cost_visibility"])
        self.assertFalse(perms_cashier["profit_visibility"])
        self.assertFalse(perms_cashier["salary_visibility"])
        self.assertFalse(perms_cashier["prescription_visibility"])
        self.assertFalse(perms_cashier["patient_pii_visibility"])

    def test_02_medicines_cost_visibility_masking(self):
        """Cashier sees cost_unit as None, Branch Manager sees exact wholesale cost."""
        # Cashier
        self.client.force_authenticate(user=self.cashier_a)
        res_cashier = self.client.get("/api/v2/field-visibility/medicines/")
        self.assertEqual(res_cashier.status_code, status.HTTP_200_OK)
        items_cashier = res_cashier.json()["results"]
        target_item = next(i for i in items_cashier if i["product_id"] == self.med1.product_id)
        self.assertIsNone(target_item["cost_unit"])
        self.assertEqual(target_item["mrp_unit"], "2.50")
        self.assertTrue(target_item["_cost_masked"])

        # Branch Manager
        self.client.force_authenticate(user=self.manager_a)
        res_mgr = self.client.get("/api/v2/field-visibility/medicines/")
        self.assertEqual(res_mgr.status_code, status.HTTP_200_OK)
        items_mgr = res_mgr.json()["results"]
        target_item_mgr = next(i for i in items_mgr if i["product_id"] == self.med1.product_id)
        self.assertEqual(target_item_mgr["cost_unit"], "1.80")
        self.assertFalse(target_item_mgr["_cost_masked"])

    def test_03_invoices_profit_and_pii_masking(self):
        """Invoices mask profits and PII for Cashier, unmask profits for Finance, unmask all for Owner."""
        # 1. Cashier: profits masked, PII masked, costs masked
        self.client.force_authenticate(user=self.cashier_a)
        res = self.client.get("/api/v2/field-visibility/invoices/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        inv = res.json()["results"][0]
        self.assertIsNone(inv["gross_profit"])
        self.assertIsNone(inv["cogs_amount"])
        self.assertEqual(inv["customer_phone"], "017****2233")
        self.assertEqual(inv["customer_name"], "T**** H****")
        self.assertTrue(inv["_profit_masked"])
        self.assertTrue(inv["_pii_masked"])

        # 2. Finance Officer: profits unmasked, PII masked
        self.client.force_authenticate(user=self.finance_a)
        res_fin = self.client.get("/api/v2/field-visibility/invoices/")
        self.assertEqual(res_fin.status_code, status.HTTP_200_OK)
        inv_fin = res_fin.json()["results"][0]
        self.assertIsNotNone(inv_fin["gross_profit"])
        self.assertFalse(inv_fin["_profit_masked"])
        self.assertTrue(inv_fin["_pii_masked"])
        self.assertEqual(inv_fin["customer_phone"], "017****2233")

        # 3. Pharmacy Owner: all unmasked
        self.client.force_authenticate(user=self.owner_a)
        res_owner = self.client.get("/api/v2/field-visibility/invoices/")
        self.assertEqual(res_owner.status_code, status.HTTP_200_OK)
        inv_owner = res_owner.json()["results"][0]
        self.assertIsNotNone(inv_owner["gross_profit"])
        self.assertEqual(inv_owner["customer_phone"], "01700112233")
        self.assertEqual(inv_owner["customer_name"], "Tanvir Hossain")
        self.assertFalse(inv_owner["_profit_masked"])
        self.assertFalse(inv_owner["_pii_masked"])

    def test_04_salaries_get_and_post_authorization(self):
        """Finance Officer can view and edit salaries, Cashier receives masked GET and 403 on POST."""
        # Cashier GET salaries -> figures are masked
        self.client.force_authenticate(user=self.cashier_a)
        res_get = self.client.get("/api/v2/field-visibility/salaries/")
        self.assertEqual(res_get.status_code, status.HTTP_200_OK)
        sal_data = res_get.json()["results"][0]
        self.assertIsNone(sal_data["base_salary_bdt"])
        self.assertEqual(sal_data["bank_account_no"], "***REDACTED***")
        self.assertTrue(sal_data["_salary_masked"])

        # Cashier attempts POST salary -> 403 Forbidden
        res_post_cashier = self.client.post("/api/v2/field-visibility/salaries/", {
            "staff_user_id": str(self.cashier_a.user_id),
            "base_salary_bdt": "30000.00",
        }, format="json")
        self.assertEqual(res_post_cashier.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_post_cashier.json()["error_code"], "ERR_PERMISSION_DENIED")

        # Finance Officer POST salary -> 201 Created
        self.client.force_authenticate(user=self.finance_a)
        res_post_fin = self.client.post("/api/v2/field-visibility/salaries/", {
            "staff_user_id": str(self.pharmacist_a.user_id),
            "base_salary_bdt": "40000.00",
            "hourly_rate_bdt": "220.00",
            "bank_name": "City Bank",
            "bank_account_no": "1122334455",
        }, format="json")
        self.assertEqual(res_post_fin.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_post_fin.json()["base_salary_bdt"], "40000.00")

    def test_05_prescriptions_clinical_masking(self):
        """Pharmacist views clinical diagnosis & notes, Cashier receives redacted clinical notations."""
        # Pharmacist
        self.client.force_authenticate(user=self.pharmacist_a)
        res_rx = self.client.get("/api/v2/field-visibility/prescriptions/")
        self.assertEqual(res_rx.status_code, status.HTTP_200_OK)
        item_rx = res_rx.json()["results"][0]
        self.assertEqual(item_rx["diagnosis"], "Acute Streptococcal Pharyngitis")
        self.assertEqual(item_rx["notes"], "Dispense Azithromycin 500mg once daily for 5 days.")
        self.assertFalse(item_rx["_prescription_masked"])

        # Cashier
        self.client.force_authenticate(user=self.cashier_a)
        res_cashier = self.client.get("/api/v2/field-visibility/prescriptions/")
        self.assertEqual(res_cashier.status_code, status.HTTP_200_OK)
        item_c = res_cashier.json()["results"][0]
        self.assertEqual(item_c["diagnosis"], "[CLINICAL_DIAGNOSIS_REDACTED]")
        self.assertEqual(item_c["notes"], "[CONFIDENTIAL_CLINICAL_NOTES]")
        self.assertTrue(item_c["_prescription_masked"])

    def test_06_universal_mask_endpoint(self):
        """POST /api/v2/field-visibility/mask/ masks arbitrary submitted batch payloads."""
        self.client.force_authenticate(user=self.cashier_a)
        raw_items = [
            {"product_id": "p-1", "brand_name": "Napa", "mrp_unit": "1.20", "cost_unit": "0.80"},
            {"salary_id": "s-1", "base_salary_bdt": "50000.00", "bank_account_no": "999888777"},
        ]
        res = self.client.post("/api/v2/field-visibility/mask/", {
            "records": raw_items,
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.json()["results"]
        # Cost is masked in item 0
        self.assertIsNone(results[0]["cost_unit"])
        # Salary is masked in item 1
        self.assertIsNone(results[1]["base_salary_bdt"])
        self.assertEqual(results[1]["bank_account_no"], "***REDACTED***")

    def test_07_secure_export_csv_masking(self):
        """Cashier exporting invoices receives masked CSV without profit or customer phone numbers."""
        self.client.force_authenticate(user=self.cashier_a)
        res = self.client.post("/api/v2/field-visibility/export/", {
            "dataset_type": "INVOICE",
            "format": "CSV",
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        csv_text = res.content.decode("utf-8")
        # Profit and unmasked phone must NOT appear in the CSV
        self.assertNotIn("125.00", csv_text)
        self.assertNotIn("01700112233", csv_text)
        self.assertIn("017****2233", csv_text)
        self.assertIn("T**** H****", csv_text)

    def test_08_custom_role_override(self):
        """Custom role with finance:profit_visibility permission unmasks profit for the user."""
        role_code = f"ROLE_ANALYST_{self.suffix}".upper()
        CustomRoleDefinition.objects.create(
            role_id=f"role-{self.suffix}",
            role_code=role_code,
            tenant=self.tenant_a,
            display_name="Sales Analyst",
            base_role="CASHIER",
            permission_keys=["sales:checkout", "finance:profit_visibility", "action:profit_visibility"],
            is_archived=False,
        )

        analyst_user = PharmacyUser.objects.create_user(
            email=f"analyst_{self.suffix}@popular.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Sales Analyst",
            password="StrongPassword123!",
            role=role_code,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.client.force_authenticate(user=analyst_user)
        res = self.client.get("/api/v2/field-visibility/invoices/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        inv = res.json()["results"][0]
        # Profit is visible because of custom role
        self.assertIsNotNone(inv["gross_profit"])
        self.assertFalse(inv["_profit_masked"])
        # Customer phone is still masked
        self.assertTrue(inv["_pii_masked"])

    def test_09_invalid_input_rejected(self):
        """Incomplete or invalid input is rejected with HTTP 400 or 422."""
        self.client.force_authenticate(user=self.owner_a)

        # 1. Invalid mask payload (records not a list)
        res1 = self.client.post("/api/v2/field-visibility/mask/", {
            "records": "not-a-list",
        }, format="json")
        self.assertIn(res1.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn(res1.json().get("error_code") or res1.json().get("code"), ["ERR_VALIDATION_FAILED", "INVALID_RECORDS"])

        # 2. Missing staff user ID on salary POST
        res2 = self.client.post("/api/v2/field-visibility/salaries/", {
            "base_salary_bdt": "25000.00",
        }, format="json")
        self.assertIn(res2.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn(res2.json().get("error_code") or res2.json().get("code"), ["ERR_VALIDATION_FAILED", "MISSING_STAFF_ID"])

    def test_10_cross_tenant_isolation_barrier(self):
        """Staff of Tenant B cannot query Tenant A's field visibility endpoints."""
        self.client.force_authenticate(user=self.user_b)

        # Attempt to supply foreign tenant_id
        res = self.client.get(f"/api/v2/field-visibility/?tenant_id={self.tenant_a.tenant_id}")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

    def test_11_suspended_tenant_subscription_blocks_salary_mutations(self):
        """Suspended tenant subscription blocks salary modifications."""
        self.tenant_a.status = "SUSPENDED"
        self.tenant_a.save(update_fields=["status"])

        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post("/api/v2/field-visibility/salaries/", {
            "staff_user_id": str(self.cashier_a.user_id),
            "base_salary_bdt": "32000.00",
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(res.json()["error_code"], ["ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED", "ERR_TENANT_SUSPENDED"])

    def test_12_audit_trail_recorded(self):
        """Exporting data and modifying salaries records immutable audit events."""
        self.client.force_authenticate(user=self.finance_a)

        # 1. Trigger export
        self.client.post("/api/v2/field-visibility/export/", {
            "dataset_type": "MEDICINE",
            "format": "CSV",
        }, format="json")

        # 2. Trigger salary update
        self.client.post("/api/v2/field-visibility/salaries/", {
            "staff_user_id": str(self.cashier_a.user_id),
            "base_salary_bdt": "35000.00",
        }, format="json")

        export_audits = FieldVisibilityAuditEvent.objects.filter(
            tenant=self.tenant_a,
            action="DATASET_EXPORT",
        )
        self.assertTrue(export_audits.exists())

        salary_audits = FieldVisibilityAuditEvent.objects.filter(
            tenant=self.tenant_a,
            action="SALARY_MODIFIED",
        )
        self.assertTrue(salary_audits.exists())
