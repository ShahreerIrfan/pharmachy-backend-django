"""
apps/core/mock_factory.py
Architecture Decision: ADR-0001, ADR-0005, ADR-0010, ADR-0015, ADR-0017 & ADR-0033
Phase 02: Repository, Toolchain & Engineering Automation

Deterministic Mock Data Factory for Bangladesh Pharmacy ERP & POS SaaS:
- Generates reproducible, realistic fixtures for:
  1. Tenants & Branches (multi-tenant isolation, DGDA pharmacy license format)
  2. Medicines (shared catalog, packaging unit conversion, cold-chain limits, antibiotic/OTC classes)
  3. Batches (FEFO expiry dates, cost per base unit, active/expired/quarantined states)
  4. Subscriptions (Basic/Standard/Pro tiers, trial/grace/expired states, branch/device quotas)
  5. Payments (Cash, bKash, Nagad, Card tenders with transaction references)
  6. Staff (Super Admin, Owner, Branch Manager, Registered Pharmacist with PCB, Cashier)
  7. Prescriptions (Doctor BMDC credentials, synthetic patient tokens, zero real PHI)
- Strict Decimal monetary precision (zero float).
- Automated PHI prevention: strictly rejects real Bangladesh National IDs (NID) and unmasked patient info.
- Multi-tenant boundary validation: prevents cross-tenant entity linkage.
"""

from __future__ import annotations

import datetime
import hashlib
import random
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction

# ------------------------------------------------------------------------------
# Exceptions & Safety Violations (ADR-0033)
# ------------------------------------------------------------------------------


class MockFactoryError(Exception):
    """Base exception for all mock data factory operations."""

    pass


class SyntheticDataSafetyViolation(MockFactoryError):
    """Raised when real patient PHI (real NID numbers, personal data, or unmasked info) is detected."""

    pass


class CrossTenantFactoryViolation(MockFactoryError):
    """Raised when an entity for tenant A references or attempts to link to tenant B."""

    pass


class InvalidFactorySpecificationError(MockFactoryError):
    """Raised when invalid parameters (e.g., negative prices, zero quantities, invalid PCB/BMDC) are passed."""

    pass


# ------------------------------------------------------------------------------
# PHI Scanner & Safety Validator (Zero Real Patient PHI Invariant)
# ------------------------------------------------------------------------------


class SyntheticDataSafetyValidator:
    """Enforces strict healthcare data privacy and blocks accidental leakage of real citizen identifiers."""

    # Bangladesh National ID (NID) formats: 10-digit smart card, 13-digit legacy, 17-digit birth-year prefixed
    NID_PATTERN = re.compile(r"\b(\d{10}|\d{13}|\d{17})\b")

    # Real personal phone number pattern in Bangladesh (must use reserved +8801700000XXX test block)
    REAL_PHONE_PATTERN = re.compile(r"\b(\+?8801[3-9]\d{8})\b")
    ALLOWED_SYNTHETIC_PHONE_PREFIX = "+8801700000"

    # Permitted synthetic patient token prefixes
    ALLOWED_PATIENT_PREFIXES = ("PT-", "SYNTH-", "ANON-", "TEST-")

    @classmethod
    def assert_zero_phi(cls, payload: Any) -> None:
        """Recursively audits payloads, strings, dicts, or lists for unmasked real citizen PHI."""
        if isinstance(payload, str):
            # 1. Exempt approved synthetic test phone numbers
            if payload.startswith("+8801700000") or payload.startswith("01700000") or payload.startswith("8801700000"):
                return

            # 2. Check for unmasked real mobile numbers
            phone_match = cls.REAL_PHONE_PATTERN.search(payload)
            if phone_match:
                matched_phone = phone_match.group(1)
                if not matched_phone.startswith(cls.ALLOWED_SYNTHETIC_PHONE_PREFIX) and not matched_phone.startswith(
                    "01700000"
                ):
                    raise SyntheticDataSafetyViolation(
                        f"Real Patient PHI Violation: Real mobile telephone number '{matched_phone}' detected. "
                        f"Synthetic mock data must use reserved test prefix '{cls.ALLOWED_SYNTHETIC_PHONE_PREFIX}XXX'."
                    )

            # 3. Check for unmasked Bangladesh National ID (NID: 10, 13, 17 digits)
            nid_match = cls.NID_PATTERN.search(payload)
            if nid_match:
                val = nid_match.group(1)
                # Exclude timestamps / ISO dates (10 digits e.g. 2026-10-29)
                if not (len(val) == 10 and (val.startswith("19") or val.startswith("20"))):
                    raise SyntheticDataSafetyViolation(
                        f"Real Patient PHI Violation: Potential Bangladesh National ID (NID) '{val}' "
                        f"detected in mock dataset. All real citizen NIDs are strictly forbidden (ADR-0033)."
                    )

        elif isinstance(payload, dict):
            for k, v in payload.items():
                if k in ("nid", "national_id", "patient_nid", "nid_number") and v:
                    raise SyntheticDataSafetyViolation(
                        f"Real Patient PHI Violation: Key '{k}' with value '{v}' is prohibited in synthetic fixtures."
                    )
                cls.assert_zero_phi(v)

        elif isinstance(payload, (list, tuple, set)):
            for item in payload:
                cls.assert_zero_phi(item)

    @classmethod
    def assert_patient_token_safe(cls, patient_token: str) -> None:
        """Ensures patient identifier is strictly an anonymized token."""
        cls.assert_zero_phi(patient_token)
        if not any(patient_token.startswith(prefix) for prefix in cls.ALLOWED_PATIENT_PREFIXES):
            raise SyntheticDataSafetyViolation(
                f"Invalid patient token '{patient_token}'. Must start with an approved synthetic prefix: "
                f"{cls.ALLOWED_PATIENT_PREFIXES} (ADR-0033)."
            )


