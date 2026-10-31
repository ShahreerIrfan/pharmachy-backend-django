"""
backend/apps/core/support_ticket_service.py

Architecture Decision: ADR-0069
Platform Support Ticket Service.
Manages multi-tenant support tickets, assignment, internal notes, attachments,
and immutable action history with strict tenant isolation and role-based confidentiality.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    PlatformSupportTicket,
    Tenant,
    TicketActionHistory,
    TicketAttachment,
    TicketCategory,
    TicketMessage,
    TicketPriority,
    TicketStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger(__name__)


class PlatformSupportTicketService:
    """
    Central orchestration service for support tickets, discussion threads, and action history.
    """

    MAX_ATTACHMENT_SIZE_BYTES = 10 * 1024 * 1024  # 10MB limit per file

    @classmethod
    def resolve_status_chip(cls, ticket_status: str) -> Dict[str, str]:
        """Maps ticket status to bilingual labels and restrained Apple-inspired UI tones."""
        clean = str(ticket_status or "OPEN").upper().strip()
        mapping = {
            TicketStatus.OPEN: {
                "tone": "info",
                "label_en": "Open",
                "label_bn": "উন্মুক্ত",
            },
            TicketStatus.IN_PROGRESS: {
                "tone": "warning",
                "label_en": "In Progress",
                "label_bn": "চলমান",
            },
            TicketStatus.WAITING_ON_TENANT: {
                "tone": "neutral",
                "label_en": "Waiting on Pharmacy",
                "label_bn": "ফার্মেসির উত্তরের অপেক্ষায়",
            },
            TicketStatus.RESOLVED: {
                "tone": "success",
                "label_en": "Resolved",
                "label_bn": "সমাধানকৃত",
            },
            TicketStatus.CLOSED: {
                "tone": "neutral",
                "label_en": "Closed",
                "label_bn": "বন্ধ",
            },
        }
        return mapping.get(clean, {"tone": "neutral", "label_en": clean, "label_bn": clean})

    @classmethod
    @transaction.atomic
    def create_ticket(
        cls,
        user: PharmacyUser,
        tenant_id: str,
        title: str,
        description: str,
        category: str = TicketCategory.GENERAL_SUPPORT,
        priority: str = TicketPriority.MEDIUM,
        branch_id: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Creates a new support ticket bound to a specific tenant.
        """
        clean_tenant_id = str(tenant_id or "").strip()
        tenant = Tenant.objects.filter(tenant_id=clean_tenant_id).first()
        if not tenant:
            raise ValidationError({"tenant_id": f"Tenant '{clean_tenant_id}' does not exist on platform."})

        # Verify tenant scope for tenant users
        user_role = str(getattr(user, "role", "")).upper().strip()
        is_platform_admin = user_role in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR] or getattr(
            user, "is_superuser", False
        )
        if not is_platform_admin and str(getattr(user, "tenant_id", "")) != clean_tenant_id:
            raise PermissionDenied(
                f"Access denied: You cannot create support tickets for foreign tenant '{clean_tenant_id}'."
            )

        # Validate Title & Description
        clean_title = str(title or "").strip()
        if len(clean_title) < 5 or len(clean_title) > 200:
            raise ValidationError({"title": "Ticket title must be between 5 and 200 characters."})

        clean_description = str(description or "").strip()
        if len(clean_description) < 10:
            raise ValidationError({"description": "Ticket description must be at least 10 characters long."})

        # Validate Category & Priority
        clean_category = str(category or "").upper().strip()
        if clean_category not in TicketCategory.values:
            raise ValidationError(
                {"category": f"Invalid category '{category}'. Allowed: {list(TicketCategory.values)}"}
            )

        clean_priority = str(priority or "").upper().strip()
        if clean_priority not in TicketPriority.values:
            raise ValidationError(
                {"priority": f"Invalid priority '{priority}'. Allowed: {list(TicketPriority.values)}"}
            )

        # Validate Branch
        branch = None
        if branch_id:
            branch = Branch.objects.filter(branch_id=str(branch_id).strip(), tenant=tenant).first()

        ticket_id = f"tkt-{uuid.uuid4().hex[:12]}"
        ticket_number = f"TKT-2026-{uuid.uuid4().hex[:6].upper()}"

        ticket = PlatformSupportTicket.objects.create(
            ticket_id=ticket_id,
            ticket_number=ticket_number,
            tenant=tenant,
            branch=branch,
            created_by=user,
            title=clean_title,
            description=clean_description,
            category=clean_category,
            priority=clean_priority,
            status=TicketStatus.OPEN,
        )

        # Handle initial attachments
        if attachments:
            cls._save_attachments(ticket=ticket, message=None, attachments=attachments, uploaded_by=user)

        # Log initial action history
        TicketActionHistory.objects.create(
            history_id=f"act-{uuid.uuid4().hex[:12]}",
            ticket=ticket,
            actor=user,
            action="CREATED",
            new_value="OPEN",
            notes=f"Support ticket '{ticket.title}' submitted.",
        )

        AuditLogEntry.objects.create(
            action="SUPPORT_TICKET_CREATED",
            resource_type="SUPPORT_TICKET",
            resource_id=ticket.ticket_id,
            tenant_id=tenant.tenant_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "ticket_number": ticket.ticket_number,
                "category": clean_category,
                "priority": clean_priority,
                "created_by": user.email,
            },
        )

        return cls.serialize_ticket(ticket=ticket, viewer=user)

    @classmethod
    @transaction.atomic
    def assign_ticket(cls, operator: PharmacyUser, ticket_id: str, assignee_user_id: str) -> Dict[str, Any]:
        """
        Assigns a support ticket to a platform operator.
        Strictly restricted to platform administrators.
        """
        cls._require_platform_operator(operator)
        clean_id = str(ticket_id or "").strip()
        ticket = PlatformSupportTicket.objects.filter(ticket_id=clean_id).first()
        if not ticket:
            raise ValidationError({"ticket_id": f"Support ticket '{clean_id}' does not exist."})

        clean_assignee_id = str(assignee_user_id or "").strip()
        assignee = PharmacyUser.objects.filter(user_id=clean_assignee_id).first()
        if not assignee:
            raise ValidationError({"assignee_user_id": f"Platform operator '{clean_assignee_id}' does not exist."})

        old_assignee_email = ticket.assigned_to.email if ticket.assigned_to else "Unassigned"
        ticket.assigned_to = assignee
        if ticket.status == TicketStatus.OPEN:
            ticket.status = TicketStatus.IN_PROGRESS
        ticket.save(update_fields=["assigned_to", "status", "updated_at"])

        TicketActionHistory.objects.create(
            history_id=f"act-{uuid.uuid4().hex[:12]}",
            ticket=ticket,
            actor=operator,
            action="ASSIGNED",
            old_value=old_assignee_email,
            new_value=assignee.email,
            notes=f"Assigned to {getattr(assignee, 'full_name', '') or assignee.email}",
        )

        AuditLogEntry.objects.create(
            action="SUPPORT_TICKET_ASSIGNED",
            resource_type="SUPPORT_TICKET",
            resource_id=ticket.ticket_id,
            tenant_id=ticket.tenant_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "assigned_to": assignee.email,
                "assigned_by": operator.email,
            },
        )

        return cls.serialize_ticket(ticket=ticket, viewer=operator)

    @classmethod
    @transaction.atomic
    def update_status(
        cls,
        user: PharmacyUser,
        ticket_id: str,
        new_status: str,
        resolution_summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Updates the lifecycle status of a support ticket.
        """
        clean_id = str(ticket_id or "").strip()
        ticket = PlatformSupportTicket.objects.filter(ticket_id=clean_id).first()
        if not ticket:
            raise ValidationError({"ticket_id": f"Support ticket '{clean_id}' does not exist."})

        # Tenant isolation check
        cls._verify_tenant_access(user=user, tenant_id=ticket.tenant_id)

        clean_status = str(new_status or "").upper().strip()
        if clean_status not in TicketStatus.values:
            raise ValidationError(
                {"status": f"Invalid ticket status '{new_status}'. Allowed: {list(TicketStatus.values)}"}
            )

        user_role = str(getattr(user, "role", "")).upper().strip()
        is_platform = user_role in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR] or getattr(
            user, "is_superuser", False
        )

        # Tenant staff can only close or reopen tickets, not mark in-progress
        if not is_platform and clean_status not in [TicketStatus.CLOSED, TicketStatus.OPEN]:
            raise PermissionDenied("Access denied: Pharmacy staff can only close their ticket or reopen it.")

        old_status = ticket.status
        ticket.status = clean_status
        now = timezone.now()

        update_fields = ["status", "updated_at"]
        if clean_status == TicketStatus.RESOLVED:
            ticket.resolved_at = now
            ticket.resolution_summary = str(resolution_summary or "").strip()
            update_fields.extend(["resolved_at", "resolution_summary"])
        elif clean_status == TicketStatus.CLOSED:
            ticket.closed_at = now
            update_fields.append("closed_at")

        ticket.save(update_fields=update_fields)

        TicketActionHistory.objects.create(
            history_id=f"act-{uuid.uuid4().hex[:12]}",
            ticket=ticket,
            actor=user,
            action="STATUS_CHANGED",
            old_value=old_status,
            new_value=clean_status,
            notes=str(resolution_summary or f"Status transitioned to {clean_status}").strip(),
        )

        AuditLogEntry.objects.create(
            action="SUPPORT_TICKET_STATUS_UPDATED",
            resource_type="SUPPORT_TICKET",
            resource_id=ticket.ticket_id,
            tenant_id=ticket.tenant_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "old_status": old_status,
                "new_status": clean_status,
                "updated_by": user.email,
            },
        )

        return cls.serialize_ticket(ticket=ticket, viewer=user)

    @classmethod
    @transaction.atomic
    def add_message(
        cls,
        user: PharmacyUser,
        ticket_id: str,
        message_text: str,
        is_internal: bool = False,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Adds a reply or an internal private note to a support ticket.
        Internal notes (is_internal=True) are strictly restricted to platform staff.
        """
        clean_id = str(ticket_id or "").strip()
        ticket = PlatformSupportTicket.objects.filter(ticket_id=clean_id).first()
        if not ticket:
            raise ValidationError({"ticket_id": f"Support ticket '{clean_id}' does not exist."})

        cls._verify_tenant_access(user=user, tenant_id=ticket.tenant_id)

        clean_text = str(message_text or "").strip()
        if len(clean_text) < 2:
            raise ValidationError({"message_text": "Message text must be at least 2 characters long."})

        user_role = str(getattr(user, "role", "")).upper().strip()
        is_platform = user_role in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR] or getattr(
            user, "is_superuser", False
        )

        # Invariant: Tenant users cannot post internal notes!
        if is_internal and not is_platform:
            raise PermissionDenied("Access denied: Pharmacy users cannot create private internal notes.")

        message_id = f"msg-{uuid.uuid4().hex[:12]}"
        msg = TicketMessage.objects.create(
            message_id=message_id,
            ticket=ticket,
            author=user,
            message_text=clean_text,
            is_internal=is_internal,
        )

        if attachments:
            cls._save_attachments(ticket=ticket, message=msg, attachments=attachments, uploaded_by=user)

        # Automatic status transition: if tenant replies and status was WAITING_ON_TENANT, advance to IN_PROGRESS
        if not is_platform and ticket.status == TicketStatus.WAITING_ON_TENANT:
            ticket.status = TicketStatus.IN_PROGRESS
            ticket.save(update_fields=["status", "updated_at"])

        action_type = "INTERNAL_NOTE_ADDED" if is_internal else "REPLY_ADDED"
        TicketActionHistory.objects.create(
            history_id=f"act-{uuid.uuid4().hex[:12]}",
            ticket=ticket,
            actor=user,
            action=action_type,
            notes=clean_text[:100],
        )

        return cls.serialize_ticket(ticket=ticket, viewer=user)

    @classmethod
    def get_ticket_detail(cls, user: PharmacyUser, ticket_id: str) -> Dict[str, Any]:
        """
        Fetches full ticket details with threaded discussion, attachments, and action history.
        Enforces confidentiality: filters out internal notes for non-platform users.
        """
        clean_id = str(ticket_id or "").strip()
        ticket = (
            PlatformSupportTicket.objects.select_related("tenant", "branch", "created_by", "assigned_to")
            .filter(ticket_id=clean_id)
            .first()
        )
        if not ticket:
            raise ValidationError({"ticket_id": f"Support ticket '{clean_id}' does not exist."})

        cls._verify_tenant_access(user=user, tenant_id=ticket.tenant_id)
        return cls.serialize_ticket(ticket=ticket, viewer=user)

    @classmethod
    def list_tickets(
        cls,
        user: PharmacyUser,
        tenant_id: Optional[str] = None,
        status_filter: Optional[str] = None,
        priority_filter: Optional[str] = None,
        category_filter: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """
        Lists support tickets with pagination, filters, and role-appropriate boundaries.
        """
        user_role = str(getattr(user, "role", "")).upper().strip()
        is_platform = user_role in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR] or getattr(
            user, "is_superuser", False
        )

        qs = PlatformSupportTicket.objects.select_related("tenant", "created_by", "assigned_to").all()

        if not is_platform:
            # Force tenant scope for tenant users
            qs = qs.filter(tenant_id=user.tenant_id)
        elif tenant_id:
            qs = qs.filter(tenant_id=str(tenant_id).strip())

        if status_filter:
            qs = qs.filter(status=status_filter.upper().strip())
        if priority_filter:
            qs = qs.filter(priority=priority_filter.upper().strip())
        if category_filter:
            qs = qs.filter(category=category_filter.upper().strip())

        total = qs.count()
        p = max(1, page)
        ps = min(100, max(1, page_size))
        offset = (p - 1) * ps
        page_items = qs[offset : offset + ps]

        serialized = [cls.serialize_ticket(ticket=t, viewer=user) for t in page_items]
        return {
            "total": total,
            "page": p,
            "page_size": ps,
            "total_pages": (total + ps - 1) // ps if ps > 0 else 1,
            "items": serialized,
        }

    @classmethod
    def serialize_ticket(cls, ticket: PlatformSupportTicket, viewer: PharmacyUser) -> Dict[str, Any]:
        """
        Serializes ticket into JSON schema format with confidentiality protection.
        Internal notes (is_internal=True) are strictly excluded for store personas.
        """
        user_role = str(getattr(viewer, "role", "")).upper().strip()
        is_platform = user_role in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR] or getattr(
            viewer, "is_superuser", False
        )

        # Messages filtering
        msg_qs = ticket.messages.select_related("author").order_by("created_at")
        if not is_platform:
            msg_qs = msg_qs.filter(is_internal=False)

        messages_list = []
        for m in msg_qs:
            messages_list.append(
                {
                    "message_id": m.message_id,
                    "author_email": m.author.email,
                    "author_role": m.author.role,
                    "message_text": m.message_text,
                    "is_internal": m.is_internal,
                    "created_at": m.created_at.isoformat(),
                }
            )

        # Attachments
        attachments_list = []
        for a in ticket.attachments.select_related("uploaded_by").order_by("created_at"):
            attachments_list.append(
                {
                    "attachment_id": a.attachment_id,
                    "file_name": a.file_name,
                    "file_url": a.file_url,
                    "file_size_bytes": a.file_size_bytes,
                    "mime_type": a.mime_type,
                    "uploaded_by_email": a.uploaded_by.email if a.uploaded_by else None,
                    "created_at": a.created_at.isoformat(),
                }
            )

        # Action History
        history_qs = ticket.action_history.select_related("actor").order_by("-timestamp")
        history_list = []
        for h in history_qs:
            # Hide internal note events from tenant users
            if not is_platform and h.action == "INTERNAL_NOTE_ADDED":
                continue
            history_list.append(
                {
                    "history_id": h.history_id,
                    "actor_email": h.actor.email if h.actor else "System",
                    "action": h.action,
                    "old_value": h.old_value or None,
                    "new_value": h.new_value or None,
                    "notes": h.notes or None,
                    "timestamp": h.timestamp.isoformat(),
                }
            )

        chip = cls.resolve_status_chip(ticket.status)

        return {
            "ticket_id": ticket.ticket_id,
            "ticket_number": ticket.ticket_number,
            "tenant_id": ticket.tenant_id,
            "branch_id": ticket.branch.branch_id if ticket.branch else None,
            "title": ticket.title,
            "description": ticket.description,
            "category": ticket.category,
            "priority": ticket.priority,
            "status": ticket.status,
            "status_chip": chip,
            "assigned_to": (
                {
                    "user_id": str(ticket.assigned_to.user_id),
                    "email": ticket.assigned_to.email,
                    "full_name": getattr(ticket.assigned_to, "full_name", "") or ticket.assigned_to.email,
                }
                if ticket.assigned_to
                else None
            ),
            "created_by": {
                "user_id": str(ticket.created_by.user_id),
                "email": ticket.created_by.email,
                "full_name": getattr(ticket.created_by, "full_name", "") or ticket.created_by.email,
                "role": ticket.created_by.role,
            },
            "resolution_summary": ticket.resolution_summary or None,
            "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
            "closed_at": ticket.closed_at.isoformat() if ticket.closed_at else None,
            "created_at": ticket.created_at.isoformat(),
            "updated_at": ticket.updated_at.isoformat(),
            "messages": messages_list,
            "attachments": attachments_list,
            "action_history": history_list,
        }

    @classmethod
    def _save_attachments(
        cls,
        ticket: PlatformSupportTicket,
        message: Optional[TicketMessage],
        attachments: List[Dict[str, Any]],
        uploaded_by: PharmacyUser,
    ):
        for att in attachments:
            file_name = str(att.get("file_name", "unnamed_file")).strip()
            file_url = str(att.get("file_url", "")).strip()
            file_size = int(att.get("file_size_bytes", 0))

            if file_size > cls.MAX_ATTACHMENT_SIZE_BYTES:
                raise ValidationError(
                    {"attachments": f"File '{file_name}' exceeds maximum permitted attachment limit of 10MB."}
                )

            TicketAttachment.objects.create(
                attachment_id=f"att-{uuid.uuid4().hex[:12]}",
                ticket=ticket,
                message=message,
                file_name=file_name,
                file_url=file_url or f"/storage/tickets/{ticket.ticket_id}/{file_name}",
                file_size_bytes=file_size,
                mime_type=str(att.get("mime_type", "application/octet-stream")),
                uploaded_by=uploaded_by,
            )

    @classmethod
    def _require_platform_operator(cls, user: PharmacyUser):
        user_role = str(getattr(user, "role", "")).upper().strip()
        if user_role not in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR] and not getattr(
            user, "is_superuser", False
        ):
            raise PermissionDenied(
                "Access denied: Operation restricted to Platform Super Administrators and Operators."
            )

    @classmethod
    def _verify_tenant_access(cls, user: PharmacyUser, tenant_id: str):
        user_role = str(getattr(user, "role", "")).upper().strip()
        is_platform = user_role in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR] or getattr(
            user, "is_superuser", False
        )
        if not is_platform and str(getattr(user, "tenant_id", "")) != str(tenant_id):
            raise PermissionDenied(
                detail={
                    "error_code": "CROSS_TENANT_VIOLATION",
                    "message_en": f"Access denied: Cannot access support tickets for foreign tenant '{tenant_id}'.",
                    "message_bn": f"অন্য ফার্মেসির ('{tenant_id}') টিকিট দেখা বা পরিবর্তন করা সম্পূর্ণ নিষিদ্ধ।",
                }
            )
