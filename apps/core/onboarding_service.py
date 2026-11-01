"""
apps/core/onboarding_service.py

Architecture Decision: ADR-0079
Multi-step progressive and atomic Tenant Onboarding Wizard Service.
Manages:
  1. Progressive session tracking (Steps 1 through 6).
  2. DGDA Drug Licensing & TIN compliance validation.
  3. Physical branch and initial staff credentials (with PCB registration).
  4. Initial inventory setup (Seed Baseline vs Empty Shelf).
  5. Subscription tier allocation with BDT precision.
  6. 100% rollback-safe atomic provisioning and immutable audit trail.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    InitialInventoryMode,
    MedicineProduct,
    OnboardingWizardStatus,
    PlanTier,
    StaffMember,
    SubscriptionRecord,
    Tenant,
    TenantLifecycleStatus,
    TenantOnboardingSession,
)
from apps.core.provisioning import TenantProvisioningEngine
from apps.identity.models import ActivationStatus, PharmacyUser, UserRole, normalize_bangladesh_phone


class TenantOnboardingService:
    """
    Coordinates multi-step onboarding wizard execution and atomic provisioning.
    """

    @classmethod
    def create_session(cls, data: Dict[str, Any], creator: Optional[Any] = None) -> TenantOnboardingSession:
        """
        Creates a new draft onboarding wizard session.
        """
        raw_subdomain = data.get("subdomain")
        clean_subdomain = TenantProvisioningEngine.validate_subdomain(raw_subdomain)

        # Check existing sessions with same subdomain
        if TenantOnboardingSession.objects.filter(
            subdomain=clean_subdomain,
            status__in=[OnboardingWizardStatus.DRAFT, OnboardingWizardStatus.IN_PROGRESS],
        ).exists():
            raise ValidationError(
                {"subdomain": [f"Subdomain '{clean_subdomain}' is already active in an ongoing onboarding session."]}
            )

        raw_phone = data.get("contact_phone", "")
        clean_phone = normalize_bangladesh_phone(raw_phone)
        if PharmacyUser.objects.filter(phone_number=clean_phone).exists():
            raise ValidationError(
                {"contact_phone": [f"A user with phone number '{clean_phone}' is already registered."]}
            )

        drug_license = str(data.get("drug_license_number", "")).strip()
        if not drug_license or len(drug_license) < 3:
            raise ValidationError(
                {"drug_license_number": ["Valid DGDA Drug License number is mandatory for pharmacy onboarding."]}
            )

        pharmacy_name = str(data.get("pharmacy_name", "")).strip()
        if not pharmacy_name or len(pharmacy_name) < 3:
            raise ValidationError({"pharmacy_name": ["Pharmacy name must be at least 3 characters."]})

        owner_full_name = str(data.get("owner_full_name", "")).strip()
        if not owner_full_name:
            raise ValidationError({"owner_full_name": ["Owner full name is required."]})

        contact_email = str(data.get("contact_email", "")).strip().lower()
        if not contact_email or "@" not in contact_email:
            raise ValidationError({"contact_email": ["Valid contact email is required."]})
        if PharmacyUser.objects.filter(email=contact_email).exists():
            raise ValidationError(
                {"contact_email": [f"A user with email '{contact_email}' is already registered on the platform."]}
            )

        session_id = f"onb-{uuid.uuid4().hex[:12]}"
        plan_tier = str(data.get("plan_tier", PlanTier.STANDARD)).upper().strip()
        if plan_tier not in PlanTier.values:
            plan_tier = PlanTier.STANDARD

        inventory_mode = data.get("initial_inventory_mode", InitialInventoryMode.SEED_BASELINE)
        if inventory_mode not in InitialInventoryMode.values:
            inventory_mode = InitialInventoryMode.SEED_BASELINE

        creator_user = creator if isinstance(creator, PharmacyUser) else None

        session = TenantOnboardingSession.objects.create(
            session_id=session_id,
            pharmacy_name=pharmacy_name,
            trade_license_number=str(data.get("trade_license_number", "")).strip(),
            drug_license_number=drug_license,
            tin_number=str(data.get("tin_number", "")).strip(),
            subdomain=clean_subdomain,
            contact_email=contact_email,
            contact_phone=clean_phone,
            owner_full_name=owner_full_name,
            branch_name=str(data.get("branch_name") or f"{pharmacy_name} - Main Branch").strip(),
            branch_address=str(data.get("branch_address") or "Dhaka, Bangladesh").strip(),
            initial_staff_data=data.get("initial_staff_data") or [],
            initial_inventory_mode=inventory_mode,
            initial_inventory_data=data.get("initial_inventory_data") or [],
            plan_tier=plan_tier,
            billing_cycle=str(data.get("billing_cycle", "MONTHLY")).upper().strip(),
            is_trial=bool(data.get("is_trial", False)),
            payment_method=str(data.get("payment_method", "BKASH")).upper().strip(),
            current_step=1,
            status=OnboardingWizardStatus.DRAFT,
            created_by=creator_user,
        )
        return session

    @classmethod
    def update_session_step(
        cls,
        session: TenantOnboardingSession,
        step: int,
        step_data: Dict[str, Any],
    ) -> TenantOnboardingSession:
        """
        Updates an ongoing onboarding session with step data and advances progress.
        """
        if session.status == OnboardingWizardStatus.COMPLETED:
            raise ValidationError("Cannot update an already completed onboarding session.")
        if session.status == OnboardingWizardStatus.ABANDONED:
            raise ValidationError("Cannot update an abandoned onboarding session.")

        if "pharmacy_name" in step_data:
            session.pharmacy_name = str(step_data["pharmacy_name"]).strip()
        if "trade_license_number" in step_data:
            session.trade_license_number = str(step_data["trade_license_number"]).strip()
        if "drug_license_number" in step_data:
            dl = str(step_data["drug_license_number"]).strip()
            if not dl or len(dl) < 3:
                raise ValidationError({"drug_license_number": ["Valid DGDA Drug License number is mandatory."]})
            session.drug_license_number = dl
        if "tin_number" in step_data:
            session.tin_number = str(step_data["tin_number"]).strip()
        if "contact_email" in step_data:
            session.contact_email = str(step_data["contact_email"]).strip().lower()
        if "contact_phone" in step_data:
            session.contact_phone = normalize_bangladesh_phone(step_data["contact_phone"])
        if "owner_full_name" in step_data:
            session.owner_full_name = str(step_data["owner_full_name"]).strip()
        if "branch_name" in step_data:
            session.branch_name = str(step_data["branch_name"]).strip()
        if "branch_address" in step_data:
            session.branch_address = str(step_data["branch_address"]).strip()
        if "initial_staff_data" in step_data:
            session.initial_staff_data = step_data["initial_staff_data"] or []
        if "initial_inventory_mode" in step_data:
            session.initial_inventory_mode = step_data["initial_inventory_mode"]
        if "initial_inventory_data" in step_data:
            session.initial_inventory_data = step_data["initial_inventory_data"] or []
        if "plan_tier" in step_data:
            tier = str(step_data["plan_tier"]).upper().strip()
            if tier in PlanTier.values:
                session.plan_tier = tier
        if "billing_cycle" in step_data:
            session.billing_cycle = str(step_data["billing_cycle"]).upper().strip()
        if "is_trial" in step_data:
            session.is_trial = bool(step_data["is_trial"])
        if "payment_method" in step_data:
            session.payment_method = str(step_data["payment_method"]).upper().strip()

        session.current_step = max(session.current_step, step)
        session.status = OnboardingWizardStatus.IN_PROGRESS
        session.save()
        return session

    @classmethod
    def complete_onboarding(
        cls,
        session: TenantOnboardingSession,
        owner_password: str,
        operator: Optional[Any] = None,
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Atomically completes the onboarding wizard and provisions all tenant artifacts:
          - Tenant
          - Branch
          - Owner PharmacyUser
          - Initial StaffMembers
          - Initial Inventory (BatchStock)
          - SubscriptionRecord
          - AuditLogEntry
        """
        if session.status == OnboardingWizardStatus.COMPLETED:
            raise ValidationError("This onboarding session has already been completed and provisioned.")
        if session.status == OnboardingWizardStatus.ABANDONED:
            raise ValidationError("Cannot complete an abandoned onboarding session.")

        if not owner_password or len(owner_password) < 8:
            raise ValidationError({"owner_password": ["Owner password must be at least 8 characters long."]})

        if not session.drug_license_number:
            raise ValidationError(
                {"drug_license_number": ["Drug license number is required before finalizing onboarding."]}
            )

        # Validate unique subdomain
        clean_subdomain = session.subdomain.strip().lower()
        if Tenant.objects.filter(subdomain=clean_subdomain).exists():
            raise ValidationError({"subdomain": [f"Subdomain '{clean_subdomain}' is already assigned to a tenant."]})

        # Validate owner email uniqueness
        if PharmacyUser.objects.filter(email=session.contact_email).exists():
            raise ValidationError(
                {"contact_email": [f"User with email '{session.contact_email}' already exists on the platform."]}
            )

        # Validate owner phone uniqueness
        if PharmacyUser.objects.filter(phone_number=session.contact_phone).exists():
            raise ValidationError(
                {"contact_phone": [f"User with phone '{session.contact_phone}' already exists on the platform."]}
            )

        now = timezone.now()
        clean_tenant_id = f"tn-{clean_subdomain}"

        # Calculate quotas and amount
        plan_tier = session.plan_tier
        branch_quota = 1 if plan_tier == PlanTier.BASIC else (5 if plan_tier == PlanTier.STANDARD else 25)
        device_quota = 2 if plan_tier == PlanTier.BASIC else (10 if plan_tier == PlanTier.STANDARD else 50)
        amount_bdt = (
            Decimal("0.00")
            if session.is_trial
            else (
                Decimal("2500.00")
                if plan_tier == PlanTier.BASIC
                else (Decimal("6500.00") if plan_tier == PlanTier.STANDARD else Decimal("15000.00"))
            )
        )

        operator_id = str(getattr(operator, "user_id", None) or operator or "platform-admin")
        operator_email = getattr(operator, "email", None) or "admin@platform.local"

        with transaction.atomic():
            # 1. Create Tenant
            tenant = Tenant.objects.create(
                tenant_id=clean_tenant_id,
                name=session.pharmacy_name,
                subdomain=clean_subdomain,
                plan_tier=plan_tier,
                status=TenantLifecycleStatus.TRIAL if session.is_trial else TenantLifecycleStatus.ACTIVE,
                billing_email=session.contact_email,
                trial_ends_at=(now + timedelta(days=14)) if session.is_trial else None,
                canonical_settings={
                    "trade_license": session.trade_license_number,
                    "drug_license": session.drug_license_number,
                    "tin": session.tin_number,
                    "initial_inventory_mode": session.initial_inventory_mode,
                    "onboarding_session_id": session.session_id,
                },
                is_active=True,
            )

            # 2. Create Default Branch
            default_branch_id = f"{clean_tenant_id}-br-main"
            branch = Branch.objects.create(
                branch_id=default_branch_id,
                tenant=tenant,
                name=session.branch_name or f"{session.pharmacy_name} - Main Branch",
                license_number=session.drug_license_number,
                address=session.branch_address or "Dhaka, Bangladesh",
                is_active=True,
            )

            # 3. Create First Owner User
            owner_user = PharmacyUser(
                user_id=str(uuid.uuid4()),
                tenant_id=tenant.tenant_id,
                branch_id=branch.branch_id,
                email=session.contact_email,
                phone_number=session.contact_phone,
                full_name=session.owner_full_name,
                role=UserRole.PHARMACY_OWNER,
                activation_status=ActivationStatus.ACTIVE,
                is_active=True,
            )
            owner_user.set_password(owner_password)
            owner_user.save()

            tenant.owner_user_id = str(owner_user.user_id)
            tenant.save(update_fields=["owner_user_id"])

            # 4. Provision Initial Staff Members
            created_staff_count = 0
            if session.initial_staff_data:
                for idx, staff_item in enumerate(session.initial_staff_data):
                    full_name = str(staff_item.get("full_name") or f"Staff Member {idx+1}").strip()
                    role = str(staff_item.get("role", "PHARMACIST")).upper().strip()
                    raw_phone = staff_item.get("mobile_number") or session.contact_phone
                    try:
                        valid_phone = normalize_bangladesh_phone(raw_phone)
                    except ValidationError:
                        valid_phone = session.contact_phone

                    pcb_no = str(staff_item.get("pcb_registration_no", "")).strip()

                    StaffMember.objects.create(
                        staff_id=f"stf-{uuid.uuid4().hex[:8]}",
                        tenant=tenant,
                        branch=branch,
                        username=f"{role.lower()}_{uuid.uuid4().hex[:4]}",
                        full_name=full_name,
                        role=role,
                        mobile_number=valid_phone,
                        pcb_registration_no=pcb_no,
                        is_active=True,
                    )
                    created_staff_count += 1

            # 5. Provision Initial Inventory
            seeded_batch_count = 0
            if session.initial_inventory_mode == InitialInventoryMode.SEED_BASELINE:
                # Find available medicines or ensure essential baseline
                medicines = list(MedicineProduct.objects.all()[:3])
                if not medicines:
                    p1 = MedicineProduct.objects.create(
                        product_id=f"med-{uuid.uuid4().hex[:8]}",
                        brand_name="Napa 500mg",
                        generic_name="Paracetamol",
                        dosage_form="Tablet",
                        strength="500mg",
                        manufacturer="Beximco Pharmaceuticals Ltd",
                        mrp_unit=Decimal("1.20"),
                        cost_unit=Decimal("0.85"),
                        dgda_dar_number="DGDA-DAR-00124",
                        is_prescription_only=False,
                    )
                    medicines = [p1]

                for med in medicines:
                    BatchStock.objects.create(
                        batch_id=f"bat-{tenant.tenant_id}-{uuid.uuid4().hex[:6]}",
                        tenant=tenant,
                        branch=branch,
                        product=med,
                        batch_number=f"INIT-{med.brand_name[:3].upper()}-01",
                        quantity_base_units=250,
                        expiry_date=(now + timedelta(days=540)).date(),
                        purchase_date=now.date(),
                        cost_per_unit=med.cost_unit or Decimal("1.00"),
                        status=BatchStatus.ACTIVE,
                    )
                    seeded_batch_count += 1

            # 6. Create Subscription Record
            subscription = SubscriptionRecord.objects.create(
                subscription_id=f"sub-{clean_tenant_id}-{uuid.uuid4().hex[:6]}",
                tenant=tenant,
                plan_tier=plan_tier,
                status="TRIAL" if session.is_trial else "ACTIVE",
                amount_bdt=amount_bdt,
                billing_cycle=session.billing_cycle,
                licensed_branches=branch_quota,
                licensed_devices=device_quota,
                features_enabled=[
                    "PRESCRIPTION_DISPENSING",
                    "FEFO_BATCH_INVENTORY",
                    "DGDA_ANTIBIOTIC_REGISTER",
                    "OFFLINE_POS_SYNC",
                ],
                start_date=now.date(),
                renewal_date=(now + timedelta(days=365)).date(),
            )

            # 7. Record Immutable Audit Log
            AuditLogEntry.objects.create(
                action="TENANT_ONBOARDING_COMPLETED",
                resource_type="TENANT",
                resource_id=tenant.tenant_id,
                tenant_id=tenant.tenant_id,
                user_id=operator_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "event": "TENANT_ONBOARDING_WIZARD_COMPLETED",
                    "session_id": session.session_id,
                    "tenant_id": tenant.tenant_id,
                    "subdomain": tenant.subdomain,
                    "plan_tier": plan_tier,
                    "owner_email": session.contact_email,
                    "drug_license": session.drug_license_number,
                    "initial_staff_count": created_staff_count,
                    "seeded_batch_count": seeded_batch_count,
                    "subscription_id": subscription.subscription_id,
                    "is_trial": session.is_trial,
                    "amount_bdt": str(amount_bdt),
                    "operator": operator_email,
                    "notes": notes,
                    "timestamp": now.isoformat(),
                },
            )

            # 8. Mark Session as Completed
            session.status = OnboardingWizardStatus.COMPLETED
            session.tenant = tenant
            session.completed_at = now
            session.save(update_fields=["status", "tenant", "completed_at"])

        return {
            "session_id": session.session_id,
            "tenant_id": tenant.tenant_id,
            "name": tenant.name,
            "subdomain": tenant.subdomain,
            "owner_user_id": str(owner_user.user_id),
            "owner_email": owner_user.email,
            "owner_phone": owner_user.phone_number,
            "default_branch_id": branch.branch_id,
            "drug_license_number": session.drug_license_number,
            "initial_staff_count": created_staff_count,
            "seeded_batch_count": seeded_batch_count,
            "subscription_id": subscription.subscription_id,
            "plan_tier": tenant.plan_tier,
            "status": tenant.status,
            "licensed_branches": subscription.licensed_branches,
            "licensed_devices": subscription.licensed_devices,
            "amount_bdt": str(amount_bdt),
            "completed_at": session.completed_at.isoformat(),
        }

    @classmethod
    def abandon_session(
        cls,
        session: TenantOnboardingSession,
        operator: Optional[Any] = None,
        reason: str = "",
    ) -> TenantOnboardingSession:
        """
        Marks an onboarding session as abandoned.
        """
        if session.status == OnboardingWizardStatus.COMPLETED:
            raise ValidationError("Cannot abandon an already completed and provisioned onboarding session.")

        session.status = OnboardingWizardStatus.ABANDONED
        session.save(update_fields=["status"])

        operator_id = str(getattr(operator, "user_id", None) or operator or "platform-admin")
        AuditLogEntry.objects.create(
            action="TENANT_ONBOARDING_ABANDONED",
            resource_type="ONBOARDING_SESSION",
            resource_id=session.session_id,
            tenant_id="platform",
            user_id=operator_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "session_id": session.session_id,
                "pharmacy_name": session.pharmacy_name,
                "reason": reason or "Cancelled by operator",
                "timestamp": timezone.now().isoformat(),
            },
        )
        return session
