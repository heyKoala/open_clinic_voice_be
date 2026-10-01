from __future__ import annotations

ACTIVE_CLINIC_HEADER = "X-Active-Clinic-Id"


def resolve_active_clinic(user, requested_id=None):
    """Return the clinic a user is acting in.

    Falls back to the user's primary clinic unless ``requested_id`` names another
    clinic the user is a member of.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    active_clinic = getattr(user, "clinic", None)
    if requested_id:
        try:
            requested_id = int(requested_id)
        except (ValueError, TypeError):
            return active_clinic
        if active_clinic is not None and active_clinic.id == requested_id:
            return active_clinic
        clinic_match = user.clinics.filter(id=requested_id).first()
        if clinic_match:
            active_clinic = clinic_match
    return active_clinic


def bind_active_clinic(request, user) -> None:
    """Resolve the active clinic and attach it to the request and (in memory) the user."""
    active_clinic = resolve_active_clinic(user, request.headers.get(ACTIVE_CLINIC_HEADER))
    request.clinic = active_clinic
    django_request = getattr(request, "_request", None)
    if django_request is not None:
        django_request.clinic = active_clinic
    if user is not None and getattr(user, "is_authenticated", False):
        # In-memory only; never save the user mid-request.
        user.clinic = active_clinic
