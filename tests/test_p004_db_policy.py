import os
import unittest


class TestP004DatabasePolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adr_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "docs",
                "architecture",
                "decisions",
                "ADR-0004-database-policy-and-sql-portability.md",
            )
        )
        assert os.path.exists(cls.adr_path), f"ADR-0004 not found at: {cls.adr_path}"
        with open(cls.adr_path, encoding="utf-8") as f:
            cls.content = f.read()

    def test_adr0004_file_exists_and_substantial(self):
        """ADR-0004 must exist and contain substantial specification."""
        self.assertGreater(len(self.content), 4000, "ADR-0004 is too brief.")

    def test_environment_matrix_covers_all_tiers(self):
        """Must cover Local Dev SQLite, CI PostgreSQL, Production PostgreSQL, and Desktop SQLCipher."""
        self.assertIn("SQLite 3", self.content)
        self.assertIn("PostgreSQL 16+", self.content)
        self.assertIn("SQLCipher 4+", self.content)
        self.assertIn("Mandatory integration parity gate", self.content)

    def test_separation_of_dev_sqlite_vs_device_sqlite(self):
        """Must explicitly differentiate Django Dev SQLite from Device Encrypted SQLite."""
        self.assertIn("Django Development SQLite", self.content)
        self.assertIn("JavaFX Device SQLite (SQLCipher)", self.content)
        self.assertRegex(
            self.content,
            r"Django Development SQLite and Device SQLite are two completely distinct systems",
            "Must explicitly emphasize that Django Dev SQLite and Device SQLite are distinct.",
        )

    def test_sql_portability_risk_matrix_covers_all_critical_risks(self):
        """Must detail all 6 SQL portability risks between PostgreSQL and SQLite."""
        required_risks = [
            "Row-Level Locking (`select_for_update`)",
            "Monetary & Numeric Precision",
            "Partial / Conditional Unique Constraints",
            "Date & Timezone Handling",
            "JSON & Dynamic Querying",
            "Case-Insensitive String Matching",
        ]
        for risk in required_risks:
            self.assertIn(risk, self.content, f"Missing risk coverage for: {risk}")

    def test_monetary_precision_guardrail_enforces_decimal(self):
        """Must enforce DecimalField(max_digits=14, decimal_places=2) and forbid float."""
        self.assertIn("DecimalField(max_digits=14, decimal_places=2)", self.content)
        self.assertIn("Binary `float` is strictly banned", self.content)

    def test_locking_contract_uses_select_for_update_atomic(self):
        """Must mandate transaction.atomic() with select_for_update() for stock concurrency."""
        self.assertIn("select_for_update", self.content)
        self.assertIn("transaction.atomic", self.content)
        self.assertIn("reserve_batch_stock", self.content)


if __name__ == "__main__":
    unittest.main()
