"""
Unit tests for P014 - Standardized Error Vocabulary and Feedback Language
Validates:
1. One valid scenario: Full error payload formatting with correlation ID, bilingual messages, and retryability.
2. One invalid/incomplete input case: FieldValidationError with granular dictionary and sanitization of raw SQL errors.
3. One unauthorized/cross-tenant case: CrossTenantViolationError and PermissionDeniedError.
4. Source-specific business rule: Safe, actionable feedback without leaking internal database schemas.
"""

import unittest

from apps.core.errors import (
    BatchExpiredError,
    CrossTenantViolationError,
    FieldValidationError,
    PermissionDeniedError,
    SyncNetworkTimeoutError,
    format_error_response,
    get_registered_error_catalog,
    process_api_exception,
)


class TestStandardizedErrorVocabulary(unittest.TestCase):
    """Verifies error code integrity, bilingual messaging, retryability flags, and sanitization."""

    def test_valid_scenario_error_payload_structure(self):
        """Acceptance Criteria: One valid scenario. Formatted error contains all mandatory fields."""
        exc = BatchExpiredError(
            message_en="Batch BATCH-2023 expired on 2026-01-01.",
            message_bn="ব্যাচ BATCH-2023-এর মেয়াদ ০১-০১-২০২৬ তারিখে শেষ হয়েছে।",
            correlation_id="corr-test-1234",
        )
        payload = format_error_response(exc)

        self.assertEqual(payload["error_code"], "ERR_BATCH_EXPIRED")
        self.assertEqual(payload["correlation_id"], "corr-test-1234")
        self.assertEqual(payload["http_status"], 422)
        self.assertFalse(payload["is_retryable"], "Clinical batch expiry is never retryable")
        self.assertEqual(payload["recovery_action"], "SELECT_ALTERNATIVE_BATCH")
        self.assertIn("expired on 2026-01-01", payload["message_en"])
        self.assertIn("মেয়াদ ০১-০১-২০২৬ তারিখে শেষ হয়েছে", payload["message_bn"])
        self.assertIn("timestamp", payload)

    def test_transient_sync_timeout_is_retryable(self):
        """Network timeouts and DB locks must be flagged as retryable for outbox backoff."""
        sync_exc = SyncNetworkTimeoutError()
        payload = format_error_response(sync_exc)

        self.assertEqual(payload["error_code"], "ERR_SYNC_NETWORK_TIMEOUT")
        self.assertEqual(payload["http_status"], 503)
        self.assertTrue(payload["is_retryable"], "Sync timeout must be retryable")
        self.assertEqual(payload["recovery_action"], "RETRY_AFTER_BACKOFF")

    def test_invalid_input_and_schema_sanitization(self):
        """Acceptance Criteria: One invalid or incomplete input case."""
        # 1. Field validation failure with granular field dictionary
        val_exc = FieldValidationError(
            field_errors={
                "mrp": ["MRP cannot be less than purchase cost."],
                "box_size": ["Box size must be an integer > 0."],
            }
        )
        val_payload = format_error_response(val_exc)
        self.assertEqual(val_payload["error_code"], "ERR_VALIDATION_FAILED")
        self.assertEqual(val_payload["http_status"], 422)
        self.assertEqual(val_payload["recovery_action"], "CORRECT_INPUT_FIELDS")
        self.assertIn("mrp", val_payload["field_errors"])
        self.assertIn("box_size", val_payload["field_errors"])

        # 2. Raw unexpected database exception must be masked from client (NFR-09 security)
        raw_db_error = Exception("psycopg2.OperationalError: FATAL: password authentication failed for user 'postgres'")
        sanitized = format_error_response(raw_db_error, include_debug=False)

        self.assertEqual(sanitized["error_code"], "ERR_INTERNAL_SERVER_ERROR")
        self.assertEqual(sanitized["http_status"], 500)
        self.assertNotIn("postgres", sanitized["message_en"], "Raw DB password/schema must never leak")
        self.assertNotIn("psycopg2", sanitized["message_en"], "Underlying driver name must not leak")

    def test_unauthorized_and_cross_tenant_errors(self):
        """Acceptance Criteria: One unauthorized or cross-tenant case."""
        # 1. Cross-tenant violation
        cross_exc = CrossTenantViolationError()
        cross_payload = format_error_response(cross_exc)
        self.assertEqual(cross_payload["error_code"], "ERR_CROSS_TENANT_VIOLATION")
        self.assertEqual(cross_payload["http_status"], 403)
        self.assertFalse(cross_payload["is_retryable"])
        self.assertEqual(cross_payload["recovery_action"], "ABORT_SECURITY_INCIDENT")

        # 2. Permission denied
        perm_exc = PermissionDeniedError()
        perm_payload = format_error_response(perm_exc)
        self.assertEqual(perm_payload["error_code"], "ERR_PERMISSION_DENIED")
        self.assertEqual(perm_payload["http_status"], 403)
        self.assertEqual(perm_payload["recovery_action"], "REQUEST_MANAGER_OVERRIDE")

    def test_correlation_id_propagation_and_error_catalog(self):
        """Verifies middleware correlation ID preservation and catalog inspection."""
        # 1. Propagated X-Correlation-ID header
        res = process_api_exception(
            SyncNetworkTimeoutError(), incoming_headers={"X-Correlation-ID": "req-trace-xyz-987"}
        )
        self.assertEqual(res["correlation_id"], "req-trace-xyz-987")

        # 2. Error Catalog
        catalog = get_registered_error_catalog()
        self.assertGreaterEqual(len(catalog), 10)
        codes = [item["error_code"] for item in catalog]
        self.assertIn("ERR_AUTH_EXPIRED", codes)
        self.assertIn("ERR_BATCH_EXPIRED", codes)
        self.assertIn("ERR_SYNC_NETWORK_TIMEOUT", codes)


if __name__ == "__main__":
    unittest.main()
