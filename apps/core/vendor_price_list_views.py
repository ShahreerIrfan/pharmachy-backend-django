"""
backend/apps/core/vendor_price_list_views.py
Architecture Decision: ADR-0183 (P183).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

REST API ViewSet governing supplier product price lists, SKU quotations,
trade discounts, MOQ validations, and automated best-price resolution.
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.vendor_price_list_service import VendorPriceListService

logger = logging.getLogger(__name__)


def _get_request_tenant_id(request) -> str:
    """Extracts tenant ID from authenticated user, request tenant, or header."""
    user = getattr(request, "user", None)
    if user and getattr(user, "tenant_id", None):
        return str(user.tenant_id)
    tenant = getattr(request, "tenant", None)
    if tenant and getattr(tenant, "tenant_id", None):
        return str(tenant.tenant_id)
    header = request.META.get("HTTP_X_TENANT_ID")
    if header:
        return str(header)
    return ""


def _check_tenant_suspended(request):
    """Blocks suspended tenants from accessing or mutating price list resources."""
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Price list operations blocked.",
                "message_en": "Tenant account subscription is suspended. Price list operations blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। মূল্য তালিকা মডিউল বন্ধ।",
            }
        )


class VendorProductPriceListViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for supplier product quotations and trade discounts.
    Enforces regulatory price-ceiling guard (cost <= MRP), minimum order quantities,
    tenant scoping, role authorization, and best-price PO allocation.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/procurement/price-lists/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        supplier_id = request.query_params.get("supplier_id")
        product_id = request.query_params.get("product_id")
        is_active_param = request.query_params.get("is_active")
        is_active: Optional[bool] = None
        if is_active_param is not None:
            is_active = is_active_param.lower() in ("true", "1")

        active_on_date = None
        date_param = request.query_params.get("active_on_date")
        if date_param:
            try:
                active_on_date = datetime.date.fromisoformat(date_param)
            except ValueError:
                pass

        try:
            results = VendorPriceListService.list_price_entries(
                tenant_id=tenant_id,
                supplier_id=supplier_id,
                product_id=product_id,
                active_on_date=active_on_date,
                is_active=is_active,
            )
            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/procurement/price-lists/{pk}/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            entry = VendorPriceListService.get_price_entry(tenant_id=tenant_id, price_entry_id=pk)
            return Response(entry, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        """POST /api/v2/procurement/price-lists/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        target_tenant = request.data.get("tenant_id") or tenant_id
        if target_tenant != tenant_id and not getattr(request.user, "is_superuser", False):
            return Response(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "detail": "Cross-tenant price list creation forbidden.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            created = VendorPriceListService.create_price_entry(
                tenant_id=target_tenant,
                actor_user=request.user,
                data=request.data,
            )
            return Response(created, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def partial_update(self, request, pk=None) -> Response:
        """PATCH /api/v2/procurement/price-lists/{pk}/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            updated = VendorPriceListService.update_price_entry(
                tenant_id=tenant_id,
                actor_user=request.user,
                price_entry_id=pk,
                data=request.data,
            )
            return Response(updated, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def destroy(self, request, pk=None) -> Response:
        """DELETE /api/v2/procurement/price-lists/{pk}/ (Soft-delete / archive)"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            archived = VendorPriceListService.archive_price_entry(
                tenant_id=tenant_id,
                actor_user=request.user,
                price_entry_id=pk,
            )
            return Response(archived, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="resolve-best")
    def resolve_best(self, request) -> Response:
        """
        POST /api/v2/procurement/price-lists/resolve-best/
        Payload:
        {
            "product_id": "...",
            "quantity": 100,
            "target_date": "2026-11-05" (optional)
        }
        """
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        product_id = request.data.get("product_id")
        if not product_id:
            return Response(
                {"error_code": "ERR_MISSING_PRODUCT_ID", "detail": "product_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            quantity = int(request.data.get("quantity", 1))
            if quantity <= 0:
                return Response(
                    {"error_code": "ERR_INVALID_QUANTITY", "detail": "Quantity must be greater than zero."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except (ValueError, TypeError):
            return Response(
                {"error_code": "ERR_INVALID_QUANTITY", "detail": "Invalid quantity format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_date = None
        target_date_raw = request.data.get("target_date")
        if target_date_raw:
            try:
                target_date = datetime.date.fromisoformat(str(target_date_raw))
            except ValueError:
                return Response(
                    {"error_code": "ERR_INVALID_DATE_FORMAT", "detail": "target_date must be YYYY-MM-DD."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            best_offer = VendorPriceListService.resolve_best_price(
                tenant_id=tenant_id,
                product_id=product_id,
                quantity=quantity,
                target_date=target_date,
            )
            if not best_offer:
                return Response(
                    {
                        "error_code": "ERR_NO_QUALIFYING_QUOTATION",
                        "detail": f"No active quotation meeting minimum order quantity found for product {product_id}.",
                        "message_en": "No active supplier quotation meeting the requested order quantity was found.",
                        "message_bn": "অনুরোধকৃত পরিমাণের জন্য সক্রিয় কোনো সরবরাহকারী কোটেশন পাওয়া যায়নি।",
                        "product_id": product_id,
                        "demanded_quantity": quantity,
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(best_offer, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

