"""
backend/apps/core/support_ticket_views.py

Architecture Decision: ADR-0069
Dedicated API ViewSets for Support Tickets:
1. PlatformSupportTicketViewSet (Platform Super Admin & Operators)
   - GET  /api/v2/platform/support/tickets/
   - GET  /api/v2/platform/support/tickets/<ticket_id>/
   - POST /api/v2/platform/support/tickets/<ticket_id>/assign/
   - POST /api/v2/platform/support/tickets/<ticket_id>/status/
   - POST /api/v2/platform/support/tickets/<ticket_id>/notes/
2. TenantSupportTicketViewSet (Tenant Store Users - POS / Mobile)
   - GET  /api/v2/tenants/<tenant_id>/support/tickets/
   - POST /api/v2/tenants/<tenant_id>/support/tickets/
   - GET  /api/v2/tenants/<tenant_id>/support/tickets/<ticket_id>/
   - POST /api/v2/tenants/<tenant_id>/support/tickets/<ticket_id>/reply/
   - POST /api/v2/tenants/<tenant_id>/support/tickets/<ticket_id>/close/
"""

from __future__ import annotations

import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TicketStatus
from apps.core.support_ticket_service import PlatformSupportTicketService
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class PlatformSupportTicketViewSet(viewsets.ViewSet):
    """
    Super Admin & Platform Operator Support Ticket Management Console.
    Full visibility over all tenants, ticket assignment, and internal notes.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _require_platform_staff(self, request):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials were not provided.")
        role = str(getattr(user, "role", "")).upper().strip()
        if role not in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR] and not getattr(user, "is_superuser", False):
            raise PermissionDenied(
                detail={
                    "error_code": "PLATFORM_ACCESS_DENIED",
                    "message_en": "Platform support ticket administration is restricted to platform operators.",
                    "message_bn": "প্ল্যাটফর্ম সাপোর্ট টিকিট পরিচালনা শুধুমাত্র প্ল্যাটফর্ম অপারেটরদের জন্য সীমাবদ্ধ।",
                }
            )

    def list(self, request):
        """Lists tickets across all tenants with filtering and pagination."""
        self._require_platform_staff(request)
        tenant_id = request.query_params.get("tenant_id")
        status_filter = request.query_params.get("status")
        priority_filter = request.query_params.get("priority")
        category_filter = request.query_params.get("category")
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", 20))

        result = PlatformSupportTicketService.list_tickets(
            user=request.user,
            tenant_id=tenant_id,
            status_filter=status_filter,
            priority_filter=priority_filter,
            category_filter=category_filter,
            page=page,
            page_size=page_size,
        )

        return Response(
            {
                "success": True,
                "code": "SUPPORT_TICKETS_RETRIEVED",
                "message_en": f"Retrieved {len(result['items'])} ticket(s).",
                "message_bn": f"{len(result['items'])}টি সাপোর্ট টিকিট প্রাপ্ত হয়েছে।",
                "data": result,
            },
            status=status.HTTP_200_OK,
        )

    def retrieve(self, request, pk=None):
        """Retrieves ticket detail with internal notes and action history."""
        self._require_platform_staff(request)
        try:
            detail = PlatformSupportTicketService.get_ticket_detail(user=request.user, ticket_id=pk)
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "SUPPORT_TICKET_DETAIL_RETRIEVED",
                "message_en": f"Support ticket '{pk}' retrieved.",
                "message_bn": f"সাপোর্ট টিকিট '{pk}' সফলভাবে প্রাপ্ত হয়েছে।",
                "data": detail,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="assign")
    def assign(self, request, pk=None):
        """Assigns ticket to a platform operator."""
        self._require_platform_staff(request)
        assignee_id = (request.data or {}).get("assignee_user_id")
        if not assignee_id:
            raise ValidationError({"assignee_user_id": "Assignee user ID is required."})

        try:
            result = PlatformSupportTicketService.assign_ticket(
                operator=request.user, ticket_id=pk, assignee_user_id=assignee_id
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)
        except DjangoPermissionDenied as err:
            raise PermissionDenied(detail=str(err))

        return Response(
            {
                "success": True,
                "code": "SUPPORT_TICKET_ASSIGNED",
                "message_en": "Support ticket successfully assigned.",
                "message_bn": "সাপোর্ট টিকিট সফলভাবে নিযুক্ত করা হয়েছে।",
                "data": result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="status")
    def update_status(self, request, pk=None):
        """Updates ticket lifecycle status."""
        self._require_platform_staff(request)
        data = request.data or {}
        new_status = data.get("status")
        resolution = data.get("resolution_summary")

        try:
            result = PlatformSupportTicketService.update_status(
                user=request.user, ticket_id=pk, new_status=new_status, resolution_summary=resolution
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)
        except DjangoPermissionDenied as err:
            raise PermissionDenied(detail=str(err))

        return Response(
            {
                "success": True,
                "code": "SUPPORT_TICKET_STATUS_UPDATED",
                "message_en": f"Ticket status updated to '{new_status}'.",
                "message_bn": f"টিকিট স্ট্যাটাস '{new_status}'-এ পরিবর্তিত হয়েছে।",
                "data": result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="notes")
    def add_internal_note(self, request, pk=None):
        """Adds a private internal note for platform engineers."""
        self._require_platform_staff(request)
        data = request.data or {}
        note_text = data.get("note_text") or data.get("message_text")
        attachments = data.get("attachments")

        try:
            result = PlatformSupportTicketService.add_message(
                user=request.user,
                ticket_id=pk,
                message_text=note_text,
                is_internal=True,
                attachments=attachments,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)
        except DjangoPermissionDenied as err:
            raise PermissionDenied(detail=str(err))

        return Response(
            {
                "success": True,
                "code": "INTERNAL_NOTE_ADDED",
                "message_en": "Internal support note recorded.",
                "message_bn": "অভ্যন্তরীণ সাপোর্ট নোট সফলভাবে সংরক্ষিত হয়েছে।",
                "data": result,
            },
            status=status.HTTP_201_CREATED,
        )


class TenantSupportTicketViewSet(viewsets.ViewSet):
    """
    Tenant Store-Level Support Ticket Endpoints.
    Allows cashiers, branch managers, and pharmacy owners to submit tickets,
    reply to discussions, and track resolutions. Internal notes are strictly masked.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _require_tenant_access(self, request, tenant_id):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials were not provided.")

        clean_tenant = str(tenant_id or "").strip()
        user_tenant = str(getattr(user, "tenant_id", "") or "").strip()

        # Allow Super Admin
        if getattr(user, "is_superuser", False) or str(getattr(user, "role", "")).upper() in [
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
        ]:
            return

        if user_tenant != clean_tenant:
            raise PermissionDenied(
                detail={
                    "error_code": "CROSS_TENANT_VIOLATION",
                    "message_en": f"Access denied: Cannot access support tickets for foreign tenant '{clean_tenant}'.",
                    "message_bn": f"অন্য ফার্মেসির ('{clean_tenant}') সাপোর্ট টিকিটে প্রবেশাধিকার কঠোরভাবে নিষিদ্ধ।",
                }
            )

    @action(detail=False, methods=["get"], url_path=r"(?P<tenant_id>[^/.]+)")
    def list_tenant_tickets(self, request, tenant_id=None):
        """Lists tickets belonging to this tenant."""
        self._require_tenant_access(request, tenant_id)
        status_filter = request.query_params.get("status")
        priority_filter = request.query_params.get("priority")
        category_filter = request.query_params.get("category")
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", 20))

        result = PlatformSupportTicketService.list_tickets(
            user=request.user,
            tenant_id=tenant_id,
            status_filter=status_filter,
            priority_filter=priority_filter,
            category_filter=category_filter,
            page=page,
            page_size=page_size,
        )

        return Response(
            {
                "success": True,
                "code": "TENANT_TICKETS_RETRIEVED",
                "message_en": f"Retrieved {len(result['items'])} ticket(s).",
                "message_bn": f"{len(result['items'])}টি সাপোর্ট টিকিট প্রাপ্ত হয়েছে।",
                "data": result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path=r"(?P<tenant_id>[^/.]+)/create")
    def create_tenant_ticket(self, request, tenant_id=None):
        """Creates a new support ticket on behalf of this pharmacy."""
        self._require_tenant_access(request, tenant_id)
        data = request.data or {}

        try:
            result = PlatformSupportTicketService.create_ticket(
                user=request.user,
                tenant_id=tenant_id,
                title=data.get("title"),
                description=data.get("description"),
                category=data.get("category", "GENERAL_SUPPORT"),
                priority=data.get("priority", "MEDIUM"),
                branch_id=data.get("branch_id"),
                attachments=data.get("attachments"),
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)
        except DjangoPermissionDenied as err:
            raise PermissionDenied(detail=str(err))

        return Response(
            {
                "success": True,
                "code": "SUPPORT_TICKET_CREATED",
                "message_en": "Support ticket created successfully.",
                "message_bn": "সাপোর্ট টিকিট সফলভাবে তৈরি হয়েছে।",
                "data": result,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"], url_path=r"(?P<tenant_id>[^/.]+)/(?P<ticket_id>[^/.]+)")
    def get_tenant_ticket(self, request, tenant_id=None, ticket_id=None):
        """Retrieves ticket detail for store view (internal notes masked)."""
        self._require_tenant_access(request, tenant_id)
        try:
            detail = PlatformSupportTicketService.get_ticket_detail(user=request.user, ticket_id=ticket_id)
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)
        except DjangoPermissionDenied as err:
            raise PermissionDenied(detail=str(err))

        return Response(
            {
                "success": True,
                "code": "SUPPORT_TICKET_DETAIL_RETRIEVED",
                "message_en": "Support ticket retrieved.",
                "message_bn": "সাপোর্ট টিকিট প্রাপ্ত হয়েছে।",
                "data": detail,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path=r"(?P<tenant_id>[^/.]+)/(?P<ticket_id>[^/.]+)/reply")
    def reply_ticket(self, request, tenant_id=None, ticket_id=None):
        """Posts a tenant reply in the public discussion thread."""
        self._require_tenant_access(request, tenant_id)
        data = request.data or {}
        reply_text = data.get("message_text") or data.get("reply_text")
        attachments = data.get("attachments")

        try:
            result = PlatformSupportTicketService.add_message(
                user=request.user,
                ticket_id=ticket_id,
                message_text=reply_text,
                is_internal=False,
                attachments=attachments,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)
        except DjangoPermissionDenied as err:
            raise PermissionDenied(detail=str(err))

        return Response(
            {
                "success": True,
                "code": "SUPPORT_TICKET_REPLY_POSTED",
                "message_en": "Reply posted to support discussion.",
                "message_bn": "সাপোর্ট আলোচনায় উত্তর পোস্ট করা হয়েছে।",
                "data": result,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path=r"(?P<tenant_id>[^/.]+)/(?P<ticket_id>[^/.]+)/close")
    def close_ticket(self, request, tenant_id=None, ticket_id=None):
        """Allows pharmacy staff to close their ticket once satisfied."""
        self._require_tenant_access(request, tenant_id)
        resolution = (request.data or {}).get("notes", "Closed by pharmacy licensee/staff.")

        try:
            result = PlatformSupportTicketService.update_status(
                user=request.user, ticket_id=ticket_id, new_status=TicketStatus.CLOSED, resolution_summary=resolution
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)
        except DjangoPermissionDenied as err:
            raise PermissionDenied(detail=str(err))

        return Response(
            {
                "success": True,
                "code": "SUPPORT_TICKET_CLOSED",
                "message_en": "Support ticket closed.",
                "message_bn": "সাপোর্ট টিকিট সফলভাবে বন্ধ করা হয়েছে।",
                "data": result,
            },
            status=status.HTTP_200_OK,
        )
