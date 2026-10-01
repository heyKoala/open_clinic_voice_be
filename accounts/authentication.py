from __future__ import annotations

import logging

from django.conf import settings
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, CSRFCheck
from rest_framework_simplejwt.authentication import JWTAuthentication

from clinics.context import bind_active_clinic

logger = logging.getLogger(__name__)


class CookieJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        raw_token = request.COOKIES.get(settings.AUTH_COOKIE_ACCESS)
        if raw_token is None:
            header = self.get_header(request)
            if header is None:
                return None
            raw_token = self.get_raw_token(header)
            if raw_token is None:
                return None
        validated_token = self.get_validated_token(raw_token)
        user = self.get_user(validated_token)

        # Resolve active clinic based on user and header
        bind_active_clinic(request, user)

        if request.method not in ("GET", "HEAD", "OPTIONS", "TRACE"):
            reason = self._enforce_csrf(request)
            if reason:
                raise exceptions.PermissionDenied(f"CSRF Failed: {reason}")

        return (user, validated_token)

    def _enforce_csrf(self, request):
        check = CSRFCheck(lambda req: None)
        check.process_request(request)
        reason = check.process_view(request, None, (), {})
        if reason:
            logger.warning("CSRF failure: %s", reason)
        return reason


class PublicEndpointAuthentication(BaseAuthentication):
    """For public endpoints (login, refresh, signup...): ignores session cookies entirely,
    so a stale or invalid cookie cannot block them, while keeping 401 responses for bad credentials."""

    def authenticate(self, request):
        return None

    def authenticate_header(self, request):
        return 'Bearer realm="api"'
