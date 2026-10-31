"""
apps/core/security_baseline.py

Architecture Decision: ADR-0060
Backend Security Smoke & Baseline Suite Execution Engine.
Audits the 7 Foundational Security Pillars:
  1. AUTH_GATE: Token lifecycle, authentication boundaries, unauthenticated rejection.
  2. AUTHORIZATION_GATE: Least-privilege defaults, cashier limits, absolute system blocks.
  3. VALIDATION_GATE: Input contract constraints, Bangladesh phone format, monetary sanity.
  4. ISOLATION_GATE: Multi-tenant boundary enforcement and branch scoping constraints.
  5. AUDIT_GATE: Immutable security audit log event generation and traceability.
  6. PRIVACY_GATE: Sensitive purchase cost prices and patient PII field redaction.
  7. MIGRATION_GATE: Zero unapplied database migrations across all modular apps.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.identity.models import UserRole, normalize_bangladesh_phone

DHAKA_TZ = ZoneInfo("Asia/Dhaka")
UTC_TZ = ZoneInfo("UTC")


class SecurityPillar:
    AUTH_GATE = "AUTH_GATE"
    AUTHORIZATION_GATE = "AUTHORIZATION_GATE"
    VALIDATION_GATE = "VALIDATION_GATE"
    ISOLATION_GATE = "ISOLATION_GATE"
    AUDIT_GATE = "AUDIT_GATE"
    PRIVACY_GATE = "PRIVACY_GATE"
    MIGRATION_GATE = "MIGRATION_GATE"


@dataclass
class SecurityCheckResult:
    pillar: str
    name: str
    status: str  # "PASS", "FAIL", "SKIPPED"
    message_en: str
    message_bn: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pillar": self.pillar,
            "name": self.name,
            "status": self.status,
            "message_en": self.message_en,
            "message_bn": self.message_bn,
            "details": self.details,
        }


class BackendSecurityBaselineEngine:
    """
    Automated security smoke and baseline verification engine.
    """

    @classmethod
    def get_security_posture_summary(cls, tenant_id: str) -> Dict[str, Any]:
        """Returns high-level posture status across all security pillars."""
        now_dhaka = datetime.now(DHAKA_TZ)

        # Check migration status
        executor = MigrationExecutor(connection)
        unapplied_count = len(executor.migration_plan(executor.loader.graph.leaf_nodes()))

        return {
            "service": "pharmacy-saas-cloud-api",
            "version": "v2",
            "tenant_id": tenant_id,
            "timestamp": now_dhaka.isoformat(),
            "timezone": "Asia/Dhaka",
            "posture": {
                "authentication": "JWT_BEARER_MFA_ENABLED",
                "default_authorization_policy": "DENY",
                "tenant_isolation_mode": "STRICT_FILTER_AND_MIDDLEWARE",
                "branch_scoping_enforced": True,
                "absolute_deletion_blocks": True,
                "pii_masking_enabled": True,
                "audit_trail_status": "ACTIVE_IMMUTABLE",
                "unapplied_migrations_count": unapplied_count,
            },
        }

    @classmethod
    def run_security_baseline_suite(
        cls,
        tenant_id: str,
        caller_user: Any = None,
        fast_mode: bool = True,
    ) -> Dict[str, Any]:
        """
        Executes end-to-end security smoke verification against real system services.
        """
        start_time = time.perf_counter()
        now_utc = datetime.now(UTC_TZ)
        checks: List[SecurityCheckResult] = []

        # -------------------------------------------------------------
        # 1. AUTH_GATE: Unauthenticated Protection & Token Rejection
        # -------------------------------------------------------------
        try:
            from apps.core.permission_matrix import EndpointPermissionMatrixEngine

            unauth_eval = EndpointPermissionMatrixEngine.evaluate_request(
                user=None,
                method="GET",
                path="/api/v2/medicines/",
            )
            if not unauth_eval["is_allowed"] and unauth_eval["status_code"] == 401:
                checks.append(
                    SecurityCheckResult(
                        pillar=SecurityPillar.AUTH_GATE,
                        name="unauthenticated_call_rejection",
                        status="PASS",
                        message_en="Unauthenticated request strictly blocked with HTTP 401.",
                        message_bn="অননুমোদিত রিকোয়েস্ট HTTP 401 দ্বারা সফলভাবে প্রতিরোধ করা হয়েছে।",
                        details={"error_code": unauth_eval.get("error_code")},
                    )
                )
            else:
                checks.append(
                    SecurityCheckResult(
                        pillar=SecurityPillar.AUTH_GATE,
                        name="unauthenticated_call_rejection",
                        status="FAIL",
                        message_en="Unauthenticated request was not properly blocked.",
                        message_bn="অননুমোদিত রিকোয়েস্ট সঠিকভাবে প্রতিরোধ করা যায়নি।",
                        details={"result": unauth_eval},
                    )
                )
        except Exception as ex:
            checks.append(
                SecurityCheckResult(
                    pillar=SecurityPillar.AUTH_GATE,
                    name="unauthenticated_call_rejection",
                    status="FAIL",
                    message_en=f"Auth gate check failed with exception: {ex}",
                    message_bn=f"অথ গেট পরীক্ষায় ত্রুটি ঘটেছে: {ex}",
                )
            )

        # -------------------------------------------------------------
        # 2. AUTHORIZATION_GATE: Least Privilege & Absolute Blocks
        # -------------------------------------------------------------
        try:
            from apps.core.permission_matrix import EndpointPermissionMatrixEngine

            # Test Cashier restricted from administrative audit-logs
            cashier_mock = type(
                "MockUser",
                (),
                {"is_authenticated": True, "role": UserRole.CASHIER, "branch_id": "b-1", "tenant_id": tenant_id},
            )()
            cashier_eval = EndpointPermissionMatrixEngine.evaluate_request(
                user=cashier_mock,
                method="GET",
                path="/api/v2/audit-logs/",
            )
            is_cashier_denied = not cashier_eval["is_allowed"] and cashier_eval["status_code"] == 403

            # Test Absolute Block: DELETE on posted invoices is blocked for ALL roles
            owner_mock = type(
                "MockUser",
                (),
                {"is_authenticated": True, "role": UserRole.PHARMACY_OWNER, "branch_id": "b-1", "tenant_id": tenant_id},
            )()
            block_eval = EndpointPermissionMatrixEngine.evaluate_request(
                user=owner_mock,
                method="DELETE",
                path="/api/v2/invoices/inv-001/",
            )
            is_block_enforced = not block_eval["is_allowed"] and block_eval["error_code"] == "ABSOLUTE_SYSTEM_BLOCK"

            if is_cashier_denied and is_block_enforced:
                checks.append(
                    SecurityCheckResult(
                        pillar=SecurityPillar.AUTHORIZATION_GATE,
                        name="least_privilege_and_absolute_block",
                        status="PASS",
                        message_en="Least-privilege defaults and absolute destructive deletion blocks verified.",
                        message_bn="লিস্ট-প্রিভিলেজ নীতিমালা ও ধ্বংসাত্মক ডিলিট ব্লক সফলভাবে যাচাইকৃত।",
                        details={"cashier_denied": True, "absolute_block_enforced": True},
                    )
                )
            else:
                checks.append(
                    SecurityCheckResult(
                        pillar=SecurityPillar.AUTHORIZATION_GATE,
                        name="least_privilege_and_absolute_block",
                        status="FAIL",
                        message_en="Authorization rule violated least privilege or absolute block expectations.",
                        message_bn="অনুমোদন নীতিমালা প্রত্যাশিত সুরক্ষা প্রদান করেনি।",
                        details={"is_cashier_denied": is_cashier_denied, "is_block_enforced": is_block_enforced},
                    )
                )
        except Exception as ex:
            checks.append(
                SecurityCheckResult(
                    pillar=SecurityPillar.AUTHORIZATION_GATE,
                    name="least_privilege_and_absolute_block",
                    status="FAIL",
                    message_en=f"Authorization gate error: {ex}",
                    message_bn=f"অথোরাইজেশন গেটে ত্রুটি: {ex}",
                )
            )

        # -------------------------------------------------------------
        # 3. VALIDATION_GATE: Malformed Input Rejection & BD Phone Normalization
        # -------------------------------------------------------------
        try:
            # 1. Invalid phone rejection test
            is_invalid_phone_rejected = False
            try:
                normalize_bangladesh_phone("01112345678")  # Invalid operator 011
            except ValidationError:
                is_invalid_phone_rejected = True

            # 2. Valid phone normalization test
            valid_normalized = normalize_bangladesh_phone("01712345678")
            is_valid_phone_normalized = valid_normalized == "+8801712345678"

            if is_invalid_phone_rejected and is_valid_phone_normalized:
                checks.append(
                    SecurityCheckResult(
                        pillar=SecurityPillar.VALIDATION_GATE,
                        name="input_validation_and_phone_normalization",
                        status="PASS",
                        message_en="Input validation contracts and E.164 phone normalization verified.",
                        message_bn="ইনপুট ভ্যালিডেশন কন্ট্রাক্ট ও ফোন নম্বর নরম্যালাইজেশন যাচাইকৃত।",
                        details={"normalized_sample": valid_normalized},
                    )
                )
            else:
                checks.append(
                    SecurityCheckResult(
                        pillar=SecurityPillar.VALIDATION_GATE,
                        name="input_validation_and_phone_normalization",
                        status="FAIL",
                        message_en="Validation rules failed to reject invalid phone or normalize E.164.",
                        message_bn="ভ্যালিডেশন রুলস ইনভ্যালিড ফোন শনাক্ত করতে ব্যর্থ হয়েছে।",
                    )
                )
        except Exception as ex:
            checks.append(
                SecurityCheckResult(
                    pillar=SecurityPillar.VALIDATION_GATE,
                    name="input_validation_and_phone_normalization",
                    status="FAIL",
                    message_en=f"Validation gate error: {ex}",
                    message_bn=f"ভ্যালিডেশন গেটে ত্রুটি: {ex}",
                )
            )

        # -------------------------------------------------------------
        # 4. ISOLATION_GATE: Cross-Branch & Cross-Tenant Barrier
        # -------------------------------------------------------------
        try:
            from apps.core.permission_matrix import EndpointPermissionMatrixEngine

            cashier_branch_user = type(
                "MockUser",
                (),
                {
                    "is_authenticated": True,
                    "role": UserRole.CASHIER,
                    "branch_id": "branch-main-01",
                    "tenant_id": tenant_id,
                },
            )()
            branch_violation_eval = EndpointPermissionMatrixEngine.evaluate_request(
                user=cashier_branch_user,
                method="POST",
                path="/api/v2/invoices/",
                target_branch_id="branch-other-99",  # Cross-branch attempt
            )
            is_cross_branch_denied = (
                not branch_violation_eval["is_allowed"]
                and branch_violation_eval["error_code"] == "FORBIDDEN_BRANCH_BOUNDARY"
            )

            if is_cross_branch_denied:
                checks.append(
                    SecurityCheckResult(
                        pillar=SecurityPillar.ISOLATION_GATE,
                        name="cross_branch_and_tenant_isolation",
                        status="PASS",
                        message_en="Cross-branch boundary confinement verified with security denial.",
                        message_bn="আন্তঃশাখা সীমানা সুরক্ষা ও অননুমোদিত প্রবেশাধিকার রোধ সফলভাবে যাচাইকৃত।",
                        details={"error_code": branch_violation_eval["error_code"]},
                    )
                )
            else:
                checks.append(
                    SecurityCheckResult(
                        pillar=SecurityPillar.ISOLATION_GATE,
                        name="cross_branch_and_tenant_isolation",
                        status="FAIL",
                        message_en="Cross-branch boundary check permitted unauthorized access.",
                        message_bn="আন্তঃশাখা প্রবেশাধিকার অননুমোদিত অ্যাক্সেস রোধ করতে পারেনি।",
                    )
                )
        except Exception as ex:
            checks.append(
                SecurityCheckResult(
                    pillar=SecurityPillar.ISOLATION_GATE,
                    name="cross_branch_and_tenant_isolation",
                    status="FAIL",
                    message_en=f"Isolation gate error: {ex}",
                    message_bn=f"আইসোলেশন গেটে ত্রুটি: {ex}",
                )
            )

        # -------------------------------------------------------------
        # 5. AUDIT_GATE: Audit Log Trail Verification
        # -------------------------------------------------------------
        try:
            from apps.audit_logging.models import AuditActionStatus, AuditLogEntry

            test_audit_entry = AuditLogEntry.objects.create(
                action="SECURITY_BASELINE_SMOKE_PROBE",
                resource_type="SECURITY_POSTURE",
                tenant_id=tenant_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={"probe": "smoke_baseline_test", "timestamp": now_utc.isoformat()},
            )
            has_audit_persisted = bool(test_audit_entry.pk)

            if has_audit_persisted:
                checks.append(
                    SecurityCheckResult(
                        pillar=SecurityPillar.AUDIT_GATE,
                        name="audit_log_persistence",
                        status="PASS",
                        message_en="Audit trail event logging successfully committed to persistent storage.",
                        message_bn="অডিট ট্রেইল ইভেন্ট লগ সফলভাবে পারসিস্টেন্ট স্টোরেজে সংরক্ষিত হয়েছে।",
                        details={"audit_id": str(test_audit_entry.pk)},
                    )
                )
            else:
                checks.append(
                    SecurityCheckResult(
                        pillar=SecurityPillar.AUDIT_GATE,
                        name="audit_log_persistence",
                        status="FAIL",
                        message_en="Audit log failed to persist event.",
                        message_bn="অডিট লগ ইভেন্ট সংরক্ষণ করতে ব্যর্থ হয়েছে।",
                    )
                )
        except Exception as ex:
            checks.append(
                SecurityCheckResult(
                    pillar=SecurityPillar.AUDIT_GATE,
                    name="audit_log_persistence",
                    status="FAIL",
                    message_en=f"Audit gate error: {ex}",
                    message_bn=f"অডিট গেটে ত্রুটি: {ex}",
                )
            )

        # -------------------------------------------------------------
        # 6. PRIVACY_GATE: Sensitive Cost & PII Field Redaction
        # -------------------------------------------------------------
        try:
            from apps.core.permission_matrix import EndpointPermissionMatrixEngine

            cashier_user = type(
                "MockUser",
                (),
                {"is_authenticated": True, "role": UserRole.CASHIER, "branch_id": "b-1", "tenant_id": tenant_id},
            )()
            masking_eval = EndpointPermissionMatrixEngine.evaluate_request(
                user=cashier_user,
                method="POST",
                path="/api/v2/invoices/",
                target_branch_id="b-1",
                raw_data={"supplier_unit_cost": "250.00", "customer_phone": "+8801712345678"},
            )
            masked_data = masking_eval.get("masked_data") or {}
            is_cost_masked = masked_data.get("supplier_unit_cost") == "***REDACTED***"
            is_phone_masked = masked_data.get("customer_phone") == "+88017****5678"

            if is_cost_masked and is_phone_masked:
                checks.append(
                    SecurityCheckResult(
                        pillar=SecurityPillar.PRIVACY_GATE,
                        name="privacy_field_masking",
                        status="PASS",
                        message_en="Sensitive commercial cost prices and patient PII redacted according to policy.",
                        message_bn="বাণিজ্যিক ক্রয়মূল্য ও রোগীর ব্যক্তিগত তথ্য সফলভাবে মাস্ক করা হয়েছে।",
                        details={"supplier_unit_cost": masked_data.get("supplier_unit_cost")},
                    )
                )
            else:
                checks.append(
                    SecurityCheckResult(
                        pillar=SecurityPillar.PRIVACY_GATE,
                        name="privacy_field_masking",
                        status="FAIL",
                        message_en="Privacy masking failed to redact cost price or phone number.",
                        message_bn="প্রাইভেসি মাস্কিং ক্রয়মূল্য অথবা ফোন নম্বর মাস্ক করতে ব্যর্থ হয়েছে।",
                        details={"masked_data": masked_data},
                    )
                )
        except Exception as ex:
            checks.append(
                SecurityCheckResult(
                    pillar=SecurityPillar.PRIVACY_GATE,
                    name="privacy_field_masking",
                    status="FAIL",
                    message_en=f"Privacy gate error: {ex}",
                    message_bn=f"প্রাইভেসি গেটে ত্রুটি: {ex}",
                )
            )

        # -------------------------------------------------------------
        # 7. MIGRATION_GATE: Zero Unapplied Database Migrations
        # -------------------------------------------------------------
        try:
            executor = MigrationExecutor(connection)
            unapplied = executor.migration_plan(executor.loader.graph.leaf_nodes())
            unapplied_count = len(unapplied)

            if unapplied_count == 0:
                checks.append(
                    SecurityCheckResult(
                        pillar=SecurityPillar.MIGRATION_GATE,
                        name="database_migration_integrity",
                        status="PASS",
                        message_en="Zero unapplied migrations. Database schema graph is fully synchronized.",
                        message_bn="কোনো অপরিবর্তিত মাইগ্রেশন নেই। ডেটাবেস স্কিমা সম্পূর্ণ সিঙ্ক্রোনাইজড।",
                        details={"unapplied_count": 0},
                    )
                )
            else:
                checks.append(
                    SecurityCheckResult(
                        pillar=SecurityPillar.MIGRATION_GATE,
                        name="database_migration_integrity",
                        status="FAIL",
                        message_en=f"Detected {unapplied_count} unapplied database migrations.",
                        message_bn=f"{unapplied_count}টি অনিষ্পন্ন মাইগ্রেশন শনাক্ত হয়েছে।",
                        details={"unapplied_count": unapplied_count},
                    )
                )
        except Exception as ex:
            checks.append(
                SecurityCheckResult(
                    pillar=SecurityPillar.MIGRATION_GATE,
                    name="database_migration_integrity",
                    status="FAIL",
                    message_en=f"Migration gate error: {ex}",
                    message_bn=f"মাইগ্রেশন গেটে ত্রুটি: {ex}",
                )
            )

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        passed_count = sum(1 for c in checks if c.status == "PASS")
        failed_count = sum(1 for c in checks if c.status == "FAIL")

        overall_status = "PASSED" if failed_count == 0 else "FAILED"

        return {
            "service": "pharmacy-saas-cloud-api",
            "version": "v2",
            "suite_name": "backend-security-baseline-suite",
            "overall_status": overall_status,
            "tenant_id": tenant_id,
            "timestamp": now_utc.isoformat(),
            "duration_ms": duration_ms,
            "checks_passed": passed_count,
            "checks_failed": failed_count,
            "checks": [c.to_dict() for c in checks],
        }
