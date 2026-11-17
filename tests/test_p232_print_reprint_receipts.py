"""
Architecture Decision: ADR-0232 (P232).
Targeted Automated Test Suite for POS 58mm/80mm Thermal and A4 Receipt Printing,
Printer Status Tracking, Watermarks, and Audited Reprints.
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
    PosPrinterStatus,
    PosPrintJobType,
    PosReceiptPrintFormat,
    PosReceiptPrintLog,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSyncState,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole
from apps.core.pos_domain_service import PosDomainService
from apps.core.pos_receipt_service import PosReceiptService


@pytest.fixture
def receipt_print_env(db):
    tenant_a = Tenant.objects.create(
        tenant_id="ten-p232-a",
        name="Receipt Printing Pharmacy A",
        subdomain="p232a",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    branch_a = Branch.objects.create(
        branch_id="br-p232-dhk",
        tenant=tenant_a,
        name="Dhanmondi Branch",
        branch_code="DHK01",
    )
    cashier_a = PharmacyUser.objects.create_user(
        email="cashier_p232a@test.com",
        phone_number="+8801722223344",
        password="PassWord123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        full_name="Cashier A",
    )

    tenant_b = Tenant.objects.create(
        tenant_id="ten-p232-b",
        name="Receipt Printing Pharmacy B",
        subdomain="p232b",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    branch_b = Branch.objects.create(
        branch_id="br-p232-ctg",
        tenant=tenant_b,
        name="Chittagong Branch",
        branch_code="CTG01",
    )
    cashier_b = PharmacyUser.objects.create_user(
        email="cashier_p232b@test.com",
        phone_number="+8801722223355",
        password="PassWord123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        full_name="Cashier B",
    )

    product = MedicineProduct.objects.create(
        product_id="prod-p232-para",
        brand_name="Napa Extra 500mg/65mg",
        generic_name="Paracetamol + Caffeine",
        dosage_form="Tablet",
        strength="500mg+65mg",
        is_antibiotic=False,
        is_narcotic=False,
        is_prescription_only=False,
        mrp_unit=Decimal("2.50"),
        cost_unit=Decimal("1.80"),
    )

    # Completed sale for Tenant A
    sale_a, _ = PosDomainService.create_sale_order(
        cashier_a,
        {
            "branch_id": branch_a.branch_id,
            "offline_device_namespace": "",
            "customer_name": "Walk-in Customer",
            "lines": [
                {
                    "product": product.product_id,
                    "quantity_units": 10,
                    "unit_price_bdt": "2.50",
                    "batch_number": "B-2026-N1",
                }
            ],
            "payments": [
                {
                    "payment_method": PaymentMethod.CASH,
                    "tender_type": "CASH",
                    "amount_bdt": "25.00",
                }
            ],
            "discount_amount_bdt": "0.00",
            "notes": "P232 Receipt Test Sale",
        },
    )

    return {
        "tenant_a": tenant_a,
        "branch_a": branch_a,
        "cashier_a": cashier_a,
        "tenant_b": tenant_b,
        "branch_b": branch_b,
        "cashier_b": cashier_b,
        "product": product,
        "sale_a": sale_a,
    }


@pytest.mark.django_db
def test_p232_scenario1_original_receipt_preview_and_print(receipt_print_env):
    """
    Scenario 1: Valid original print of completed sale:
    - 58mm & 80mm formatting.
    - No duplicate watermark.
    - Printer status recorded as READY.
    - PosReceiptPrintLog audit record created with reprint_sequence = 0.
    """
    env = receipt_print_env
    client = APIClient()
    client.force_authenticate(user=env["cashier_a"])

    # 1. Preview
    res_prev = client.get(
        f"/api/v2/pos/sales/{env['sale_a'].sale_id}/receipt-preview/?print_format=THERMAL_80MM"
    )
    assert res_prev.status_code == status.HTTP_200_OK
    assert res_prev.data["invoice_number"] == env["sale_a"].order_number
    assert res_prev.data["reprint_count"] == 0
    assert res_prev.data["is_duplicate"] is False
    assert res_prev.data["watermark_text"] == ""
    assert "Napa Extra" in res_prev.data["raw_receipt_text"]

    # 2. Original Print
    res_print = client.post(
        f"/api/v2/pos/sales/{env['sale_a'].sale_id}/print-receipt/",
        {
            "print_format": PosReceiptPrintFormat.THERMAL_80MM,
            "printer_status": PosPrinterStatus.READY,
            "printer_name": "Epson TM-T88VI",
        },
        format="json",
    )
    assert res_print.status_code == status.HTTP_201_CREATED
    data = res_print.data
    assert data["invoice_number"] == env["sale_a"].order_number
    assert data["job_type"] == PosPrintJobType.ORIGINAL_RECEIPT
    assert data["reprint_sequence"] == 0
    assert data["printer_status"] == PosPrinterStatus.READY
    assert data["watermark_text"] == ""

    # Verify audit log in DB
    log = PosReceiptPrintLog.objects.get(print_id=data["print_id"])
    assert log.tenant == env["tenant_a"]
    assert log.sale == env["sale_a"]
    assert log.reprint_count == 0
    assert log.is_duplicate is False


@pytest.mark.django_db
def test_p232_scenario2_reprint_requires_reason_and_injects_watermark(receipt_print_env):
    """
    Scenario 2: Duplicate reprint business rule:
    - First print is original.
    - Second print without reprint_reason fails with 400 ValidationError.
    - Second print with valid reason succeeds, increments reprint_sequence to 1,
      and injects duplicate watermark: '*** DUPLICATE RECEIPT / পুনর্মুদ্রণ কপি #1 ***'.
    """
    env = receipt_print_env
    client = APIClient()
    client.force_authenticate(user=env["cashier_a"])

    # 1. First print (Original)
    res_orig = client.post(
        f"/api/v2/pos/sales/{env['sale_a'].sale_id}/print-receipt/",
        {"print_format": PosReceiptPrintFormat.THERMAL_80MM, "printer_status": PosPrinterStatus.READY},
        format="json",
    )
    assert res_orig.status_code == status.HTTP_201_CREATED

    # 2. Duplicate reprint WITHOUT reason -> Rejected
    res_no_reason = client.post(
        f"/api/v2/pos/sales/{env['sale_a'].sale_id}/print-receipt/",
        {"print_format": PosReceiptPrintFormat.THERMAL_80MM, "printer_status": PosPrinterStatus.READY},
        format="json",
    )
    assert res_no_reason.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "reprint_reason" in res_no_reason.data or "error_code" in res_no_reason.data

    # 3. Duplicate reprint WITH reason -> Accepted with watermark
    res_with_reason = client.post(
        f"/api/v2/pos/sales/{env['sale_a'].sale_id}/print-receipt/",
        {
            "print_format": PosReceiptPrintFormat.THERMAL_80MM,
            "printer_status": PosPrinterStatus.READY,
            "reprint_reason": "Customer lost the original invoice slip",
        },
        format="json",
    )
    assert res_with_reason.status_code == status.HTTP_201_CREATED
    data = res_with_reason.data
    assert data["job_type"] == PosPrintJobType.REPRINT_DUPLICATE
    assert data["reprint_sequence"] == 1
    assert "DUPLICATE RECEIPT" in data["watermark_text"]
    assert "পুনর্মুদ্রণ কপি #1" in data["watermark_text"]
    assert "DUPLICATE RECEIPT" in data["raw_text"]
    assert data["reprint_reason"] == "Customer lost the original invoice slip"


@pytest.mark.django_db
def test_p232_scenario3_offline_unsynced_receipt_watermark(receipt_print_env):
    """
    Scenario 3: Offline sale pending synchronization:
    - Receipt must display '*** PENDING CLOUD CONFIRMATION / ক্লাউড কনফার্মেশন অপেক্ষমান ***'.
    """
    env = receipt_print_env
    sale = env["sale_a"]
    sale.sync_status = PosSyncState.LOCAL_PENDING_SYNC
    sale.lifecycle_state = PosSaleLifecycleState.OFFLINE_PENDING_SYNC
    sale.save(update_fields=["sync_status", "lifecycle_state"])

    client = APIClient()
    client.force_authenticate(user=env["cashier_a"])

    res = client.post(
        f"/api/v2/pos/sales/{sale.sale_id}/print-receipt/",
        {"print_format": PosReceiptPrintFormat.THERMAL_58MM, "printer_status": PosPrinterStatus.READY},
        format="json",
    )
    assert res.status_code == status.HTTP_201_CREATED
    assert "PENDING CLOUD CONFIRMATION" in res.data["watermark_text"]
    assert "ক্লাউড কনফার্মেশন অপেক্ষমান" in res.data["watermark_text"]
    assert "PENDING CLOUD CONFIRMATION" in res.data["raw_text"]


@pytest.mark.django_db
def test_p232_scenario4_cross_tenant_isolation(receipt_print_env):
    """
    Scenario 4: Cross-tenant isolation gate:
    - Cashier B (Tenant B) cannot preview, print, or view print logs of Tenant A's sale.
    """
    env = receipt_print_env
    client = APIClient()
    client.force_authenticate(user=env["cashier_b"])

    # Attempt to preview Tenant A sale
    res_prev = client.get(f"/api/v2/pos/sales/{env['sale_a'].sale_id}/receipt-preview/")
    assert res_prev.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN)

    # Attempt to print Tenant A sale
    res_print = client.post(
        f"/api/v2/pos/sales/{env['sale_a'].sale_id}/print-receipt/",
        {"print_format": PosReceiptPrintFormat.THERMAL_80MM},
        format="json",
    )
    assert res_print.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN)
