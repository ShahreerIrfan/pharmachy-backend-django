"""
Standardized Error Vocabulary, Correlation IDs & Actionable Feedback Language
Architecture Decision: ADR-0014
Provides machine error codes, correlation tracing, bilingual messages, and retryability flags.
"""

import datetime
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class RecoveryAction(str, Enum):
    REAUTHENTICATE = "REAUTHENTICATE"
    REQUEST_MANAGER_OVERRIDE = "REQUEST_MANAGER_OVERRIDE"
    SELECT_ALTERNATIVE_BATCH = "SELECT_ALTERNATIVE_BATCH"
    QUARANTINE_HOLD = "QUARANTINE_HOLD"
    ADJUST_QUANTITY = "ADJUST_QUANTITY"
    SUBMIT_RX_VERIFICATION = "SUBMIT_RX_VERIFICATION"
    RETRY_AFTER_BACKOFF = "RETRY_AFTER_BACKOFF"
    CONNECT_FOR_LEASE_REFRESH = "CONNECT_FOR_LEASE_REFRESH"
    ABORT_SECURITY_INCIDENT = "ABORT_SECURITY_INCIDENT"
    CORRECT_INPUT_FIELDS = "CORRECT_INPUT_FIELDS"


@dataclass(frozen=True)
class ErrorPayload:
    error_code: str
    correlation_id: str
    http_status: int
    is_retryable: bool
    message_en: str
    message_bn: str
    recovery_action: RecoveryAction
    field_errors: dict[str, list[str]] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["recovery_action"] = self.recovery_action.value
        return data


class PharmacyBaseException(Exception):
    """Base class for all business and operational exceptions."""

    error_code: str = "ERR_INTERNAL_ERROR"
    http_status: int = 500
    is_retryable: bool = False
    recovery_action: RecoveryAction = RecoveryAction.RETRY_AFTER_BACKOFF
    default_message_en: str = "An unexpected error occurred."
    default_message_bn: str = "একটি অপ্রত্যাশিত ত্রুটি ঘটেছে।"

    def __init__(
        self,
        message_en: str | None = None,
        message_bn: str | None = None,
        field_errors: dict[str, list[str]] | None = None,
        correlation_id: str | None = None,
    ):
        self.message_en = message_en or self.default_message_en
        self.message_bn = message_bn or self.default_message_bn
        self.field_errors = field_errors or {}
        self.correlation_id = correlation_id or str(uuid.uuid4())
        super().__init__(self.message_en)

    def to_payload(self) -> ErrorPayload:
        return ErrorPayload(
            error_code=self.error_code,
            correlation_id=self.correlation_id,
            http_status=self.http_status,
            is_retryable=self.is_retryable,
            message_en=self.message_en,
            message_bn=self.message_bn,
            recovery_action=self.recovery_action,
            field_errors=self.field_errors,
        )


class AuthenticationExpiredError(PharmacyBaseException):
    error_code = "ERR_AUTH_EXPIRED"
    http_status = 401
    is_retryable = False
    recovery_action = RecoveryAction.REAUTHENTICATE
    default_message_en = "Your authentication session has expired. Please log in again."
    default_message_bn = "আপনার লগইন সেশনের মেয়াদ শেষ হয়েছে। অনুগ্রহ করে পুনরায় লগইন করুন।"


class PermissionDeniedError(PharmacyBaseException):
    error_code = "ERR_PERMISSION_DENIED"
    http_status = 403
    is_retryable = False
    recovery_action = RecoveryAction.REQUEST_MANAGER_OVERRIDE
    default_message_en = "You do not have permission to perform this action."
    default_message_bn = "এই কাজটি সম্পাদন করার অনুমতি আপনার নেই।"


class CrossTenantViolationError(PharmacyBaseException):
    error_code = "ERR_CROSS_TENANT_VIOLATION"
    http_status = 403
    is_retryable = False
    recovery_action = RecoveryAction.ABORT_SECURITY_INCIDENT
    default_message_en = "Access to another tenant's resource is strictly forbidden."
    default_message_bn = "অন্য ফার্মেসির তথ্যে প্রবেশাধিকার কঠোরভাবে নিষিদ্ধ।"


class BatchExpiredError(PharmacyBaseException):
    error_code = "ERR_BATCH_EXPIRED"
    http_status = 422
    is_retryable = False
    recovery_action = RecoveryAction.SELECT_ALTERNATIVE_BATCH
    default_message_en = "The selected medicine batch has expired and cannot be dispensed."
    default_message_bn = "নির্বাচিত ওষুধের ব্যাচের মেয়াদ উত্তীর্ণ হয়েছে এবং এটি বিক্রি করা যাবে না।"


class BatchQuarantinedError(PharmacyBaseException):
    error_code = "ERR_BATCH_QUARANTINED"
    http_status = 422
    is_retryable = False
    recovery_action = RecoveryAction.QUARANTINE_HOLD
    default_message_en = "This batch is under quarantine or recalled and cannot be sold."
    default_message_bn = "এই ব্যাচটি কোয়ারেন্টাইন বা প্রত্যাহৃত অবস্থায় আছে এবং এটি বিক্রি করা নিষিদ্ধ।"


class InsufficientStockError(PharmacyBaseException):
    error_code = "ERR_INSUFFICIENT_STOCK"
    http_status = 422
    is_retryable = False
    recovery_action = RecoveryAction.ADJUST_QUANTITY
    default_message_en = "Requested quantity exceeds available batch stock."
    default_message_bn = "অনুরোধকৃত পরিমাণ বিদ্যমান ব্যাচ স্টকের চেয়ে বেশি।"


