"""
backend/tests/test_p192_supplier_credit_notes.py
Architecture Decision: ADR-0192 (P192).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration tests for P192 Supplier Credit Notes & Audited Reversals:
1. Create draft credit note with SKU items & origin reference -> DRAFT status, line amounts calculated.
2. Post credit note -> POSTED status, issues SupplierLedgerEntry debit note, decreasing AP balance.
3. Non-destructive reversal -> REVERSED status, issues SupplierLedgerEntry reversal credit, restoring AP balance.
4. Duplicate vendor credit note reference guard -> HTTP 400 ERR_DUPLICATE_CREDIT_NOTE_REF.
5. Mandatory reversal reason -> HTTP 400 ERR_REVERSAL_REASON_REQUIRED.
6. Cannot reverse unposted draft -> HTTP 400 ERR_CANNOT_REVERSE_UNPOSTED.
7. Cannot cancel posted credit note -> HTTP 400 ERR_CANNOT_CANCEL_POSTED_CREDIT_NOTE (must use reversal).
8. Cancel draft credit note -> CANCELLED status.
9. List and filter credit notes by status and origin_type.
10. RBAC and cross-tenant isolation -> Cashier blocked (403), foreign tenant blocked (404).
"""

from __future__ import annotations

import datetime
import random
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    CreditNoteOriginType,
    MedicineProduct,
    Supplier,
    SupplierCreditNote,
    SupplierCreditNoteItem,
    SupplierCreditNoteReason,
    SupplierCreditNoteStatus,
    SupplierLedgerEntry,
    SupplierPostingType,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestSupplierCreditNoteIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p192-{self.suffix}",
            name="Square Health Care",
            subdomain=f"square-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-sq-{self.suffix}",
            tenant=self.tenant,
            name="Main Dispensary",
        )

        # 2. Foreign Tenant & Branch
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"ten-foreign-{self.suffix}",
            name="Popular Pharma Ltd",
            subdomain=f"popular-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.foreign_branch = Branch.objects.create(
            branch_id=f"br-pop-{self.suffix}",
            tenant=self.foreign_tenant,
            name="Popular Central",
        )

        # 3. Users
        self.manager = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@square.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Procurement Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Front Desk Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )

        self.foreign_manager = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@popular.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.foreign_tenant.tenant_id,
            branch_id=self.foreign_branch.branch_id,
        )

        # 4. Supplier
        self.supplier = Supplier.objects.create(
            supplier_id=f"sup-sq-{self.suffix}",
            tenant=self.tenant,
            name="Square Pharmaceuticals Ltd",
            contact_person="M. Rahman",
            phone="+8801700000000",
            email="supply@squarepharma.com.bd",
            is_active=True,
        )

        # 5. Product
        self.product = MedicineProduct.objects.create(
            product_id=f"med-c192-{self.suffix}",
            brand_name="Seclo 20mg Capsule",
            generic_name="Omeprazole",
            strength="20mg",
            dosage_form="Capsule",
            manufacturer="Square Pharmaceuticals PLC",
            cost_unit=Decimal("4.5000"),
            mrp_unit=Decimal("6.0000"),
        )

    def test_01_create_draft_credit_note_with_items(self):
        """Creates a draft supplier credit note with calculated line item subtotals and taxes."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "branch_id": self.branch.branch_id,
            "supplier_credit_note_ref": f"VCN-{self.suffix.upper()}-001",
            "reason": "PRICE_DIFFERENCE",
            "origin_type": "INVOICE",
            "origin_reference": f"INV-{self.suffix.upper()}-889",
            "items": [
                {
                    "product_id": self.product.product_id,
                    "product_name": self.product.brand_name,
                    "quantity": 100,
                    "unit_credit_rate": "0.5000",  # ৳0.50 rate discount per cap = ৳50.00
                    "tax_percentage": "5.00",      # 5% tax = ৳2.50
                    "description": "Rate difference credit agreed for promotional order",
                }
            ],
        }

        resp = self.client.post("/api/v2/procurement/supplier-credit-notes/", data=payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()

        assert data["status"] == "DRAFT"
        assert data["supplier_credit_note_ref"] == f"VCN-{self.suffix.upper()}-001"
        assert Decimal(data["subtotal_amount"]) == Decimal("50.0000")
        assert Decimal(data["tax_amount"]) == Decimal("2.5000")
        assert Decimal(data["total_credit_amount"]) == Decimal("52.5000")
        assert len(data["items"]) == 1
        assert Decimal(data["items"][0]["line_total"]) == Decimal("52.5000")

    def test_02_post_credit_note_reduces_ap_balance(self):
        """Posting a supplier credit note reduces Accounts Payable balance via a SupplierLedgerEntry debit."""
        # Seed an existing AP balance of ৳25,000
        SupplierLedgerEntry.objects.create(
            posting_id=f"sup-init-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch,
            supplier=self.supplier,
            posting_type=SupplierPostingType.GRN_INVOICE,
            reference_number="INV-INIT-001",
            debit_amount=Decimal("0.0000"),
            credit_amount=Decimal("25000.0000"),
            balance_after=Decimal("25000.0000"),
            currency="BDT",
        )

        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "supplier_credit_note_ref": f"VCN-POST-{self.suffix.upper()}",
            "total_credit_amount": "5000.0000",
            "reason": "VOLUME_REBATE",
        }
        create_resp = self.client.post("/api/v2/procurement/supplier-credit-notes/", data=payload, format="json")
        assert create_resp.status_code == status.HTTP_201_CREATED
        note_id = create_resp.json()["note_id"]

        # Post credit note
        post_resp = self.client.post(f"/api/v2/procurement/supplier-credit-notes/{note_id}/post-credit-note/")
        assert post_resp.status_code == status.HTTP_200_OK
        data = post_resp.json()
        assert data["status"] == "POSTED"
        assert data["posted_at"] is not None

        # Verify AP ledger entry
        ledger = SupplierLedgerEntry.objects.filter(
            tenant=self.tenant,
            supplier=self.supplier,
            posting_type=SupplierPostingType.SUPPLIER_CREDIT_NOTE,
        ).first()

        assert ledger is not None
        assert ledger.debit_amount == Decimal("5000.0000")
        # 25,000 - 5,000 = 20,000
        assert ledger.balance_after == Decimal("20000.0000")

    def test_03_non_destructive_reversal_restores_ap_balance(self):
        """Audited reversal transitions status to REVERSED and issues reversing ledger credit to restore AP balance."""
        # Seed initial AP balance of ৳10,000
        SupplierLedgerEntry.objects.create(
            posting_id=f"sup-init-rev-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch,
            supplier=self.supplier,
            posting_type=SupplierPostingType.GRN_INVOICE,
            reference_number="INV-INIT-REV",
            debit_amount=Decimal("0.0000"),
            credit_amount=Decimal("10000.0000"),
            balance_after=Decimal("10000.0000"),
            currency="BDT",
        )

        self.client.force_authenticate(user=self.manager)
        # Create and auto-post credit note for ৳3,000
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "supplier_credit_note_ref": f"VCN-REV-{self.suffix.upper()}",
            "total_credit_amount": "3000.0000",
            "reason": "PRICE_DIFFERENCE",
        }
        create_resp = self.client.post(
            "/api/v2/procurement/supplier-credit-notes/?auto_post=true", data=payload, format="json"
        )
        assert create_resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED)
        note_id = create_resp.json()["note_id"]

        # AP balance after posting: 10,000 - 3,000 = 7,000
        last_entry = SupplierLedgerEntry.objects.filter(tenant=self.tenant, supplier=self.supplier).order_by("-posted_at").first()
        assert last_entry.balance_after == Decimal("7000.0000")

        # Now perform audited reversal
        rev_resp = self.client.post(
            f"/api/v2/procurement/supplier-credit-notes/{note_id}/reverse/",
            data={"reason": "Vendor mistakenly credited wrong customer account number"},
            format="json",
        )
        assert rev_resp.status_code == status.HTTP_200_OK
        rev_data = rev_resp.json()
        assert rev_data["status"] == "REVERSED"
        assert rev_data["reversal_reason"] == "Vendor mistakenly credited wrong customer account number"
        assert rev_data["reversal_posted_at"] is not None

        # Verify reversing ledger entry restores balance: 7,000 + 3,000 = 10,000
        rev_entry = SupplierLedgerEntry.objects.filter(
            tenant=self.tenant,
            supplier=self.supplier,
            posting_type=SupplierPostingType.CREDIT_NOTE_REVERSAL,
        ).first()

        assert rev_entry is not None
        assert rev_entry.credit_amount == Decimal("3000.0000")
        assert rev_entry.balance_after == Decimal("10000.0000")

    def test_04_duplicate_vendor_credit_ref_guard(self):
        """Submitting the same vendor credit note reference for the same supplier raises ERR_DUPLICATE_CREDIT_NOTE_REF."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "supplier_credit_note_ref": f"VCN-DUP-{self.suffix.upper()}",
            "total_credit_amount": "1000.0000",
        }
        resp1 = self.client.post("/api/v2/procurement/supplier-credit-notes/", data=payload, format="json")
        assert resp1.status_code == status.HTTP_201_CREATED

        # Duplicate attempt
        resp2 = self.client.post("/api/v2/procurement/supplier-credit-notes/", data=payload, format="json")
        assert resp2.status_code == status.HTTP_400_BAD_REQUEST
        assert resp2.json()["error_code"] == "ERR_DUPLICATE_CREDIT_NOTE_REF"

    def test_05_reversal_requires_reason(self):
        """Reversing a credit note without a reason throws ERR_REVERSAL_REASON_REQUIRED."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "supplier_credit_note_ref": f"VCN-NOREAS-{self.suffix.upper()}",
            "total_credit_amount": "500.0000",
        }
        create_resp = self.client.post(
            "/api/v2/procurement/supplier-credit-notes/?auto_post=true", data=payload, format="json"
        )
        note_id = create_resp.json()["note_id"]

        rev_resp = self.client.post(
            f"/api/v2/procurement/supplier-credit-notes/{note_id}/reverse/",
            data={"reason": ""},
            format="json",
        )
        assert rev_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert rev_resp.json()["error_code"] == "ERR_REVERSAL_REASON_REQUIRED"

    def test_06_cannot_reverse_unposted_draft(self):
        """Attempting to reverse an unposted DRAFT credit note throws ERR_CANNOT_REVERSE_UNPOSTED."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "supplier_credit_note_ref": f"VCN-DRAFTREV-{self.suffix.upper()}",
            "total_credit_amount": "500.0000",
        }
        create_resp = self.client.post("/api/v2/procurement/supplier-credit-notes/", data=payload, format="json")
        note_id = create_resp.json()["note_id"]

        rev_resp = self.client.post(
            f"/api/v2/procurement/supplier-credit-notes/{note_id}/reverse/",
            data={"reason": "Should fail on draft"},
            format="json",
        )
        assert rev_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert rev_resp.json()["error_code"] == "ERR_CANNOT_REVERSE_UNPOSTED"

    def test_07_cannot_cancel_posted_credit_note(self):
        """Attempting to cancel an already posted credit note throws ERR_CANNOT_CANCEL_POSTED_CREDIT_NOTE."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "supplier_credit_note_ref": f"VCN-NOCAN-{self.suffix.upper()}",
            "total_credit_amount": "1200.0000",
        }
        create_resp = self.client.post(
            "/api/v2/procurement/supplier-credit-notes/?auto_post=true", data=payload, format="json"
        )
        note_id = create_resp.json()["note_id"]

        cancel_resp = self.client.post(
            f"/api/v2/procurement/supplier-credit-notes/{note_id}/cancel/",
            data={"reason": "Direct cancel should be blocked"},
            format="json",
        )
        assert cancel_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert cancel_resp.json()["error_code"] == "ERR_CANNOT_CANCEL_POSTED_CREDIT_NOTE"

    def test_08_cancel_draft_credit_note(self):
        """Cancels an unposted draft credit note."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "supplier_credit_note_ref": f"VCN-CANDRAFT-{self.suffix.upper()}",
            "total_credit_amount": "700.0000",
        }
        create_resp = self.client.post("/api/v2/procurement/supplier-credit-notes/", data=payload, format="json")
        note_id = create_resp.json()["note_id"]

        cancel_resp = self.client.post(
            f"/api/v2/procurement/supplier-credit-notes/{note_id}/cancel/",
            data={"reason": "Entered under wrong supplier"},
            format="json",
        )
        assert cancel_resp.status_code == status.HTTP_200_OK
        assert cancel_resp.json()["status"] == "CANCELLED"

    def test_09_list_and_filter_credit_notes(self):
        """Lists supplier credit notes with filtering by status and supplier."""
        self.client.force_authenticate(user=self.manager)

        # Note 1: Draft
        self.client.post(
            "/api/v2/procurement/supplier-credit-notes/",
            data={"supplier_id": self.supplier.supplier_id, "supplier_credit_note_ref": f"VCN-L1-{self.suffix}", "total_credit_amount": "100.0000"},
            format="json",
        )

        # Note 2: Posted
        self.client.post(
            "/api/v2/procurement/supplier-credit-notes/?auto_post=true",
            data={"supplier_id": self.supplier.supplier_id, "supplier_credit_note_ref": f"VCN-L2-{self.suffix}", "total_credit_amount": "200.0000"},
            format="json",
        )

        # List all
        resp = self.client.get("/api/v2/procurement/supplier-credit-notes/")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["count"] == 2

        # Filter by status=POSTED
        resp_posted = self.client.get("/api/v2/procurement/supplier-credit-notes/?status=POSTED")
        assert resp_posted.status_code == status.HTTP_200_OK
        assert resp_posted.json()["count"] == 1
        assert resp_posted.json()["results"][0]["status"] == "POSTED"

    def test_10_rbac_and_cross_tenant_isolation(self):
        """Cashier role is blocked from managing credit notes, and foreign tenants cannot access records."""
        # 1. Cashier attempts to create credit note -> 403 Forbidden
        self.client.force_authenticate(user=self.cashier)
        resp_csh = self.client.post(
            "/api/v2/procurement/supplier-credit-notes/",
            data={"supplier_id": self.supplier.supplier_id, "supplier_credit_note_ref": f"VCN-CSH-{self.suffix}", "total_credit_amount": "100.0000"},
            format="json",
        )
        assert resp_csh.status_code == status.HTTP_403_FORBIDDEN
        assert resp_csh.json()["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 2. Manager creates credit note
        self.client.force_authenticate(user=self.manager)
        create_resp = self.client.post(
            "/api/v2/procurement/supplier-credit-notes/",
            data={"supplier_id": self.supplier.supplier_id, "supplier_credit_note_ref": f"VCN-SEC-{self.suffix}", "total_credit_amount": "100.0000"},
            format="json",
        )
        note_id = create_resp.json()["note_id"]

        # 3. Foreign tenant attempts to access credit note -> 404 Not Found
        self.client.force_authenticate(user=self.foreign_manager)
        resp_foreign = self.client.get(f"/api/v2/procurement/supplier-credit-notes/{note_id}/")
        assert resp_foreign.status_code == status.HTTP_404_NOT_FOUND