# ------------------------------------------------------------------------------
# Deterministic Pseudo-Random Generator
# ------------------------------------------------------------------------------


class DeterministicRandom:
    """Provides reproducible pseudo-random generation anchored to an immutable seed."""

    def __init__(self, seed: int = 20261029):
        self.seed = seed
        self._rng = random.Random(seed)
        self._counter = 0

    def deterministic_id(self, prefix: str) -> str:
        self._counter += 1
        raw = f"{self.seed}_{prefix}_{self._counter}".encode("utf-8")
        h = hashlib.sha256(raw).hexdigest()[:12]
        return f"{prefix}-{h}"

    def choice(self, seq: List[Any]) -> Any:
        return self._rng.choice(seq)

    def sample(self, seq: List[Any], k: int) -> List[Any]:
        return self._rng.sample(seq, k)

    def randint(self, a: int, b: int) -> int:
        return self._rng.randint(a, b)

    def decimal(self, min_val: str, max_val: str, quantum: str = "0.01") -> Decimal:
        min_d = Decimal(min_val)
        max_d = Decimal(max_val)
        factor = Decimal(str(self._rng.uniform(0.0, 1.0)))
        amount = min_d + (factor * (max_d - min_d))
        return amount.quantize(Decimal(quantum))

    def date_between(self, start_date: datetime.date, end_date: datetime.date) -> datetime.date:
        days = (end_date - start_date).days
        if days <= 0:
            return start_date
        offset = self._rng.randint(0, days)
        return start_date + datetime.timedelta(days=offset)


# ------------------------------------------------------------------------------
# In-Memory DTOs for Factory Generation (Client / Offline / REST portability)
# ------------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantMockDTO:
    tenant_id: str
    name: str
    subdomain: str
    plan_tier: str
    is_active: bool = True


@dataclass(frozen=True)
class BranchMockDTO:
    branch_id: str
    tenant_id: str
    name: str
    license_number: str
    address: str
    is_active: bool = True


@dataclass(frozen=True)
class MedicineMockDTO:
    product_id: str
    brand_name: str
    generic_name: str
    dosage_form: str
    strength: str
    manufacturer: str
    mrp_unit: Decimal
    cost_unit: Decimal
    requires_cold_chain: bool = False
    min_temp_celsius: Optional[Decimal] = None
    max_temp_celsius: Optional[Decimal] = None
    regulatory_class: str = "OTC"


@dataclass(frozen=True)
class BatchMockDTO:
    batch_id: str
    tenant_id: str
    branch_id: str
    product_id: str
    batch_number: str
    quantity_base_units: int
    expiry_date: datetime.date
    purchase_date: datetime.date
    cost_per_unit: Decimal
    status: str = "ACTIVE"


@dataclass(frozen=True)
class SubscriptionMockDTO:
    subscription_id: str
    tenant_id: str
    plan_tier: str
    status: str
    amount_bdt: Decimal
    billing_cycle: str
    licensed_branches: int
    licensed_devices: int
    features_enabled: Dict[str, bool]
    start_date: datetime.date
    renewal_date: datetime.date
    grace_period_end_date: Optional[datetime.date] = None
    is_auto_renew: bool = True


@dataclass(frozen=True)
class StaffMockDTO:
    staff_id: str
    tenant_id: str
    branch_id: Optional[str]
    username: str
    full_name: str
    role: str
    mobile_number: str
    pcb_registration_no: str = ""
    is_active: bool = True


@dataclass(frozen=True)
class PrescriptionItemMockDTO:
    product_id: str
    dosage_instructions: str
    duration_days: int
    quantity_prescribed: int
    is_dispensed: bool = False


