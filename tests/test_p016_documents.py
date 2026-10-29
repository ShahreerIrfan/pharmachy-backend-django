"""
Unit and Contract Tests for ADR-0016: Operational Documents Inventory & Template Specifications
Verifies:
1. Catalog registration of all 10 operational document templates with bilingual labels.
2. Valid generation for all 10 operational documents with strict Decimal precision.
3. Offline thermal receipt watermark banner generation (PENDING_SYNC vs CONFIRMED).
4. Mandatory field validation and rejection (e.g., missing doctor BMDC or patient address in antibiotic register).
5. Floating-point rejection for monetary and temperature values (strictly Decimal).
6. Role-based authorization per document type (e.g., Cashier blocked from Antibiotic Register).
7. Cross-tenant isolation enforcement.
"""

import datetime
import unittest
from decimal import Decimal

from apps.core.documents import (
    DocumentType,
    OperationalDocumentCatalog,
    PrintFormat,
)
from apps.core.errors import FieldValidationError


class TestOperationalDocumentsInventory(unittest.TestCase):
    def setUp(self):
        self.catalog = OperationalDocumentCatalog()
        self.tenant_id = "tenant-apollo-dhaka"

    def test_catalog_registers_all_10_documents(self):
        templates = self.catalog.list_templates()
        self.assertEqual(len(templates), 10)
        registered_types = {t.doc_type for t in templates}
        for dt in DocumentType:
            self.assertIn(dt, registered_types)

        for template in templates:
            self.assertTrue(template.name_en)
            self.assertTrue(template.name_bn)
            self.assertIsInstance(template.print_format, PrintFormat)
            self.assertTrue(len(template.fields) > 0)
            self.assertTrue(len(template.allowed_roles) > 0)
            for f_def in template.fields:
                self.assertTrue(f_def.name)
                self.assertTrue(f_def.bilingual_label_en)
                self.assertTrue(f_def.bilingual_label_bn)

    def test_valid_generation_for_all_10_documents(self):
        now = datetime.datetime.now(datetime.UTC)
        today = datetime.date.today()

        samples = {
            DocumentType.INVOICE: (
                "CASHIER",
                {
                    "invoice_number": "INV-2026-0001",
                    "tenant_id": self.tenant_id,
                    "branch_id": "branch-main",
                    "timestamp": now,
                    "cashier_name": "Rahim Ahmed",
                    "subtotal": Decimal("500.00"),
                    "discount": Decimal("50.00"),
                    "net_total": Decimal("450.00"),
                    "items": [{"item_id": "med-1", "qty": 2, "price": Decimal("225.00")}],
                },
            ),
            DocumentType.PURCHASE_ORDER: (
                "PURCHASER",
                {
                    "po_number": "PO-2026-0045",
                    "tenant_id": self.tenant_id,
                    "supplier_id": "sup-square-01",
                    "supplier_name": "Square Pharmaceuticals Ltd.",
                    "order_date": today,
                    "total_cost": Decimal("25000.00"),
                    "items": [{"medicine": "Ace 500mg", "qty": 1000}],
                },
            ),
            DocumentType.GOODS_RECEIVED_NOTE: (
                "INVENTORY_STAFF",
                {
                    "grn_number": "GRN-2026-0012",
                    "tenant_id": self.tenant_id,
                    "po_reference": "PO-2026-0045",
                    "challan_number": "CH-9921",
                    "receiving_date": today,
                    "total_received_cost": Decimal("25000.00"),
                    "received_batches": [{"batch_no": "SQ-B1", "qty": 1000}],
                },
            ),
            DocumentType.RETURN_VOUCHER: (
                "BRANCH_MANAGER",
                {
                    "return_number": "RET-2026-0003",
                    "tenant_id": self.tenant_id,
                    "original_invoice_ref": "INV-2026-0001",
                    "return_reason": "Customer adverse reaction / wrong item",
                    "refund_amount": Decimal("225.00"),
                    "returned_items": [{"item_id": "med-1", "qty": 1}],
                },
            ),
            DocumentType.TEMPERATURE_LOG: (
                "PHARMACIST",
                {
                    "log_id": "TEMP-2026-04-001",
                    "tenant_id": self.tenant_id,
                    "unit_identifier": "COLD-FRIDGE-01",
                    "recorded_temp_celsius": Decimal("4.2"),
                    "is_excursion": False,
                    "reviewer_name": "Kazi Nazmul (B.Pharm)",
                    "timestamp": now,
                },
            ),
            DocumentType.EXPENSE_VOUCHER: (
                "CASHIER",
                {
                    "voucher_number": "EXP-2026-0099",
                    "tenant_id": self.tenant_id,
                    "category": "Office Cleaning Supplies",
                    "amount": Decimal("350.00"),
                    "shift_session_id": "SHIFT-20260429-01",
                    "approved_by": "Manager S. Alam",
                },
            ),
            DocumentType.THERMAL_RECEIPT: (
                "CASHIER",
                {
                    "receipt_number": "REC-2026-10492",
                    "tenant_id": self.tenant_id,
                    "branch_id": "branch-main",
                    "pharmacy_name": "Apollo Care Pharmacy",
                    "drug_license_no": "DL-DHK-2024-9981",
                    "timestamp": now,
                    "sync_status": "CONFIRMED",
                    "total_payable": Decimal("450.00"),
                    "tender_type": "CASH",
                    "items": [{"name": "Ace Plus", "qty": 2}],
                },
            ),
            DocumentType.PRESCRIPTION_RECORD: (
                "PHARMACIST",
                {
                    "prescription_id": "RX-2026-5512",
                    "tenant_id": self.tenant_id,
                    "patient_name": "Fatema Begum",
                    "doctor_name": "Dr. Mahmud Hasan",
                    "doctor_bmdc_no": "A-54321",
                    "verification_status": "VERIFIED_VALID",
                    "pharmacist_name": "Kazi Nazmul",
                },
            ),
            DocumentType.ANTIBIOTIC_REGISTER: (
                "PHARMACIST",
                {
                    "entry_id": "ANT-2026-0081",
                    "tenant_id": self.tenant_id,
                    "patient_name": "Fatema Begum",
                    "patient_address": "House 12, Road 4, Dhanmondi, Dhaka",
                    "doctor_name": "Dr. Mahmud Hasan",
                    "doctor_bmdc_no": "A-54321",
                    "medicine_name": "Azithromycin 500mg",
                    "batch_number": "AZ-2026-01",
                    "quantity_dispensed": 6,
                    "dispensed_date": today,
                    "pharmacist_license": "PCB-REG-10928",
                },
            ),
            DocumentType.SUBSCRIPTION_INVOICE: (
                "TENANT_OWNER",
                {
                    "statement_number": "SUB-2026-04",
                    "tenant_id": self.tenant_id,
                    "plan_tier": "STANDARD",
                    "billing_period": "2026-04-01 to 2026-04-30",
                    "amount_bdt": Decimal("3500.00"),
                    "payment_status": "PAID_BKASH",
                },
            ),
        }

        for doc_type, (role, payload) in samples.items():
            validated = self.catalog.validate_document_data(
                doc_type=doc_type,
                data=payload,
                actor_role=role,
                actor_tenant_id=self.tenant_id,
            )
            self.assertEqual(validated["tenant_id"], self.tenant_id)

    def test_offline_thermal_receipt_watermark(self):
        watermark_en = self.catalog.format_offline_thermal_watermark("PENDING_SYNC", "en")
        self.assertEqual(watermark_en, "*** OFFLINE RECEIPT - PENDING CLOUD CONFIRMATION ***")

        watermark_bn = self.catalog.format_offline_thermal_watermark("PENDING_SYNC", "bn")
        self.assertEqual(watermark_bn, "*** অফলাইন রসিদ - ক্লাউড কনফার্মেশন অপেক্ষমান ***")

        watermark_confirmed = self.catalog.format_offline_thermal_watermark("CONFIRMED", "en")
        self.assertIsNone(watermark_confirmed)

    def test_mandatory_field_rejection_antibiotic_register(self):
        invalid_payload = {
            "entry_id": "ANT-2026-0082",
            "tenant_id": self.tenant_id,
            "patient_name": "Abdul Karim",
            # Missing patient_address!
            "doctor_name": "Dr. S. Rahman",
            # Missing doctor_bmdc_no!
            "medicine_name": "Ciprofloxacin 500mg",
            "batch_number": "CP-2026-9",
            "quantity_dispensed": 10,
            "dispensed_date": datetime.date.today(),
            "pharmacist_license": "PCB-REG-10928",
        }
        with self.assertRaises(FieldValidationError) as ctx:
            self.catalog.validate_document_data(
                doc_type=DocumentType.ANTIBIOTIC_REGISTER,
                data=invalid_payload,
                actor_role="PHARMACIST",
                actor_tenant_id=self.tenant_id,
            )
        err = ctx.exception
        self.assertIn("patient_address", err.field_errors)
        self.assertIn("doctor_bmdc_no", err.field_errors)

    def test_float_money_strictly_forbidden(self):
        float_invoice = {
            "invoice_number": "INV-2026-FLOAT",
            "tenant_id": self.tenant_id,
            "branch_id": "branch-main",
            "timestamp": datetime.datetime.now(datetime.UTC),
            "cashier_name": "Rahim Ahmed",
            "subtotal": 500.50,  # FLOAT is strictly forbidden!
            "discount": Decimal("0.00"),
            "net_total": Decimal("500.50"),
            "items": [],
        }
        with self.assertRaises(FieldValidationError) as ctx:
            self.catalog.validate_document_data(
                doc_type=DocumentType.INVOICE,
                data=float_invoice,
                actor_role="CASHIER",
                actor_tenant_id=self.tenant_id,
            )
        self.assertIn("subtotal", ctx.exception.field_errors)
        self.assertIn("strictly forbidden", ctx.exception.field_errors["subtotal"][0])

    def test_role_authorization_blocked_cases(self):
        # Cashier attempting to generate Antibiotic Register (restricted to Pharmacist/Auditor)
        ant_payload = {
            "entry_id": "ANT-2026-0081",
            "tenant_id": self.tenant_id,
            "patient_name": "Fatema Begum",
            "patient_address": "Dhanmondi, Dhaka",
            "doctor_name": "Dr. Mahmud",
            "doctor_bmdc_no": "A-54321",
            "medicine_name": "Azithromycin 500mg",
            "batch_number": "AZ-2026-01",
            "quantity_dispensed": 6,
            "dispensed_date": datetime.date.today(),
            "pharmacist_license": "PCB-REG-10928",
        }
        with self.assertRaises(PermissionError) as ctx:
            self.catalog.validate_document_data(
                doc_type=DocumentType.ANTIBIOTIC_REGISTER,
                data=ant_payload,
                actor_role="CASHIER",
                actor_tenant_id=self.tenant_id,
            )
        self.assertIn("Role 'CASHIER' is not authorized", str(ctx.exception))

    def test_cross_tenant_isolation_blocked(self):
        doc_payload = {
            "invoice_number": "INV-2026-0001",
            "tenant_id": "tenant-other-pharmacy",
            "branch_id": "branch-main",
            "timestamp": datetime.datetime.now(datetime.UTC),
            "cashier_name": "Rahim Ahmed",
            "subtotal": Decimal("100.00"),
            "discount": Decimal("0.00"),
            "net_total": Decimal("100.00"),
            "items": [],
        }
        with self.assertRaises(PermissionError) as ctx:
            self.catalog.validate_document_data(
                doc_type=DocumentType.INVOICE,
                data=doc_payload,
                actor_role="CASHIER",
                actor_tenant_id="tenant-my-pharmacy",
            )
        self.assertIn("Cross-tenant document generation blocked", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
