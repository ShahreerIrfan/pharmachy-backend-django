"""
backend/tests/test_p275_payment_method_registry.py
Architecture Decision: ADR-0275 (P275).
Phase 14: Customers, Suppliers, Dues & Payments.

Integration tests verifying Payment Method Registry:
1. Valid configuration of Cash, bKash, and Card payment methods with linked clearing accounts.
2. Zero Secrets Guard: Rejection of plaintext API keys, passwords, PINs, and credentials.
3. Fine-grained RBAC matrix: Cashier/Pharmacist denied configuration, Owner/Manager authorized.
4. Cashier checkout access: Cashier can view active payment methods for their branch.
5. Cross-tenant isolation strictly enforced for branches and clearing accounts.
6. Tender validation: limits, reference pattern matching, and customer surcharge calculations.
7. Offline payment enforcement: online-only payment methods blocked during offline disconnects.
8. Suspended tenant account write blocking.
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    FinancialAccount,
    FinancialAccountType,
    PaymentMethod,
    PaymentMethodConfig,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestPaymentMethodRegistryIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branch
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-a-{self.suffix}",
            name=f"Square Pharma {self.suffix}",
            subdomain=f"sq-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
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

        # 4. Clearing Financial Accounts
        self.cash_account = FinancialAccount.objects.create(
            account_id=f"finacc-csh-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            account_type=FinancialAccountType.CASH_REGISTER,
            account_name="Till 1 Cash Drawer",
            account_code=f"CR-T1-{self.suffix}",
        )

        self.bkash_account = FinancialAccount.objects.create(
            account_id=f"finacc-mfs-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            account_type=FinancialAccountType.MFS_CLEARING,
            account_name="bKash Merchant Settlement",
            account_code=f"MFS-BK-{self.suffix}",
        )

        self.foreign_account = FinancialAccount.objects.create(
            account_id=f"finacc-for-{self.suffix}",
            tenant=self.tenant_b,
            branch=self.branch_b,
            account_type=FinancialAccountType.BANK_ACCOUNT,
            account_name="Foreign Bank",
            account_code=f"BA-FOR-{self.suffix}",
        )

        # 5. Users
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@square.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.branch_manager = PharmacyUser.objects.create_user(
            email=f"bm_{self.suffix}@square.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Branch Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="POS Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"pharm_{self.suffix}@square.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Staff Pharmacist",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.suspended_user = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@square.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Suspended Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

        self.foreign_user = PharmacyUser.objects.create_user(
            email=f"for_{self.suffix}@foreign.com",
            phone_number=f"+880176{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_b.tenant_id,
        )

    def test_01_valid_payment_methods_configuration(self):
        """Branch manager configures Cash and bKash payment channels with clearing accounts and surcharges."""
        self.client.force_authenticate(user=self.branch_manager)
        url = "/api/v2/finance/payment-methods/"

        # 1. Configure Cash
        payload_cash = {
            "method_type": PaymentMethod.CASH,
            "display_name": "Cash at POS Counter",
            "display_order": 1,
            "branch_id": self.branch_a.branch_id,
            "clearing_account_id": self.cash_account.account_id,
            "allow_offline": True,
        }
        res_cash = self.client.post(url, payload_cash, format="json")
        assert res_cash.status_code == status.HTTP_201_CREATED, res_cash.data
        assert res_cash.data["method_type"] == PaymentMethod.CASH
        assert res_cash.data["allow_offline"] is True
        assert res_cash.data["clearing_account_id"] == self.cash_account.account_id

        # 2. Configure bKash with TrxID pattern and 1.5% surcharge
        payload_bkash = {
            "method_type": PaymentMethod.BKASH,
            "display_name": "bKash Merchant Pay",
            "display_order": 2,
            "branch_id": self.branch_a.branch_id,
            "clearing_account_id": self.bkash_account.account_id,
            "requires_reference": True,
            "reference_label": "TrxID",
            "reference_pattern": r"^[A-Z0-9]{8,12}$",
            "customer_surcharge_percentage": "1.50",
            "min_transaction_bdt": "10.00",
            "max_transaction_bdt": "25000.00",
            "allow_offline": False,
            "vault_secret_reference": "vault://mfs/bkash-prod-01",
        }
        res_bkash = self.client.post(url, payload_bkash, format="json")
        assert res_bkash.status_code == status.HTTP_201_CREATED, res_bkash.data
        assert res_bkash.data["requires_reference"] is True
        assert Decimal(res_bkash.data["customer_surcharge_percentage"]) == Decimal("1.50")
        assert res_bkash.data["vault_secret_reference"] == "vault://mfs/bkash-prod-01"

        # Verify Audit Log
        assert AuditLogEntry.objects.filter(
            action="PAYMENT_METHOD_CONFIGURED",
            resource_id=res_bkash.data["config_id"],
        ).exists()

    def test_02_zero_secrets_guard_blocks_plain_credentials(self):
        """Zero Secrets Guard blocks plain sensitive payment credentials, API secrets, and PINs."""
        self.client.force_authenticate(user=self.branch_manager)
        url = "/api/v2/finance/payment-methods/"

        # 1. Payload containing raw api_key
        payload_bad1 = {
            "method_type": PaymentMethod.CARD,
            "display_name": "Card Terminal",
            "api_key": "raw_insecure_api_key_123456",
        }
        res1 = self.client.post(url, payload_bad1, format="json")
        assert res1.status_code == status.HTTP_400_BAD_REQUEST
        assert res1.data["error_code"] == "ERR_SENSITIVE_SECRET_EXPOSURE_BLOCKED"

        # 2. Payload containing mpin / password
        payload_bad2 = {
            "method_type": PaymentMethod.NAGAD,
            "display_name": "Nagad Pay",
            "mpin": "12345",
        }
        res2 = self.client.post(url, payload_bad2, format="json")
        assert res2.status_code == status.HTTP_400_BAD_REQUEST
        assert res2.data["error_code"] == "ERR_SENSITIVE_SECRET_EXPOSURE_BLOCKED"

    def test_03_role_authorization_cashier_and_pharmacist_denied_configuration(self):
        """Cashier and Pharmacist cannot configure, update, or toggle payment methods."""
        url = "/api/v2/finance/payment-methods/"
        payload = {"method_type": PaymentMethod.NAGAD, "display_name": "Nagad"}

        # 1. Cashier creation attempt -> 403 Forbidden
        self.client.force_authenticate(user=self.cashier)
        res_cashier = self.client.post(url, payload, format="json")
        assert res_cashier.status_code == status.HTTP_403_FORBIDDEN
        assert res_cashier.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 2. Pharmacist toggle attempt -> 403 Forbidden
        self.client.force_authenticate(user=self.owner)
        res_cfg = self.client.post(url, payload, format="json")
        cfg_id = res_cfg.data["config_id"]

        self.client.force_authenticate(user=self.pharmacist)
        res_toggle = self.client.post(f"/api/v2/finance/payment-methods/{cfg_id}/toggle/", {"is_enabled": False}, format="json")
        assert res_toggle.status_code == status.HTTP_403_FORBIDDEN
        assert res_toggle.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

    def test_04_cashier_can_list_enabled_methods_for_checkout(self):
        """Cashiers can query active payment methods for checkout at their assigned branch."""
        self.client.force_authenticate(user=self.owner)
        url = "/api/v2/finance/payment-methods/"

        # 1. Active Cash
        self.client.post(url, {
            "method_type": PaymentMethod.CASH,
            "display_name": "Cash",
            "branch_id": self.branch_a.branch_id,
            "is_enabled": True,
        }, format="json")

        # 2. Disabled Card
        self.client.post(url, {
            "method_type": PaymentMethod.CARD,
            "display_name": "Card Disabled",
            "branch_id": self.branch_a.branch_id,
            "is_enabled": False,
        }, format="json")

        # Cashier lists enabled methods
        self.client.force_authenticate(user=self.cashier)
        res = self.client.get(f"{url}?only_enabled=true")
        assert res.status_code == status.HTTP_200_OK
        methods = res.data["payment_methods"]

        assert len(methods) == 1
        assert methods[0]["method_type"] == PaymentMethod.CASH
        assert methods[0]["is_enabled"] is True

    def test_05_cross_tenant_isolation(self):
        """Cross-tenant payment method access and linking of foreign clearing accounts are forbidden."""
        # 1. Tenant B configures Nagad
        self.client.force_authenticate(user=self.foreign_user)
        res_b = self.client.post("/api/v2/finance/payment-methods/", {
            "method_type": PaymentMethod.NAGAD,
            "branch_id": self.branch_b.branch_id,
        }, format="json")
        cfg_b_id = res_b.data["config_id"]

        # 2. Tenant A attempts to access Tenant B config -> 404 / 403
        self.client.force_authenticate(user=self.branch_manager)
        res_get = self.client.get(f"/api/v2/finance/payment-methods/{cfg_b_id}/")
        assert res_get.status_code in [status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN]

        # 3. Tenant A attempts to link Tenant B's clearing account -> 403 Forbidden
        res_cross_acc = self.client.post("/api/v2/finance/payment-methods/", {
            "method_type": PaymentMethod.CARD,
            "branch_id": self.branch_a.branch_id,
            "clearing_account_id": self.foreign_account.account_id,
        }, format="json")
        assert res_cross_acc.status_code == status.HTTP_403_FORBIDDEN
        assert res_cross_acc.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"

    def test_06_tender_validation_limits_reference_pattern_and_surcharge(self):
        """Validates tender limits, regex format for transaction reference, and surcharge calculation."""
        self.client.force_authenticate(user=self.branch_manager)
        url = "/api/v2/finance/payment-methods/"

        res_cfg = self.client.post(url, {
            "method_type": PaymentMethod.BKASH,
            "display_name": "bKash Pay",
            "branch_id": self.branch_a.branch_id,
            "requires_reference": True,
            "reference_label": "TrxID",
            "reference_pattern": r"^[A-Z0-9]{10}$",
            "customer_surcharge_percentage": "2.00",
            "min_transaction_bdt": "50.00",
            "max_transaction_bdt": "10000.00",
            "allow_offline": False,
        }, format="json")
        cfg_id = res_cfg.data["config_id"]

        val_url = f"/api/v2/finance/payment-methods/{cfg_id}/validate-tender/"

        # 1. Valid tender (1,000 BDT + 2% surcharge = 1,020 BDT)
        res_valid = self.client.post(val_url, {
            "amount_bdt": "1000.00",
            "reference": "BK99887766",
            "is_offline": False,
        }, format="json")
        assert res_valid.status_code == status.HTTP_200_OK
        assert res_valid.data["is_valid"] is True
        assert Decimal(res_valid.data["surcharge_bdt"]) == Decimal("20.00")
        assert Decimal(res_valid.data["total_payable_bdt"]) == Decimal("1020.00")

        # 2. Reference format mismatch (only 5 chars instead of 10)
        res_bad_ref = self.client.post(val_url, {
            "amount_bdt": "1000.00",
            "reference": "SHORT",
        }, format="json")
        assert res_bad_ref.status_code == status.HTTP_400_BAD_REQUEST
        assert res_bad_ref.data["error_code"] == "ERR_INVALID_REFERENCE_FORMAT"

        # 3. Amount out of bounds (below min 50.00)
        res_bad_amt = self.client.post(val_url, {
            "amount_bdt": "25.00",
            "reference": "BK99887766",
        }, format="json")
        assert res_bad_amt.status_code == status.HTTP_400_BAD_REQUEST
        assert res_bad_amt.data["error_code"] == "ERR_TRANSACTION_AMOUNT_OUT_OF_BOUNDS"

    def test_07_offline_payment_rejection_for_online_only_methods(self):
        """Tender in offline mode is rejected for payment methods that require online connectivity."""
        self.client.force_authenticate(user=self.branch_manager)
        url = "/api/v2/finance/payment-methods/"

        res_cfg = self.client.post(url, {
            "method_type": PaymentMethod.CARD,
            "display_name": "Online Card Terminal",
            "branch_id": self.branch_a.branch_id,
            "allow_offline": False,
        }, format="json")
        cfg_id = res_cfg.data["config_id"]

        val_url = f"/api/v2/finance/payment-methods/{cfg_id}/validate-tender/"
        res_offline = self.client.post(val_url, {
            "amount_bdt": "500.00",
            "is_offline": True,
        }, format="json")
        assert res_offline.status_code == status.HTTP_400_BAD_REQUEST
        assert res_offline.data["error_code"] == "ERR_OFFLINE_PAYMENT_NOT_ALLOWED"

    def test_08_suspended_tenant_blocked(self):
        """Suspended tenant is barred from configuring payment methods."""
        self.client.force_authenticate(user=self.suspended_user)
        url = "/api/v2/finance/payment-methods/"
        res = self.client.post(url, {"method_type": PaymentMethod.CASH}, format="json")
        data = res.data if hasattr(res, "data") else res.json()
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert data.get("error_code") in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]
