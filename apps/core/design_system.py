"""
apps/core/design_system.py

Executable Design System Engine & Token Validator for Pharmacy ERP SaaS.
Enforces macOS-inspired visual restraint, WCAG 2.1 AA accessibility contrast,
8pt spatial scale, responsive viewport layouts, and anti-pattern bans (no emojis/no trademark copies).
"""

import re
from dataclasses import dataclass


class AccessibilityContrastError(Exception):
    """Raised when a color pair fails WCAG 2.1 AA contrast requirements."""

    pass


class InvalidSpacingGridError(Exception):
    """Raised when a layout dimension violates the 8pt spatial grid."""

    pass


class ProhibitedDesignAssetError(Exception):
    """Raised when forbidden assets (emojis, Apple trademarks) are detected."""

    pass


def hex_to_rgb(hex_code: str) -> tuple[int, int, int]:
    """Converts a 6-digit hex string (#RRGGBB) to (R, G, B) tuple."""
    if not isinstance(hex_code, str) or not hex_code.startswith("#") or len(hex_code) != 7:
        raise ValueError(
            f"Invalid hex color code: '{hex_code}' (must start with '#' and have exactly 6 hex characters)"
        )
    hex_clean = hex_code[1:]
    return (int(hex_clean[0:2], 16), int(hex_clean[2:4], 16), int(hex_clean[4:6], 16))


def calculate_relative_luminance(hex_code: str) -> float:
    """
    Calculates relative luminance per W3C WCAG 2.1 specifications.
    https://www.w3.org/WAI/GL/wiki/Relative_luminance
    """
    r_int, g_int, b_int = hex_to_rgb(hex_code)

    def transform_channel(c_int: int) -> float:
        c_srgb = c_int / 255.0
        if c_srgb <= 0.03928:
            return c_srgb / 12.92
        else:
            return ((c_srgb + 0.055) / 1.055) ** 2.4

    r_linear = transform_channel(r_int)
    g_linear = transform_channel(g_int)
    b_linear = transform_channel(b_int)

    return 0.2126 * r_linear + 0.7152 * g_linear + 0.0722 * b_linear


def calculate_contrast_ratio(hex_foreground: str, hex_background: str) -> float:
    """
    Calculates the contrast ratio between two hex colors: (L1 + 0.05) / (L2 + 0.05).
    Returns a rounded float (e.g. 7.42).
    """
    l1 = calculate_relative_luminance(hex_foreground)
    l2 = calculate_relative_luminance(hex_background)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    ratio = (lighter + 0.05) / (darker + 0.05)
    return round(ratio, 2)


@dataclass(frozen=True)
class ColorToken:
    name: str
    hex_code: str
    description: str


@dataclass(frozen=True)
class StatusBadgeToken:
    name: str
    text_color: str
    bg_color: str
    border_color: str
    description: str


@dataclass(frozen=True)
class ViewportLayoutSpec:
    breakpoint_name: str
    min_width_px: int
    sidebar_width_px: int
    cart_panel_width_px: int
    table_row_height_px: int
    primary_font_size_px: int


