from __future__ import annotations

ACTIVE_CLINIC_HEADER = "X-Active-Clinic-Id"
# Header value meaning "every centre of my hospital at once" (the front desk's default view).
ALL_CENTRES = "all"


def scope_clinics(request):
    """The clinics a request may read: the active one, or the whole hospital group in all-centres mode."""
    clinic = getattr(request, "clinic", None)
    if clinic is None:
        return []
    if getattr(request, "all_centres", False):
        return list(clinic.group_clinics())
    return [clinic]


def in_scope(request, clinic_id) -> bool:
    return any(c.id == clinic_id for c in scope_clinics(request))


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
    requested = request.headers.get(ACTIVE_CLINIC_HEADER)
    all_centres = str(requested or "").strip().lower() == ALL_CENTRES
    active_clinic = resolve_active_clinic(user, None if all_centres else requested)
    if all_centres and active_clinic is not None:
        # Act from the main hospital, over every centre the user belongs to. A user who is only a member
        # of one centre keeps that centre's view.
        root = active_clinic.root
        if root.id == active_clinic.id or user.clinics.filter(id=root.id).exists():
            active_clinic = root
        else:
            all_centres = False
    request.clinic = active_clinic
    request.all_centres = all_centres
    django_request = getattr(request, "_request", None)
    if django_request is not None:
        django_request.clinic = active_clinic
        django_request.all_centres = all_centres
    if user is not None and getattr(user, "is_authenticated", False):
        # In-memory only; never save the user mid-request.
        user.clinic = active_clinic
