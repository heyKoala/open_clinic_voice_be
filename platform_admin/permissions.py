from __future__ import annotations

from django.http import Http404
from rest_framework.permissions import BasePermission
from rest_framework.views import APIView


class IsPlatformAdmin(BasePermission):
    """HeyKoala's own staff login: ``is_staff``, active and verified. Hospital users are not platform admins."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_active and user.is_staff and user.is_verified)


class PlatformAPIView(APIView):
    """Base for every /platform/ view. Anyone who is not a platform admin gets a plain 404, so the area is not advertised."""

    permission_classes = [IsPlatformAdmin]

    def permission_denied(self, request, message=None, code=None):
        raise Http404
