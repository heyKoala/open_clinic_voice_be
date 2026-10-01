from __future__ import annotations

from clinics.context import bind_active_clinic


class ClinicContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        request.clinic = None
        if user is not None and getattr(user, "is_authenticated", False):
            bind_active_clinic(request, user)
        return self.get_response(request)
