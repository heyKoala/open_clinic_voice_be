"""
Authentication flow tests — covers the full signup → verify → login → logout
cycle plus edge cases: lockout, token refresh, enumeration-safe password reset.

These tests exercise the API layer end-to-end using the locmem email backend
so no real emails are sent and we can inspect django.core.mail.outbox.
"""
from __future__ import annotations

import re

import pytest
from django.conf import settings
from django.core import mail
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from accounts.services import _create_email_verification

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# URL constants
# ---------------------------------------------------------------------------

SIGNUP_URL = "/api/v1/accounts/signup/"
VERIFY_EMAIL_URL = "/api/v1/accounts/verify-email/"
LOGIN_URL = "/api/v1/accounts/login/"
LOGOUT_URL = "/api/v1/accounts/logout/"
REFRESH_URL = "/api/v1/accounts/refresh/"
ME_URL = "/api/v1/accounts/me/"
TEAM_URL = "/api/v1/accounts/access/"
FORGOT_PASSWORD_URL = "/api/v1/accounts/forgot-password/"
RESET_PASSWORD_URL = "/api/v1/accounts/reset-password/"

_VALID_SIGNUP = {
    "clinic_name": "Flow Clinic",
    "full_name": "Flow Admin",
    "email": "flow.admin@example.com",
    "password": "FlowPass123!",
}


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------

