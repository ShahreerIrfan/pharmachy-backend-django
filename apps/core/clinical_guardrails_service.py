"""
backend/apps/core/clinical_guardrails_service.py

Architecture Decision: ADR-0250 (P250).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Service for Disallowing Automated Substitution, Dosage Advice, Diagnosis,
and Prescriber Impersonation across all screens and POS services.

Statutory Principles:
1. No Automated Substitution: Software cannot autonomously swap or recommend brand substitutions.
   Every substitution requires human clinical evaluation by a credentialed pharmacist (PCB).
2. No Algorithmic Dosage or Diagnosis Advice: Administrative ERP cannot provide clinical diagnosis
   or prescriptive therapy calculation.
3. No Prescriber Impersonation: Software cannot generate synthetic BMDC credentials or simulate
   prescriber authorization.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from rest_framework.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import (
    Branch,
    ClinicalSubstitutionAudit,
    MedicineProduct,
    PharmacistClinicalOverride,
    PosRegulatoryClass,
    PosRxGateStatus,
    PosSaleOrder,
    PosSaleOrderLine,
    StaffMember,
    Tenant,
)
from apps.core.pos_domain_service import PosDomainService
from apps.core.pos_rx_safety_service import PosRxSafetyService
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class ClinicalSafetyGuardrailsService:
    """
    Enforces Bangladesh DGDA clinical compliance guardrails against machine-driven
    medical practice or automated brand substitution.
    """

    ALLOWED_PHARMACIST_ROLES = {
        UserRole.STAFF_PHARMACIST,
        UserRole.PHARMACY_OWNER,
        UserRole.BRANCH_MANAGER,
        UserRole.SUPERADMIN,
        "STAFF_PHARMACIST",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "SUPERADMIN",
    }

    STATUTORY_DISCLAIMER_EN = (
        "Pharmacy ERP is an administrative workflow system and does not provide medical diagnoses, "
        "automated prescription substitutions, or clinical dosage advice. All clinical decisions must be "
        "made by qualified registered medical practitioners (BMDC) or licensed pharmacists (PCB)."
    )

    STATUTORY_DISCLAIMER_BN = (
        "ফার্মাসি ইআরপি একটি প্রশাসনিক ওয়ার্কফ্লো সিস্টেম। এটি কোনো চিকিৎসাগত রোগ নির্ণয়, "
        "স্বয়ংক্রিয় ওষুধ পরিবর্তন বা ক্লিনিক্যাল ডোজের পরামর্শ প্রদান করে না। সকল ক্লিনিক্যাল সিদ্ধান্ত "
        "অবশ্যই বিএমডিসি নিবন্ধিত চিকিৎসক বা পিসিবি নিবন্ধিত ফার্মাসিস্ট কর্তৃক গৃহীত হতে হবে।"
    )

    @classmethod
    def get_statutory_disclaimers(cls) -> Dict[str, Any]:
        """Returns statutory clinical safety disclaimers and policy limits."""
        return {
            "disclaimer_en": cls.STATUTORY_DISCLAIMER_EN,
            "disclaimer_bn": cls.STATUTORY_DISCLAIMER_BN,
            "dgda_statutory_source": "Bangladesh Drugs Act & DGDA Good Pharmacy Practice Guidelines 2024",
            "prohibitions": {
                "no_automated_substitution": True,
                "no_algorithmic_dosage_advice": True,
                "no_ai_diagnosis": True,
                "no_prescriber_impersonation": True,
            },
        }

    @classmethod
    def inspect_payload_for_forbidden_clinical_actions(cls, data: Dict[str, Any]) -> None:
        """
        Inspects request data for illegal attempts at autonomous substitution,
        algorithmic medical advice, automated diagnosis, or prescriber impersonation.
        """
        # 1. Reject automated / autonomous brand substitution
        if data.get("is_autonomous_system_substitution") is True or data.get("auto_substitute") is True:
            raise ValidationError(
                {
                    "error_code": "ERR_AUTOMATED_SUBSTITUTION_FORBIDDEN",
                    "message_en": "Autonomous or algorithmic brand substitution is strictly prohibited by law. All substitutions require human clinical evaluation and approval by a licensed pharmacist.",
                    "message_bn": "আইন অনুযায়ী সফটওয়্যার দ্বারা স্বয়ংক্রিয় ওষুধ পরিবর্তন সম্পূর্ণ নিষিদ্ধ। সকল ওষুধ পরিবর্তন অবশ্যই নিবন্ধিত ফার্মাসিস্ট কর্তৃক ম্যানুয়ালি যাচাই ও অনুমোদিত হতে হবে।",
                }
            )

        # 2. Reject automated diagnosis or clinical therapy / dosage advice
        if (
            data.get("ai_diagnosis")
            or data.get("suggested_diagnosis")
            or data.get("automated_dosage_prescribed")
            or data.get("clinical_treatment_advice")
        ):
            raise ValidationError(
                {
                    "error_code": "ERR_CLINICAL_ADVICE_FORBIDDEN",
                    "message_en": "Administrative ERP is legally barred from offering autonomous clinical diagnoses or dosage advice.",
                    "message_bn": "প্রশাসনিক ইআরপি সিস্টেম রোগ নির্ণয় বা চিকিৎসার ডোজ সংক্রান্ত পরামর্শ প্রদান করতে পারে না।",
                }
            )

        # 3. Reject prescriber impersonation or forged synthetic BMDC credentials
        if (
            data.get("impersonate_prescriber") is True
            or data.get("auto_generate_bmdc") is True
            or data.get("synthetic_doctor_signature")
        ):
            raise ValidationError(
                {
                    "error_code": "ERR_PRESCRIBER_IMPERSONATION_FORBIDDEN",
                    "message_en": "System impersonation of licensed prescribers or generating artificial medical credentials is strictly forbidden.",
                    "message_bn": "চিকিৎসকের পরিচয় নকল করা বা কৃত্রিম বিএমডিসি সনদ তৈরি করা সম্পূর্ণ নিষিদ্ধ।",
                }
            )

    @classmethod
    def serialize_substitution(cls, audit: ClinicalSubstitutionAudit) -> Dict[str, Any]:
        """Serializes substitution audit record to JSON dict."""
        return {
            "audit_id": audit.audit_id,
            "sale_id": audit.sale_id,
            "original_product_id": audit.original_product_id,
            "original_product_name": audit.original_product.brand_name,
            "replacement_product_id": audit.replacement_product_id,
            "replacement_product_name": audit.replacement_product.brand_name,
            "generic_name": audit.replacement_product.generic_name,
            "pharmacist_user_id": str(audit.pharmacist_user_id),
            "pharmacist_pcb_no": audit.pharmacist_pcb_no,
            "linked_override_id": audit.linked_override_id,
            "is_manual_human_decision": audit.is_manual_human_decision,
            "clinical_rationale": audit.clinical_rationale,
            "sha256_hash": audit.sha256_hash,
            "created_at": audit.created_at.isoformat() if audit.created_at else None,
        }

    @classmethod
    @transaction.atomic
    def execute_manual_brand_substitution(
        cls,
        user: Any,
        sale: PosSaleOrder,
        original_product: MedicineProduct,
        replacement_product: MedicineProduct,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Executes an audited human-pharmacist manual brand substitution.
        Strictly verifies that this is NOT an automated machine substitution,
        enforces pharmacist PCB credentialing, checks product safety (no recalled or narcotic items),
        updates the sale line, and writes an immutable tamper-evident audit record.
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required for brand substitution.")

        # 1. Role validation
        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in cls.ALLOWED_PHARMACIST_ROLES:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_PHARMACIST_SUBSTITUTION_UNAUTHORIZED",
                    "message_en": "Only licensed pharmacists or pharmacy managers may perform manual brand substitutions.",
                    "message_bn": "শুধুমাত্র নিবন্ধিত ফার্মাসিস্ট বা ফার্মেসি ম্যানেজার ম্যানুয়াল ওষুধ প্রতিস্থাপন করতে পারবেন।",
                }
            )

        # 2. Inspect for illegal autonomous flags
        cls.inspect_payload_for_forbidden_clinical_actions(data)

        # 3. Multi-tenant and branch check
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=sale.branch_id, require_write=True, allowed_roles=cls.ALLOWED_PHARMACIST_ROLES
        )

        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        if sale.tenant_id != tenant.tenant_id:
            raise PermissionDenied("Cross-tenant substitution access denied.")

        # 4. Product Safety Verifications
        orig_classification = PosRxSafetyService.classify_product(original_product)
        repl_classification = PosRxSafetyService.classify_product(replacement_product)

        if orig_classification["is_blocked_recall_or_ban"] or repl_classification["is_blocked_recall_or_ban"]:
            raise ValidationError(
                {
                    "error_code": "ERR_REGULATED_PRODUCT_BLOCKED",
                    "message_en": "Cannot substitute to or from a recalled or banned medicine.",
                    "message_bn": "প্রত্যাহৃত বা নিষিদ্ধ ওষুধের ক্ষেত্রে প্রতিস্থাপন করা যাবে না।",
                }
            )

        if orig_classification["is_narcotic"] or repl_classification["is_narcotic"]:
            raise ValidationError(
                {
                    "error_code": "ERR_NARCOTIC_OVERRIDE_FORBIDDEN",
                    "message_en": "Narcotics / Schedule-G controlled substances cannot be substituted under any circumstance.",
                    "message_bn": "মাদকদ্রব্য কোনো অবস্থাতেই প্রতিস্থাপন করা যাবে না।",
                }
            )

        # 5. Mandatory PCB verification
        clean_pcb = str(
            data.get("pharmacist_pcb_no")
            or getattr(user, "pcb_registration_no", "")
            or ""
        ).strip().upper()
        if len(clean_pcb) < 4:
            raise ValidationError(
                {"pharmacist_pcb_no": "A valid Pharmacist PCB registration number (minimum 4 characters) is required for substitution."}
            )

        # 6. Rationale verification
        rationale = str(data.get("clinical_rationale") or "").strip()
        if len(rationale) < 5:
            raise ValidationError(
                {"clinical_rationale": "Clinical rationale for manual substitution (minimum 5 characters) is mandatory."}
            )

        # Optional linked override
        override_id = data.get("override_id")
        linked_override = None
        if override_id:
            linked_override = PharmacistClinicalOverride.objects.filter(
                override_id=str(override_id).strip(), tenant=tenant
            ).first()

        # 7. Locate sale line
        sale_line = sale.lines.filter(product=original_product).first()
        if not sale_line:
            raise ValidationError(
                {"original_product_id": f"Product '{original_product.brand_name}' not found in active sale lines."}
            )

        # Update sale line to point to replacement product
        sale_line.product = replacement_product
        sale_line.requires_rx_verification = repl_classification["requires_rx_verification"]
        sale_line.regulatory_classification = repl_classification["regulatory_classification"]
        # Update price to match replacement product MRP
        sale_line.unit_price_bdt = replacement_product.mrp_unit
        sale_line.unit_cost_bdt = replacement_product.cost_unit
        sale_line.line_subtotal_bdt = sale_line.unit_price_bdt * sale_line.quantity_units
        sale_line.line_total_bdt = sale_line.line_subtotal_bdt
        sale_line.save(
            update_fields=[
                "product",
                "requires_rx_verification",
                "regulatory_classification",
                "unit_price_bdt",
                "unit_cost_bdt",
                "line_subtotal_bdt",
                "line_total_bdt",
                "updated_at",
            ]
        )

        # Recompute sale totals
        total = sum(l.line_total_bdt for l in sale.lines.all())
        sale.subtotal_bdt = total
        sale.grand_total_bdt = total
        sale.paid_amount_bdt = total
        sale.save(update_fields=["subtotal_bdt", "grand_total_bdt", "paid_amount_bdt", "updated_at"])

        # 8. Create immutable substitution audit record
        audit = ClinicalSubstitutionAudit.objects.create(
            audit_id=f"csa-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            sale=sale,
            original_product=original_product,
            replacement_product=replacement_product,
            pharmacist_user=user,
            pharmacist_pcb_no=clean_pcb,
            linked_override=linked_override,
            is_manual_human_decision=True,
            clinical_rationale=rationale,
        )

        # 9. Record POS Rx Audit entry
        PosRxSafetyService._record_rx_audit(
            sale=sale,
            action="MANUAL_BRAND_SUBSTITUTION",
            from_gate_status=sale.rx_gate_status,
            to_gate_status=sale.rx_gate_status,
            user=user,
            reason=f"Manual substitution: {original_product.brand_name} -> {replacement_product.brand_name} by {clean_pcb}: {rationale}",
            metadata={
                "audit_id": audit.audit_id,
                "original_product_id": original_product.product_id,
                "replacement_product_id": replacement_product.product_id,
                "pharmacist_pcb_no": clean_pcb,
                "sha256": audit.sha256_hash,
            },
        )

        return {
            "substitution": cls.serialize_substitution(audit),
            "disclaimer": cls.get_statutory_disclaimers(),
            "sale_id": sale.sale_id,
            "updated_line_id": sale_line.line_id,
            "grand_total_bdt": str(sale.grand_total_bdt),
        }

    @classmethod
    def list_sale_substitutions(cls, user: Any, sale: PosSaleOrder) -> List[Dict[str, Any]]:
        """Lists all manual substitutions conducted on this sale order."""
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=sale.branch_id, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        audits = (
            ClinicalSubstitutionAudit.objects.filter(tenant=tenant, sale=sale)
            .select_related("original_product", "replacement_product", "pharmacist_user")
            .order_by("-created_at")
        )
        return [cls.serialize_substitution(a) for a in audits]
