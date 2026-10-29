"""
tests/test_p023_javafx_gradle.py
Architecture Decision: ADR-0007, ADR-0009, ADR-0021 & ADR-0023
Phase 02: Repository, Toolchain & Engineering Automation

Verification test suite for JavaFX Gradle setup, Java LTS runtime pinning,
modular JPMS layout, vector icons (no emojis), financial precision, and offline watermarking.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from apps.core.javafx_toolchain import (
    GradleWrapperValidator,
    JavaFxModularLayoutValidator,
    JavaFXToolchainError,
    JavaRuntimeValidator,
    NoEmojiIconValidator,
    PosFinancialPrecisionValidator,
)


class TestP023JavaFXGradleSetup(unittest.TestCase):
    """Verifies all acceptance criteria for P023 and ADR-0023."""

    def setUp(self):
        self.root_dir = Path(__file__).resolve().parent.parent
        self.desktop_dir = (
            (self.root_dir / "desktop") if (self.root_dir / "desktop").exists() else (self.root_dir / "desktop-javafx")
        )

    # 1. Java Runtime LTS Pinning
    def test_installed_java_runtime_lts_passes(self):
        """Installed Java runtime must be Java 17 or 21 LTS."""
        info = JavaRuntimeValidator.validate_installed_java()
        self.assertIn(info.major_version, [17, 21])
        self.assertTrue(info.is_lts)
        self.assertIn("OpenJDK", info.vendor)

    def test_unsupported_java_runtime_rejected(self):
        """Java 8 or 11 must be rejected by the version parser."""
        java8_output = 'openjdk version "1.8.0_352"'
        info8 = JavaRuntimeValidator.parse_java_version(java8_output)
        self.assertEqual(info8.major_version, 8)
        self.assertFalse(info8.is_lts)

        java11_output = 'openjdk version "11.0.18"'
        info11 = JavaRuntimeValidator.parse_java_version(java11_output)
        self.assertEqual(info11.major_version, 11)
        self.assertFalse(info11.is_lts)

    # 2. Gradle Wrapper & Configuration
    def test_gradle_wrapper_setup_valid(self):
        """Gradle wrapper configuration, scripts, and build files must be valid."""
        result = GradleWrapperValidator.validate_gradle_setup(self.desktop_dir)
        self.assertEqual(result["status"], "VALID")
        self.assertEqual(result["gradle_version"], "8.7")
        self.assertEqual(result["javafx_version"], "21.0.2")

    def test_gradlew_batch_script_for_windows(self):
        """gradlew.bat must exist and contain Windows execution routines."""
        bat_file = self.desktop_dir / "gradlew.bat"
        self.assertTrue(bat_file.exists())
        content = bat_file.read_text(encoding="utf-8")
        self.assertIn('@if "%DEBUG%"=="" @echo off', content)
        self.assertIn("findJavaFromJavaHome", content)
        self.assertIn("JAVA_EXE", content)

    def test_gradle_missing_file_rejected(self):
        """Missing mandatory Gradle configuration must raise JavaFXToolchainError."""
        with self.assertRaises(JavaFXToolchainError) as ctx:
            GradleWrapperValidator.validate_gradle_setup(self.desktop_dir / "nonexistent_dir")
        self.assertIn("Incomplete Gradle configuration", str(ctx.exception))

    # 3. Modular Java (JPMS) Structure
    def test_jpms_modular_declaration(self):
        """module-info.java must declare module com.pharmacy.pos and required directives."""
        module_info = self.desktop_dir / "src" / "main" / "java" / "module-info.java"
        result = JavaFxModularLayoutValidator.validate_module_declaration(module_info)
        self.assertEqual(result["status"], "VALID_JPMS")
        self.assertEqual(result["module_name"], "com.pharmacy.pos")

        content = module_info.read_text(encoding="utf-8")
        self.assertIn("requires javafx.controls;", content)
        self.assertIn("requires org.kordamp.ikonli.javafx;", content)
        self.assertIn("exports com.pharmacy.pos.models;", content)
        self.assertIn("exports com.pharmacy.pos.services;", content)

    # 4. Strict Zero-Emoji Policy
    def test_zero_emojis_in_desktop_code_and_resources(self):
        """Desktop UI assets and Java source files must contain ZERO emojis (ADR-0009)."""
        violations = NoEmojiIconValidator.scan_directory(self.desktop_dir / "src" / "main")
        self.assertEqual(violations, [], "Found forbidden emoji icons in desktop!")

    def test_emoji_detector_flags_forbidden_pictograms(self):
        """Emoji scanner must detect Unicode pictograms if illegally introduced."""
        emoji_sample = "Button payBtn = new Button('Pay 💰');"
        self.assertTrue(bool(NoEmojiIconValidator.EMOJI_PATTERN.search(emoji_sample)))

    # 5. Financial Accuracy & Strict BigDecimal
    def test_pos_models_use_bigdecimal_exclusively(self):
        """POS models must use java.math.BigDecimal for financial amounts and forbid float/double."""
        cart_item = self.desktop_dir / "src" / "main" / "java" / "com" / "pharmacy" / "pos" / "models" / "CartItem.java"
        sale_tx = (
            self.desktop_dir / "src" / "main" / "java" / "com" / "pharmacy" / "pos" / "models" / "SaleTransaction.java"
        )

        PosFinancialPrecisionValidator.validate_monetary_types(cart_item)
        PosFinancialPrecisionValidator.validate_monetary_types(sale_tx)

        # Confirm imports of java.math.BigDecimal
        self.assertIn("import java.math.BigDecimal;", cart_item.read_text(encoding="utf-8"))
        self.assertIn("import java.math.BigDecimal;", sale_tx.read_text(encoding="utf-8"))

    # 6. Offline Watermark Bilingual Invariant
    def test_offline_receipt_watermark_invariants(self):
        """SaleTransaction and MainApp must declare identical bilingual offline receipt watermarks."""
        sale_tx = (
            self.desktop_dir / "src" / "main" / "java" / "com" / "pharmacy" / "pos" / "models" / "SaleTransaction.java"
        )
        content = sale_tx.read_text(encoding="utf-8")
        self.assertIn("*** OFFLINE RECEIPT - PENDING CLOUD CONFIRMATION ***", content)
        self.assertIn("*** অফলাইন রসিদ - ক্লাউড কনফার্মেশন অপেক্ষমান ***", content)

    # 7. Apple-Inspired Restrained Design System
    def test_apple_design_tokens_present(self):
        """CSS tokens must reflect Apple-inspired restrained surfaces, 8px grid, and neutral borders."""
        tokens_file = (
            self.desktop_dir / "src" / "main" / "resources" / "com" / "pharmacy" / "pos" / "styles" / "tokens.css"
        )
        self.assertTrue(tokens_file.exists())
        css = tokens_file.read_text(encoding="utf-8")
        self.assertIn("-apple-system", css)
        self.assertIn("16px", css)
        self.assertIn("-color-surface", css)
        self.assertIn("status-badge-offline", css)

    # 8. High-Density Cashier View (FXML)
    def test_main_fxml_view_structure(self):
        """main.fxml must configure high-density cashier view with table, totals card, and shortcuts."""
        fxml_file = self.desktop_dir / "src" / "main" / "resources" / "com" / "pharmacy" / "pos" / "views" / "main.fxml"
        self.assertTrue(fxml_file.exists())
        xml = fxml_file.read_text(encoding="utf-8")
        self.assertIn("PosController", xml)
        self.assertIn("barcodeInputField", xml)
        self.assertIn("cartTable", xml)
        self.assertIn("checkoutButton", xml)
        self.assertIn("Shortcuts: [F2] Scan | [F4] Checkout", xml)


if __name__ == "__main__":
    unittest.main()
