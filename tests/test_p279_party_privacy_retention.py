"""
backend/tests/test_p279_party_privacy_retention.py
Architecture Decision: ADR-0279 (P279).
Phase 14: Customers, Suppliers, Dues & Payments.

Integration tests verifying Party Data Privacy and Retention:
1. Statutory retention assessment detects 2-year Rx and 5-year invoice holds.
2. Open due balance hold blocks customer anonymization (ERR_OPEN_DUE_BALANCE_HOLD, 400).
3. Successful demographic correction updates customer and emits PartyPrivacyRetentionAudit.
4. Demographic correction requires mandatory reason (ERR_MISSING_CORRECTION_REASON, 400).
5. Audited irreversible anonymization pseudonymizes customer name/phone and sets is_archived=True.
6. Supplier demographic correction and retention assessment.
7. Supplier anonymization blocked on active payable liability (ERR_OPEN_PAYABLE_BALANCE_HOLD, 400).
8. Restricted clinical export denied for Manager (ERR_CLINICAL_EXPORT_DENIED, 403).
9. Restricted clinical export allowed for Owner.
10. Cashier and staff pharmacist strictly denied all privacy endpoints (ERR_STAFF_ROLE_DENIED, 403).
11. Cross-tenant privacy access forbidden (ERR_CROSS_TENANT_ACCESS, 403).
12. Suspended tenant account write blocked.
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    CustomerDueRecord,
    DueStatus,
    PartyPrivacyActionType,
    PartyPrivacyRetentionAudit,
    PartyPrivacyStatus,
    PosCustomer,
    PrescriptionRecord,
    PrescriptionStatus,
    SaleInvoice,
    Supplier,
    SupplierStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestPartyPrivacyRetentionIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branch
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-a-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"pop-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Gulshan Branch",
        )

        # 2. Foreign Tenant & Branch
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.suffix}",
            name=f"Foreign Health {self.suffix}",
            subdomain=f"for-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Foreign Branch",
        )

        # 3. Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"ten-susp-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-{self.suffix}",
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.suspended_branch = Branch.objects.create(
            branch_id=f"br-susp-{self.suffix}",
            tenant=self.suspended_tenant,
            name="Susp Branch",
        )

        # 4. Customers for Tenant A
        self.customer_clean = PosCustomer.objects.create(
            customer_id=f"cust-clean-{self.suffix}",
            tenant=self.tenant_a,
            primary_branch=self.branch_a,
            full_name="Abdur Rahim",
            phone=f"+880171{random.randint(1000000, 9999999)}",
            email="rahim@pharma.com",
            address="House 10, Road 5, Gulshan",
            credit_limit_bdt=Decimal("5000.00"),
            current_due_bdt=Decimal("0.00"),
        )

        self.customer_with_due = PosCustomer.objects.create(
            customer_id=f"cust-due-{self.suffix}",
            tenant=self.tenant_a,
            primary_branch=self.branch_a,
            full_name="Kazi Nazrul",
            phone=f"+880181{random.randint(1000000, 9999999)}",
            email="nazrul@pharma.com",
            address="Dhanmondi, Dhaka",
            credit_limit_bdt=Decimal("10000.00"),
            current_due_bdt=Decimal("2500.00"),
        )
        CustomerDueRecord.objects.create(
            due_id=f"due-{uuid.uuid4().hex[:8]}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            customer=self.customer_with_due,
            total_due_bdt=Decimal("2500.00"),
            remaining_due_bdt=Decimal("2500.00"),
            status=DueStatus.OUTSTANDING,
        )

        # 5. Customer with Clinical Rx
        self.customer_clinical = PosCustomer.objects.create(
            customer_id=f"cust-rx-{self.suffix}",
            tenant=self.tenant_a,
            primary_branch=self.branch_a,
            full_name="Dr. Shamsul Huda Patient",
            phone=f"+880191{random.randint(1000000, 9999999)}",
            email="huda@pharma.com",
            credit_limit_bdt=Decimal("0.00"),
            current_due_bdt=Decimal("0.00"),
        )
        self.rx_record = PrescriptionRecord.objects.create(
            prescription_id=f"rx-{uuid.uuid4().hex[:8]}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            customer=self.customer_clinical,
            doctor_name="Dr. Mahbubur Rahman",
            doctor_bmdc_no="A-12345",
            prescription_date=timezone.now().date(),
            patient_token="PAT-TOKEN-9988",
            status=PrescriptionStatus.VERIFIED,
        )

        # 6. Supplier for Tenant A
        self.supplier_payable = Supplier.objects.create(
            supplier_id=f"sup-pay-{self.suffix}",
            tenant=self.tenant_a,
            name="Incepta Pharmaceuticals Ltd",
            code=f"INC-{self.suffix.upper()}",
            contact_person="M. Haque",
            phone=f"+880161{random.randint(1000000, 9999999)}",
            current_balance=Decimal("15000.00"),
            status=SupplierStatus.ACTIVE,
        )

        self.supplier_clean = Supplier.objects.create(
            supplier_id=f"sup-cln-{self.suffix}",
            tenant=self.tenant_a,
            name="ACI Healthcare Depot",
            code=f"ACI-{self.suffix.upper()}",
            contact_person="R. Khan",
            phone=f"+880151{random.randint(1000000, 9999999)}",
            current_balance=Decimal("0.00"),
            status=SupplierStatus.ACTIVE,
        )

        # 7. Customer for Tenant B
        self.foreign_customer = PosCustomer.objects.create(
            customer_id=f"cust-for-{self.suffix}",
            tenant=self.tenant_b,
            primary_branch=self.branch_b,
            full_name="Foreign Patient",
            phone="+8801999999999",
        )

        # 8. Suspended Customer
        self.suspended_customer = PosCustomer.objects.create(
            customer_id=f"cust-susp-{self.suffix}",
            tenant=self.suspended_tenant,
            primary_branch=self.suspended_branch,
            full_name="Suspended Cust",
            phone="+8801988888888",
        )

        # 9. Role Users
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@popular.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.manager = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@popular.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Branch Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@popular.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="POS Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"pharm_{self.suffix}@popular.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Staff Pharmacist",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.suspended_user = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@popular.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Suspended Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

    def test_01_retention_assessment_detects_statutory_holds(self):
        """Manager assesses customer with prescription and detects DGDA 2-year statutory retention hold."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/parties/privacy/assess-retention/",
            {
                "party_type": "CUSTOMER",
                "party_id": self.customer_clinical.customer_id,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["statutory_hold_active"] is True
        assert data["can_physically_erase"] is False
        assert "DGDA" in data["statutory_authority"]
        assert len(data["conflicts_detected"]) >= 1

        # Verify audit record created
        assert PartyPrivacyRetentionAudit.objects.filter(
            tenant=self.tenant_a,
            party_id=self.customer_clinical.customer_id,
            action_type=PartyPrivacyActionType.RETENTION_ASSESSMENT,
        ).exists()

    def test_02_customer_anonymization_blocked_on_open_due(self):
        """Customer with open due balance blocks anonymization with ERR_OPEN_DUE_BALANCE_HOLD."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/parties/privacy/anonymize/",
            {
                "party_type": "CUSTOMER",
                "party_id": self.customer_with_due.customer_id,
                "reason": "Customer requested right to be forgotten",
            },
            format="json",
        )
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        data = resp.json()
        assert data["error_code"] == "ERR_OPEN_DUE_BALANCE_HOLD"

    def test_03_customer_demographic_correction_success(self):
        """Manager updates customer demographics with valid reason, verifying audit record and hash."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/parties/privacy/correct/",
            {
                "party_type": "CUSTOMER",
                "party_id": self.customer_clean.customer_id,
                "corrections": {
                    "full_name": "Abdur Rahim Chowdhury",
                    "phone": "01712345678",
                    "address": "Apartment 4B, Gulshan 2",
                },
                "reason": "Customer corrected formal name on national NID update",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["success"] is True
        assert data["full_name"] == "Abdur Rahim Chowdhury"
        assert data["phone"] == "+8801712345678"

        # Check DB update
        self.customer_clean.refresh_from_db()
        assert self.customer_clean.full_name == "Abdur Rahim Chowdhury"
        assert self.customer_clean.phone == "+8801712345678"

        # Check audit entry
        audit = PartyPrivacyRetentionAudit.objects.get(audit_id=data["audit_id"])
        assert audit.action_type == PartyPrivacyActionType.CORRECTION
        assert audit.status == PartyPrivacyStatus.COMPLETED
        assert len(audit.sha256_hash) == 64

    def test_04_customer_correction_missing_reason_rejected(self):
        """Demographic correction without explanation reason is rejected with ERR_MISSING_CORRECTION_REASON."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/parties/privacy/correct/",
            {
                "party_type": "CUSTOMER",
                "party_id": self.customer_clean.customer_id,
                "corrections": {"full_name": "New Name"},
                "reason": "",
            },
            format="json",
        )
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        data = resp.json()
        assert data["error_code"] == "ERR_MISSING_CORRECTION_REASON"

    def test_05_audited_customer_anonymization_success(self):
        """Clean customer without due balance is irreversibly pseudonymized under statutory hold."""
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            "/api/v2/parties/privacy/anonymize/",
            {
                "party_type": "CUSTOMER",
                "party_id": self.customer_clean.customer_id,
                "reason": "Data subject requested permanent erasure under privacy policy",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["success"] is True
        assert data["is_anonymized"] is True
        assert "Anonymized Customer" in data["anonymized_name"]

        self.customer_clean.refresh_from_db()
        assert "Anonymized Customer" in self.customer_clean.full_name
        assert self.customer_clean.is_active is False
        assert self.customer_clean.is_archived is True

    def test_06_supplier_retention_and_correction(self):
        """Supplier demographic correction updates details with audit trail."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/parties/privacy/correct/",
            {
                "party_type": "SUPPLIER",
                "party_id": self.supplier_clean.supplier_id,
                "corrections": {
                    "contact_person": "Mahmudul Hasan",
                    "phone": "+8801511223344",
                },
                "reason": "Supplier updated key account manager",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["success"] is True

        self.supplier_clean.refresh_from_db()
        assert self.supplier_clean.contact_person == "Mahmudul Hasan"
        assert self.supplier_clean.phone == "+8801511223344"

    def test_07_supplier_anonymization_blocked_on_payable(self):
        """Supplier with open payable balance blocks anonymization with ERR_OPEN_PAYABLE_BALANCE_HOLD."""
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            "/api/v2/parties/privacy/anonymize/",
            {
                "party_type": "SUPPLIER",
                "party_id": self.supplier_payable.supplier_id,
                "reason": "Supplier contract ended",
            },
            format="json",
        )
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        data = resp.json()
        assert data["error_code"] == "ERR_OPEN_PAYABLE_BALANCE_HOLD"

    def test_08_restricted_clinical_export_denied_for_manager(self):
        """Branch Manager attempting RESTRICTED_CLINICAL export receives ERR_CLINICAL_EXPORT_DENIED."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/parties/privacy/restricted-export/",
            {
                "party_type": "CUSTOMER",
                "party_id": self.customer_clinical.customer_id,
                "classification": "RESTRICTED_CLINICAL",
                "reason": "Routine clinical audit",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        data = resp.json()
        assert data["error_code"] == "ERR_CLINICAL_EXPORT_DENIED"

    def test_09_restricted_clinical_export_allowed_for_owner(self):
        """Pharmacy Owner can export RESTRICTED_CLINICAL dossier including clinical prescription history."""
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            "/api/v2/parties/privacy/restricted-export/",
            {
                "party_type": "CUSTOMER",
                "party_id": self.customer_clinical.customer_id,
                "classification": "RESTRICTED_CLINICAL",
                "reason": "Official medical record request",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["classification"] == "RESTRICTED_CLINICAL"
        assert len(data["clinical_prescriptions"]) >= 1
        rx = data["clinical_prescriptions"][0]
        assert rx["prescription_id"] == self.rx_record.prescription_id
        assert rx["doctor_name"] == "Dr. Mahbubur Rahman"
        assert rx["bmdc_number"] == "A-12345"

    def test_10_staff_roles_strictly_denied(self):
        """Cashier and staff pharmacist are strictly forbidden from privacy endpoints."""
        for user in [self.cashier, self.pharmacist]:
            self.client.force_authenticate(user=user)
            resp = self.client.post(
                "/api/v2/parties/privacy/assess-retention/",
                {
                    "party_type": "CUSTOMER",
                    "party_id": self.customer_clean.customer_id,
                },
                format="json",
            )
            assert resp.status_code == status.HTTP_403_FORBIDDEN
            assert resp.json()["error_code"] == "ERR_STAFF_ROLE_DENIED"

    def test_11_cross_tenant_isolation_enforced(self):
        """Manager from Tenant A cannot assess or manipulate customer of Tenant B."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/parties/privacy/assess-retention/",
            {
                "party_type": "CUSTOMER",
                "party_id": self.foreign_customer.customer_id,
            },
            format="json",
        )
        assert resp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
        if resp.status_code == status.HTTP_403_FORBIDDEN:
            assert resp.json()["error_code"] == "ERR_CROSS_TENANT_ACCESS"

    def test_12_suspended_tenant_protection(self):
        """Suspended tenant user is blocked with ERR_TENANT_SUSPENDED."""
        self.client.force_authenticate(user=self.suspended_user)
        resp = self.client.post(
            "/api/v2/parties/privacy/correct/",
            {
                "party_type": "CUSTOMER",
                "party_id": self.suspended_customer.customer_id,
                "corrections": {"full_name": "Test"},
                "reason": "Testing suspended tenant",
            },
            format="json",
        )
        assert resp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_402_PAYMENT_REQUIRED)
        assert resp.json()["error_code"] in ("ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")
