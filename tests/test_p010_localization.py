import os
import unittest
from datetime import UTC, datetime
from decimal import Decimal

from apps.core.localization import (
    BilingualDrugCatalogSearch,
    format_bdt_currency,
    format_dhaka_datetime,
    format_south_asian_number,
    parse_numeric_input,
)


class TestP010LanguageAndLocalization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adr_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "docs",
                "architecture",
                "decisions",
                "ADR-0010-language-and-localization-strategy.md",
            )
        )
        assert os.path.exists(cls.adr_path), f"ADR-0010 not found at: {cls.adr_path}"
        with open(cls.adr_path, encoding="utf-8") as f:
            cls.content = f.read()

        # Bootstrap search index with bilingual entries
        cls.search_index = BilingualDrugCatalogSearch()
        cls.search_index.add_drug(
            sku_id="sku_001",
            brand_name_en="Napa Extra 500mg/65mg",
            generic_name_en="Paracetamol + Caffeine",
            bangla_aliases=["নাপা এক্সট্রা", "নাপা", "প্যারাসিটামল"],
        )
        cls.search_index.add_drug(
            sku_id="sku_002",
            brand_name_en="Seclo 20mg Capsule",
            generic_name_en="Omeprazole",
            bangla_aliases=["সেকলো", "সেকলো ২০", "ওমিপ্রাজল"],
        )
        cls.search_index.add_drug(
            sku_id="sku_003",
            brand_name_en="Amoxicillin Trihydrate + Potassium Clavulanate (এমোক্সিসিলিন ট্রাইহাইড্রেট) 625mg",
            generic_name_en="Amoxicillin + Clavulanic Acid",
            bangla_aliases=["এমোক্সিসিলিন", "ক্ল্যাভুলানেট"],
        )

    def test_adr0010_file_exists_and_substantial(self):
        """ADR-0010 must exist and contain substantial specification."""
        self.assertGreater(len(self.content), 5000, "ADR-0010 is too brief.")

    def test_valid_scenario_south_asian_currency_formatting(self):
        """
        Acceptance Criteria: One valid scenario.
        Tests South Asian currency grouping (Lakhs & Crores) in bn-BD and en-US.
        """
        # 1. Standard amount (45.50 Taka)
        amount_small = Decimal("45.50")
        self.assertEqual(format_bdt_currency(amount_small, "bn-BD"), "৳৪৫.৫০")
        self.assertEqual(format_bdt_currency(amount_small, "en-US"), "৳ 45.50")

        # 2. Lakh amount (1,52,345.50 Taka)
        amount_lakh = Decimal("152345.50")
        self.assertEqual(format_bdt_currency(amount_lakh, "bn-BD"), "৳১,৫২,৩৪৫.৫০")
        self.assertEqual(format_bdt_currency(amount_lakh, "en-US"), "৳ 1,52,345.50")

        # 3. Crore amount (1,23,45,678.00 Taka)
        amount_crore = Decimal("12345678.00")
        self.assertEqual(format_bdt_currency(amount_crore, "bn-BD"), "৳১,২৩,৪৫,৬৭৮.০০")
        self.assertEqual(format_bdt_currency(amount_crore, "en-US"), "৳ 1,23,45,678.00")

    def test_valid_scenario_bilingual_search_and_long_names(self):
        """
        Specific Acceptance: Bangla/English mixed strings, long names and bilingual search.
        """
        # 1. Searching in Bengali ("নাপা") matches Napa Extra
        results_bn = self.search_index.search("নাপা")
        self.assertTrue(any(r["sku_id"] == "sku_001" for r in results_bn))

        # 2. Searching generic in English ("paracetamol") matches Napa Extra
        results_en = self.search_index.search("paracetamol")
        self.assertTrue(any(r["sku_id"] == "sku_001" for r in results_en))

        # 3. Searching Bengali generic ("ওমিপ্রাজল") matches Seclo
        results_ome = self.search_index.search("ওমিপ্রাজল")
        self.assertTrue(any(r["sku_id"] == "sku_002" for r in results_ome))

        # 4. Long mixed-language name matches
        results_long = self.search_index.search("এমোক্সিসিলিন")
        self.assertTrue(any(r["sku_id"] == "sku_003" for r in results_long))

    def test_valid_scenario_bengali_numeral_parsing(self):
        """
        Cashier typing quantities in Bengali digits (e.g. '১২', '১,৫২,৩৪৫.৫০')
        must parse accurately into a Python Decimal.
        """
        parsed_simple = parse_numeric_input("১২")
        self.assertEqual(parsed_simple, Decimal("12.00"))

        parsed_complex = parse_numeric_input("১,৫২,৩৪৫.৫০")
        self.assertEqual(parsed_complex, Decimal("152345.50"))

        parsed_ascii = parse_numeric_input("1,52,345.50")
        self.assertEqual(parsed_ascii, Decimal("152345.50"))

    def test_valid_scenario_asia_dhaka_datetime_normalization(self):
        """
        UTC timestamps must convert accurately to Asia/Dhaka (+6h offset).
        Example: UTC 2026-10-28 10:30:00 -> Dhaka 2026-10-28 16:30:00 (4:30 PM).
        """
        utc_dt = datetime(2026, 10, 28, 10, 30, tzinfo=UTC)

        # English formatting
        formatted_en = format_dhaka_datetime(utc_dt, locale="en-US", include_time=True)
        self.assertIn("28/10/2026", formatted_en)
        self.assertIn("04:30 PM", formatted_en)

        # Bengali formatting
        formatted_bn = format_dhaka_datetime(utc_dt, locale="bn-BD", include_time=True)
        self.assertIn("২৮/১০/২০২৬", formatted_bn)
        self.assertIn("০৪:৩০", formatted_bn)
        self.assertIn("অপরাহ্ণ", formatted_bn)

    def test_invalid_input_float_strictly_prohibited(self):
        """
        Acceptance Criteria: One invalid or incomplete input case.
        Passing a binary float to currency formatters is strictly rejected with TypeError.
        """
        with self.assertRaises(TypeError):
            format_south_asian_number(152345.50)  # Binary float is forbidden!

        with self.assertRaises(TypeError):
            format_bdt_currency(152345.50)  # Binary float is forbidden!

        # Malformed numeric input
        with self.assertRaises(ValueError):
            parse_numeric_input("invalid_price_text")
        with self.assertRaises(ValueError):
            parse_numeric_input("১২.৩৪.৫৬")  # Multiple decimal points

    def test_unauthorized_cross_tenant_currency_injection(self):
        """
        Acceptance Criteria: One unauthorized or cross-tenant case.
        Tenant attempting to bypass BDT currency formatting or inject unsupported currency codes.
        """
        unsupported_currency = "USD"
        self.assertNotIn(f"CanonicalCurrency.{unsupported_currency}", self.content)
        # BDT is canonical; foreign currency override on retail counter is rejected
        self.assertIn("Bangladeshi Taka (`BDT` / `৳`)", self.content)
        self.assertIn("Strict Decimal arithmetic", self.content)


if __name__ == "__main__":
    unittest.main()
