import os
import unittest
from typing import Any


class Role:
    PLATFORM_SUPER_ADMIN = "SUPER_ADMIN"
    PLATFORM_SUPPORT = "SUPPORT"
    TENANT_OWNER = "TENANT_OWNER"
    BRANCH_MANAGER = "BRANCH_MANAGER"
    REGISTERED_PHARMACIST = "PHARMACIST"
    CASHIER = "CASHIER"
    PURCHASER = "PURCHASER"
    INVENTORY_STAFF = "INVENTORY_STAFF"
    ACCOUNTANT = "ACCOUNTANT"
    AUDITOR = "AUDITOR"


class SensitiveAction:
    DISPENSE_ANTIBIOTIC_RX = "DISPENSE_ANTIBIOTIC_RX"
    OVERRIDE_EXPIRED_STOCK = "OVERRIDE_EXPIRED_STOCK"
    VOID_FINALIZED_SALE = "VOID_FINALIZED_SALE"
    APPLY_EXCESSIVE_DISCOUNT = "APPLY_EXCESSIVE_DISCOUNT"
    APPROVE_CASH_SHIFT_VARIANCE = "APPROVE_CASH_SHIFT_VARIANCE"
    APPROVE_PURCHASE_ORDER = "APPROVE_PURCHASE_ORDER"
    POST_GOODS_RECEIPT = "POST_GOODS_RECEIPT"
    STOCK_ADJUSTMENT = "STOCK_ADJUSTMENT"
    POST_MANUAL_JOURNAL = "POST_MANUAL_JOURNAL"
    HARD_DELETE_POSTED_RECORD = "HARD_DELETE_POSTED_RECORD"
    VIEW_PRESCRIPTION_ATTACHMENT = "VIEW_PRESCRIPTION_ATTACHMENT"
    ENROLL_REVOKE_DEVICE = "ENROLL_REVOKE_DEVICE"
    MODIFY_SUBSCRIPTION_QUOTAS = "MODIFY_SUBSCRIPTION_QUOTAS"


class PermissionDeniedError(Exception):
    pass


class AbsoluteBlockError(Exception):
    pass


class MakerCheckerViolationError(Exception):
    pass


