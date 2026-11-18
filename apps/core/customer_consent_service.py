"""
backend/apps/core/customer_consent_service.py
Architecture Decision: ADR-0269 (P269).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service for:
1. Tracking explicit customer consent for optional reminders and communications
   (refill reminders, due payment reminders, email receipts, marketing/promotional campaigns, WhatsApp updates).
2. Enforcing strict statutory separation:
   - Mandatory statutory records (tax invoices, prescription dispensations, DGDA antibiotic registers,
     Accounts Receivable ledgers) CANNOT be opted out of or deleted.
   - Opting out of optional communications DOES NOT affect fiscal receipts, transaction histories,
     or debt collection ledgers.
3. Immutable audit logging of all consent grants, updates, and revocations with SHA-256 integrity hashes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    ConsentCaptureMethod,
    CustomerCommunicationConsent,
    CustomerConsentAuditLog,
    CustomerConsentChannel,
    CustomerConsentStatus,
    PosCustomer,
    Tenant,
)
from apps.core.pos_domain_service import PosDomainService


class CustomerConsentService:
    """Domain service managing customer communication consents and statutory separation."""

    STATUTORY_CHANNELS = {
        "STATUTORY_TAX_INVOICES",
        "STATUTORY_AR_LEDGER",
        "STATUTORY_PRESCRIPTION_AUDIT",
        "STATUTORY_ANTIBIOTIC_REGISTER",
    }

    STATUTORY_RETENTION_POLICIES = {
        "tax_invoices": {
            "title_en": "Fiscal Tax & VAT Invoices",
            "title_bn": "মূসক ও রাজস্ব চালান রেকর্ড",
            "is_mandatory": True,
            "can_opt_out": False,
            "legal_basis": "National Board of Revenue VAT Act 2012, Section 51",
            "retention_period_years": 6,
        },
        "receivable_ledger": {
            "title_en": "Accounts Receivable Ledger",
            "title_bn": "গ্রাহক বকেয়া ও লেনদেন খতিয়ান",
            "is_mandatory": True,
            "can_opt_out": False,
            "legal_basis": "Financial Reporting Act 2015 & Bangladesh Accounting Standards",
            "retention_period_years": 7,
        },
        "prescription_audits": {
            "title_en": "Prescription Safety & Dispensing Audits",
            "title_bn": "প্রেসক্রিপশন বিতরণ ও ফার্মাসিস্ট নিরীক্ষা",
            "is_mandatory": True,
            "can_opt_out": False,
            "legal_basis": "DGDA Pharmacy Management & Good Pharmacy Practice Guidelines 2024",
            "retention_period_years": 5,
        },
        "antibiotic_register": {
            "title_en": "Antibiotic & Controlled Drug Dispense Register",
            "title_bn": "অ্যান্টিবায়োটিক রেজিস্টার ও নজরদারি",
            "is_mandatory": True,
            "can_opt_out": False,
            "legal_basis": "DGDA Antimicrobial Resistance (AMR) Containment Mandate 2024",
            "retention_period_years": 5,
        },
    }

    @classmethod
    def get_customer_consent_matrix(
        cls,
        *,
        user: Any,
        customer_id: str,
        branch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Retrieves the complete communication consent matrix for a customer,
        clearly separated from non-opt-out statutory record retention policies.
        """
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=branch_id, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )

        customer = PosCustomer.objects.filter(tenant=tenant, customer_id=customer_id).first()
        if not customer:
            raise NotFound(
                {
                    "error_code": "ERR_CUSTOMER_NOT_FOUND",
                    "customer_id": f"Customer '{customer_id}' was not found in active tenant scope.",
                }
            )

        # Retrieve existing consent records
        existing_consents = {
            c.channel: c
            for c in CustomerCommunicationConsent.objects.filter(tenant=tenant, customer=customer)
        }

        optional_matrix = []
        for ch_key, ch_label in CustomerConsentChannel.choices:
            record = existing_consents.get(ch_key)
            is_granted = record is not None and record.status == CustomerConsentStatus.GRANTED
            optional_matrix.append(
                {
                    "channel": ch_key,
                    "display_name": ch_label,
                    "status": record.status if record else CustomerConsentStatus.REVOKED,
                    "is_granted": is_granted,
                    "capture_method": record.capture_method if record else None,
                    "notes": record.notes if record else "",
                    "granted_at": record.granted_at.isoformat() if record and record.granted_at else None,
                    "revoked_at": record.revoked_at.isoformat() if record and record.revoked_at else None,
                    "revocation_reason": record.revocation_reason if record else "",
                    "last_updated_at": record.updated_at.isoformat() if record and record.updated_at else None,
                }
            )

        return {
            "customer": {
                "customer_id": customer.customer_id,
                "full_name": customer.full_name,
                "phone": customer.phone,
                "email": customer.email,
                "is_active": customer.is_active,
            },
            "statutory_record_retention": cls.STATUTORY_RETENTION_POLICIES,
            "optional_communications": optional_matrix,
            "all_optional_revoked": all(not m["is_granted"] for m in optional_matrix),
        }

    @classmethod
    @transaction.atomic
    def set_channel_consent(
        cls,
        *,
        user: Any,
        customer_id: str,
        channel: str,
        status: str,
        capture_method: str = ConsentCaptureMethod.POS_VERBAL_CONSENT,
        reason: str = "",
        notes: str = "",
        branch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Grants or revokes customer consent for a specific optional communication channel.
        Rejects attempts to opt out of statutory mandatory records.
        """
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES | PosDomainService.POS_CUSTOMER_ADMIN_ROLES,
        )

        clean_channel = str(channel or "").strip().upper()
        clean_status = str(status or "").strip().upper()

        # Statutory record opt-out rejection
        if clean_channel in cls.STATUTORY_CHANNELS:
            raise ValidationError(
                {
                    "error_code": "ERR_STATUTORY_RECORD_IMMUTABLE",
                    "channel": clean_channel,
                    "message_en": "Statutory transaction record keeping (tax invoices, prescription registers, and accounts receivable ledgers) is legally mandated and cannot be opted out of or deleted.",
                    "message_bn": "আইনত বাধ্যতামূলক লেনদেনের হিসাব, প্রেসক্রিপশন রেজিস্টার ও লেজার রেকর্ড বাতিল বা অপ্ট-আউট করা সম্ভব নয়।",
                }
            )

        if clean_channel not in CustomerConsentChannel.values:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_CONSENT_CHANNEL",
                    "channel": f"'{clean_channel}' is not a recognized optional communication channel. Valid choices: {list(CustomerConsentChannel.values)}.",
                }
            )

        if clean_status not in CustomerConsentStatus.values:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_CONSENT_STATUS",
                    "status": f"'{clean_status}' is not a valid consent status. Valid choices: {list(CustomerConsentStatus.values)}.",
                }
            )

        customer = PosCustomer.objects.select_for_update().filter(tenant=tenant, customer_id=customer_id).first()
        if not customer:
            raise NotFound(
                {
                    "error_code": "ERR_CUSTOMER_NOT_FOUND",
                    "customer_id": f"Customer '{customer_id}' was not found in active tenant scope.",
                }
            )

        consent_record, created = CustomerCommunicationConsent.objects.select_for_update().get_or_create(
            tenant=tenant,
            customer=customer,
            channel=clean_channel,
            defaults={
                "branch": branch or customer.primary_branch,
                "status": clean_status,
                "capture_method": capture_method or ConsentCaptureMethod.POS_VERBAL_CONSENT,
                "notes": notes,
                "captured_by": user,
                "granted_at": timezone.now() if clean_status == CustomerConsentStatus.GRANTED else timezone.now(),
            },
        )

        prev_status = "" if created else consent_record.status

        # Update record
        now = timezone.now()
        consent_record.status = clean_status
        consent_record.capture_method = capture_method or consent_record.capture_method
        consent_record.notes = notes or consent_record.notes

        if clean_status == CustomerConsentStatus.GRANTED:
            consent_record.granted_at = now
            consent_record.revoked_at = None
            consent_record.revocation_reason = ""
            consent_record.captured_by = user
        else:
            consent_record.revoked_at = now
            consent_record.revocation_reason = reason or "Customer requested opt-out"
            consent_record.revoked_by = user

        consent_record.save()

        # Update JSON privacy_preferences mirror on customer profile
        prefs = dict(customer.privacy_preferences or {})
        prefs[clean_channel.lower()] = clean_status
        customer.privacy_preferences = prefs
        customer.save(update_fields=["privacy_preferences", "updated_at"])

        # Compute SHA256 integrity hash for audit log
        hash_payload = {
            "consent_id": consent_record.consent_id,
            "tenant_id": str(tenant.tenant_id),
            "customer_id": customer.customer_id,
            "channel": clean_channel,
            "previous_status": prev_status,
            "new_status": clean_status,
            "performed_by_id": str(getattr(user, "user_id", user.pk)),
            "timestamp": now.isoformat(),
        }
        canonical_str = json.dumps(hash_payload, sort_keys=True, separators=(",", ":"))
        sha_hash = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()

        # Create immutable audit log
        audit = CustomerConsentAuditLog.objects.create(
            tenant=tenant,
            consent=consent_record,
            customer=customer,
            action=clean_status,
            channel=clean_channel,
            previous_status=prev_status,
            new_status=clean_status,
            reason=reason,
            performed_by=user,
            sha256_hash=sha_hash,
        )

        return {
            "consent_id": consent_record.consent_id,
            "channel": clean_channel,
            "status": clean_status,
            "is_granted": clean_status == CustomerConsentStatus.GRANTED,
            "capture_method": consent_record.capture_method,
            "granted_at": consent_record.granted_at.isoformat() if consent_record.granted_at else None,
            "revoked_at": consent_record.revoked_at.isoformat() if consent_record.revoked_at else None,
            "audit_id": audit.audit_id,
            "sha256_hash": sha_hash,
        }

    @classmethod
    @transaction.atomic
    def revoke_all_optional_consents(
        cls,
        *,
        user: Any,
        customer_id: str,
        reason: str = "Customer requested complete opt-out of optional communications",
        branch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Revokes all optional communication consents in one step while explicitly
        guaranteeing that statutory transaction ledgers and prescription records remain active.
        """
        results = []
        for ch_key in CustomerConsentChannel.values:
            res = cls.set_channel_consent(
                user=user,
                customer_id=customer_id,
                channel=ch_key,
                status=CustomerConsentStatus.REVOKED,
                reason=reason,
                branch_id=branch_id,
            )
            results.append(res)

        return {
            "customer_id": customer_id,
            "revoked_channels_count": len(results),
            "channels_revoked": [r["channel"] for r in results],
            "statutory_records_status": "ACTIVE_IMMUTABLE",
            "message": "All optional communications revoked. Statutory transaction records remain preserved as required by law.",
        }

    @classmethod
    def is_communication_allowed(
        cls,
        *,
        user: Any,
        customer_id: str,
        channel: str,
    ) -> bool:
        """
        Gate check: Returns True if active granted consent exists for the optional channel.
        Always returns False for unknown or revoked channels.
        """
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )

        record = CustomerCommunicationConsent.objects.filter(
            tenant=tenant,
            customer_id=customer_id,
            channel=str(channel).upper().strip(),
            status=CustomerConsentStatus.GRANTED,
        ).first()

        return record is not None

    @classmethod
    def get_consent_audit_logs(
        cls,
        *,
        user: Any,
        customer_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Returns immutable consent audit trail for a customer."""
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )

        logs = (
            CustomerConsentAuditLog.objects.filter(tenant=tenant, customer_id=customer_id)
            .select_related("performed_by")
            .order_by("-created_at")[:limit]
        )

        return [
            {
                "audit_id": a.audit_id,
                "action": a.action,
                "channel": a.channel,
                "previous_status": a.previous_status,
                "new_status": a.new_status,
                "reason": a.reason,
                "performed_by_email": a.performed_by.email if a.performed_by else "",
                "sha256_hash": a.sha256_hash,
                "created_at": a.created_at.isoformat(),
            }
            for a in logs
        ]
