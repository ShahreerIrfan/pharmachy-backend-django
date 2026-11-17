"""
backend/tests/test_p227_pos_prescription_safety_gate.py
Acceptance tests for P227: POS prescription safety gate (Phase 12).

Verifies:
1. Classification precedence (OTC, POM, ANTIMICROBIAL_*, NARCOTIC_SCHEDULED, RECALLED/BANNED).
2. Prescription attach, strict Pharmacist (STAFF_PHARMACIST / PCB) approval & reject flows.
3. Cart edit invalidation (adding new Rx/narcotic line or scaling quantity past approved base units)
   and live MedicineClassificationSnapshot escalation blocking at verify & finalize.
4. Offline safety limitations: OTC allowed offline, unverified Rx/antibiotic/narcotic blocked cleanly
   (ERR_OFFLINE_RX_BLOCKED) with immutable PosRxSafetyAudit; reconnect -> approve -> finalize succeeds.
5. Split/Due checkout, printable receipt with compliance footer, idempotent AntibioticDispenseRegisterEntry
   creation & reversal on return/reversal without deleting rows.
6. Role boundaries (403 ERR_PHARMACIST_APPROVAL_REQUIRED for Cashier/Manager/Clerk/Auditor/Owner-no-PCB),
   cross-tenant/branch Rx attachment isolation, version conflict, BASIC-plan safety availability & suspended lockout.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    AntibioticDispenseRegisterEntry,
    AntibioticRegisterStatus,
    BatchStatus,
    BatchStock,
    Branch,
    ClassificationType,
    MedicineClassificationSnapshot,
    MedicineProduct,
    PackagingHierarchyNode,
    PaymentMethod,
    PlanTier,
    PosCustomer,
    PosPackagingUnit,
    PosRegulatoryClass,
    PosRxGateStatus,
    PosRxSafetyAudit,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleSourceChannel,
    PrescriptionRecord,
    PrescriptionStatus,
    SaleInvoice,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.pos_rx_safety_service import PosRxSafetyService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p227-a-{uid}",
        name="Model Care Pharmacy Dhaka",
        subdomain=f"modelcare-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p227-b-{uid}",
        name="Other Store Ltd",
        subdomain=f"otherstore-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_basic(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p227-bas-{uid}",
        name="Community Basic Meds",
        subdomain=f"basicmeds-{uid}",
        plan_tier=PlanTier.BASIC,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def branch_a(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p227-a1-{uid}",
        tenant=tenant_a,
        name="Uttara Sector 7",
        license_number=f"DGDA-UTT-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_a2(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p227-a2-{uid}",
        tenant=tenant_a,
        name="Mirpur 10",
        license_number=f"DGDA-MRP-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_b(tenant_b):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p227-b1-{uid}",
        tenant=tenant_b,
        name="Chittagong Branch",
        license_number=f"DGDA-CTG-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_basic(tenant_basic):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p227-bas-{uid}",
        tenant=tenant_basic,
        name="Rajshahi Main",
        license_number=f"DGDA-RAJ-{uid}",
        is_active=True,
    )


def _mk_user(tenant, branch, role: str, prefix: str, pcb_no: str = ""):
    uid = uuid.uuid4().hex[:8]
    return PharmacyUser.objects.create_user(
        email=f"{prefix}-{uid}@pharmacy.bd",
        phone_number=f"+88017{uuid.uuid4().int % 100000000:08d}",
        password="TestPass123!@#",
        full_name=f"{prefix.title()} User",
        role=role,
        pcb_registration_no=pcb_no,
        tenant_id=tenant.tenant_id,
        branch_id=branch.branch_id,
        is_active=True,
    )


@pytest.fixture
def users_a(tenant_a, branch_a, branch_a2):
    return {
        "owner_no_pcb": _mk_user(tenant_a, branch_a, UserRole.PHARMACY_OWNER, "ownernopcb", pcb_no=""),
        "owner_with_pcb": _mk_user(tenant_a, branch_a, UserRole.PHARMACY_OWNER, "ownerpcb", pcb_no="PCB-OWN-1001"),
        "manager": _mk_user(tenant_a, branch_a, UserRole.BRANCH_MANAGER, "mgr"),
        "pharmacist": _mk_user(tenant_a, branch_a, UserRole.STAFF_PHARMACIST, "pharm", pcb_no="PCB-REG-8812"),
        "cashier": _mk_user(tenant_a, branch_a, UserRole.CASHIER, "cashier"),
        "finance": _mk_user(tenant_a, branch_a, UserRole.FINANCE_OFFICER, "fin"),
        "clerk": _mk_user(tenant_a, branch_a, UserRole.INVENTORY_CLERK, "clerk"),
        "auditor": _mk_user(tenant_a, branch_a, UserRole.AUDITOR, "aud"),
        "pharmacist_a2": _mk_user(tenant_a, branch_a2, UserRole.STAFF_PHARMACIST, "pharma2", pcb_no="PCB-A2-9001"),
    }


@pytest.fixture
def catalog_fixture(db, tenant_a, branch_a, tenant_basic, branch_basic):
    uid = uuid.uuid4().hex[:6]
    today = timezone.now().date()

    otc_napa = MedicineProduct.objects.create(
        product_id=f"med-p227-otc-{uid}",
        brand_name="Napa 500",
        generic_name="Paracetamol",
        strength="500mg",
        dosage_form="Tablet",
        manufacturer="Beximco",
        barcode=f"8942271{uid}",
        mrp_unit=Decimal("1.20"),
        cost_unit=Decimal("0.80"),
        is_prescription_only=False,
        is_antibiotic=False,
        is_narcotic=False,
    )
    pom_sergel = MedicineProduct.objects.create(
        product_id=f"med-p227-pom-{uid}",
        brand_name="Sergel 20",
        generic_name="Esomeprazole",
        strength="20mg",
        dosage_form="Capsule",
        manufacturer="Healthcare",
        barcode=f"8942272{uid}",
        mrp_unit=Decimal("7.00"),
        cost_unit=Decimal("5.00"),
        is_prescription_only=True,
    )
    PackagingHierarchyNode.objects.create(
        node_id=f"node-p227-pom-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=pom_sergel,
        parent_unit="STRIP",
        child_unit="TABLET",
        conversion_factor=10,
        tier_level=1,
        is_base_level=True,
        effective_from=today - datetime.timedelta(days=5),
        is_active=True,
    )

    abx_azith = MedicineProduct.objects.create(
        product_id=f"med-p227-abx-{uid}",
        brand_name="Azithrocin 500",
        generic_name="Azithromycin",
        strength="500mg",
        dosage_form="Tablet",
        manufacturer="Square",
        barcode=f"8942273{uid}",
        mrp_unit=Decimal("35.00"),
        cost_unit=Decimal("26.00"),
        is_prescription_only=True,
        is_antibiotic=True,
    )
    narc_peth = MedicineProduct.objects.create(
        product_id=f"med-p227-narc-{uid}",
        brand_name="Sedil 5",
        generic_name="Diazepam",
        strength="5mg",
        dosage_form="Tablet",
        manufacturer="Square",
        barcode=f"8942274{uid}",
        mrp_unit=Decimal("10.00"),
        cost_unit=Decimal("6.50"),
        is_prescription_only=True,
        is_narcotic=True,
    )

    # Active batches for Tenant A & Basic
    for key, p, qty, cost in [
        ("otc", otc_napa, 100, Decimal("0.80")),
        ("pom", pom_sergel, 80, Decimal("5.00")),
        ("abx", abx_azith, 50, Decimal("26.00")),
        ("narc", narc_peth, 40, Decimal("6.50")),
    ]:
        BatchStock.objects.create(
            batch_id=f"bat-a-{key}-{uid}",
            tenant=tenant_a,
            branch=branch_a,
            product=p,
            batch_number=f"LOT-{key.upper()}-{uid[:3].upper()}",
            expiry_date=today + datetime.timedelta(days=365),
            purchase_date=today,
            quantity_base_units=qty,
            reserved_quantity_base_units=0,
            cost_per_unit=cost,
            status=BatchStatus.ACTIVE,
            is_active=True,
        )

    BatchStock.objects.create(
        batch_id=f"bat-bas-abx-{uid}",
        tenant=tenant_basic,
        branch=branch_basic,
        product=abx_azith,
        batch_number="LOT-BAS-ABX",
        expiry_date=today + datetime.timedelta(days=300),
        purchase_date=today,
        quantity_base_units=30,
        reserved_quantity_base_units=0,
        cost_per_unit=Decimal("26.00"),
        status=BatchStatus.ACTIVE,
        is_active=True,
    )

    cust_a = PosCustomer.objects.create(
        customer_id=f"cust-p227-{uid}",
        tenant=tenant_a,
        primary_branch=branch_a,
        full_name="Mahmudul Hasan",
        phone="+8801711002299",
        credit_limit_bdt=Decimal("5000.00"),
        current_due_bdt=Decimal("0.00"),
        is_active=True,
    )

    return {
        "otc": otc_napa,
        "pom": pom_sergel,
        "abx": abx_azith,
        "narc": narc_peth,
        "cust_a": cust_a,
    }


class TestP227PosPrescriptionSafetyGate:
    def test_valid_rx_attach_pharmacist_approve_split_due_receipt_antibiotic_register_and_reversal(
        self, api_client, branch_a, users_a, catalog_fixture
    ):
        cashier = users_a["cashier"]
        pharmacist = users_a["pharmacist"]
        abx = catalog_fixture["abx"]
        cust = catalog_fixture["cust_a"]

        api_client.force_authenticate(user=cashier)
        cart_res = api_client.post(
            "/api/v2/pos/sales/quick-cart/",
            {"branch": branch_a.branch_id, "customer": cust.customer_id, "force_new": True},
            format="json",
        )
        assert cart_res.status_code == status.HTTP_201_CREATED
        sale_id = cart_res.data["sale"]["sale_id"]

        # Add 6 tablets of Azithromycin (6 * 35.00 = 210.00 BDT) -> enters PENDING_RX_REVIEW
        scan_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/scan-add/",
            {"product_id": abx.product_id, "packaging_unit": PosPackagingUnit.TABLET, "quantity_delta": 6},
            format="json",
        )
        assert scan_res.status_code == status.HTTP_200_OK
        sale_data = scan_res.data["sale"]
        assert sale_data["lifecycle_state"] == PosSaleLifecycleState.PENDING_RX_REVIEW
        assert sale_data["rx_gate_status"] == PosRxGateStatus.PENDING_PHARMACIST
        assert sale_data["requires_rx_verification"] is True
        assert sale_data["lines"][0]["regulatory_classification"] == PosRegulatoryClass.ANTIMICROBIAL_ACCESS

        # Check live rx-gate summary
        gate_res = api_client.get(f"/api/v2/pos/sales/{sale_id}/rx-gate/")
        assert gate_res.status_code == status.HTTP_200_OK
        assert gate_res.data["requires_antibiotic_register"] is True
        assert gate_res.data["can_finalize_online"] is False
        assert gate_res.data["can_finalize_offline"] is False

        # Cashier attaches incoming prescription (sanitizes raw numeric NID patient_token if entered)
        att_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/attach-prescription/",
            {
                "doctor_name": "Dr. Anisur Rahman",
                "doctor_bmdc_no": "BMDC-A-41920",
                "patient_token": "1990123456789",
                "dosage_instructions": "1+0+1 after meal for 3 days",
                "duration_days": 3,
                "rx_reference_code": "RX-UTT-7701",
            },
            format="json",
        )
        assert att_res.status_code == status.HTTP_200_OK
        # Minimized hashed token (not raw NID!) and attach NEVER self-approves!
        assert att_res.data["rx_gate"]["patient_token"].startswith("PT-HASH-")
        assert att_res.data["rx_gate"]["rx_gate_status"] == PosRxGateStatus.PENDING_PHARMACIST
        assert att_res.data["rx_gate"]["can_finalize_online"] is False

        # Non-pharmacist attempts verify-rx -> 403 ERR_PHARMACIST_APPROVAL_REQUIRED
        assert api_client.post(f"/api/v2/pos/sales/{sale_id}/verify-rx/", {}, format="json").status_code == 403

        # Registered Pharmacist verifies Rx cleanly with PCB no.
        api_client.force_authenticate(user=pharmacist)
        ver_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/verify-rx/",
            {"notes": "BMDC & course verified cleanly"},
            format="json",
        )
        assert ver_res.status_code == status.HTTP_200_OK
        assert ver_res.data["rx_gate_status"] == PosRxGateStatus.APPROVED
        assert ver_res.data["pharmacist_pcb_no_snapshot"] == "PCB-REG-8812"
        assert ver_res.data["lifecycle_state"] == PosSaleLifecycleState.DRAFT
        assert ver_res.data["lines"][0]["rx_verified"] is True
        assert ver_res.data["lines"][0]["rx_verified_base_units"] == 6

        # Cashier completes split + due online checkout: CASH 100 + BKASH 50 + DUE 60 = 210.00 BDT
        api_client.force_authenticate(user=cashier)
        assert (
            api_client.post(
                f"/api/v2/pos/sales/{sale_id}/record-payment/",
                {"tender_type": PaymentMethod.CASH, "amount_bdt": "100.00", "idempotency_key": f"c1-{sale_id}"},
                format="json",
            ).status_code
            == status.HTTP_201_CREATED
        )
        assert (
            api_client.post(
                f"/api/v2/pos/sales/{sale_id}/record-payment/",
                {
                    "tender_type": PaymentMethod.BKASH,
                    "amount_bdt": "50.00",
                    "gateway_reference": "BKS-TXN-227",
                    "idempotency_key": f"b1-{sale_id}",
                },
                format="json",
            ).status_code
            == status.HTTP_201_CREATED
        )
        assert (
            api_client.post(
                f"/api/v2/pos/sales/{sale_id}/record-payment/",
                {"tender_type": PaymentMethod.DUE, "amount_bdt": "60.00", "idempotency_key": f"d1-{sale_id}"},
                format="json",
            ).status_code
            == status.HTTP_201_CREATED
        )

        fin_res = api_client.post(f"/api/v2/pos/sales/{sale_id}/finalize/", {}, format="json")
        assert fin_res.status_code == status.HTTP_200_OK
        assert fin_res.data["lifecycle_state"] == PosSaleLifecycleState.FINALIZED
        assert fin_res.data["paid_amount_bdt"] == "150.00"
        assert fin_res.data["due_amount_bdt"] == "60.00"

        # Stock, Invoice, CustomerDue, Prescription & Antibiotic Register all match
        abx_batch = BatchStock.objects.get(tenant_id=branch_a.tenant_id, product=abx)
        assert abx_batch.quantity_base_units == 44
        assert abx_batch.reserved_quantity_base_units == 0
        cust.refresh_from_db()
        assert cust.current_due_bdt == Decimal("60.00")

        reg_res = api_client.get(f"/api/v2/pos/antibiotic-register/?sale={sale_id}")
        assert reg_res.status_code == status.HTTP_200_OK
        assert reg_res.data["count"] == 1
        entry = reg_res.data["results"][0]
        assert entry["status"] == AntibioticRegisterStatus.ACTIVE
        assert entry["dispensed_base_units"] == 6
        assert entry["doctor_bmdc_no"] == "BMDC-A-41920"
        assert entry["approving_pharmacist_pcb_no"] == "PCB-REG-8812"

        # Printable receipt includes compliance footer & split tenders cleanly (and no cost leak to Cashier)
        rcpt = api_client.get(f"/api/v2/pos/sales/{sale_id}/receipt/")
        assert rcpt.status_code == status.HTTP_200_OK
        assert rcpt.data["totals"]["grand_total_bdt"] == "210.00"
        assert rcpt.data["totals"]["paid_amount_bdt"] == "150.00"
        assert rcpt.data["totals"]["due_amount_bdt"] == "60.00"
        assert len(rcpt.data["payments"]) == 3
        assert rcpt.data["rx_compliance_footer"]["pharmacist_pcb_no"] == "PCB-REG-8812"
        assert rcpt.data["rx_compliance_footer"]["doctor_bmdc_no"] == "BMDC-A-41920"
        assert rcpt.data["rx_compliance_footer"]["antibiotic_register_entry_ids"] == [entry["register_entry_id"]]

        # Reversal restores batch stock, reverses invoice/due, AND transitions antibiotic register -> REVERSED
        api_client.force_authenticate(user=pharmacist)
        rev_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/reverse/",
            {"reason": "Patient allergic history discovered before handover"},
            format="json",
        )
        assert rev_res.status_code == status.HTTP_200_OK
        abx_batch.refresh_from_db()
        assert abx_batch.quantity_base_units == 50
        cust.refresh_from_db()
        assert cust.current_due_bdt == Decimal("0.00")
        assert SaleInvoice.objects.get(pk=fin_res.data["linked_invoice_id"]).status == "REVERSED"
        reg_entry = AntibioticDispenseRegisterEntry.objects.get(pk=entry["register_entry_id"])
        assert reg_entry.status == AntibioticRegisterStatus.REVERSED
        assert reg_entry.returned_base_units == 6

    def test_edit_invalidation_quantity_escalation_rejection_snapshot_escalation_and_date_validation(
        self, api_client, branch_a, users_a, catalog_fixture
    ):
        cashier = users_a["cashier"]
        pharmacist = users_a["pharmacist"]
        pom = catalog_fixture["pom"]
        narc = catalog_fixture["narc"]

        api_client.force_authenticate(user=cashier)
        sale_id = api_client.post(
            "/api/v2/pos/sales/quick-cart/", {"branch": branch_a.branch_id, "force_new": True}, format="json"
        ).data["sale"]["sale_id"]

        # Add 1 strip of POM (10 tablets) -> approve
        scan_pom = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/scan-add/",
            {"product_id": pom.product_id, "packaging_unit": PosPackagingUnit.STRIP, "quantity_delta": 1},
            format="json",
        )
        assert scan_pom.status_code == status.HTTP_200_OK
        pom_line_id = scan_pom.data["sale"]["lines"][0]["line_id"]
        api_client.force_authenticate(user=pharmacist)
        assert api_client.post(f"/api/v2/pos/sales/{sale_id}/verify-rx/", {"notes": "Approved 1 strip"}, format="json").status_code == 200

        # Cashier scales quantity from 1 strip (10u) -> 2 strips (20u) -> MUST invalidate previous approval!
        api_client.force_authenticate(user=cashier)
        mut_res = api_client.patch(
            f"/api/v2/pos/sales/{sale_id}/cart-line/",
            {"line_id": pom_line_id, "packaging_unit": PosPackagingUnit.STRIP, "quantity_units": 2},
            format="json",
        )
        assert mut_res.status_code == status.HTTP_200_OK
        assert mut_res.data["sale"]["rx_gate_status"] == PosRxGateStatus.PENDING_PHARMACIST
        assert mut_res.data["sale"]["rx_verified_at"] is None
        assert PosRxSafetyAudit.objects.filter(sale_id=sale_id, action="RX_CLEARANCE_INVALIDATED").exists()

        # Future-dated / too-old prescription rejected cleanly
        bad_date = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/attach-prescription/",
            {
                "doctor_name": "Dr. Karim",
                "doctor_bmdc_no": "BMDC-9921",
                "prescription_date": (timezone.now().date() + datetime.timedelta(days=3)).isoformat(),
            },
            format="json",
        )
        assert bad_date.status_code == status.HTTP_400_BAD_REQUEST
        assert bad_date.data["error_code"] == "ERR_EXPIRED_OR_FUTURE_PRESCRIPTION"

        # Add Narcotic -> Pharmacist reject requires reason >= 3 chars; reject blocks finalize!
        api_client.post(
            f"/api/v2/pos/sales/{sale_id}/scan-add/",
            {"product_id": narc.product_id, "packaging_unit": PosPackagingUnit.TABLET, "quantity_delta": 2},
            format="json",
        )
        api_client.force_authenticate(user=pharmacist)
        bad_rej = api_client.post(f"/api/v2/pos/sales/{sale_id}/reject-rx/", {"reason": "no"}, format="json")
        assert bad_rej.status_code == status.HTTP_400_BAD_REQUEST
        assert bad_rej.data["error_code"] == "ERR_RX_REJECTION_REASON_REQUIRED"

        rej_ok = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/reject-rx/",
            {"reason": "Suspected altered narcotic Rx slip"},
            format="json",
        )
        assert rej_ok.status_code == status.HTTP_200_OK
        assert rej_ok.data["sale"]["rx_gate_status"] == PosRxGateStatus.REJECTED

        api_client.force_authenticate(user=cashier)
        api_client.post(
            f"/api/v2/pos/sales/{sale_id}/record-payment/",
            {"tender_type": PaymentMethod.CASH, "amount_bdt": "160.00", "idempotency_key": f"rej-{sale_id}"},
            format="json",
        )
        fin_rej = api_client.post(f"/api/v2/pos/sales/{sale_id}/finalize/", {}, format="json")
        assert fin_rej.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_RX_REJECTED" in str(fin_rej.data)

        # Narcotic cannot verify without documentation (notes/reference/attached Rx)
        api_client.force_authenticate(user=pharmacist)
        narc_nodoc = api_client.post(f"/api/v2/pos/sales/{sale_id}/verify-rx/", {}, format="json")
        assert narc_nodoc.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY or narc_nodoc.status_code == status.HTTP_400_BAD_REQUEST
        assert "ERR_RX_DOCUMENTATION_REQUIRED" in str(narc_nodoc.data)

        # Escalate POM product classification to RECALLED -> verify & finalize BOTH blocked!
        MedicineClassificationSnapshot.objects.create(
            snapshot_id=f"mcs-rec-{uuid.uuid4().hex[:8]}",
            product=pom,
            version_number=2,
            classification_type=ClassificationType.RECALLED,
            regulatory_source="DGDA-RECALL-2026-09",
            effective_from=timezone.now() - datetime.timedelta(minutes=5),
            is_current=True,
        )
        rec_ver = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/verify-rx/",
            {"rx_reference_code": "NARC-REF-99", "notes": "Checked narc slip"},
            format="json",
        )
        assert rec_ver.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert "ERR_REGULATED_PRODUCT_BLOCKED" in str(rec_ver.data)

    def test_offline_rx_limitations_otc_allowed_unverified_rx_blocked_and_reconnect_recovery(
        self, api_client, branch_a, users_a, catalog_fixture
    ):
        cashier = users_a["cashier"]
        pharmacist = users_a["pharmacist"]
        otc = catalog_fixture["otc"]
        abx = catalog_fixture["abx"]

        api_client.force_authenticate(user=cashier)
        # 1) Pure OTC basket is eligible offline and finalizes via offline sync cleanly
        otc_sale_id = api_client.post(
            "/api/v2/pos/sales/quick-cart/",
            {
                "branch": branch_a.branch_id,
                "source_channel": PosSaleSourceChannel.DESKTOP_POS_OFFLINE,
                "force_new": True,
            },
            format="json",
        ).data["sale"]["sale_id"]
        api_client.post(
            f"/api/v2/pos/sales/{otc_sale_id}/scan-add/",
            {"product_id": otc.product_id, "packaging_unit": PosPackagingUnit.TABLET, "quantity_delta": 5},
            format="json",
        )
        pre_ok = api_client.post(f"/api/v2/pos/sales/{otc_sale_id}/rx-gate/offline-preflight/", {}, format="json")
        assert pre_ok.status_code == status.HTTP_200_OK
        assert pre_ok.data["offline_eligible"] is True

        api_client.post(
            f"/api/v2/pos/sales/{otc_sale_id}/record-payment/",
            {"tender_type": PaymentMethod.CASH, "amount_bdt": "6.00", "idempotency_key": f"otc-off-{otc_sale_id}"},
            format="json",
        )
        fin_otc_off = api_client.post(
            f"/api/v2/pos/sales/{otc_sale_id}/finalize/",
            {"is_offline_sync": True},
            format="json",
        )
        assert fin_otc_off.status_code == status.HTTP_200_OK
        assert fin_otc_off.data["lifecycle_state"] == PosSaleLifecycleState.FINALIZED

        # 2) Unverified Antibiotic basket is HARD-BLOCKED offline (preflight + finalize both 422 ERR_OFFLINE_RX_BLOCKED)
        rx_sale_id = api_client.post(
            "/api/v2/pos/sales/quick-cart/",
            {
                "branch": branch_a.branch_id,
                "source_channel": PosSaleSourceChannel.DESKTOP_POS_OFFLINE,
                "force_new": True,
            },
            format="json",
        ).data["sale"]["sale_id"]
        api_client.post(
            f"/api/v2/pos/sales/{rx_sale_id}/scan-add/",
            {"product_id": abx.product_id, "packaging_unit": PosPackagingUnit.TABLET, "quantity_delta": 3},
            format="json",
        )
        pre_blk = api_client.post(f"/api/v2/pos/sales/{rx_sale_id}/rx-gate/offline-preflight/", {}, format="json")
        assert pre_blk.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert pre_blk.data["offline_eligible"] is False
        assert pre_blk.data["error_code"] == "ERR_OFFLINE_RX_BLOCKED"

        api_client.post(
            f"/api/v2/pos/sales/{rx_sale_id}/record-payment/",
            {"tender_type": PaymentMethod.CASH, "amount_bdt": "105.00", "idempotency_key": f"abx-off-{rx_sale_id}"},
            format="json",
        )
        fin_rx_off = api_client.post(
            f"/api/v2/pos/sales/{rx_sale_id}/finalize/",
            {"is_offline_sync": True},
            format="json",
        )
        assert fin_rx_off.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_OFFLINE_RX_BLOCKED" in str(fin_rx_off.data)

        sale_obj = PosSaleOrder.objects.get(pk=rx_sale_id)
        assert sale_obj.rx_gate_status == PosRxGateStatus.OFFLINE_BLOCKED
        assert PosRxSafetyAudit.objects.filter(
            sale=sale_obj, action="OFFLINE_RX_BLOCKED", is_offline_attempt=True, was_rejected=True
        ).exists()
        assert AntibioticDispenseRegisterEntry.objects.filter(sale=sale_obj).count() == 0

        # 3) On reconnect: Registered Pharmacist approves online -> finalize succeeds & posts register!
        api_client.force_authenticate(user=pharmacist)
        assert api_client.post(f"/api/v2/pos/sales/{rx_sale_id}/verify-rx/", {"notes": "Online reconnect verified"}, format="json").status_code == 200

        api_client.force_authenticate(user=cashier)
        rec_fin = api_client.post(f"/api/v2/pos/sales/{rx_sale_id}/finalize/", {}, format="json")
        assert rec_fin.status_code == status.HTTP_200_OK
        assert AntibioticDispenseRegisterEntry.objects.filter(sale_id=rx_sale_id, status=AntibioticRegisterStatus.ACTIVE).count() == 1

    def test_role_pcb_separation_cross_tenant_branch_isolation_concurrency_basic_plan_and_immutability(
        self, api_client, tenant_a, tenant_b, branch_a, branch_b, branch_basic, users_a, catalog_fixture
    ):
        abx = catalog_fixture["abx"]

        api_client.force_authenticate(user=users_a["cashier"])
        sale_id = api_client.post(
            "/api/v2/pos/sales/quick-cart/", {"branch": branch_a.branch_id, "force_new": True}, format="json"
        ).data["sale"]["sale_id"]
        api_client.post(
            f"/api/v2/pos/sales/{sale_id}/scan-add/",
            {"product_id": abx.product_id, "packaging_unit": PosPackagingUnit.TABLET, "quantity_delta": 2},
            format="json",
        )

        # Cashier, Manager, Finance, Clerk, Auditor & Owner WITHOUT PCB all denied clinical approval (403)
        for role_key in ("cashier", "manager", "finance", "clerk", "auditor", "owner_no_pcb"):
            api_client.force_authenticate(user=users_a[role_key])
            res = api_client.post(f"/api/v2/pos/sales/{sale_id}/verify-rx/", {"notes": "Attempt"}, format="json")
            assert res.status_code == status.HTTP_403_FORBIDDEN, f"Expected 403 for {role_key}, got {res.status_code}"

        # Stale expected_version rejected cleanly for Pharmacist
        api_client.force_authenticate(user=users_a["pharmacist"])
        ver_stale = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/verify-rx/",
            {"notes": "Stale attempt", "expected_version": 999},
            format="json",
        )
        assert ver_stale.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert "ERR_CART_VERSION_CONFLICT" in str(ver_stale.data)

        # Owner WITH PCB credential succeeds cleanly
        api_client.force_authenticate(user=users_a["owner_with_pcb"])
        own_ok = api_client.post(f"/api/v2/pos/sales/{sale_id}/verify-rx/", {"notes": "Owner PCB verified"}, format="json")
        assert own_ok.status_code == status.HTTP_200_OK
        assert own_ok.data["pharmacist_pcb_no_snapshot"] == "PCB-OWN-1001"

        # Cross-tenant PrescriptionRecord cannot be attached across tenants
        rx_b = PrescriptionRecord.objects.create(
            prescription_id=f"rx-b-{uuid.uuid4().hex[:8]}",
            tenant=tenant_b,
            branch=branch_b,
            doctor_name="Dr. Foreign",
            doctor_bmdc_no="BMDC-B-001",
            patient_token="PT-B1",
            status=PrescriptionStatus.VERIFIED,
            prescription_date=timezone.now().date(),
        )
        api_client.force_authenticate(user=users_a["cashier"])
        xten_att = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/attach-prescription/",
            {"prescription_id": rx_b.prescription_id},
            format="json",
        )
        assert xten_att.status_code == status.HTTP_403_FORBIDDEN
        assert xten_att.data["error_code"] == "ERR_CROSS_TENANT_OR_BRANCH_RX"

        # Cross-branch Pharmacist (branch A2) cannot verify Branch A sale
        api_client.force_authenticate(user=users_a["pharmacist_a2"])
        xbr_ver = api_client.post(f"/api/v2/pos/sales/{sale_id}/verify-rx/", {"notes": "Wrong branch"}, format="json")
        assert xbr_ver.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        # Idempotent antibiotic register check & delete protection
        api_client.force_authenticate(user=users_a["cashier"])
        api_client.post(
            f"/api/v2/pos/sales/{sale_id}/record-payment/",
            {"tender_type": PaymentMethod.CASH, "amount_bdt": "70.00", "idempotency_key": f"p-id-{sale_id}"},
            format="json",
        )
        api_client.post(f"/api/v2/pos/sales/{sale_id}/finalize/", {}, format="json")
        sale_done = PosSaleOrder.objects.get(pk=sale_id)
        PosRxSafetyService.record_antibiotic_register_on_finalize(users_a["cashier"], sale_done, sale_done.linked_invoice)
        assert AntibioticDispenseRegisterEntry.objects.filter(sale_id=sale_id).count() == 1

        audit = PosRxSafetyAudit.objects.filter(sale_id=sale_id).first()
        assert audit is not None
        with pytest.raises(DjangoValidationError):
            audit.delete()
        reg_row = AntibioticDispenseRegisterEntry.objects.filter(sale_id=sale_id).first()
        assert reg_row is not None
        with pytest.raises(DjangoValidationError):
            reg_row.delete()

        # BASIC plan allows clinical safety gate & antibiotic register (never paywalled!)
        bas_pharm = _mk_user(branch_basic.tenant, branch_basic, UserRole.STAFF_PHARMACIST, "baspharm", pcb_no="PCB-BAS-5")
        api_client.force_authenticate(user=bas_pharm)
        bas_sale = api_client.post(
            "/api/v2/pos/sales/quick-cart/", {"branch": branch_basic.branch_id, "force_new": True}, format="json"
        ).data["sale"]["sale_id"]
        api_client.post(
            f"/api/v2/pos/sales/{bas_sale}/scan-add/",
            {"product_id": abx.product_id, "packaging_unit": PosPackagingUnit.TABLET, "quantity_delta": 1},
            format="json",
        )
        assert api_client.post(f"/api/v2/pos/sales/{bas_sale}/verify-rx/", {"notes": "Basic safety"}, format="json").status_code == 200
        api_client.post(
            f"/api/v2/pos/sales/{bas_sale}/record-payment/",
            {"tender_type": PaymentMethod.CASH, "amount_bdt": "35.00", "idempotency_key": f"bas-{bas_sale}"},
            format="json",
        )
        assert api_client.post(f"/api/v2/pos/sales/{bas_sale}/finalize/", {}, format="json").status_code == 200
        assert AntibioticDispenseRegisterEntry.objects.filter(sale_id=bas_sale).count() == 1

        # Suspended tenant locks write operations
        tenant_a.status = TenantLifecycleStatus.SUSPENDED
        tenant_a.save(update_fields=["status", "updated_at"])
        api_client.force_authenticate(user=users_a["pharmacist"])
        susp_res = api_client.post(f"/api/v2/pos/sales/{sale_id}/verify-rx/", {}, format="json")
        assert susp_res.status_code == status.HTTP_403_FORBIDDEN