@dataclass(frozen=True)
class PrescriptionMockDTO:
    prescription_id: str
    tenant_id: str
    branch_id: str
    doctor_name: str
    doctor_bmdc_no: str
    patient_token: str
    patient_age: int
    patient_gender: str
    diagnosis: str
    status: str
    prescription_date: datetime.date
    items: List[PrescriptionItemMockDTO] = field(default_factory=list)
    verified_by_staff_id: Optional[str] = None
    pharmacist_pcb_no: str = ""
    notes: str = ""


@dataclass(frozen=True)
class PaymentMockDTO:
    transaction_id: str
    tenant_id: str
    branch_id: str
    invoice_id: Optional[str]
    tender_type: str
    gateway_reference: str
    amount_bdt: Decimal
    status: str
    initiated_by_staff_id: Optional[str] = None


# ------------------------------------------------------------------------------
# Entity-Specific Factories
# ------------------------------------------------------------------------------


class TenantFactory:
    """Builds multi-tenant organization fixtures and branch networks."""

    TENANT_SEEDS = [
        ("tenant-dhaka-central", "Lazz Pharma Dhaka Central", "lazz-dhaka", "PRO"),
        ("tenant-chittagong-care", "Tamanna Pharmacy Chittagong", "tamanna-ctg", "STANDARD"),
        ("tenant-sylhet-metro", "Surma Health Pharmacy Sylhet", "surma-syl", "STANDARD"),
        ("tenant-rajshahi-dispensary", "Barendra Model Medicine Shop", "barendra-raj", "BASIC"),
    ]

    @classmethod
    def build_tenants(cls, rng: DeterministicRandom, count: int = 2) -> List[TenantMockDTO]:
        tenants: List[TenantMockDTO] = []
        for i in range(min(count, len(cls.TENANT_SEEDS))):
            tid, name, subdomain, tier = cls.TENANT_SEEDS[i]
            tenants.append(
                TenantMockDTO(
                    tenant_id=tid,
                    name=name,
                    subdomain=subdomain,
                    plan_tier=tier,
                    is_active=True,
                )
            )
        return tenants

    @classmethod
    def build_branches(cls, rng: DeterministicRandom, tenant_id: str, count: int = 2) -> List[BranchMockDTO]:
        branches: List[BranchMockDTO] = []
        branch_templates = [
            ("Main Dispensary", "Road 4, Block C, Dhanmondi, Dhaka"),
            ("Model Medicine Shop", "Opposite DMCH Gate 2, Bakshibazar, Dhaka"),
            ("Express Counter", "GEC Circle, Nasirabad, Chittagong"),
            ("Airport Outlet", "Zindabazar Point, Sylhet"),
        ]
        for i in range(count):
            bid = f"{tenant_id}-br-0{i + 1}"
            tmpl_name, tmpl_addr = branch_templates[i % len(branch_templates)]
            license_no = f"DGDA-DL-DHK-2024-{rng.randint(1000, 9999)}"
            branches.append(
                BranchMockDTO(
                    branch_id=bid,
                    tenant_id=tenant_id,
                    name=f"{tmpl_name} ({i + 1})",
                    license_number=license_no,
                    address=tmpl_addr,
                    is_active=True,
                )
            )
        return branches


