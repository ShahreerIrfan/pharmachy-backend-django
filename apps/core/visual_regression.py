"""
apps/core/visual_regression.py
Architecture Decision: ADR-0007, ADR-0009, ADR-0023, ADR-0024 & ADR-0032
Phase 02: Repository, Toolchain & Engineering Automation

Provides automated domain modeling, validation, and governance for Frontend Visual Regression Testing,
including JavaFX snapshot testing and Flutter golden tests with fixed fonts, scaling, dimensions,
and stable DGDA-informed fictitious fixtures.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class VisualBaselineError(Exception):
    """Raised when visual regression specs, dimensions, scaling, or baseline integrity are violated."""

    pass


class VisualBaselineSecurityError(PermissionError):
    """Raised when unauthorized actors attempt to mutate or approve visual golden baselines."""

    pass


class PlatformClient(str, Enum):
    DESKTOP_JAVAFX = "DESKTOP_JAVAFX"
    MOBILE_FLUTTER = "MOBILE_FLUTTER"


class VisualState(str, Enum):
    LOADED = "LOADED"
    LOADING = "LOADING"
    EMPTY = "EMPTY"
    ERROR = "ERROR"
    OFFLINE = "OFFLINE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    BILINGUAL_BN_EN = "BILINGUAL_BN_EN"


@dataclass(frozen=True)
class VisualDimensions:
    width: int
    height: int
    pixel_ratio: float = 1.0
    font_scale: float = 1.0

    def validate(self, client: PlatformClient) -> None:
        """Enforces normalized, approved dimensions and scaling rules."""
        if self.width <= 0 or self.height <= 0:
            raise VisualBaselineError(f"Invalid dimensions: width={self.width}, height={self.height}. Must be > 0.")

        if self.font_scale != 1.0:
            raise VisualBaselineError(
                f"Font scale must be frozen at 1.0 to prevent flaky visual regressions. Found {self.font_scale}."
            )

        if client == PlatformClient.DESKTOP_JAVAFX:
            # Approved desktop POS terminal aspect ratios (16:10 / 4:3)
            approved = [(1280, 800), (1024, 768), (1920, 1080)]
            if (self.width, self.height) not in approved:
                raise VisualBaselineError(
                    f"Non-standard JavaFX POS dimensions ({self.width}x{self.height}). "
                    f"Must match approved resolutions: {approved} (ADR-0032)."
                )
            if self.pixel_ratio != 1.0:
                raise VisualBaselineError(f"Desktop JavaFX pixel ratio must be fixed at 1.0. Found {self.pixel_ratio}.")

        elif client == PlatformClient.MOBILE_FLUTTER:
            # Approved mobile phone viewports
            approved = [(390, 844), (412, 915), (375, 667)]
            if (self.width, self.height) not in approved:
                raise VisualBaselineError(
                    f"Non-standard Flutter Mobile viewport ({self.width}x{self.height}). "
                    f"Must match approved viewports: {approved} (ADR-0032)."
                )
            if self.pixel_ratio not in (1.0, 2.0, 3.0):
                raise VisualBaselineError(
                    f"Flutter device pixel ratio must be 1.0, 2.0, or 3.0. Found {self.pixel_ratio}."
                )


@dataclass(frozen=True)
class SyntheticMedicineItem:
    medicine_id: str
    brand_name: str
    generic_name: str
    manufacturer: str
    packaging_unit: str
    unit_price_bdt: Decimal
    batch_number: str
    quantity: int
    is_prescription_only: bool = False

    @property
    def line_total_bdt(self) -> Decimal:
        return (self.unit_price_bdt * Decimal(self.quantity)).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class SyntheticPharmacyFixture:
    tenant_id: str = "tenant-dhk-01"
    tenant_name: str = "Lazz Pharma (Dhanmondi Branch)"
    branch_id: str = "branch-dhanmondi-main"
    cashier_id: str = "user-cashier-01"
    cashier_name: str = "Karim Ullah"
    frozen_timestamp: str = "2026-10-29T12:00:00Z"
    items: List[SyntheticMedicineItem] = field(default_factory=list)

    @classmethod
    def create_canonical_sale_fixture(cls) -> "SyntheticPharmacyFixture":
        """Builds standardized DGDA-informed sales cart fixture."""
        items = [
            SyntheticMedicineItem(
                medicine_id="med-001",
                brand_name="Napa Extra 500mg/65mg",
                generic_name="Paracetamol + Caffeine",
                manufacturer="Beximco Pharmaceuticals Ltd.",
                packaging_unit="Strip of 10",
                unit_price_bdt=Decimal("35.00"),
                batch_number="BX-2026-N1",
                quantity=2,
            ),
            SyntheticMedicineItem(
                medicine_id="med-002",
                brand_name="Seclo 20mg",
                generic_name="Omeprazole",
                manufacturer="Square Pharmaceuticals PLC",
                packaging_unit="Strip of 10",
                unit_price_bdt=Decimal("70.00"),
                batch_number="SQ-2026-S4",
                quantity=1,
            ),
            SyntheticMedicineItem(
                medicine_id="med-003",
                brand_name="Azithrocin 500mg",
                generic_name="Azithromycin",
                manufacturer="Incepta Pharmaceuticals Ltd.",
                packaging_unit="Strip of 3",
                unit_price_bdt=Decimal("140.00"),
                batch_number="IN-2026-A2",
                quantity=1,
                is_prescription_only=True,
            ),
        ]
        return cls(items=items)

    @property
    def subtotal_bdt(self) -> Decimal:
        return sum((item.line_total_bdt for item in self.items), Decimal("0.00")).quantize(Decimal("0.01"))

    @property
    def vat_bdt(self) -> Decimal:
        # Bangladesh 5% retail pharmacy VAT on eligible formulations
        return (self.subtotal_bdt * Decimal("0.05")).quantize(Decimal("0.01"))

    @property
    def grand_total_bdt(self) -> Decimal:
        return (self.subtotal_bdt + self.vat_bdt).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class VisualGoldenBaseline:
    baseline_id: str
    client: PlatformClient
    screen_name: str
    state: VisualState
    dimensions: VisualDimensions
    checksum_sha256: str
    tolerated_diff_percent: float = 0.50
    fixture: Optional[SyntheticPharmacyFixture] = None
    approved_by: str = "Dr. Tariq Hasan (Lead Enterprise Architect)"
    approved_at: str = "2026-10-29T12:00:00Z"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["client"] = self.client.value
        data["state"] = self.state.value
        return data


class VisualRegressionSecurityBoundary:
    """Enforces strict authorization and multi-tenant isolation over visual golden baselines."""

    AUTHORIZED_ROLES = {"PLATFORM_SUPER_ADMIN", "LEAD_ARCHITECT"}

    @classmethod
    def assert_can_mutate_baselines(cls, actor_role: str) -> None:
        if actor_role not in cls.AUTHORIZED_ROLES:
            raise VisualBaselineSecurityError(
                f"Unauthorized visual baseline mutation: Role '{actor_role}' is not authorized to update or "
                f"approve visual regression goldens. Authorized roles: {sorted(cls.AUTHORIZED_ROLES)} (ADR-0032)."
            )

    @classmethod
    def assert_tenant_boundary(cls, session_tenant_id: str, baseline_tenant_id: str) -> None:
        if session_tenant_id != baseline_tenant_id:
            raise VisualBaselineSecurityError(
                f"Cross-tenant visual fixture leak: Session tenant '{session_tenant_id}' cannot inspect or "
                f"modify visual baseline artifacts belonging to tenant '{baseline_tenant_id}' (ADR-0005, ADR-0032)."
            )


class VisualRegressionRegistry:
    """Manages canonical visual regression golden catalog for JavaFX and Flutter."""

    EMOJI_PATTERN = re.compile(
        r"[\U0001F600-\U0001F64F"  # Emoticons
        r"\U0001F300-\U0001F5FF"  # Misc Symbols and Pictographs
        r"\U0001F680-\U0001F6FF"  # Transport and Map
        r"\U0001F1E0-\U0001F1FF"  # Flags
        r"\U00002702-\U000027B0"
        r"\U000024C2-\U0001F251"
        r"\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
        r"]+",
        flags=re.UNICODE,
    )

    def __init__(self):
        self._baselines: Dict[str, VisualGoldenBaseline] = {}
        self._register_default_baselines()

    def _register_default_baselines(self) -> None:
        canonical_fixture = SyntheticPharmacyFixture.create_canonical_sale_fixture()
        desktop_dim = VisualDimensions(width=1280, height=800, pixel_ratio=1.0, font_scale=1.0)
        mobile_dim = VisualDimensions(width=390, height=844, pixel_ratio=2.0, font_scale=1.0)

        # 1. Desktop JavaFX POS Baselines (7 states)
        for state in VisualState:
            bid = f"desktop_pos_{state.value.lower()}"
            h = hashlib.sha256(
                f"desktop_{state.value}_{desktop_dim.width}_{desktop_dim.height}".encode("utf-8")
            ).hexdigest()
            self._baselines[bid] = VisualGoldenBaseline(
                baseline_id=bid,
                client=PlatformClient.DESKTOP_JAVAFX,
                screen_name="PosMainView",
                state=state,
                dimensions=desktop_dim,
                checksum_sha256=h,
                fixture=canonical_fixture if state in (VisualState.LOADED, VisualState.BILINGUAL_BN_EN) else None,
            )

        # 2. Mobile Flutter Manager Baselines (7 states)
        for state in VisualState:
            bid = f"mobile_manager_{state.value.lower()}"
            h = hashlib.sha256(
                f"mobile_{state.value}_{mobile_dim.width}_{mobile_dim.height}".encode("utf-8")
            ).hexdigest()
            self._baselines[bid] = VisualGoldenBaseline(
                baseline_id=bid,
                client=PlatformClient.MOBILE_FLUTTER,
                screen_name="ExecutiveDashboardScreen",
                state=state,
                dimensions=mobile_dim,
                checksum_sha256=h,
                fixture=canonical_fixture if state in (VisualState.LOADED, VisualState.BILINGUAL_BN_EN) else None,
            )

    def get_baseline(self, baseline_id: str) -> VisualGoldenBaseline:
        if baseline_id not in self._baselines:
            raise VisualBaselineError(f"Visual baseline '{baseline_id}' not found in registry.")
        return self._baselines[baseline_id]

    def list_baselines(self, client: Optional[PlatformClient] = None) -> List[VisualGoldenBaseline]:
        if client:
            return [b for b in self._baselines.values() if b.client == client]
        return list(self._baselines.values())

    def validate_baseline(self, baseline: VisualGoldenBaseline) -> Tuple[bool, List[str]]:
        """Audits baseline against strict dimensions, non-emoji rule, checksum, and precision."""
        issues: List[str] = []

        try:
            baseline.dimensions.validate(baseline.client)
        except VisualBaselineError as e:
            issues.append(str(e))

        # Validate checksum SHA-256 length
        if not re.fullmatch(r"^[a-f0-9]{64}$", baseline.checksum_sha256):
            issues.append(
                f"Invalid SHA-256 checksum '{baseline.checksum_sha256}' for baseline '{baseline.baseline_id}'."
            )

        # Validate tolerance
        if baseline.tolerated_diff_percent < 0.0 or baseline.tolerated_diff_percent > 5.0:
            issues.append(
                f"Tolerated visual diff {baseline.tolerated_diff_percent}% exceeds allowed threshold (0.0% to 5.0%)."
            )

        # Validate fixture if present
        if baseline.fixture:
            for item in baseline.fixture.items:
                if self.EMOJI_PATTERN.search(item.brand_name) or self.EMOJI_PATTERN.search(item.generic_name):
                    issues.append(f"Forbidden emoji found in synthetic item '{item.brand_name}' (ADR-0009).")
                if item.unit_price_bdt <= Decimal("0.00"):
                    issues.append(f"Item '{item.brand_name}' has invalid non-positive price: {item.unit_price_bdt}.")

        return len(issues) == 0, issues

    def register_baseline(self, baseline: VisualGoldenBaseline, actor_role: str = "LEAD_ARCHITECT") -> None:
        """Registers a new or updated visual baseline with RBAC guard."""
        VisualRegressionSecurityBoundary.assert_can_mutate_baselines(actor_role)
        is_valid, issues = self.validate_baseline(baseline)
        if not is_valid:
            raise VisualBaselineError(f"Cannot register invalid visual baseline: {', '.join(issues)}")
        self._baselines[baseline.baseline_id] = baseline


class VisualDiffEngine:
    """Calculates deterministic visual diff percentages between rendered frames and golden baselines."""

    @classmethod
    def compare_renders(
        cls,
        golden_checksum: str,
        actual_checksum: str,
        simulated_pixel_mismatches: int = 0,
        total_pixels: int = 1280 * 800,
    ) -> Dict[str, Any]:
        """
        Calculates exact diff percentage and determines PASS/FAIL.
        If checksums match identically, diff is strictly 0.0%.
        """
        if total_pixels <= 0:
            raise VisualBaselineError(f"Invalid total_pixels: {total_pixels}")

        if golden_checksum == actual_checksum and simulated_pixel_mismatches == 0:
            return {
                "status": "PASS",
                "diff_percent": 0.0,
                "mismatched_pixels": 0,
                "total_pixels": total_pixels,
                "is_match": True,
            }

        diff_percent = round((simulated_pixel_mismatches / total_pixels) * 100.0, 4)
        is_match = diff_percent <= 0.50  # 0.50% threshold

        return {
            "status": "PASS" if is_match else "FAIL",
            "diff_percent": diff_percent,
            "mismatched_pixels": simulated_pixel_mismatches,
            "total_pixels": total_pixels,
            "is_match": is_match,
        }
