"""
Acceptance Tests for P242 - Secure Rx Upload Flow, Private Partitioned Storage & 5-Year Retention.
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Coverage:
1. Valid Scenario: Upload valid JPEG / PDF prescription attachment, SHA-256 integrity, private storage partitioning, expiring signed URL.
2. Content & Security Inspection: Rejection of prohibited file types (.exe, DOS MZ magic headers), oversized and empty files.
3. Cross-Tenant Isolation: Tenant B strictly blocked from viewing, downloading, or verifying Tenant A attachments.
4. Pharmacist Clinical Verification: Verification sign-off with PCB registration number.
5. Quarantine Security Guard: Quarantined / infected files strictly blocked from download URL generation.
"""

from datetime import timedelta
from decimal import Decimal
import hashlib
import io
import random
import uuid
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    MedicineProduct,
    PrescriptionAttachment,
    PrescriptionRecord,
    PrescriptionStatus,
    StaffMember,
    StaffRole,
    Tenant,
)
from apps.core.prescription_service import PrescriptionService
from apps.core.prescription_upload_service import PrescriptionUploadService
from apps.file_storage.antivirus import AntivirusPolicyEngine
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def rx_upload_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(tenant_id=f"ten-p242-a-{uid}", name="Rx Upload Tenant A", subdomain=f"p242a-{uid}")
    br_a = Branch.objects.create(tenant=ten_a, branch_id=f"br-p242-a-{uid}", name="Mohakhali Branch", branch_code="MK01")

    # Pharmacist User for Tenant A
    pharmacist_user_a = PharmacyUser.objects.create_user(
        email=f"pharmacist_a_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )
    staff_pharmacist_a = StaffMember.objects.create(
        staff_id=f"stf-a-{uid}",
        tenant=ten_a,
        branch=br_a,
        username=pharmacist_user_a.email,
        full_name="Md. Tariqul Islam",
        role=StaffRole.PHARMACIST,
        pcb_registration_no="PCB-B99214",
        is_active=True,
    )

    # Cashier User for Tenant A
    cashier_user_a = PharmacyUser.objects.create_user(
        email=f"cashier_a_{uid}@test.com",
        phone_number=f"+88015{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Tenant B (Cross-Tenant)
    ten_b = Tenant.objects.create(tenant_id=f"ten-p242-b-{uid}", name="Rx Upload Tenant B", subdomain=f"p242b-{uid}")
    br_b = Branch.objects.create(tenant=ten_b, branch_id=f"br-p242-b-{uid}", name="Mirpur Branch", branch_code="MP01")
    manager_user_b = PharmacyUser.objects.create_user(
        email=f"manager_b_{uid}@test.com",
        phone_number=f"+88018{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )

    # Product
    prod = MedicineProduct.objects.create(
        product_id=f"prod-p242-{uid}",
        brand_name="Azithrocin 500mg",
        generic_name="Azithromycin",
        dosage_form="Tablet",
        strength="500mg",
        is_antibiotic=True,
        is_prescription_only=True,
        mrp_unit=Decimal("35.00"),
        cost_unit=Decimal("25.00"),
    )

    # Prescription in Tenant A
    rx_a = PrescriptionService.create_prescription(
        pharmacist_user_a,
        doctor_name="Prof. Dr. M. A. Jalil",
        doctor_bmdc_no="BMDC-78129",
        patient_token=f"PT-P242-{uid}",
        prescription_date=timezone.now().date(),
        expiry_date=timezone.now().date() + timedelta(days=30),
        items_data=[
            {
                "product_id": prod.product_id,
                "dosage_instructions": "1 daily for 3 days",
                "quantity_prescribed": 3,
            }
        ],
    )

    return {
        "tenant_a": ten_a,
        "branch_a": br_a,
        "pharmacist_user_a": pharmacist_user_a,
        "staff_pharmacist_a": staff_pharmacist_a,
        "cashier_user_a": cashier_user_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "manager_user_b": manager_user_b,
        "rx_a": rx_a,
    }


@pytest.mark.django_db
def test_p242_valid_secure_prescription_upload_and_signed_url(rx_upload_env):
    """
    Scenario 1: Valid prescription image upload:
    - Validates SHA-256 checksum and statutory 5-year retention policy.
    - Generates expiring signed download URL.
    """
    env = rx_upload_env
    user = env["pharmacist_user_a"]
    rx = env["rx_a"]

    # Valid JPEG image payload (header: FF D8 FF)
    dummy_jpeg_content = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00" + b"X" * 300
    expected_sha = hashlib.sha256(dummy_jpeg_content).hexdigest()

    attachment = PrescriptionUploadService.upload_attachment(
        user,
        prescription_id=rx.prescription_id,
        filename="dr_jalil_prescription.jpg",
        content=dummy_jpeg_content,
        declared_mime="image/jpeg",
        notes="High-resolution flatbed scan",
    )

    assert attachment.attachment_id.startswith("att-rx-")
    assert attachment.prescription == rx
    assert attachment.file_name == "dr_jalil_prescription.jpg"
    assert attachment.mime_type == "image/jpeg"
    assert attachment.file_size_bytes == len(dummy_jpeg_content)
    assert attachment.checksum_sha256 == expected_sha
    assert attachment.retention_days == 1825
    expected_retention_date = timezone.now().date() + timedelta(days=1825)
    assert attachment.retention_expires_at == expected_retention_date
    assert attachment.is_quarantined is False
    assert attachment.file_record.scan_status == "CLEAN"

    # Generate expiring signed URL
    url_data = PrescriptionUploadService.get_attachment_signed_url(
        user,
        attachment_id=attachment.attachment_id,
        ttl_seconds=900,
    )
    assert url_data["attachment_id"] == attachment.attachment_id
    assert "signed_url" in url_data
    assert url_data["expires_in_seconds"] == 900
    assert url_data["retention_expires_at"] == expected_retention_date


@pytest.mark.django_db
def test_p242_rest_api_upload_flow_and_signed_download(rx_upload_env):
    """
    Scenario 2: Execute multipart file upload via DRF REST API endpoint.
    """
    env = rx_upload_env
    user = env["pharmacist_user_a"]
    rx = env["rx_a"]

    client = APIClient()
    client.force_authenticate(user=user)

    # Valid PDF file (header: %PDF-)
    dummy_pdf_content = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n" + b"A" * 200
    uploaded_file = SimpleUploadedFile(
        "prescription_document.pdf",
        dummy_pdf_content,
        content_type="application/pdf",
    )

    res = client.post(
        f"/api/v2/prescriptions/{rx.prescription_id}/upload-attachment/",
        data={"file": uploaded_file, "notes": "Digital Rx copy"},
        format="multipart",
    )
    assert res.status_code == status.HTTP_201_CREATED, res.data
    att_id = res.data["attachment_id"]
    assert res.data["mime_type"] == "application/pdf"
    assert res.data["retention_days"] == 1825

    # Request signed download URL via endpoint
    url_res = client.get(f"/api/v2/prescription-attachments/{att_id}/download-url/?ttl=600")
    assert url_res.status_code == status.HTTP_200_OK, url_res.data
    assert "signed_url" in url_res.data
    assert url_res.data["expires_in_seconds"] == 600

    # List attachments endpoint
    list_res = client.get(f"/api/v2/prescriptions/{rx.prescription_id}/attachments/")
    assert list_res.status_code == status.HTTP_200_OK
    assert len(list_res.data) >= 1
    assert any(a["attachment_id"] == att_id for a in list_res.data)


@pytest.mark.django_db
def test_p242_security_inspection_and_prohibited_types(rx_upload_env):
    """
    Scenario 3: Reject prohibited file formats:
    - Executable binary headers (.exe with 'MZ' magic bytes)
    - Forbidden file extensions
    - Oversized files (>10 MB)
    - Empty files (<100 bytes)
    """
    env = rx_upload_env
    user = env["pharmacist_user_a"]
    rx = env["rx_a"]

    # 1. Executable file extension
    with pytest.raises(Exception) as exc_exe:
        PrescriptionUploadService.upload_attachment(
            user,
            prescription_id=rx.prescription_id,
            filename="hack_payload.exe",
            content=b"MZ" + b"X" * 200,
            declared_mime="application/x-msdownload",
        )
    assert "is forbidden" in str(exc_exe.value)

    # 2. Executable magic bytes disguised as JPG
    disguised_exe = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 300
    with pytest.raises(Exception) as exc_magic:
        PrescriptionUploadService.upload_attachment(
            user,
            prescription_id=rx.prescription_id,
            filename="trojan.jpg",
            content=disguised_exe,
            declared_mime="image/jpeg",
        )
    assert "Executable binary header detected" in str(exc_magic.value) or "malware policy" in str(exc_magic.value)

    # 3. Empty / too small file
    with pytest.raises(Exception) as exc_small:
        PrescriptionUploadService.upload_attachment(
            user,
            prescription_id=rx.prescription_id,
            filename="empty.jpg",
            content=b"\xff\xd8\xff" + b"x" * 10,
            declared_mime="image/jpeg",
        )
    assert "too small" in str(exc_small.value)

    # 4. Oversized file (>10 MB)
    with pytest.raises(Exception) as exc_big:
        PrescriptionUploadService.upload_attachment(
            user,
            prescription_id=rx.prescription_id,
            filename="huge_scan.jpg",
            content=b"\xff\xd8\xff" + b"0" * (11 * 1024 * 1024),
            declared_mime="image/jpeg",
        )
    assert "exceeds allowable maximum" in str(exc_big.value)


@pytest.mark.django_db
def test_p242_cross_tenant_isolation(rx_upload_env):
    """
    Scenario 4: Tenant B cannot access or download Tenant A's prescription attachments.
    """
    env = rx_upload_env
    user_a = env["pharmacist_user_a"]
    user_b = env["manager_user_b"]
    rx_a = env["rx_a"]

    dummy_content = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00" + b"Z" * 300
    att_a = PrescriptionUploadService.upload_attachment(
        user_a,
        prescription_id=rx_a.prescription_id,
        filename="tenant_a_rx.jpg",
        content=dummy_content,
        declared_mime="image/jpeg",
    )

    client_b = APIClient()
    client_b.force_authenticate(user=user_b)

    # Tenant B tries to request download URL for Tenant A's attachment
    res_url = client_b.get(f"/api/v2/prescription-attachments/{att_a.attachment_id}/download-url/")
    assert res_url.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_ENTITY, status.HTTP_403_FORBIDDEN)

    # Tenant B cannot see attachment in list
    list_res = client_b.get("/api/v2/prescription-attachments/")
    assert list_res.status_code == status.HTTP_200_OK
    items = list_res.data if isinstance(list_res.data, list) else list_res.data.get("results", [])
    assert all(a["attachment_id"] != att_a.attachment_id for a in items)


@pytest.mark.django_db
def test_p242_pharmacist_clinical_attachment_verification(rx_upload_env):
    """
    Scenario 5: Pharmacist verifies an uploaded attachment with PCB number.
    """
    env = rx_upload_env
    pharmacist = env["pharmacist_user_a"]
    rx = env["rx_a"]

    dummy_content = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00" + b"M" * 300
    att = PrescriptionUploadService.upload_attachment(
        pharmacist,
        prescription_id=rx.prescription_id,
        filename="verified_scan.jpg",
        content=dummy_content,
        declared_mime="image/jpeg",
    )

    assert att.is_verified_by_pharmacist is False

    verified_att = PrescriptionUploadService.verify_attachment(
        pharmacist,
        attachment_id=att.attachment_id,
        pharmacist_pcb_no="PCB-B99214",
        notes="Physical stamp & signature match BMDC records",
    )

    assert verified_att.is_verified_by_pharmacist is True
    assert verified_att.verified_by == env["staff_pharmacist_a"]
    assert "PCB-B99214" in verified_att.notes


@pytest.mark.django_db
def test_p242_quarantined_file_download_lockout(rx_upload_env):
    """
    Scenario 6: Quarantined files are strictly blocked from generating signed download URLs.
    """
    env = rx_upload_env
    user = env["pharmacist_user_a"]
    rx = env["rx_a"]

    # EICAR signature test file
    eicar_content = AntivirusPolicyEngine.EICAR_SIGNATURE + b"\x00" * 100

    # Uploading infected payload raises security validation error
    with pytest.raises(Exception) as exc:
        PrescriptionUploadService.upload_attachment(
            user,
            prescription_id=rx.prescription_id,
            filename="eicar_test.jpg",
            content=eicar_content,
            declared_mime="image/jpeg",
        )
    assert "malware policy" in str(exc.value)

    # Check that attachment was created in DB but quarantined
    quarantined_att = PrescriptionAttachment.objects.filter(
        prescription=rx,
        file_name="eicar_test.jpg",
    ).first()
    assert quarantined_att is not None
    assert quarantined_att.is_quarantined is True

    # Attempting to get download URL raises PermissionDenied
    with pytest.raises(Exception) as exc_download:
        PrescriptionUploadService.get_attachment_signed_url(
            user,
            attachment_id=quarantined_att.attachment_id,
        )
    assert "quarantined" in str(exc_download.value).lower()
