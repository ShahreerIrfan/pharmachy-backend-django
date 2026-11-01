"""
backend/apps/core/owner_dashboard_views.py

Architecture Decision: ADR-0101
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility (P101)
Requirement Trace: FR-031, FR-032, FR-033, FR-044, FR-066, FR-070

DRF ViewSet for Tenant Owner Scoped Dashboard:
- Provides high-level business intelligence, gross/net profit, stock valuation, and clinical Rx status.
- Strict Role-Based Access Control (RBAC):
  * Pharmacy Owners & Superadmins: Full tenant and multi-branch access.
  * Branch Managers: Restricted strictly to their assigned branch.
  * Cashiers, Staff Pharmacists, Inventory Clerks: Strictly denied (ERR_PERMISSION_DENIED).
- Cross-tenant boundaries strictly enforced (ERR_CROSS_TENANT_VIOLATION).
- Decimal monetary accuracy and audited access tracking.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.dateparse import parse_date
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.errors import CrossTenantViolationError
from apps.core.owner_dashboard_service import TenantOwnerDashboardService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.owner_dashboard.views")


class OwnerDashboardViewSet(viewsets.ViewSet):
    """
    API ViewSet for executive pharmacy management and operational monitoring.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _is_platform_admin(self, user: Any) -> bool:
        if not user or not user.is_authenticated:
            return False
        return (
            getattr(user, "is_superuser", False)
            or getattr(user, "role", None) in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR]
            or str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "PLATFORM_OPERATOR", "SUPER_ADMIN"]
        )

    def _require_authorized_role(self, user: Any) -> None:
        """Enforces that only owners, managers, or platform administrators can view executive metrics."""
        if self._is_platform_admin(user):
            return

        role = str(getattr(user, "role", "")).upper()
        allowed_roles = [
            "PHARMACY_OWNER",
            "OWNER",
            "ADMIN",
            "PHARMACY_MANAGER",
            "BRANCH_MANAGER",
            "FINANCE_OFFICER",
        ]
        if role not in allowed_roles:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": f"Access denied for role '{role}'. Only pharmacy owners and branch managers can access executive dashboards.",
                    "message_bn": f"আপনার রোলে ('{role}') এক্সিকিউটিভ ড্যাশবোর্ড দেখার অনুমতি নেই। শুধুমাত্র ফার্মেসি মালিক ও ব্রাঞ্চ ম্যানেজারদের প্রবেশাধিকার আছে।",
                }
            )

    def _resolve_tenant_and_branch(
        self,
        request: Any,
        target_tenant_id: str | None = None,
        requested_branch_id: str | None = None,
    ) -> tuple[str, str | None]:
        """Resolves and validates tenant and branch scopes with strict role boundaries."""
        user = request.user
        user_tid = str(getattr(user, "tenant_id", "")).strip()

        # 1. Tenant boundary enforcement
        if target_tenant_id:
            clean_target = str(target_tenant_id).strip()
            if not self._is_platform_admin(user) and user_tid and user_tid != clean_target:
                raise CrossTenantViolationError(
                    f"User from tenant '{user_tid}' cannot access dashboard for tenant '{clean_target}'."
                )
            resolved_tid = clean_target
        elif user_tid:
            resolved_tid = user_tid
        elif self._is_platform_admin(user):
            header_tid = str(request.headers.get("X-Tenant-ID", "")).strip()
            if header_tid:
                resolved_tid = header_tid
            else:
                raise ValidationError({"tenant_id": "Target tenant_id must be specified by platform administrators."})
        else:
            raise ValidationError({"tenant_id": "Could not resolve authenticated tenant context."})

        # 2. Branch boundary enforcement
        user_role = str(getattr(user, "role", "")).upper()
        user_bid = getattr(user, "branch_id", None) or getattr(user, "branch", None)
        user_bid_str = str(getattr(user_bid, "branch_id", None) or user_bid or "").strip()

        if user_role == "BRANCH_MANAGER":
            if not user_bid_str:
                raise PermissionDenied(
                    detail={
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Branch manager is not assigned to any specific branch.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার কোনো নির্দিষ্ট শাখায় যুক্ত নেই।",
                    }
                )
            if requested_branch_id and str(requested_branch_id).strip() != user_bid_str:
                raise PermissionDenied(
                    detail={
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": f"Branch manager cannot access sibling branch '{requested_branch_id}'.",
                        "message_bn": f"ব্রাঞ্চ ম্যানেজার অন্য শাখা '{requested_branch_id}'-এর তথ্য দেখতে পারবেন না।",
                    }
                )
            return resolved_tid, user_bid_str

        # Owners and admins can filter by branch or view all
        clean_branch = str(requested_branch_id).strip() if requested_branch_id else None
        return resolved_tid, clean_branch

    def list(self, request: Any) -> Response:
        """
        Retrieves the complete tenant owner dashboard summary.
        Query params:
        - tenant_id (optional for owners; required for superadmins)
        - branch_id (optional; filtered to single branch)
        - date_from (optional; format YYYY-MM-DD)
        - date_to (optional; format YYYY-MM-DD)
        """
        self._require_authorized_role(request.user)

        target_tid = request.query_params.get("tenant_id")
        requested_bid = request.query_params.get("branch_id")
        resolved_tid, resolved_bid = self._resolve_tenant_and_branch(request, target_tid, requested_bid)

        date_from_str = request.query_params.get("date_from")
        date_to_str = request.query_params.get("date_to")

        date_from = None
        if date_from_str:
            date_from = parse_date(date_from_str)
            if not date_from:
                return Response(
                    {"success": False, "code": "INVALID_DATE_FORMAT", "message": f"Invalid date_from: '{date_from_str}'. Expected YYYY-MM-DD."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        date_to = None
        if date_to_str:
            date_to = parse_date(date_to_str)
            if not date_to:
                return Response(
                    {"success": False, "code": "INVALID_DATE_FORMAT", "message": f"Invalid date_to: '{date_to_str}'. Expected YYYY-MM-DD."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            summary = TenantOwnerDashboardService.get_dashboard_summary(
                tenant_id=resolved_tid,
                branch_id=resolved_bid,
                date_from=date_from,
                date_to=date_to,
            )
        except DjangoValidationError as err:
            return Response(
                {"success": False, "code": "VALIDATION_ERROR", "message": str(err)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Audit log for executive dashboard viewing
        actor_id = str(
            getattr(request.user, "user_id", None)
            or getattr(request.user, "id", None)
            or getattr(request.user, "email", "system")
        )
        AuditLogEntry.objects.create(
            action="OWNER_DASHBOARD_ACCESSED",
            resource_type="EXECUTIVE_DASHBOARD",
            resource_id=resolved_bid or resolved_tid,
            tenant_id=resolved_tid,
            user_id=actor_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "branch_filter": resolved_bid,
                "date_from": date_from.isoformat() if date_from else None,
                "date_to": date_to.isoformat() if date_to else None,
            },
        )

        return Response(
            {
                "success": True,
                "code": "OWNER_DASHBOARD_RETRIEVED",
                "data": summary,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="inventory-alerts")
    def inventory_alerts(self, request: Any) -> Response:
        """Returns low-stock items and upcoming DGDA expiring batches."""
        self._require_authorized_role(request.user)
        resolved_tid, resolved_bid = self._resolve_tenant_and_branch(
            request,
            request.query_params.get("tenant_id"),
            request.query_params.get("branch_id"),
        )

        summary = TenantOwnerDashboardService.get_dashboard_summary(
            tenant_id=resolved_tid,
            branch_id=resolved_bid,
        )

        return Response(
            {
                "success": True,
                "code": "INVENTORY_ALERTS_RETRIEVED",
                "data": {
                    "tenant_id": resolved_tid,
                    "branch_filter": resolved_bid,
                    "inventory_summary": summary["inventory_summary"],
                    "expiry_summary": summary["expiry_summary"],
                },
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="sync-health")
    def sync_health(self, request: Any) -> Response:
        """Returns registered POS terminals, offline lease certificates, and hold queue status."""
        self._require_authorized_role(request.user)
        resolved_tid, resolved_bid = self._resolve_tenant_and_branch(
            request,
            request.query_params.get("tenant_id"),
            request.query_params.get("branch_id"),
        )

        summary = TenantOwnerDashboardService.get_dashboard_summary(
            tenant_id=resolved_tid,
            branch_id=resolved_bid,
        )

        return Response(
            {
                "success": True,
                "code": "SYNC_HEALTH_RETRIEVED",
                "data": {
                    "tenant_id": resolved_tid,
                    "branch_filter": resolved_bid,
                    "sync_health_summary": summary["sync_health_summary"],
                },
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="dues/record")
    def record_due(self, request: Any) -> Response:
        """Records a new retail customer credit/due balance."""
        self._require_authorized_role(request.user)

        data = request.data or {}
        branch_id = data.get("branch_id")
        customer_name = data.get("customer_name")
        customer_phone = data.get("customer_phone")
        total_due_bdt = data.get("total_due_bdt")
        invoice_id = data.get("invoice_id")
        due_date_str = data.get("due_date")
        notes = data.get("notes", "")

        resolved_tid, resolved_bid = self._resolve_tenant_and_branch(request, data.get("tenant_id"), branch_id)
        if not resolved_bid:
            raise ValidationError({"branch_id": "branch_id is required to record customer due."})

        due_date = parse_date(due_date_str) if due_date_str else None

        try:
            record = TenantOwnerDashboardService.record_customer_due(
                tenant_id=resolved_tid,
                branch_id=resolved_bid,
                customer_name=customer_name,
                customer_phone=customer_phone,
                total_due_bdt=total_due_bdt,
                invoice_id=invoice_id,
                due_date=due_date,
                notes=notes,
            )
        except DjangoValidationError as err:
            return Response(
                {"success": False, "code": "VALIDATION_ERROR", "message": str(err)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "success": True,
                "code": "CUSTOMER_DUE_RECORDED",
                "data": {
                    "due_id": record.due_id,
                    "customer_name": record.customer_name,
                    "customer_phone": record.customer_phone,
                    "total_due_bdt": f"{record.total_due_bdt:.2f}",
                    "remaining_due_bdt": f"{record.remaining_due_bdt:.2f}",
                    "status": record.status,
                },
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="dues/settle")
    def settle_due(self, request: Any) -> Response:
        """Settles an outstanding customer due balance."""
        self._require_authorized_role(request.user)

        data = request.data or {}
        due_id = data.get("due_id")
        amount = data.get("amount_bdt")

        if not due_id:
            raise ValidationError({"due_id": "due_id is required."})
        if not amount:
            raise ValidationError({"amount_bdt": "amount_bdt is required."})

        try:
            record = TenantOwnerDashboardService.settle_customer_due(
                due_id=due_id,
                payment_amount_bdt=Decimal(str(amount)),
                actor_user=request.user,
            )
        except DjangoValidationError as err:
            return Response(
                {"success": False, "code": "VALIDATION_ERROR", "message": str(err)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "success": True,
                "code": "CUSTOMER_DUE_SETTLED",
                "data": {
                    "due_id": record.due_id,
                    "paid_amount_bdt": f"{record.paid_amount_bdt:.2f}",
                    "remaining_due_bdt": f"{record.remaining_due_bdt:.2f}",
                    "status": record.status,
                },
            },
            status=status.HTTP_200_OK,
        )
