"""
REST API ViewSet for Desktop Login Device Registration & Cryptographic Trust.
Architecture Decision: ADR-0005, ADR-0096, ADR-0111 (P111).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.

Secures Point of Sale (POS) workstations:
- Desktop POS hardware cryptographic public key registration (Ed25519 / RSA-2048)
- Authoritative branch ownership & human-readable terminal labels
- Maker-checker audited approval workflow (PENDING_APPROVAL -> APPROVED / REJECTED / REVOKED)
- Active login verification and cryptographic challenge gate
- Tenant plan quota synchronization via DeviceSeat integration
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.device_registration_service import DeviceRegistrationService
from apps.core.models import LoginDeviceRegistration
from apps.core.postgres_service import CrossTenantOperationError

logger = logging.getLogger("pharmacy.device_registration.views")


class LoginDeviceRegistrationViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for POS workstation cryptographic registration and approval.
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
            or request.headers.get("X-Tenant-ID")
            or request.query_params.get("tenant_id")
        )
        if hasattr(tenant_id, "tenant_id"):
            tenant_id = tenant_id.tenant_id

        target_tenant = request.query_params.get("tenant_id") or request.data.get("tenant_id")
        if target_tenant and role not in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]:
            clean_target = str(target_tenant).strip()
            clean_user_tid = str(tenant_id).strip()
            if clean_user_tid and clean_target != clean_user_tid:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_CROSS_TENANT_VIOLATION",
                        "message_en": f"User from tenant '{clean_user_tid}' cannot access tenant '{clean_target}'.",
                        "message_bn": "ক্রস-টেন্যান্ট অ্যাক্সেস নিষিদ্ধ।",
                    }
                )

        if not tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_NOT_IDENTIFIED",
                    "message_en": "Tenant identification missing from user session or header.",
                    "message_bn": "টেন্যান্ট চিহ্নিত করা যায়নি।",
                }
            )

        user_branch_id = getattr(user, "branch_id", None)
        if hasattr(user_branch_id, "branch_id"):
            user_branch_id = user_branch_id.branch_id

        return str(tenant_id), role, str(user_branch_id) if user_branch_id else None

    @classmethod
    def _serialize_registration(cls, reg: LoginDeviceRegistration) -> Dict[str, Any]:
        """Serializes LoginDeviceRegistration according to login_device_registration.schema.json."""
        return {
            "registration_id": reg.registration_id,
            "tenant_id": reg.tenant_id,
            "branch_id": reg.branch_id,
            "branch_name": reg.branch.name if reg.branch else None,
            "device_id": reg.device_id,
            "device_label": reg.device_label,
            "device_type": reg.device_type,
            "public_key_pem": reg.public_key_pem,
            "key_algorithm": reg.key_algorithm,
            "key_fingerprint": reg.key_fingerprint,
            "hardware_fingerprint": reg.hardware_fingerprint,
            "mac_address": reg.mac_address,
            "ip_address": reg.ip_address,
            "status": reg.status,
            "requested_by_username": reg.requested_by_username,
            "approved_by_username": reg.approved_by_username,
            "approved_at": reg.approved_at.isoformat() if reg.approved_at else None,
            "rejected_by_username": reg.rejected_by_username,
            "rejected_at": reg.rejected_at.isoformat() if reg.rejected_at else None,
            "rejection_reason": reg.rejection_reason,
            "revoked_by_username": reg.revoked_by_username,
            "revoked_at": reg.revoked_at.isoformat() if reg.revoked_at else None,
            "revocation_reason": reg.revocation_reason,
            "approval_notes": reg.approval_notes,
            "last_seen_at": reg.last_seen_at.isoformat() if reg.last_seen_at else None,
            "created_at": reg.created_at.isoformat() if reg.created_at else None,
            "updated_at": reg.updated_at.isoformat() if reg.updated_at else None,
        }

    def list(self, request) -> Response:
        """Lists device registrations for tenant, optionally filtered by branch and status."""
        try:
            tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

        branch_id = request.query_params.get("branch_id")
        status_filter = request.query_params.get("status")

        # If Branch Manager and no branch specified, scope to their branch
        if role == "BRANCH_MANAGER" and user_branch_id and not branch_id:
            branch_id = user_branch_id

        qs = LoginDeviceRegistration.objects.filter(tenant_id=tenant_id).select_related("branch", "tenant")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if status_filter:
            qs = qs.filter(status=status_filter.upper())

        qs = qs.order_by("-created_at")
        results = [self._serialize_registration(reg) for reg in qs]
        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        """Retrieves details of a single device registration by registration_id or device_id."""
        try:
            tenant_id, _, _ = self._resolve_tenant_and_role(request)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

        reg = (
            LoginDeviceRegistration.objects.filter(tenant_id=tenant_id)
            .filter(registration_id=pk)
            .select_related("branch", "tenant")
            .first()
        )
        if not reg:
            reg = (
                LoginDeviceRegistration.objects.filter(tenant_id=tenant_id)
                .filter(device_id=pk)
                .select_related("branch", "tenant")
                .first()
            )
        if not reg:
            return Response(
                {"error_code": "REGISTRATION_NOT_FOUND", "message": f"Device registration '{pk}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(self._serialize_registration(reg), status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        """Submits initial device registration request."""
        try:
            tenant_id, _, user_branch_id = self._resolve_tenant_and_role(request)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

        data = request.data
        ip_addr = request.META.get("REMOTE_ADDR", "") or data.get("ip_address", "")
        branch_id = data.get("branch_id") or user_branch_id

        if not branch_id:
            return Response(
                {"error_code": "BRANCH_REQUIRED", "message": "branch_id is required for device registration."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        actor_name = getattr(request.user, "email", None) or getattr(request.user, "username", "terminal_agent")

        try:
            reg = DeviceRegistrationService.register_device(
                tenant_id=tenant_id,
                branch_id=branch_id,
                device_id=data.get("device_id", ""),
                device_label=data.get("device_label", ""),
                public_key_pem=data.get("public_key_pem", ""),
                key_algorithm=data.get("key_algorithm", "ED25519"),
                device_type=data.get("device_type", "DESKTOP_POS"),
                hardware_fingerprint=data.get("hardware_fingerprint", ""),
                mac_address=data.get("mac_address", ""),
                ip_address=ip_addr,
                requested_by=actor_name,
            )
            return Response(self._serialize_registration(reg), status=status.HTTP_201_CREATED)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except CrossTenantOperationError as exc:
            return Response(
                {"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )

    @action(detail=False, methods=["post"], url_path="register")
    def register_endpoint(self, request) -> Response:
        """Explicit POST /register endpoint for device self-enrollment."""
        return self.create(request)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None) -> Response:
        """Approves pending device registration (Manager / Owner)."""
        try:
            self._resolve_tenant_and_role(request)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

        notes = request.data.get("approval_notes", "")
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            reg = DeviceRegistrationService.approve_device(
                registration_id=pk,
                actor_user=request.user,
                approval_notes=notes,
                ip_address=ip_addr,
            )
            return Response(self._serialize_registration(reg), status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None) -> Response:
        """Rejects pending device registration with mandatory reason."""
        try:
            self._resolve_tenant_and_role(request)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

        reason = request.data.get("rejection_reason", "")
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            reg = DeviceRegistrationService.reject_device(
                registration_id=pk,
                actor_user=request.user,
                rejection_reason=reason,
                ip_address=ip_addr,
            )
            return Response(self._serialize_registration(reg), status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request, pk=None) -> Response:
        """Revokes trusted device cryptographic credential with mandatory reason."""
        try:
            self._resolve_tenant_and_role(request)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

        reason = request.data.get("revocation_reason", "")
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            reg = DeviceRegistrationService.revoke_device(
                registration_id=pk,
                actor_user=request.user,
                revocation_reason=reason,
                ip_address=ip_addr,
            )
            return Response(self._serialize_registration(reg), status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

    @action(detail=False, methods=["post"], url_path="verify")
    def verify(self, request) -> Response:
        """
        Cryptographic verification gate for workstation logins.
        Returns whether device is trusted and authorized for terminal operations.
        """
        try:
            tenant_id, _, _ = self._resolve_tenant_and_role(request)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

        data = request.data
        device_id = data.get("device_id", "")
        pub_key = data.get("public_key_pem")
        ip_addr = request.META.get("REMOTE_ADDR", "") or data.get("ip_address", "")

        if not device_id:
            return Response(
                {"error_code": "MISSING_DEVICE_ID", "message": "device_id is required for verification."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = DeviceRegistrationService.verify_device(
            tenant_id=tenant_id,
            device_id=device_id,
            public_key_pem=pub_key,
            ip_address=ip_addr,
        )

        resp_status = status.HTTP_200_OK if result.get("is_trusted") else status.HTTP_403_FORBIDDEN
        return Response(result, status=resp_status)

    def partial_update(self, request, pk=None) -> Response:
        """Updates human-readable label of device registration."""
        try:
            self._resolve_tenant_and_role(request)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

        new_label = request.data.get("device_label", "")
        try:
            reg = DeviceRegistrationService.update_device_label(
                registration_id=pk,
                actor_user=request.user,
                new_label=new_label,
            )
            return Response(self._serialize_registration(reg), status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

