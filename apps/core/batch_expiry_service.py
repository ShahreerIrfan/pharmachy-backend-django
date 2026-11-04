"""
backend/apps/core/batch_expiry_service.py
Architecture Decision: ADR-0172 (P172).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Authoritative Batch Expiry Windows & Sale Cutoff Guard Service.
Enforces multi-tier proactive expiry alert windows (Warning, Critical, Quarantine, Cutoff),
hard blocks checkout of expired or cutoff-breached medicines under DGDA standards,
and performs automated quarantine sweeps with stock availability synchronization.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchExpiryPolicy,
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.stock_availability_service import StockAvailabilityService
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

VIEW_ALLOWED_ROLES = {
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.INVENTORY_CLERK,
    UserRole.AUDITOR,
    UserRole.SUPERADMIN,
    "CASHIER",
    "STAFF_PHARMACIST",
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "INVENTORY_CLERK",
    "AUDITOR",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

MANAGE_ALLOWED_ROLES = {
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}


class BatchExpiryService:
    """
    Service managing configurable batch expiry windows, sale cutoff guards,
    and automatic quarantine state transitions.
    """

    @classmethod
    def _assert_view_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in VIEW_ALLOWED_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Role is not authorized to view batch expiry analytics.",
                    "error_code": "ERR_ROLE_UNAUTHORIZED",
                }
            )

    @classmethod
    def _assert_manage_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in MANAGE_ALLOWED_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Role is not authorized to configure batch expiry policies or trigger quarantine sweeps.",
                    "error_code": "ERR_ROLE_UNAUTHORIZED",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant: Tenant) -> None:
        if (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {
                    "detail": "Tenant subscription is suspended. Expiry operations are locked.",
                    "error_code": "ERR_TENANT_SUSPENDED",
                }
            )

    @classmethod
    def get_effective_policy(
        cls, tenant: Tenant, branch: Optional[Branch] = None
    ) -> BatchExpiryPolicy:
        """
        Retrieves the effective policy: branch-specific active policy first,
        falling back to tenant-wide default. If none exists, creates a standard default.
        """
        if branch:
            branch_policy = BatchExpiryPolicy.objects.filter(
                tenant=tenant, branch=branch, is_active=True
            ).first()
            if branch_policy:
                return branch_policy

        tenant_policy = BatchExpiryPolicy.objects.filter(
            tenant=tenant, branch__isnull=True, is_active=True
        ).first()
        if tenant_policy:
            return tenant_policy

        # Persist default policy
        return BatchExpiryPolicy.objects.create(
            policy_id=f"expol-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=None,
            warning_lead_days=90,
            critical_lead_days=30,
            quarantine_lead_days=15,
            sale_cutoff_lead_days=7,
            auto_quarantine_on_cutoff=True,
            allow_manager_override_near_expiry=False,
            is_active=True,
        )

    @classmethod
    def classify_batch_expiry(
        cls,
        batch: BatchStock,
        policy: BatchExpiryPolicy,
        as_of_date: Optional[datetime.date] = None,
    ) -> Dict[str, Any]:
        """
        Classifies batch into one of 5 deterministic expiry tiers based on remaining days.
        """
        today = as_of_date or timezone.now().date()
        days_remaining = (batch.expiry_date - today).days

        if days_remaining <= 0:
            return {
                "tier": "EXPIRED",
                "tier_name_en": "Expired (Illegal to Sell)",
                "tier_name_bn": "মেয়াদোত্তীর্ণ (বিক্রয় নিষিদ্ধ)",
                "days_remaining": days_remaining,
                "is_sale_blocked": True,
                "is_quarantine_recommended": True,
                "requires_manager_override": False,
                "severity": "DANGER",
            }
        elif days_remaining <= policy.sale_cutoff_lead_days:
            return {
                "tier": "CUTOFF_BLOCKED",
                "tier_name_en": f"Sale Cutoff Reached (<= {policy.sale_cutoff_lead_days} days)",
                "tier_name_bn": f"বিক্রয় কাট-অফ সময়সীমা উত্তীর্ণ (<= {policy.sale_cutoff_lead_days} দিন)",
                "days_remaining": days_remaining,
                "is_sale_blocked": True,
                "is_quarantine_recommended": policy.auto_quarantine_on_cutoff,
                "requires_manager_override": policy.allow_manager_override_near_expiry,
                "severity": "DANGER",
            }
        elif days_remaining <= policy.quarantine_lead_days:
            return {
                "tier": "QUARANTINE_DISPOSITION",
                "tier_name_en": f"Urgent Disposition (<= {policy.quarantine_lead_days} days)",
                "tier_name_bn": f"জরুরি স্থানান্তর / ফেরত (<= {policy.quarantine_lead_days} দিন)",
                "days_remaining": days_remaining,
                "is_sale_blocked": False,
                "is_quarantine_recommended": True,
                "requires_manager_override": False,
                "severity": "WARNING_HIGH",
            }
        elif days_remaining <= policy.critical_lead_days:
            return {
                "tier": "NEAR_EXPIRY_CRITICAL",
                "tier_name_en": f"Near Expiry (<= {policy.critical_lead_days} days)",
                "tier_name_bn": f"আসন্ন মেয়াদোত্তীর্ণ (<= {policy.critical_lead_days} দিন)",
                "days_remaining": days_remaining,
                "is_sale_blocked": False,
                "is_quarantine_recommended": False,
                "requires_manager_override": False,
                "severity": "WARNING_MEDIUM",
            }
        elif days_remaining <= policy.warning_lead_days:
            return {
                "tier": "EARLY_WARNING",
                "tier_name_en": f"Early Warning (<= {policy.warning_lead_days} days)",
                "tier_name_bn": f"প্রাথমিক সতর্কতা (<= {policy.warning_lead_days} দিন)",
                "days_remaining": days_remaining,
                "is_sale_blocked": False,
                "is_quarantine_recommended": False,
                "requires_manager_override": False,
                "severity": "INFO",
            }
        else:
            return {
                "tier": "NORMAL_SAFE",
                "tier_name_en": "Normal Safe Shelf Life",
                "tier_name_bn": "নিরাপদ মেয়াদকাল",
                "days_remaining": days_remaining,
                "is_sale_blocked": False,
                "is_quarantine_recommended": False,
                "requires_manager_override": False,
                "severity": "SAFE",
            }

    @classmethod
    def verify_sale_cutoff(cls, user: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        POS verification before checkout to ensure the selected batch passes expiry cutoffs.
        """
        cls._assert_view_role(user)

        tenant_id = params.get("tenant_id") or getattr(user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant_id": "tenant_id is required.", "error_code": "ERR_MISSING_TENANT"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})
        cls._assert_tenant_active(tenant)

        batch_id = params.get("batch_id")
        if not batch_id:
            raise ValidationError({"batch_id": "batch_id is required.", "error_code": "ERR_MISSING_BATCH"})

        batch = BatchStock.objects.select_related("product", "branch", "tenant").filter(
            batch_id=batch_id, tenant=tenant
        ).first()
        if not batch:
            raise NotFound({"detail": "Batch not found.", "error_code": "ERR_BATCH_NOT_FOUND"})

        policy = cls.get_effective_policy(tenant, batch.branch)
        classification = cls.classify_batch_expiry(batch, policy)

        # DGDA Statutory Hard Block (FR-019):
        if classification["tier"] == "EXPIRED":
            raise ValidationError(
                {
                    "detail": "Batch has expired. Dispensing is strictly illegal under DGDA regulations.",
                    "error_code": "ERR_BATCH_EXPIRED",
                    "expiry_date": batch.expiry_date.isoformat(),
                    "days_remaining": classification["days_remaining"],
                }
            )

        # Sale Cutoff Guard:
        is_override_requested = bool(params.get("manager_override", False))
        if classification["tier"] == "CUTOFF_BLOCKED":
            if is_override_requested and policy.allow_manager_override_near_expiry:
                role = getattr(user, "role", None)
                if role not in MANAGE_ALLOWED_ROLES:
                    raise PermissionDenied(
                        {
                            "detail": "Manager authorization required to override sale cutoff.",
                            "error_code": "ERR_MANAGER_OVERRIDE_REQUIRED",
                        }
                    )
            else:
                raise ValidationError(
                    {
                        "detail": f"Batch has breached branch sale cutoff ({policy.sale_cutoff_lead_days} days). Sale blocked.",
                        "error_code": "ERR_SALE_CUTOFF_BREACHED",
                        "cutoff_days": policy.sale_cutoff_lead_days,
                        "days_remaining": classification["days_remaining"],
                    }
                )

        return {
            "status": "APPROVED",
            "batch_id": batch.batch_id,
            "batch_number": batch.batch_number,
            "product_name": batch.product.brand_name,
            "expiry_date": batch.expiry_date.isoformat(),
            "days_remaining": classification["days_remaining"],
            "tier": classification["tier"],
            "tier_name_en": classification["tier_name_en"],
            "tier_name_bn": classification["tier_name_bn"],
            "manager_override_applied": is_override_requested and classification["tier"] == "CUTOFF_BLOCKED",
        }

    @classmethod
    @transaction.atomic
    def scan_and_quarantine_expired_batches(
        cls, user: Any, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Executes an authoritative sweep over branch batches:
        - Expired batches (<= today) -> status=EXPIRED, is_quarantined=True.
        - Cutoff breached batches (<= cutoff days) -> is_quarantined=True (if policy.auto_quarantine_on_cutoff).
        - Recalculates BranchStockAvailability for affected products.
        - Records immutable AuditLogEntry for DGDA compliance.
        """
        cls._assert_manage_role(user)

        tenant_id = data.get("tenant_id") or getattr(user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant_id": "tenant_id is required.", "error_code": "ERR_MISSING_TENANT"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})
        cls._assert_tenant_active(tenant)

        branch_id = data.get("branch_id")
        if not branch_id:
            raise ValidationError({"branch_id": "branch_id is required.", "error_code": "ERR_MISSING_BRANCH"})
        branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
        if not branch:
            raise NotFound({"detail": "Branch not found.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        policy = cls.get_effective_policy(tenant, branch)
        today = timezone.now().date()

        batches_to_inspect = BatchStock.objects.select_for_update().filter(
            tenant=tenant, branch=branch, is_active=True
        )

        expired_count = 0
        cutoff_quarantined_count = 0
        affected_product_ids = set()

        for b in batches_to_inspect:
            days_rem = (b.expiry_date - today).days

            # 1. Past Expiration Date -> Hard Expired
            if days_rem <= 0 and (b.status != BatchStatus.EXPIRED or not b.is_quarantined):
                b.status = BatchStatus.EXPIRED
                b.is_quarantined = True
                b.save(update_fields=["status", "is_quarantined", "updated_at"])
                expired_count += 1
                affected_product_ids.add(b.product_id)

            # 2. Cutoff Breached -> Auto Quarantine if configured
            elif (
                0 < days_rem <= policy.sale_cutoff_lead_days
                and policy.auto_quarantine_on_cutoff
                and not b.is_quarantined
            ):
                b.is_quarantined = True
                b.save(update_fields=["is_quarantined", "updated_at"])
                cutoff_quarantined_count += 1
                affected_product_ids.add(b.product_id)

        # Recalculate BranchStockAvailability for all affected products
        for pid in affected_product_ids:
            prod = MedicineProduct.objects.filter(product_id=pid).first()
            if prod:
                StockAvailabilityService.recalculate_availability(tenant, branch, prod, as_of_date=today)

        # Audit log for DGDA record
        try:
            AuditLogEntry.objects.create(
                tenant=tenant,
                branch=branch,
                actor=user if hasattr(user, "pk") and user.pk else None,
                actor_email=getattr(user, "email", "system"),
                actor_role=getattr(user, "role", "BRANCH_MANAGER"),
                action="BATCH_EXPIRY_QUARANTINE_SWEEP",
                entity_type="BatchStock",
                entity_id=branch.branch_id,
                status=AuditActionStatus.SUCCESS,
                details={
                    "branch_id": branch.branch_id,
                    "expired_batches_count": expired_count,
                    "cutoff_quarantined_count": cutoff_quarantined_count,
                    "affected_products_count": len(affected_product_ids),
                },
            )
        except Exception as audit_err:
            logger.warning("Audit log recording failed: %s", audit_err)

        return {
            "status": "SUCCESS",
            "branch_id": branch.branch_id,
            "expired_batches_count": expired_count,
            "cutoff_quarantined_count": cutoff_quarantined_count,
            "affected_products_count": len(affected_product_ids),
            "executed_at": timezone.now().isoformat(),
        }

    @classmethod
    def get_expiry_dashboard(
        cls, user: Any, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Returns tiered expiry analytics, total stock units, and unblended GAAP cost
        valuation at risk per tier for the branch.
        """
        cls._assert_view_role(user)

        tenant_id = params.get("tenant_id") or getattr(user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant_id": "tenant_id is required.", "error_code": "ERR_MISSING_TENANT"})

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != UserRole.SUPERADMIN:
            raise PermissionDenied({"detail": "Cross-tenant access forbidden.", "error_code": "ERR_TENANT_MISMATCH"})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": "Tenant not found.", "error_code": "ERR_TENANT_NOT_FOUND"})
        cls._assert_tenant_active(tenant)

        branch_id = params.get("branch_id")
        if not branch_id:
            raise ValidationError({"branch_id": "branch_id is required.", "error_code": "ERR_MISSING_BRANCH"})
        branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
        if not branch:
            raise NotFound({"detail": "Branch not found.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        policy = cls.get_effective_policy(tenant, branch)
        today = timezone.now().date()

        batches = BatchStock.objects.select_related("product").filter(
            tenant=tenant, branch=branch, is_active=True
        ).order_by("expiry_date")

        tier_counts = {
            "EXPIRED": {"batch_count": 0, "total_units": 0, "valuation_cost": Decimal("0.00")},
            "CUTOFF_BLOCKED": {"batch_count": 0, "total_units": 0, "valuation_cost": Decimal("0.00")},
            "QUARANTINE_DISPOSITION": {"batch_count": 0, "total_units": 0, "valuation_cost": Decimal("0.00")},
            "NEAR_EXPIRY_CRITICAL": {"batch_count": 0, "total_units": 0, "valuation_cost": Decimal("0.00")},
            "EARLY_WARNING": {"batch_count": 0, "total_units": 0, "valuation_cost": Decimal("0.00")},
            "NORMAL_SAFE": {"batch_count": 0, "total_units": 0, "valuation_cost": Decimal("0.00")},
        }

        detailed_batches = []
        for b in batches:
            classification = cls.classify_batch_expiry(b, policy, as_of_date=today)
            t = classification["tier"]
            cost = b.cost_per_unit * Decimal(b.quantity_base_units)

            tier_counts[t]["batch_count"] += 1
            tier_counts[t]["total_units"] += b.quantity_base_units
            tier_counts[t]["valuation_cost"] += cost

            detailed_batches.append(
                {
                    "batch_id": b.batch_id,
                    "batch_number": b.batch_number,
                    "product_id": b.product.product_id,
                    "product_name": b.product.brand_name,
                    "expiry_date": b.expiry_date.isoformat(),
                    "days_remaining": classification["days_remaining"],
                    "quantity_base_units": b.quantity_base_units,
                    "unit_cost": str(b.cost_per_unit),
                    "total_cost": str(cost),
                    "tier": t,
                    "tier_name_en": classification["tier_name_en"],
                    "tier_name_bn": classification["tier_name_bn"],
                    "is_sale_blocked": classification["is_sale_blocked"],
                    "is_quarantined": b.is_quarantined,
                    "status": b.status,
                }
            )

        # Convert Decimals to string
        formatted_summary = {}
        for k, v in tier_counts.items():
            formatted_summary[k] = {
                "batch_count": v["batch_count"],
                "total_units": v["total_units"],
                "valuation_cost": str(v["valuation_cost"].quantize(Decimal("0.01"))),
            }

        return {
            "tenant_id": tenant.tenant_id,
            "branch_id": branch.branch_id,
            "policy": {
                "policy_id": policy.policy_id,
                "warning_lead_days": policy.warning_lead_days,
                "critical_lead_days": policy.critical_lead_days,
                "quarantine_lead_days": policy.quarantine_lead_days,
                "sale_cutoff_lead_days": policy.sale_cutoff_lead_days,
                "auto_quarantine_on_cutoff": policy.auto_quarantine_on_cutoff,
                "allow_manager_override_near_expiry": policy.allow_manager_override_near_expiry,
            },
            "summary": formatted_summary,
            "batches": detailed_batches,
            "as_of_date": today.isoformat(),
        }

