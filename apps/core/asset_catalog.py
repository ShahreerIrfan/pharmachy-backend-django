"""
apps/core/asset_catalog.py

Executable Iconography & Visual Asset Catalog for Pharmacy ERP SaaS.
Enforces vector SVG licensing (Lucide/Ikonli), localized accessibility labels,
strict no-emoji audit, and Antigravity Nano Banana static artwork management.
"""

import re
from dataclasses import dataclass
from enum import Enum


class IconCategory(str, Enum):
    NAVIGATION = "NAVIGATION"
    ACTION = "ACTION"
    STATUS = "STATUS"
    CLINICAL = "CLINICAL"


class AssetPolicyViolationError(Exception):
    """Raised when an asset violates licensing, format, or no-emoji rules."""

    pass


@dataclass(frozen=True)
class IconDefinition:
    key: str
    package: str  # e.g. "lucide"
    license: str  # e.g. "ISC License"
    svg_identifier: str  # e.g. "lucide:shopping-cart"
    alt_en: str
    alt_bn: str
    category: IconCategory
    standard_size_px: int = 20


@dataclass(frozen=True)
class StaticArtworkRequirement:
    asset_id: str
    purpose: str
    target_resolution: tuple[int, int]
    aspect_ratio: str
    prompt_recipe: str
    fallback_vector_svg: str


