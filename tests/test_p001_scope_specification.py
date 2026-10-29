import os
import re
import unittest


class TestP001ScopeSpecification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adr_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "docs",
                "architecture",
                "decisions",
                "ADR-0001-project-scope-and-non-goals.md",
            )
        )
        assert os.path.exists(cls.adr_path), f"ADR-0001 file not found at: {cls.adr_path}"
        with open(cls.adr_path, encoding="utf-8") as f:
            cls.content = f.read()

    def test_file_exists_and_substantial(self):
        """ADR-0001 must exist and contain at least 5000 characters of detailed specification."""
        self.assertGreater(len(self.content), 5000, "ADR content is too brief.")

    def test_required_core_sections_present(self):
        """All foundational architectural sections must be present."""
        required_headings = [
            "## 1. Context & Business Problem",
            "## 2. Actors & Personas",
            "## 3. In-Scope Workflows",
            "## 4. Explicit Non-Goals & Ten Negative Use Cases",
            "## 5. Stakeholder Scope Matrix",
            "## 6. Release Boundaries & Phasing",
            "## 7. Operational Assumptions & Hardware Matrix",
            "## 8. Compliance & Legal Review Status",
        ]
        for heading in required_headings:
            self.assertIn(heading, self.content, f"Missing required section: {heading}")

    def test_actors_and_personas_coverage(self):
        """All primary SaaS personas must be catalogued."""
        required_personas = [
            "Platform Super Admin",
            "Tenant Owner",
            "Branch Manager",
            "Registered Pharmacist",
            "Cashier",
            "Purchaser",
            "Accountant",
        ]
        for persona in required_personas:
            self.assertIn(persona, self.content, f"Persona '{persona}' not defined in ADR-0001")

    def test_prohibition_of_medical_diagnosis_and_auto_substitution(self):
        """Medical diagnosis and autonomous medicine substitution must be strictly prohibited."""
        self.assertRegex(
            self.content,
            r"(?i)No (?:Automated )?Medical Diagnosis",
            "Explicit prohibition of medical diagnosis must be stated.",
        )
        self.assertRegex(
            self.content,
            r"(?i)No Autonomous (?:Therapeutic )?Medicine Substitution",
            "Explicit prohibition of autonomous medicine substitution must be stated.",
        )

    def test_ten_negative_use_cases_complete(self):
        """Ten explicit negative use cases must be catalogued in Section 4."""
        # Find numbered items 01 to 10 in Section 4
        sec4_match = re.search(r"## 4\. Explicit Non-Goals.*?(?=## 5\.)", self.content, re.DOTALL)
        self.assertIsNotNone(sec4_match, "Section 4 could not be isolated.")
        sec4_text = sec4_match.group(0)

        for i in range(1, 11):
            num_str = f"{i:02d}"
            self.assertIn(f"**{num_str}**", sec4_text, f"Negative use case #{num_str} is missing from Section 4 table.")

    def test_stakeholder_scope_matrix_contains_clients_and_offline(self):
        """Stakeholder Scope Matrix must define Cloud, Desktop, Mobile and Offline capabilities."""
        self.assertIn("Cloud Canonical Authority (PostgreSQL)", self.content)
        self.assertIn("Desktop Client (JavaFX + Encrypted SQLite)", self.content)
        self.assertIn("Mobile Client (Flutter Cloud-First)", self.content)
        self.assertIn("Retail Barcode Checkout (POS)", self.content)
        self.assertIn("Prescription & Antibiotic Register", self.content)

    def test_monetary_precision_and_no_floats(self):
        """Monetary calculations must require Decimal and forbid float."""
        self.assertIn("Decimal", self.content)
        self.assertIn("binary floating-point (`float`) is strictly prohibited", self.content)

    def test_no_unsupported_certification_claims(self):
        """Must explicitly state that software does not claim official government certification."""
        self.assertRegex(
            self.content,
            r"(?i)No Fabricated Regulatory Certification",
            "Must guard against fabricated regulatory certification.",
        )


if __name__ == "__main__":
    unittest.main()
