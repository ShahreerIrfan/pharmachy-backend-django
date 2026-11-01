"""
backend/apps/core/feature_flag_service.py
Architecture Decision: ADR-0004, ADR-0005 & ADR-0072
Server-Authoritative Platform Feature Flag Service.
Evaluates flags with priority order:
1. Kill Switch (Highest priority, global disable)
2. Tenant-Specific Targeting Override
3. Subscription Plan Tier Gate
4. Deterministic Percentage Rollout
5. Global Enablement Toggle
6. Safe Default Fallback
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Dict, Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    PlanTier,
    PlatformFeatureFlag,
    Tenant,
    TenantFeatureFlagOverride,
)
from apps.identity.models import PharmacyUser

PLAN_TIER_RANK: Dict[str, int] = {
    PlanTier.BASIC: 1,
    PlanTier.STANDARD: 2,
    PlanTier.PRO: 3,
}


class FeatureFlagService:
    """
    Central server-authoritative evaluation and lifecycle service for platform feature flags.
    """

    @classmethod
    def serialize_flag(cls, flag: PlatformFeatureFlag) -> Dict[str, Any]:
        """Serializes a PlatformFeatureFlag instance conforming to schema contract."""
        return {
            "flag_key": flag.flag_key,
            "name": flag.name,
            "description": flag.description or "",
            "is_enabled_globally": flag.is_enabled_globally,
            "default_enabled": flag.default_enabled,
            "rollout_percentage": flag.rollout_percentage,
            "min_plan_tier": flag.min_plan_tier,
            "is_kill_switch_active": flag.is_kill_switch_active,
            "kill_switch_reason": flag.kill_switch_reason or "",
            "kill_switch_tripped_at": flag.kill_switch_tripped_at.isoformat() if flag.kill_switch_tripped_at else None,
            "overrides_count": flag.tenant_overrides.count(),
            "created_at": flag.created_at.isoformat() if flag.created_at else None,
            "updated_at": flag.updated_at.isoformat() if flag.updated_at else None,
        }

    @classmethod
    def serialize_override(cls, override: TenantFeatureFlagOverride) -> Dict[str, Any]:
        """Serializes a TenantFeatureFlagOverride instance."""
        return {
            "override_id": override.override_id,
            "flag_key": override.flag_id,
            "tenant_id": override.tenant_id,
            "is_enabled": override.is_enabled,
            "notes": override.notes or "",
            "created_at": override.created_at.isoformat() if override.created_at else None,
            "updated_at": override.updated_at.isoformat() if override.updated_at else None,
        }

    @classmethod
    def evaluate_flag(
        cls,
        flag_key: str,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Server-authoritative evaluation of a single feature flag.
        Returns resolution dictionary with is_enabled boolean and decision reason.
        """
        clean_key = str(flag_key or "").strip().lower()
        flag = PlatformFeatureFlag.objects.filter(flag_key=clean_key).first()

        if not flag:
            return {
                "flag_key": clean_key,
                "is_enabled": False,
                "is_kill_switch_active": False,
                "reason": "FLAG_NOT_FOUND",
            }

        # Priority 1: Emergency Kill Switch
        if flag.is_kill_switch_active:
            return {
                "flag_key": clean_key,
                "is_enabled": False,
                "is_kill_switch_active": True,
                "reason": "KILL_SWITCH_ACTIVE",
            }

        # If tenant context is provided:
        if tenant_id and str(tenant_id).strip() not in ["", "platform-global"]:
            clean_tenant = str(tenant_id).strip()

            # Priority 2: Explicit Tenant Override
            override = TenantFeatureFlagOverride.objects.filter(flag=flag, tenant_id=clean_tenant).first()
            if override is not None:
                return {
                    "flag_key": clean_key,
                    "is_enabled": override.is_enabled,
                    "is_kill_switch_active": False,
                    "reason": "TENANT_OVERRIDE",
                }

            # Priority 3: Subscription Plan Tier Gating
            tenant = Tenant.objects.filter(tenant_id=clean_tenant).first()
            if tenant:
                tenant_rank = PLAN_TIER_RANK.get(tenant.plan_tier, 1)
                min_rank = PLAN_TIER_RANK.get(flag.min_plan_tier, 1)
                if tenant_rank < min_rank:
                    return {
                        "flag_key": clean_key,
                        "is_enabled": False,
                        "is_kill_switch_active": False,
                        "reason": "PLAN_TIER_INSUFFICIENT",
                    }

            # Priority 4: Gradual Percentage Rollout (Deterministic Hashing)
            if 0 < flag.rollout_percentage < 100:
                hash_input = f"{clean_key}:{clean_tenant}".encode("utf-8")
                hash_bucket = int(hashlib.md5(hash_input).hexdigest(), 16) % 100
                is_in_bucket = hash_bucket < flag.rollout_percentage
                return {
                    "flag_key": clean_key,
                    "is_enabled": is_in_bucket,
                    "is_kill_switch_active": False,
                    "reason": "GRADUAL_ROLLOUT" if is_in_bucket else "GRADUAL_ROLLOUT_EXCLUDED",
                }

        # Priority 5: Global Enablement
        if flag.is_enabled_globally:
            return {
                "flag_key": clean_key,
                "is_enabled": True,
                "is_kill_switch_active": False,
                "reason": "GLOBAL_ENABLED",
            }

        # Priority 6: Safe Default
        return {
            "flag_key": clean_key,
            "is_enabled": flag.default_enabled,
            "is_kill_switch_active": False,
            "reason": "SAFE_DEFAULT",
        }

    @classmethod
    def evaluate_all_flags_for_tenant(cls, tenant_id: str) -> Dict[str, Any]:
        """
        Evaluates all registered feature flags for a specific pharmacy tenant.
        Returns map of {flag_key: bool} and evaluation metadata.
        """
        flags = PlatformFeatureFlag.objects.all().prefetch_related("tenant_overrides")
        evaluated_map: Dict[str, bool] = {}
        details: Dict[str, Dict[str, Any]] = {}

        for flag in flags:
            eval_res = cls.evaluate_flag(flag.flag_key, tenant_id=tenant_id)
            evaluated_map[flag.flag_key] = eval_res["is_enabled"]
            details[flag.flag_key] = eval_res

        return {
            "tenant_id": tenant_id,
            "flags": evaluated_map,
            "details": details,
            "evaluated_at": timezone.now().isoformat(),
        }

    @classmethod
    @transaction.atomic
    def set_kill_switch(
        cls,
        flag_key: str,
        is_active: bool,
        actor: PharmacyUser,
        reason: str = "",
    ) -> Dict[str, Any]:
        """
        Trips or resets emergency kill switch for a feature flag.
        Logs an immutable security audit event.
        """
        clean_key = str(flag_key or "").strip().lower()
        flag = PlatformFeatureFlag.objects.filter(flag_key=clean_key).first()
        if not flag:
            raise ValidationError({"flag_key": f"Feature flag '{clean_key}' does not exist."})

        flag.is_kill_switch_active = bool(is_active)
        if is_active:
            flag.kill_switch_reason = str(reason or "").strip() or "Emergency kill switch activated by operator."
            flag.kill_switch_tripped_at = timezone.now()
            flag.kill_switch_tripped_by = actor
        else:
            flag.kill_switch_reason = ""
            flag.kill_switch_tripped_at = None
            flag.kill_switch_tripped_by = None
        flag.updated_by = actor
        flag.save()

        action_name = "FEATURE_FLAG_KILL_SWITCH_TRIPPED" if is_active else "FEATURE_FLAG_KILL_SWITCH_RESET"
        AuditLogEntry.objects.create(
            action=action_name,
            resource_type="FEATURE_FLAG",
            resource_id=flag.flag_key,
            tenant_id="platform-global",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email,
                "flag_key": flag.flag_key,
                "is_kill_switch_active": flag.is_kill_switch_active,
                "reason": flag.kill_switch_reason,
            },
        )
        return cls.serialize_flag(flag)

    @classmethod
    @transaction.atomic
    def upsert_tenant_override(
        cls,
        flag_key: str,
        tenant_id: str,
        is_enabled: bool,
        actor: PharmacyUser,
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Targets or excludes a specific pharmacy tenant for a feature flag.
        """
        clean_key = str(flag_key or "").strip().lower()
        clean_tenant = str(tenant_id or "").strip()

        flag = PlatformFeatureFlag.objects.filter(flag_key=clean_key).first()
        if not flag:
            raise ValidationError({"flag_key": f"Feature flag '{clean_key}' does not exist."})

        tenant = Tenant.objects.filter(tenant_id=clean_tenant).first()
        if not tenant:
            raise ValidationError({"tenant_id": f"Pharmacy tenant '{clean_tenant}' does not exist."})

        override = TenantFeatureFlagOverride.objects.filter(flag=flag, tenant=tenant).first()
        action_name = "FEATURE_FLAG_OVERRIDE_UPDATED" if override else "FEATURE_FLAG_OVERRIDE_CREATED"

        if not override:
            override = TenantFeatureFlagOverride(
                override_id=f"tfo-{uuid.uuid4().hex[:12]}",
                flag=flag,
                tenant=tenant,
            )

        override.is_enabled = bool(is_enabled)
        override.notes = str(notes or "").strip()
        override.created_by = actor
        override.save()

        AuditLogEntry.objects.create(
            action=action_name,
            resource_type="FEATURE_FLAG_OVERRIDE",
            resource_id=override.override_id,
            tenant_id=clean_tenant,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email,
                "flag_key": flag.flag_key,
                "tenant_id": clean_tenant,
                "is_enabled": override.is_enabled,
                "notes": override.notes,
            },
        )
        return cls.serialize_override(override)

    @classmethod
    @transaction.atomic
    def remove_tenant_override(
        cls,
        flag_key: str,
        tenant_id: str,
        actor: PharmacyUser,
    ) -> bool:
        """Removes tenant override, reverting tenant to standard rollout and global rules."""
        clean_key = str(flag_key or "").strip().lower()
        clean_tenant = str(tenant_id or "").strip()

        override = TenantFeatureFlagOverride.objects.filter(flag_id=clean_key, tenant_id=clean_tenant).first()
        if not override:
            return False

        override_id = override.override_id
        override.delete()

        AuditLogEntry.objects.create(
            action="FEATURE_FLAG_OVERRIDE_REMOVED",
            resource_type="FEATURE_FLAG_OVERRIDE",
            resource_id=override_id,
            tenant_id=clean_tenant,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email,
                "flag_key": clean_key,
                "tenant_id": clean_tenant,
            },
        )
        return True
