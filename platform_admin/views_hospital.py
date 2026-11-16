"""The platform admin's tools for one hospital: its people, centres, phone number and integrations.

Phone numbers and integrations reuse the hospital app's own views (``clinics.views``): the platform view fixes the
hospital from the URL and hands the request over, so there is one set of rules (and one set of tests) for both.
Everything the platform changes is written to the audit log (never a password, key or secret).
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from accounts.exceptions import SeatLimitReachedError
from accounts.models import Invitation, User
from accounts.serializers import _enforce_strong_password
from accounts.services import create_invitation, get_or_create_clinic_entitlement, send_password_reset_email
from clinics import views as hospital_views
from clinics.models import Clinic, ClinicConfiguration
from doctors.models import Doctor
from patients.models import Patient
from platform_admin import presenters
from platform_admin.audit import log_platform_action
from platform_admin.permissions import PlatformAPIView
from platform_admin.phone import link_existing_number
from platform_admin.views import get_hospital

logger = logging.getLogger(__name__)

ROLE_NAME = {"clinic_admin": "hospital admins", "doctor": "doctors", "receptionist": "receptionists"}


# ── people ────────────────────────────────────────────────────────────────────

def user_status(user: User) -> str:
    if user.membership_status == User.MembershipStatus.ARCHIVED:
        return "archived"
    if not user.is_active or user.membership_status == User.MembershipStatus.SUSPENDED:
        return "suspended"
    if user.membership_status == User.MembershipStatus.INVITED:
        return "invited"
    return "active"


def user_row(user: User) -> dict:
    return {"id": user.id, "full_name": user.full_name, "email": user.email, "role": user.role, "status": user_status(user), "is_active": user.is_active}


def invitation_row(invitation: Invitation) -> dict:
    # Someone invited but not yet signed up has no account, only an invitation. Its id is sent negative so it can never
    # be mistaken for a user's id (the screens use the id to resend or cancel the invite).
    return {"id": -invitation.id, "full_name": "", "email": invitation.email, "role": invitation.role, "status": "invited", "is_active": True}


def usable_invitations(root: Clinic):
    return Invitation.objects.filter(
        clinic__in=root.group_clinics(), used_at__isnull=True, revoked_at__isnull=True, expires_at__gte=timezone.now()
    ).order_by("id")


def end_sessions(user: User) -> None:
    """Sign the person out everywhere: their refresh tokens stop working (an access token lasts at most minutes)."""
    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)


def find_person(root: Clinic, ref: int):
    """``ref`` > 0 is a user of this hospital; ``ref`` < 0 is an unused invitation. Returns (user, invitation)."""
    if ref < 0:
        return None, usable_invitations(root).filter(pk=-ref).first()
    return User.objects.filter(pk=ref, clinic__in=root.group_clinics()).first(), None


def not_found() -> Response:
    return Response({"detail": "No such person in this hospital."}, status=status.HTTP_404_NOT_FOUND)


class NewUserSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=[User.Role.CLINIC_ADMIN, User.Role.DOCTOR, User.Role.RECEPTIONIST])
    is_also_doctor = serializers.BooleanField(required=False, default=False)


class UserChangeSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=255, required=False)
    status = serializers.ChoiceField(choices=["active", "suspended"], required=False)

    def validate_full_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Enter a name.")
        return value.strip()


class SetPasswordSerializer(serializers.Serializer):
    password = serializers.CharField(trim_whitespace=False)
    must_change_password = serializers.BooleanField(required=False, default=True)

    def validate_password(self, value):
        _enforce_strong_password(value)
        return value


def seat_message(role: str, limit: int) -> str:
    return f"The plan allows {limit} {ROLE_NAME.get(role, role)}. Raise the limit under Plan & limits first."


class HospitalUsersView(PlatformAPIView):
    def get(self, request, pk):
        root = get_hospital(pk)
        users = User.objects.filter(clinic__in=root.group_clinics()).order_by("role", "id")
        rows = [user_row(u) for u in users] + [invitation_row(i) for i in usable_invitations(root)]
        return Response(rows)

    def post(self, request, pk):
        root = get_hospital(pk)
        serializer = NewUserSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        email = data["email"].strip().lower()

        if User.objects.filter(email__iexact=email).exists():
            return Response({"email": ["This email already has an account."]}, status=status.HTTP_400_BAD_REQUEST)

        sent = True
        try:
            create_invitation(clinic=root, invited_by=request.user, email=email, role=data["role"], is_also_doctor=data["is_also_doctor"])
        except SeatLimitReachedError as exc:
            return Response({"detail": seat_message(data["role"], exc.detail["max_allowed"])}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            # The invitation is saved before the email goes out; if the email fails it can be sent again.
            logger.exception("The invite email to %s could not be sent", email)
            sent = False

        invitation = Invitation.objects.filter(clinic=root, email=email, revoked_at__isnull=True, used_at__isnull=True).order_by("-id").first()
        if invitation is None:
            return Response({"detail": "The invitation could not be created."}, status=status.HTTP_502_BAD_GATEWAY)
        log_platform_action(request.user, "add_user", root, {"email": email, "role": data["role"], "invite_sent": sent})
        return Response({**invitation_row(invitation), "invite_sent": sent}, status=status.HTTP_201_CREATED)


class HospitalUserView(PlatformAPIView):
    def patch(self, request, pk, ref):
        root = get_hospital(pk)
        user, invitation = find_person(root, ref)
        if invitation is not None:
            return Response({"detail": "This person hasn't accepted their invite yet. Cancel or resend the invite instead."}, status=status.HTTP_400_BAD_REQUEST)
        if user is None:
            return not_found()
        serializer = UserChangeSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            changed = {}
            if "full_name" in data and data["full_name"] != user.full_name:
                user.full_name = data["full_name"]
                changed["full_name"] = data["full_name"]
                user.save(update_fields=["full_name", "updated_at"])
            if "status" in data and data["status"] != user_status(user):
                if data["status"] == "suspended":
                    self._suspend(root, user)
                else:
                    seat_error = self._reactivate(root, user)
                    if seat_error:
                        return seat_error
                changed["status"] = data["status"]
            if changed:
                log_platform_action(request.user, "update_user", root, {"email": user.email, **changed})
        return Response(user_row(user))

    @staticmethod
    def _suspend(root: Clinic, user: User) -> None:
        if user.role == User.Role.CLINIC_ADMIN:
            others = User.objects.filter(
                clinic__in=root.group_clinics(), role=User.Role.CLINIC_ADMIN, is_active=True, membership_status=User.MembershipStatus.ACTIVE
            ).exclude(pk=user.pk)
            if not others.exists():
                raise serializers.ValidationError({"detail": "This is the only active hospital admin. Add or reactivate another admin before suspending them."})
        user.is_active = False
        user.membership_status = User.MembershipStatus.SUSPENDED
        user.save(update_fields=["is_active", "membership_status", "updated_at"])
        profile = Doctor.objects.filter(user=user).first()
        if profile:
            profile.is_active = False
            profile.save(update_fields=["is_active", "updated_at"])
        end_sessions(user)

    @staticmethod
    def _reactivate(root: Clinic, user: User):
        seats = presenters.seat_usage(root)[user.role]
        if seats["used"] >= seats["limit"]:
            return Response({"detail": seat_message(user.role, seats["limit"])}, status=status.HTTP_400_BAD_REQUEST)
        user.is_active = True
        user.membership_status = User.MembershipStatus.ACTIVE
        user.save(update_fields=["is_active", "membership_status", "updated_at"])
        profile = Doctor.objects.filter(user=user).first()
        if profile and (user.role == User.Role.DOCTOR or user.is_doctor):
            profile.is_active = True
            profile.save(update_fields=["is_active", "updated_at"])
        return None


class ResendInviteView(PlatformAPIView):
    def post(self, request, pk, ref):
        root = get_hospital(pk)
        user, invitation = find_person(root, ref)
        if user is not None:
            return Response({"detail": "This person has already joined."}, status=status.HTTP_400_BAD_REQUEST)
        if invitation is None:
            return not_found()
        # Cancel the old one first, or it would count a second time against the plan's seat limit.
        invitation.revoked_at = timezone.now()
        invitation.save(update_fields=["revoked_at", "updated_at"])
        try:
            create_invitation(clinic=invitation.clinic, invited_by=request.user, email=invitation.email, role=invitation.role, is_also_doctor=invitation.is_also_doctor)
        except Exception:
            logger.exception("The invite email to %s could not be sent", invitation.email)
            log_platform_action(request.user, "resend_invite", root, {"email": invitation.email, "sent": False})
            return Response({"detail": "A new invite was created, but the email could not be sent. Try again in a moment."}, status=status.HTTP_502_BAD_GATEWAY)
        log_platform_action(request.user, "resend_invite", root, {"email": invitation.email, "sent": True})
        return Response({"sent": True})


class RevokeInviteView(PlatformAPIView):
    def post(self, request, pk, ref):
        root = get_hospital(pk)
        user, invitation = find_person(root, ref)
        if user is not None:
            return Response({"detail": "This person has already joined."}, status=status.HTTP_400_BAD_REQUEST)
        if invitation is None:
            return not_found()
        invitation.revoked_at = timezone.now()
        invitation.save(update_fields=["revoked_at", "updated_at"])
        log_platform_action(request.user, "revoke_invite", root, {"email": invitation.email})
        return Response({})


class ResetPasswordView(PlatformAPIView):
    def post(self, request, pk, ref):
        root = get_hospital(pk)
        user, _ = find_person(root, ref)
        if user is None:
            return not_found()
        try:
            send_password_reset_email(user.email)
        except Exception:
            logger.exception("The password reset email to %s could not be sent", user.email)
            return Response({"detail": "The email could not be sent. Try again in a moment."}, status=status.HTTP_502_BAD_GATEWAY)
        log_platform_action(request.user, "reset_password", root, {"email": user.email})
        return Response({"sent": True})


class SetPasswordView(PlatformAPIView):
    def post(self, request, pk, ref):
        root = get_hospital(pk)
        user, _ = find_person(root, ref)
        if user is None:
            return not_found()
        serializer = SetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            user.set_password(serializer.validated_data["password"])
            user.must_change_password = serializer.validated_data["must_change_password"]
            user.failed_login_attempts = 0
            user.lock_until = None
            user.save(update_fields=["password", "must_change_password", "failed_login_attempts", "lock_until", "updated_at"])
            end_sessions(user)
            # The password itself is never written down anywhere.
            log_platform_action(request.user, "set_password", root, {"email": user.email, "must_change": user.must_change_password})
        return Response({})


# ── centres ───────────────────────────────────────────────────────────────────

def centre_row(centre: Clinic) -> dict:
    return {
        "id": centre.id, "name": centre.name, "address": centre.address, "phone": centre.phone, "is_main": centre.parent_id is None,
        "doctors": Doctor.objects.filter(clinic=centre).count(), "patients": Patient.objects.filter(clinic=centre).count(),
    }


class CentreSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False)
    address = serializers.CharField(allow_blank=True, required=False)
    phone = serializers.CharField(max_length=32, allow_blank=True, required=False)

    def __init__(self, *args, hospital: Clinic, centre: Clinic | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.hospital, self.centre = hospital, centre

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Enter the centre name.")
        # A hospital's name is unique among hospitals; a centre's only within its own hospital.
        scope = None if (self.centre is not None and self.centre.parent_id is None) else self.hospital.id
        taken = Clinic.objects.filter(parent_id=scope, name__iexact=value)
        if self.centre is not None:
            taken = taken.exclude(pk=self.centre.pk)
        if taken.exists():
            raise serializers.ValidationError("A hospital with this name already exists." if scope is None else "This hospital already has a centre with this name.")
        return value


def hospital_centre(root: Clinic, cid: int):
    return Clinic.objects.filter(pk=cid).filter(pk=root.pk).first() or Clinic.objects.filter(pk=cid, parent=root).first()


class HospitalCentresView(PlatformAPIView):
    def get(self, request, pk):
        root = get_hospital(pk)
        return Response([centre_row(c) for c in root.group_clinics()])

    def post(self, request, pk):
        root = get_hospital(pk)
        serializer = CentreSerializer(data=request.data, hospital=root)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if "name" not in data:
            return Response({"name": ["Enter the centre name."]}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            centre = Clinic.objects.create(
                name=data["name"], address=data.get("address", ""), phone=data.get("phone", ""), parent=root,
                subscription_status=Clinic.SubscriptionStatus.ACTIVE, is_onboarded=True,
            )
            ClinicConfiguration.objects.get_or_create(clinic=centre)
            get_or_create_clinic_entitlement(centre)
            # The hospital's admins can switch to a centre only if they belong to it, as when an admin adds one themselves.
            for admin in User.objects.filter(clinic__in=root.group_clinics(), role=User.Role.CLINIC_ADMIN):
                admin.clinics.add(centre)
            log_platform_action(request.user, "add_centre", root, {"centre": centre.name})
        return Response(centre_row(centre), status=status.HTTP_201_CREATED)


class HospitalCentreView(PlatformAPIView):
    def patch(self, request, pk, cid):
        root = get_hospital(pk)
        centre = hospital_centre(root, cid)
        if centre is None:
            return Response({"detail": "No such centre in this hospital."}, status=status.HTTP_404_NOT_FOUND)
        serializer = CentreSerializer(data=request.data, partial=True, hospital=root, centre=centre)
        serializer.is_valid(raise_exception=True)
        changed = {k: v for k, v in serializer.validated_data.items() if getattr(centre, k) != v}
        if changed:
            for field, value in changed.items():
                setattr(centre, field, value)
            centre.save()
            log_platform_action(request.user, "update_centre", root, {"centre": centre.name, "fields": sorted(changed)})
        return Response(centre_row(centre))

    def delete(self, request, pk, cid):
        root = get_hospital(pk)
        centre = hospital_centre(root, cid)
        if centre is None:
            return Response({"detail": "No such centre in this hospital."}, status=status.HTTP_404_NOT_FOUND)
        if centre.parent_id is None:
            return Response({"detail": "The main centre can't be deleted."}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            Clinic.objects.select_for_update().filter(id=centre.id).first()
            blockers = hospital_views._centre_delete_blockers(centre)
            if blockers:
                return Response({"detail": f"{centre.name} can't be deleted because it still has {', '.join(blockers)}."}, status=status.HTTP_400_BAD_REQUEST)
            name = centre.name
            try:
                centre.delete()
            except Exception:
                transaction.set_rollback(True)
                return Response({"detail": f"{name} can't be deleted because other records still depend on it."}, status=status.HTTP_400_BAD_REQUEST)
            log_platform_action(request.user, "delete_centre", root, {"centre": name})
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── phone number and integrations: the hospital app's own views, acting for the hospital in the URL ──

def act_for(request, root: Clinic) -> None:
    """Make the hospital-app view treat this request as made inside ``root`` (it reads ``request.clinic``)."""
    request.clinic = root
    request.all_centres = False
    inner = getattr(request, "_request", None)
    if inner is not None:
        inner.clinic, inner.all_centres = root, False


class HospitalPhoneView(PlatformAPIView):
    """POST buys a number, PUT links one already on the telephone account, DELETE releases (or disconnects) it."""

    def post(self, request, pk):
        root = get_hospital(pk)
        act_for(request, root)
        response = hospital_views.PhoneNumberView().post(request)
        if response.status_code == status.HTTP_201_CREATED:
            log_platform_action(request.user, "buy_phone", root, {"number": response.data["phone_number"]["number"]})
        return response

    def put(self, request, pk):
        root = get_hospital(pk)
        record = link_existing_number(root, request.data.get("number"))
        log_platform_action(request.user, "link_phone", root, {"number": record.number})
        return Response(hospital_views._phone_number_payload(root))

    def delete(self, request, pk):
        root = get_hospital(pk)
        act_for(request, root)
        number = getattr(getattr(root, "phone_number", None), "number", "")
        bought = getattr(getattr(root, "phone_number", None), "bought_here", True)
        response = hospital_views.PhoneNumberView().delete(request)
        if response.status_code == status.HTTP_204_NO_CONTENT:
            log_platform_action(request.user, "release_phone" if bought else "disconnect_phone", root, {"number": number})
        return response


class HospitalPhoneSearchView(PlatformAPIView):
    def get(self, request, pk):
        act_for(request, get_hospital(pk))
        return hospital_views.PhoneNumberSearchView().get(request)


class HospitalPhoneActivateView(PlatformAPIView):
    def post(self, request, pk):
        root = get_hospital(pk)
        act_for(request, root)
        response = hospital_views.PhoneNumberActivateView().post(request)
        log_platform_action(request.user, "activate_phone", root, {"ok": response.status_code == status.HTTP_200_OK})
        return response


class HospitalIntegrationsView(PlatformAPIView):
    def get(self, request, pk):
        act_for(request, get_hospital(pk))
        return hospital_views.IntegrationListView().get(request)


class HospitalIntegrationView(PlatformAPIView):
    def put(self, request, pk, provider):
        root = get_hospital(pk)
        act_for(request, root)
        response = hospital_views.IntegrationDetailView().put(request, provider)
        if response.status_code == status.HTTP_200_OK:
            log_platform_action(request.user, "save_integration", root, {"provider": provider})  # never the key itself
        return response

    def delete(self, request, pk, provider):
        root = get_hospital(pk)
        act_for(request, root)
        response = hospital_views.IntegrationDetailView().delete(request, provider)
        if response.status_code == status.HTTP_204_NO_CONTENT:
            log_platform_action(request.user, "disconnect_integration", root, {"provider": provider})
        return response


class HospitalIntegrationTestView(PlatformAPIView):
    def post(self, request, pk, provider):
        act_for(request, get_hospital(pk))
        return hospital_views.IntegrationTestView().post(request, provider)