class AssetCatalog:
    """Canonical registry and compliance auditor for icons and visual artwork."""

    EMOJI_PATTERN = re.compile(r"[\U00010000-\U0010ffff]|[\u2600-\u27ff]|[\u2300-\u23ff]|[\u2b50-\u2b55]")

    ALLOWED_LICENSES = {"ISC License", "Apache-2.0", "MIT", "BSD-3-Clause"}

    def __init__(self):
        self._icons: dict[str, IconDefinition] = {}
        self._artwork_requirements: dict[str, StaticArtworkRequirement] = {}
        self._bootstrap_core_catalog()

    def register_icon(self, icon: IconDefinition):
        """Validates and registers an icon. Raises AssetPolicyViolationError on policy breach."""
        # 1. No-Emoji Check
        for field_val in [icon.key, icon.svg_identifier, icon.alt_en, icon.alt_bn]:
            if self.EMOJI_PATTERN.search(field_val):
                raise AssetPolicyViolationError(
                    f"Forbidden Unicode emoji detected in icon definition '{icon.key}': '{field_val}'"
                )

        # 2. License Check
        if icon.license not in self.ALLOWED_LICENSES:
            raise AssetPolicyViolationError(
                f"Unauthorized or unvetted license '{icon.license}' for icon '{icon.key}'. "
                f"Allowed licenses: {self.ALLOWED_LICENSES}"
            )

        # 3. Vector SVG Format Check (Reject raster formats like .png, .jpg)
        lower_id = icon.svg_identifier.lower()
        if lower_id.endswith(".png") or lower_id.endswith(".jpg") or lower_id.endswith(".jpeg"):
            raise AssetPolicyViolationError(
                f"Functional icons must be vector SVGs, not raster images: '{icon.svg_identifier}'"
            )

        self._icons[icon.key] = icon

    def get_icon(self, key: str) -> IconDefinition | None:
        return self._icons.get(key)

    def get_all_icons(self) -> list[IconDefinition]:
        return list(self._icons.values())

    def get_artwork_requirement(self, asset_id: str) -> StaticArtworkRequirement | None:
        return self._artwork_requirements.get(asset_id)

    def audit_all_icons(self) -> dict[str, int]:
        """Audits entire catalog and returns verification summary."""
        total = len(self._icons)
        no_emoji_verified = 0
        license_verified = 0
        accessible_labels_verified = 0

        for icon in self._icons.values():
            if not self.EMOJI_PATTERN.search(icon.alt_en) and not self.EMOJI_PATTERN.search(icon.alt_bn):
                no_emoji_verified += 1
            if icon.license in self.ALLOWED_LICENSES:
                license_verified += 1
            if icon.alt_en.strip() and icon.alt_bn.strip():
                accessible_labels_verified += 1

        return {
            "total_icons": total,
            "no_emoji_verified": no_emoji_verified,
            "license_verified": license_verified,
            "accessible_labels_verified": accessible_labels_verified,
        }

    def _bootstrap_core_catalog(self):
        # Navigation Icons (ADR-0006)
        nav_icons = [
            ("nav-dashboard", "lucide:layout-dashboard", "Dashboard Overview", "ড্যাশবোর্ড ওভারভিউ"),
            ("nav-pos", "lucide:shopping-cart", "Retail POS Checkout", "বিক্রয় কাউন্টার"),
            ("nav-sales", "lucide:receipt", "Sales and Returns", "বিক্রয় ও রিটার্ন"),
            ("nav-purchases", "lucide:truck", "Purchases and Receiving", "ক্রয় ও মালামাল গ্রহণ"),
            ("nav-inventory", "lucide:boxes", "Inventory Ledger", "ইনভেন্টরি লেজার"),
            ("nav-catalog", "lucide:pill", "Medicine Catalog", "ওষুধ ক্যাটালগ"),
            ("nav-batches", "lucide:calendar-clock", "Batches and Expiry", "ব্যাচ ও মেয়াদোত্তীর্ণ"),
            ("nav-rx", "lucide:file-text", "Prescription Verification", "প্রেসক্রিপশন ভেরিফিকেশন"),
            ("nav-antibiotic", "lucide:shield-alert", "Antibiotic Register", "অ্যান্টিবায়োটিক রেজিস্টার"),
            ("nav-customers", "lucide:users", "Customers and Receivables", "গ্রাহক ও বাকির খাতা"),
            ("nav-suppliers", "lucide:building-2", "Suppliers Directory", "সরবরাহকারী তালিকা"),
            ("nav-accounting", "lucide:book-open", "General Ledger", "জেনারেল লেজার"),
            ("nav-compliance", "lucide:clipboard-check", "DGDA Compliance Logs", "ডিজিডিএ কমপ্লায়েন্স"),
            ("nav-sync", "lucide:refresh-cw", "Device and Sync Status", "ডিভাইস ও সিঙ্ক স্ট্যাটাস"),
        ]
        for key, svg_id, alt_en, alt_bn in nav_icons:
            self.register_icon(
                IconDefinition(
                    key=key,
                    package="lucide",
                    license="ISC License",
                    svg_identifier=svg_id,
                    alt_en=alt_en,
                    alt_bn=alt_bn,
                    category=IconCategory.NAVIGATION,
                    standard_size_px=24,
                )
            )

        # Operational Action Icons
        actions = [
            ("action-search", "lucide:search", "Search Records", "খুঁজুন", IconCategory.ACTION),
            ("action-print", "lucide:printer", "Print Receipt", "রসিদ প্রিন্ট করুন", IconCategory.ACTION),
            ("action-barcode", "lucide:scan-barcode", "Scan Barcode Wedge", "বারকোড স্ক্যান", IconCategory.ACTION),
            ("action-refund", "lucide:rotate-ccw", "Issue Return or Refund", "ফেরত বা রিফান্ড", IconCategory.ACTION),
            (
                "action-verify-rx",
                "lucide:check-square",
                "Sign Prescription Verification",
                "প্রেসক্রিপশন সাইন-অফ",
                IconCategory.CLINICAL,
            ),
        ]
        for key, svg_id, alt_en, alt_bn, cat in actions:
            self.register_icon(
                IconDefinition(
                    key=key,
                    package="lucide",
                    license="ISC License",
                    svg_identifier=svg_id,
                    alt_en=alt_en,
                    alt_bn=alt_bn,
                    category=cat,
                    standard_size_px=20,
                )
            )

        # Status Indicators
        statuses = [
            ("status-ok", "lucide:check-circle-2", "Cloud Confirmed Success", "সফল ও ক্লাউড কনফার্মড"),
            ("status-pending", "lucide:clock", "Pending Cloud Sync", "ক্লাউড সিঙ্ক অপেক্ষমাণ"),
            ("status-danger", "lucide:alert-octagon", "Hard Block or Expired", "ব্লক অথবা মেয়াদোত্তীর্ণ"),
        ]
        for key, svg_id, alt_en, alt_bn in statuses:
            self.register_icon(
                IconDefinition(
                    key=key,
                    package="lucide",
                    license="ISC License",
                    svg_identifier=svg_id,
                    alt_en=alt_en,
                    alt_bn=alt_bn,
                    category=IconCategory.STATUS,
                    standard_size_px=16,
                )
            )

        # Static Artwork Requirements (Nano Banana Specifications)
        self._artwork_requirements["img-onboarding-hero"] = StaticArtworkRequirement(
            asset_id="img-onboarding-hero",
            purpose="Tenant Onboarding Wizard Hero Artwork",
            target_resolution=(1200, 675),
            aspect_ratio="16:9",
            prompt_recipe=(
                "Subtle editorial illustration for a modern Bangladesh pharmacy SaaS app, "
                "pharmacist welcoming a customer in a bright organized dispensary, gentle daylight, "
                "natural human scale, ivory and mist-blue surfaces, restrained teal accent, minimal composition, "
                "ample negative space on the left for real UI text overlay, tasteful realistic materials, "
                "no text, no logos, no medicine dosage instructions, accessible neutral palette, 16:9."
            ),
            fallback_vector_svg="assets/vectors/fallbacks/onboarding_hero_fallback.svg",
        )
        self._artwork_requirements["img-empty-shelf"] = StaticArtworkRequirement(
            asset_id="img-empty-shelf",
            purpose="Zero-Stock Initial Catalog State Artwork",
            target_resolution=(600, 450),
            aspect_ratio="4:3",
            prompt_recipe=(
                "Subtle editorial illustration for a modern Bangladesh pharmacy SaaS app, "
                "clean empty pharmacy storage shelf ready for initial inventory stocking, gentle morning light, "
                "minimalist aesthetic, mist-blue and warm gray tones, ample center negative space, "
                "no text, no logos, no medicine dosage instructions, accessible neutral palette, 4:3."
            ),
            fallback_vector_svg="assets/vectors/fallbacks/empty_shelf_fallback.svg",
        )
