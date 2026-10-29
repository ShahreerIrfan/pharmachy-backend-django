"""
Domain Examples and Realistic Pharmaceutical Fixtures Catalog
Architecture Decision: ADR-0017
Provides concrete, realistic domain fixtures spanning tablet strips, syrups,
insulin pens, injection vials, cold-chain products, loose pills, and multi-branch operations.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from apps.core.errors import FieldValidationError


@dataclass(frozen=True)
class PackagingUnitHierarchy:
    """Represents the multi-tier packaging hierarchy common in Bangladesh pharmaceuticals."""

    box_name: str = "Box"
    strip_name: str | None = "Strip"
    unit_name: str = "Tablet"
    strips_per_box: int = 1
    units_per_strip: int = 1

    @property
    def total_units_per_box(self) -> int:
        return self.strips_per_box * self.units_per_strip

    def convert_to_base_units(self, boxes: int = 0, strips: int = 0, units: int = 0) -> int:
        """Converts arbitrary packaging combination into base units."""
        return (boxes * self.total_units_per_box) + (strips * self.units_per_strip) + units

    def format_breakdown(self, base_units: int) -> str:
        """Formats base units back into a readable Box / Strip / Unit string."""
        if self.strip_name and self.strips_per_box > 1:
            boxes = base_units // self.total_units_per_box
            remainder = base_units % self.total_units_per_box
            strips = remainder // self.units_per_strip
            units = remainder % self.units_per_strip
            parts = []
            if boxes > 0 or (strips == 0 and units == 0):
                parts.append(
                    f"{boxes} {self.box_name}{'es' if boxes != 1 and self.box_name.endswith('x') else 's' if boxes != 1 else ''}"
                )
            if strips > 0:
                parts.append(f"{strips} {self.strip_name}{'s' if strips != 1 else ''}")
            if units > 0 or not parts:
                parts.append(f"{units} {self.unit_name}{'s' if units != 1 else ''}")
            return ", ".join(parts)
        else:
            boxes = base_units // self.total_units_per_box
            units = base_units % self.total_units_per_box
            parts = []
            if boxes > 0:
                parts.append(f"{boxes} {self.box_name}")
            if units > 0 or not parts:
                parts.append(f"{units} {self.unit_name}{'s' if units != 1 else ''}")
            return ", ".join(parts)


@dataclass(frozen=True)
class MedicineProductFixture:
    """Canonical model for pharmaceutical products in the master catalog."""

    product_id: str
    brand_name: str
    generic_name: str
    dosage_form: str
    strength: str
    manufacturer: str
    packaging: PackagingUnitHierarchy
    mrp_unit: Decimal
    cost_unit: Decimal
    regulatory_class: str  # OTC, PRESCRIPTION_ONLY, ANTIBIOTIC, NARCOTIC
    requires_cold_chain: bool = False
    min_temp_celsius: Decimal | None = None
    max_temp_celsius: Decimal | None = None

    def calculate_box_price(self) -> Decimal:
        return self.mrp_unit * Decimal(self.packaging.total_units_per_box)

    def calculate_strip_price(self) -> Decimal:
        return self.mrp_unit * Decimal(self.packaging.units_per_strip)


@dataclass(frozen=True)
class BatchStockFixture:
    """Represents a specific physical batch of medicine held at a branch."""

    batch_id: str
    product_id: str
    batch_number: str
    tenant_id: str
    branch_id: str
    quantity_base_units: int
    expiry_date: datetime.date
    purchase_date: datetime.date
    cost_per_unit: Decimal
    status: str = "ACTIVE"  # ACTIVE, QUARANTINED, EXPIRED, RECALLED

    def is_expired(self, as_of: datetime.date) -> bool:
        return as_of >= self.expiry_date

    def is_near_expiry(self, as_of: datetime.date, days_threshold: int = 90) -> bool:
        if self.is_expired(as_of):
            return False
        delta = (self.expiry_date - as_of).days
        return 0 <= delta <= days_threshold


@dataclass(frozen=True)
class StockTransferOrderFixture:
    """Represents an Inter-Branch Stock Transfer Order (STO) within the same tenant."""

    sto_id: str
    tenant_id: str
    source_branch_id: str
    dest_branch_id: str
    transfer_date: datetime.date
    status: str  # DRAFT, IN_TRANSIT, RECEIVED, CANCELLED
    items: list[dict[str, Any]]
    transferred_by: str


@dataclass(frozen=True)
class TemperatureLogFixture:
    """Cold-chain storage temperature logger entry."""

    log_id: str
    tenant_id: str
    branch_id: str
    storage_unit: str
    recorded_temp: Decimal
    is_excursion: bool
    timestamp: datetime.datetime
    logged_by: str


class DomainFixtureCatalog:
    """Registry and validation engine for realistic Bangladesh pharmaceutical fixtures."""

    def __init__(self):
        self._products: dict[str, MedicineProductFixture] = {}
        self._batches: list[BatchStockFixture] = []
        self._stos: list[StockTransferOrderFixture] = []
        self._temperature_logs: list[TemperatureLogFixture] = []
        self._load_canonical_fixtures()

    def _load_canonical_fixtures(self) -> None:
        # 1. Tablet Blister Strips (OTC): Napa Extra
        napa_extra = MedicineProductFixture(
            product_id="prod-napa-extra-500",
            brand_name="Napa Extra",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg + 65mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            packaging=PackagingUnitHierarchy(
                box_name="Box",
                strip_name="Strip",
                unit_name="Tablet",
                strips_per_box=20,
                units_per_strip=12,  # 20 * 12 = 240 tablets
            ),
            mrp_unit=Decimal("2.50"),
            cost_unit=Decimal("2.00"),
            regulatory_class="OTC",
            requires_cold_chain=False,
        )
        self._products[napa_extra.product_id] = napa_extra

        # 2. Antibiotic Blister Strips: Ciprocin 500
        ciprocin = MedicineProductFixture(
            product_id="prod-ciprocin-500",
            brand_name="Ciprocin 500",
            generic_name="Ciprofloxacin",
            dosage_form="Tablet",
            strength="500mg",
            manufacturer="Square Pharmaceuticals PLC",
            packaging=PackagingUnitHierarchy(
                box_name="Box",
                strip_name="Strip",
                unit_name="Tablet",
                strips_per_box=3,
                units_per_strip=10,  # 3 * 10 = 30 tablets
            ),
            mrp_unit=Decimal("15.00"),
            cost_unit=Decimal("12.20"),
            regulatory_class="ANTIBIOTIC",
            requires_cold_chain=False,
        )
        self._products[ciprocin.product_id] = ciprocin

        # 3. Oral Suspension / Syrup: Tofen Syrup
        tofen = MedicineProductFixture(
            product_id="prod-tofen-syr-100",
            brand_name="Tofen Syrup",
            generic_name="Ketotifen",
            dosage_form="Syrup",
            strength="1mg/5ml (100ml)",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            packaging=PackagingUnitHierarchy(
                box_name="Unit Carton",
                strip_name=None,
                unit_name="Bottle",
                strips_per_box=1,
                units_per_strip=1,
            ),
            mrp_unit=Decimal("75.00"),
            cost_unit=Decimal("60.00"),
            regulatory_class="PRESCRIPTION_ONLY",
            requires_cold_chain=False,
        )
        self._products[tofen.product_id] = tofen

        # 4. Insulin Pens & Cartridges: NovoRapid FlexPen (Cold-Chain)
        novorapid = MedicineProductFixture(
            product_id="prod-novorapid-pen-100",
            brand_name="NovoRapid FlexPen",
            generic_name="Insulin Aspart",
            dosage_form="Prefilled Pen",
            strength="100 U/ml (3ml)",
            manufacturer="Novo Nordisk / Transcom",
            packaging=PackagingUnitHierarchy(
                box_name="Box",
                strip_name=None,
                unit_name="Pen",
                strips_per_box=1,
                units_per_strip=5,  # Box of 5 pens
            ),
            mrp_unit=Decimal("850.00"),
            cost_unit=Decimal("720.00"),
            regulatory_class="PRESCRIPTION_ONLY",
            requires_cold_chain=True,
            min_temp_celsius=Decimal("2.0"),
            max_temp_celsius=Decimal("8.0"),
        )
        self._products[novorapid.product_id] = novorapid

        # 5. Injection Vials & Ampoules: Ceftron 1g IV Injection
        ceftron = MedicineProductFixture(
            product_id="prod-ceftron-1g-inj",
            brand_name="Ceftron 1g IV",
            generic_name="Ceftriaxone Sodium",
            dosage_form="Injection",
            strength="1g Powder + 10ml WFI",
            manufacturer="Square Pharmaceuticals PLC",
            packaging=PackagingUnitHierarchy(
                box_name="Pack",
                strip_name=None,
                unit_name="Vial+Ampoule",
                strips_per_box=1,
                units_per_strip=1,
            ),
            mrp_unit=Decimal("185.00"),
            cost_unit=Decimal("150.00"),
            regulatory_class="ANTIBIOTIC",
            requires_cold_chain=False,
        )
        self._products[ceftron.product_id] = ceftron

        # 6. Loose Bulk Pills Jar: Folison 5mg
        folison = MedicineProductFixture(
            product_id="prod-folison-5mg-jar",
            brand_name="Folison 5mg",
            generic_name="Folic Acid",
            dosage_form="Loose Tablet",
            strength="5mg",
            manufacturer="Jayson Pharmaceuticals Ltd.",
            packaging=PackagingUnitHierarchy(
                box_name="Dispensary Jar",
                strip_name=None,
                unit_name="Tablet",
                strips_per_box=1,
                units_per_strip=500,  # 500 loose pills in a jar
            ),
            mrp_unit=Decimal("0.50"),
            cost_unit=Decimal("0.35"),
            regulatory_class="OTC",
            requires_cold_chain=False,
        )
        self._products[folison.product_id] = folison

        # Multi-Branch Batches (Evergreen Pharma Group)
        tenant_id = "tenant-evergreen-pharma"
        branch_main = "branch-dhanmondi-01"
        branch_outlet = "branch-mirpur-02"

        batch_1 = BatchStockFixture(
            batch_id="batch-napa-001",
            product_id="prod-napa-extra-500",
            batch_number="BX-N2026-9",
            tenant_id=tenant_id,
            branch_id=branch_main,
            quantity_base_units=2400,  # 10 boxes
            expiry_date=datetime.date(2028, 6, 30),
            purchase_date=datetime.date(2026, 1, 15),
            cost_per_unit=Decimal("2.00"),
            status="ACTIVE",
        )
        self._batches.append(batch_1)

        batch_insulin = BatchStockFixture(
            batch_id="batch-novo-002",
            product_id="prod-novorapid-pen-100",
            batch_number="NN-INS-4401",
            tenant_id=tenant_id,
            branch_id=branch_main,
            quantity_base_units=25,  # 5 boxes of 5 pens
            expiry_date=datetime.date(2027, 3, 31),
            purchase_date=datetime.date(2026, 2, 10),
            cost_per_unit=Decimal("720.00"),
            status="ACTIVE",
        )
        self._batches.append(batch_insulin)

        # 7. Stock Transfer Order Fixture (Inter-branch transfer)
        sto = StockTransferOrderFixture(
            sto_id="STO-2026-0001",
            tenant_id=tenant_id,
            source_branch_id=branch_main,
            dest_branch_id=branch_outlet,
            transfer_date=datetime.date(2026, 4, 15),
            status="IN_TRANSIT",
            items=[
                {"product_id": "prod-napa-extra-500", "batch_number": "BX-N2026-9", "quantity_base_units": 480},
                {"product_id": "prod-novorapid-pen-100", "batch_number": "NN-INS-4401", "quantity_base_units": 5},
            ],
            transferred_by="Staff Kamrul Hasan",
        )
        self._stos.append(sto)

        # Temperature Logs
        temp_log_normal = TemperatureLogFixture(
            log_id="LOG-TEMP-101",
            tenant_id=tenant_id,
            branch_id=branch_main,
            storage_unit="BIOLOGICAL-REFRIGERATOR-01",
            recorded_temp=Decimal("4.5"),
            is_excursion=False,
            timestamp=datetime.datetime(2026, 4, 29, 8, 30, tzinfo=datetime.UTC),
            logged_by="Pharmacist Kazi Nazmul",
        )
        self._temperature_logs.append(temp_log_normal)

    def get_product(self, product_id: str) -> MedicineProductFixture:
        if product_id not in self._products:
            raise KeyError(f"Product '{product_id}' not found in canonical fixtures.")
        return self._products[product_id]

    def list_products(self) -> list[MedicineProductFixture]:
        return list(self._products.values())

    def list_batches(self) -> list[BatchStockFixture]:
        return list(self._batches)

    def list_stos(self) -> list[StockTransferOrderFixture]:
        return list(self._stos)

    def list_temperature_logs(self) -> list[TemperatureLogFixture]:
        return list(self._temperature_logs)

    def export_fixtures_dict(self) -> dict[str, Any]:
        """Exports canonical fixtures as a serializable dictionary for Django seeding or offline client databases."""
        return {
            "products": [
                {
                    "product_id": p.product_id,
                    "brand_name": p.brand_name,
                    "generic_name": p.generic_name,
                    "dosage_form": p.dosage_form,
                    "strength": p.strength,
                    "manufacturer": p.manufacturer,
                    "packaging": {
                        "box_name": p.packaging.box_name,
                        "strip_name": p.packaging.strip_name,
                        "unit_name": p.packaging.unit_name,
                        "strips_per_box": p.packaging.strips_per_box,
                        "units_per_strip": p.packaging.units_per_strip,
                        "total_units_per_box": p.packaging.total_units_per_box,
                    },
                    "mrp_unit": str(p.mrp_unit),
                    "cost_unit": str(p.cost_unit),
                    "regulatory_class": p.regulatory_class,
                    "requires_cold_chain": p.requires_cold_chain,
                    "min_temp_celsius": str(p.min_temp_celsius) if p.min_temp_celsius is not None else None,
                    "max_temp_celsius": str(p.max_temp_celsius) if p.max_temp_celsius is not None else None,
                }
                for p in self._products.values()
            ],
            "batches": [
                {
                    "batch_id": b.batch_id,
                    "product_id": b.product_id,
                    "batch_number": b.batch_number,
                    "tenant_id": b.tenant_id,
                    "branch_id": b.branch_id,
                    "quantity_base_units": b.quantity_base_units,
                    "expiry_date": b.expiry_date.isoformat(),
                    "purchase_date": b.purchase_date.isoformat(),
                    "cost_per_unit": str(b.cost_per_unit),
                    "status": b.status,
                }
                for b in self._batches
            ],
            "stock_transfer_orders": [
                {
                    "sto_id": s.sto_id,
                    "tenant_id": s.tenant_id,
                    "source_branch_id": s.source_branch_id,
                    "dest_branch_id": s.dest_branch_id,
                    "transfer_date": s.transfer_date.isoformat(),
                    "status": s.status,
                    "items": s.items,
                    "transferred_by": s.transferred_by,
                }
                for s in self._stos
            ],
            "temperature_logs": [
                {
                    "log_id": t.log_id,
                    "tenant_id": t.tenant_id,
                    "branch_id": t.branch_id,
                    "storage_unit": t.storage_unit,
                    "recorded_temp": str(t.recorded_temp),
                    "is_excursion": t.is_excursion,
                    "timestamp": t.timestamp.isoformat(),
                    "logged_by": t.logged_by,
                }
                for t in self._temperature_logs
            ],
        }

    def validate_product_fixture(self, product: MedicineProductFixture) -> None:
        """Validates product fixture schema, precision, and cold-chain consistency."""
        errors: dict[str, list[str]] = {}

        if not isinstance(product.mrp_unit, Decimal):
            errors["mrp_unit"] = [f"mrp_unit must be Decimal, got {type(product.mrp_unit).__name__}."]
        elif product.mrp_unit <= Decimal("0.00"):
            errors["mrp_unit"] = ["mrp_unit must be positive."]

        if not isinstance(product.cost_unit, Decimal):
            errors["cost_unit"] = [f"cost_unit must be Decimal, got {type(product.cost_unit).__name__}."]
        elif product.cost_unit < Decimal("0.00"):
            errors["cost_unit"] = ["cost_unit cannot be negative."]

        if product.packaging.strips_per_box < 1:
            errors["strips_per_box"] = ["strips_per_box must be at least 1."]
        if product.packaging.units_per_strip < 1:
            errors["units_per_strip"] = ["units_per_strip must be at least 1."]

        if product.requires_cold_chain:
            if product.min_temp_celsius is None or product.max_temp_celsius is None:
                errors["cold_chain_bounds"] = [
                    "Cold chain products must specify min_temp_celsius and max_temp_celsius."
                ]
            elif not isinstance(product.min_temp_celsius, Decimal) or not isinstance(product.max_temp_celsius, Decimal):
                errors["temp_precision"] = ["Temperature bounds must be Decimal, float is strictly forbidden."]
            elif product.min_temp_celsius >= product.max_temp_celsius:
                errors["temp_range"] = ["min_temp_celsius must be strictly less than max_temp_celsius."]

        if errors:
            raise FieldValidationError(
                message_en=f"Validation failed for product fixture '{product.product_id}': {list(errors.keys())}",
                message_bn=f"পণ্য ফিক্সচার '{product.product_id}'-এ ত্রুটি রয়েছে: {list(errors.keys())}",
                field_errors=errors,
            )

    def validate_stock_transfer(
        self,
        sto: StockTransferOrderFixture,
        source_branch_tenant: str,
        dest_branch_tenant: str,
    ) -> None:
        """
        Validates Inter-Branch Stock Transfer:
        1. Tenant boundaries: Both branches must belong to the identical tenant!
        2. Non-empty items list.
        3. Valid positive quantities.
        """
        if source_branch_tenant != sto.tenant_id or dest_branch_tenant != sto.tenant_id:
            raise PermissionError(
                f"Cross-tenant stock transfer strictly blocked! STO Tenant '{sto.tenant_id}', "
                f"Source Branch Tenant '{source_branch_tenant}', Destination Branch Tenant '{dest_branch_tenant}'."
            )

        if sto.source_branch_id == sto.dest_branch_id:
            raise FieldValidationError(
                message_en="Source and destination branches cannot be identical in Stock Transfer Order.",
                message_bn="স্টক স্থানান্তর আদেশে উৎস ও গন্তব্য শাখা একই হতে পারে না।",
                field_errors={"dest_branch_id": ["Source and destination branches must differ."]},
            )

        if not sto.items:
            raise FieldValidationError(
                message_en="Stock Transfer Order cannot have empty items list.",
                message_bn="স্টক স্থানান্তর আদেশে পণ্যের তালিকা খালি হতে পারে না।",
                field_errors={"items": ["Items list cannot be empty."]},
            )

        for idx, item in enumerate(sto.items):
            qty = item.get("quantity_base_units", 0)
            if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
                raise FieldValidationError(
                    message_en=f"Item at index {idx} has invalid quantity: {qty}. Must be positive integer.",
                    message_bn=f"সূচক {idx}-এর পণ্যের পরিমাণ অবৈধ: {qty}। ধনাত্মক পূর্ণসংখ্যা আবশ্যক।",
                    field_errors={"quantity_base_units": [f"Invalid quantity: {qty}"]},
                )

    def reconcile_stock_transfer(
        self,
        sto: StockTransferOrderFixture,
        source_inventory_base_units: int,
        dest_inventory_base_units: int,
        item_index: int = 0,
    ) -> dict[str, int]:
        """
        Calculates and verifies stock conservation during Inter-Branch STO receipt:
        Ensures strict inventory conservation: source_before + dest_before == source_after + dest_after.
        """
        if item_index >= len(sto.items):
            raise IndexError(f"Item index {item_index} out of bounds for STO {sto.sto_id}.")

        qty = sto.items[item_index]["quantity_base_units"]
        if source_inventory_base_units < qty:
            raise FieldValidationError(
                message_en=f"Insufficient source stock ({source_inventory_base_units}) to fulfill transfer quantity ({qty}).",
                message_bn=f"উৎস শাখায় পর্যাপ্ত স্টক নেই ({source_inventory_base_units}), স্থানান্তরের পরিমাণ ({qty})।",
                field_errors={"source_inventory": ["Insufficient stock for transfer."]},
            )

        new_source = source_inventory_base_units - qty
        new_dest = dest_inventory_base_units + qty

        # Invariant: Conservation of stock
        assert (source_inventory_base_units + dest_inventory_base_units) == (new_source + new_dest), (
            "Stock conservation invariant violated!"
        )

        return {
            "source_before": source_inventory_base_units,
            "source_after": new_source,
            "dest_before": dest_inventory_base_units,
            "dest_after": new_dest,
            "transferred_quantity": qty,
        }

    def validate_cold_chain_reading(
        self,
        log: TemperatureLogFixture,
        min_temp: Decimal = Decimal("2.0"),
        max_temp: Decimal = Decimal("8.0"),
    ) -> bool:
        """
        Evaluates cold-chain reading against biological safety bounds (2°C to 8°C).
        Returns True if within acceptable safe limits, False if excursion.
        """
        if not isinstance(log.recorded_temp, Decimal):
            raise FieldValidationError(
                message_en=f"Recorded temperature must be Decimal, got {type(log.recorded_temp).__name__}.",
                message_bn=f"রেকর্ডকৃত তাপমাত্রা অবশ্যই Decimal হতে হবে, {type(log.recorded_temp).__name__} গ্রহণযোগ্য নয়।",
                field_errors={"recorded_temp": ["Must be Decimal instance."]},
            )

        is_safe = min_temp <= log.recorded_temp <= max_temp
        expected_excursion = not is_safe

        if log.is_excursion != expected_excursion:
            raise FieldValidationError(
                message_en=f"Inconsistent excursion flag for reading {log.recorded_temp}°C. Expected is_excursion={expected_excursion}.",
                message_bn=f"তাপমাত্রা {log.recorded_temp}°C এর জন্য সীমা লঙ্ঘন ফ্ল্যাগ অসঙ্গতিপূর্ণ। প্রত্যাশিত {expected_excursion}।",
                field_errors={"is_excursion": [f"Expected {expected_excursion} for temp {log.recorded_temp}"]},
            )

        return is_safe
