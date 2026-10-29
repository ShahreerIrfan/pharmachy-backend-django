"""
Operational Documents Inventory, Template Specifications & Field Dictionaries
Architecture Decision: ADR-0016
Catalogs the 10 core operational documents, mandatory field validations, and thermal/A4 rules.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from apps.core.errors import FieldValidationError


class DocumentType(str, Enum):
    INVOICE = "DOC-INV"
    PURCHASE_ORDER = "DOC-PO"
    GOODS_RECEIVED_NOTE = "DOC-GRN"
    RETURN_VOUCHER = "DOC-RET"
    TEMPERATURE_LOG = "DOC-TEMP"
    EXPENSE_VOUCHER = "DOC-EXP"
    THERMAL_RECEIPT = "DOC-REC"
    PRESCRIPTION_RECORD = "DOC-RX"
    ANTIBIOTIC_REGISTER = "DOC-ANT"
    SUBSCRIPTION_INVOICE = "DOC-SUB"


class PrintFormat(str, Enum):
    THERMAL_58MM = "THERMAL_58MM"
    THERMAL_80MM = "THERMAL_80MM"
    A4_PDF = "A4_PDF"
    LEDGER_SHEET = "LEDGER_SHEET"


class FieldType(str, Enum):
    STRING = "STRING"
    DECIMAL = "DECIMAL"
    INTEGER = "INTEGER"
    DATE = "DATE"
    DATETIME = "DATETIME"
    BOOLEAN = "BOOLEAN"
    LIST = "LIST"
    DICT = "DICT"


@dataclass(frozen=True)
class FieldDefinition:
    name: str
    field_type: FieldType
    is_required: bool
    bilingual_label_en: str
    bilingual_label_bn: str


@dataclass(frozen=True)
class DocumentTemplate:
    doc_type: DocumentType
    name_en: str
    name_bn: str
    print_format: PrintFormat
    fields: list[FieldDefinition]
    allowed_roles: set[str]

    def get_field_map(self) -> dict[str, FieldDefinition]:
        return {f.name: f for f in self.fields}


class OperationalDocumentCatalog:
    """Canonical registry and validator for the 10 pharmacy operational documents."""

    def __init__(self):
        self._templates: dict[DocumentType, DocumentTemplate] = {}
        self._register_canonical_templates()

    def _register_canonical_templates(self) -> None:
        # 1. Sales Invoice (DOC-INV)
        self._templates[DocumentType.INVOICE] = DocumentTemplate(
            doc_type=DocumentType.INVOICE,
            name_en="Sales Invoice",
            name_bn="বিক্রয় চালান",
            print_format=PrintFormat.A4_PDF,
            allowed_roles={"CASHIER", "BRANCH_MANAGER", "TENANT_OWNER"},
            fields=[
                FieldDefinition("invoice_number", FieldType.STRING, True, "Invoice Number", "চালান নম্বর"),
                FieldDefinition("tenant_id", FieldType.STRING, True, "Tenant ID", "টেন্যান্ট আইডি"),
                FieldDefinition("branch_id", FieldType.STRING, True, "Branch ID", "শাখা আইডি"),
                FieldDefinition("timestamp", FieldType.DATETIME, True, "Date & Time", "তারিখ ও সময়"),
                FieldDefinition("cashier_name", FieldType.STRING, True, "Cashier", "ক্যাশিয়ার"),
                FieldDefinition("subtotal", FieldType.DECIMAL, True, "Subtotal", "মোট মূল্য"),
                FieldDefinition("discount", FieldType.DECIMAL, True, "Discount", "ছাড়"),
                FieldDefinition("net_total", FieldType.DECIMAL, True, "Net Total", "সর্বমোট প্রদেয়"),
                FieldDefinition("items", FieldType.LIST, True, "Line Items", "পণ্য বিবরণী"),
            ],
        )

        # 2. Purchase Order (DOC-PO)
        self._templates[DocumentType.PURCHASE_ORDER] = DocumentTemplate(
            doc_type=DocumentType.PURCHASE_ORDER,
            name_en="Purchase Order",
            name_bn="ক্রয় আদেশ",
            print_format=PrintFormat.A4_PDF,
            allowed_roles={"PURCHASER", "BRANCH_MANAGER", "TENANT_OWNER"},
            fields=[
                FieldDefinition("po_number", FieldType.STRING, True, "PO Number", "ক্রয় আদেশ নম্বর"),
                FieldDefinition("tenant_id", FieldType.STRING, True, "Tenant ID", "টেন্যান্ট আইডি"),
                FieldDefinition("supplier_id", FieldType.STRING, True, "Supplier ID", "সরবরাহকারী আইডি"),
                FieldDefinition("supplier_name", FieldType.STRING, True, "Supplier Name", "সরবরাহকারীর নাম"),
                FieldDefinition("order_date", FieldType.DATE, True, "Order Date", "আদেশের তারিখ"),
                FieldDefinition("total_cost", FieldType.DECIMAL, True, "Total Cost", "সর্বমোট খরচ"),
                FieldDefinition("items", FieldType.LIST, True, "Order Items", "পণ্যের তালিকা"),
            ],
        )

        # 3. Goods Received Note (DOC-GRN)
        self._templates[DocumentType.GOODS_RECEIVED_NOTE] = DocumentTemplate(
            doc_type=DocumentType.GOODS_RECEIVED_NOTE,
            name_en="Goods Received Note",
            name_bn="মালামাল গ্রহণ রশিদ",
            print_format=PrintFormat.A4_PDF,
            allowed_roles={"INVENTORY_STAFF", "BRANCH_MANAGER", "PHARMACIST"},
            fields=[
                FieldDefinition("grn_number", FieldType.STRING, True, "GRN Number", "জিআরএন নম্বর"),
                FieldDefinition("tenant_id", FieldType.STRING, True, "Tenant ID", "টেন্যান্ট আইডি"),
                FieldDefinition("po_reference", FieldType.STRING, True, "PO Reference", "পিও রেফারেন্স"),
                FieldDefinition("challan_number", FieldType.STRING, True, "Challan No", "চালান নম্বর"),
                FieldDefinition("receiving_date", FieldType.DATE, True, "Receiving Date", "গ্রহণের তারিখ"),
                FieldDefinition("total_received_cost", FieldType.DECIMAL, True, "Total Cost", "মোট গৃহীত মূল্য"),
                FieldDefinition("received_batches", FieldType.LIST, True, "Received Batches", "গৃহীত ব্যাচের বিবরণ"),
            ],
        )

        # 4. Return Voucher (DOC-RET)
        self._templates[DocumentType.RETURN_VOUCHER] = DocumentTemplate(
            doc_type=DocumentType.RETURN_VOUCHER,
            name_en="Return Voucher",
            name_bn="পণ্য ফেরত ভাউচার",
            print_format=PrintFormat.THERMAL_80MM,
            allowed_roles={"BRANCH_MANAGER", "TENANT_OWNER", "PURCHASER"},
            fields=[
                FieldDefinition("return_number", FieldType.STRING, True, "Return Number", "ফেরত নম্বর"),
                FieldDefinition("tenant_id", FieldType.STRING, True, "Tenant ID", "টেন্যান্ট আইডি"),
                FieldDefinition("original_invoice_ref", FieldType.STRING, True, "Original Invoice", "মূল চালান রেফারেন্স"),
                FieldDefinition("return_reason", FieldType.STRING, True, "Return Reason", "ফেরতের কারণ"),
                FieldDefinition("refund_amount", FieldType.DECIMAL, True, "Refund Amount", "ফেরতকৃত অর্থ"),
                FieldDefinition("returned_items", FieldType.LIST, True, "Returned Items", "ফেরতকৃত পণ্য"),
            ],
        )

        # 5. Temperature Log (DOC-TEMP)
        self._templates[DocumentType.TEMPERATURE_LOG] = DocumentTemplate(
            doc_type=DocumentType.TEMPERATURE_LOG,
            name_en="Storage Temperature Log",
            name_bn="তাপমাত্রা পর্যবেক্ষণ লগ",
            print_format=PrintFormat.LEDGER_SHEET,
            allowed_roles={"PHARMACIST", "BRANCH_MANAGER"},
            fields=[
                FieldDefinition("log_id", FieldType.STRING, True, "Log ID", "লগ আইডি"),
                FieldDefinition("tenant_id", FieldType.STRING, True, "Tenant ID", "টেন্যান্ট আইডি"),
                FieldDefinition("unit_identifier", FieldType.STRING, True, "Refrigerator/Room ID", "ফ্রিজ/রুম আইডি"),
                FieldDefinition("recorded_temp_celsius", FieldType.DECIMAL, True, "Temperature (°C)", "তাপমাত্রা (°সে)"),
                FieldDefinition("is_excursion", FieldType.BOOLEAN, True, "Excursion Flag", "সীমা লঙ্ঘন ফ্ল্যাগ"),
                FieldDefinition("reviewer_name", FieldType.STRING, True, "Reviewer", "পর্যবেক্ষক"),
                FieldDefinition("timestamp", FieldType.DATETIME, True, "Timestamp", "সময়কাল"),
            ],
        )

        # 6. Expense Voucher (DOC-EXP)
        self._templates[DocumentType.EXPENSE_VOUCHER] = DocumentTemplate(
            doc_type=DocumentType.EXPENSE_VOUCHER,
            name_en="Expense Voucher",
            name_bn="খরচ ভাউচার",
            print_format=PrintFormat.THERMAL_80MM,
            allowed_roles={"CASHIER", "BRANCH_MANAGER", "ACCOUNTANT"},
            fields=[
                FieldDefinition("voucher_number", FieldType.STRING, True, "Voucher No", "ভাউচার নম্বর"),
                FieldDefinition("tenant_id", FieldType.STRING, True, "Tenant ID", "টেন্যান্ট আইডি"),
                FieldDefinition("category", FieldType.STRING, True, "Expense Category", "খরচের খাত"),
                FieldDefinition("amount", FieldType.DECIMAL, True, "Amount", "পরিমাণ"),
                FieldDefinition("shift_session_id", FieldType.STRING, True, "Shift Session", "শিফট সেশন"),
                FieldDefinition("approved_by", FieldType.STRING, True, "Approved By", "অনুমোদনকারী"),
            ],
        )

        # 7. Thermal POS Receipt (DOC-REC)
        self._templates[DocumentType.THERMAL_RECEIPT] = DocumentTemplate(
            doc_type=DocumentType.THERMAL_RECEIPT,
            name_en="Thermal POS Receipt",
            name_bn="থার্মাল পিওএস রসিদ",
            print_format=PrintFormat.THERMAL_80MM,
            allowed_roles={"CASHIER", "BRANCH_MANAGER", "SIGNED_DEVICE"},
            fields=[
                FieldDefinition("receipt_number", FieldType.STRING, True, "Receipt No", "রসিদ নম্বর"),
                FieldDefinition("tenant_id", FieldType.STRING, True, "Tenant ID", "টেন্যান্ট আইডি"),
                FieldDefinition("branch_id", FieldType.STRING, True, "Branch ID", "শাখা আইডি"),
                FieldDefinition("pharmacy_name", FieldType.STRING, True, "Pharmacy Name", "ফার্মেসির নাম"),
                FieldDefinition("drug_license_no", FieldType.STRING, True, "Drug License", "ড্রাগ লাইসেন্স"),
                FieldDefinition("timestamp", FieldType.DATETIME, True, "Date & Time", "তারিখ ও সময়"),
                FieldDefinition("sync_status", FieldType.STRING, True, "Sync Status", "সিঙ্ক স্ট্যাটাস"),
                FieldDefinition("total_payable", FieldType.DECIMAL, True, "Total Payable", "সর্বমোট প্রদেয়"),
                FieldDefinition("tender_type", FieldType.STRING, True, "Tender Type", "পরিশোধের মাধ্যম"),
                FieldDefinition("items", FieldType.LIST, True, "Items", "ওষুধের তালিকা"),
            ],
        )

        # 8. Prescription Record (DOC-RX)
        self._templates[DocumentType.PRESCRIPTION_RECORD] = DocumentTemplate(
            doc_type=DocumentType.PRESCRIPTION_RECORD,
            name_en="Prescription Record",
            name_bn="প্রেসক্রিপশন রেকর্ড",
            print_format=PrintFormat.A4_PDF,
            allowed_roles={"PHARMACIST"},
            fields=[
                FieldDefinition("prescription_id", FieldType.STRING, True, "Prescription ID", "প্রেসক্রিপশন আইডি"),
                FieldDefinition("tenant_id", FieldType.STRING, True, "Tenant ID", "টেন্যান্ট আইডি"),
                FieldDefinition("patient_name", FieldType.STRING, True, "Patient Name", "রোগীর নাম"),
                FieldDefinition("doctor_name", FieldType.STRING, True, "Doctor Name", "চিকিৎসকের নাম"),
                FieldDefinition("doctor_bmdc_no", FieldType.STRING, True, "BMDC Reg No", "বিএমডিসি নিবন্ধন নম্বর"),
                FieldDefinition("verification_status", FieldType.STRING, True, "Verification Status", "যাচাই স্ট্যাটাস"),
                FieldDefinition("pharmacist_name", FieldType.STRING, True, "Pharmacist", "ফার্মাসিস্ট"),
            ],
        )

        # 9. DGDA Antibiotic Register Entry (DOC-ANT)
        self._templates[DocumentType.ANTIBIOTIC_REGISTER] = DocumentTemplate(
            doc_type=DocumentType.ANTIBIOTIC_REGISTER,
            name_en="DGDA Antibiotic Register",
            name_bn="অ্যান্টিবায়োটিক রেজিস্টার",
            print_format=PrintFormat.A4_PDF,
            allowed_roles={"PHARMACIST", "AUDITOR"},
            fields=[
                FieldDefinition("entry_id", FieldType.STRING, True, "Entry ID", "এন্ট্রি আইডি"),
                FieldDefinition("tenant_id", FieldType.STRING, True, "Tenant ID", "টেন্যান্ট আইডি"),
                FieldDefinition("patient_name", FieldType.STRING, True, "Patient Name", "রোগীর নাম"),
                FieldDefinition("patient_address", FieldType.STRING, True, "Patient Address", "রোগীর ঠিকানা"),
                FieldDefinition("doctor_name", FieldType.STRING, True, "Doctor Name", "চিকিৎসকের নাম"),
                FieldDefinition("doctor_bmdc_no", FieldType.STRING, True, "Doctor BMDC Reg", "বিএমডিসি নম্বর"),
                FieldDefinition("medicine_name", FieldType.STRING, True, "Medicine Name", "ওষুধের নাম"),
                FieldDefinition("batch_number", FieldType.STRING, True, "Batch Number", "ব্যাচ নম্বর"),
                FieldDefinition("quantity_dispensed", FieldType.INTEGER, True, "Quantity", "পরিমাণ"),
                FieldDefinition("dispensed_date", FieldType.DATE, True, "Dispensed Date", "বিতরণের তারিখ"),
                FieldDefinition(
                    "pharmacist_license", FieldType.STRING, True, "Pharmacist PCB Reg", "ফার্মাসিস্ট পিসিবি সনদ"
                ),
            ],
        )

        # 10. SaaS Subscription Invoice (DOC-SUB)
        self._templates[DocumentType.SUBSCRIPTION_INVOICE] = DocumentTemplate(
            doc_type=DocumentType.SUBSCRIPTION_INVOICE,
            name_en="Subscription Statement",
            name_bn="সাবস্ক্রিপশন চালান",
            print_format=PrintFormat.A4_PDF,
            allowed_roles={"TENANT_OWNER", "PLATFORM_SUPER_ADMIN"},
            fields=[
                FieldDefinition("statement_number", FieldType.STRING, True, "Statement No", "স্টেটমেন্ট নম্বর"),
                FieldDefinition("tenant_id", FieldType.STRING, True, "Tenant ID", "টেন্যান্ট আইডি"),
                FieldDefinition("plan_tier", FieldType.STRING, True, "Plan Tier", "প্ল্যান স্তর"),
                FieldDefinition("billing_period", FieldType.STRING, True, "Billing Period", "বিলিং পিরিয়ড"),
                FieldDefinition("amount_bdt", FieldType.DECIMAL, True, "Amount (BDT)", "পরিমাণ (টাকা)"),
                FieldDefinition("payment_status", FieldType.STRING, True, "Payment Status", "পেমেন্ট স্ট্যাটাস"),
            ],
        )

    def get_template(self, doc_type: DocumentType) -> DocumentTemplate:
        if doc_type not in self._templates:
            raise ValueError(f"Unknown document type: {doc_type}")
        return self._templates[doc_type]

    def list_templates(self) -> list[DocumentTemplate]:
        return list(self._templates.values())

    def format_offline_thermal_watermark(self, sync_status: str, lang: str = "en") -> str | None:
        """Provides prominent watermark banner for offline unconfirmed receipts."""
        if sync_status == "PENDING_SYNC":
            if lang == "bn":
                return "*** অফলাইন রসিদ - ক্লাউড কনফার্মেশন অপেক্ষমান ***"
            return "*** OFFLINE RECEIPT - PENDING CLOUD CONFIRMATION ***"
        return None

    def validate_document_data(
        self,
        doc_type: DocumentType,
        data: dict[str, Any],
        actor_role: str,
        actor_tenant_id: str,
    ) -> dict[str, Any]:
        """
        Validates document data against canonical template specifications:
        1. Tenant boundary enforcement.
        2. Role-based creation authorization.
        3. Mandatory field presence.
        4. Decimal precision for monetary amounts (no float!).
        """
        template = self.get_template(doc_type)

        # Security: Role Authorization
        if actor_role != "PLATFORM_SUPER_ADMIN" and actor_role not in template.allowed_roles:
            raise PermissionError(
                f"Role '{actor_role}' is not authorized to generate document '{template.name_en}' ({doc_type.value}). Allowed: {sorted(list(template.allowed_roles))}"
            )

        # Security: Tenant Isolation
        doc_tenant_id = data.get("tenant_id")
        if not doc_tenant_id:
            raise FieldValidationError(
                message_en=f"Document '{doc_type.value}' is missing required field 'tenant_id'.",
                message_bn=f"নথি '{doc_type.value}'-এ আবশ্যকীয় 'tenant_id' অনুপস্থিত।",
                field_errors={"tenant_id": ["Field is required."]},
            )

        if actor_role != "PLATFORM_SUPER_ADMIN" and doc_tenant_id != actor_tenant_id:
            raise PermissionError(
                f"Cross-tenant document generation blocked: Actor tenant '{actor_tenant_id}' cannot create document for tenant '{doc_tenant_id}'"
            )

        # Mandatory fields and data type checks
        field_map = template.get_field_map()
        errors: dict[str, list[str]] = {}

        for name, f_def in field_map.items():
            val = data.get(name)
            if f_def.is_required and (val is None or (isinstance(val, str) and not val.strip())):
                errors[name] = [f"Field '{f_def.bilingual_label_en}' is required."]
                continue

            if val is not None:
                # Cardinal Rule: Money and temperatures must be Decimal, never float!
                if f_def.field_type == FieldType.DECIMAL:
                    if isinstance(val, float):
                        errors[name] = [f"Field '{name}' must be Decimal, float is strictly forbidden."]
                    elif not isinstance(val, Decimal):
                        errors[name] = [f"Field '{name}' must be an instance of Decimal, got {type(val).__name__}."]
                elif f_def.field_type == FieldType.INTEGER:
                    if not isinstance(val, int) or isinstance(val, bool):
                        errors[name] = [f"Field '{name}' must be an integer, got {type(val).__name__}."]
                elif f_def.field_type == FieldType.BOOLEAN:
                    if not isinstance(val, bool):
                        errors[name] = [f"Field '{name}' must be boolean, got {type(val).__name__}."]

        if errors:
            raise FieldValidationError(
                message_en=f"Validation failed for document '{template.name_en}': {list(errors.keys())}",
                message_bn=f"নথি '{template.name_bn}'-এর তথ্যে ত্রুটি রয়েছে: {list(errors.keys())}",
                field_errors=errors,
            )

        return data