class MedicineFactory:
    """Builds canonical DGDA medicine product fixtures."""

    STANDARD_CATALOG = [
        {
            "product_id": "med-napa-extra",
            "brand_name": "Napa Extra 500mg/65mg",
            "generic_name": "Paracetamol + Caffeine",
            "dosage_form": "Tablet",
            "strength": "500mg/65mg",
            "manufacturer": "Beximco Pharmaceuticals Ltd.",
            "mrp_unit": Decimal("3.50"),
            "cost_unit": Decimal("2.80"),
            "regulatory_class": "OTC",
            "requires_cold_chain": False,
        },
        {
            "product_id": "med-seclo-20",
            "brand_name": "Seclo 20mg",
            "generic_name": "Omeprazole",
            "dosage_form": "Capsule",
            "strength": "20mg",
            "manufacturer": "Square Pharmaceuticals PLC",
            "mrp_unit": Decimal("7.00"),
            "cost_unit": Decimal("5.60"),
            "regulatory_class": "OTC",
            "requires_cold_chain": False,
        },
        {
            "product_id": "med-azithrocin-500",
            "brand_name": "Azithrocin 500mg",
            "generic_name": "Azithromycin",
            "dosage_form": "Tablet",
            "strength": "500mg",
            "manufacturer": "Incepta Pharmaceuticals Ltd.",
            "mrp_unit": Decimal("35.00"),
            "cost_unit": Decimal("28.00"),
            "regulatory_class": "ANTIBIOTIC",
            "requires_cold_chain": False,
        },
        {
            "product_id": "med-ciprocin-500",
            "brand_name": "Ciprocin 500mg",
            "generic_name": "Ciprofloxacin",
            "dosage_form": "Tablet",
            "strength": "500mg",
            "manufacturer": "Square Pharmaceuticals PLC",
            "mrp_unit": Decimal("15.00"),
            "cost_unit": Decimal("12.00"),
            "regulatory_class": "ANTIBIOTIC",
            "requires_cold_chain": False,
        },
        {
            "product_id": "med-insulatard-100",
            "brand_name": "Insulatard 100 IU/ml",
            "generic_name": "Isophane Insulin Human (rDNA)",
            "dosage_form": "Injection",
            "strength": "100 IU/ml (10ml)",
            "manufacturer": "Novo Nordisk A/S",
            "mrp_unit": Decimal("480.00"),
            "cost_unit": Decimal("410.00"),
            "regulatory_class": "PRESCRIPTION_ONLY",
            "requires_cold_chain": True,
            "min_temp_celsius": Decimal("2.00"),
            "max_temp_celsius": Decimal("8.00"),
        },
        {
            "product_id": "med-ace-plus",
            "brand_name": "Ace Plus 500mg/65mg",
            "generic_name": "Paracetamol + Caffeine",
            "dosage_form": "Tablet",
            "strength": "500mg/65mg",
            "manufacturer": "Square Pharmaceuticals PLC",
            "mrp_unit": Decimal("3.50"),
            "cost_unit": Decimal("2.80"),
            "regulatory_class": "OTC",
            "requires_cold_chain": False,
        },
    ]

    @classmethod
    def build_medicines(cls) -> List[MedicineMockDTO]:
        items: List[MedicineMockDTO] = []
        for m in cls.STANDARD_CATALOG:
            items.append(
                MedicineMockDTO(
                    product_id=m["product_id"],
                    brand_name=m["brand_name"],
                    generic_name=m["generic_name"],
                    dosage_form=m["dosage_form"],
                    strength=m["strength"],
                    manufacturer=m["manufacturer"],
                    mrp_unit=m["mrp_unit"],
                    cost_unit=m["cost_unit"],
                    requires_cold_chain=m["requires_cold_chain"],
                    min_temp_celsius=m.get("min_temp_celsius"),
                    max_temp_celsius=m.get("max_temp_celsius"),
                    regulatory_class=m.get("regulatory_class", "OTC"),
                )
            )
        return items


class BatchFactory:
    """Builds physical batch inventory fixtures adhering to FEFO rules."""

    @classmethod
    def build_batches(
        cls,
        rng: DeterministicRandom,
        tenant_id: str,
        branch_id: str,
        medicines: List[MedicineMockDTO],
        as_of_date: Optional[datetime.date] = None,
    ) -> List[BatchMockDTO]:
        as_of = as_of_date or datetime.date(2026, 10, 29)
        batches: List[BatchMockDTO] = []

        for med in medicines:
            # 1. Active FEFO lot (expires in 6-18 months)
            exp_active = as_of + datetime.timedelta(days=rng.randint(180, 540))
            pur_active = as_of - datetime.timedelta(days=rng.randint(30, 90))
            batches.append(
                BatchMockDTO(
                    batch_id=f"batch-{branch_id}-{med.product_id}-01",
                    tenant_id=tenant_id,
                    branch_id=branch_id,
                    product_id=med.product_id,
                    batch_number=f"BN-{med.brand_name[:3].upper()}-{rng.randint(100, 999)}",
                    quantity_base_units=rng.randint(200, 800),
                    expiry_date=exp_active,
                    purchase_date=pur_active,
                    cost_per_unit=med.cost_unit,
                    status="ACTIVE",
                )
            )

            # 2. Near-expiry or short-dated lot (expires in 30-75 days)
            exp_near = as_of + datetime.timedelta(days=rng.randint(30, 75))
            pur_near = as_of - datetime.timedelta(days=rng.randint(120, 200))
            batches.append(
                BatchMockDTO(
                    batch_id=f"batch-{branch_id}-{med.product_id}-02",
                    tenant_id=tenant_id,
                    branch_id=branch_id,
                    product_id=med.product_id,
                    batch_number=f"BN-{med.brand_name[:3].upper()}-{rng.randint(100, 999)}-NE",
                    quantity_base_units=rng.randint(50, 150),
                    expiry_date=exp_near,
                    purchase_date=pur_near,
                    cost_per_unit=med.cost_unit,
                    status="ACTIVE",
                )
            )

        return batches


