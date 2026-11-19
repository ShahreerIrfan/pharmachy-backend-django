"""
backend/apps/core/desktop_shell_service.py
Architecture Decision: ADR-0321 (P321).
Phase 17: JavaFX macOS-Inspired App Shell & UI Components.

Desktop Shell Architecture & Workspace Profile Service:
- Manages user-device workspace configurations, themes, locales, and authorized routing.
- Gating rules:
  - Restricts managerial/admin routes (Inventory Ledger, Settings, Shift Close Approvals) to OWNER / MANAGER.
  - Enforces strict tenant and branch isolation.
  - Logs workspace configuration adjustments to the audit log.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    DesktopShellProfile,
    DesktopShellTheme,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger(__name__)

STANDARD_CASHIER_ROUTES = [
    "ROUTE_POS_SALES",
    "ROUTE_CUSTOMER_LOOKUP",
    "ROUTE_DAILY_SHIFT",
    "ROUTE_HELP_SHORTCUTS",
]

MANAGER_ELEVATED_ROUTES = [
    "ROUTE_INVENTORY_LEDGER",
    "ROUTE_PRESCRIPTION_VERIFICATION",
    "ROUTE_OFFLINE_SYNC_CENTER",
    "ROUTE_SETTINGS",
    "ROUTE_CONFLICT_REVIEW",
]


class DesktopShellService:

    @classmethod
    def assert_tenant_active(cls, tenant_id: str) -> Tenant:
        try:
            t = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' not found."})
        if t.status != TenantLifecycleStatus.ACTIVE:
            raise PermissionDenied(
                {"error_code": "ERR_TENANT_SUSPENDED", "detail": f"Tenant '{tenant_id}' is inactive or suspended."}
            )
        return t

    @classmethod
    def get_authorized_routes_for_role(cls, role: str) -> List[str]:
        routes = list(STANDARD_CASHIER_ROUTES)
        if role in [UserRole.PHARMACY_OWNER, UserRole.BRANCH_MANAGER]:
            routes.extend(MANAGER_ELEVATED_ROUTES)
        elif role == UserRole.STAFF_PHARMACIST:
            routes.append("ROUTE_PRESCRIPTION_VERIFICATION")
            routes.append("ROUTE_INVENTORY_LEDGER")
        return sorted(list(set(routes)))

    @classmethod
    @transaction.atomic
    def get_or_create_workspace_profile(
        cls,
        *,
        tenant_id: str,
        branch_id: str,
        user: PharmacyUser,
        device_id: str,
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)

        # Cross-tenant check for user
        if str(getattr(user, "tenant_id", "")) != str(tenant_id):
            raise PermissionDenied(
                {"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "User does not belong to the specified tenant."}
            )

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
        if not branch:
            foreign = Branch.objects.filter(branch_id=branch_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied(
                    {"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant branch access forbidden."}
                )
            raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        if not device_id or len(device_id.strip()) < 3:
            raise ValidationError({"error_code": "ERR_INVALID_DEVICE_ID", "detail": "Valid device_id is required."})

        role = getattr(user, "role", UserRole.CASHIER)
        allowed_routes = cls.get_authorized_routes_for_role(role)

        profile, created = DesktopShellProfile.objects.get_or_create(
            tenant=tenant,
            branch=branch,
            user=user,
            device_id=device_id.strip(),
            defaults={
                "profile_id": f"dsp-{uuid.uuid4().hex[:12]}",
                "theme": DesktopShellTheme.MACOS_LIGHT,
                "locale": "en_BD",
                "sidebar_collapsed": False,
                "quick_shortcuts_enabled": True,
                "enabled_routes_json": allowed_routes,
            },
        )

        return {
            "profile_id": profile.profile_id,
            "tenant_id": str(tenant.tenant_id),
            "branch_id": str(branch.branch_id),
            "user_id": str(user.user_id),
            "device_id": profile.device_id,
            "theme": profile.theme,
            "locale": profile.locale,
            "sidebar_collapsed": profile.sidebar_collapsed,
            "quick_shortcuts_enabled": profile.quick_shortcuts_enabled,
            "enabled_routes": profile.enabled_routes_json,
            "created": created,
        }

    @classmethod
    @transaction.atomic
    def update_workspace_preferences(
        cls,
        *,
        tenant_id: str,
        branch_id: str,
        user: PharmacyUser,
        device_id: str,
        theme: Optional[str] = None,
        locale: Optional[str] = None,
        sidebar_collapsed: Optional[bool] = None,
        quick_shortcuts_enabled: Optional[bool] = None,
        requested_routes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)

        profile = DesktopShellProfile.objects.filter(
            tenant=tenant,
            branch__branch_id=branch_id,
            user=user,
            device_id=device_id,
        ).first()

        if not profile:
            raise NotFound({"error_code": "ERR_PROFILE_NOT_FOUND", "detail": "Workspace profile does not exist."})

        if theme:
            valid_themes = [DesktopShellTheme.MACOS_LIGHT, DesktopShellTheme.MACOS_DARK, DesktopShellTheme.HIGH_CONTRAST]
            if theme not in valid_themes:
                raise ValidationError(
                    {"error_code": "ERR_INVALID_THEME", "detail": f"Theme must be one of {valid_themes}."}
                )
            profile.theme = theme

        if locale:
            if locale not in ["en_BD", "bn_BD"]:
                raise ValidationError({"error_code": "ERR_INVALID_LOCALE", "detail": "Locale must be en_BD or bn_BD."})
            profile.locale = locale

        if sidebar_collapsed is not None:
            profile.sidebar_collapsed = bool(sidebar_collapsed)

        if quick_shortcuts_enabled is not None:
            profile.quick_shortcuts_enabled = bool(quick_shortcuts_enabled)

        if requested_routes is not None:
            role = getattr(user, "role", UserRole.CASHIER)
            allowed_max = cls.get_authorized_routes_for_role(role)
            for r in requested_routes:
                if r not in allowed_max:
                    raise PermissionDenied(
                        {
                            "error_code": "ERR_ROUTE_PERMISSION_DENIED",
                            "detail": f"Role '{role}' is not permitted to activate route '{r}'.",
                        }
                    )
            profile.enabled_routes_json = requested_routes

        profile.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=str(user.user_id),
            actor_email=str(getattr(user, "email", "") or ""),
            actor_role=str(getattr(user, "role", "CASHIER")),
            action="DESKTOP_WORKSPACE_PREFERENCES_UPDATED",
            resource_type="DesktopShellProfile",
            resource_id=profile.profile_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "theme": profile.theme,
                "locale": profile.locale,
                "sidebar_collapsed": profile.sidebar_collapsed,
                "routes_count": len(profile.enabled_routes_json),
            },
        )

        return {
            "profile_id": profile.profile_id,
            "theme": profile.theme,
            "locale": profile.locale,
            "sidebar_collapsed": profile.sidebar_collapsed,
            "quick_shortcuts_enabled": profile.quick_shortcuts_enabled,
            "enabled_routes": profile.enabled_routes_json,
            "updated_at": profile.updated_at.isoformat(),
        }
