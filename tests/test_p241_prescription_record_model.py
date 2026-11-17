"""
Acceptance Tests for P241 - Prescription Record Model, Prescriber Validation & Dispense Auditing.
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Coverage:
1. Valid Scenario: Complete prescription lifecycle (Creation -> Pharmacist PCB verification -> Partial Dispense -> Full Dispense).
2. Dispense bounds & Over-dispense prevention: Rejection when dispense qty exceeds remaining prescribed quantity.
3. Expiry enforcement: Dispense blocked on expired prescription and CheckConstraint on expiry_date >= prescription_date.
4. Incomplete/invalid inputs: Validation error on missing doctor BMDC or invalid dates.
5. Cross-tenant isolation: Tenant B strictly prohibited from accessing or modifying Tenant A's prescriptions.
6. Absolute block on deletion: HTTP 405 Method Not Allowed on DELETE requests.
"""

from datetime import date, timedelta
from decimal import Decimal
import random
import uuid
import pytest
from django.db import IntegrityError
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStock,
    Branch,
    MedicineProduct,
    PrescriptionDispenseRecord,
    PrescriptionItem,
    PrescriptionRecord,
    PrescriptionStatus,
    StaffMember,
    StaffRole,
    Tenant,
)
from apps.core.prescription_service import PrescriptionService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def rx_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(tenant_id=f"ten-p241-a-{uid}", name="Prescription Tenant A", subdomain=f"p241a-{uid}")
    br_a = Branch.objects.create(tenant=ten_a, branch_id=f"br-p241-a-{uid}", name="Dhanmondi Branch", branch_code="DH01")

    # Staff & Users for Tenant A
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

    manager_user_a = PharmacyUser.objects.create_user(
        email=f"manager_a_{uid}@test.com",
        phone_number=f"+88018{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )

    # Tenant B (Cross-Tenant)
    ten_b = Tenant.objects.create(tenant_id=f"ten-p241-b-{uid}", name="Prescription Tenant B", subdomain=f"p241b-{uid}")
    br_b = Branch.objects.create(tenant=ten_b, branch_id=f"br-p241-b-{uid}", name="Banani Branch", branch_code="BN01")
    manager_user_b = PharmacyUser.objects.create_user(
        email=f"manager_b_{uid}@test.com",
        phone_number=f"+88019{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )

    # Medicine Products
    prod_amox = MedicineProduct.objects.create(
        product_id=f"prod-amox-{uid}",
        brand_name="Moxacil 500mg",
        generic_name="Amoxicillin",
        dosage_form="Capsule",
        strength="500mg",
        is_antibiotic=True,
        is_prescription_only=True,
        mrp_unit=Decimal("12.00"),
        cost_unit=Decimal("8.50"),
    )
    prod_parac = MedicineProduct.objects.create(
        product_id=f"prod-parac-{uid}",
        brand_name="Ace Plus 500mg",
        generic_name="Paracetamol + Caffeine",
        dosage_form="Tablet",
        strength="500mg+65mg",
        is_antibiotic=False,
        is_prescription_only=False,
        mrp_unit=Decimal("3.00"),
        cost_unit=Decimal("2.10"),
    )

    return {
        "tenant_a": ten_a,
        "branch_a": br_a,
        "pharmacist_user_a": pharmacist_user_a,
        "staff_pharmacist_a": staff_pharmacist_a,
        "manager_user_a": manager_user_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "manager_user_b": manager_user_b,
        "prod_amox": prod_amox,
        "prod_parac": prod_parac,
    }


@pytest.mark.django_db
def test_p241_valid_prescription_lifecycle_and_dispense_flow(rx_env):
    """
    Scenario 1: Full valid lifecycle:
    Creation -> Pharmacist verification -> Partial dispense -> Full dispense.
    """
    env = rx_env
    user = env["pharmacist_user_a"]
    today = timezone.now().date()
    expiry = today + timedelta(days=30)

    # 1. Create prescription via service
    rx = PrescriptionService.create_prescription(
        user,
        doctor_name="Prof. Dr. Shamsul Alam",
        doctor_bmdc_no="BMDC-A28341",
        doctor_workplace="Dhaka Medical College Hospital",
        prescriber_contact="+8801711223344",
        patient_token="PT-ALPHA9900",
        patient_age=45,
        patient_gender="MALE",
        diagnosis="Bacterial respiratory tract infection",
        prescription_date=today,
        expiry_date=expiry,
        items_data=[
            {
                "product_id": env["prod_amox"].product_id,
                "dosage_instructions": "1+1+1 for 7 days after meal",
                "duration_days": 7,
                "quantity_prescribed": 21,
            },
            {
                "product_id": env["prod_parac"].product_id,
                "dosage_instructions": "1+0+1 if fever exceeds 100F",
                "duration_days": 5,
                "quantity_prescribed": 10,
            },
        ],
    )

    assert rx.status == PrescriptionStatus.PENDING
    assert rx.is_expired is False
    assert rx.can_dispense is False
    assert rx.items.count() == 2

    amox_item = rx.items.get(product=env["prod_amox"])
    parac_item = rx.items.get(product=env["prod_parac"])
    assert amox_item.quantity_prescribed == 21
    assert amox_item.quantity_dispensed == 0
    assert amox_item.remaining_dispense_quantity == 21

    # 2. Pharmacist clinical verification
    verified_rx = PrescriptionService.verify_prescription(
        user,
        prescription_id=rx.prescription_id,
        decision="APPROVED",
        pharmacist_pcb_reg_no="PCB-B99214",
        notes="Clinical indications verified. Dosage appropriate for adult.",
    )
    assert verified_rx.status == PrescriptionStatus.VERIFIED
    assert verified_rx.can_dispense is True
    assert verified_rx.pharmacist_pcb_no == "PCB-B99214"
    assert verified_rx.verified_by == env["staff_pharmacist_a"]

    # 3. Partial dispense: Dispense 7 capsules of Amoxicillin
    dispense_1 = PrescriptionService.dispense_item(
        user,
        prescription_id=rx.prescription_id,
        item_id=amox_item.id,
        quantity=7,
        notes="First 2 days course taken by patient",
    )
    assert dispense_1.quantity_dispensed == 7
    amox_item.refresh_from_db()
    rx.refresh_from_db()

    assert amox_item.quantity_dispensed == 7
    assert amox_item.remaining_dispense_quantity == 14
    assert amox_item.is_dispensed is False
    assert rx.status == PrescriptionStatus.PARTIALLY_DISPENSED

    # 4. Complete remaining dispense
    PrescriptionService.dispense_item(
        user,
        prescription_id=rx.prescription_id,
        item_id=amox_item.id,
        quantity=14,
    )
    PrescriptionService.dispense_item(
        user,
        prescription_id=rx.prescription_id,
        item_id=parac_item.id,
        quantity=10,
    )
    amox_item.refresh_from_db()
    parac_item.refresh_from_db()
    rx.refresh_from_db()

    assert amox_item.is_dispensed is True
    assert parac_item.is_dispensed is True
    assert rx.status == PrescriptionStatus.DISPENSED
    assert PrescriptionDispenseRecord.objects.filter(prescription=rx).count() == 3


@pytest.mark.django_db
def test_p241_api_creation_and_dispense_endpoint(rx_env):
    """
    Scenario 2: Create prescription and execute dispense action via DRF REST API.
    """
    env = rx_env
    user = env["pharmacist_user_a"]
    client = APIClient()
    client.force_authenticate(user=user)

    today = timezone.now().date().isoformat()
    expiry = (timezone.now().date() + timedelta(days=14)).isoformat()

    post_data = {
        "doctor_name": "Dr. Fatema Zohra",
        "doctor_bmdc_no": "BMDC-D88123",
        "doctor_workplace": "BIRDEM Hospital",
        "patient_token": "PT-BIRDEM-01",
        "patient_age": 52,
        "patient_gender": "FEMALE",
        "diagnosis": "Type-2 Diabetes & Urinary Tract Infection",
        "prescription_date": today,
        "expiry_date": expiry,
        "items_data": [
            {
                "product_id": env["prod_amox"].product_id,
                "dosage_instructions": "1+0+1 for 7 days",
                "duration_days": 7,
                "quantity_prescribed": 14,
            }
        ],
    }

    res = client.post("/api/v2/prescriptions/", data=post_data, format="json")
    assert res.status_code == status.HTTP_201_CREATED, res.data
    rx_id = res.data["prescription_id"]
    item_id = res.data["items"][0]["id"]
    assert res.data["status"] == "PENDING"

    # Verify via API
    ver_res = client.post(
        f"/api/v2/prescriptions/{rx_id}/verify/",
        data={
            "pharmacist_user_id": env["staff_pharmacist_a"].staff_id,
            "decision": "APPROVED",
            "pharmacist_pcb_reg_no": "PCB-A55123",
            "notes": "Verified through REST API",
        },
        format="json",
    )
    assert ver_res.status_code == status.HTTP_200_OK, ver_res.data
    assert ver_res.data["status"] == "VERIFIED"

    # Dispense via API
    disp_res = client.post(
        f"/api/v2/prescriptions/{rx_id}/dispense/",
        data={
            "item_id": item_id,
            "quantity": 14,
            "notes": "Full course dispensed at counter",
        },
        format="json",
    )
    assert disp_res.status_code == status.HTTP_201_CREATED, disp_res.data
    assert disp_res.data["quantity_dispensed"] == 14

    # Check updated prescription status is DISPENSED
    get_res = client.get(f"/api/v2/prescriptions/{rx_id}/")
    assert get_res.status_code == status.HTTP_200_OK
    assert get_res.data["status"] == "DISPENSED"


@pytest.mark.django_db
def test_p241_dispense_bounds_and_over_dispense_prevention(rx_env):
    """
    Scenario 3: Over-dispense rejection when quantity requested exceeds remaining prescribed.
    """
    env = rx_env
    user = env["pharmacist_user_a"]

    rx = PrescriptionService.create_prescription(
        user,
        doctor_name="Dr. Monirul Islam",
        doctor_bmdc_no="BMDC-M1001",
        patient_token="PT-OVERDISP",
        items_data=[
            {
                "product_id": env["prod_amox"].product_id,
                "dosage_instructions": "1+1+1",
                "quantity_prescribed": 10,
            }
        ],
    )
    PrescriptionService.verify_prescription(
        user,
        prescription_id=rx.prescription_id,
        decision="APPROVED",
        pharmacist_pcb_reg_no="PCB-M1001",
    )

    item = rx.items.first()

    # Attempt to dispense 15 when prescribed is 10
    with pytest.raises(Exception) as exc_info:
        PrescriptionService.dispense_item(
            user,
            prescription_id=rx.prescription_id,
            item_id=item.id,
            quantity=15,
        )
    assert "Remaining allowable quantity" in str(exc_info.value)


@pytest.mark.django_db
def test_p241_expired_prescription_enforcement_and_constraint(rx_env):
    """
    Scenario 4: Prescription expiration enforcement:
    1. Dispensing on expired prescription is blocked.
    2. CheckConstraint blocks expiry_date < prescription_date.
    """
    env = rx_env
    user = env["pharmacist_user_a"]
    today = timezone.now().date()
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)

    # 1. Past expired prescription
    rx_expired = PrescriptionRecord.objects.create(
        prescription_id=f"rx-exp-{uuid.uuid4().hex[:6]}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        doctor_name="Dr. Past Expiry",
        doctor_bmdc_no="BMDC-EXP01",
        patient_token="PT-EXPIRED-01",
        prescription_date=two_days_ago,
        expiry_date=yesterday,
        status=PrescriptionStatus.VERIFIED,
    )
    item = PrescriptionItem.objects.create(
        prescription=rx_expired,
        product=env["prod_amox"],
        dosage_instructions="1+0+1",
        quantity_prescribed=10,
    )

    assert rx_expired.is_expired is True
    assert rx_expired.can_dispense is False

    with pytest.raises(Exception) as exc_info:
        PrescriptionService.dispense_item(
            user,
            prescription_id=rx_expired.prescription_id,
            item_id=item.id,
            quantity=5,
        )
    assert "Cannot dispense expired prescription" in str(exc_info.value)

    # 2. Database CheckConstraint: expiry_date < prescription_date
    with pytest.raises(IntegrityError):
        PrescriptionRecord.objects.create(
            prescription_id=f"rx-bad-date-{uuid.uuid4().hex[:6]}",
            tenant=env["tenant_a"],
            branch=env["branch_a"],
            doctor_name="Dr. Invalid Dates",
            doctor_bmdc_no="BMDC-BAD01",
            patient_token="PT-BAD-01",
            prescription_date=today,
            expiry_date=yesterday,  # Earlier than issued date!
            status=PrescriptionStatus.PENDING,
        )


@pytest.mark.django_db
def test_p241_incomplete_input_validation(rx_env):
    """
    Scenario 5: Rejection of invalid inputs (missing doctor name, missing BMDC, empty items).
    """
    env = rx_env
    user = env["pharmacist_user_a"]

    # Missing doctor name
    with pytest.raises(Exception) as exc_name:
        PrescriptionService.create_prescription(
            user,
            doctor_name="",
            doctor_bmdc_no="BMDC-12345",
            patient_token="PT-001",
        )
    assert "doctor_name" in str(exc_name.value)

    # Missing doctor BMDC
    with pytest.raises(Exception) as exc_bmdc:
        PrescriptionService.create_prescription(
            user,
            doctor_name="Dr. Valid Name",
            doctor_bmdc_no="",
            patient_token="PT-001",
        )
    assert "doctor_bmdc_no" in str(exc_bmdc.value)


@pytest.mark.django_db
def test_p241_cross_tenant_isolation_and_delete_protection(rx_env):
    """
    Scenario 6: Cross-tenant isolation & absolute block on prescription deletion.
    """
    env = rx_env
    user_a = env["pharmacist_user_a"]
    user_b = env["manager_user_b"]

    rx_a = PrescriptionService.create_prescription(
        user_a,
        doctor_name="Dr. Tenant A Doctor",
        doctor_bmdc_no="BMDC-TENANT-A",
        patient_token="PT-TENANT-A",
        items_data=[
            {
                "product_id": env["prod_amox"].product_id,
                "quantity_prescribed": 10,
            }
        ],
    )

    # Tenant B tries to view Tenant A's prescription via API
    client_b = APIClient()
    client_b.force_authenticate(user=user_b)

    # List endpoint does not expose Tenant A's prescription
    list_res = client_b.get("/api/v2/prescriptions/")
    assert list_res.status_code == status.HTTP_200_OK
    items = list_res.data if isinstance(list_res.data, list) else list_res.data.get("results", [])
    assert all(r["prescription_id"] != rx_a.prescription_id for r in items)

    # Detail endpoint returns 404 for Tenant B
    detail_res = client_b.get(f"/api/v2/prescriptions/{rx_a.prescription_id}/")
    assert detail_res.status_code == status.HTTP_404_NOT_FOUND

    # Attempt to delete Tenant A's prescription returns 405 Method Not Allowed
    client_a = APIClient()
    client_a.force_authenticate(user=user_a)
    del_res = client_a.delete(f"/api/v2/prescriptions/{rx_a.prescription_id}/")
    assert del_res.status_code in (status.HTTP_405_METHOD_NOT_ALLOWED, status.HTTP_403_FORBIDDEN)
    assert PrescriptionRecord.objects.filter(prescription_id=rx_a.prescription_id).exists()
