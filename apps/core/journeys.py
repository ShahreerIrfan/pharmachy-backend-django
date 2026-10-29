"""
UX User Journeys & Interaction Paths Engine
Architecture Decision: ADR-0018
Maps the 6 canonical pharmacy SaaS user journeys: Owner Onboarding, Cashier Checkout (with Offline variant),
Pharmacist Clinical Verification, GRN Receipt, Expense Approval, and Super-Admin Support.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from apps.core.errors import FieldValidationError


class JourneyId(str, Enum):
    OWNER_ONBOARDING = "J-01-OWNER-ONBOARDING"
    CASHIER_CHECKOUT = "J-02-CASHIER-CHECKOUT"
    PHARMACIST_VERIFICATION = "J-03-PHARMACIST-VERIFICATION"
    GRN_RECEIPT = "J-04-GRN-RECEIPT"
    EXPENSE_APPROVAL = "J-05-EXPENSE-APPROVAL"
    SUPER_ADMIN_SUPPORT = "J-06-SUPER-ADMIN-SUPPORT"


@dataclass(frozen=True)
class JourneyStep:
    step_id: str
    name_en: str
    name_bn: str
    allowed_roles: set[str]
    is_offline_capable: bool
    required_inputs: list[str]


@dataclass(frozen=True)
class JourneyDefinition:
    journey_id: JourneyId
    name_en: str
    name_bn: str
    primary_role: str
    steps: list[JourneyStep]


@dataclass
class JourneyExecutionState:
    journey_id: JourneyId
    tenant_id: str
    actor_role: str
    actor_id: str
    is_offline: bool
    current_step_index: int = 0
    step_data: dict[str, Any] = field(default_factory=dict)
    completed_steps: list[str] = field(default_factory=list)
    status: str = "ACTIVE"  # ACTIVE, COMPLETED, FAILED


class UserJourneyEngine:
    """Canonical registry and execution validator for SaaS user journeys."""

    def __init__(self):
        self._definitions: dict[JourneyId, JourneyDefinition] = {}
        self._register_canonical_journeys()

    def _register_canonical_journeys(self) -> None:
        # 1. Owner Onboarding
        self._definitions[JourneyId.OWNER_ONBOARDING] = JourneyDefinition(
            journey_id=JourneyId.OWNER_ONBOARDING,
            name_en="Tenant Owner Onboarding",
            name_bn="মালিকের অনবোর্ডিং প্রক্রিয়া",
            primary_role="TENANT_OWNER",
            steps=[
                JourneyStep(
                    "STEP-01-PHONE-OTP",
                    "Verify Phone & OTP",
                    "ফোন নম্বর ও ওটিপি যাচাই",
                    {"TENANT_OWNER"},
                    False,
                    ["phone_number", "otp_code"],
                ),
                JourneyStep(
                    "STEP-02-LEGAL-PROFILE",
                    "Enter Drug License & TIN",
                    "ড্রাগ লাইসেন্স ও টিআইএন এন্ট্রি",
                    {"TENANT_OWNER"},
                    False,
                    ["drug_license_no", "tin_number"],
                ),
                JourneyStep(
                    "STEP-03-BRANCH-SETUP",
                    "Configure First Branch",
                    "প্রথম শাখা কনফিগারেশন",
                    {"TENANT_OWNER"},
                    False,
                    ["branch_name", "address"],
                ),
                JourneyStep(
                    "STEP-04-PLAN-SELECT",
                    "Select Subscription Plan",
                    "সাবস্ক্রিপশন প্ল্যান নির্বাচন",
                    {"TENANT_OWNER"},
                    False,
                    ["plan_tier"],
                ),
            ],
        )

        # 2. Cashier POS Checkout (Supports Offline Mode)
        self._definitions[JourneyId.CASHIER_CHECKOUT] = JourneyDefinition(
            journey_id=JourneyId.CASHIER_CHECKOUT,
            name_en="Cashier POS Checkout",
            name_bn="ক্যাশিয়ার পিওএস বিক্রয় প্রক্রিয়া",
            primary_role="CASHIER",
            steps=[
                JourneyStep(
                    "STEP-01-ITEM-LOOKUP",
                    "Scan Barcode / Select Drug",
                    "বারকোড স্ক্যান / ওষুধ নির্বাচন",
                    {"CASHIER", "BRANCH_MANAGER"},
                    True,
                    ["product_id", "quantity", "unit_type"],
                ),
                JourneyStep(
                    "STEP-02-RX-CHECK",
                    "Check Prescription Requirement",
                    "প্রেসক্রিপশন প্রয়োজনীয়তা যাচাই",
                    {"CASHIER", "BRANCH_MANAGER"},
                    True,
                    ["is_rx_required"],
                ),
                JourneyStep(
                    "STEP-03-TENDER-PAYMENT",
                    "Collect Payment & Tender",
                    "পেমেন্ট গ্রহণ",
                    {"CASHIER", "BRANCH_MANAGER"},
                    True,
                    ["tender_type", "amount_paid"],
                ),
                JourneyStep(
                    "STEP-04-PRINT-RECEIPT",
                    "Emit Receipt & Ledger Post",
                    "রসিদ প্রদান ও হিসাব হালনাগাদ",
                    {"CASHIER", "BRANCH_MANAGER"},
                    True,
                    ["receipt_format"],
                ),
            ],
        )

        # 3. Pharmacist Clinical Verification
        self._definitions[JourneyId.PHARMACIST_VERIFICATION] = JourneyDefinition(
            journey_id=JourneyId.PHARMACIST_VERIFICATION,
            name_en="Pharmacist Clinical Verification",
            name_bn="ফার্মাসিস্ট ক্লিনিক্যাল যাচাইকরণ",
            primary_role="PHARMACIST",
            steps=[
                JourneyStep(
                    "STEP-01-RX-INGEST",
                    "Ingest Prescription Scan",
                    "প্রেসক্রিপশন স্ক্যান আপলোড",
                    {"PHARMACIST"},
                    False,
                    ["rx_image_ref", "patient_name"],
                ),
                JourneyStep(
                    "STEP-02-BMDC-VERIFY",
                    "Verify Prescribing Doctor BMDC",
                    "চিকিৎসকের বিএমডিসি সনদ যাচাই",
                    {"PHARMACIST"},
                    False,
                    ["doctor_bmdc_no", "doctor_name"],
                ),
                JourneyStep(
                    "STEP-03-CLINICAL-SIGNOFF",
                    "Digital Sign-Off with PCB License",
                    "ফার্মাসিস্ট পিসিবি লাইসেন্স অনুমোদন",
                    {"PHARMACIST"},
                    False,
                    ["pharmacist_pcb_no", "clinical_decision"],
                ),
            ],
        )

        # 4. GRN Supplier Goods Receipt
        self._definitions[JourneyId.GRN_RECEIPT] = JourneyDefinition(
            journey_id=JourneyId.GRN_RECEIPT,
            name_en="GRN Goods Receipt",
            name_bn="মালামাল গ্রহণ ও চালান যাচাই",
            primary_role="INVENTORY_STAFF",
            steps=[
                JourneyStep(
                    "STEP-01-PO-MATCH",
                    "Match Purchase Order & Challan",
                    "পিও এবং চালান যাচাই",
                    {"INVENTORY_STAFF", "BRANCH_MANAGER"},
                    False,
                    ["po_number", "challan_number"],
                ),
                JourneyStep(
                    "STEP-02-BATCH-INSPECT",
                    "Inspect Physical Batches & Expiry",
                    "শারীরিক ব্যাচ ও মেয়াদ পরিদর্শন",
                    {"INVENTORY_STAFF", "BRANCH_MANAGER"},
                    False,
                    ["batch_number", "expiry_date", "received_cost"],
                ),
                JourneyStep(
                    "STEP-03-LEDGER-POST",
                    "Credit Stock Ledger",
                    "স্টক লেজার ক্রেডিট ও সমাপ্তি",
                    {"INVENTORY_STAFF", "BRANCH_MANAGER"},
                    False,
                    ["storage_location"],
                ),
            ],
        )

        # 5. Shift Expense Approval
        self._definitions[JourneyId.EXPENSE_APPROVAL] = JourneyDefinition(
            journey_id=JourneyId.EXPENSE_APPROVAL,
            name_en="Shift Expense Approval",
            name_bn="শিফট খরচ অনুমোদন",
            primary_role="CASHIER",
            steps=[
                JourneyStep(
                    "STEP-01-REQUEST-EXPENSE",
                    "Initiate Petty Cash Request",
                    "খুচরা খরচ প্রস্তাব",
                    {"CASHIER"},
                    True,
                    ["category", "amount", "reason"],
                ),
                JourneyStep(
                    "STEP-02-MANAGER-APPROVAL",
                    "Manager Authorizes & Debits Float",
                    "ম্যানেজার অনুমোদন ও ক্যাশ ডেবিট",
                    {"BRANCH_MANAGER", "TENANT_OWNER"},
                    True,
                    ["approved", "manager_auth_code"],
                ),
            ],
        )

        # 6. Super-Admin Support Session
        self._definitions[JourneyId.SUPER_ADMIN_SUPPORT] = JourneyDefinition(
            journey_id=JourneyId.SUPER_ADMIN_SUPPORT,
            name_en="Super-Admin Support Session",
            name_bn="সুপার-এডমিন সাপোর্ট সেশন",
            primary_role="PLATFORM_SUPER_ADMIN",
            steps=[
                JourneyStep(
                    "STEP-01-SUPPORT-TOKEN",
                    "Generate Diagnostic Token",
                    "ডায়াগনস্টিক টোকেন তৈরি",
                    {"PLATFORM_SUPER_ADMIN"},
                    False,
                    ["support_ticket_id", "tenant_id"],
                ),
                JourneyStep(
                    "STEP-02-TELEMETRY-REVIEW",
                    "Inspect Diagnostics & Sync Status",
                    "টেলিমেট্রি ও সিঙ্ক স্ট্যাটাস পরিদর্শন",
                    {"PLATFORM_SUPER_ADMIN"},
                    False,
                    ["action_scope"],
                ),
                JourneyStep(
                    "STEP-03-CLOSE-SESSION",
                    "Close Session with Audit Log",
                    "অডিট লগসহ সেশন সমাপ্তি",
                    {"PLATFORM_SUPER_ADMIN"},
                    False,
                    ["resolution_notes"],
                ),
            ],
        )

    def get_journey_definition(self, journey_id: JourneyId) -> JourneyDefinition:
        if journey_id not in self._definitions:
            raise KeyError(f"Journey '{journey_id}' not found in registry.")
        return self._definitions[journey_id]

    def start_journey(
        self,
        journey_id: JourneyId,
        tenant_id: str,
        actor_role: str,
        actor_id: str,
        is_offline: bool = False,
    ) -> JourneyExecutionState:
        definition = self.get_journey_definition(journey_id)

        # Enforce Role Authorization for Journey Initiation
        first_step = definition.steps[0]
        if actor_role != "PLATFORM_SUPER_ADMIN" and actor_role not in first_step.allowed_roles:
            raise PermissionError(
                f"Role '{actor_role}' is not authorized to start journey '{definition.name_en}' ({journey_id.value}). "
                f"Allowed initial roles: {sorted(list(first_step.allowed_roles))}"
            )

        # Enforce Offline Suitability
        if is_offline and not all(step.is_offline_capable for step in definition.steps):
            raise FieldValidationError(
                message_en=f"Journey '{definition.name_en}' cannot be executed offline; requires cloud connectivity.",
                message_bn=f"যাত্রা '{definition.name_bn}' অফলাইনে সম্পন্ন করা সম্ভব নয়; ক্লাউড সংযোগ আবশ্যক।",
                field_errors={"is_offline": ["Cloud connectivity required for this journey."]},
            )

        return JourneyExecutionState(
            journey_id=journey_id,
            tenant_id=tenant_id,
            actor_role=actor_role,
            actor_id=actor_id,
            is_offline=is_offline,
            current_step_index=0,
        )

    def execute_step(
        self,
        state: JourneyExecutionState,
        step_inputs: dict[str, Any],
        actor_role: str | None = None,
    ) -> JourneyExecutionState:
        if state.status != "ACTIVE":
            raise FieldValidationError(
                message_en=f"Cannot execute step on journey with status '{state.status}'.",
                message_bn=f"স্ট্যাটাস '{state.status}'-যুক্ত যাত্রায় ধাপ সম্পাদন সম্ভব নয়।",
                field_errors={"status": [f"Journey is {state.status}."]},
            )

        definition = self.get_journey_definition(state.journey_id)
        current_step = definition.steps[state.current_step_index]

        effective_role = actor_role or state.actor_role
        if effective_role != "PLATFORM_SUPER_ADMIN" and effective_role not in current_step.allowed_roles:
            raise PermissionError(
                f"Role '{effective_role}' is not authorized to execute step '{current_step.name_en}' ({current_step.step_id}). "
                f"Allowed: {sorted(list(current_step.allowed_roles))}"
            )

        # Check required inputs
        errors: dict[str, list[str]] = {}
        for req in current_step.required_inputs:
            val = step_inputs.get(req)
            if val is None or (isinstance(val, str) and not val.strip()):
                errors[req] = [f"Input '{req}' is mandatory for step '{current_step.name_en}'."]

        if errors:
            raise FieldValidationError(
                message_en=f"Missing inputs for step '{current_step.name_en}': {list(errors.keys())}",
                message_bn=f"ধাপ '{current_step.name_bn}'-এর জন্য প্রয়োজনীয় তথ্য অনুপস্থিত: {list(errors.keys())}",
                field_errors=errors,
            )

        # Domain specific validations
        if state.journey_id == JourneyId.OWNER_ONBOARDING:
            if current_step.step_id == "STEP-01-PHONE-OTP":
                phone = step_inputs.get("phone_number", "")
                if not (phone.startswith("+8801") or phone.startswith("01")) or len(phone) < 11:
                    raise FieldValidationError(
                        message_en=f"Invalid Bangladesh phone number format: '{phone}'. Expected +8801... or 01...",
                        message_bn=f"অবৈধ বাংলাদেশী মোবাইল নম্বর ফরম্যাট: '{phone}'।",
                        field_errors={"phone_number": ["Must be valid Bangladesh mobile number."]},
                    )
            elif current_step.step_id == "STEP-02-LEGAL-PROFILE":
                dl = step_inputs.get("drug_license_no", "")
                if not dl.upper().startswith("DL-"):
                    raise FieldValidationError(
                        message_en=f"Invalid DGDA drug license format: '{dl}'. Must start with 'DL-'.",
                        message_bn=f"অবৈধ ডিজিডিএ ড্রাগ লাইসেন্স ফরম্যাট: '{dl}'। 'DL-' দিয়ে শুরু হতে হবে।",
                        field_errors={"drug_license_no": ["Must start with 'DL-'."]},
                    )

        elif state.journey_id == JourneyId.CASHIER_CHECKOUT:
            if current_step.step_id == "STEP-02-RX-CHECK":
                if step_inputs.get("is_rx_required") is True and not step_inputs.get("rx_verification_id"):
                    raise FieldValidationError(
                        message_en="Prescription verification ID is required for prescription-only items.",
                        message_bn="প্রেসক্রিপশন-নির্ভর ওষুধের জন্য যাচাইকরণ আইডি আবশ্যক।",
                        field_errors={"rx_verification_id": ["Pharmacist verification required."]},
                    )
            elif current_step.step_id == "STEP-03-TENDER-PAYMENT":
                amount = step_inputs.get("amount_paid")
                if not isinstance(amount, Decimal):
                    raise FieldValidationError(
                        message_en=f"Amount paid must be Decimal, got {type(amount).__name__}.",
                        message_bn="পরিশোধিত অর্থ অবশ্যই Decimal হতে হবে।",
                        field_errors={"amount_paid": ["Must be Decimal instance."]},
                    )
            elif current_step.step_id == "STEP-04-PRINT-RECEIPT":
                if state.is_offline:
                    step_inputs["offline_watermark"] = "*** OFFLINE RECEIPT - PENDING CLOUD CONFIRMATION ***"

        elif state.journey_id == JourneyId.PHARMACIST_VERIFICATION:
            if current_step.step_id == "STEP-03-CLINICAL-SIGNOFF":
                decision = step_inputs.get("clinical_decision")
                if decision not in {"VERIFIED_VALID", "REJECTED_UNSAFE"}:
                    raise FieldValidationError(
                        message_en=f"Invalid clinical decision: '{decision}'. Must be VERIFIED_VALID or REJECTED_UNSAFE.",
                        message_bn=f"অবৈধ ক্লিনিক্যাল সিদ্ধান্ত: '{decision}'।",
                        field_errors={"clinical_decision": ["Invalid decision value."]},
                    )

        elif state.journey_id == JourneyId.GRN_RECEIPT:
            if current_step.step_id == "STEP-02-BATCH-INSPECT":
                cost = step_inputs.get("received_cost")
                if not isinstance(cost, Decimal) or cost <= Decimal("0.00"):
                    raise FieldValidationError(
                        message_en="Received batch cost must be positive Decimal.",
                        message_bn="গৃহীত ব্যাচ মূল্য অবশ্যই ধনাত্মক Decimal হতে হবে।",
                        field_errors={"received_cost": ["Must be positive Decimal."]},
                    )
                # Check for short-dated expiry waiver
                is_short_dated = step_inputs.get("is_short_dated", False)
                has_waiver = step_inputs.get("manager_short_dated_waiver", False)
                if is_short_dated and not has_waiver:
                    raise FieldValidationError(
                        message_en="Short-dated batch (< 6 months expiry) rejected without manager waiver.",
                        message_bn="ম্যানেজার অনুমোদন ছাড়া স্বল্প মেয়াদী ব্যাচ (< ৬ মাস মেয়াদ) গ্রহণ নিষিদ্ধ।",
                        field_errors={"manager_short_dated_waiver": ["Manager waiver required for short-dated stock."]},
                    )

        elif state.journey_id == JourneyId.EXPENSE_APPROVAL:
            if current_step.step_id == "STEP-01-REQUEST-EXPENSE":
                amount = step_inputs.get("amount")
                if not isinstance(amount, Decimal) or amount <= Decimal("0.00"):
                    raise FieldValidationError(
                        message_en="Expense amount must be a positive Decimal.",
                        message_bn="খরচের পরিমাণ অবশ্যই ধনাত্মক Decimal হতে হবে।",
                        field_errors={"amount": ["Must be positive Decimal."]},
                    )
            elif current_step.step_id == "STEP-02-MANAGER-APPROVAL":
                # Check if expense amount exceeds active drawer float
                req_data = state.step_data.get("STEP-01-REQUEST-EXPENSE", {})
                req_amount = req_data.get("amount", Decimal("0.00"))
                drawer_balance = step_inputs.get("cash_drawer_balance")
                if drawer_balance is not None:
                    if not isinstance(drawer_balance, Decimal):
                        raise FieldValidationError(
                            message_en="Drawer balance must be Decimal.",
                            message_bn="ক্যাশ ড্রয়ার ব্যালেন্স অবশ্যই Decimal হতে হবে।",
                            field_errors={"cash_drawer_balance": ["Must be Decimal."]},
                        )
                    if req_amount > drawer_balance:
                        raise FieldValidationError(
                            message_en=f"Expense amount ({req_amount}) exceeds cash drawer float ({drawer_balance}).",
                            message_bn=f"খরচের পরিমাণ ({req_amount}) ক্যাশ ড্রয়ার ফ্লোট ({drawer_balance}) অতিক্রম করেছে।",
                            field_errors={"amount": ["Expense exceeds drawer balance."]},
                        )

        # Step successfully executed
        state.step_data[current_step.step_id] = step_inputs
        state.completed_steps.append(current_step.step_id)
        state.current_step_index += 1

        if state.current_step_index >= len(definition.steps):
            state.status = "COMPLETED"

        return state

    def validate_tenant_boundary(self, state: JourneyExecutionState, target_tenant_id: str) -> None:
        """Enforces tenant isolation across journey operations."""
        if state.actor_role != "PLATFORM_SUPER_ADMIN" and state.tenant_id != target_tenant_id:
            raise PermissionError(
                f"Cross-tenant journey operation blocked! State tenant '{state.tenant_id}', Target tenant '{target_tenant_id}'."
            )