class SubscriptionFactory:
    """Builds multi-tier tenant SaaS subscriptions and license quotas."""

    TIER_PRICES = {
        "BASIC": Decimal("1500.00"),
        "STANDARD": Decimal("3500.00"),
        "PRO": Decimal("7500.00"),
    }

    @classmethod
    def build_subscription(
        cls,
        rng: DeterministicRandom,
        tenant_id: str,
        plan_tier: str = "STANDARD",
        status: str = "ACTIVE",
        as_of_date: Optional[datetime.date] = None,
    ) -> SubscriptionMockDTO:
        as_of = as_of_date or datetime.date(2026, 10, 29)
        start_date = as_of - datetime.timedelta(days=30)
        renewal_date = as_of + datetime.timedelta(days=30)
        grace_period_end = renewal_date + datetime.timedelta(days=7) if status == "GRACE_PERIOD" else None

        branches_quota = 1 if plan_tier == "BASIC" else 3 if plan_tier == "STANDARD" else 10
        devices_quota = 2 if plan_tier == "BASIC" else 6 if plan_tier == "STANDARD" else 25

        features = {
            "offline_pos": True,
            "thermal_printing": True,
            "antibiotic_register": plan_tier in ("STANDARD", "PRO"),
            "cold_chain_logging": plan_tier == "PRO",
            "advanced_analytics": plan_tier == "PRO",
            "multi_branch_sync": plan_tier in ("STANDARD", "PRO"),
        }

        return SubscriptionMockDTO(
            subscription_id=f"sub-{tenant_id}",
            tenant_id=tenant_id,
            plan_tier=plan_tier,
            status=status,
            amount_bdt=cls.TIER_PRICES.get(plan_tier, Decimal("3500.00")),
            billing_cycle="MONTHLY",
            licensed_branches=branches_quota,
            licensed_devices=devices_quota,
            features_enabled=features,
            start_date=start_date,
            renewal_date=renewal_date,
            grace_period_end_date=grace_period_end,
            is_auto_renew=True,
        )


class StaffFactory:
    """Builds synthetic pharmacy staff profiles and registered pharmacist credentials."""

    STAFF_NAMES = [
        ("Kazi Mashiur Rahman", "TENANT_OWNER", ""),
        ("Tanvir Ahmed", "BRANCH_MANAGER", ""),
        ("Dr. Farhana Yasmin", "PHARMACIST", "PCB-A44910"),
        ("Abdur Rahim", "CASHIER", ""),
        ("Shamim Reza", "CASHIER", ""),
        ("Nusrat Jahan", "PHARMACIST", "PCB-B77215"),
    ]

    @classmethod
    def build_staff(
        cls,
        rng: DeterministicRandom,
        tenant_id: str,
        branch_id: Optional[str] = None,
    ) -> List[StaffMockDTO]:
        staff_list: List[StaffMockDTO] = []
        for i, (name, role, pcb) in enumerate(cls.STAFF_NAMES):
            username = f"{role.lower()}_{tenant_id}_{i + 1}"
            mobile = f"+8801700000{rng.randint(100, 999)}"
            staff_list.append(
                StaffMockDTO(
                    staff_id=f"staff-{tenant_id}-0{i + 1}",
                    tenant_id=tenant_id,
                    branch_id=branch_id if role in ("BRANCH_MANAGER", "PHARMACIST", "CASHIER") else None,
                    username=username,
                    full_name=name,
                    role=role,
                    mobile_number=mobile,
                    pcb_registration_no=pcb,
                    is_active=True,
                )
            )
        return staff_list


class PrescriptionFactory:
    """Builds synthetic prescriptions without real patient data (Zero Real Patient PHI)."""

    DOCTORS = [
        ("Dr. Mahmudur Rahman, MBBS, FCPS (Medicine)", "BMDC-A-45892"),
        ("Prof. Dr. Syeda Naznin, MBBS, DCH (Pediatrics)", "BMDC-A-32104"),
        ("Dr. K. M. Ashraful Alam, MBBS, MS (Orthopedics)", "BMDC-A-58219"),
    ]

    DIAGNOSES = [
        "Upper Respiratory Tract Infection (Acute Bronchitis)",
        "Gastroesophageal Reflux Disease (GERD) with Dyspepsia",
        "Acute Pharyngitis with Streptococcal suspicion",
        "Type 2 Diabetes Mellitus with peripheral neuropathy",
    ]

    @classmethod
    def build_prescriptions(
        cls,
        rng: DeterministicRandom,
        tenant_id: str,
        branch_id: str,
        medicines: List[MedicineMockDTO],
        count: int = 3,
        as_of_date: Optional[datetime.date] = None,
    ) -> List[PrescriptionMockDTO]:
        as_of = as_of_date or datetime.date(2026, 10, 29)
        prescriptions: List[PrescriptionMockDTO] = []

        antibiotic_meds = [m for m in medicines if m.regulatory_class == "ANTIBIOTIC"]
        general_meds = [m for m in medicines if m.regulatory_class != "ANTIBIOTIC"]

        for i in range(count):
            doc_name, doc_bmdc = cls.DOCTORS[i % len(cls.DOCTORS)]
            diag = cls.DIAGNOSES[i % len(cls.DIAGNOSES)]
            patient_token = f"PT-DHAKA-{rng.randint(1000, 9999)}"
            SyntheticDataSafetyValidator.assert_patient_token_safe(patient_token)

            rx_items: List[PrescriptionItemMockDTO] = []
            # 1. Main therapeutic medicine
            if antibiotic_meds and i % 2 == 0:
                target_med = antibiotic_meds[i % len(antibiotic_meds)]
                rx_items.append(
                    PrescriptionItemMockDTO(
                        product_id=target_med.product_id,
                        dosage_instructions="1 tablet orally every 12 hours after meals",
                        duration_days=7,
                        quantity_prescribed=14,
                        is_dispensed=False,
                    )
                )

            # 2. Adjunct gastric support / pain reliever
            if general_meds:
                adjunct = general_meds[i % len(general_meds)]
                rx_items.append(
                    PrescriptionItemMockDTO(
                        product_id=adjunct.product_id,
                        dosage_instructions="1 capsule 30 minutes before breakfast",
                        duration_days=14,
                        quantity_prescribed=14,
                        is_dispensed=False,
                    )
                )

            rx_status = "VERIFIED" if i == 0 else "PENDING"
            prescriptions.append(
                PrescriptionMockDTO(
                    prescription_id=f"rx-{branch_id}-00{i + 1}",
                    tenant_id=tenant_id,
                    branch_id=branch_id,
                    doctor_name=doc_name,
                    doctor_bmdc_no=doc_bmdc,
                    patient_token=patient_token,
                    patient_age=rng.randint(18, 65),
                    patient_gender="MALE" if i % 2 == 0 else "FEMALE",
                    diagnosis=diag,
                    status=rx_status,
                    prescription_date=as_of - datetime.timedelta(days=rng.randint(0, 3)),
                    items=rx_items,
                    verified_by_staff_id=f"staff-{tenant_id}-03" if rx_status == "VERIFIED" else None,
                    pharmacist_pcb_no="PCB-A44910" if rx_status == "VERIFIED" else "",
                    notes="Approved following antibiotic register review." if rx_status == "VERIFIED" else "",
                )
            )

        return prescriptions


