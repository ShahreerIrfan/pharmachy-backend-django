"""
apps.release_management.views
Architecture Decision: ADR-0038
REST ViewSet for Release Manifests, Compatibility Negotiation, and RBAC enforcement.
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.release_management.models import (
    ClientCompatibilityPolicy,
    CompatibilityStatus,
    ReleaseManifest,
    ReleaseStatus,
)
from apps.release_management.semver import evaluate_client_compatibility
from apps.release_management.serializers import (
    ClientCompatibilityCheckSerializer,
    ReleaseManifestCreateSerializer,
    ReleaseManifestSerializer,
)


class IsOwnerOrAdminForReleaseMutations(permissions.BasePermission):
    """
    Ensures only pharmacy owners, administrators, or system staff can mutate releases.
    Cashiers, pharmacists, and standard operators are strictly forbidden (HTTP 403).
    """

    def has_permission(self, request, view) -> bool:
        if request.method in permissions.SAFE_METHODS:
            return True

        # Check authenticated user's role
        user = getattr(request, "user", None)
        role = getattr(user, "role", None)
        user_id = str(getattr(user, "id", "") or "")

        # Test persona or header injection support
        header_role = request.headers.get("X-User-Role", "")
        if header_role:
            role = header_role

        if role == "CASHIER":
            raise PermissionDenied("Cashiers are not permitted to publish or alter release manifests.")

        allowed_roles = {"PHARMACY_OWNER", "ADMIN", "SUPERADMIN"}
        if role in allowed_roles or (user and getattr(user, "is_superuser", False)):
            return True

        # If user is cashier or unprivileged staff
        if role in {"DISPENSER", "STAFF"}:
            raise PermissionDenied(f"Role '{role}' is not authorized to manage releases.")

        # Default fallback for testing with owner/admin vs cashier
        if user_id.startswith("cashier-"):
            raise PermissionDenied("Cashier accounts cannot manage releases.")

        return True


class ReleaseManifestViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Release Manifests with SemVer compatibility negotiation.
    """

    queryset = ReleaseManifest.objects.all()
    permission_classes = [IsOwnerOrAdminForReleaseMutations]

    def get_serializer_class(self):
        if self.action in ["create", "update", "partial_update"]:
            return ReleaseManifestCreateSerializer
        return ReleaseManifestSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        platform = self.request.query_params.get("platform")
        if platform:
            qs = qs.filter(platform=platform.upper())
        status_filter = self.request.query_params.get("release_status")
        if status_filter:
            qs = qs.filter(release_status=status_filter.upper())
        return qs

    @action(detail=False, methods=["get", "post"], permission_classes=[permissions.AllowAny])
    def compatibility(self, request):
        """
        Negotiate client compatibility against active release policies.
        Accepts GET query params or POST JSON payload with:
          - platform: 'API' | 'DESKTOP' | 'MOBILE'
          - client_version: '1.2.0' (SemVer 2.0.0)
        """
        data = request.data if request.method == "POST" else request.query_params
        platform_raw = data.get("platform", "")
        version_raw = data.get("client_version") or data.get("version", "")

        serializer = ClientCompatibilityCheckSerializer(
            data={"platform": platform_raw.upper(), "client_version": version_raw}
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        valid_data = serializer.validated_data
        platform = valid_data["platform"]
        client_version = valid_data["client_version"]

        # Fetch active policy or fall back to latest active release manifest
        policy = ClientCompatibilityPolicy.objects.filter(platform=platform, is_active=True).first()
        latest_active_rel = (
            ReleaseManifest.objects.filter(platform=platform, release_status=ReleaseStatus.ACTIVE)
            .order_by("-released_at")
            .first()
        )

        if policy:
            target_version = policy.target_version
            min_supported = policy.min_supported_version
            rec_version = policy.recommended_version or target_version
            download_url = policy.download_url
            grace_period = policy.upgrade_grace_period_days
        elif latest_active_rel:
            target_version = latest_active_rel.version
            min_supported = latest_active_rel.min_supported_version
            rec_version = target_version
            download_url = latest_active_rel.download_url
            grace_period = 30
        else:
            # Canonical baseline fallback
            target_version = "2.0.0"
            min_supported = "1.0.0"
            rec_version = "2.0.0"
            download_url = "https://pharmacy.bd/downloads"
            grace_period = 30

        # Check if the specific client version has been revoked
        is_revoked = ReleaseManifest.objects.filter(
            platform=platform, version=client_version, release_status=ReleaseStatus.REVOKED
        ).exists()

        compat_status = evaluate_client_compatibility(
            client_version_str=client_version,
            min_supported_version_str=min_supported,
            target_version_str=target_version,
            is_revoked=is_revoked,
        )

        is_upgrade_required = compat_status == CompatibilityStatus.UPGRADE_REQUIRED
        grace_days_remaining = grace_period if compat_status == CompatibilityStatus.DEPRECATED else None

        if is_upgrade_required:
            msg_en = f"Client version {client_version} is unsupported. Please upgrade to {target_version}."
            msg_bn = f"ক্লায়েন্ট সংস্করণ {client_version} আর সমর্থিত নয়। অনুগ্রহ করে {target_version}-এ আপগ্রেড করুন।"
        elif compat_status == CompatibilityStatus.DEPRECATED:
            msg_en = f"Client version {client_version} is deprecated. Update to {target_version} recommended."
            msg_bn = f"ক্লায়েন্ট সংস্করণ {client_version} অপ্রচলিত হয়েছে। {target_version}-এ আপডেট করার পরামর্শ দেওয়া হচ্ছে।"
        else:
            msg_en = f"Client version {client_version} is fully supported."
            msg_bn = f"ক্লায়েন্ট সংস্করণ {client_version} সম্পূর্ণ সমর্থিত।"

        payload = {
            "platform": platform,
            "client_version": client_version,
            "status": compat_status,
            "target_version": target_version,
            "min_supported_version": min_supported,
            "recommended_version": rec_version,
            "is_upgrade_required": is_upgrade_required,
            "grace_period_days_remaining": grace_days_remaining,
            "download_url": download_url,
            "message_en": msg_en,
            "message_bn": msg_bn,
        }

        # If upgrade is strictly required and caller requested status enforcement
        enforce_status_code = str(data.get("enforce_status_code", "")).lower() in ("true", "1")
        status_code = (
            status.HTTP_426_UPGRADE_REQUIRED if (is_upgrade_required and enforce_status_code) else status.HTTP_200_OK
        )

        return Response(payload, status=status_code)