class DesignSystem:
    """Canonical repository of design tokens, spatial grid, and viewport layout specs."""

    # 1. Neutral Surfaces & Text
    CANVAS_BG = "#F8FAFC"  # Slate 50
    SURFACE_BG = "#FFFFFF"  # Pure White
    SURFACE_FROSTED = "#F1F5F9"  # Slate 100
    BORDER_SUBTLE = "#E2E8F0"  # Slate 200

    TEXT_PRIMARY = "#0F172A"  # Slate 900 (High contrast body & headers)
    TEXT_SECONDARY = "#475569"  # Slate 600 (Generics, batches)
    TEXT_MUTED = "#64748B"  # Slate 500 (Labels, timestamps)

    # 2. Clinical Teal-Blue Accents
    ACCENT_PRIMARY = "#0369A1"  # Sky 700
    ACCENT_TEAL = "#0F766E"  # Teal 700
    FOCUS_RING = "#0284C7"  # Sky 600 (Accessible 2px focus ring)

    # 3. Semantic Status Tokens (All meet WCAG 2.1 AAA on light backgrounds >= 8.0:1)
    STATUS_SUCCESS = StatusBadgeToken(
        name="SUCCESS",
        text_color="#14532D",  # Green 900 (High contrast)
        bg_color="#DCFCE7",  # Green 100
        border_color="#86EFAC",  # Green 300
        description="Cloud Confirmed, Active, Paid",
    )
    STATUS_WARNING = StatusBadgeToken(
        name="WARNING",
        text_color="#78350F",  # Amber 900 (High contrast)
        bg_color="#FEF3C7",  # Amber 100
        border_color="#FDE68A",  # Amber 300
        description="Pending Sync, Expiring Soon, Due Payment",
    )
    STATUS_DANGER = StatusBadgeToken(
        name="DANGER",
        text_color="#7F1D1D",  # Red 900 (High contrast)
        bg_color="#FEE2E2",  # Red 100
        border_color="#FCA5A5",  # Red 300
        description="Expired Batch, Recall Block, Sync Rejected",
    )

    # 4. 8pt Spatial Scale (with allowed 2px/4px sub-steps)
    ALLOWED_SPACING_STEPS: set = {2, 4, 8, 12, 16, 24, 32, 48, 64}

    # 5. Responsive Viewport Layout Specifications
    VIEWPORT_1280 = ViewportLayoutSpec(
        breakpoint_name="Desktop_Standard_1280",
        min_width_px=1280,
        sidebar_width_px=220,
        cart_panel_width_px=440,
        table_row_height_px=36,
        primary_font_size_px=13,
    )
    VIEWPORT_1440 = ViewportLayoutSpec(
        breakpoint_name="Widescreen_Retail_1440",
        min_width_px=1440,
        sidebar_width_px=240,
        cart_panel_width_px=440,
        table_row_height_px=38,
        primary_font_size_px=14,
    )
    VIEWPORT_1920 = ViewportLayoutSpec(
        breakpoint_name="Full_HD_Manager_1920",
        min_width_px=1920,
        sidebar_width_px=260,
        cart_panel_width_px=480,
        table_row_height_px=40,
        primary_font_size_px=14,
    )


class DesignSystemValidator:
    """Validates custom themes and layout configurations against accessibility rules."""

    MIN_NORMAL_TEXT_CONTRAST = 4.5  # WCAG 2.1 AA
    MIN_LARGE_TEXT_CONTRAST = 3.0  # WCAG 2.1 AA for 18pt+ or UI components

    EMOJI_REGEX = re.compile(r"[\U00010000-\U0010ffff]|[\u2600-\u27ff]|[\u2300-\u23ff]|[\u2b50-\u2b55]")

    FORBIDDEN_BRAND_TERMS = {"apple_logo", "sf_pro_display", "san_francisco_font", "apple_inc", "mac_logo"}

    @classmethod
    def validate_contrast(cls, foreground_hex: str, background_hex: str, is_large_text: bool = False) -> float:
        """
        Calculates contrast and raises AccessibilityContrastError if non-compliant.
        """
        ratio = calculate_contrast_ratio(foreground_hex, background_hex)
        required_minimum = cls.MIN_LARGE_TEXT_CONTRAST if is_large_text else cls.MIN_NORMAL_TEXT_CONTRAST
        if ratio < required_minimum:
            raise AccessibilityContrastError(
                f"Contrast ratio {ratio}:1 between {foreground_hex} and {background_hex} "
                f"fails WCAG 2.1 AA minimum of {required_minimum}:1."
            )
        return ratio

    @classmethod
    def validate_spacing(cls, pixel_dimension: int) -> bool:
        """
        Ensures all margins, paddings, and gaps conform to the 8pt grid scale.
        """
        if pixel_dimension not in DesignSystem.ALLOWED_SPACING_STEPS:
            raise InvalidSpacingGridError(
                f"Dimension {pixel_dimension}px violates 8pt grid scale. "
                f"Allowed values: {sorted(list(DesignSystem.ALLOWED_SPACING_STEPS))}"
            )
        return True

    @classmethod
    def validate_asset_string(cls, asset_string: str) -> bool:
        """
        Strictly prevents emojis and unauthorized Apple trademark strings.
        """
        if cls.EMOJI_REGEX.search(asset_string):
            raise ProhibitedDesignAssetError(f"Forbidden Unicode emoji detected in design asset: '{asset_string}'.")

        lower_str = asset_string.lower()
        for forbidden in cls.FORBIDDEN_BRAND_TERMS:
            if forbidden in lower_str:
                raise ProhibitedDesignAssetError(
                    f"Forbidden trademarked asset reference detected: '{forbidden}' in '{asset_string}'."
                )

        return True