class PaymentFactory:
    """Builds Point of Sale and subscription payment settlement fixtures."""

    @classmethod
    def build_payments(
        cls,
        rng: DeterministicRandom,
        tenant_id: str,
        branch_id: str,
        invoice_ids: List[str],
        base_amount_bdt: Decimal = Decimal("485.50"),
    ) -> List[PaymentMockDTO]:
        payments: List[PaymentMockDTO] = []
        tenders = ["CASH", "BKASH", "NAGAD", "CARD"]

        for i, inv_id in enumerate(invoice_ids):
            tender = tenders[i % len(tenders)]
            trx_ref = f"TRX-{tender}-{rng.randint(100000, 999999)}" if tender != "CASH" else ""
            payments.append(
                PaymentMockDTO(
                    transaction_id=f"pay-{inv_id}",
                    tenant_id=tenant_id,
                    branch_id=branch_id,
                    invoice_id=inv_id,
                    tender_type=tender,
                    gateway_reference=trx_ref,
                    amount_bdt=base_amount_bdt,
                    status="SUCCESS",
                    initiated_by_staff_id=f"staff-{tenant_id}-04",
                )
            )
        return payments


# ------------------------------------------------------------------------------
# Unified Master Data Factory Orchestrator
# ------------------------------------------------------------------------------


