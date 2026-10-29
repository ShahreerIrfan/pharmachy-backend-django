"""
Canonical Status Vocabulary and Finite State Machine Lifecycle Engine
Architecture Decision: ADR-0013
Enforces valid state transitions, immutability of posted records, and role/tenant boundaries.
"""

import datetime
from dataclasses import dataclass
from enum import Enum


class SaleStatus(str, Enum):
    DRAFT = "DRAFT"
    PENDING_SYNC = "PENDING_SYNC"
    SERVER_ACCEPTED = "SERVER_ACCEPTED"
    REJECTED = "REJECTED"
    REVERSED = "REVERSED"


class BatchStatus(str, Enum):
    ACTIVE = "ACTIVE"
    NEAR_EXPIRY = "NEAR_EXPIRY"
    EXPIRED = "EXPIRED"
    QUARANTINED = "QUARANTINED"
    RECALLED = "RECALLED"
    DEPLETED = "DEPLETED"


class PrescriptionStatus(str, Enum):
    NEW = "NEW"
    VERIFIED = "VERIFIED"
    DISPENSED = "DISPENSED"
    CANCELLED = "CANCELLED"


class PurchaseOrderStatus(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


class SyncOpStatus(str, Enum):
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SERVER_ACCEPTED = "SERVER_ACCEPTED"
    REJECTED = "REJECTED"
    DEAD_LETTER = "DEAD_LETTER"


class InvalidStateTransitionError(ValueError):
    """Raised when an illegal state machine transition is attempted."""

    pass


class ImmutableRecordModificationError(RuntimeError):
    """Raised when trying to mutate or delete a finalized/posted record."""

    pass


@dataclass(frozen=True)
class StateTransitionAudit:
    entity_type: str
    entity_id: str
    from_state: str
    to_state: str
    actor_id: str
    actor_role: str
    tenant_id: str
    reason: str
    timestamp: datetime.datetime = datetime.datetime.now(datetime.UTC)


class LifecycleEngine:
    """Manages and enforces deterministic lifecycle transitions across domain entities."""

    # Sale transitions: (Current, Target) -> Permitted Roles
    SALE_TRANSITIONS: dict[tuple[SaleStatus, SaleStatus], set[str]] = {
        (SaleStatus.DRAFT, SaleStatus.PENDING_SYNC): {"CASHIER", "BRANCH_MANAGER", "SIGNED_DEVICE"},
        (SaleStatus.DRAFT, SaleStatus.SERVER_ACCEPTED): {"CASHIER", "BRANCH_MANAGER", "TENANT_OWNER"},
        (SaleStatus.PENDING_SYNC, SaleStatus.SERVER_ACCEPTED): {"SIGNED_DEVICE", "PLATFORM_SUPER_ADMIN"},
        (SaleStatus.PENDING_SYNC, SaleStatus.REJECTED): {"SIGNED_DEVICE", "PLATFORM_SUPER_ADMIN"},
        (SaleStatus.SERVER_ACCEPTED, SaleStatus.REVERSED): {"BRANCH_MANAGER", "TENANT_OWNER"},
    }

    # Batch transitions: (Current, Target) -> Permitted Roles
    BATCH_TRANSITIONS: dict[tuple[BatchStatus, BatchStatus], set[str]] = {
        (BatchStatus.ACTIVE, BatchStatus.NEAR_EXPIRY): {"SYSTEM", "INVENTORY_STAFF", "PHARMACIST"},
        (BatchStatus.ACTIVE, BatchStatus.EXPIRED): {"SYSTEM", "INVENTORY_STAFF", "PHARMACIST"},
        (BatchStatus.ACTIVE, BatchStatus.QUARANTINED): {"PHARMACIST", "BRANCH_MANAGER"},
        (BatchStatus.ACTIVE, BatchStatus.DEPLETED): {"SYSTEM", "INVENTORY_STAFF"},
        (BatchStatus.NEAR_EXPIRY, BatchStatus.EXPIRED): {"SYSTEM", "INVENTORY_STAFF", "PHARMACIST"},
        (BatchStatus.NEAR_EXPIRY, BatchStatus.QUARANTINED): {"PHARMACIST", "BRANCH_MANAGER"},
        (BatchStatus.NEAR_EXPIRY, BatchStatus.DEPLETED): {"SYSTEM", "INVENTORY_STAFF"},
        (BatchStatus.QUARANTINED, BatchStatus.ACTIVE): {"PHARMACIST"},  # Requires clinical sign-off
        (BatchStatus.QUARANTINED, BatchStatus.RECALLED): {"PHARMACIST", "BRANCH_MANAGER"},
        (BatchStatus.EXPIRED, BatchStatus.DEPLETED): {"INVENTORY_STAFF", "PHARMACIST"},
    }

    # Prescription transitions: (Current, Target) -> Permitted Roles
    PRESCRIPTION_TRANSITIONS: dict[tuple[PrescriptionStatus, PrescriptionStatus], set[str]] = {
        (PrescriptionStatus.NEW, PrescriptionStatus.VERIFIED): {"PHARMACIST"},
        (PrescriptionStatus.NEW, PrescriptionStatus.CANCELLED): {"PHARMACIST", "BRANCH_MANAGER"},
        (PrescriptionStatus.VERIFIED, PrescriptionStatus.DISPENSED): {"PHARMACIST"},
        (PrescriptionStatus.VERIFIED, PrescriptionStatus.CANCELLED): {"PHARMACIST", "BRANCH_MANAGER"},
    }

    # Purchase Order transitions: (Current, Target) -> Permitted Roles
    PO_TRANSITIONS: dict[tuple[PurchaseOrderStatus, PurchaseOrderStatus], set[str]] = {
        (PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.SUBMITTED): {"PURCHASER", "BRANCH_MANAGER"},
        (PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.CANCELLED): {"PURCHASER", "BRANCH_MANAGER"},
        (PurchaseOrderStatus.SUBMITTED, PurchaseOrderStatus.APPROVED): {"BRANCH_MANAGER", "TENANT_OWNER"},
        (PurchaseOrderStatus.SUBMITTED, PurchaseOrderStatus.CANCELLED): {"BRANCH_MANAGER", "TENANT_OWNER"},
        (PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.PARTIALLY_RECEIVED): {"INVENTORY_STAFF", "BRANCH_MANAGER"},
        (PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.RECEIVED): {"INVENTORY_STAFF", "BRANCH_MANAGER"},
        (PurchaseOrderStatus.PARTIALLY_RECEIVED, PurchaseOrderStatus.RECEIVED): {"INVENTORY_STAFF", "BRANCH_MANAGER"},
    }

    # Outbox Sync transitions: (Current, Target) -> Permitted Roles
    SYNC_TRANSITIONS: dict[tuple[SyncOpStatus, SyncOpStatus], set[str]] = {
        (SyncOpStatus.QUEUED, SyncOpStatus.SENDING): {"SIGNED_DEVICE"},
        (SyncOpStatus.SENDING, SyncOpStatus.SERVER_ACCEPTED): {"SIGNED_DEVICE", "PLATFORM_SUPER_ADMIN"},
        (SyncOpStatus.SENDING, SyncOpStatus.REJECTED): {"SIGNED_DEVICE", "PLATFORM_SUPER_ADMIN"},
        (SyncOpStatus.SENDING, SyncOpStatus.QUEUED): {"SIGNED_DEVICE"},  # Retry backoff
        (SyncOpStatus.REJECTED, SyncOpStatus.DEAD_LETTER): {"BRANCH_MANAGER", "PLATFORM_SUPER_ADMIN"},
    }

    def __init__(self):
        self.audit_log: list[StateTransitionAudit] = []

    def assert_record_mutable(self, entity_type: str, status: str) -> None:
        """Enforces that posted, confirmed or disbursed records cannot be directly updated or deleted."""
        immutable_statuses = {"SERVER_ACCEPTED", "CONFIRMED", "DISPENSED", "REVERSED", "RECEIVED", "DEAD_LETTER"}
        if status in immutable_statuses:
            raise ImmutableRecordModificationError(
                f"Record of type '{entity_type}' in state '{status}' is immutable and cannot be directly updated or deleted. Reversals must use audited credit notes or return vouchers."
            )

    def transition_sale(
        self,
        entity_id: str,
        current_state: SaleStatus,
        target_state: SaleStatus,
        actor_role: str,
        actor_id: str,
        actor_tenant_id: str,
        entity_tenant_id: str,
        reason: str,
    ) -> StateTransitionAudit:
        """Transitions sale invoice through state machine."""
        self._validate_tenant_boundary(actor_tenant_id, entity_tenant_id, actor_role)

        key = (current_state, target_state)
        if key not in self.SALE_TRANSITIONS:
            raise InvalidStateTransitionError(
                f"Illegal sale transition from '{current_state.value}' to '{target_state.value}'"
            )

        permitted_roles = self.SALE_TRANSITIONS[key]
        if actor_role not in permitted_roles:
            raise PermissionError(
                f"Role '{actor_role}' is not authorized to transition sale from '{current_state.value}' to '{target_state.value}'. Permitted: {sorted(list(permitted_roles))}"
            )

        audit = StateTransitionAudit(
            entity_type="SaleInvoice",
            entity_id=entity_id,
            from_state=current_state.value,
            to_state=target_state.value,
            actor_id=actor_id,
            actor_role=actor_role,
            tenant_id=entity_tenant_id,
            reason=reason,
        )
        self.audit_log.append(audit)
        return audit

    def transition_batch(
        self,
        entity_id: str,
        current_state: BatchStatus,
        target_state: BatchStatus,
        actor_role: str,
        actor_id: str,
        actor_tenant_id: str,
        entity_tenant_id: str,
        reason: str,
    ) -> StateTransitionAudit:
        """Transitions inventory batch state."""
        self._validate_tenant_boundary(actor_tenant_id, entity_tenant_id, actor_role)

        key = (current_state, target_state)
        if key not in self.BATCH_TRANSITIONS:
            raise InvalidStateTransitionError(
                f"Illegal batch transition from '{current_state.value}' to '{target_state.value}'"
            )

        permitted_roles = self.BATCH_TRANSITIONS[key]
        if actor_role not in permitted_roles:
            raise PermissionError(
                f"Role '{actor_role}' is not authorized to transition batch from '{current_state.value}' to '{target_state.value}'. Permitted: {sorted(list(permitted_roles))}"
            )

        audit = StateTransitionAudit(
            entity_type="InventoryBatch",
            entity_id=entity_id,
            from_state=current_state.value,
            to_state=target_state.value,
            actor_id=actor_id,
            actor_role=actor_role,
            tenant_id=entity_tenant_id,
            reason=reason,
        )
        self.audit_log.append(audit)
        return audit

    def transition_prescription(
        self,
        entity_id: str,
        current_state: PrescriptionStatus,
        target_state: PrescriptionStatus,
        actor_role: str,
        actor_id: str,
        actor_tenant_id: str,
        entity_tenant_id: str,
        reason: str,
    ) -> StateTransitionAudit:
        """Transitions clinical prescription state."""
        self._validate_tenant_boundary(actor_tenant_id, entity_tenant_id, actor_role)

        key = (current_state, target_state)
        if key not in self.PRESCRIPTION_TRANSITIONS:
            raise InvalidStateTransitionError(
                f"Illegal prescription transition from '{current_state.value}' to '{target_state.value}'"
            )

        permitted_roles = self.PRESCRIPTION_TRANSITIONS[key]
        if actor_role not in permitted_roles:
            raise PermissionError(
                f"Role '{actor_role}' is not authorized to transition prescription from '{current_state.value}' to '{target_state.value}'. Permitted: {sorted(list(permitted_roles))}"
            )

        audit = StateTransitionAudit(
            entity_type="Prescription",
            entity_id=entity_id,
            from_state=current_state.value,
            to_state=target_state.value,
            actor_id=actor_id,
            actor_role=actor_role,
            tenant_id=entity_tenant_id,
            reason=reason,
        )
        self.audit_log.append(audit)
        return audit

    def transition_purchase_order(
        self,
        entity_id: str,
        current_state: PurchaseOrderStatus,
        target_state: PurchaseOrderStatus,
        actor_role: str,
        actor_id: str,
        actor_tenant_id: str,
        entity_tenant_id: str,
        reason: str,
    ) -> StateTransitionAudit:
        """Transitions purchase order workflow state."""
        self._validate_tenant_boundary(actor_tenant_id, entity_tenant_id, actor_role)

        key = (current_state, target_state)
        if key not in self.PO_TRANSITIONS:
            raise InvalidStateTransitionError(
                f"Illegal purchase order transition from '{current_state.value}' to '{target_state.value}'"
            )

        permitted_roles = self.PO_TRANSITIONS[key]
        if actor_role not in permitted_roles:
            raise PermissionError(
                f"Role '{actor_role}' is not authorized to transition PO from '{current_state.value}' to '{target_state.value}'. Permitted: {sorted(list(permitted_roles))}"
            )

        audit = StateTransitionAudit(
            entity_type="PurchaseOrder",
            entity_id=entity_id,
            from_state=current_state.value,
            to_state=target_state.value,
            actor_id=actor_id,
            actor_role=actor_role,
            tenant_id=entity_tenant_id,
            reason=reason,
        )
        self.audit_log.append(audit)
        return audit

    def transition_sync_op(
        self,
        entity_id: str,
        current_state: SyncOpStatus,
        target_state: SyncOpStatus,
        actor_role: str,
        actor_id: str,
        actor_tenant_id: str,
        entity_tenant_id: str,
        reason: str,
    ) -> StateTransitionAudit:
        """Transitions offline sync outbox operation state."""
        self._validate_tenant_boundary(actor_tenant_id, entity_tenant_id, actor_role)

        key = (current_state, target_state)
        if key not in self.SYNC_TRANSITIONS:
            raise InvalidStateTransitionError(
                f"Illegal sync operation transition from '{current_state.value}' to '{target_state.value}'"
            )

        permitted_roles = self.SYNC_TRANSITIONS[key]
        if actor_role not in permitted_roles:
            raise PermissionError(
                f"Role '{actor_role}' is not authorized to transition sync operation from '{current_state.value}' to '{target_state.value}'. Permitted: {sorted(list(permitted_roles))}"
            )

        audit = StateTransitionAudit(
            entity_type="SyncOperation",
            entity_id=entity_id,
            from_state=current_state.value,
            to_state=target_state.value,
            actor_id=actor_id,
            actor_role=actor_role,
            tenant_id=entity_tenant_id,
            reason=reason,
        )
        self.audit_log.append(audit)
        return audit

    def _validate_tenant_boundary(self, actor_tenant_id: str, entity_tenant_id: str, actor_role: str) -> None:
        """Enforces that an actor cannot transition state on another tenant's entity."""
        if actor_role == "PLATFORM_SUPER_ADMIN":
            return
        if actor_tenant_id != entity_tenant_id:
            raise PermissionError(
                f"Cross-tenant operation rejected: Actor tenant '{actor_tenant_id}' cannot mutate entity owned by tenant '{entity_tenant_id}'"
            )
