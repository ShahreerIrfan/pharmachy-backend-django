"""
backend/apps/core/branch_lifecycle_views.py
Architecture Decision: ADR-0005, ADR-0015, ADR-0081, ADR-0082, ADR-0091, ADR-0096, ADR-0105, ADR-0107
Phase 06: Milestone M03 - Branch Lifecycle Management (P107)
Requirement Trace: FR-001, FR-004, FR-049, FR-070, FR-071, FR-082

REST API ViewSets for:
- Branch store provisioning within plan quota (create)
- Profile and license updates
- Temporary branch suspension with offline lease revocation
- Plan-quota gated branch reactivation
- Branch closure with 100% historical transaction and inventory preservation
- Audit event logs and historical data retention summaries
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.branch_lifecycle_service import BranchLifecycleService
from apps.core.models import Branch
from apps.core.postgres_service import CrossTenantOperationError
from apps.core.quota_guard_service import QuotaExceededError

logger = logging.getLogger("pharmacy.branch_lifecycle.views")


class BranchLifecycleViewSet(viewsets.ViewSet):
    """
    REST API ViewSet managing the complete physical branch store lifecycle.
    - Pharmacy Owner / Superadmin: Full tenant-wide branch management.
    - Branch Manager: Allowed to view assigned branch and update contact info (address, phone).
      Blocked from creating new branches, closing branches, or modifying other branches.
    - Frontline Staff: 403 Forbidden on all lifecycle mutations.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _resolve_tenant_and_role(self, request) -> Tuple[str, str, Optional[str]]:
        user = request.user
        role = str(getattr(user, "role", "")).upper()

        if not user or not user.is_authenticated:
            raise PermissionDenied(
                {
                    "error_code": "ERR_AUTHENTICATION_REQUIRED",
                    "message_en": "Authentication credentials were not provided.",
                    "message_bn": "প্রবেশাধিকারের জন্য অথেনটিকেশন প্রয়োজন।",
                }
            )

        tenant_id = (
            getattr(user, "tenant_id", None)
            or getattr(user, "tenant", None)
            or getattr(request, "tenant_id", None)
        )
        if hasattr(tenant_id, "tenant_id"):
            tenant_id = tenant_id.tenant_id
        tenant_id = str(tenant_id) if tenant_id else None

        req_tenant = request.query_params.get("tenant_id") or request.data.get("tenant_id")
        if (
            req_tenant
            and tenant_id
            and req_tenant != tenant_id
            and role not in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": f"Access denied: cannot access foreign tenant '{req_tenant}'.",
                    "message_bn": f"অননুমোদিত টেন্যান্ট অ্যাক্সেস: '{req_tenant}' দেখার অনুমতি নেই।",
                }
            )

        if not tenant_id and req_tenant:
            tenant_id = req_tenant

        if not tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_CONTEXT_MISSING",
                    "message_en": "Tenant context is missing from user session.",
                    "message_bn": "ইউজার সেশনে টেন্যান্ট কনটেক্সট অনুপস্থিত।",
                }
            )

        user_branch_id = getattr(user, "branch_id", None)
        return str(tenant_id), role, str(user_branch_id) if user_branch_id else None

    def _get_actor_identifier(self, request) -> str:
        u = getattr(request, "user", None)
        if not u:
            return "admin"
        return getattr(u, "email", None) or getattr(u, "username", "admin")

    def _assert_can_view_branch(self, role: str) -> None:
        allowed = [
            "SUPERADMIN",
            "SUPER_ADMIN",
            "PLATFORM_OPERATOR",
            "PHARMACY_OWNER",
            "TENANT_OWNER",
            "BRANCH_MANAGER",
        ]
        if role not in allowed:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": f"Role '{role}' is not authorized to view branch management.",
                    "message_bn": f"আপনার রোলে ('{role}') ব্রাঞ্চ ব্যবস্থাপনা দেখার অনুমতি নেই।",
                }
            )

    def _assert_can_manage_branch(self, role: str) -> None:
        allowed = [
            "SUPERADMIN",
            "SUPER_ADMIN",
            "PLATFORM_OPERATOR",
            "PHARMACY_OWNER",
            "TENANT_OWNER",
        ]
        if role not in allowed:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": "Only pharmacy owners or superadmins can perform this branch action.",
                    "message_bn": "শুধুমাত্র ফার্মেসি মালিক বা সুপারএডমিন এই ব্রাঞ্চ অ্যাকশন সম্পাদন করতে পারেন।",
                }
            )

    def list(self, request) -> Response:
        """Lists branches for tenant with optional filters."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)
        self._assert_can_view_branch(role)

        try:
            branches = BranchLifecycleService.list_branches(
                tenant_id=tenant_id,
                status=request.query_params.get("status"),
                is_active=None if "is_active" not in request.query_params else request.query_params.get("is_active").lower() in ["true", "1"],
                search=request.query_params.get("search"),
            )
            # If Branch Manager, filter to assigned branch only unless querying details
            if role == "BRANCH_MANAGER" and user_branch_id:
                branches = [b for b in branches if b["branch_id"] == user_branch_id]

            return Response({"count": len(branches), "results": branches}, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})

    def create(self, request) -> Response:
        """Provisions a new branch within subscription plan branch quota."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_manage_branch(role)

        data = request.data
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = BranchLifecycleService.create_branch(
                tenant_id=tenant_id,
                name=data.get("name"),
                license_number=data.get("license_number"),
                address=data.get("address", ""),
                branch_code=data.get("branch_code", ""),
                phone_number=data.get("phone_number", ""),
                email=data.get("email", ""),
                manager_name=data.get("manager_name", ""),
                is_main_branch=bool(data.get("is_main_branch", False)),
                performed_by_username=self._get_actor_identifier(request),
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except QuotaExceededError as exc:
            return Response(
                {"error_code": "ERR_QUOTA_EXCEEDED", "message_en": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, pk=None) -> Response:
        """Retrieves branch detail with active staff, device counts, and historical preservation counts."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)
        self._assert_can_view_branch(role)

        if role == "BRANCH_MANAGER" and user_branch_id and pk != user_branch_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": "Branch Manager cannot inspect other branches.",
                    "message_bn": "ব্রাঞ্চ ম্যানেজার অন্য ব্রাঞ্চের তথ্য দেখতে পারেন না।",
                }
            )

        try:
            detail = BranchLifecycleService.get_branch_detail(tenant_id=tenant_id, branch_id=pk)
            return Response(detail, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})

    def partial_update(self, request, pk=None) -> Response:
        """Updates branch attributes with role-specific constraints."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)

        # Branch Manager can only update contact details of their own branch
        if role == "BRANCH_MANAGER":
            if user_branch_id and pk != user_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Branch Manager cannot update other branches.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার অন্য ব্রাঞ্চের তথ্য আপডেট করতে পারেন না।",
                    }
                )
            # Manager cannot rename branch or change license number or change main branch flag
            if any(k in request.data for k in ["name", "license_number", "is_main_branch"]):
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Branch Manager cannot alter branch name, drug license, or main branch status.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার ব্রাঞ্চের নাম, ড্রাগ লাইসেন্স বা প্রধান ব্রাঞ্চের মর্যাদা পরিবর্তন করতে পারেন না।",
                    }
                )
        else:
            self._assert_can_manage_branch(role)

        data = request.data
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            updated = BranchLifecycleService.update_branch(
                tenant_id=tenant_id,
                branch_id=pk,
                name=data.get("name"),
                license_number=data.get("license_number"),
                address=data.get("address"),
                branch_code=data.get("branch_code"),
                phone_number=data.get("phone_number"),
                email=data.get("email"),
                manager_name=data.get("manager_name"),
                is_main_branch=data.get("is_main_branch"),
                performed_by_username=self._get_actor_identifier(request),
                ip_address=ip_addr,
            )
            return Response(updated, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="suspend")
    def suspend(self, request, pk=None) -> Response:
        """Temporarily suspends branch operations and revokes active offline leases."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_manage_branch(role)

        reason = request.data.get("reason", "Administrative branch suspension")
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = BranchLifecycleService.suspend_branch(
                tenant_id=tenant_id,
                branch_id=pk,
                reason=reason,
                performed_by_username=self._get_actor_identifier(request),
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="reactivate")
    def reactivate(self, request, pk=None) -> Response:
        """Reactivates a suspended branch subject to plan branch quota."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_manage_branch(role)

        reason = request.data.get("reason", "Branch reactivated by administrator")
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = BranchLifecycleService.reactivate_branch(
                tenant_id=tenant_id,
                branch_id=pk,
                reason=reason,
                performed_by_username=self._get_actor_identifier(request),
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except QuotaExceededError as exc:
            return Response(
                {"error_code": "ERR_QUOTA_EXCEEDED", "message_en": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="close")
    def close(self, request, pk=None) -> Response:
        """
        Permanently closes a branch while strictly preserving historical invoices and inventory.
        """
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_manage_branch(role)

        reason = request.data.get("reason", "")
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = BranchLifecycleService.close_branch(
                tenant_id=tenant_id,
                branch_id=pk,
                reason=reason,
                performed_by_username=self._get_actor_identifier(request),
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["get"], url_path="historical-summary")
    def historical_summary(self, request, pk=None) -> Response:
        """Returns preserved historic invoice, stock, and prescription summary for audit."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)
        self._assert_can_view_branch(role)

        if role == "BRANCH_MANAGER" and user_branch_id and pk != user_branch_id:
            raise PermissionDenied({"error_code": "ERR_PERMISSION_DENIED", "message_en": "Branch Manager cannot view other branches."})

        try:
            summary = BranchLifecycleService.get_historical_summary(tenant_id=tenant_id, branch_id=pk)
            return Response(summary, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})

    @action(detail=True, methods=["get"], url_path="events")
    def events(self, request, pk=None) -> Response:
        """Returns the immutable branch lifecycle event history."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)
        self._assert_can_view_branch(role)

        if role == "BRANCH_MANAGER" and user_branch_id and pk != user_branch_id:
            raise PermissionDenied({"error_code": "ERR_PERMISSION_DENIED", "message_en": "Branch Manager cannot view other branches."})

        try:
            event_list = BranchLifecycleService.list_branch_events(tenant_id=tenant_id, branch_id=pk)
            return Response({"count": len(event_list), "results": event_list}, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})

    def destroy(self, request, pk=None) -> Response:
        """
        Safeguarded DELETE: Hard deletion is strictly forbidden if historical records exist.
        Directs caller to use the close action instead.
        """
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_manage_branch(role)

        try:
            branch = Branch.objects.get(branch_id=pk, tenant_id=tenant_id)
        except Branch.DoesNotExist:
            raise CrossTenantOperationError(f"Branch '{pk}' not found for tenant '{tenant_id}'.")

        # Check for historical transactions, stock, dues, or clinical Rx records
        has_sales = (hasattr(branch, "invoices") and branch.invoices.exists()) or (hasattr(branch, "sales") and branch.sales.exists())
        has_batches = hasattr(branch, "batches") and branch.batches.exists()
        has_rx = hasattr(branch, "prescriptions") and branch.prescriptions.exists()
        has_dues = hasattr(branch, "customer_dues") and branch.customer_dues.exists()

        if has_sales or has_batches or has_rx or has_dues:
            return Response(
                {
                    "error_code": "ERR_CANNOT_DELETE_BRANCH_WITH_HISTORY",
                    "message_en": (
                        f"Branch '{branch.name}' contains historical sales invoices, inventory batches, "
                        "or prescription verification logs. Hard delete is prohibited to preserve audit integrity. "
                        "Please use POST /api/v2/branches/{branch_id}/close/ to decommission this branch."
                    ),
                    "message_bn": (
                        f"ব্রাঞ্চ '{branch.name}'-এ পূর্ববর্তী বিক্রয় ইনভয়েস, স্টক ব্যাচ বা প্রেসক্রিপশন রেকর্ড বিদ্যমান। "
                        "অডিট ও আইনি কারণে এটি স্থায়ীভাবে ডিলিট করা নিষিদ্ধ। অনুগ্রহ করে ক্লোজ (close) অ্যান্ডপয়েন্ট ব্যবহার করুন।"
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Only empty provisional branches with zero transactions can be hard deleted
        branch.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
