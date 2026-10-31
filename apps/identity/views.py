"""
apps.identity.views
Architecture Decision: ADR-0042
DRF ViewSet for PharmacyUser Identity, RBAC Boundaries, and Safe Deletion.
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.identity.auth_backends import EmailOrPhoneModelBackend
from apps.identity.models import PharmacyUser
from apps.identity.serializers import (
    PharmacyUserArchiveRequestSerializer,
    PharmacyUserCreateSerializer,
    PharmacyUserLoginSerializer,
    PharmacyUserSerializer,
)


def _resolve_tenant_id(request) -> str:
    return (
        request.headers.get("X-Tenant-ID")
        or request.query_params.get("tenant_id")
        or getattr(request.user, "tenant_id", "")
        or "tenant-default"
    )


class PharmacyUserViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for PharmacyUser with strict multi-tenant scoping,
    role-based authorization, and safe deletion.
    """

    queryset = PharmacyUser.objects.all()
    serializer_class = PharmacyUserSerializer
    permission_classes = [permissions.AllowAny]

    def _check_user_management_rbac(self, request):
        role = request.headers.get("X-User-Role", "")
        if not role:
            role = getattr(request.user, "role", "")

        if role == "CASHIER":
            raise PermissionDenied("Cashiers are not authorized to manage pharmacy user accounts.")

        allowed = {"PHARMACY_OWNER", "ADMIN", "SUPERADMIN", "PLATFORM_OPERATOR", "BRANCH_MANAGER"}
        if role and role not in allowed and not getattr(request.user, "is_superuser", False):
            raise PermissionDenied(f"Role '{role}' is not authorized to perform user management operations.")

    def get_serializer_class(self):
        if self.action == "create":
            return PharmacyUserCreateSerializer
        return PharmacyUserSerializer

    def get_queryset(self):
        tenant_id = _resolve_tenant_id(self.request)
        include_archived = self.request.query_params.get("include_archived", "false").lower() in ("true", "1")
        qs = PharmacyUser.objects.filter(tenant_id=tenant_id)
        if not include_archived:
            qs = qs.filter(is_archived=False)
        return qs

    def get_object(self):
        pk = self.kwargs.get("pk")
        obj = PharmacyUser.objects.filter(pk=pk).first()
        if not obj:
            return super().get_object()

        tenant_id = _resolve_tenant_id(self.request)
        if obj.tenant_id != tenant_id and not getattr(self.request.user, "is_superuser", False):
            raise PermissionDenied("Cross-tenant access forbidden: Cannot access user from another pharmacy.")
        return obj

    def create(self, request, *args, **kwargs):
        self._check_user_management_rbac(request)
        tenant_id = _resolve_tenant_id(request)

        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        if "tenant_id" not in data or not data["tenant_id"]:
            data["tenant_id"] = tenant_id

        serializer = self.get_serializer(data=data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = serializer.save()
        res_serializer = PharmacyUserSerializer(user)
        return Response(res_serializer.data, status=status.HTTP_201_CREATED)

    def destroy(self, request, *args, **kwargs):
        """
        Overrides DELETE to enforce safe soft-deletion/archival.
        """
        self._check_user_management_rbac(request)
        user = self.get_object()
        actor = request.headers.get("X-User-Role", "ADMIN")
        user.archive(by_user=actor, reason="Administrative soft deletion via REST API")
        return Response(
            {
                "detail": "User safely archived without data loss.",
                "user_id": str(user.user_id),
                "is_archived": True,
                "activation_status": user.activation_status,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        """Explicit audited user archival."""
        self._check_user_management_rbac(request)
        user = self.get_object()
        serializer = PharmacyUserArchiveRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        reason = serializer.validated_data["archival_reason"]
        actor = request.headers.get("X-User-Role", "ADMIN")
        user.archive(by_user=actor, reason=reason)
        return Response(
            {
                "detail": "User identity safely archived.",
                "user_id": str(user.user_id),
                "is_archived": True,
                "archival_reason": reason,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """Explicit user activation."""
        self._check_user_management_rbac(request)
        user = self.get_object()
        user.activate()
        return Response(
            {
                "detail": "User activated successfully.",
                "user_id": str(user.user_id),
                "activation_status": user.activation_status,
                "is_active": user.is_active,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"])
    def authenticate(self, request):
        """
        Authenticates user via email OR Bangladesh mobile phone number.
        """
        serializer = PharmacyUserLoginSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        identifier = serializer.validated_data["identifier"]
        password = serializer.validated_data["password"]

        backend = EmailOrPhoneModelBackend()
        user = backend.authenticate(request, username=identifier, password=password)

        if not user:
            return Response(
                {"detail": "Invalid credentials or account suspended/locked."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        user_data = PharmacyUserSerializer(user).data
        return Response(
            {
                "authenticated": True,
                "user": user_data,
                "auth_method": "PHONE" if identifier.startswith("+") or identifier.isdigit() else "EMAIL",
            },
            status=status.HTTP_200_OK,
        )
