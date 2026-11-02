"""
Approval Workflow Service for P108 Manager and Supervisor Approvals (Maker-Checker).
Architecture Decision: ADR-0108.
Implements dual-control segregation of duties across high discounts, refunds,
stock write-offs, sensitive operational expenses, and clinical escalations.
"""

import logging
import secrets
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.core.models import (
    ApprovalAuditEvent,
    ApprovalRequest,
    ApprovalRequestType,
    ApprovalStatus,
    ApprovalUrgency,
    Branch,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.postgres_service import CrossTenantOperationError
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger(__name__)


class SubscriptionSuspendedError(Exception):
    """Raised when tenant subscription is suspended preventing mutations."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class MakerCheckerViolationError(Exception):
    """Raised when a user attempts to approve or check their own request."""

    def __init__(self, detail: Dict[str, Any]):
        self.detail = detail
        super().__init__(str(detail))


class InsufficientApprovalAuthorityError(Exception):
    """Raised when a checker lacks the necessary role or authority."""

    def __init__(self, detail: Dict[str, Any]):
        self.detail = detail
        super().__init__(str(detail))


class ApprovalWorkflowService:
    """Core domain service orchestrating dual-control Maker-Checker approval lifecycles."""

    @classmethod
    def _get_tenant(cls, tenant_id: str) -> Tenant:
        try:
            return Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise CrossTenantOperationError(f"Tenant '{tenant_id}' does not exist.")

    @classmethod
    def _check_subscription_active(cls, tenant: Tenant) -> None:
        if tenant.status == TenantLifecycleStatus.SUSPENDED:
            raise SubscriptionSuspendedError(
                f"Tenant '{tenant.tenant_id}' subscription is suspended. Maker-checker mutations are locked."
            )

    @classmethod
    def _get_actor_name(cls, user: Optional[Any]) -> str:
        if not user:
            return ""
        return (
            getattr(user, "username", None)
            or getattr(user, "email", None)
            or getattr(user, "full_name", None)
            or str(user)
        )

    @classmethod
    def submit_request(
        cls,
        tenant_id: str,
        branch_id: str,
        maker_user: PharmacyUser,
        approval_type: str,
        title: str,
        reason: str,
        amount_bdt: Decimal = Decimal("0.00"),
        discount_percent: Decimal = Decimal("0.00"),
        urgency: str = ApprovalUrgency.ROUTINE,
        payload: Optional[Dict[str, Any]] = None,
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Submits a new Maker-Checker approval request.
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        # Tenant isolation check for maker
        if maker_user.tenant_id != tenant.tenant_id:
            raise CrossTenantOperationError("Maker does not belong to the target tenant.")

        # Verify branch belongs to tenant
        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise CrossTenantOperationError(f"Branch '{branch_id}' not found for tenant '{tenant_id}'.")

        clean_type = (approval_type or "").strip().upper()
        if clean_type not in ApprovalRequestType.values:
            raise ValidationError(
                {"error_code": "INVALID_APPROVAL_TYPE", "message": f"Unsupported approval type '{approval_type}'."}
            )

        clean_urgency = (urgency or "").strip().upper()
        if clean_urgency not in ApprovalUrgency.values:
            clean_urgency = ApprovalUrgency.ROUTINE

        clean_title = (title or "").strip()
        if len(clean_title) < 3:
            raise ValidationError(
                {"error_code": "INVALID_TITLE", "message": "Approval title must be at least 3 characters."}
            )

        clean_reason = (reason or "").strip()
        if len(clean_reason) < 4:
            raise ValidationError(
                {"error_code": "INVALID_REASON", "message": "Detailed operational justification (minimum 4 characters) is mandatory."}
            )

        dec_amount = Decimal(str(amount_bdt or "0.00"))
        if dec_amount < Decimal("0.00"):
            raise ValidationError({"error_code": "INVALID_AMOUNT", "message": "Amount BDT cannot be negative."})

        dec_discount = Decimal(str(discount_percent or "0.00"))
        if dec_discount < Decimal("0.00") or dec_discount > Decimal("100.00"):
            raise ValidationError(
                {"error_code": "INVALID_DISCOUNT_PERCENT", "message": "Discount percent must be between 0.00% and 100.00%."}
            )

        req_id = f"appr-{uuid.uuid4().hex[:12]}"
        maker_ident = cls._get_actor_name(maker_user)

        with transaction.atomic():
            approval_req = ApprovalRequest.objects.create(
                request_id=req_id,
                tenant=tenant,
                branch=branch,
                approval_type=clean_type,
                status=ApprovalStatus.PENDING,
                urgency=clean_urgency,
                maker_user=maker_user,
                amount_bdt=dec_amount,
                discount_percent=dec_discount,
                title=clean_title,
                reason=clean_reason,
                payload=payload or {},
            )

            ApprovalAuditEvent.objects.create(
                event_id=f"audit-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                approval_request=approval_req,
                action="SUBMITTED",
                actor_username=maker_ident,
                notes=clean_reason,
                metadata={"title": clean_title, "amount_bdt": str(dec_amount), "discount_percent": str(dec_discount)},
                ip_address=ip_address,
            )

        logger.info("Submitted Maker-Checker request %s [%s] by %s", req_id, clean_type, maker_ident)
        return cls.get_request_detail(tenant_id=tenant_id, request_id=req_id)

    @classmethod
    def _assert_checker_authority(
        cls,
        checker_user: PharmacyUser,
        approval_req: ApprovalRequest,
    ) -> None:
        """
        Enforces Dual-Control (Maker != Checker) and Role-specific sign-off authority.
        """
        # 1. Fundamental Dual-Control Segregation
        if checker_user.user_id == approval_req.maker_user_id:
            raise MakerCheckerViolationError(
                {
                    "error_code": "ERR_CANNOT_APPROVE_OWN_REQUEST",
                    "message_en": "Maker-Checker violation: You cannot approve or sign off on your own approval request.",
                    "message_bn": "মেকার-চেকার লঙ্ঘন: আপনি নিজের তৈরি করা অনুমোদনের অনুরোধ নিজে অ্যাপ্রুভ করতে পারবেন না।",
                }
            )

        # 2. Cross-Tenant Barrier
        if checker_user.tenant_id != approval_req.tenant_id:
            raise CrossTenantOperationError("Checker belongs to a different tenant organization.")

        role = getattr(checker_user, "role", "")

        # 3. Superadmin and Pharmacy Owner have tenant-wide authority across all types
        if role in [UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER]:
            return

        # 4. Branch Manager authority
        if role == UserRole.BRANCH_MANAGER:
            # Must match branch
            if checker_user.branch_id and checker_user.branch_id != approval_req.branch_id:
                raise InsufficientApprovalAuthorityError(
                    {
                        "error_code": "ERR_CROSS_BRANCH_VIOLATION",
                        "message_en": f"Branch Manager assigned to branch '{checker_user.branch_id}' cannot approve requests for branch '{approval_req.branch_id}'.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার শুধুমাত্র তার নির্ধারিত ব্রাঞ্চের অনুমোদন প্রদান করতে পারেন।",
                    }
                )
            return

        # 5. Specialized Roles
        if approval_req.approval_type == ApprovalRequestType.CLINICAL_ESCALATION:
            if role == UserRole.STAFF_PHARMACIST:
                return
            raise InsufficientApprovalAuthorityError(
                {
                    "error_code": "ERR_CLINICAL_AUTHORITY_REQUIRED",
                    "message_en": "Clinical escalation sign-offs require a licensed Supervising Pharmacist, Branch Manager, or Owner.",
                    "message_bn": "ক্লিনিক্যাল এস্কেলেশন অনুমোদনের জন্য লাইসেন্সধারী ফার্মাসিস্ট বা ব্রাঞ্চ ম্যানেজারের অনুমোদন প্রয়োজন।",
                }
            )

        if approval_req.approval_type == ApprovalRequestType.SENSITIVE_EXPENSE:
            if role == UserRole.FINANCE_OFFICER:
                return

        if approval_req.approval_type == ApprovalRequestType.REFUND:
            if role == UserRole.FINANCE_OFFICER:
                return

        # Frontline staff (Cashier, Inventory Clerk, Auditor) cannot approve
        raise InsufficientApprovalAuthorityError(
            {
                "error_code": "ERR_INSUFFICIENT_APPROVAL_AUTHORITY",
                "message_en": f"Role '{role}' is not authorized to act as checker for '{approval_req.approval_type}'.",
                "message_bn": f"'{role}' পদবীর জন্য '{approval_req.approval_type}' অনুমোদনের ক্ষমতা নেই।",
            }
        )

    @classmethod
    def approve_request(
        cls,
        tenant_id: str,
        request_id: str,
        checker_user: PharmacyUser,
        decision_notes: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Signs off and approves a pending Maker-Checker request. Generates single-use approval token.
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            req = ApprovalRequest.objects.get(request_id=request_id, tenant=tenant)
        except ApprovalRequest.DoesNotExist:
            raise CrossTenantOperationError(f"Approval request '{request_id}' not found for tenant '{tenant_id}'.")

        if req.status != ApprovalStatus.PENDING:
            raise ValidationError(
                {
                    "error_code": "ERR_REQUEST_NOT_PENDING",
                    "message": f"Approval request '{request_id}' is already {req.status} and cannot be approved.",
                }
            )

        # Enforce Maker-Checker Invariant and Role Authority
        cls._assert_checker_authority(checker_user=checker_user, approval_req=req)

        clean_notes = (decision_notes or "").strip()
        now = timezone.now()
        token = f"tok_appr_{secrets.token_hex(16)}"
        checker_ident = cls._get_actor_name(checker_user)

        with transaction.atomic():
            req.status = ApprovalStatus.APPROVED
            req.checker_user = checker_user
            req.decision_notes = clean_notes
            req.decision_at = now
            req.approval_token = token
            req.save()

            ApprovalAuditEvent.objects.create(
                event_id=f"audit-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                approval_request=req,
                action="APPROVED",
                actor_username=checker_ident,
                notes=clean_notes,
                metadata={"token_prefix": token[:14] + "..."},
                ip_address=ip_address,
            )

        logger.info("Approved Maker-Checker request %s by %s", request_id, checker_ident)
        return cls.get_request_detail(tenant_id=tenant_id, request_id=request_id)

    @classmethod
    def reject_request(
        cls,
        tenant_id: str,
        request_id: str,
        checker_user: PharmacyUser,
        rejection_reason: str,
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Rejects a pending Maker-Checker request with mandatory justification.
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            req = ApprovalRequest.objects.get(request_id=request_id, tenant=tenant)
        except ApprovalRequest.DoesNotExist:
            raise CrossTenantOperationError(f"Approval request '{request_id}' not found for tenant '{tenant_id}'.")

        if req.status != ApprovalStatus.PENDING:
            raise ValidationError(
                {
                    "error_code": "ERR_REQUEST_NOT_PENDING",
                    "message": f"Approval request '{request_id}' is already {req.status} and cannot be rejected.",
                }
            )

        clean_reason = (rejection_reason or "").strip()
        if len(clean_reason) < 4:
            raise ValidationError(
                {"error_code": "REJECTION_REASON_REQUIRED", "message": "Detailed rejection reason (minimum 4 characters) is mandatory."}
            )

        # Enforce Maker-Checker Invariant and Role Authority
        cls._assert_checker_authority(checker_user=checker_user, approval_req=req)

        now = timezone.now()
        checker_ident = cls._get_actor_name(checker_user)

        with transaction.atomic():
            req.status = ApprovalStatus.REJECTED
            req.checker_user = checker_user
            req.decision_notes = clean_reason
            req.decision_at = now
            req.save()

            ApprovalAuditEvent.objects.create(
                event_id=f"audit-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                approval_request=req,
                action="REJECTED",
                actor_username=checker_ident,
                notes=clean_reason,
                metadata={},
                ip_address=ip_address,
            )

        logger.info("Rejected Maker-Checker request %s by %s", request_id, checker_ident)
        return cls.get_request_detail(tenant_id=tenant_id, request_id=request_id)

    @classmethod
    def cancel_request(
        cls,
        tenant_id: str,
        request_id: str,
        maker_user: PharmacyUser,
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Allows the original Maker to cancel a pending request before a decision is made.
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            req = ApprovalRequest.objects.get(request_id=request_id, tenant=tenant)
        except ApprovalRequest.DoesNotExist:
            raise CrossTenantOperationError(f"Approval request '{request_id}' not found for tenant '{tenant_id}'.")

        if req.status != ApprovalStatus.PENDING:
            raise ValidationError(
                {"error_code": "ERR_REQUEST_NOT_PENDING", "message": f"Cannot cancel request in status {req.status}."}
            )

        role = getattr(maker_user, "role", "")
        # Only original maker or owner/superadmin can cancel
        if maker_user.user_id != req.maker_user_id and role not in [UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER]:
            raise ValidationError(
                {"error_code": "ERR_CANNOT_CANCEL_OTHERS_REQUEST", "message": "Only the original maker or tenant owner can cancel this request."}
            )

        maker_ident = cls._get_actor_name(maker_user)

        with transaction.atomic():
            req.status = ApprovalStatus.CANCELLED
            req.decision_at = timezone.now()
            req.decision_notes = "Cancelled by requester."
            req.save()

            ApprovalAuditEvent.objects.create(
                event_id=f"audit-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                approval_request=req,
                action="CANCELLED",
                actor_username=maker_ident,
                notes="Cancelled by requester.",
                metadata={},
                ip_address=ip_address,
            )

        logger.info("Cancelled Maker-Checker request %s by %s", request_id, maker_ident)
        return cls.get_request_detail(tenant_id=tenant_id, request_id=request_id)

    @classmethod
    def verify_token(cls, tenant_id: str, approval_token: str) -> Dict[str, Any]:
        """
        Verifies that an approval token is valid, active, and approved.
        """
        tenant = cls._get_tenant(tenant_id)
        clean_token = (approval_token or "").strip()
        if not clean_token:
            return {"valid": False, "error_code": "EMPTY_TOKEN", "message": "Token cannot be empty."}

        try:
            req = ApprovalRequest.objects.get(tenant=tenant, approval_token=clean_token, status=ApprovalStatus.APPROVED)
            return {
                "valid": True,
                "request_id": req.request_id,
                "approval_type": req.approval_type,
                "branch_id": req.branch_id,
                "amount_bdt": float(req.amount_bdt),
                "discount_percent": float(req.discount_percent),
                "maker_username": cls._get_actor_name(req.maker_user),
                "checker_username": cls._get_actor_name(req.checker_user) if req.checker_user else None,
                "decision_at": req.decision_at.isoformat() if req.decision_at else None,
            }
        except ApprovalRequest.DoesNotExist:
            return {
                "valid": False,
                "error_code": "INVALID_OR_EXPIRED_TOKEN",
                "message": "Approval token does not exist or has expired.",
            }

    @classmethod
    def get_request_detail(cls, tenant_id: str, request_id: str) -> Dict[str, Any]:
        """Returns request detail conforming to maker_checker_approval.schema.json with audit trail."""
        tenant = cls._get_tenant(tenant_id)
        try:
            req = ApprovalRequest.objects.get(request_id=request_id, tenant=tenant)
        except ApprovalRequest.DoesNotExist:
            raise CrossTenantOperationError(f"Approval request '{request_id}' not found for tenant '{tenant_id}'.")

        audits = [
            {
                "event_id": a.event_id,
                "action": a.action,
                "actor_username": a.actor_username,
                "notes": a.notes,
                "metadata": a.metadata,
                "ip_address": a.ip_address,
                "created_at": a.created_at.isoformat(),
            }
            for a in req.audit_trail.all()
        ]

        return {
            "request_id": req.request_id,
            "tenant_id": req.tenant_id,
            "branch_id": req.branch_id,
            "approval_type": req.approval_type,
            "status": req.status,
            "urgency": req.urgency,
            "maker_username": cls._get_actor_name(req.maker_user),
            "checker_username": cls._get_actor_name(req.checker_user) if req.checker_user else None,
            "amount_bdt": float(req.amount_bdt),
            "discount_percent": float(req.discount_percent),
            "title": req.title,
            "reason": req.reason,
            "decision_notes": req.decision_notes or None,
            "approval_token": req.approval_token or None,
            "payload": req.payload,
            "created_at": req.created_at.isoformat(),
            "decision_at": req.decision_at.isoformat() if req.decision_at else None,
            "audit_trail": audits,
        }

    @classmethod
    def list_requests(
        cls,
        tenant_id: str,
        branch_id: Optional[str] = None,
        status: Optional[str] = None,
        approval_type: Optional[str] = None,
        urgency: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists approval requests for a tenant with optional scoping."""
        tenant = cls._get_tenant(tenant_id)
        qs = ApprovalRequest.objects.filter(tenant=tenant)

        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if status:
            qs = qs.filter(status=status.upper())
        if approval_type:
            qs = qs.filter(approval_type=approval_type.upper())
        if urgency:
            qs = qs.filter(urgency=urgency.upper())

        return [cls.get_request_detail(tenant_id=tenant_id, request_id=r.request_id) for r in qs]
