"""
backend/apps/core/dosage_form_qa_views.py
Architecture Decision: ADR-0160 (P160).
REST API ViewSet for Dosage Form QA Examples & Automated Validation Harness.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response

from apps.core.dosage_form_qa_service import DosageFormQaService

logger = logging.getLogger("pharmacy.core.dosage_form_qa.views")


class DosageFormQaViewSet(viewsets.ViewSet):
    """
    ViewSet for Dosage Form QA Fixtures and Cross-Platform Verification.
    Exposes positive fixtures for all 11 dosage forms, negative failure test cases,
    fixture execution, and catalog seeding.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request):
        """Returns catalog of positive and negative dosage form QA fixtures."""
        dosage_form = request.query_params.get("dosage_form_family")
        error_code = request.query_params.get("target_error_code")

        positives = DosageFormQaService.get_positive_fixtures(dosage_form_family=dosage_form)
        negatives = DosageFormQaService.get_negative_fixtures(target_error_code=error_code)

        return Response({
            "positive_count": len(positives),
            "negative_count": len(negatives),
            "positive_fixtures": positives,
            "negative_fixtures": negatives,
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="validate-fixture")
    def validate_fixture(self, request):
        """Executes live validation on a specified QA fixture ID."""
        fixture_id = request.data.get("fixture_id")
        if not fixture_id:
            return Response(
                {"error_code": "ERR_MISSING_FIXTURE_ID", "detail": "Field 'fixture_id' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = DosageFormQaService.validate_fixture(fixture_id)
            return Response(result, status=status.HTTP_200_OK)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="seed")
    def seed(self, request):
        """Seeds canonical positive fixtures into the master medicine catalog."""
        try:
            result = DosageFormQaService.seed_qa_fixtures(user=request.user)
            return Response(result, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

