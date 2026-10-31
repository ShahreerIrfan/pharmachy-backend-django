"""
backend/apps/core/support_access_service.py

Architecture Decision: ADR-0068
Secure Delegated Support Access, Scoped Support Grants, and Break-Glass Governance.
Explicitly bans unsafe impersonation ("ghost logins"), password bypasses, or arbitrary tenant user JWT issuance.
Enforces tenant owner consent, narrow scopes, automatic expiration, break-glass audit governance,
and non-dismissible client notice banners.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import SupportAccessGrant, SupportAccessScope, SupportAccessStatus, Tenant
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger(__name__)


class SupportAccessService:
    """
    Central orchestration service for tenant support access governance.
    Provides auditable, time-bounded support grants without ever sharing secrets or impersonating identities.
    """

    ALLOWED_SCOPES = set(SupportAccessScope.values)
    MIN_DURATION_MINUTES = 15
    MAX_DURATION_MINUTES = 120
    DEFAULT_DURATION_MINUTES = 30

    @classmethod
    def request_support_access(
        cls,
        operator: PharmacyUser,
        tenant_id: str,
        scope: str = SupportAccessScope.DIAGNOSTIC_READ,
        reason: str = "",
        ticket_reference: Optional[str] = None,
        duration_minutes: int = DEFAULT_DURATION_MINUTES,
        is_break_glass: bool = False,
        break_glass_justification: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Submits a request for delegated support access.
        Standard access requires pharmacy owner approval.
        Break-glass emergency mode requires Super Admin role, documented justification (>= 15 chars),
        and dispatches high-priority audit alerts.
        """
        # 1. Verify operator role
        operator_role = str(getattr(operator, "role", "")).upper().strip()
        if operator_role not in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR] and not getattr(
            operator, "is_superuser", False
        ):
            raise PermissionDenied(
                "Access denied: Only platform administrators and support operators can request support access."
            )

        # 2. Verify target tenant exists
        clean_tenant_id = str(tenant_id or "").strip()
        tenant = Tenant.objects.filter(tenant_id=clean_tenant_id).first()
        if not tenant:
            raise ValidationError({"tenant_id": f"Target tenant '{clean_tenant_id}' does not exist on platform."})

        # 3. Validate Scope
        clean_scope = str(scope or "").upper().strip()
        if clean_scope not in cls.ALLOWED_SCOPES:
            raise ValidationError(
                {"scope": f"Invalid scope '{scope}'. Allowed scopes: {sorted(list(cls.ALLOWED_SCOPES))}"}
            )

        # 4. Validate Reason
        clean_reason = str(reason or "").strip()
        if len(clean_reason) < 10:
            raise ValidationError(
                {"reason": "A detailed technical reason of at least 10 characters is required for support access."}
            )

        # 5. Validate Duration
        try:
            dur = int(duration_minutes)
        except (ValueError, TypeError):
            dur = cls.DEFAULT_DURATION_MINUTES
        if dur < cls.MIN_DURATION_MINUTES or dur > cls.MAX_DURATION_MINUTES:
            raise ValidationError(
                {
                    "duration_minutes": (
                        f"Duration must be between {cls.MIN_DURATION_MINUTES} and {cls.MAX_DURATION_MINUTES} minutes."
                    )
                }
            )

        # 6. Evaluate Break-Glass
        grant_id = f"sup-{uuid.uuid4().hex[:12]}"
        now = timezone.now()

        if is_break_glass:
            # Strictly Super Admin only
            if operator_role != UserRole.SUPERADMIN and not getattr(operator, "is_superuser", False):
                raise PermissionDenied("Emergency break-glass access is strictly restricted to Super Administrators.")

            clean_bg_justification = str(break_glass_justification or "").strip()
            if len(clean_bg_justification) < 15:
                raise ValidationError(
                    {
                        "break_glass_justification": (
                            "Break-glass emergency declaration requires a comprehensive documented justification "
                            "of at least 15 characters."
                        )
                    }
                )

            # Auto-approve under Break-Glass
            expires_at = now + timedelta(minutes=dur)
            token = f"sat_bg_{uuid.uuid4().hex}"
            grant = SupportAccessGrant.objects.create(
                grant_id=grant_id,
                tenant=tenant,
                requested_by_operator=operator,
                approved_by_user=operator,
                scope=clean_scope,
                status=SupportAccessStatus.APPROVED,
                reason=clean_reason,
                ticket_reference=str(ticket_reference or "").strip(),
                is_break_glass=True,
                break_glass_justification=clean_bg_justification,
                duration_minutes=dur,
                approved_at=now,
                expires_at=expires_at,
                access_token=token,
            )

            # Record tamper-evident audit alert
            AuditLogEntry.objects.create(
                action="SUPPORT_ACCESS_BREAK_GLASS_TRIGGERED",
                resource_type="SUPPORT_ACCESS_GRANT",
                resource_id=grant.grant_id,
                tenant_id=tenant.tenant_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "operator_id": str(getattr(operator, "user_id", None) or operator),
                    "operator_email": operator.email,
                    "tenant_id": tenant.tenant_id,
                    "scope": clean_scope,
                    "duration_minutes": dur,
                    "ticket_reference": ticket_reference,
                    "break_glass_justification": clean_bg_justification,
                    "expires_at": expires_at.isoformat(),
                },
            )
        else:
            # Standard support request requires Pharmacy Owner approval
            grant = SupportAccessGrant.objects.create(
                grant_id=grant_id,
                tenant=tenant,
                requested_by_operator=operator,
                scope=clean_scope,
                status=SupportAccessStatus.PENDING,
                reason=clean_reason,
                ticket_reference=str(ticket_reference or "").strip(),
                is_break_glass=False,
                duration_minutes=dur,
            )

            AuditLogEntry.objects.create(
                action="SUPPORT_ACCESS_REQUESTED",
                resource_type="SUPPORT_ACCESS_GRANT",
                resource_id=grant.grant_id,
                tenant_id=tenant.tenant_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "operator_id": str(getattr(operator, "user_id", None) or operator),
                    "operator_email": operator.email,
                    "tenant_id": tenant.tenant_id,
                    "scope": clean_scope,
                    "duration_minutes": dur,
                    "ticket_reference": ticket_reference,
                },
            )

        return cls.serialize_grant(grant)

    @classmethod
    def approve_support_access(cls, user: PharmacyUser, grant_id: str) -> Dict[str, Any]:
        """
        Approves a pending support access request.
        Must be approved by the target pharmacy's PHARMACY_OWNER or a secondary Super Admin (dual-custody rule).
        Generates a time-bounded support access token.
        """
        clean_id = str(grant_id or "").strip()
        grant = SupportAccessGrant.objects.filter(grant_id=clean_id).first()
        if not grant:
            raise ValidationError({"grant_id": f"Support access request '{clean_id}' does not exist."})

        if grant.status != SupportAccessStatus.PENDING:
            raise ValidationError(
                {"status": f"Cannot approve support grant in '{grant.status}' state. Must be 'PENDING'."}
            )

        user_role = str(getattr(user, "role", "")).upper().strip()
        is_owner = user_role == UserRole.PHARMACY_OWNER and str(user.tenant_id) == str(grant.tenant.tenant_id)
        is_admin = user_role == UserRole.SUPERADMIN or getattr(user, "is_superuser", False)

        if not (is_owner or is_admin):
            raise PermissionDenied(
                "Access denied: Only the target pharmacy owner or an authorized administrator can approve support access."
            )

        # Dual-person rule for administrators: cannot approve own non-break-glass request
        if is_admin and not is_owner and str(grant.requested_by_operator_id) == str(getattr(user, "user_id", None)):
            raise PermissionDenied(
                "Dual-person rule violation: Platform operators cannot approve their own support requests."
            )

        now = timezone.now()
        expires_at = now + timedelta(minutes=grant.duration_minutes)
        token = f"sat_{uuid.uuid4().hex}"

        grant.status = SupportAccessStatus.APPROVED
        grant.approved_by_user = user
        grant.approved_at = now
        grant.expires_at = expires_at
        grant.access_token = token
        grant.save()

        AuditLogEntry.objects.create(
            action="SUPPORT_ACCESS_APPROVED",
            resource_type="SUPPORT_ACCESS_GRANT",
            resource_id=grant.grant_id,
            tenant_id=grant.tenant.tenant_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "approver_id": str(getattr(user, "user_id", None) or user),
                "approver_email": user.email,
                "approver_role": user_role,
                "expires_at": expires_at.isoformat(),
            },
        )

        return cls.serialize_grant(grant)

    @classmethod
    def reject_support_access(cls, user: PharmacyUser, grant_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """
        Rejects a pending support access request.
        """
        clean_id = str(grant_id or "").strip()
        grant = SupportAccessGrant.objects.filter(grant_id=clean_id).first()
        if not grant:
            raise ValidationError({"grant_id": f"Support access request '{clean_id}' does not exist."})

        if grant.status != SupportAccessStatus.PENDING:
            raise ValidationError(
                {"status": f"Cannot reject support grant in '{grant.status}' state. Must be 'PENDING'."}
            )

        user_role = str(getattr(user, "role", "")).upper().strip()
        is_owner = user_role == UserRole.PHARMACY_OWNER and str(user.tenant_id) == str(grant.tenant.tenant_id)
        is_admin = user_role == UserRole.SUPERADMIN or getattr(user, "is_superuser", False)

        if not (is_owner or is_admin):
            raise PermissionDenied("Access denied: Not authorized to reject this support access request.")

        grant.status = SupportAccessStatus.REJECTED
        grant.revocation_reason = str(reason or "Rejected by pharmacy owner/administrator.").strip()
        grant.save(update_fields=["status", "revocation_reason", "updated_at"])

        AuditLogEntry.objects.create(
            action="SUPPORT_ACCESS_REJECTED",
            resource_type="SUPPORT_ACCESS_GRANT",
            resource_id=grant.grant_id,
            tenant_id=grant.tenant.tenant_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "rejector_id": str(getattr(user, "user_id", None) or user),
                "rejector_email": user.email,
                "reason": grant.revocation_reason,
            },
        )

        return cls.serialize_grant(grant)

    @classmethod
    def revoke_support_access(cls, user: PharmacyUser, grant_id: str, reason: str = "") -> Dict[str, Any]:
        """
        Instantly revokes an active support access grant (Emergency Kill Switch).
        Can be executed by the target pharmacy owner or a platform super administrator.
        """
        clean_id = str(grant_id or "").strip()
        grant = SupportAccessGrant.objects.filter(grant_id=clean_id).first()
        if not grant:
            raise ValidationError({"grant_id": f"Support access grant '{clean_id}' does not exist."})

        grant.expire_if_needed()
        if grant.status != SupportAccessStatus.APPROVED:
            raise ValidationError({"status": f"Cannot revoke support grant in '{grant.status}' state."})

        user_role = str(getattr(user, "role", "")).upper().strip()
        is_owner = user_role == UserRole.PHARMACY_OWNER and str(user.tenant_id) == str(grant.tenant.tenant_id)
        is_admin = user_role == UserRole.SUPERADMIN or getattr(user, "is_superuser", False)

        if not (is_owner or is_admin):
            raise PermissionDenied("Access denied: Not authorized to revoke this support access grant.")

        clean_reason = str(reason or "Revoked via emergency kill switch.").strip()
        now = timezone.now()

        grant.status = SupportAccessStatus.REVOKED
        grant.revoked_at = now
        grant.revocation_reason = clean_reason
        grant.revoked_by_user = user
        grant.save(update_fields=["status", "revoked_at", "revocation_reason", "revoked_by_user", "updated_at"])

        AuditLogEntry.objects.create(
            action="SUPPORT_ACCESS_REVOKED",
            resource_type="SUPPORT_ACCESS_GRANT",
            resource_id=grant.grant_id,
            tenant_id=grant.tenant.tenant_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "revoker_id": str(getattr(user, "user_id", None) or user),
                "revoker_email": user.email,
                "revoker_role": user_role,
                "reason": clean_reason,
                "revoked_at": now.isoformat(),
            },
        )

        return cls.serialize_grant(grant)

    @classmethod
    def get_active_support_notice(cls, tenant_id: str) -> Dict[str, Any]:
        """
        Inspects whether any active support access grant is currently in effect for a tenant.
        Automatically retires expired grants.
        Returns the real-time notice banner configuration for JavaFX Desktop POS and Flutter Mobile Manager.
        """
        clean_id = str(tenant_id or "").strip()
        now = timezone.now()

        # Retire any past-due grants
        expired_candidates = SupportAccessGrant.objects.filter(
            tenant_id=clean_id,
            status=SupportAccessStatus.APPROVED,
            expires_at__lte=now,
        )
        for g in expired_candidates:
            g.expire_if_needed()

        active_grant = (
            SupportAccessGrant.objects.filter(
                tenant_id=clean_id,
                status=SupportAccessStatus.APPROVED,
                expires_at__gt=now,
            )
            .order_by("-approved_at")
            .first()
        )

        if not active_grant:
            return {
                "show_banner": False,
                "banner_text_en": "",
                "banner_text_bn": "",
                "tone": "neutral",
                "can_revoke": False,
                "grant_id": None,
                "seconds_remaining": 0,
            }

        remaining_secs = max(0, int((active_grant.expires_at - now).total_seconds()))
        operator_email = active_grant.requested_by_operator.email

        if active_grant.is_break_glass:
            text_en = (
                f"EMERGENCY BREAK-GLASS SUPPORT: Platform administrator '{operator_email}' is actively repairing "
                f"issues under scope '{active_grant.scope}' (Expires in {remaining_secs // 60}m)."
            )
            text_bn = (
                f"জরুরি ব্রেক-গ্লাস সাপোর্ট: প্ল্যাটফর্ম অ্যাডমিন '{operator_email}' বর্তমানে '{active_grant.scope}' "
                f"স্কোপে সহায়তা প্রদান করছেন (মেয়াদ বাকি: {remaining_secs // 60} মিনিট)।"
            )
            tone = "danger"
        else:
            text_en = (
                f"Active Support Access: Support engineer '{operator_email}' is assisting under scope "
                f"'{active_grant.scope}' (Expires in {remaining_secs // 60}m)."
            )
            text_bn = (
                f"সক্রিয় সাপোর্ট অ্যাক্সেস: প্রকৌশলী '{operator_email}' বর্তমানে '{active_grant.scope}' "
                f"স্কোপে সহায়তা প্রদান করছেন (মেয়াদ বাকি: {remaining_secs // 60} মিনিট)।"
            )
            tone = "warning"

        return {
            "show_banner": True,
            "banner_text_en": text_en,
            "banner_text_bn": text_bn,
            "tone": tone,
            "can_revoke": True,
            "grant_id": active_grant.grant_id,
            "seconds_remaining": remaining_secs,
        }

    @classmethod
    def validate_support_token(cls, token: str) -> Tuple[bool, Optional[SupportAccessGrant], Optional[str]]:
        """
        Validates a delegated support access token.
        Guarantees that expired or revoked tokens immediately fail authorization.
        """
        clean_token = str(token or "").strip()
        if not clean_token:
            return False, None, "MISSING_SUPPORT_TOKEN"

        grant = SupportAccessGrant.objects.filter(access_token=clean_token).first()
        if not grant:
            return False, None, "INVALID_SUPPORT_TOKEN"

        grant.expire_if_needed()
        if grant.status == SupportAccessStatus.EXPIRED:
            return False, grant, "SUPPORT_ACCESS_EXPIRED"
        if grant.status == SupportAccessStatus.REVOKED:
            return False, grant, "SUPPORT_ACCESS_REVOKED"
        if grant.status != SupportAccessStatus.APPROVED:
            return False, grant, "SUPPORT_ACCESS_NOT_ACTIVE"

        return True, grant, None

    @classmethod
    def serialize_grant(cls, grant: SupportAccessGrant) -> Dict[str, Any]:
        """
        Serializes a SupportAccessGrant adhering to ADR-0068 and JSON schema specification.
        """
        grant.expire_if_needed()
        notice = (
            cls.get_active_support_notice(grant.tenant_id) if grant.status == SupportAccessStatus.APPROVED else None
        )

        return {
            "grant_id": grant.grant_id,
            "tenant_id": grant.tenant_id,
            "scope": grant.scope,
            "status": grant.status,
            "reason": grant.reason,
            "ticket_reference": grant.ticket_reference or None,
            "is_break_glass": grant.is_break_glass,
            "break_glass_justification": grant.break_glass_justification or None,
            "duration_minutes": grant.duration_minutes,
            "operator_email": grant.requested_by_operator.email,
            "approved_by_email": grant.approved_by_user.email if grant.approved_by_user else None,
            "approved_at": grant.approved_at.isoformat() if grant.approved_at else None,
            "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
            "revoked_at": grant.revoked_at.isoformat() if grant.revoked_at else None,
            "revocation_reason": grant.revocation_reason or None,
            "access_token": grant.access_token if grant.status == SupportAccessStatus.APPROVED else None,
            "created_at": grant.created_at.isoformat(),
            "notice_banner": (
                notice
                if notice
                else {
                    "show_banner": False,
                    "banner_text_en": "",
                    "banner_text_bn": "",
                    "tone": "neutral",
                    "can_revoke": False,
                    "seconds_remaining": 0,
                }
            ),
        }
