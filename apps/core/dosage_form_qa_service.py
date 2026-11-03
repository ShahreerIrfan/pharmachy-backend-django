"""
backend/apps/core/dosage_form_qa_service.py
Architecture Decision: ADR-0160 (P160).
Comprehensive Positive and Negative QA Fixtures Service for All Dosage Forms.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.conf import settings
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.flexible_medicine_payload_service import FlexibleMedicinePayloadService
from apps.core.models import Tenant, TenantLifecycleStatus
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.dosage_form_qa_service")


class DosageFormQaService:
    """
    Coordinates comprehensive dosage form QA fixtures, automated validation runs,
    and fixture seeding across all 11 pharmaceutical dosage families.
    """

    FIXTURE_PATH = (
        Path(settings.BASE_DIR).parent / "shared" / "contracts" / "fixtures" / "dosage_form_qa_fixtures.json"
    )

    @classmethod
    def load_fixtures(cls) -> Dict[str, Any]:
        """Loads canonical QA fixtures from shared contracts repository."""
        if not cls.FIXTURE_PATH.exists():
            logger.error(f"Dosage form QA fixtures file not found at {cls.FIXTURE_PATH}")
            return {"positive_fixtures": [], "negative_fixtures": []}

        try:
            with open(cls.FIXTURE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.error(f"Failed to read dosage form QA fixtures: {exc}")
            raise ValidationError({
                "error_code": "ERR_FIXTURES_LOAD_FAILED",
                "detail": f"Could not load QA fixtures: {str(exc)}",
            })

    @classmethod
    def get_positive_fixtures(cls, dosage_form_family: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns positive fixtures, optionally filtered by dosage form family."""
        data = cls.load_fixtures()
        positives = data.get("positive_fixtures", [])
        if dosage_form_family:
            form_upper = dosage_form_family.strip().upper()
            return [f for f in positives if f.get("dosage_form_family", "").upper() == form_upper]
        return positives

    @classmethod
    def get_negative_fixtures(cls, target_error_code: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns negative fixtures, optionally filtered by expected error code."""
        data = cls.load_fixtures()
        negatives = data.get("negative_fixtures", [])
        if target_error_code:
            code_upper = target_error_code.strip().upper()
            return [f for f in negatives if f.get("target_error_code", "").upper() == code_upper]
        return negatives

    @classmethod
    def validate_fixture(cls, fixture_id: str) -> Dict[str, Any]:
        """
        Executes validation on a specific fixture and asserts expected vs observed outcomes.
        """
        data = cls.load_fixtures()
        target = None
        is_positive = False

        for f in data.get("positive_fixtures", []):
            if f.get("fixture_id") == fixture_id:
                target = f
                is_positive = True
                break

        if not target:
            for f in data.get("negative_fixtures", []):
                if f.get("fixture_id") == fixture_id:
                    target = f
                    is_positive = False
                    break

        if not target:
            raise NotFound({
                "error_code": "ERR_FIXTURE_NOT_FOUND",
                "detail": f"QA fixture with ID '{fixture_id}' was not found.",
            })

        payload = target.get("payload", {})
        val_result = FlexibleMedicinePayloadService.validate_flexible_payload(payload)

        if is_positive:
            passed = val_result["is_valid"] and val_result["error_count"] == 0
            return {
                "fixture_id": fixture_id,
                "fixture_type": "POSITIVE",
                "passed": passed,
                "expected": "SUCCESS (0 errors)",
                "observed": f"is_valid={val_result['is_valid']}, error_count={val_result['error_count']}",
                "validation_result": val_result,
            }
        else:
            expected_code = target.get("target_error_code")
            observed_codes = [e["error_code"] for e in val_result.get("errors", [])]
            passed = (not val_result["is_valid"]) and (expected_code in observed_codes)
            return {
                "fixture_id": fixture_id,
                "fixture_type": "NEGATIVE",
                "passed": passed,
                "expected_error_code": expected_code,
                "observed_error_codes": observed_codes,
                "validation_result": val_result,
            }

    @classmethod
    def seed_qa_fixtures(cls, user: PharmacyUser) -> Dict[str, Any]:
        """
        Seeds all canonical positive fixtures into the master medicine catalog.
        Requires non-cashier authorized role.
        """
        if getattr(user, "role", None) in [UserRole.CASHIER]:
            raise PermissionDenied({
                "error_code": "ERR_ROLE_UNAUTHORIZED",
                "detail": "Cashier role is not authorized to seed master QA fixtures.",
            })

        tenant_id = getattr(user, "tenant_id", None)
        if tenant_id:
            tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
            if tenant and (not tenant.is_active or tenant.status in [TenantLifecycleStatus.SUSPENDED, TenantLifecycleStatus.ARCHIVED]):
                raise PermissionDenied({
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Suspended tenant cannot seed master catalog.",
                })

        data = cls.load_fixtures()
        positives = data.get("positive_fixtures", [])
        seeded_products = []

        for f in positives:
            payload = f.get("payload", {})
            try:
                prod_data = FlexibleMedicinePayloadService.submit_flexible_payload(payload, user)
                seeded_products.append(prod_data)
            except Exception as e:
                logger.warning(f"Could not seed fixture {f.get('fixture_id')}: {e}")

        return {
            "seeded_count": len(seeded_products),
            "total_fixtures": len(positives),
            "products": seeded_products,
        }

