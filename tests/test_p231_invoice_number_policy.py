"""
Architecture Decision: ADR-0231 (P231).
Targeted Automated Test Suite for POS Invoice Number Policy & Collision-Resistant Identifiers.
"""

from decimal import Decimal
import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    MedicineProduct,
    PaymentMethod,
    PlanTier,
    PosInvoiceNumberPolicy,
    PosInvoiceSequenceResetFrequency,
    PosInvoiceSequenceTracker,
    PosSaleLifecycleState,
    PosSaleOrder,
    SaleInvoice,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole
from apps.core.pos_domain_service import PosDomainService
from apps.core.pos_invoice_number_service import PosInvoiceNumberService


@pytest.fixture
def invoice_policy_env(db):
    tenant = Tenant.objects.create(
        tenant_id="ten-p231-inv",
        name="Invoice Policy Pharmacy",
        subdomain="p231inv",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    branch = Branch.objects.create(
        branch_id="br-p231-dhk",
        tenant=tenant,
        name="Dhanmondi Branch",
        branch_code="DHK01",
    )

    owner = PharmacyUser.objects.create_user(
        email="owner_p231@test.com",
        phone_number="+8801711112233",
        password="PassWord123!",
        role=UserRole.PHARMACY_OWNER,
        tenant_id=tenant.tenant_id,
        branch_id=branch.branch_id,
        full_name="Owner P231",
    )
    cashier = PharmacyUser.objects.create_user(
        email="cashier_p231@test.com",
        phone_number="+8801711112234",
        password="PassWord123!",
        role=UserRole.CASHIER,
        tenant_id=tenant.tenant_id,
        branch_id=branch.branch_id,
        full_name="Cashier P231",
    )

    # Tenant B for cross-tenant checks
    tenant_b = Tenant.objects.create(
        tenant_id="ten-p231-other",
        name="Other Pharmacy",
        subdomain="otherinv",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    branch_b = Branch.objects.create(
        branch_id="br-p231-ctg",
        tenant=tenant_b,
        name="Chittagong Branch",
        branch_code="CTG01",
    )
    owner_b = PharmacyUser.objects.create_user(
        email="owner_b_p231@test.com",
        phone_number="+8801711112235",
        password="PassWord123!",
        role=UserRole.PHARMACY_OWNER,
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        full_name="Owner B P231",
    )

    product = MedicineProduct.objects.create(
        product_id="med-p231-napa",
        brand_name="Napa 500mg",
        generic_name="Paracetamol",
        strength="500mg",
        dosage_form="Tablet",
        manufacturer="Beximco",
        mrp_unit=Decimal("10.00"),
        cost_unit=Decimal("6.00"),
        is_prescription_only=False,
        is_antibiotic=False,
    )

    return {
        "tenant": tenant,
        "branch": branch,
        "owner": owner,
        "cashier": cashier,
        "product": product,
        "tenant_b": tenant_b,
        "branch_b": branch_b,
        "owner_b": owner_b,
    }


@pytest.mark.django_db
def test_p231_monotonic_sequence_generation_and_policy_preview(invoice_policy_env):
    env = invoice_policy_env
    tenant = env["tenant"]
    branch = env["branch"]

    # 1. Preview next sequence (should not reserve)
    prev = PosInvoiceNumberService.preview_next_invoice_number(tenant, branch, device_namespace="REG01")
    assert prev["sequence"] == 1
    assert "DHK01" in prev["invoice_number"]
    assert "REG01" in prev["invoice_number"]
    assert prev["invoice_number"].endswith("-0001")
    assert prev["is_reserved"] is False

    # Sequence tracker row shouldn't exist or last_sequence should be 0
    trkr = PosInvoiceSequenceTracker.objects.filter(tenant=tenant, branch=branch, device_namespace="REG01").first()
    assert trkr is None or trkr.last_sequence == 0

    # 2. Reserve next sequence
    gen1 = PosInvoiceNumberService.generate_next_invoice_number_in_tx(
        tenant, branch, device_namespace="REG01", reserve=True
    )
    assert gen1["sequence"] == 1
    assert gen1["invoice_number"].endswith("-0001")

    # 3. Reserve second sequence
    gen2 = PosInvoiceNumberService.generate_next_invoice_number_in_tx(
        tenant, branch, device_namespace="REG01", reserve=True
    )
    assert gen2["sequence"] == 2
    assert gen2["invoice_number"].endswith("-0002")
    assert gen1["invoice_number"] != gen2["invoice_number"]

    # 4. Independent device namespace sequence
    gen_tab = PosInvoiceNumberService.generate_next_invoice_number_in_tx(
        tenant, branch, device_namespace="TAB02", reserve=True
    )
    assert gen_tab["sequence"] == 1
    assert "TAB02" in gen_tab["invoice_number"]
    assert gen_tab["invoice_number"].endswith("-0001")


@pytest.mark.django_db
def test_p231_offline_collision_rejection_and_sale_checkout_integration(invoice_policy_env):
    env = invoice_policy_env
    tenant = env["tenant"]
    branch = env["branch"]
    cashier = env["cashier"]
    product = env["product"]

    # 1. Create a sale order without order_number -> should auto-generate via policy
    sale, _ = PosDomainService.create_sale_order(
        cashier,
        {
            "branch_id": branch.branch_id,
            "offline_device_namespace": "",
            "customer_name": "Walk-in Online",
            "lines": [{"product": product.product_id, "quantity_units": 1, "unit_price_bdt": "10.00"}],
        },
    )
    assert sale.order_number != ""
    assert "DHK01" in sale.order_number
    assert sale.order_number.endswith("-0001")

    # 2. Offline sync with an existing order_number -> collision rejection
    with pytest.raises(ValidationError) as exc:
        PosInvoiceNumberService.validate_and_register_offline_invoice(
            tenant=tenant,
            branch=branch,
            invoice_number=sale.order_number,
            offline_device_namespace="OFF-REG01",
            offline_local_sequence=1,
        )
    assert "already exists in POS sales" in str(exc.value)

    # 3. Offline sync with duplicate sequence on same namespace -> collision rejection
    sale_off, _ = PosDomainService.create_sale_order(
        cashier,
        {
            "branch_id": branch.branch_id,
            "order_number": "INV-DHK01-OFF01-20261116-0001",
            "offline_device_namespace": "OFF-REG01",
            "offline_local_sequence": 1001,
            "customer_name": "Walk-in Offline",
            "lines": [{"product": product.product_id, "quantity_units": 1, "unit_price_bdt": "10.00"}],
        },
    )
    assert sale_off.order_number == "INV-DHK01-OFF01-20261116-0001"

    with pytest.raises(ValidationError) as exc2:
        PosInvoiceNumberService.validate_and_register_offline_invoice(
            tenant=tenant,
            branch=branch,
            invoice_number="INV-DHK01-OFF01-20261116-0002",
            offline_device_namespace="OFF-REG01",
            offline_local_sequence=1001,  # Same sequence already synchronized
        )
    assert "already been synchronized" in str(exc2.value)


@pytest.mark.django_db
def test_p231_api_preview_reserve_and_cross_tenant_isolation(invoice_policy_env):
    env = invoice_policy_env
    cashier = env["cashier"]
    owner = env["owner"]
    owner_b = env["owner_b"]

    client = APIClient()

    # 1. Cashier previews next invoice number
    client.force_authenticate(user=cashier)
    res_prev = client.get("/api/v2/pos/invoice-policies/preview-next/?device=POS1")
    assert res_prev.status_code == status.HTTP_200_OK
    assert "POS1" in res_prev.data["invoice_number"]

    # 2. Reserve next via API
    res_res = client.post("/api/v2/pos/invoice-policies/reserve-next/", {"device": "POS1"})
    assert res_res.status_code == status.HTTP_201_CREATED
    assert res_res.data["sequence"] == 1

    # 3. Policy update by Owner
    client.force_authenticate(user=owner)
    pol_id = res_res.data["policy_id"]
    res_patch = client.patch(
        f"/api/v2/pos/invoice-policies/{pol_id}/",
        {"prefix": "BILL", "padding_digits": 5},
    )
    assert res_patch.status_code == status.HTTP_200_OK
    assert res_patch.data["prefix"] == "BILL"
    assert res_patch.data["padding_digits"] == 5

    # 4. Cross-tenant isolation: Owner B cannot view or modify Tenant A's policy
    client.force_authenticate(user=owner_b)
    res_cross = client.get(f"/api/v2/pos/invoice-policies/{pol_id}/")
    assert res_cross.status_code == status.HTTP_404_NOT_FOUND

    res_cross_patch = client.patch(f"/api/v2/pos/invoice-policies/{pol_id}/", {"prefix": "HACK"})
    assert res_cross_patch.status_code == status.HTTP_404_NOT_FOUND
