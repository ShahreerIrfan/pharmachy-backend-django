"""
apps.data_privacy.retention_engine
Architecture Decision: ADR-0052
Statutory Retention Schedule, Deletion Assessment Engine, and Audited Anonymization.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.utils import timezone

from apps.core.models import PrescriptionRecord, SaleInvoice


class PrivacyRetentionEngine:
    """
    Enforces DGDA dispensing retention schedules, evaluates deletion conflicts,
    and conducts audited irreversible PII anonymization.
    """

    STATUTORY_RETENTION_SCHEDULE: dict[str, dict[str, Any]] = {
        "CLINICAL_PRESCRIPTIONS": {
            "retention_days": 730,
            "retention_years": 2,
            "legal_authority": "Directorate General of Drug Administration (DGDA) Dispensing Rules",
            "erasure_action": "ANONYMIZATION_ONLY",
            "description_en": "Medical prescription records must be retained for at least 2 years for clinical traceability.",
            "description_bn": "চিকিৎসকের প্রেসক্রিপশন রেকর্ড ন্যূনতম ২ বছর ক্লিনিক্যাল ট্র্যাকিংয়ের জন্য সংরক্ষণ বাধ্যতামূলক।",
        },
        "CONTROLLED_ANTIBIOTIC_LOGS": {
            "retention_days": 1825,
            "retention_years": 5,
            "legal_authority": "DGDA Antimicrobial Resistance & Narcotics Schedule",
            "erasure_action": "STATUTORY_HOLD_MANDATORY",
            "description_en": "Antibiotic and controlled drug dispensing records must be retained for 5 years.",
            "description_bn": "অ্যান্টিবায়োটিক ও নিয়ন্ত্রিত ওষুধের রেকর্ড ৫ বছর সংরক্ষণ বাধ্যতামূলক।",
        },
        "FINANCIAL_TAX_INVOICES": {
            "retention_days": 1825,
            "retention_years": 5,
            "legal_authority": "National Board of Revenue (NBR) VAT & Tax Regulations",
            "erasure_action": "ANONYMIZED_LEDGER_ONLY",
            "description_en": "Sales tax invoices and financial ledgers cannot be deleted prior to tax audit statutory limit.",
            "description_bn": "ভ্যাট ও ট্যাক্স অডিট মেয়াদের আগে বিক্রয় চালান বা লেজার মুছে ফেলা নিষিদ্ধ।",
        },
        "REGULATORY_AUDIT_LOGS": {
            "retention_days": 730,
            "retention_years": 2,
            "legal_authority": "ADR-0050 Regulatory Audit Trail Policy",
            "erasure_action": "IMMUTABLE_APPEND_ONLY",
            "description_en": "Security and dispensing audit logs are immutable and cannot be deleted.",
            "description_bn": "অডিট ট্রেইল অপরিবর্তনযোগ্য এবং মুছে ফেলা সম্পূর্ণ নিষিদ্ধ।",
        },
        "MARKETING_CONSENT_PROFILES": {
            "retention_days": 180,
            "retention_years": 0.5,
            "legal_authority": "Draft Bangladesh Data Protection Act / Consent Management",
            "erasure_action": "PHYSICAL_ERASURE_APPROVED",
            "description_en": "Non-clinical customer marketing profiles can be fully erased on demand.",
            "description_bn": "মার্কেটিং ও সাধারণ গ্রাহক তথ্য অনুরোধ প্রাপ্তিমাত্র সম্পূর্ণ মুছে ফেলা অনুমোদিত।",
        },
    }

    @classmethod
    def check_deletion_statutory_conflict(
        cls,
        tenant_id: str,
        subject_identifier: str,
    ) -> tuple[bool, int, str]:
        """
        Evaluates whether subject data is bound by mandatory DGDA prescription or NBR tax retention.
        Returns: (has_conflict, retention_horizon_days, conflict_reason)
        """
        now = timezone.now()
        two_years_ago = now - datetime.timedelta(days=730)

        # Check for clinical prescriptions
        rx_count = PrescriptionRecord.objects.filter(
            tenant_id=tenant_id,
            patient_token=subject_identifier,
            created_at__gte=two_years_ago,
        ).count()

        if rx_count > 0:
            return (
                True,
                730,
                f"Data subject has {rx_count} clinical prescription(s) under active DGDA 2-year mandatory statutory retention. Physical erasure is legally forbidden; irreversible PII anonymization must be applied.",
            )

        # Check for finalized tax invoices
        invoice_count = SaleInvoice.objects.filter(
            tenant_id=tenant_id,
            customer_phone=subject_identifier,
        ).count()

        if invoice_count > 0:
            return (
                True,
                1825,
                f"Data subject is linked to {invoice_count} finalized sales tax invoice(s) under NBR audit retention. Direct record deletion is legally forbidden; PII scrubbing must be applied.",
            )

        return (False, 180, "No active statutory retention hold detected. Subject data eligible for standard processing.")

    @classmethod
    def anonymize_subject_records(
        cls,
        tenant_id: str,
        subject_identifier: str,
    ) -> dict[str, int]:
        """
        Irreversibly scrubs identifiable personal data while preserving statutory medical/tax records.
        """
        token_hash = abs(hash(subject_identifier)) % 100000
        anon_customer_name = f"[ANONYMIZED_CUSTOMER_{token_hash}]"

        # Anonymize clinical prescriptions
        rx_qs = PrescriptionRecord.objects.filter(
            tenant_id=tenant_id,
            patient_token=subject_identifier,
        )
        rx_updated = rx_qs.update(
            diagnosis="[REDACTED_PHI]",
            notes="[ANONYMIZED_UNDER_DSR_REQUEST]",
        )

        # Anonymize sales invoices
        inv_qs = SaleInvoice.objects.filter(
            tenant_id=tenant_id,
            customer_phone=subject_identifier,
        )
        inv_updated = inv_qs.update(
            customer_name=anon_customer_name,
            customer_phone="[ANONYMIZED_PHONE]",
        )

        return {
            "prescriptions_anonymized": rx_updated,
            "invoices_anonymized": inv_updated,
        }

    @classmethod
    def build_export_bundle(
        cls,
        tenant_id: str,
        subject_identifier: str,
    ) -> dict[str, Any]:
        """
        Builds a comprehensive, machine-readable JSON data portability bundle.
        """
        now_str = timezone.now().isoformat()

        # Gather prescriptions
        prescriptions = []
        for rx in PrescriptionRecord.objects.filter(tenant_id=tenant_id, patient_token=subject_identifier):
            items = [
                {
                    "brand_name": it.product.brand_name if it.product else "Unknown",
                    "quantity": it.quantity_prescribed,
                    "dosage_instructions": it.dosage_instructions,
                }
                for it in rx.items.all()
            ]
            prescriptions.append({
                "prescription_id": rx.prescription_id,
                "prescription_date": rx.prescription_date.isoformat() if rx.prescription_date else None,
                "doctor_name": rx.doctor_name,
                "doctor_bmdc_no": rx.doctor_bmdc_no,
                "status": rx.status,
                "items": items,
            })

        # Gather invoices
        invoices = []
        for inv in SaleInvoice.objects.filter(tenant_id=tenant_id, customer_phone=subject_identifier):
            items = [
                {
                    "brand_name": it.product.brand_name if it.product else "Unknown",
                    "quantity": it.quantity,
                    "subtotal_bdt": str(it.subtotal),
                }
                for it in inv.items.all()
            ]
            invoices.append({
                "invoice_id": inv.invoice_id,
                "invoice_number": inv.invoice_number,
                "customer_name": inv.customer_name,
                "total_amount_bdt": str(inv.total_amount),
                "items": items,
            })

        return {
            "export_version": "1.0",
            "generated_at": now_str,
            "tenant_id": tenant_id,
            "subject_identifier": subject_identifier,
            "clinical_prescriptions_count": len(prescriptions),
            "clinical_prescriptions": prescriptions,
            "sales_invoices_count": len(invoices),
            "sales_invoices": invoices,
            "legal_retention_notice": "Clinical dispensing data exported under Bangladesh Data Protection rights. Statutory retention obligations apply under DGDA guidelines.",
        }

