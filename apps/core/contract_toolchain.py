"""
apps/core/contract_toolchain.py
Architecture Decision: ADR-0001, ADR-0014, ADR-0021 & ADR-0025
Phase 02: Repository, Toolchain & Engineering Automation

Comprehensive contract validation tooling for OpenAPI 3.1 and JSON Schemas:
- Validates OpenAPI 3.1 specification integrity and endpoint security declarations.
- Validates standalone JSON Schema Draft 2020-12 files in contracts/schemas/.
- Enforces strict Decimal monetary string transmission (preventing float drift).
- Verifies cross-platform DTO parity across Python DRF, JavaFX, and Flutter.
- Enforces multi-tenant and RBAC security boundaries.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator
from referencing import Registry, Resource


class ContractValidationError(Exception):
    """Raised when an API payload, schema, or DTO violates contract invariants."""

    pass


class OpenApiSpecValidator:
    """Validates the canonical OpenAPI 3.1 specification."""

    @classmethod
    def validate_spec_file(cls, spec_path: Path | str) -> dict[str, Any]:
        p = Path(spec_path).resolve()
        if not p.is_file():
            raise ContractValidationError(f"OpenAPI specification not found at: {p}")

        try:
            with open(p, encoding="utf-8") as f:
                spec = json.load(f)
        except json.JSONDecodeError as e:
            raise ContractValidationError(f"Malformed JSON in OpenAPI spec {p.name}: {e}")

        # Check OpenAPI 3.1.x
        version = spec.get("openapi", "")
        if not version.startswith("3.1."):
            raise ContractValidationError(f"OpenAPI specification version must be 3.1.x (ADR-0025). Got: '{version}'")

        # Check Info & Servers
        if "info" not in spec or "title" not in spec["info"]:
            raise ContractValidationError("OpenAPI spec missing mandatory 'info.title'.")
        if not spec.get("servers"):
            raise ContractValidationError("OpenAPI spec missing server definitions.")

        # Check Mandatory Paths
        required_paths = [
            "/auth/token/",
            "/sales/invoices/",
            "/sync/replay/",
            "/dashboard/kpi/",
            "/medicines/",
            "/prescriptions/verify/",
        ]
        missing_paths = [p for p in required_paths if p not in spec.get("paths", {})]
        if missing_paths:
            raise ContractValidationError(f"OpenAPI spec missing mandatory endpoints: {', '.join(missing_paths)}")

        # Check Security Schemes (BearerAuth & TenantHeader)
        security_schemes = spec.get("components", {}).get("securitySchemes", {})
        if "BearerAuth" not in security_schemes:
            raise ContractValidationError("OpenAPI spec missing components.securitySchemes.BearerAuth")
        if "TenantHeader" not in security_schemes:
            raise ContractValidationError("OpenAPI spec missing components.securitySchemes.TenantHeader")

        return {
            "status": "VALID_OPENAPI_3_1",
            "title": spec["info"]["title"],
            "version": spec["info"]["version"],
            "paths_count": len(spec["paths"]),
            "schemas_count": len(spec.get("components", {}).get("schemas", {})),
        }


class JsonSchemaRepositoryValidator:
    """Validates standalone JSON Schema Draft 2020-12 documents and tests payloads."""

    MANDATORY_SCHEMAS = [
        "sale_invoice.schema.json",
        "sync_replay.schema.json",
        "dashboard_kpi.schema.json",
        "medicine_catalog.schema.json",
        "prescription_verify.schema.json",
        "error_response.schema.json",
    ]

    def __init__(self, schemas_dir: Path | str):
        self.schemas_dir = Path(schemas_dir).resolve()
        self._loaded_schemas: dict[str, dict[str, Any]] = {}
        self._registry = Registry()
        self._load_all_schemas()

    def _load_all_schemas(self) -> None:
        if not self.schemas_dir.is_dir():
            raise ContractValidationError(f"Schemas directory not found at: {self.schemas_dir}")

        for sname in self.MANDATORY_SCHEMAS:
            fpath = self.schemas_dir / sname
            if not fpath.is_file():
                raise ContractValidationError(f"Mandatory schema '{sname}' not found in {self.schemas_dir}")
            try:
                with open(fpath, encoding="utf-8") as f:
                    schema_data = json.load(f)
                # Verify that the schema itself is a valid Draft 2020-12 schema
                Draft202012Validator.check_schema(schema_data)
                self._loaded_schemas[sname] = schema_data
                resource = Resource.from_contents(schema_data)
                self._registry = self._registry.with_resource(sname, resource)
                if "$id" in schema_data:
                    self._registry = self._registry.with_resource(schema_data["$id"], resource)
            except Exception as e:
                raise ContractValidationError(f"Invalid JSON Schema in '{sname}': {e}")

    def get_schema(self, schema_name: str) -> dict[str, Any]:
        if schema_name not in self._loaded_schemas:
            raise ContractValidationError(
                f"Unknown schema '{schema_name}'. Available: {list(self._loaded_schemas.keys())}"
            )
        return self._loaded_schemas[schema_name]

    def validate_payload(self, schema_name: str, payload: dict[str, Any]) -> None:
        """Validates payload against schema, checking for float drift and schema violations."""
        # 1. Enforce Monetary Float Drift Invariant
        MonetaryFloatDriftDetector.assert_no_float_money(payload)

        # 2. Enforce JSON Schema
        schema = self.get_schema(schema_name)
        try:
            validator = Draft202012Validator(schema, registry=self._registry)
            errors = list(validator.iter_errors(payload))
            if errors:
                err_msgs = [f"[{e.json_path}] {e.message}" for e in errors]
                raise ContractValidationError(f"Schema validation failed for '{schema_name}':\n" + "\n".join(err_msgs))
        except jsonschema.ValidationError as e:
            raise ContractValidationError(f"Schema validation error in '{schema_name}': {e.message}")


class MonetaryFloatDriftDetector:
    """
    Enforces the strict rule that all monetary amounts must be transmitted as exact
    Decimal strings (e.g. '45.25') rather than floating-point numbers.
    """

    MONEY_FIELDS = {
        "unit_price",
        "line_total",
        "subtotal",
        "vat_amount",
        "discount_amount",
        "grand_total",
        "today_sales_bdt",
        "amount",
        "cash_tendered",
        "change_amount",
    }

    DECIMAL_REGEX = re.compile(r"^-?\d+\.\d{2}$")

    @classmethod
    def assert_no_float_money(cls, obj: Any, path: str = "root") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                current_path = f"{path}.{k}"
                if k in cls.MONEY_FIELDS:
                    if isinstance(v, (float, int)):
                        raise ContractValidationError(
                            f"Monetary float drift violation at '{current_path}': value {v!r} is of type {type(v).__name__}. "
                            f"All money amounts must be transmitted as exact 2-decimal strings (e.g. '12.50') (ADR-0001, ADR-0025)."
                        )
                    if isinstance(v, str) and not cls.DECIMAL_REGEX.match(v):
                        raise ContractValidationError(
                            f"Monetary format violation at '{current_path}': '{v}' does not match pattern '^\\d+\\.\\d{{2}}$'."
                        )
                cls.assert_no_float_money(v, current_path)
        elif isinstance(obj, list):
            for idx, item in enumerate(obj):
                cls.assert_no_float_money(item, f"{path}[{idx}]")


class CrossPlatformDtoParityValidator:
    """
    Verifies that fields declared across Python, Java, and Dart DTOs match contract schemas.
    """

    @staticmethod
    def _to_camel(s: str) -> str:
        return re.sub(r"_([a-z])", lambda m: m.group(1).upper(), s)

    @classmethod
    def verify_java_sale_invoice_parity(cls, schema: dict[str, Any], java_fields: set[str]) -> None:
        required_schema_fields = set(schema.get("required", []))
        # Mapping snake_case schema to camelCase Java fields
        expected_camel = {cls._to_camel(f) for f in required_schema_fields}

        missing = expected_camel - java_fields
        if missing:
            raise ContractValidationError(f"Java SaleTransaction DTO is missing required contract fields: {missing}")

    @classmethod
    def verify_flutter_dashboard_kpi_parity(cls, schema: dict[str, Any], dart_fields: set[str]) -> None:
        required_schema_fields = set(schema.get("required", []))
        expected_camel = {cls._to_camel(f) for f in required_schema_fields}

        missing = expected_camel - dart_fields
        if missing:
            raise ContractValidationError(f"Dart DashboardKpi DTO is missing required contract fields: {missing}")


class ContractSecurityValidator:
    """
    Validates tenant isolation headers and endpoint RBAC permission requirements.
    """

    ENDPOINT_PERMISSIONS: dict[str, set[str]] = {
        "/api/v2/sales/invoices/": {"CASHIER", "BRANCH_MANAGER", "TENANT_OWNER"},
        "/api/v2/dashboard/kpi/": {"TENANT_OWNER", "BRANCH_MANAGER", "PLATFORM_SUPER_ADMIN"},
        "/api/v2/prescriptions/verify/": {"PHARMACIST", "PLATFORM_SUPER_ADMIN"},
        "/api/v2/sync/replay/": {"CASHIER", "BRANCH_MANAGER", "DEVICE_AUTHENTICATED"},
    }

    @classmethod
    def assert_authorized(cls, endpoint: str, role: str, tenant_header: str | None) -> None:
        if not tenant_header or not tenant_header.strip():
            raise ContractValidationError(
                f"Missing mandatory multi-tenant header 'X-Tenant-ID' on request to {endpoint} (ADR-0025)."
            )

        allowed_roles = cls.ENDPOINT_PERMISSIONS.get(endpoint)
        if allowed_roles and role not in allowed_roles:
            raise PermissionError(
                f"Access Denied: Role '{role}' is not authorized to access endpoint '{endpoint}'. "
                f"Allowed roles: {sorted(list(allowed_roles))} (HTTP 403 Forbidden)."
            )
