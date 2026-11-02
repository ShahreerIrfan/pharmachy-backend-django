"""
backend/apps/core/classification_snapshot_views.py
Architecture Decision: ADR-0130 (P130).
API ViewSet for Versioned Controlled Classification Snapshots, Recalls,
Point-in-Time Regulatory Audits, and Master Medicine Synchronization.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import dateparse
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.classification_snapshot_service import MedicineClassificationSnapshotService
from apps.core.models import MedicineClassificationSnapshot


class MedicineClassificationSnapshotViewSet(viewsets.ViewSet):
    """
    Controlled Classification Snapshots ViewSet.
    Enforces strict platform authority for creating classification snapshots
    and provides point-in-time audit resolution and recall status checks for POS/Mobile.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _is_platform_operator(self, user) -> bool:
        if not user or not user.is_authenticated:
            return False
        return bool(
            getattr(user, "is_superuser", False)
            or str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]
        )

    def _require_platform_operator(self, request):
        user = getattr(request, "user", None)
        if not self._is_platform_operator(user):
            raise PermissionDenied(
                detail={
                    "error_code": "PLATFORM_CATALOG_OWNERSHIP_PROTECTED",
                    "message_en": "Tenant users cannot mutate global regulatory medicine classification snapshots.",
                    "message_bn": "টেন্যান্ট ব্যবহারকারীরা গ্লোবাল ওষুধ ক্লাসিফিকেশন স্ন্যাপশট পরিবর্তন করতে পারবেন না।",
                }
            )

    def create(self, request):
        """
        POST /api/v2/catalog/classifications/
        Creates a new immutable regulatory classification snapshot.
        Restricted exclusively to Platform Superadmins / Operators.
        """
        self._require_platform_operator(request)

        data = request.data or {}
        product_id = data.get("product_id")
        if not product_id:
            raise ValidationError({"product_id": "product_id is a required field."})

        try:
            snapshot_data = MedicineClassificationSnapshotService.create_snapshot(
                product_id=str(product_id).strip(),
                data=data,
                actor=request.user,
            )
            return Response(snapshot_data, status=status.HTTP_201_CREATED)
        except DjangoValidationError as exc:
            if hasattr(exc, "message_dict"):
                raise ValidationError(exc.message_dict)
            raise ValidationError(str(exc))
        except Exception as exc:
            raise ValidationError(str(exc))

    def list(self, request):
        """
        GET /api/v2/catalog/classifications/?product_id=<id>
        Returns classification snapshot history for a medicine.
        """
        product_id = request.query_params.get("product_id")
        if not product_id:
            raise ValidationError({"product_id": "product_id query parameter is required."})

        try:
            history = MedicineClassificationSnapshotService.list_snapshots(product_id)
            return Response(history, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            if hasattr(exc, "message_dict"):
                raise ValidationError(exc.message_dict)
            raise ValidationError(str(exc))

    def retrieve(self, request, pk=None):
        """
        GET /api/v2/catalog/classifications/<snapshot_id>/
        Retrieves a single classification snapshot by snapshot_id.
        """
        snapshot = MedicineClassificationSnapshot.objects.filter(snapshot_id=str(pk).strip()).first()
        if not snapshot:
            return Response(
                {"error_code": "ERR_SNAPSHOT_NOT_FOUND", "message": f"Classification snapshot '{pk}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(MedicineClassificationSnapshotService.serialize_snapshot(snapshot), status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="current")
    def current(self, request):
        """
        GET /api/v2/catalog/classifications/current/?product_id=<id>
        Returns the currently active classification snapshot for a medicine.
        """
        product_id = request.query_params.get("product_id")
        if not product_id:
            raise ValidationError({"product_id": "product_id query parameter is required."})

        snapshot = MedicineClassificationSnapshotService.get_current_snapshot(product_id)
        return Response(
            {
                "product_id": str(product_id).strip(),
                "current_snapshot": snapshot,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="point-in-time")
    def point_in_time(self, request):
        """
        GET /api/v2/catalog/classifications/point-in-time/?product_id=<id>&target_date=<iso>
        Point-in-time temporal resolution: answers what the regulatory classification
        was on a specific historical date.
        """
        product_id = request.query_params.get("product_id")
        target_date_raw = request.query_params.get("target_date")

        if not product_id:
            raise ValidationError({"product_id": "product_id query parameter is required."})
        if not target_date_raw:
            raise ValidationError({"target_date": "target_date query parameter is required in ISO format."})

        clean_raw = str(target_date_raw).strip().replace(" ", "+")
        target_date = dateparse.parse_datetime(clean_raw)
        if not target_date:
            raise ValidationError({"target_date": "Invalid ISO format for target_date."})

        snapshot = MedicineClassificationSnapshotService.get_point_in_time_snapshot(product_id, target_date)
        return Response(
            {
                "product_id": str(product_id).strip(),
                "target_date": target_date.isoformat(),
                "snapshot": snapshot,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="history")
    def history(self, request):
        """
        GET /api/v2/catalog/classifications/history/?product_id=<id>
        Returns complete audit trail of classification versions.
        """
        product_id = request.query_params.get("product_id")
        if not product_id:
            raise ValidationError({"product_id": "product_id query parameter is required."})

        try:
            history = MedicineClassificationSnapshotService.list_snapshots(product_id)
            return Response(history, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            if hasattr(exc, "message_dict"):
                raise ValidationError(exc.message_dict)
            raise ValidationError(str(exc))

    @action(detail=False, methods=["get"], url_path="check-recall")
    def check_recall(self, request):
        """
        GET /api/v2/catalog/classifications/check-recall/?product_id=<id>
        Fast check to verify if a medicine is under statutory recall or banned.
        """
        product_id = request.query_params.get("product_id")
        if not product_id:
            raise ValidationError({"product_id": "product_id query parameter is required."})

        is_blocked, block_reason = MedicineClassificationSnapshotService.is_dispensing_blocked(product_id)
        return Response(
            {
                "product_id": str(product_id).strip(),
                "is_dispensing_blocked": is_blocked,
                "block_reason": block_reason,
            },
            status=status.HTTP_200_OK,
        )
