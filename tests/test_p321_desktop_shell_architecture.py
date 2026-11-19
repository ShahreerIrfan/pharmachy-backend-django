"""
backend/tests/test_p321_desktop_shell_architecture.py
Architecture Decision: ADR-0321 (P321).
Phase 17: JavaFX macOS-Inspired App Shell & UI Components.

Targeted integration tests for Desktop App Shell Architecture & Workspace Profiles:
1. Valid workspace profile resolution and role-based route defaults.
2. Preference updates (theme, locale, sidebar toggle, shortcut enablement).
3. RBAC route restriction: Cashiers cannot enable elevated manager routes (ERR_ROUTE_PERMISSION_DENIED).
4. Cross-tenant isolation: Tenant B cannot access Tenant A's device workspace profile.
5. Incomplete/invalid payload rejection.
6. DRF REST endpoints: /resolve-profile/ and /update-preferences/.
"""

import random
import uuid
import pytest
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.desktop_shell_service import DesktopShellService
from apps.core.models import (
    Branch,
    DesktopShellProfile,
    DesktopShellTheme,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestDesktopShellArchitectureIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-shl-a-{self.suffix}",
            name=f"Shell Pharma A {self.suffix}",
            subdomain=f"shl-a-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-shl-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
        )
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_shl_a_{self.suffix}@test.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name=f"Owner Shell A {self.suffix}",
            password="StrongPassword123!",
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )
        self.cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_shl_a_{self.suffix}@test.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name=f"Cashier Shell A {self.suffix}",
            password="StrongPassword123!",
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            role=UserRole.CASHIER,
            is_active=True,
        )

        # Tenant B
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-shl-b-{self.suffix}",
            name=f"Shell Pharma B {self.suffix}",
            subdomain=f"shl-b-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-shl-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Gulshan Branch",
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_shl_b_{self.suffix}@test.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name=f"Owner Shell B {self.suffix}",
            password="StrongPassword123!",
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )

    def test_get_or_create_workspace_profile_cashier(self):
        res = DesktopShellService.get_or_create_workspace_profile(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            user=self.cashier_a,
            device_id=f"POS-DEV-{self.suffix}",
        )
        assert res["theme"] == DesktopShellTheme.MACOS_LIGHT
        assert res["locale"] == "en_BD"
        assert res["sidebar_collapsed"] is False
        assert "ROUTE_POS_SALES" in res["enabled_routes"]
        assert "ROUTE_SETTINGS" not in res["enabled_routes"]  # Cashier cannot have settings

        profile = DesktopShellProfile.objects.get(profile_id=res["profile_id"])
        assert profile.user == self.cashier_a
        assert profile.device_id == f"POS-DEV-{self.suffix}"

    def test_update_workspace_preferences_valid(self):
        profile_res = DesktopShellService.get_or_create_workspace_profile(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            user=self.owner_a,
            device_id=f"POS-OWNER-{self.suffix}",
        )

        update_res = DesktopShellService.update_workspace_preferences(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            user=self.owner_a,
            device_id=f"POS-OWNER-{self.suffix}",
            theme=DesktopShellTheme.MACOS_DARK,
            locale="bn_BD",
            sidebar_collapsed=True,
            requested_routes=["ROUTE_POS_SALES", "ROUTE_SETTINGS"],
        )
        assert update_res["theme"] == DesktopShellTheme.MACOS_DARK
        assert update_res["locale"] == "bn_BD"
        assert update_res["sidebar_collapsed"] is True
        assert update_res["enabled_routes"] == ["ROUTE_POS_SALES", "ROUTE_SETTINGS"]

        audit = AuditLogEntry.objects.filter(resource_id=profile_res["profile_id"]).first()
        assert audit is not None
        assert audit.action == "DESKTOP_WORKSPACE_PREFERENCES_UPDATED"
        assert audit.status == "SUCCESS"

    def test_cashier_cannot_activate_manager_elevated_routes(self):
        DesktopShellService.get_or_create_workspace_profile(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            user=self.cashier_a,
            device_id=f"POS-CASHIER-{self.suffix}",
        )

        with pytest.raises(PermissionDenied) as exc:
            DesktopShellService.update_workspace_preferences(
                tenant_id=self.tenant_a.tenant_id,
                branch_id=self.branch_a.branch_id,
                user=self.cashier_a,
                device_id=f"POS-CASHIER-{self.suffix}",
                requested_routes=["ROUTE_POS_SALES", "ROUTE_SETTINGS"],
            )
        assert exc.value.detail["error_code"] == "ERR_ROUTE_PERMISSION_DENIED"

    def test_cross_tenant_branch_isolation(self):
        with pytest.raises(PermissionDenied) as exc:
            DesktopShellService.get_or_create_workspace_profile(
                tenant_id=self.tenant_a.tenant_id,
                branch_id=self.branch_b.branch_id,  # Branch of Tenant B
                user=self.owner_a,
                device_id=f"POS-DEV-{self.suffix}",
            )
        assert exc.value.detail["error_code"] == "ERR_CROSS_TENANT_ACCESS"

    def test_invalid_device_or_theme_rejected(self):
        with pytest.raises(ValidationError) as exc:
            DesktopShellService.get_or_create_workspace_profile(
                tenant_id=self.tenant_a.tenant_id,
                branch_id=self.branch_a.branch_id,
                user=self.owner_a,
                device_id="x",  # Too short
            )
        assert exc.value.detail["error_code"] == "ERR_INVALID_DEVICE_ID"

    def test_drf_endpoints(self):
        self.client.force_authenticate(user=self.owner_a)

        # 1. Resolve Profile endpoint
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "device_id": f"POS-API-{self.suffix}",
        }
        res = self.client.post("/api/v2/desktop/shell-profiles/resolve-profile/", data=payload, format="json")
        assert res.status_code == status.HTTP_200_OK
        assert res.data["theme"] == DesktopShellTheme.MACOS_LIGHT

        # 2. Update preferences endpoint
        patch_payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "device_id": f"POS-API-{self.suffix}",
            "locale": "bn_BD",
            "sidebar_collapsed": True,
        }
        res_patch = self.client.patch("/api/v2/desktop/shell-profiles/update-preferences/", data=patch_payload, format="json")
        assert res_patch.status_code == status.HTTP_200_OK
        assert res_patch.data["locale"] == "bn_BD"
        assert res_patch.data["sidebar_collapsed"] is True