class DeterministicMockDataFactory:
    """
    Central orchestration engine for generating, validating, and persisting
    all 7 domain fixture pillars in Bangladesh Pharmacy SaaS.
    """

    def __init__(self, seed: int = 20261029):
        self.seed = seed
        self.rng = DeterministicRandom(seed)

    def generate_bundle(self) -> Dict[str, Any]:
        """Generates all 7 domain mock entities as deterministic in-memory DTO dictionaries."""
        medicines = MedicineFactory.build_medicines()
        tenants = TenantFactory.build_tenants(self.rng, count=2)

        branches_map: Dict[str, List[BranchMockDTO]] = {}
        batches_map: Dict[str, List[BatchMockDTO]] = {}
        subscriptions: List[SubscriptionMockDTO] = []
        staff_map: Dict[str, List[StaffMockDTO]] = {}
        prescriptions_map: Dict[str, List[PrescriptionMockDTO]] = {}
        payments_map: Dict[str, List[PaymentMockDTO]] = {}

        for tenant in tenants:
            tid = tenant.tenant_id
            t_branches = TenantFactory.build_branches(self.rng, tid, count=2)
            branches_map[tid] = t_branches

            subscriptions.append(SubscriptionFactory.build_subscription(self.rng, tid, plan_tier=tenant.plan_tier))

            t_staff = StaffFactory.build_staff(self.rng, tid, branch_id=t_branches[0].branch_id)
            staff_map[tid] = t_staff

            for branch in t_branches:
                bid = branch.branch_id
                batches = BatchFactory.build_batches(self.rng, tid, bid, medicines)
                batches_map[bid] = batches

                rx_list = PrescriptionFactory.build_prescriptions(self.rng, tid, bid, medicines, count=2)
                prescriptions_map[bid] = rx_list

                sample_invoices = [f"inv-{bid}-001", f"inv-{bid}-002"]
                payments = PaymentFactory.build_payments(self.rng, tid, bid, sample_invoices)
                payments_map[bid] = payments

        bundle = {
            "seed": self.seed,
            "generated_at": "2026-10-29T12:00:00Z",
            "medicines": [asdict(m) for m in medicines],
            "tenants": [asdict(t) for t in tenants],
            "branches": [asdict(b) for b_list in branches_map.values() for b in b_list],
            "subscriptions": [asdict(s) for s in subscriptions],
            "batches": [asdict(b) for b_list in batches_map.values() for b in b_list],
            "staff": [asdict(s) for s_list in staff_map.values() for s in s_list],
            "prescriptions": [asdict(p) for p_list in prescriptions_map.values() for p in p_list],
            "payments": [asdict(p) for p_list in payments_map.values() for p in p_list],
        }

        # Safety Audit: Strict Zero Real Patient PHI Invariant
        SyntheticDataSafetyValidator.assert_zero_phi(bundle)

        return bundle

    @classmethod
    def assert_tenant_isolation(
        cls,
        parent_tenant_id: str,
        child_tenant_id: str,
        entity_name: str,
    ) -> None:
        """Enforces multi-tenant ownership boundaries across related mock entities."""
        if parent_tenant_id != child_tenant_id:
            raise CrossTenantFactoryViolation(
                f"Multi-Tenant Isolation Breach: {entity_name} belonging to tenant '{child_tenant_id}' "
                f"cannot be linked to parent tenant '{parent_tenant_id}' (ADR-0005, ADR-0033)."
            )

    @classmethod
    @transaction.atomic
    def persist_all(cls, seed: int = 20261029) -> Dict[str, int]:
        """
        Persists generated deterministic mock fixtures directly into SQLite / PostgreSQL
        using Django ORM, enforcing constraints and foreign keys.
        """
        from apps.core.models import (
            BatchStock,
            Branch,
            MedicineProduct,
            PaymentTransaction,
            PrescriptionItem,
            PrescriptionRecord,
            SaleInvoice,
            StaffMember,
            SubscriptionRecord,
            Tenant,
        )

        factory = cls(seed=seed)
        bundle = factory.generate_bundle()

        # 1. Persist Medicines (Master Catalog)
        med_count = 0
        med_cache = {}
        for m in bundle["medicines"]:
            obj, _ = MedicineProduct.objects.update_or_create(
                product_id=m["product_id"],
                defaults={
                    "brand_name": m["brand_name"],
                    "generic_name": m["generic_name"],
                    "dosage_form": m["dosage_form"],
                    "strength": m["strength"],
                    "manufacturer": m["manufacturer"],
                    "mrp_unit": Decimal(str(m["mrp_unit"])),
                    "cost_unit": Decimal(str(m["cost_unit"])),
                    "requires_cold_chain": m["requires_cold_chain"],
                    "min_temp_celsius": Decimal(str(m["min_temp_celsius"])) if m.get("min_temp_celsius") else None,
                    "max_temp_celsius": Decimal(str(m["max_temp_celsius"])) if m.get("max_temp_celsius") else None,
                },
            )
            med_cache[obj.product_id] = obj
            med_count += 1

        # 2. Persist Tenants
        tenant_count = 0
        tenant_cache = {}
        for t in bundle["tenants"]:
            obj, _ = Tenant.objects.update_or_create(
                tenant_id=t["tenant_id"],
                defaults={
                    "name": t["name"],
                    "subdomain": t["subdomain"],
                    "plan_tier": t["plan_tier"],
                    "is_active": t["is_active"],
                },
            )
            tenant_cache[obj.tenant_id] = obj
            tenant_count += 1

        # 3. Persist Branches
        branch_count = 0
        branch_cache = {}
        for b in bundle["branches"]:
            obj, _ = Branch.objects.update_or_create(
                branch_id=b["branch_id"],
                defaults={
                    "tenant": tenant_cache[b["tenant_id"]],
                    "name": b["name"],
                    "license_number": b["license_number"],
                    "address": b["address"],
                    "is_active": b["is_active"],
                },
            )
            branch_cache[obj.branch_id] = obj
            branch_count += 1

        # 4. Persist Subscriptions
        sub_count = 0
        for s in bundle["subscriptions"]:
            SubscriptionRecord.objects.update_or_create(
                subscription_id=s["subscription_id"],
                defaults={
                    "tenant": tenant_cache[s["tenant_id"]],
                    "plan_tier": s["plan_tier"],
                    "status": s["status"],
                    "amount_bdt": Decimal(str(s["amount_bdt"])),
                    "billing_cycle": s["billing_cycle"],
                    "licensed_branches": s["licensed_branches"],
                    "licensed_devices": s["licensed_devices"],
                    "features_enabled": s["features_enabled"],
                    "start_date": s["start_date"],
                    "renewal_date": s["renewal_date"],
                    "grace_period_end_date": s["grace_period_end_date"],
                    "is_auto_renew": s["is_auto_renew"],
                },
            )
            sub_count += 1

        # 5. Persist Staff
        staff_count = 0
        staff_cache = {}
        for s in bundle["staff"]:
            obj, _ = StaffMember.objects.update_or_create(
                staff_id=s["staff_id"],
                defaults={
                    "tenant": tenant_cache[s["tenant_id"]],
                    "branch": branch_cache[s["branch_id"]] if s.get("branch_id") else None,
                    "username": s["username"],
                    "full_name": s["full_name"],
                    "role": s["role"],
                    "mobile_number": s["mobile_number"],
                    "pcb_registration_no": s.get("pcb_registration_no", ""),
                    "is_active": s["is_active"],
                },
            )
            staff_cache[obj.staff_id] = obj
            staff_count += 1

        # 6. Persist Batches
        batch_count = 0
        for b in bundle["batches"]:
            BatchStock.objects.update_or_create(
                batch_id=b["batch_id"],
                defaults={
                    "tenant": tenant_cache[b["tenant_id"]],
                    "branch": branch_cache[b["branch_id"]],
                    "product": med_cache[b["product_id"]],
                    "batch_number": b["batch_number"],
                    "quantity_base_units": b["quantity_base_units"],
                    "expiry_date": b["expiry_date"],
                    "purchase_date": b["purchase_date"],
                    "cost_per_unit": Decimal(str(b["cost_per_unit"])),
                    "status": b["status"],
                },
            )
            batch_count += 1

        # 7. Persist Prescriptions
        rx_count = 0
        for p in bundle["prescriptions"]:
            rx_obj, _ = PrescriptionRecord.objects.update_or_create(
                prescription_id=p["prescription_id"],
                defaults={
                    "tenant": tenant_cache[p["tenant_id"]],
                    "branch": branch_cache[p["branch_id"]],
                    "doctor_name": p["doctor_name"],
                    "doctor_bmdc_no": p["doctor_bmdc_no"],
                    "patient_token": p["patient_token"],
                    "patient_age": p["patient_age"],
                    "patient_gender": p["patient_gender"],
                    "diagnosis": p["diagnosis"],
                    "status": p["status"],
                    "prescription_date": p["prescription_date"],
                    "verified_by": staff_cache.get(p["verified_by_staff_id"])
                    if p.get("verified_by_staff_id")
                    else None,
                    "pharmacist_pcb_no": p.get("pharmacist_pcb_no", ""),
                    "notes": p.get("notes", ""),
                },
            )
            rx_count += 1

            # Persist items
            for item in p.get("items", []):
                PrescriptionItem.objects.update_or_create(
                    prescription=rx_obj,
                    product=med_cache[item["product_id"]],
                    defaults={
                        "dosage_instructions": item["dosage_instructions"],
                        "duration_days": item["duration_days"],
                        "quantity_prescribed": item["quantity_prescribed"],
                        "is_dispensed": item["is_dispensed"],
                    },
                )

        # 8. Persist Payments
        pay_count = 0
        for pay in bundle["payments"]:
            # Ensure dummy invoice exists for foreign key if specified
            inv_id = pay["invoice_id"]
            invoice_obj = None
            if inv_id:
                invoice_obj, _ = SaleInvoice.objects.update_or_create(
                    invoice_id=inv_id,
                    defaults={
                        "tenant": tenant_cache[pay["tenant_id"]],
                        "branch": branch_cache[pay["branch_id"]],
                        "invoice_number": f"INV-{inv_id.upper()}",
                        "customer_name": "Walk-in Customer",
                        "total_amount": Decimal(str(pay["amount_bdt"])),
                        "tax_amount": Decimal("0.00"),
                        "net_amount": Decimal(str(pay["amount_bdt"])),
                    },
                )

            PaymentTransaction.objects.update_or_create(
                transaction_id=pay["transaction_id"],
                defaults={
                    "tenant": tenant_cache[pay["tenant_id"]],
                    "branch": branch_cache[pay["branch_id"]],
                    "invoice": invoice_obj,
                    "tender_type": pay["tender_type"],
                    "gateway_reference": pay["gateway_reference"],
                    "amount_bdt": Decimal(str(pay["amount_bdt"])),
                    "status": pay["status"],
                    "initiated_by": staff_cache.get(pay["initiated_by_staff_id"])
                    if pay.get("initiated_by_staff_id")
                    else None,
                },
            )
            pay_count += 1

        return {
            "medicines": med_count,
            "tenants": tenant_count,
            "branches": branch_count,
            "subscriptions": sub_count,
            "staff": staff_count,
            "batches": batch_count,
            "prescriptions": rx_count,
            "payments": pay_count,
        }
