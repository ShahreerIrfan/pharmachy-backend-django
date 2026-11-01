"""
backend/apps/core/billing_report_service.py

Architecture Decision: ADR-0098
Phase 05: Basic/Standard/Pro Subscription & Billing (P098)
Platform MRR, Churn, Collections, Outstanding Invoices, and Subscription Audit Reports Service.

Computes authoritative macro and tenant-level billing metrics from verified database models:
- SubscriptionRecord
- SubscriptionRenewalInvoice
- ManualPaymentClaim
- PaymentGatewayWebhookLog
- AuditLogEntry
- PlatformBillingReportSnapshot
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BillingReportPeriod,
    BillingReportType,
    ManualPaymentClaim,
    PaymentGatewayWebhookLog,
    PlatformBillingReportSnapshot,
    SubscriptionRecord,
    SubscriptionRenewalInvoice,
    Tenant,
)

logger = logging.getLogger("pharmacy.billing.reports")


class PlatformBillingReportService:
    """
    Central authoritative service for Platform MRR, Churn, Collections,
    Accounts Receivable / Outstanding Invoices, and Subscription Audit reporting.
    """

    @classmethod
    def _parse_date(cls, value: Any, field_name: str) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, date):
            return value
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            clean_val = value.strip()
            if not clean_val:
                return None
            try:
                return datetime.strptime(clean_val, "%Y-%m-%d").date()
            except ValueError:
                raise ValidationError({field_name: f"Invalid date format '{value}'. Expected YYYY-MM-DD."})
        raise ValidationError({field_name: f"Invalid date type for '{field_name}'."})

    @classmethod
    def _format_decimal(cls, value: Any) -> str:
        if value is None:
            return "0.00"
        dec = Decimal(str(value)).quantize(Decimal("0.01"))
        return str(dec)

    @classmethod
    def get_mrr_report(
        cls,
        start_date: Any = None,
        end_date: Any = None,
        operator_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Calculates Monthly Recurring Revenue (MRR), ARR, ARPU, and tier breakdown.
        """
        parsed_start = cls._parse_date(start_date, "start_date")
        parsed_end = cls._parse_date(end_date, "end_date")
        if parsed_start and parsed_end and parsed_start > parsed_end:
            raise ValidationError({"date_range": "start_date must be earlier than or equal to end_date."})

        active_subs = SubscriptionRecord.objects.filter(status__in=["ACTIVE", "GRACE_PERIOD"])

        tier_breakdown = {
            "BASIC": Decimal("0.00"),
            "STANDARD": Decimal("0.00"),
            "PRO": Decimal("0.00"),
        }
        total_mrr = Decimal("0.00")
        paying_tenant_ids = set()

        for sub in active_subs:
            paying_tenant_ids.add(sub.tenant_id)
            amt = Decimal(str(sub.amount_bdt or "0.00"))
            if sub.billing_cycle == "YEARLY":
                monthly_amt = (amt / Decimal("12")).quantize(Decimal("0.01"))
            else:
                monthly_amt = amt.quantize(Decimal("0.01"))

            tier = str(sub.plan_tier).upper()
            if tier in tier_breakdown:
                tier_breakdown[tier] += monthly_amt
            total_mrr += monthly_amt

        paying_tenants_count = len(paying_tenant_ids)
        total_arr = (total_mrr * Decimal("12")).quantize(Decimal("0.01"))
        denom = Decimal(str(max(1, paying_tenants_count)))
        arpu = (total_mrr / denom).quantize(Decimal("0.01"))

        # Expansion MRR from upgrades
        expansion_mrr = Decimal("0.00")
        try:
            from apps.core.models import SubscriptionUpgradeRecord

            upgrades_qs = SubscriptionUpgradeRecord.objects.all()
            if parsed_start:
                upgrades_qs = upgrades_qs.filter(created_at__date__gte=parsed_start)
            if parsed_end:
                upgrades_qs = upgrades_qs.filter(created_at__date__lte=parsed_end)
            exp_agg = upgrades_qs.aggregate(total=Sum("prorated_charge_bdt"))["total"]
            if exp_agg:
                expansion_mrr = Decimal(str(exp_agg)).quantize(Decimal("0.01"))
        except Exception:
            pass

        # Churned MRR from cancelled/expired subscriptions
        churned_subs = SubscriptionRecord.objects.filter(status__in=["CANCELLED", "EXPIRED"])
        churned_mrr = Decimal("0.00")
        for sub in churned_subs:
            amt = Decimal(str(sub.amount_bdt or "0.00"))
            if sub.billing_cycle == "YEARLY":
                m_amt = (amt / Decimal("12")).quantize(Decimal("0.01"))
            else:
                m_amt = amt.quantize(Decimal("0.01"))
            churned_mrr += m_amt

        return {
            "currency": "BDT",
            "total_mrr_bdt": cls._format_decimal(total_mrr),
            "total_arr_bdt": cls._format_decimal(total_arr),
            "arpu_bdt": cls._format_decimal(arpu),
            "active_subscriptions_count": active_subs.count(),
            "paying_tenants_count": paying_tenants_count,
            "tier_breakdown": {
                "BASIC": cls._format_decimal(tier_breakdown["BASIC"]),
                "STANDARD": cls._format_decimal(tier_breakdown["STANDARD"]),
                "PRO": cls._format_decimal(tier_breakdown["PRO"]),
            },
            "expansion_mrr_bdt": cls._format_decimal(expansion_mrr),
            "churned_mrr_bdt": cls._format_decimal(churned_mrr),
        }

    @classmethod
    def get_churn_report(
        cls,
        start_date: Any = None,
        end_date: Any = None,
        operator_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Calculates churn metrics, attrition rate percentage, and reasons breakdown.
        """
        parsed_start = cls._parse_date(start_date, "start_date")
        parsed_end = cls._parse_date(end_date, "end_date")
        if parsed_start and parsed_end and parsed_start > parsed_end:
            raise ValidationError({"date_range": "start_date must be earlier than or equal to end_date."})

        active_count = SubscriptionRecord.objects.filter(status__in=["ACTIVE", "GRACE_PERIOD"]).count()
        churned_subs = SubscriptionRecord.objects.filter(status__in=["CANCELLED", "EXPIRED", "SUSPENDED"])

        lost_mrr = Decimal("0.00")
        reasons_breakdown: Dict[str, int] = {
            "CANCELLED": 0,
            "EXPIRED": 0,
            "SUSPENDED": 0,
        }

        for sub in churned_subs:
            st = str(sub.status).upper()
            reasons_breakdown[st] = reasons_breakdown.get(st, 0) + 1
            amt = Decimal(str(sub.amount_bdt or "0.00"))
            if sub.billing_cycle == "YEARLY":
                m_amt = (amt / Decimal("12")).quantize(Decimal("0.01"))
            else:
                m_amt = amt.quantize(Decimal("0.01"))
            lost_mrr += m_amt

        churned_count = churned_subs.count()
        total_subscribers = active_count + churned_count
        if total_subscribers > 0:
            churn_pct = (Decimal(str(churned_count)) / Decimal(str(total_subscribers)) * Decimal("100")).quantize(
                Decimal("0.01")
            )
        else:
            churn_pct = Decimal("0.00")

        return {
            "churn_rate_pct": cls._format_decimal(churn_pct),
            "churned_subscriptions_count": churned_count,
            "lost_mrr_bdt": cls._format_decimal(lost_mrr),
            "reasons_breakdown": reasons_breakdown,
        }

    @classmethod
    def get_collections_report(
        cls,
        start_date: Any = None,
        end_date: Any = None,
        operator_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Calculates verified realized collections broken down by payment rails.
        """
        parsed_start = cls._parse_date(start_date, "start_date")
        parsed_end = cls._parse_date(end_date, "end_date")
        if parsed_start and parsed_end and parsed_start > parsed_end:
            raise ValidationError({"date_range": "start_date must be earlier than or equal to end_date."})

        # Manual approved claims
        claims_qs = ManualPaymentClaim.objects.filter(status="APPROVED")
        if parsed_start:
            claims_qs = claims_qs.filter(created_at__date__gte=parsed_start)
        if parsed_end:
            claims_qs = claims_qs.filter(created_at__date__lte=parsed_end)

        by_method = {
            "BKASH": Decimal("0.00"),
            "NAGAD": Decimal("0.00"),
            "BANK_TRANSFER": Decimal("0.00"),
            "GATEWAY_ONLINE": Decimal("0.00"),
        }

        for claim in claims_qs:
            amt = Decimal(
                str(getattr(claim, "claimed_amount_bdt", None) or getattr(claim, "amount_bdt", None) or "0.00")
            )
            method = str(claim.payment_method).upper()
            if "BKASH" in method:
                by_method["BKASH"] += amt
            elif "NAGAD" in method:
                by_method["NAGAD"] += amt
            elif "BANK" in method:
                by_method["BANK_TRANSFER"] += amt
            else:
                by_method["BANK_TRANSFER"] += amt

        # Gateway verified logs
        gw_qs = PaymentGatewayWebhookLog.objects.filter(signature_verified=True)
        if parsed_start:
            gw_qs = gw_qs.filter(created_at__date__gte=parsed_start)
        if parsed_end:
            gw_qs = gw_qs.filter(created_at__date__lte=parsed_end)

        gw_count = gw_qs.count()
        # Sum gateway payments from paid invoices marked with gateway
        paid_invoices_qs = SubscriptionRenewalInvoice.objects.filter(status="PAID")
        if parsed_start:
            paid_invoices_qs = paid_invoices_qs.filter(paid_at__date__gte=parsed_start)
        if parsed_end:
            paid_invoices_qs = paid_invoices_qs.filter(paid_at__date__lte=parsed_end)

        total_paid_invoices = paid_invoices_qs.aggregate(total=Sum("total_bdt"))["total"] or Decimal("0.00")
        total_paid_invoices = Decimal(str(total_paid_invoices))

        # Manual total
        manual_total = by_method["BKASH"] + by_method["NAGAD"] + by_method["BANK_TRANSFER"]
        if total_paid_invoices > manual_total:
            by_method["GATEWAY_ONLINE"] = (total_paid_invoices - manual_total).quantize(Decimal("0.01"))

        total_collected = sum(by_method.values()).quantize(Decimal("0.01"))

        # Billed amount
        invoices_all = SubscriptionRenewalInvoice.objects.all()
        if parsed_start:
            invoices_all = invoices_all.filter(created_at__date__gte=parsed_start)
        if parsed_end:
            invoices_all = invoices_all.filter(created_at__date__lte=parsed_end)
        total_billed = invoices_all.aggregate(total=Sum("total_bdt"))["total"] or Decimal("0.00")
        total_billed = Decimal(str(total_billed))

        if total_billed > 0:
            eff_pct = (total_collected / total_billed * Decimal("100")).quantize(Decimal("0.01"))
        else:
            eff_pct = Decimal("100.00") if total_collected > 0 else Decimal("0.00")

        return {
            "currency": "BDT",
            "total_collected_bdt": cls._format_decimal(total_collected),
            "collections_by_method": {
                "BKASH": cls._format_decimal(by_method["BKASH"]),
                "NAGAD": cls._format_decimal(by_method["NAGAD"]),
                "BANK_TRANSFER": cls._format_decimal(by_method["BANK_TRANSFER"]),
                "GATEWAY_ONLINE": cls._format_decimal(by_method["GATEWAY_ONLINE"]),
            },
            "verified_claims_count": claims_qs.count(),
            "gateway_transactions_count": gw_count,
            "collection_efficiency_pct": cls._format_decimal(eff_pct),
        }

    @classmethod
    def get_outstanding_report(
        cls,
        start_date: Any = None,
        end_date: Any = None,
        operator_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Calculates outstanding accounts receivable, aging brackets, and at-risk delinquent accounts.
        """
        parsed_start = cls._parse_date(start_date, "start_date")
        parsed_end = cls._parse_date(end_date, "end_date")
        if parsed_start and parsed_end and parsed_start > parsed_end:
            raise ValidationError({"date_range": "start_date must be earlier than or equal to end_date."})

        outstanding_qs = SubscriptionRenewalInvoice.objects.filter(
            status__in=["ISSUED", "PAST_DUE", "OVERDUE"]
        )
        if parsed_start:
            outstanding_qs = outstanding_qs.filter(period_end__gte=parsed_start)
        if parsed_end:
            outstanding_qs = outstanding_qs.filter(period_end__lte=parsed_end)

        today = timezone.now().date()
        brackets: Dict[str, Dict[str, Any]] = {
            "current": {"count": 0, "amount": Decimal("0.00")},
            "days_1_30": {"count": 0, "amount": Decimal("0.00")},
            "days_31_60": {"count": 0, "amount": Decimal("0.00")},
            "days_61_90": {"count": 0, "amount": Decimal("0.00")},
            "days_over_90": {"count": 0, "amount": Decimal("0.00")},
        }

        at_risk_tenants = set()
        total_outstanding = Decimal("0.00")

        for inv in outstanding_qs:
            amt = Decimal(str(inv.total_bdt or "0.00"))
            total_outstanding += amt
            days_overdue = (today - inv.period_end).days

            if days_overdue <= 0:
                brackets["current"]["count"] += 1
                brackets["current"]["amount"] += amt
            elif 1 <= days_overdue <= 30:
                brackets["days_1_30"]["count"] += 1
                brackets["days_1_30"]["amount"] += amt
            elif 31 <= days_overdue <= 60:
                brackets["days_31_60"]["count"] += 1
                brackets["days_31_60"]["amount"] += amt
                at_risk_tenants.add(inv.tenant_id)
            elif 61 <= days_overdue <= 90:
                brackets["days_61_90"]["count"] += 1
                brackets["days_61_90"]["amount"] += amt
                at_risk_tenants.add(inv.tenant_id)
            else:
                brackets["days_over_90"]["count"] += 1
                brackets["days_over_90"]["amount"] += amt
                at_risk_tenants.add(inv.tenant_id)

        formatted_brackets = {
            k: {"count": v["count"], "amount_bdt": cls._format_decimal(v["amount"])}
            for k, v in brackets.items()
        }

        return {
            "currency": "BDT",
            "total_outstanding_bdt": cls._format_decimal(total_outstanding),
            "outstanding_invoices_count": outstanding_qs.count(),
            "aging_brackets": formatted_brackets,
            "at_risk_tenants_count": len(at_risk_tenants),
        }

    @classmethod
    def get_subscription_audit_report(
        cls,
        tenant_id: Optional[str] = None,
        start_date: Any = None,
        end_date: Any = None,
        event_type: Optional[str] = None,
        limit: int = 50,
        operator_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Retrieves immutable audit trail for subscription and billing actions.
        """
        parsed_start = cls._parse_date(start_date, "start_date")
        parsed_end = cls._parse_date(end_date, "end_date")
        if parsed_start and parsed_end and parsed_start > parsed_end:
            raise ValidationError({"date_range": "start_date must be earlier than or equal to end_date."})

        audit_qs = AuditLogEntry.objects.filter(
            resource_type__in=[
                "SUBSCRIPTION",
                "RENEWAL_INVOICE",
                "MANUAL_PAYMENT",
                "GATEWAY_WEBHOOK",
                "OFFLINE_LEASE",
                "QUOTA_COUNTER",
                "DEVICE_SEAT",
                "PLATFORM_BILLING_REPORT",
            ]
        )
        if tenant_id:
            audit_qs = audit_qs.filter(tenant_id=tenant_id)
        if parsed_start:
            audit_qs = audit_qs.filter(timestamp__date__gte=parsed_start)
        if parsed_end:
            audit_qs = audit_qs.filter(timestamp__date__lte=parsed_end)
        if event_type:
            audit_qs = audit_qs.filter(action__icontains=event_type)

        total_count = audit_qs.count()
        entries = audit_qs.order_by("-timestamp")[:limit]

        events = []
        for entry in entries:
            events.append(
                {
                    "event_id": str(entry.entry_id),
                    "timestamp": entry.timestamp.isoformat() if hasattr(entry.timestamp, "isoformat") else str(entry.timestamp),
                    "tenant_id": entry.tenant_id,
                    "action": entry.action,
                    "resource_type": entry.resource_type,
                    "resource_id": entry.resource_id,
                    "status": entry.status,
                    "operator_id": str(entry.user_id) if entry.user_id else None,
                    "metadata": entry.metadata_json or {},
                }
            )

        return {
            "total_events_count": total_count,
            "events": events,
        }

    @classmethod
    def get_full_summary(
        cls,
        start_date: Any = None,
        end_date: Any = None,
        period: str = "MONTHLY",
        operator_user: Any = None,
        save_snapshot: bool = False,
    ) -> Dict[str, Any]:
        """
        Consolidates MRR, Churn, Collections, and Outstanding Invoices into an executive report.
        Optionally persists an audited snapshot.
        """
        parsed_start = cls._parse_date(start_date, "start_date") or timezone.now().date().replace(day=1)
        parsed_end = cls._parse_date(end_date, "end_date") or timezone.now().date()
        if parsed_start > parsed_end:
            raise ValidationError({"date_range": "start_date must be earlier than or equal to end_date."})

        mrr_rep = cls.get_mrr_report(start_date=parsed_start, end_date=parsed_end, operator_user=operator_user)
        churn_rep = cls.get_churn_report(start_date=parsed_start, end_date=parsed_end, operator_user=operator_user)
        coll_rep = cls.get_collections_report(start_date=parsed_start, end_date=parsed_end, operator_user=operator_user)
        out_rep = cls.get_outstanding_report(start_date=parsed_start, end_date=parsed_end, operator_user=operator_user)

        full_summary = {
            "generated_at": timezone.now().isoformat(),
            "currency": "BDT",
            "period": str(period).upper(),
            "mrr_summary": mrr_rep,
            "churn_summary": churn_rep,
            "collections_summary": coll_rep,
            "outstanding_summary": out_rep,
        }

        if save_snapshot:
            report_id = f"rpt-bill-{uuid.uuid4().hex[:12]}"
            PlatformBillingReportSnapshot.objects.create(
                report_id=report_id,
                report_type=BillingReportType.FULL_SUMMARY,
                period=period if period in BillingReportPeriod.values else BillingReportPeriod.MONTHLY,
                start_date=parsed_start,
                end_date=parsed_end,
                currency="BDT",
                total_mrr_bdt=Decimal(mrr_rep["total_mrr_bdt"]),
                total_arr_bdt=Decimal(mrr_rep["total_arr_bdt"]),
                arpu_bdt=Decimal(mrr_rep["arpu_bdt"]),
                total_collected_bdt=Decimal(coll_rep["total_collected_bdt"]),
                total_outstanding_bdt=Decimal(out_rep["total_outstanding_bdt"]),
                churn_rate_pct=Decimal(churn_rep["churn_rate_pct"]),
                active_subscriptions_count=mrr_rep["active_subscriptions_count"],
                churned_subscriptions_count=churn_rep["churned_subscriptions_count"],
                outstanding_invoices_count=out_rep["outstanding_invoices_count"],
                mrr_breakdown_json=mrr_rep,
                churn_details_json=churn_rep,
                collections_breakdown_json=coll_rep,
                aging_brackets_json=out_rep["aging_brackets"],
                generated_by_user_id=str(getattr(operator_user, "id", None) or "system"),
            )
            full_summary["report_id"] = report_id

        # Audit trail entry
        AuditLogEntry.objects.create(
            action="PLATFORM_BILLING_REPORT_GENERATED",
            resource_type="PLATFORM_BILLING_REPORT",
            resource_id="summary",
            tenant_id="platform-global",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "period": period,
                "total_mrr_bdt": mrr_rep["total_mrr_bdt"],
                "total_collected_bdt": coll_rep["total_collected_bdt"],
                "total_outstanding_bdt": out_rep["total_outstanding_bdt"],
            },
        )

        return full_summary

    @classmethod
    def get_tenant_billing_report(
        cls,
        tenant_id: str,
        operator_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Retrieves tenant-isolated billing statement, lifetime totals, recent invoices,
        and payment claims for pharmacy owners and managers.
        """
        clean_tid = str(tenant_id or "").strip()
        if not clean_tid:
            raise ValidationError({"tenant_id": "tenant_id is required."})

        try:
            tenant = Tenant.objects.get(tenant_id=clean_tid)
        except Tenant.DoesNotExist:
            raise ValidationError({"tenant_id": f"Tenant '{clean_tid}' does not exist."})

        # Subscription record
        sub = SubscriptionRecord.objects.filter(tenant_id=clean_tid).first()
        sub_dict = None
        if sub:
            sub_dict = {
                "plan_tier": sub.plan_tier,
                "plan_code": getattr(sub, "plan_code", None) or f"{sub.plan_tier}_{sub.billing_cycle}",
                "status": sub.status,
                "billing_cycle": sub.billing_cycle,
                "amount_bdt": cls._format_decimal(sub.amount_bdt),
                "start_date": str(sub.start_date) if sub.start_date else None,
                "renewal_date": str(sub.renewal_date) if sub.renewal_date else None,
                "is_auto_renew": getattr(sub, "is_auto_renew", False),
            }

        # Invoices
        invoices_qs = SubscriptionRenewalInvoice.objects.filter(tenant_id=clean_tid).order_by("-created_at")
        total_billed = invoices_qs.aggregate(total=Sum("total_bdt"))["total"] or Decimal("0.00")
        total_paid = (
            invoices_qs.filter(status="PAID").aggregate(total=Sum("total_bdt"))["total"] or Decimal("0.00")
        )
        total_outstanding = (
            invoices_qs.filter(status__in=["ISSUED", "PAST_DUE", "OVERDUE"]).aggregate(total=Sum("total_bdt"))["total"]
            or Decimal("0.00")
        )

        recent_invoices = []
        for inv in invoices_qs[:10]:
            recent_invoices.append(
                {
                    "invoice_number": inv.invoice_number,
                    "plan_tier": inv.plan_tier,
                    "status": inv.status,
                    "total_bdt": cls._format_decimal(inv.total_bdt),
                    "issued_at": inv.created_at.isoformat() if inv.created_at else None,
                    "due_date": str(inv.period_end),
                    "paid_at": inv.paid_at.isoformat() if inv.paid_at else None,
                }
            )

        # Payment claims
        claims_qs = ManualPaymentClaim.objects.filter(tenant_id=clean_tid).order_by("-created_at")
        recent_claims = []
        for claim in claims_qs[:10]:
            recent_claims.append(
                {
                    "claim_id": claim.claim_id,
                    "invoice_id": getattr(claim, "invoice_id", None) or str(getattr(claim, "invoice", "")),
                    "amount_bdt": cls._format_decimal(
                        getattr(claim, "claimed_amount_bdt", None) or getattr(claim, "amount_bdt", None)
                    ),
                    "payment_method": claim.payment_method,
                    "transaction_reference": claim.transaction_reference,
                    "status": claim.status,
                    "created_at": claim.created_at.isoformat() if claim.created_at else None,
                }
            )

        # Audit events for this tenant
        audit_events = cls.get_subscription_audit_report(tenant_id=clean_tid, limit=20)["events"]

        AuditLogEntry.objects.create(
            action="TENANT_BILLING_REPORT_VIEWED",
            resource_type="TENANT_BILLING_REPORT",
            resource_id=clean_tid,
            tenant_id=clean_tid,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "tenant_name": tenant.name,
                "invoices_count": invoices_qs.count(),
                "outstanding_balance_bdt": cls._format_decimal(total_outstanding),
            },
        )

        return {
            "tenant_id": clean_tid,
            "currency": "BDT",
            "current_subscription": sub_dict,
            "lifetime_billed_bdt": cls._format_decimal(total_billed),
            "lifetime_paid_bdt": cls._format_decimal(total_paid),
            "outstanding_balance_bdt": cls._format_decimal(total_outstanding),
            "invoices_count": invoices_qs.count(),
            "recent_invoices": recent_invoices,
            "payment_claims_count": claims_qs.count(),
            "recent_payment_claims": recent_claims,
            "subscription_audit_events": audit_events,
        }
