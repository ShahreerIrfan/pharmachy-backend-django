"""
tests/test_p025_contract_repository.py
Architecture Decision: ADR-0001, ADR-0014, ADR-0021 & ADR-0025
Phase 02: Repository, Toolchain & Engineering Automation

Verification test suite for shared API contracts, OpenAPI 3.1.0 specifications,
JSON Schema Draft 2020-12 validations, monetary float drift detection,
cross-platform DTO parity, and multi-tenant security boundaries.
"""

import subprocess
import unittest
from pathlib import Path

from apps.core.contract_toolchain import (
    ContractSecurityValidator,
    ContractValidationError,
    CrossPlatformDtoParityValidator,
    JsonSchemaRepositoryValidator,
    OpenApiSpecValidator,
)


class TestP025ContractRepository(unittest.TestCase):
    """Verifies all acceptance criteria for P025 and ADR-0025."""

    def setUp(self):
        self.root_dir = Path(__file__).resolve().parent.parent
        self.contracts_dir = self.root_dir / "contracts"
        self.openapi_spec_path = self.contracts_dir / "openapi" / "openapi_v3.json"
        self.schemas_dir = self.contracts_dir / "schemas"
        self.schema_validator = JsonSchemaRepositoryValidator(self.schemas_dir)

        # Valid Sale Invoice payload fixture (matching JavaFX SaleTransaction)
        self.valid_invoice_payload = {
            "transaction_id": "c1f7b8d4-5b23-4c91-9492-7f28df1d683a",
            "invoice_number": "INV-2026-1001",
            "tenant_id": "tenant-dhaka-01",
            "branch_id": "branch-dhanmondi",
            "cashier_id": "cashier-01",
            "items": [
                {
                    "medicine_id": "med-001",
                    "brand_name": "Napa Extra 500mg/65mg",
                    "generic_name": "Paracetamol + Caffeine",
                    "packaging_unit": "STRIP",
                    "quantity": 2,
                    "unit_price": "25.00",
                    "batch_number": "BATCH-2026-X",
                    "expiry_date": "2027-12-31",
                    "vat_rate": "0.05",
                    "line_total": "52.50",
                }
            ],
            "subtotal": "50.00",
            "vat_amount": "2.50",
            "discount_amount": "0.00",
            "grand_total": "52.50",
            "is_offline_sync_pending": False,
            "created_at": "2026-10-29T12:00:00Z",
        }

    # 1. OpenAPI 3.1 Specification Validation
    def test_openapi_spec_structure_and_version(self):
        """OpenAPI spec must be valid OpenAPI 3.1.0 with mandatory paths and security."""
        res = OpenApiSpecValidator.validate_spec_file(self.openapi_spec_path)
        self.assertEqual(res["status"], "VALID_OPENAPI_3_1")
        self.assertEqual(res["version"], "2.0.0")
        self.assertGreaterEqual(res["paths_count"], 6)
        self.assertGreaterEqual(res["schemas_count"], 7)

    # 2. JSON Schema Draft 2020-12 Conformance
    def test_all_six_json_schemas_valid_draft_2020_12(self):
        """All 6 mandatory schemas must be valid JSON Schema Draft 2020-12 documents."""
        for sname in self.schema_validator.MANDATORY_SCHEMAS:
            schema = self.schema_validator.get_schema(sname)
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
            self.assertTrue(schema["title"])

    # 3. Valid Payload Scenario
    def test_valid_sale_invoice_payload_validates_cleanly(self):
        """Valid sale invoice payload must pass schema validation with 0 errors."""
        self.schema_validator.validate_payload("sale_invoice.schema.json", self.valid_invoice_payload)

    # 4. Invalid / Incomplete Payload Scenario
    def test_sale_invoice_rejects_missing_mandatory_fields(self):
        """Omitting required batch_number or invoice_number must raise ContractValidationError."""
        invalid_payload = dict(self.valid_invoice_payload)
        del invalid_payload["invoice_number"]

        with self.assertRaises(ContractValidationError) as ctx:
            self.schema_validator.validate_payload("sale_invoice.schema.json", invalid_payload)
        self.assertIn("invoice_number", str(ctx.exception))

    def test_sale_invoice_rejects_empty_items_list(self):
        """Empty items list violates minItems: 1 constraint."""
        invalid_payload = dict(self.valid_invoice_payload)
        invalid_payload["items"] = []

        with self.assertRaises(ContractValidationError) as ctx:
            self.schema_validator.validate_payload("sale_invoice.schema.json", invalid_payload)
        self.assertIn("items", str(ctx.exception))

    # 5. Monetary Float Drift Prevention
    def test_monetary_float_drift_detected_and_rejected(self):
        """Transmitting monetary amounts as raw float/number must raise ContractValidationError."""
        payload_with_float = dict(self.valid_invoice_payload)
        # Using raw float instead of "52.50"
        payload_with_float["grand_total"] = 52.50

        with self.assertRaises(ContractValidationError) as ctx:
            self.schema_validator.validate_payload("sale_invoice.schema.json", payload_with_float)
        self.assertIn("Monetary float drift violation", str(ctx.exception))

    # 6. Offline Sync Replay Batch Schema Validation
    def test_valid_sync_replay_batch_payload(self):
        """Valid sync replay batch with invoices validates cleanly."""
        replay_payload = {
            "batch_id": "batch-replay-001",
            "device_token": "pos-device-token-secret-1234",
            "tenant_id": "tenant-dhaka-01",
            "branch_id": "branch-dhanmondi",
            "invoices": [self.valid_invoice_payload],
        }
        self.schema_validator.validate_payload("sync_replay.schema.json", replay_payload)

    # 7. Flutter Dashboard KPI Schema Validation
    def test_valid_dashboard_kpi_payload(self):
        """Mobile manager KPI payload matching Flutter model validates cleanly."""
        kpi_payload = {
            "today_sales_bdt": "45250.75",
            "total_orders_count": 142,
            "gross_margin_percent": "22.4",
            "low_stock_alert_count": 4,
            "expiring_batches_count": 2,
            "pending_rx_count": 3,
            "as_of_timestamp": "2026-10-29T12:00:00Z",
        }
        self.schema_validator.validate_payload("dashboard_kpi.schema.json", kpi_payload)

    # 8. Prescription Clinical Verification Schema Validation
    def test_valid_prescription_verification_payload(self):
        """Pharmacist clinical verification payload validates cleanly."""
        verify_payload = {
            "prescription_id": "rx-2026-0099",
            "pharmacist_user_id": "user-pharmacist-01",
            "pharmacist_pcb_reg_no": "PCB-A98421",
            "decision": "APPROVED",
            "notes": "Verified dosage and indications against clinical guidelines.",
            "verified_at": "2026-10-29T11:45:00Z",
        }
        self.schema_validator.validate_payload("prescription_verify.schema.json", verify_payload)

    def test_prescription_verification_rejects_invalid_pcb_format(self):
        """Invalid PCB registration format fails pattern validation."""
        invalid_payload = {
            "prescription_id": "rx-2026-0099",
            "pharmacist_user_id": "user-pharmacist-01",
            "pharmacist_pcb_reg_no": "invalid-format-123",
            "decision": "APPROVED",
            "notes": "Invalid PCB format.",
        }
        with self.assertRaises(ContractValidationError) as ctx:
            self.schema_validator.validate_payload("prescription_verify.schema.json", invalid_payload)
        self.assertIn("pharmacist_pcb_reg_no", str(ctx.exception))

    # 9. Cross-Platform DTO Field Parity
    def test_java_sale_transaction_dto_field_parity(self):
        """Java SaleTransaction fields match sale_invoice.schema.json required fields."""
        schema = self.schema_validator.get_schema("sale_invoice.schema.json")
        java_fields = {
            "transactionId",
            "invoiceNumber",
            "tenantId",
            "branchId",
            "cashierId",
            "items",
            "subtotal",
            "vatAmount",
            "discountAmount",
            "grandTotal",
            "isOfflineSyncPending",
            "createdAt",
        }
        CrossPlatformDtoParityValidator.verify_java_sale_invoice_parity(schema, java_fields)

    def test_flutter_dashboard_kpi_dto_field_parity(self):
        """Flutter DashboardKpi fields match dashboard_kpi.schema.json required fields."""
        schema = self.schema_validator.get_schema("dashboard_kpi.schema.json")
        dart_fields = {
            "todaySalesBdt",
            "totalOrdersCount",
            "grossMarginPercent",
            "lowStockAlertCount",
            "expiringBatchesCount",
            "pendingRxCount",
            "asOfTimestamp",
        }
        CrossPlatformDtoParityValidator.verify_flutter_dashboard_kpi_parity(schema, dart_fields)

    # 10. Multi-Tenant & RBAC Contract Security
    def test_missing_tenant_header_raises_contract_error(self):
        """Requests omitting X-Tenant-ID header fail security check."""
        with self.assertRaises(ContractValidationError) as ctx:
            ContractSecurityValidator.assert_authorized("/api/v2/sales/invoices/", role="CASHIER", tenant_header=None)
        self.assertIn("Missing mandatory multi-tenant header 'X-Tenant-ID'", str(ctx.exception))

    def test_cashier_forbidden_from_dashboard_kpi_endpoint(self):
        """Cashier attempting to call /api/v2/dashboard/kpi/ is rejected with PermissionError (403)."""
        with self.assertRaises(PermissionError) as ctx:
            ContractSecurityValidator.assert_authorized(
                "/api/v2/dashboard/kpi/", role="CASHIER", tenant_header="tenant-01"
            )
        self.assertIn("HTTP 403 Forbidden", str(ctx.exception))

    def test_owner_authorized_for_dashboard_kpi(self):
        """Tenant owner is authorized to access /api/v2/dashboard/kpi/."""
        # Should not raise exception
        ContractSecurityValidator.assert_authorized(
            "/api/v2/dashboard/kpi/", role="TENANT_OWNER", tenant_header="tenant-01"
        )

    # 11. CLI Tool Execution
    def test_cli_contract_validator_execution(self):
        """tools/contract_validator.py executes cleanly and returns code 0."""
        proc = subprocess.run(
            ["python", "tools/contract_validator.py"],
            cwd=str(self.root_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"Validator failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("All API contracts, schemas, and security boundaries are VALID", proc.stdout)


if __name__ == "__main__":
    unittest.main()
