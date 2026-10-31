"""
backend/tests/test_p051_file_storage_security.py
Architecture Decision: ADR-0051
Comprehensive Test Suite for Private Object Storage, Antivirus Content Policy, and Signed Expiring URLs.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.models import Branch, Tenant
from apps.file_storage.antivirus import AntivirusPolicyEngine
from apps.file_storage.models import FileCategory
from apps.file_storage.storage_engine import PrivateStorageEngine
from apps.identity.models import ActivationStatus, PharmacyUser, UserRole


class TestP051FileStorageSecurity(unittest.TestCase):
    """
    Validates private object storage, antivirus scanning, signed expiring URLs,
    Zero-PHI RBAC, and cross-tenant isolation under ADR-0051.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = APIClient()
        cls.t_id = "tenant-dhanmondi-test-51"
        cls.t_foreign_id = "tenant-foreign-test-51"
        cls.b_id = "b-dhanmondi-51-01"

    def setUp(self):
        Branch.objects.filter(branch_id=self.b_id).delete()
        PharmacyUser.objects.filter(
            email__in=[
                "owner51t@dhanmondi.com",
                "cashier51t@dhanmondi.com",
                "pharmacist51t@dhanmondi.com",
                "superadmin51t@platform.bd",
            ]
        ).delete()
        Tenant.objects.filter(tenant_id__in=[self.t_id, self.t_foreign_id]).delete()

        self.tenant = Tenant.objects.create(
            tenant_id=self.t_id,
            name="Dhanmondi Test Care 51",
            subdomain="dhanmondi-test-51",
            plan_tier="PRO",
            is_active=True,
        )
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=self.t_foreign_id,
            name="Foreign Test Care 51",
            subdomain="foreign-test-51",
            plan_tier="STANDARD",
            is_active=True,
        )

        self.branch = Branch.objects.create(
            branch_id=self.b_id,
            tenant=self.tenant,
            name="Main Branch",
            license_number="DGDA-51-01",
            is_active=True,
        )

        self.owner = PharmacyUser.objects.create_user(
            email="owner51t@dhanmondi.com",
            phone_number="+8801719990001",
            password="OwnerPassword123!",
            tenant_id=self.t_id,
            branch_id=self.b_id,
            full_name="Pharmacy Owner 51",
            role=UserRole.PHARMACY_OWNER,
            activation_status=ActivationStatus.ACTIVE,
        )

        self.pharmacist = PharmacyUser.objects.create_user(
            email="pharmacist51t@dhanmondi.com",
            phone_number="+8801719990002",
            password="PharmacistPassword123!",
            tenant_id=self.t_id,
            branch_id=self.b_id,
            full_name="Registered Pharmacist 51",
            role=UserRole.STAFF_PHARMACIST,
            activation_status=ActivationStatus.ACTIVE,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email="cashier51t@dhanmondi.com",
            phone_number="+8801719990003",
            password="CashierPassword123!",
            tenant_id=self.t_id,
            branch_id=self.b_id,
            full_name="Pharmacy Cashier 51",
            role=UserRole.CASHIER,
            activation_status=ActivationStatus.ACTIVE,
        )

        self.superadmin = PharmacyUser.objects.create_user(
            email="superadmin51t@platform.bd",
            phone_number="+8801719990004",
            password="SuperPassword123!",
            tenant_id="tenant-platform",
            full_name="Super Admin 51",
            role=UserRole.SUPERADMIN,
            is_superuser=True,
            is_staff=True,
            activation_status=ActivationStatus.ACTIVE,
        )

        # Tokens
        r_owner = RefreshToken.for_user(self.owner)
        r_owner["tenant_id"] = self.t_id
        r_owner["branch_id"] = self.b_id
        r_owner["role"] = UserRole.PHARMACY_OWNER
        self.token_owner = str(r_owner.access_token)

        r_pharmacist = RefreshToken.for_user(self.pharmacist)
        r_pharmacist["tenant_id"] = self.t_id
        r_pharmacist["branch_id"] = self.b_id
        r_pharmacist["role"] = UserRole.STAFF_PHARMACIST
        self.token_pharmacist = str(r_pharmacist.access_token)

        r_cashier = RefreshToken.for_user(self.cashier)
        r_cashier["tenant_id"] = self.t_id
        r_cashier["branch_id"] = self.b_id
        r_cashier["role"] = UserRole.CASHIER
        self.token_cashier = str(r_cashier.access_token)

        r_super = RefreshToken.for_user(self.superadmin)
        r_super["tenant_id"] = "tenant-platform"
        r_super["role"] = UserRole.SUPERADMIN
        self.token_superadmin = str(r_super.access_token)

    def tearDown(self):
        Branch.objects.filter(branch_id=self.b_id).delete()
        PharmacyUser.objects.filter(
            email__in=[
                "owner51t@dhanmondi.com",
                "cashier51t@dhanmondi.com",
                "pharmacist51t@dhanmondi.com",
                "superadmin51t@platform.bd",
            ]
        ).delete()
        Tenant.objects.filter(tenant_id__in=[self.t_id, self.t_foreign_id]).delete()

    def test_01_clean_pdf_upload_creates_record_in_clean_status(self):
        """Valid Scenario: Uploading clean clinical PDF returns 201 Created and CLEAN scan status."""
        pdf_bytes = b"%PDF-1.4\n1 0 obj\n<< /Title (Prescription) >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF"
        upload = SimpleUploadedFile("rx_clean.pdf", pdf_bytes, content_type="application/pdf")
        res = self.client.post(
            "/api/v2/storage/files/",
            data={
                "file": upload,
                "category": FileCategory.PRESCRIPTION_IMAGE,
                "branch_id": self.b_id,
            },
            format="multipart",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()["data"]
        self.assertEqual(data["scan_status"], "CLEAN")
        self.assertFalse(data["is_quarantined"])
        self.assertEqual(data["tenant_id"], self.t_id)
        self.assertIsNotNone(data["signed_download_url"])

    def test_02_eicar_malware_payload_triggers_quarantine_state(self):
        """Antivirus Scenario: EICAR malware test string triggers INFECTED scan status and quarantine."""
        eicar_bytes = b"%PDF-1.4\n" + AntivirusPolicyEngine.EICAR_SIGNATURE + b"\n%%EOF"
        upload = SimpleUploadedFile("rx_infected.pdf", eicar_bytes, content_type="application/pdf")
        res = self.client.post(
            "/api/v2/storage/files/",
            data={
                "file": upload,
                "category": FileCategory.PRESCRIPTION_IMAGE,
            },
            format="multipart",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()["data"]
        self.assertEqual(data["scan_status"], "INFECTED")
        self.assertTrue(data["is_quarantined"])
        self.assertIn("EICAR", data["quarantine_reason"])

    def test_03_executable_binary_header_is_rejected_with_422(self):
        """Antivirus Scenario: Uploading an executable binary (MZ) is rejected with 422 ERR_INVALID_FILE_TYPE."""
        exe_bytes = b"MZ\x90\x00FakePEHeaderExecutablePayload"
        upload = SimpleUploadedFile("script.exe", exe_bytes, content_type="application/pdf")
        res = self.client.post(
            "/api/v2/storage/files/",
            data={
                "file": upload,
                "category": FileCategory.SALES_INVOICE_PDF,
            },
            format="multipart",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error_code"], "ERR_INVALID_FILE_TYPE")

    def test_04_signed_download_url_generation_and_successful_download(self):
        """URL Scenario: Generating signed expiring URL allows downloading private file content."""
        pdf_bytes = b"%PDF-1.4\nDoc Content\n%%EOF"
        upload = SimpleUploadedFile("invoice_doc.pdf", pdf_bytes, content_type="application/pdf")
        res_up = self.client.post(
            "/api/v2/storage/files/",
            data={"file": upload, "category": FileCategory.SALES_INVOICE_PDF},
            format="multipart",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        file_id = res_up.json()["data"]["file_id"]

        # Generate signed URL
        res_url = self.client.post(
            f"/api/v2/storage/files/{file_id}/signed-url/",
            data={"ttl_seconds": 600},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res_url.status_code, 200)
        token = res_url.json()["data"]["token"]

        # Download via token
        res_dl = self.client.get(
            f"/api/v2/storage/download/{file_id}/?token={token}&tenant_id={self.t_id}",
        )
        self.assertEqual(res_dl.status_code, 200)
        self.assertEqual(res_dl["Content-Type"], "application/pdf")
        self.assertEqual(res_dl["X-Content-Type-Options"], "nosniff")

    def test_05_expired_signed_download_url_returns_403_err_storage_url_expired(self):
        """Expiration Scenario: Requesting download with expired timestamp returns 403 ERR_STORAGE_URL_EXPIRED."""
        pdf_bytes = b"%PDF-1.4\nDoc Content\n%%EOF"
        upload = SimpleUploadedFile("invoice_exp.pdf", pdf_bytes, content_type="application/pdf")
        res_up = self.client.post(
            "/api/v2/storage/files/",
            data={"file": upload, "category": FileCategory.SALES_INVOICE_PDF},
            format="multipart",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        file_id = res_up.json()["data"]["file_id"]

        expired_token = f"{int(time.time()) - 30}:fakesignature"
        res = self.client.get(
            f"/api/v2/storage/download/{file_id}/?token={expired_token}&tenant_id={self.t_id}",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error_code"], "ERR_STORAGE_URL_EXPIRED")

    def test_06_quarantined_file_download_is_blocked_with_403_err_file_quarantined(self):
        """Quarantine Scenario: Attempting download of quarantined attachment returns 403 ERR_FILE_QUARANTINED."""
        eicar_bytes = b"%PDF-1.4\n" + AntivirusPolicyEngine.EICAR_SIGNATURE + b"\n%%EOF"
        upload = SimpleUploadedFile("blocked.pdf", eicar_bytes, content_type="application/pdf")
        res_up = self.client.post(
            "/api/v2/storage/files/",
            data={"file": upload, "category": FileCategory.PRESCRIPTION_IMAGE},
            format="multipart",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        file_id = res_up.json()["data"]["file_id"]

        token, _ = PrivateStorageEngine.generate_signed_token(file_id, self.t_id, ttl_seconds=600)
        res = self.client.get(
            f"/api/v2/storage/download/{file_id}/?token={token}&tenant_id={self.t_id}",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error_code"], "ERR_FILE_QUARANTINED")

    def test_07_prescription_category_rbac_blocks_cashier_and_superadmin(self):
        """RBAC Scenario: Cashier and SuperAdmin are forbidden from accessing prescription attachments (Zero-PHI)."""
        pdf_bytes = b"%PDF-1.4\nRx Content\n%%EOF"
        upload = SimpleUploadedFile("rx_rbac.pdf", pdf_bytes, content_type="application/pdf")
        res_up = self.client.post(
            "/api/v2/storage/files/",
            data={"file": upload, "category": FileCategory.PRESCRIPTION_IMAGE},
            format="multipart",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        file_id = res_up.json()["data"]["file_id"]

        # Pharmacist is allowed
        res_pharm = self.client.get(
            f"/api/v2/storage/files/{file_id}/",
            HTTP_AUTHORIZATION=f"Bearer {self.token_pharmacist}",
        )
        self.assertEqual(res_pharm.status_code, 200)

        # Cashier is blocked
        res_cashier = self.client.get(
            f"/api/v2/storage/files/{file_id}/",
            HTTP_AUTHORIZATION=f"Bearer {self.token_cashier}",
        )
        self.assertEqual(res_cashier.status_code, 403)

        # SuperAdmin is blocked (Zero PHI policy)
        res_super = self.client.get(
            f"/api/v2/storage/files/{file_id}/",
            HTTP_AUTHORIZATION=f"Bearer {self.token_superadmin}",
        )
        self.assertEqual(res_super.status_code, 403)

    def test_08_cross_tenant_access_and_download_raises_403_err_cross_tenant_violation(self):
        """Cross-Tenant Scenario: Attempting to query or download foreign tenant file returns 403 ERR_CROSS_TENANT_VIOLATION."""
        pdf_bytes = b"%PDF-1.4\nDoc Content\n%%EOF"
        upload = SimpleUploadedFile("tenant_doc.pdf", pdf_bytes, content_type="application/pdf")
        res_up = self.client.post(
            "/api/v2/storage/files/",
            data={"file": upload, "category": FileCategory.SALES_INVOICE_PDF},
            format="multipart",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        file_id = res_up.json()["data"]["file_id"]

        # Cross-tenant list query
        res_list = self.client.get(
            f"/api/v2/storage/files/?tenant_id={self.t_foreign_id}",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res_list.status_code, 403)
        self.assertEqual(res_list.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

        # Cross-tenant download token mismatch
        token, _ = PrivateStorageEngine.generate_signed_token(file_id, self.t_id, ttl_seconds=600)
        res_dl = self.client.get(
            f"/api/v2/storage/download/{file_id}/?token={token}&tenant_id={self.t_foreign_id}",
        )
        self.assertEqual(res_dl.status_code, 403)
        self.assertEqual(res_dl.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")
