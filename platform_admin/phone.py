"""Linking a number that is already on the telephone account to a hospital (nothing is bought).

Same rules as the ``link_phone_number`` management command, for the platform admin page.
"""
from __future__ import annotations

from django.conf import settings
from rest_framework import serializers
from rest_framework.exceptions import APIException
from rest_framework import status

from clinics import plivo
from clinics.models import Clinic, ClinicConfiguration, ClinicPhoneNumber


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"


def link_existing_number(root: Clinic, raw_number) -> ClinicPhoneNumber:
    digits = plivo.normalize_number(raw_number)
    if not 8 <= len(digits) <= 15:
        raise serializers.ValidationError({"number": ["Enter a valid phone number with country code."]})

    existing = ClinicPhoneNumber.objects.filter(number=digits).select_related("clinic").first()
    if existing and existing.clinic_id != root.id:
        raise Conflict({"number": ["This number already belongs to another hospital."]})
    other = ClinicPhoneNumber.objects.filter(clinic=root).exclude(number=digits).first()
    if other:
        raise Conflict({"detail": "This hospital already has a phone number. Release it first."})

    try:
        info = plivo._request("GET", f"Number/{digits}/")
    except plivo.PlivoError:
        raise serializers.ValidationError({"number": [f"+{digits} is not on the telephone account, so it can't be linked."]})

    app_id = settings.PLIVO_APP_ID
    routed = bool(app_id) and app_id in (info.get("application") or "")
    record, _ = ClinicPhoneNumber.objects.update_or_create(
        number=digits,
        defaults={
            "clinic": root,
            "bought_here": existing.bought_here if existing else False,
            "city": info.get("city") or info.get("region") or "",
            "monthly_rental_rate": str(info.get("monthly_rental_rate") or ""),
            "app_id": app_id if routed else "",
            "status": ClinicPhoneNumber.Status.ACTIVE if routed else ClinicPhoneNumber.Status.PURCHASED,
            "last_error": "" if routed else "Not routed to the voice agent's SIP app yet.",
        },
    )
    config, _ = ClinicConfiguration.objects.get_or_create(clinic=root)
    config.ai_phone_number = f"+{digits}"
    config.save(update_fields=["ai_phone_number", "updated_at"])
    return record
