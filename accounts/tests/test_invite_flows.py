"""
Invitation flow tests — covers the full invite → preview → accept cycle
including doctor profile auto-creation, cookie issuance, and all rejection
cases (expired, double-accept, revoked).
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Invitation, User
from accounts.services import create_invitation
from doctors.models import Doctor

pytestmark = pytest.mark.django_db


INVITES_URL = "/api/v1/accounts/invites/"


def _preview_url(token: str) -> str:
    return f"/api/v1/accounts/invites/preview/{token}/"


def _accept_url(token: str) -> str:
    return f"/api/v1/accounts/invites/accept/{token}/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_doctor_invite(clinic, admin) -> str:
    """Create a doctor invitation via the service and return the signed token."""
    return create_invitation(
        clinic=clinic,
        invited_by=admin,
        email="invited.doctor@example.com",
        role=User.Role.DOCTOR,
    )


def _create_receptionist_invite(clinic, admin) -> str:
    return create_invitation(
        clinic=clinic,
        invited_by=admin,
        email="invited.receptionist@example.com",
        role=User.Role.RECEPTIONIST,
    )


# ---------------------------------------------------------------------------
# Invite creation (API)
# ---------------------------------------------------------------------------

class TestInviteCreation:
    def test_admin_can_invite_doctor(self, admin_client, clinic):
        res = admin_client.post(INVITES_URL, {
            "email": "new.doctor@example.com",
            "role": "doctor",
        }, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        assert "token" in res.data

    def test_admin_can_invite_receptionist(self, admin_client):
        res = admin_client.post(INVITES_URL, {
            "email": "new.reception@example.com",
            "role": "receptionist",
        }, format="json")
        assert res.status_code == status.HTTP_201_CREATED

    def test_admin_cannot_invite_another_admin(self, admin_client):
        """InviteCreateSerializer only allows doctor and receptionist roles."""
        res = admin_client.post(INVITES_URL, {
            "email": "second.admin@example.com",
            "role": "clinic_admin",
        }, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST

    def test_doctor_cannot_send_invite(self, doctor_client):
        res = doctor_client.post(INVITES_URL, {
            "email": "doc@example.com",
            "role": "doctor",
        }, format="json")
        assert res.status_code == status.HTTP_403_FORBIDDEN

    def test_receptionist_cannot_send_invite(self, receptionist_client):
        res = receptionist_client.post(INVITES_URL, {
            "email": "doc@example.com",
            "role": "doctor",
        }, format="json")
        assert res.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# Preview endpoint
# ---------------------------------------------------------------------------

class TestInvitePreview:
    def test_preview_returns_clinic_name_and_role(self, clinic, clinic_admin):
        signed_token = _create_doctor_invite(clinic, clinic_admin)
        client = APIClient()
        res = client.get(_preview_url(signed_token))
        assert res.status_code == status.HTTP_200_OK
        assert res.data["clinic_name"] == clinic.name
        assert res.data["role"] == User.Role.DOCTOR
        assert res.data["email"] == "invited.doctor@example.com"

    def test_preview_invalid_token_returns_400(self):
        client = APIClient()
        res = client.get(_preview_url("totally.invalid.token"))
        assert res.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Accept endpoint
# ---------------------------------------------------------------------------

class TestInviteAccept:
    _ACCEPT_PAYLOAD = {
        "full_name": "Accepted User",
        "password": "AcceptPass123!",
    }

    def test_accept_creates_user_with_correct_role(self, clinic, clinic_admin):
        signed_token = _create_doctor_invite(clinic, clinic_admin)
        client = APIClient()
        res = client.post(_accept_url(signed_token), self._ACCEPT_PAYLOAD, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        user = User.objects.get(email="invited.doctor@example.com")
        assert user.role == User.Role.DOCTOR
        assert user.clinic == clinic

    def test_accept_doctor_invitation_creates_doctor_profile(self, clinic, clinic_admin):
        signed_token = _create_doctor_invite(clinic, clinic_admin)
        client = APIClient()
        client.post(_accept_url(signed_token), self._ACCEPT_PAYLOAD, format="json")
        user = User.objects.get(email="invited.doctor@example.com")
        assert Doctor.objects.filter(user=user).exists()

    def test_accept_receptionist_invitation_does_not_create_doctor_profile(self, clinic, clinic_admin):
        signed_token = _create_receptionist_invite(clinic, clinic_admin)
        client = APIClient()
        client.post(_accept_url(signed_token), self._ACCEPT_PAYLOAD, format="json")
        user = User.objects.get(email="invited.receptionist@example.com")
        assert not Doctor.objects.filter(user=user).exists()

    def test_accept_sets_auth_cookies(self, clinic, clinic_admin):
        from django.conf import settings as django_settings
        signed_token = _create_doctor_invite(clinic, clinic_admin)
        client = APIClient()
        res = client.post(_accept_url(signed_token), self._ACCEPT_PAYLOAD, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        assert django_settings.AUTH_COOKIE_ACCESS in res.cookies
        assert django_settings.AUTH_COOKIE_REFRESH in res.cookies

    def test_accept_marks_user_as_verified(self, clinic, clinic_admin):
        """Users who accept an invitation skip email verification."""
        signed_token = _create_doctor_invite(clinic, clinic_admin)
        client = APIClient()
        client.post(_accept_url(signed_token), self._ACCEPT_PAYLOAD, format="json")
        user = User.objects.get(email="invited.doctor@example.com")
        assert user.is_verified

    def test_double_accept_rejected(self, clinic, clinic_admin):
        signed_token = _create_doctor_invite(clinic, clinic_admin)
        client = APIClient()
        client.post(_accept_url(signed_token), self._ACCEPT_PAYLOAD, format="json")
        res = client.post(_accept_url(signed_token), {
            "full_name": "Second Accept",
            "password": "AcceptPass456!",
        }, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST

    def test_invalid_token_rejected(self):
        client = APIClient()
        res = client.post(_accept_url("invalid.token"), self._ACCEPT_PAYLOAD, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST

    def test_revoked_invitation_rejected(self, clinic, clinic_admin):
        signed_token = _create_doctor_invite(clinic, clinic_admin)
        # Revoke the invitation directly on the model
        invite = Invitation.objects.get(email="invited.doctor@example.com")
        invite.revoked_at = timezone.now()
        invite.save(update_fields=["revoked_at"])

        client = APIClient()
        res = client.post(_accept_url(signed_token), self._ACCEPT_PAYLOAD, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST

    def test_expired_invitation_rejected(self, clinic, clinic_admin):
        signed_token = _create_doctor_invite(clinic, clinic_admin)
        # Backdate the expiry so the invite is already expired
        invite = Invitation.objects.get(email="invited.doctor@example.com")
        invite.expires_at = timezone.now() - timedelta(days=1)
        invite.save(update_fields=["expires_at"])

        client = APIClient()
        res = client.post(_accept_url(signed_token), self._ACCEPT_PAYLOAD, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST
