"""
backend/apps/core/packaging_hierarchy_service.py
Architecture Decision: ADR-0162 (P162).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Service layer for N-tier packaging hierarchies, effective-dated factors,
cycle detection, tree resolution, and multi-tier conversions.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    MedicineProduct,
    PackagingHierarchyNode,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.packaging_hierarchy_service")


class PackagingHierarchyService:
    """Coordinates N-tier packaging hierarchy nodes and effective-dated factor calculations."""

    @staticmethod
    def _parse_date(date_val: Any) -> date:
        if isinstance(date_val, date):
            return date_val
        if isinstance(date_val, datetime):
            return date_val.date()
        if isinstance(date_val, str) and date_val.strip():
            return datetime.strptime(date_val.strip(), "%Y-%m-%d").date()
        raise ValidationError({
            "error_code": "ERR_INVALID_DATE_FORMAT",
            "detail": f"Invalid date value '{date_val}', expected YYYY-MM-DD.",
        })

    @classmethod
    def _check_cycle(cls, tenant: Tenant, product: MedicineProduct, parent: str, child: str, exclude_node_id: Optional[str] = None):
        """Detects if adding parent -> child would create a circular dependency in the packaging tree."""
        if parent == child:
            raise ValidationError({
                "error_code": "ERR_PACKAGING_CYCLE_SELF",
                "detail": f"Packaging unit cannot contain itself: '{parent}' == '{child}'.",
            })

        # Fetch existing active edges
        qs = PackagingHierarchyNode.objects.filter(tenant=tenant, product=product, is_active=True)
        if exclude_node_id:
            qs = qs.exclude(node_id=exclude_node_id)

        # Build adjacency graph: u -> list of children
        graph: Dict[str, Set[str]] = {}
        for node in qs:
            p = node.parent_unit.strip().upper()
            c = node.child_unit.strip().upper()
            graph.setdefault(p, set()).add(c)

        # Check if there is already a path from child to parent
        visited = set()
        queue = [child]
        while queue:
            curr = queue.pop(0)
            if curr == parent:
                raise ValidationError({
                    "error_code": "ERR_PACKAGING_CYCLE_DETECTED",
                    "detail": f"Packaging cycle detected: adding edge '{parent}' -> '{child}' creates a circular hierarchy loop.",
                })
            if curr not in visited:
                visited.add(curr)
                queue.extend(graph.get(curr, set()) - visited)

    @classmethod
    def create_or_update_node(
        cls,
        user: PharmacyUser,
        data: Dict[str, Any],
        node_id: Optional[str] = None,
    ) -> PackagingHierarchyNode:
        """Creates or updates a packaging hierarchy node with strict cycle and overlap prevention."""
        # 1. RBAC check
        if getattr(user, "role", None) in [UserRole.CASHIER]:
            raise PermissionDenied({
                "error_code": "ERR_ROLE_UNAUTHORIZED",
                "detail": "Cashier role is not authorized to configure packaging hierarchies.",
            })

        # 2. Tenant check
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({
                "error_code": "ERR_TENANT_REQUIRED",
                "detail": "Active tenant context is required.",
            })

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant or not tenant.is_active or tenant.status in [
            TenantLifecycleStatus.SUSPENDED,
            TenantLifecycleStatus.ARCHIVED,
        ]:
            raise PermissionDenied({
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Suspended or inactive tenant cannot configure packaging hierarchies.",
            })

        # 3. Product check
        product_id = data.get("product_id")
        if not product_id:
            raise ValidationError({
                "error_code": "ERR_PRODUCT_REQUIRED",
                "detail": "Field 'product_id' is required.",
            })
        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise NotFound({
                "error_code": "ERR_PRODUCT_NOT_FOUND",
                "detail": f"Medicine product '{product_id}' was not found.",
            })

        branch_id = data.get("branch_id")
        branch = None
        if branch_id:
            branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()

        parent_unit = str(data.get("parent_unit", "")).strip().upper()
        child_unit = str(data.get("child_unit", "")).strip().upper()
        if not parent_unit or not child_unit:
            raise ValidationError({
                "error_code": "ERR_UNITS_REQUIRED",
                "detail": "Both 'parent_unit' and 'child_unit' are required.",
            })

        try:
            factor = int(data.get("conversion_factor", 1))
            if factor < 1:
                raise ValueError()
        except Exception:
            raise ValidationError({
                "error_code": "ERR_INVALID_UNIT_MULTIPLIER",
                "detail": "Conversion factor must be an integer >= 1.",
            })

        # Dates
        raw_eff_from = data.get("effective_from")
        if not raw_eff_from:
            raise ValidationError({
                "error_code": "ERR_EFFECTIVE_FROM_REQUIRED",
                "detail": "Field 'effective_from' is required.",
            })
        eff_from = cls._parse_date(raw_eff_from)

        raw_eff_to = data.get("effective_to")
        eff_to = cls._parse_date(raw_eff_to) if raw_eff_to else None

        if eff_to and eff_to < eff_from:
            raise ValidationError({
                "error_code": "ERR_INVALID_DATE_RANGE",
                "detail": f"effective_to ({eff_to}) cannot be earlier than effective_from ({eff_from}).",
            })

        # 4. Cycle check
        cls._check_cycle(tenant, product, parent_unit, child_unit, exclude_node_id=node_id)

        # 5. Overlapping effective date check for same parent & child
        existing_nodes = PackagingHierarchyNode.objects.filter(
            tenant=tenant,
            product=product,
            parent_unit=parent_unit,
            child_unit=child_unit,
            is_active=True,
        )
        if node_id:
            existing_nodes = existing_nodes.exclude(node_id=node_id)

        for ex in existing_nodes:
            ex_from = ex.effective_from
            ex_to = ex.effective_to
            # Overlap condition: max(eff_from, ex_from) <= min(eff_to, ex_to)
            latest_start = max(eff_from, ex_from)
            if eff_to and ex_to:
                earliest_end = min(eff_to, ex_to)
                if latest_start <= earliest_end:
                    raise ValidationError({
                        "error_code": "ERR_OVERLAPPING_PACKAGING_FACTOR",
                        "detail": (
                            f"Packaging factor for {parent_unit} -> {child_unit} overlaps with existing node '{ex.node_id}' "
                            f"({ex_from} to {ex_to or 'Open'})."
                        ),
                    })
            elif eff_to and not ex_to:
                if latest_start <= eff_to:
                    raise ValidationError({
                        "error_code": "ERR_OVERLAPPING_PACKAGING_FACTOR",
                        "detail": f"Packaging factor overlaps with existing open-ended node '{ex.node_id}'.",
                    })
            elif not eff_to and ex_to:
                if latest_start <= ex_to:
                    raise ValidationError({
                        "error_code": "ERR_OVERLAPPING_PACKAGING_FACTOR",
                        "detail": f"Packaging factor overlaps with existing node '{ex.node_id}'.",
                    })
            else:  # both open-ended
                raise ValidationError({
                    "error_code": "ERR_OVERLAPPING_PACKAGING_FACTOR",
                    "detail": f"Packaging factor overlaps with existing open-ended node '{ex.node_id}'.",
                })

        with transaction.atomic():
            if node_id:
                node = PackagingHierarchyNode.objects.filter(node_id=node_id, tenant=tenant).first()
                if not node:
                    raise NotFound({
                        "error_code": "ERR_NODE_NOT_FOUND",
                        "detail": f"Packaging hierarchy node '{node_id}' was not found.",
                    })
            else:
                node = PackagingHierarchyNode(
                    node_id=f"pkn-{uuid.uuid4().hex[:12]}",
                    tenant=tenant,
                    branch=branch,
                    product=product,
                )

            node.parent_unit = parent_unit
            node.child_unit = child_unit
            node.conversion_factor = factor
            node.tier_level = int(data.get("tier_level", node.tier_level or 1))
            node.is_base_level = bool(data.get("is_base_level", node.is_base_level))
            node.tier_gtin_barcode = str(data.get("tier_gtin_barcode", node.tier_gtin_barcode)).strip()
            node.effective_from = eff_from
            node.effective_to = eff_to
            node.is_active = bool(data.get("is_active", node.is_active))
            node.save()

            AuditLogEntry.objects.create(
                entry_id=uuid.uuid4(),
                tenant_id=tenant.tenant_id,
                user_id=str(user.pk),
                actor_role=getattr(user, "role", "UNKNOWN"),
                action="CONFIGURE_PACKAGING_HIERARCHY_NODE",
                resource_type="PackagingHierarchyNode",
                resource_id=node.node_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "product_id": product.product_id,
                    "parent_unit": node.parent_unit,
                    "child_unit": node.child_unit,
                    "conversion_factor": node.conversion_factor,
                    "effective_from": node.effective_from.isoformat(),
                    "effective_to": node.effective_to.isoformat() if node.effective_to else None,
                },
            )

        return node

    @classmethod
    def resolve_packaging_tree(
        cls,
        product_id: str,
        tenant_id: str,
        as_of_date: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Resolves the ordered packaging tree for a product active at target date.
        Computes cumulative conversion factors to atomic base level.
        """
        target_date = cls._parse_date(as_of_date) if as_of_date else timezone.now().date()

        nodes = PackagingHierarchyNode.objects.filter(
            tenant_id=tenant_id,
            product_id=product_id,
            is_active=True,
            effective_from__lte=target_date,
        ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=target_date)).order_by("tier_level")

        if not nodes.exists():
            return {
                "product_id": product_id,
                "as_of_date": target_date.isoformat(),
                "node_count": 0,
                "chain": [],
                "cumulative_factors": {},
            }

        # Build mapping: parent -> node
        parent_map: Dict[str, PackagingHierarchyNode] = {}
        children_set = set()
        for n in nodes:
            parent_map[n.parent_unit] = n
            children_set.add(n.child_unit)

        # Roots are units that are parents but not children
        root_units = [p for p in parent_map.keys() if p not in children_set]
        if not root_units:
            root_units = [list(parent_map.keys())[0]]

        # Traverse from root down to base
        chain: List[Dict[str, Any]] = []
        curr = root_units[0]
        visited = set()

        while curr in parent_map and curr not in visited:
            visited.add(curr)
            n = parent_map[curr]
            chain.append({
                "node_id": n.node_id,
                "parent_unit": n.parent_unit,
                "child_unit": n.child_unit,
                "conversion_factor": n.conversion_factor,
                "tier_level": n.tier_level,
                "is_base_level": n.is_base_level,
                "tier_gtin_barcode": n.tier_gtin_barcode,
                "effective_from": n.effective_from.isoformat(),
                "effective_to": n.effective_to.isoformat() if n.effective_to else None,
            })
            curr = n.child_unit

        # Calculate cumulative multipliers to base (leaf unit)
        base_unit = curr  # final unit in chain
        cumulative_factors: Dict[str, int] = {base_unit: 1}
        acc = 1
        for link in reversed(chain):
            acc *= link["conversion_factor"]
            cumulative_factors[link["parent_unit"]] = acc

        # Enrich chain with cumulative multiplier
        for link in chain:
            link["cumulative_to_base"] = cumulative_factors.get(link["parent_unit"], 1)

        return {
            "product_id": product_id,
            "as_of_date": target_date.isoformat(),
            "node_count": len(chain),
            "base_unit": base_unit,
            "chain": chain,
            "cumulative_factors": cumulative_factors,
        }

    @classmethod
    def convert_packaging_tier(
        cls,
        product_id: str,
        tenant_id: str,
        from_unit: str,
        to_unit: str,
        quantity: Decimal,
        as_of_date: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Converts quantity between any two packaging tiers in the resolved tree."""
        from_u = from_unit.strip().upper()
        to_u = to_unit.strip().upper()

        if quantity <= Decimal("0"):
            raise ValidationError({
                "error_code": "ERR_INVALID_QUANTITY",
                "detail": "Quantity must be strictly positive.",
            })

        tree = cls.resolve_packaging_tree(product_id=product_id, tenant_id=tenant_id, as_of_date=as_of_date)
        cum_map = tree.get("cumulative_factors", {})

        if from_u not in cum_map or to_u not in cum_map:
            raise ValidationError({
                "error_code": "ERR_UNLINKED_PACKAGING_TIER",
                "detail": f"Units '{from_u}' and/or '{to_u}' are not part of the active packaging tree for this product.",
            })

        from_mult = Decimal(str(cum_map[from_u]))
        to_mult = Decimal(str(cum_map[to_u]))

        base_qty = quantity * from_mult
        converted_qty = base_qty / to_mult

        ratio = from_mult / to_mult

        return {
            "product_id": product_id,
            "as_of_date": tree["as_of_date"],
            "from_unit": from_u,
            "to_unit": to_u,
            "input_quantity": str(quantity),
            "converted_quantity": str(converted_qty),
            "conversion_ratio": str(ratio),
            "base_unit": tree["base_unit"],
            "base_unit_equivalent": str(base_qty),
        }

