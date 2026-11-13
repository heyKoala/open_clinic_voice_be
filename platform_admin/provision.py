"""Creating a hospital from the platform admin: the only way a hospital comes into being (there is no public signup).

One transaction creates everything the hospital needs, so a failure part-way leaves nothing behind:
the hospital and its configuration, its plan and seat limits, its credit account (and opening credits), its first
admin (when a password is set here), its logo and the audit entry. An invite email, being outside our control, is
sent only after that succeeded; if it fails the hospital still exists and the invite can be re-sent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers

from accounts.models import User
from accounts.services import create_invitation
from clinics.logos import save_logo
from clinics.models import Clinic, ClinicConfiguration
from doctors.models import Doctor
from platform_admin import credits
from platform_admin.audit import log_platform_action
from platform_admin.models import CreditAccount, PlatformDefaults
from subscriptions.models import ClinicEntitlement, SubscriptionPlan

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Asia/Kolkata"


@dataclass
class Provisioned:
    clinic: Clinic
    invite_sent: bool | None  # None when no invite was needed (a password was set instead)


def _create_admin_user(clinic: Clinic, admin: dict) -> User:
    try:
        user = User.objects.create_user(
            clinic=clinic,
            email=admin["email"],
            full_name=admin["full_name"],
            password=admin["password"],
            role=User.Role.CLINIC_ADMIN,
            is_clinic_admin=True,
            is_doctor=admin["is_also_doctor"],
            membership_status=User.MembershipStatus.ACTIVE,
            # HeyKoala vouches for this person, so there is no email to verify.
            is_verified=True,
            email_verified_at=timezone.now(),
            must_change_password=admin["must_change_password"],
        )
    except IntegrityError:
        raise serializers.ValidationError({"admin": ["This email already has an account."]})
    if admin["is_also_doctor"]:
        Doctor.objects.create(clinic=clinic, user=user, full_name=admin["full_name"])
    return user


def provision_hospital(*, actor, data: dict, logo=None) -> Provisioned:
    """Create a hospital from validated data (see ``NewHospitalSerializer``). Raises a ValidationError on bad input."""
    defaults = PlatformDefaults.load()
    admin = data["admin"]
    commercials = data["commercials"]
    plan = SubscriptionPlan.get_default_plan(commercials["plan"])
    stored_file = None

    try:
        with transaction.atomic():
            try:
                clinic = Clinic.objects.create(
                    name=data["name"],
                    address=data.get("address", ""),
                    phone=data.get("phone", ""),
                    support_email=data.get("support_email", ""),
                    subscription_status=Clinic.SubscriptionStatus.ACTIVE,
                    # The platform sets up the number and the AI receptionist, so the hospital's admin lands in the app
                    # rather than in the self-serve setup wizard.
                    is_onboarded=True,
                )
            except IntegrityError:  # two people creating the same name at the same moment
                raise serializers.ValidationError({"name": ["A hospital with this name already exists."]})

            ClinicConfiguration.objects.create(clinic=clinic, timezone=DEFAULT_TIMEZONE)

            ClinicEntitlement.objects.create(
                clinic=clinic,
                plan=plan,
                credits_per_minute_override=(
                    None if commercials["credits_per_minute"] == plan.credits_per_minute else commercials["credits_per_minute"]
                ),
                # At least one admin seat, or the first admin could not be invited.
                max_admins_override=max(defaults.max_admins, 1),
                max_doctors_override=defaults.max_doctors,
                max_receptionists_override=defaults.max_receptionists,
            )

            account = CreditAccount.objects.create(clinic=clinic, low_balance_threshold=commercials["low_balance_threshold"])
            if commercials["opening_credits"] > 0:
                credits.add_credits(account, commercials["opening_credits"], note="Opening credits", actor=actor)

            if admin["mode"] == "password":
                _create_admin_user(clinic, admin)

            if logo is not None:
                save_logo(clinic, logo)
                stored_file = clinic.logo_file.name
                clinic.save(update_fields=["logo_file", "logo_url", "updated_at"])

            log_platform_action(actor, "create_hospital", clinic, {
                "admin_email": admin["email"],
                "admin_mode": admin["mode"],
                "plan": commercials["plan"],
                "credits_per_minute": commercials["credits_per_minute"],
                "opening_credits": commercials["opening_credits"],
                "logo": bool(logo),
            })
    except Exception:
        # The database rolled back; a logo file already written would be left behind, so remove it.
        if stored_file:
            try:
                Clinic._meta.get_field("logo_file").storage.delete(stored_file)
            except Exception:
                logger.warning("Could not remove the orphaned logo %s", stored_file)
        raise

    invite_sent = None
    if admin["mode"] == "invite":
        # Outside the transaction on purpose: a mail failure must not undo a hospital that was created correctly.
        try:
            create_invitation(
                clinic=clinic, invited_by=actor, email=admin["email"], role=User.Role.CLINIC_ADMIN,
                is_also_doctor=admin["is_also_doctor"],
            )
            invite_sent = True
        except Exception:
            logger.exception("The invite email for the new hospital %s could not be sent", clinic.id)
            invite_sent = False
    return Provisioned(clinic=clinic, invite_sent=invite_sent)
