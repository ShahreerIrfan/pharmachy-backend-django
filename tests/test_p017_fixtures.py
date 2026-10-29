"""
Unit and Contract Tests for ADR-0017: Domain Examples and Realistic Pharmaceutical Fixtures
Verifies:
1. Canonical registration of 7 realistic Bangladesh pharmaceutical archetypes.
2. Packaging unit conversion mathematics (Boxes -> Strips -> Tablets/Units).
3. Batch expiry and near-expiry detection.
4. Cold-chain storage temperature bounds and excursion validation (2.0°C - 8.0°C).
5. Floating-point price and temperature rejection (strict Decimal enforcement).
6. Multi-branch Stock Transfer Order (STO) within same tenant.
7. Cross-tenant stock transfer rejection (strict tenant isolation boundary).
"""

import datetime
import unittest
from decimal import Decimal

from apps.core.errors import FieldValidationError
from apps.core.fixtures import (
    BatchStockFixture,
    DomainFixtureCatalog,
    MedicineProductFixture,
    PackagingUnitHierarchy,
    StockTransferOrderFixture,
    TemperatureLogFixture,
)


class TestDomainExamplesAndFixtures(unittest.TestCase):
    def setUp(self):
        self.catalog = DomainFixtureCatalog()
        self.tenant_id = "tenant-evergreen-pharma"

    def test_canonical_registration_of_all_archetypes(self):
        products = self.catalog.list_products()
        self.assertGreaterEqual(len(products), 6)

        product_ids = {p.product_id for p in products}
        expected_ids = {
            "prod-napa-extra-500",
            "prod-ciprocin-500",
            "prod-tofen-syr-100",
            "prod-novorapid-pen-100",
            "prod-ceftron-1g-inj",
            "prod-folison-5mg-jar",
        }
        for eid in expected_ids:
            self.assertIn(eid, product_ids)

        # Multi-branch batches and STO check
        batches = self.catalog.list_batches()
        self.assertGreaterEqual(len(batches), 2)
        stos = self.catalog.list_stos()
        self.assertEqual(len(stos), 1)

    def test_packaging_unit_conversion_mathematics(self):
        napa = self.catalog.get_product("prod-napa-extra-500")
        packaging = napa.packaging

        # 1 Box = 20 Strips = 240 Tablets (12 tablets / strip)
        self.assertEqual(packaging.strips_per_box, 20)
        self.assertEqual(packaging.units_per_strip, 12)
        self.assertEqual(packaging.total_units_per_box, 240)

        # 2 Boxes, 3 Strips, 5 Tablets -> (2*240) + (3*12) + 5 = 480 + 36 + 5 = 521 tablets
        total_units = packaging.convert_to_base_units(boxes=2, strips=3, units=5)
        self.assertEqual(total_units, 521)

        # Breakdown string formatting
        breakdown = packaging.format_breakdown(521)
        self.assertEqual(breakdown, "2 Boxes, 3 Strips, 5 Tablets")

        # Exact box breakdown
        self.assertEqual(packaging.format_breakdown(240), "1 Box")

        # Price calculations
        # Unit MRP = 2.50 BDT, Strip MRP = 12 * 2.50 = 30.00 BDT, Box MRP = 240 * 2.50 = 600.00 BDT
        self.assertEqual(napa.calculate_strip_price(), Decimal("30.00"))
        self.assertEqual(napa.calculate_box_price(), Decimal("600.00"))

    def test_batch_expiry_and_near_expiry_detection(self):
        batch = BatchStockFixture(
            batch_id="batch-test-exp-01",
            product_id="prod-napa-extra-500",
            batch_number="TEST-EXP-01",
            tenant_id=self.tenant_id,
            branch_id="branch-main",
            quantity_base_units=100,
            expiry_date=datetime.date(2026, 6, 30),
            purchase_date=datetime.date(2026, 1, 1),
            cost_per_unit=Decimal("2.00"),
            status="ACTIVE",
        )

        # As of 2026-04-01: 90 days before June 30 is within 90 days -> Near Expiry
        as_of_april = datetime.date(2026, 4, 1)
        self.assertFalse(batch.is_expired(as_of_april))
        self.assertTrue(batch.is_near_expiry(as_of_april, days_threshold=90))

        # As of 2026-01-01: 180 days before June 30 -> Not Near Expiry
        as_of_jan = datetime.date(2026, 1, 1)
        self.assertFalse(batch.is_expired(as_of_jan))
        self.assertFalse(batch.is_near_expiry(as_of_jan, days_threshold=90))

        # As of 2026-07-01: Past June 30 -> Expired
        as_of_july = datetime.date(2026, 7, 1)
        self.assertTrue(batch.is_expired(as_of_july))
        self.assertFalse(batch.is_near_expiry(as_of_july, days_threshold=90))

    def test_cold_chain_validation_and_excursion_detection(self):
        # 1. Normal safe reading (4.5°C)
        normal_log = TemperatureLogFixture(
            log_id="LOG-01",
            tenant_id=self.tenant_id,
            branch_id="branch-main",
            storage_unit="FRIDGE-A",
            recorded_temp=Decimal("4.5"),
            is_excursion=False,
            timestamp=datetime.datetime.now(datetime.UTC),
            logged_by="Pharmacist",
        )
        is_safe = self.catalog.validate_cold_chain_reading(normal_log)
        self.assertTrue(is_safe)

        # 2. Excursion reading (9.8°C > 8.0°C) with correct excursion flag
        excursion_log = TemperatureLogFixture(
            log_id="LOG-02",
            tenant_id=self.tenant_id,
            branch_id="branch-main",
            storage_unit="FRIDGE-A",
            recorded_temp=Decimal("9.8"),
            is_excursion=True,
            timestamp=datetime.datetime.now(datetime.UTC),
            logged_by="Pharmacist",
        )
        is_safe = self.catalog.validate_cold_chain_reading(excursion_log)
        self.assertFalse(is_safe)

    def test_invalid_input_rejection(self):
        # 1. Float price strictly forbidden
        float_product = MedicineProductFixture(
            product_id="prod-float-fail",
            brand_name="Float Brand",
            generic_name="Generic Test",
            dosage_form="Tablet",
            strength="10mg",
            manufacturer="Test Pharma",
            packaging=PackagingUnitHierarchy(),
            mrp_unit=12.50,  # FLOAT is strictly forbidden!
            cost_unit=Decimal("10.00"),
            regulatory_class="OTC",
        )
        with self.assertRaises(FieldValidationError) as ctx:
            self.catalog.validate_product_fixture(float_product)
        self.assertIn("mrp_unit", ctx.exception.field_errors)

        # 2. Inconsistent cold-chain excursion flag
        mismatched_log = TemperatureLogFixture(
            log_id="LOG-03",
            tenant_id=self.tenant_id,
            branch_id="branch-main",
            storage_unit="FRIDGE-A",
            recorded_temp=Decimal("12.0"),  # Excursion!
            is_excursion=False,  # Inconsistent flag!
            timestamp=datetime.datetime.now(datetime.UTC),
            logged_by="Pharmacist",
        )
        with self.assertRaises(FieldValidationError) as ctx:
            self.catalog.validate_cold_chain_reading(mismatched_log)
        self.assertIn("is_excursion", ctx.exception.field_errors)

        # 3. Empty STO items
        empty_sto = StockTransferOrderFixture(
            sto_id="STO-EMPTY",
            tenant_id=self.tenant_id,
            source_branch_id="branch-main",
            dest_branch_id="branch-outlet",
            transfer_date=datetime.date.today(),
            status="DRAFT",
            items=[],  # Empty!
            transferred_by="Staff",
        )
        with self.assertRaises(FieldValidationError) as ctx:
            self.catalog.validate_stock_transfer(
                sto=empty_sto,
                source_branch_tenant=self.tenant_id,
                dest_branch_tenant=self.tenant_id,
            )
        self.assertIn("items", ctx.exception.field_errors)

    def test_unauthorized_and_cross_tenant_transfer_blocked(self):
        sto = self.catalog.list_stos()[0]

        # Attempt transfer where destination branch belongs to a different tenant
        other_tenant = "tenant-other-hospital-pharmacy"
        with self.assertRaises(PermissionError) as ctx:
            self.catalog.validate_stock_transfer(
                sto=sto,
                source_branch_tenant=self.tenant_id,
                dest_branch_tenant=other_tenant,
            )
        self.assertIn("Cross-tenant stock transfer strictly blocked", str(ctx.exception))

    def test_export_fixtures_dict_is_fully_json_serializable(self):
        import json

        data = self.catalog.export_fixtures_dict()
        self.assertIn("products", data)
        self.assertIn("batches", data)
        self.assertIn("stock_transfer_orders", data)
        self.assertIn("temperature_logs", data)

        # Invariant: Must serialize cleanly to valid JSON string
        json_str = json.dumps(data)
        self.assertTrue(len(json_str) > 500)

        # Deserialize and verify content
        parsed = json.loads(json_str)
        self.assertEqual(len(parsed["products"]), 6)
        self.assertEqual(len(parsed["batches"]), 2)
        self.assertEqual(len(parsed["stock_transfer_orders"]), 1)
        self.assertEqual(len(parsed["temperature_logs"]), 1)

    def test_stock_reconciliation_conserves_total_inventory(self):
        sto = self.catalog.list_stos()[0]
        # Item 0 in STO has 480 base units of Napa Extra
        # Dhanmondi has 2400 units, Mirpur has 100 units
        reconciliation = self.catalog.reconcile_stock_transfer(
            sto=sto,
            source_inventory_base_units=2400,
            dest_inventory_base_units=100,
            item_index=0,
        )
        self.assertEqual(reconciliation["source_before"], 2400)
        self.assertEqual(reconciliation["source_after"], 1920)
        self.assertEqual(reconciliation["dest_before"], 100)
        self.assertEqual(reconciliation["dest_after"], 580)
        self.assertEqual(
            reconciliation["source_before"] + reconciliation["dest_before"],
            reconciliation["source_after"] + reconciliation["dest_after"],
        )

        # Insufficient source stock raises FieldValidationError
        with self.assertRaises(FieldValidationError) as ctx:
            self.catalog.reconcile_stock_transfer(
                sto=sto,
                source_inventory_base_units=300,  # Less than 480 required!
                dest_inventory_base_units=100,
                item_index=0,
            )
        self.assertIn("source_inventory", ctx.exception.field_errors)

    def test_integration_with_operational_documents_inventory(self):
        from apps.core.documents import DocumentType, OperationalDocumentCatalog

        doc_catalog = OperationalDocumentCatalog()
        napa = self.catalog.get_product("prod-napa-extra-500")

        # Generate DOC-INV using Napa Extra fixture
        inv_payload = {
            "invoice_number": "INV-2026-FIXTURE-01",
            "tenant_id": self.tenant_id,
            "branch_id": "branch-main",
            "timestamp": datetime.datetime.now(datetime.UTC),
            "cashier_name": "Rahim Ahmed",
            "subtotal": napa.calculate_box_price(),  # Decimal("600.00")
            "discount": Decimal("0.00"),
            "net_total": napa.calculate_box_price(),
            "items": [
                {
                    "product_id": napa.product_id,
                    "brand_name": napa.brand_name,
                    "qty": 1,
                    "unit": napa.packaging.box_name,
                    "price": napa.calculate_box_price(),
                }
            ],
        }
        validated_inv = doc_catalog.validate_document_data(
            doc_type=DocumentType.INVOICE,
            data=inv_payload,
            actor_role="CASHIER",
            actor_tenant_id=self.tenant_id,
        )
        self.assertEqual(validated_inv["net_total"], Decimal("600.00"))


if __name__ == "__main__":
    unittest.main()