class PrescriptionVerificationRequiredError(PharmacyBaseException):
    error_code = "ERR_RX_VERIFICATION_REQUIRED"
    http_status = 422
    is_retryable = False
    recovery_action = RecoveryAction.SUBMIT_RX_VERIFICATION
    default_message_en = "Prescription verification by a licensed pharmacist is required before dispensing."
    default_message_bn = "বিক্রির পূর্বে নিবন্ধিত ফার্মাসিস্ট কর্তৃক প্রেসক্রিপশন যাচাইকরণ আবশ্যক।"


class SyncNetworkTimeoutError(PharmacyBaseException):
    error_code = "ERR_SYNC_NETWORK_TIMEOUT"
    http_status = 503
    is_retryable = True
    recovery_action = RecoveryAction.RETRY_AFTER_BACKOFF
    default_message_en = "Network connection timed out during cloud sync. Retrying with exponential backoff."
    default_message_bn = "ক্লাউড সিঙ্কের সময় সংযোগ বিচ্ছিন্ন হয়েছে। স্বয়ংক্রিয়ভাবে পুনরায় চেষ্টা করা হচ্ছে।"


class DatabaseLockTimeoutError(PharmacyBaseException):
    error_code = "ERR_DB_LOCK_TIMEOUT"
    http_status = 503
    is_retryable = True
    recovery_action = RecoveryAction.RETRY_AFTER_BACKOFF
    default_message_en = "Temporary concurrent database lock. Safe to retry."
    default_message_bn = "একই সাথে অন্য লেনদেন চলায় সাময়িক বিলম্ব। পুনরায় চেষ্টা করুন।"


class OfflineLeaseExpiredError(PharmacyBaseException):
    error_code = "ERR_OFFLINE_LEASE_EXPIRED"
    http_status = 403
    is_retryable = False
    recovery_action = RecoveryAction.CONNECT_FOR_LEASE_REFRESH
    default_message_en = "Offline device lease has expired (72h limit). Please connect to internet to renew."
    default_message_bn = "অফলাইন ডিভাইসের লাইসেন্স মেয়াদ উত্তীর্ণ (৭২ ঘণ্টা সীমা)। নবায়নের জন্য ইন্টারনেটে সংযুক্ত হন।"


class FieldValidationError(PharmacyBaseException):
    error_code = "ERR_VALIDATION_FAILED"
    http_status = 422
    is_retryable = False
    recovery_action = RecoveryAction.CORRECT_INPUT_FIELDS
    default_message_en = "One or more input fields failed validation."
    default_message_bn = "এক বা একাধিক তথ্যের মান সঠিক নয়।"


# Canonical registry of all specialized business exceptions
CANONICAL_EXCEPTION_REGISTRY: list[type[PharmacyBaseException]] = [
    AuthenticationExpiredError,
    PermissionDeniedError,
    CrossTenantViolationError,
    BatchExpiredError,
    BatchQuarantinedError,
    InsufficientStockError,
    PrescriptionVerificationRequiredError,
    SyncNetworkTimeoutError,
    DatabaseLockTimeoutError,
    OfflineLeaseExpiredError,
    FieldValidationError,
]


def get_registered_error_catalog() -> list[dict[str, Any]]:
    """Returns catalog of all registered machine error codes and recovery guidance."""
    return [
        {
            "error_code": cls.error_code,
            "http_status": cls.http_status,
            "is_retryable": cls.is_retryable,
            "recovery_action": cls.recovery_action.value,
            "default_message_en": cls.default_message_en,
            "default_message_bn": cls.default_message_bn,
        }
        for cls in CANONICAL_EXCEPTION_REGISTRY
    ]


def format_error_response(
    exc: Exception,
    correlation_id: str | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Formats any exception into safe, client-facing JSON structure without leaking internal schemas."""
    cid = correlation_id or str(uuid.uuid4())

    if isinstance(exc, PharmacyBaseException):
        payload = exc.to_payload()
        res = payload.to_dict()
        if correlation_id:
            res["correlation_id"] = correlation_id
        return res

    # Generic or untrusted exception: sanitize and mask from external disclosure (NFR-09 & NFR-10)
    safe_payload = ErrorPayload(
        error_code="ERR_INTERNAL_SERVER_ERROR",
        correlation_id=cid,
        http_status=500,
        is_retryable=False,
        message_en="An internal server error occurred. Please contact support with the correlation ID.",
        message_bn="একটি অভ্যন্তরীণ সমস্যা দেখা দিয়েছে। অনুগ্রহ করে সাপোর্ট টিমের সাথে যোগাযোগ করুন।",
        recovery_action=RecoveryAction.RETRY_AFTER_BACKOFF,
        field_errors={},
    )
    res = safe_payload.to_dict()
    if include_debug:
        res["debug_detail"] = str(exc)
    return res


def process_api_exception(
    exc: Exception,
    incoming_headers: dict[str, str] | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Middleware entry point: extracts X-Correlation-ID from headers or generates a new trace UUID."""
    headers = incoming_headers or {}
    correlation_id = headers.get("X-Correlation-ID") or headers.get("x-correlation-id") or str(uuid.uuid4())
    return format_error_response(exc, correlation_id=correlation_id, include_debug=include_debug)
