"""
apps.quality_governance.engine
Architecture Decision: ADR-0039
Formal Definition of Done evaluation engine with 7-pillar verification and clinical safety guardrails.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


class DoDValidationError(ValueError):
    """Raised when Definition of Done payload is structurally incomplete."""


@dataclass(frozen=True)
class DoDResult:
    status: str  # 'PASSED', 'DEFICIENT', 'BLOCKED'
    is_approved: bool
    reasons_en: List[str]
    reasons_bn: List[str]


class DefinitionOfDoneEngine:
    """
    Evaluates whether a developmental change satisfies all 7 mandatory DoD pillars.
    Source-Specific Rule:
      - Any unresolved CRITICAL_CLINICAL_SAFETY flaw or failing tests strictly forces BLOCKED.
      - Any false pillar with zero failing tests yields DEFICIENT.
      - All 7 pillars passing with verified zero-fail test evidence yields PASSED.
    """

    MANDATORY_PILLARS = (
        "traceability_pass",
        "security_phi_pass",
        "decimal_money_pass",
        "fefo_stock_pass",
        "migration_safety_pass",
        "multiplatform_ui_pass",
        "test_evidence_pass",
    )

    @classmethod
    def evaluate(cls, audit_data: Dict[str, Any]) -> DoDResult:
        reasons_en: List[str] = []
        reasons_bn: List[str] = []

        # 1. Check for Critical Clinical Safety Flaw
        has_clinical_flaw = bool(audit_data.get("has_critical_clinical_safety_flaw", False))
        if has_clinical_flaw:
            reasons_en.append("Blocked: Unresolved critical clinical safety defect detected (e.g. DGDA compliance).")
            reasons_bn.append("অবরুদ্ধ: অপরিশোধিত ক্লিনিকাল নিরাপত্তা ত্রুটি শনাক্ত হয়েছে (যেমন ডিজিডিএ নির্দেশিকা)।")

        # 2. Check Test Evidence Numbers
        tests_failed = int(audit_data.get("tests_failed", 0))
        tests_passed = int(audit_data.get("tests_passed", 0))
        if tests_failed > 0:
            reasons_en.append(f"Blocked: {tests_failed} automated regression tests failed.")
            reasons_bn.append(f"অবরুদ্ধ: {tests_failed}টি স্বয়ংক্রিয় রিগ্রেশন টেস্ট ব্যর্থ হয়েছে।")

        if tests_passed == 0 and not has_clinical_flaw:
            reasons_en.append("Blocked: Zero test pass evidence provided.")
            reasons_bn.append("অবরুদ্ধ: কোনো উত্তীর্ণ টেস্ট এভিডেন্স প্রদান করা হয়নি।")

        # If any blocking condition met, immediately return BLOCKED
        if has_clinical_flaw or tests_failed > 0 or tests_passed == 0:
            return DoDResult(
                status="BLOCKED",
                is_approved=False,
                reasons_en=reasons_en,
                reasons_bn=reasons_bn,
            )

        # 3. Check 7 DoD Pillars
        failed_pillars: List[str] = []
        for pillar in cls.MANDATORY_PILLARS:
            val = audit_data.get(pillar)
            if val is not True:
                failed_pillars.append(pillar)

        if failed_pillars:
            reasons_en.append(f"Deficient: The following DoD pillars were not satisfied: {', '.join(failed_pillars)}")
            reasons_bn.append(f"অসম্পূর্ণ: নিম্নলিখিত ডিওডি স্তম্ভগুলো পূরণ করা হয়নি: {', '.join(failed_pillars)}")
            return DoDResult(
                status="DEFICIENT",
                is_approved=False,
                reasons_en=reasons_en,
                reasons_bn=reasons_bn,
            )

        # 4. All Clear: PASSED
        reasons_en.append("All 7 Definition of Done pillars satisfied with verified test evidence.")
        reasons_bn.append("যাচাইকৃত টেস্ট এভিডেন্স সহ ডেফিনিশন অব ডান-এর ৭টি স্তম্ভই সফলভাবে উত্তীর্ণ হয়েছে।")
        return DoDResult(
            status="PASSED",
            is_approved=True,
            reasons_en=reasons_en,
            reasons_bn=reasons_bn,
        )