class TestSignup:
    def test_creates_clinic_admin_role(self, api_client):
        res = api_client.post(SIGNUP_URL, _VALID_SIGNUP, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        user = User.objects.get(email=_VALID_SIGNUP["email"])
        assert user.role == User.Role.CLINIC_ADMIN

    def test_new_user_is_unverified(self, api_client):
        api_client.post(SIGNUP_URL, _VALID_SIGNUP, format="json")
        user = User.objects.get(email=_VALID_SIGNUP["email"])
        assert not user.is_verified

    def test_sends_verification_email(self, api_client):
        api_client.post(SIGNUP_URL, _VALID_SIGNUP, format="json")
        assert len(mail.outbox) == 1
        assert _VALID_SIGNUP["email"] in mail.outbox[0].to

    def test_duplicate_email_rejected(self, api_client):
        api_client.post(SIGNUP_URL, _VALID_SIGNUP, format="json")
        res = api_client.post(SIGNUP_URL, _VALID_SIGNUP, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST

    def test_weak_password_rejected(self, api_client):
        payload = {**_VALID_SIGNUP, "email": "weak@example.com", "password": "short"}
        res = api_client.post(SIGNUP_URL, payload, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST

    def test_missing_clinic_name_rejected(self, api_client):
        payload = {k: v for k, v in _VALID_SIGNUP.items() if k != "clinic_name"}
        res = api_client.post(SIGNUP_URL, payload, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

class TestEmailVerification:
    def _make_unverified_user(self, clinic):
        """Create an unverified user directly via the service layer."""
        return User.objects.create_user(
            clinic=clinic,
            email="unverified@example.com",
            full_name="Unverified",
            password="UnverifiedPass123!",
            role=User.Role.CLINIC_ADMIN,
            is_verified=False,
        )

    def test_valid_token_marks_verified(self, api_client, clinic):
        user = self._make_unverified_user(clinic)
        signed_token = _create_email_verification(user)
        res = api_client.post(VERIFY_EMAIL_URL, {"token": signed_token}, format="json")
        assert res.status_code == status.HTTP_200_OK
        user.refresh_from_db()
        assert user.is_verified

    def test_invalid_token_rejected(self, api_client):
        res = api_client.post(VERIFY_EMAIL_URL, {"token": "bad.token.here"}, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST
        assert "token" in res.data

    def test_token_used_twice_rejected(self, api_client, clinic):
        user = self._make_unverified_user(clinic)
        signed_token = _create_email_verification(user)
        api_client.post(VERIFY_EMAIL_URL, {"token": signed_token}, format="json")
        res = api_client.post(VERIFY_EMAIL_URL, {"token": signed_token}, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Login / session cookies
# ---------------------------------------------------------------------------

class TestLogin:
    def test_login_returns_200_and_sets_cookies(self, clinic_admin):
        client = APIClient()
        res = client.post(LOGIN_URL, {
            "email": clinic_admin.email,
            "password": "AdminPass123!",
        }, format="json")
        assert res.status_code == status.HTTP_200_OK
        assert settings.AUTH_COOKIE_ACCESS in res.cookies
        assert settings.AUTH_COOKIE_REFRESH in res.cookies

    def test_login_access_cookie_is_http_only(self, clinic_admin):
        client = APIClient()
        res = client.post(LOGIN_URL, {
            "email": clinic_admin.email,
            "password": "AdminPass123!",
        }, format="json")
        assert res.cookies[settings.AUTH_COOKIE_ACCESS]["httponly"]

    def test_wrong_password_returns_401(self, clinic_admin):
        client = APIClient()
        res = client.post(LOGIN_URL, {
            "email": clinic_admin.email,
            "password": "WrongPassword!",
        }, format="json")
        assert res.status_code == status.HTTP_401_UNAUTHORIZED

    def test_nonexistent_email_returns_401(self, api_client):
        res = api_client.post(LOGIN_URL, {
            "email": "nobody@nowhere.com",
            "password": "AnyPassword123!",
        }, format="json")
        assert res.status_code == status.HTTP_401_UNAUTHORIZED

    def test_account_locked_after_5_failed_attempts(self, clinic_admin):
        """After 5 wrong-password attempts the account is temporarily locked."""
        client = APIClient()
        bad_creds = {"email": clinic_admin.email, "password": "WrongPass!"}
        for _ in range(5):
            client.post(LOGIN_URL, bad_creds, format="json")
        res = client.post(LOGIN_URL, bad_creds, format="json")
        assert res.status_code == status.HTTP_401_UNAUTHORIZED
        assert "locked" in res.data.get("detail", "").lower()

    def test_unverified_user_can_login_but_protected_endpoints_return_403(self, clinic):
        """Unverified users can get tokens but are blocked by AuthenticatedAndVerified (e.g. on TeamView)."""
        unverified = User.objects.create_user(
            clinic=clinic,
            email="unverified.login@example.com",
            full_name="Unverified",
            password="UnverifiedPass123!",
            role=User.Role.CLINIC_ADMIN,
            is_verified=False,
        )
        client = APIClient()
        login_res = client.post(LOGIN_URL, {
            "email": unverified.email,
            "password": "UnverifiedPass123!",
        }, format="json")
        assert login_res.status_code == status.HTTP_200_OK

        # Now try to access a protected endpoint
        team_res = client.get(TEAM_URL)
        assert team_res.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

class TestTokenRefresh:
    def test_refresh_returns_200_and_new_cookies(self, clinic_admin):
        client = APIClient()
        # Login to get initial tokens
        client.post(LOGIN_URL, {
            "email": clinic_admin.email,
            "password": "AdminPass123!",
        }, format="json")
        # Refresh — client carries the cookies automatically in the test session
        res = client.post(REFRESH_URL)
        assert res.status_code == status.HTTP_200_OK
        assert settings.AUTH_COOKIE_ACCESS in res.cookies

    def test_refresh_without_cookie_returns_401(self, api_client):
        res = api_client.post(REFRESH_URL)
        assert res.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

class TestLogout:
    def test_logout_clears_cookies(self, clinic_admin):
        client = APIClient()
        client.post(LOGIN_URL, {
            "email": clinic_admin.email,
            "password": "AdminPass123!",
        }, format="json")
        client.force_authenticate(user=clinic_admin)  # for the logout endpoint
        res = client.post(LOGOUT_URL)
        assert res.status_code == status.HTTP_200_OK
        # After logout the refresh cookie should be deleted (max-age=0 or empty value)
        refresh_cookie = res.cookies.get(settings.AUTH_COOKIE_REFRESH)
        if refresh_cookie:
            assert refresh_cookie["max-age"] == 0 or refresh_cookie.value == ""


# ---------------------------------------------------------------------------
# /me — profile endpoint
# ---------------------------------------------------------------------------

class TestMeEndpoint:
    def test_me_returns_correct_user(self, admin_client, clinic_admin):
        res = admin_client.get(ME_URL)
        assert res.status_code == status.HTTP_200_OK
        assert res.data["email"] == clinic_admin.email
        assert res.data["role"] == User.Role.CLINIC_ADMIN

    def test_me_unauthenticated_returns_401(self, api_client):
        # DRF's CookieJWTAuthentication raises AuthenticationFailed (401) when no
        # credentials are present. 403 would require the user to be authenticated
        # but lack permission — an anonymous request is 401, not 403.
        res = api_client.get(ME_URL)
        assert res.status_code == status.HTTP_401_UNAUTHORIZED

    def test_me_patch_updates_full_name(self, admin_client, clinic_admin):
        res = admin_client.patch(ME_URL, {"full_name": "Updated Name"}, format="json")
        assert res.status_code == status.HTTP_200_OK
        clinic_admin.refresh_from_db()
        assert clinic_admin.full_name == "Updated Name"

    def test_me_patch_role_escalation_denied(self, doctor_client, doctor_user):
        res = doctor_client.patch(ME_URL, {"role": "clinic_admin"}, format="json")
        assert res.status_code == status.HTTP_403_FORBIDDEN
        doctor_user.refresh_from_db()
        assert doctor_user.role == User.Role.DOCTOR


# ---------------------------------------------------------------------------
# Forgot / reset password
# ---------------------------------------------------------------------------

class TestPasswordReset:
    def test_forgot_password_always_returns_200(self, api_client):
        """No user enumeration — returns 200 even for unknown email."""
        res = api_client.post(FORGOT_PASSWORD_URL, {
            "email": "nobody@example.com",
        }, format="json")
        assert res.status_code == status.HTTP_200_OK

    def test_forgot_password_sends_email_for_known_user(self, clinic_admin):
        client = APIClient()
        client.post(FORGOT_PASSWORD_URL, {"email": clinic_admin.email}, format="json")
        assert len(mail.outbox) == 1
        assert clinic_admin.email in mail.outbox[0].to

    def test_reset_password_full_cycle(self, clinic_admin):
        """Forgot → extract token from email → reset → login with new password."""
        client = APIClient()
        client.post(FORGOT_PASSWORD_URL, {"email": clinic_admin.email}, format="json")
        assert len(mail.outbox) == 1

        # Extract the signed token from the reset URL in the email body
        email_body = mail.outbox[0].body
        match = re.search(r"token=([^\s]+)", email_body)
        assert match, "Reset token not found in email body"
        signed_token = match.group(1)

        new_password = "NewSecurePass456!"
        res = client.post(RESET_PASSWORD_URL, {
            "token": signed_token,
            "password": new_password,
        }, format="json")
        assert res.status_code == status.HTTP_200_OK

        # Login with new password
        login_res = client.post(LOGIN_URL, {
            "email": clinic_admin.email,
            "password": new_password,
        }, format="json")
        assert login_res.status_code == status.HTTP_200_OK

    def test_reset_with_invalid_token_returns_400(self, api_client):
        res = api_client.post(RESET_PASSWORD_URL, {
            "token": "bad.token",
            "password": "NewSecurePass456!",
        }, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST
        assert "token" in res.data
