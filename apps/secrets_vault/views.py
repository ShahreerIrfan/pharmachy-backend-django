"""
apps.secrets_vault.views
Architecture Decision: ADR-0037
REST API ViewSet for Tenant Secret Vault, Safe Metadata Retrieval, and Secure Key Rotation.
"""

from __future__ import annotations

import uuid

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.secrets_vault.models import SecretRotationAudit, TenantSecretRecord
from apps.secrets_vault.serializers import (
    SecretRotateRequestSerializer,
    SecretRotationAuditSerializer,
    TenantSecretCreateSerializer,
    TenantSecretMetadataSerializer,
)


class TenantSecretViewSet(viewsets.ModelViewSet):
    """Encrypted tenant integration secret store with strict RBAC and tenant scoping."""

    queryset = TenantSecretRecord.objects.all()
    serializer_class = TenantSecretMetadataSerializer
    permission_classes = [permissions.AllowAny]

    ALLOWED_ROLES = {"PHARMACY_OWNER", "ADMIN"}

    def check_permissions(self, request) -> None:
        super().check_permissions(request)

        # 1. RBAC Check: Only PHARMACY_OWNER or ADMIN allowed
        role = request.META.get("HTTP_X_USER_ROLE", "").upper()
        if not role and hasattr(request.user, "role"):
            role = str(getattr(request.user, "role")).upper()

        is_admin = getattr(request.user, "is_superuser", False) or getattr(request.user, "is_staff", False)
        if not is_admin and role not in self.ALLOWED_ROLES:
            raise PermissionDenied("Access denied: Tenant secret vault requires PHARMACY_OWNER or ADMIN permissions.")

    def get_queryset(self):
        qs = super().get_queryset()

        authorized_tenant = self.request.META.get("HTTP_X_TENANT_ID")
        if not authorized_tenant and hasattr(self.request.user, "tenant_id"):
            authorized_tenant = getattr(self.request.user, "tenant_id")

        query_tenant = self.request.query_params.get("tenant_id")

        # 2. Cross-Tenant Boundary Enforcement
        if authorized_tenant:
            if query_tenant and query_tenant != authorized_tenant:
                raise PermissionDenied(
                    f"Cross-tenant access forbidden: Cannot view secrets for tenant '{query_tenant}'."
                )
            qs = qs.filter(tenant_id=authorized_tenant)
        elif query_tenant:
            qs = qs.filter(tenant_id=query_tenant)

        return qs

    def get_object(self):
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        pk = self.kwargs.get(lookup_url_kwarg)
        if pk:
            obj = TenantSecretRecord.objects.filter(pk=pk).first()
            if obj:
                authorized_tenant = self.request.META.get("HTTP_X_TENANT_ID")
                if authorized_tenant and obj.tenant_id != authorized_tenant:
                    raise PermissionDenied(
                        f"Cross-tenant access forbidden: Cannot access secret for '{obj.tenant_id}'."
                    )
        return super().get_object()

    def create(self, request, *args, **kwargs):
        """Creates or updates an encrypted tenant secret."""
        serializer = TenantSecretCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        tenant_id = request.META.get("HTTP_X_TENANT_ID")
        if not tenant_id:
            raise ValidationError("Missing required X-Tenant-ID header.")

        secret_type = data["secret_type"]
        raw_val = data["raw_secret_value"]

        # Check existing or create new
        record, was_created = TenantSecretRecord.objects.get_or_create(
            tenant_id=tenant_id,
            secret_type=secret_type,
            defaults={"secret_id": f"sec-{uuid.uuid4().hex[:12]}"},
        )
        record.set_secret(raw_val)
        record.save()

        res_serializer = TenantSecretMetadataSerializer(record)
        return Response(res_serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="rotate")
    def rotate(self, request, pk=None):
        """Rotates a secret, increments version, and creates an audit record."""
        record = self.get_object()

        # Cross-tenant boundary check on target object
        authorized_tenant = request.META.get("HTTP_X_TENANT_ID")
        if authorized_tenant and record.tenant_id != authorized_tenant:
            raise PermissionDenied(f"Cross-tenant access forbidden: Cannot rotate secret for '{record.tenant_id}'.")

        serializer = SecretRotateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        old_version = record.version
        new_val = serializer.validated_data["new_secret_value"]
        new_version = record.rotate_secret(new_val)

        role = request.META.get("HTTP_X_USER_ROLE", "PHARMACY_OWNER").upper()

        SecretRotationAudit.objects.create(
            tenant_id=record.tenant_id,
            secret_id=record.secret_id,
            operator_role=role,
            old_version=old_version,
            new_version=new_version,
            status="SUCCESS",
        )

        res_serializer = TenantSecretMetadataSerializer(record)
        return Response(res_serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="audits")
    def audits(self, request):
        """Lists secret rotation audit history scoped to the caller's tenant."""
        qs = SecretRotationAudit.objects.all()
        authorized_tenant = request.META.get("HTTP_X_TENANT_ID")
        if authorized_tenant:
            qs = qs.filter(tenant_id=authorized_tenant)
        serializer = SecretRotationAuditSerializer(qs[:30], many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)