class PermissionEvaluator:
    """Reference authorization engine implementing ADR-0005 permission matrix rules."""

    ROLE_PERMISSIONS: dict[str, set[str]] = {
        Role.PLATFORM_SUPER_ADMIN: {
            SensitiveAction.MODIFY_SUBSCRIPTION_QUOTAS,
        },
        Role.PLATFORM_SUPPORT: set(),
        Role.TENANT_OWNER: {
            SensitiveAction.VOID_FINALIZED_SALE,
            SensitiveAction.APPLY_EXCESSIVE_DISCOUNT,
            SensitiveAction.APPROVE_CASH_SHIFT_VARIANCE,
            SensitiveAction.APPROVE_PURCHASE_ORDER,
            SensitiveAction.POST_GOODS_RECEIPT,
            SensitiveAction.STOCK_ADJUSTMENT,
            SensitiveAction.POST_MANUAL_JOURNAL,
            SensitiveAction.VIEW_PRESCRIPTION_ATTACHMENT,
            SensitiveAction.ENROLL_REVOKE_DEVICE,
            SensitiveAction.MODIFY_SUBSCRIPTION_QUOTAS,
        },
        Role.BRANCH_MANAGER: {
            SensitiveAction.VOID_FINALIZED_SALE,
            SensitiveAction.APPLY_EXCESSIVE_DISCOUNT,
            SensitiveAction.APPROVE_CASH_SHIFT_VARIANCE,
            SensitiveAction.APPROVE_PURCHASE_ORDER,
            SensitiveAction.POST_GOODS_RECEIPT,
            SensitiveAction.STOCK_ADJUSTMENT,
            SensitiveAction.VIEW_PRESCRIPTION_ATTACHMENT,
            SensitiveAction.ENROLL_REVOKE_DEVICE,
        },
        Role.REGISTERED_PHARMACIST: {
            SensitiveAction.DISPENSE_ANTIBIOTIC_RX,
            SensitiveAction.VIEW_PRESCRIPTION_ATTACHMENT,
            SensitiveAction.POST_GOODS_RECEIPT,
        },
        Role.CASHIER: set(),  # Cashiers have checkout permissions, but zero sensitive override permissions
        Role.PURCHASER: set(),  # Purchasers draft POs, cannot approve them
        Role.INVENTORY_STAFF: {
            SensitiveAction.POST_GOODS_RECEIPT,
        },
        Role.ACCOUNTANT: {
            SensitiveAction.POST_MANUAL_JOURNAL,
            SensitiveAction.APPROVE_CASH_SHIFT_VARIANCE,
        },
        Role.AUDITOR: {
            SensitiveAction.VIEW_PRESCRIPTION_ATTACHMENT,  # Masked read-only
        },
    }

    @classmethod
    def evaluate(cls, user_role: str, action: str, context: dict[str, Any] = None) -> bool:
        context = context or {}

        # 1. Absolute System Blocks (Zero Override across ALL users)
        if action == SensitiveAction.OVERRIDE_EXPIRED_STOCK:
            raise AbsoluteBlockError("Absolute Block: Expired medicines cannot be sold or overridden by any persona.")
        if action == SensitiveAction.HARD_DELETE_POSTED_RECORD:
            raise AbsoluteBlockError("Absolute Block: Posted financial/inventory records cannot be hard deleted.")

        # 2. Maker-Checker Segregation
        if action == SensitiveAction.APPROVE_PURCHASE_ORDER:
            creator_id = context.get("po_creator_id")
            approver_id = context.get("user_id")
            if creator_id and approver_id and creator_id == approver_id:
                raise MakerCheckerViolationError(
                    "Maker-Checker violation: Creator cannot approve their own Purchase Order."
                )

        # 3. RBAC Permission Check
        allowed_actions = cls.ROLE_PERMISSIONS.get(user_role, set())
        if action not in allowed_actions:
            raise PermissionDeniedError(f"Role '{user_role}' is denied action '{action}'.")

        return True


class TestP005UserPersonasAndPermissionMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adr_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "docs",
                "architecture",
                "decisions",
                "ADR-0005-user-personas-and-permission-matrix.md",
            )
        )
        assert os.path.exists(cls.adr_path), f"ADR-0005 not found at: {cls.adr_path}"
        with open(cls.adr_path, encoding="utf-8") as f:
            cls.content = f.read()

    def test_adr0005_file_exists_and_substantial(self):
        """ADR-0005 must exist and contain substantial specification."""
        self.assertGreater(len(self.content), 5000, "ADR-0005 is too brief.")

    def test_all_ten_personas_documented(self):
        """All ten personas must be explicitly catalogued in Section 2."""
        expected_personas = [
            "Platform Super Admin",
            "Platform Support Staff",
            "Tenant Owner",
            "Branch Manager",
            "Registered Pharmacist",
            "Cashier",
            "Purchaser",
            "Inventory Staff",
            "Accountant",
            "Auditor",
        ]
        for persona in expected_personas:
            self.assertIn(persona, self.content, f"Persona '{persona}' missing in ADR-0005.")

    def test_sensitive_action_matrix_present(self):
        """Sensitive Action Permission Matrix must cover critical operational domains."""
        required_actions = [
            "Dispense Antibiotic / Rx Medicine",
            "Override / Dispense Expired Stock",
            "Void / Reverse Finalized Sale",
            "Apply Line-Item Discount",
            "Close Cash Shift with Discrepancy",
            "Approve Purchase Order (PO)",
            "Post Goods Receipt (Commit Stock)",
            "Post Manual Journal Entry",
            "View Patient Prescription Attachments",
        ]
        for act in required_actions:
            self.assertIn(act, self.content, f"Action '{act}' missing in Permission Matrix.")

    def test_absolute_blocks_enforced_for_all_roles(self):
        """Absolute blocks (Expired stock & Hard delete) must fail for ALL roles including Super Admin."""
        all_roles = [
            Role.PLATFORM_SUPER_ADMIN,
            Role.TENANT_OWNER,
            Role.BRANCH_MANAGER,
            Role.REGISTERED_PHARMACIST,
            Role.CASHIER,
        ]
        for r in all_roles:
            with self.assertRaises(AbsoluteBlockError):
                PermissionEvaluator.evaluate(r, SensitiveAction.OVERRIDE_EXPIRED_STOCK)
            with self.assertRaises(AbsoluteBlockError):
                PermissionEvaluator.evaluate(r, SensitiveAction.HARD_DELETE_POSTED_RECORD)

    def test_negative_permissions_denied_strictly(self):
        """Unauthorized personas must be strictly rejected with PermissionDeniedError."""
        # 1. Cashier cannot dispense Antibiotics
        with self.assertRaises(PermissionDeniedError):
            PermissionEvaluator.evaluate(Role.CASHIER, SensitiveAction.DISPENSE_ANTIBIOTIC_RX)

        # 2. Cashier cannot void finalized sale
        with self.assertRaises(PermissionDeniedError):
            PermissionEvaluator.evaluate(Role.CASHIER, SensitiveAction.VOID_FINALIZED_SALE)

        # 3. Super Admin cannot access patient prescription attachments
        with self.assertRaises(PermissionDeniedError):
            PermissionEvaluator.evaluate(Role.PLATFORM_SUPER_ADMIN, SensitiveAction.VIEW_PRESCRIPTION_ATTACHMENT)

        # 4. Auditor cannot post manual journal
        with self.assertRaises(PermissionDeniedError):
            PermissionEvaluator.evaluate(Role.AUDITOR, SensitiveAction.POST_MANUAL_JOURNAL)

        # 5. Purchaser cannot approve POs
        with self.assertRaises(PermissionDeniedError):
            PermissionEvaluator.evaluate(Role.PURCHASER, SensitiveAction.APPROVE_PURCHASE_ORDER)

    def test_positive_permissions_authorized(self):
        """Authorized roles must be successfully allowed."""
        # 1. Pharmacist can dispense antibiotics
        self.assertTrue(
            PermissionEvaluator.evaluate(Role.REGISTERED_PHARMACIST, SensitiveAction.DISPENSE_ANTIBIOTIC_RX)
        )
        # 2. Branch manager can void finalized sale
        self.assertTrue(PermissionEvaluator.evaluate(Role.BRANCH_MANAGER, SensitiveAction.VOID_FINALIZED_SALE))
        # 3. Accountant can post manual journal
        self.assertTrue(PermissionEvaluator.evaluate(Role.ACCOUNTANT, SensitiveAction.POST_MANUAL_JOURNAL))
        # 4. Inventory staff can post goods receipt
        self.assertTrue(PermissionEvaluator.evaluate(Role.INVENTORY_STAFF, SensitiveAction.POST_GOODS_RECEIPT))
        # 5. Super Admin can modify subscription quotas
        self.assertTrue(
            PermissionEvaluator.evaluate(Role.PLATFORM_SUPER_ADMIN, SensitiveAction.MODIFY_SUBSCRIPTION_QUOTAS)
        )

    def test_maker_checker_purchase_order_segregation(self):
        """Purchaser who drafted a PO cannot approve it (Maker-Checker violation)."""
        context_same_user = {"po_creator_id": "usr_99", "user_id": "usr_99"}
        with self.assertRaises(MakerCheckerViolationError):
            PermissionEvaluator.evaluate(Role.TENANT_OWNER, SensitiveAction.APPROVE_PURCHASE_ORDER, context_same_user)

        context_different_user = {"po_creator_id": "usr_99", "user_id": "usr_100"}
        self.assertTrue(
            PermissionEvaluator.evaluate(
                Role.BRANCH_MANAGER, SensitiveAction.APPROVE_PURCHASE_ORDER, context_different_user
            )
        )


if __name__ == "__main__":
    unittest.main()
