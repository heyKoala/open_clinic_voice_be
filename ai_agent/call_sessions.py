"""Call logs for calls whose only contact with us is the call-start config request.

Inbound numbers configured on the voice provider (HeyKoala) never send call lifecycle events: the
only request we get is the config request at call start, ``{room_name, phone_number, metadata}``.
So the CallLog is created then, and a background task later fetches the recording and transcript
from the provider's ``GET /recordings?room_name=`` endpoint once the call has ended.
"""
from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ai_agent.call_events import _digits, _upsert, match_patient
from ai_agent.models import CallLog

logger = logging.getLogger(__name__)

RECORDINGS_URL = "https://worker.heykoala.ai/recordings"
IN_PROGRESS = "in_progress"
CALLER_KEYS = ("phone_number", "from_number", "caller", "caller_number", "from", "sip.phoneNumber")


def caller_number(data) -> str:
    """The patient's phone number for this call ("" if unknown)."""
    if not isinstance(data, dict):
        return ""
    sources = [data, data.get("metadata") or {}, data.get("participant_attributes") or {}, data.get("attributes") or {}]
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in CALLER_KEYS:
            digits = _digits(source.get(key))
            if len(digits) >= 10:
                return digits
    identity = str(data.get("participant_identity") or "")  # e.g. "sip_+919876543210"
    digits = _digits(identity)
    return digits if identity.startswith("sip") and len(digits) >= 10 else ""


def start_call_log(clinic, data) -> CallLog | None:
    """Create the CallLog when a call starts, and schedule fetching its recording/transcript."""
    room = str((data or {}).get("room_name") or "")[:100] if isinstance(data, dict) else ""
    if not room:
        return None
    caller = caller_number(data)
    root = clinic.root
    log = _upsert(root, room, defaults={
        "direction": CallLog.Direction.INBOUND,
        "agent_name": "ManageOPD AI",
        "occurred_at": timezone.now(),
        "patient": match_patient(root, caller),
        "language": "en",
        "outcome": IN_PROGRESS,
    }, updates={})
    if caller and not log.summary:
        CallLog.objects.filter(id=log.id).update(summary=f"Call from +{caller}.")
    enqueue_recording_fetch(log.id, room, caller)
    return log


def enqueue_recording_fetch(call_log_id: int, room_name: str, caller: str, countdown: int = 90) -> None:
    from ai_agent.tasks import fetch_call_recording

    def _enqueue():
        try:
            fetch_call_recording.apply_async((call_log_id, room_name, caller), countdown=countdown)
        except Exception:
            # No broker: the log still exists; its media just won't be fetched automatically.
            logger.warning("Task queue unavailable; recording for CallLog %s will not be fetched", call_log_id)

    transaction.on_commit(_enqueue)


def recording_links(room_name: str) -> dict:
    response = requests.get(RECORDINGS_URL, params={"room_name": room_name},
                            headers={"accept": "application/json", "X-API-Key": settings.ROCK8_API_KEY}, timeout=30)
    response.raise_for_status()
    return response.json()


def finish_call_log(call_log_id: int, caller: str, got_media: bool) -> None:
    """Link the caller (they may have been registered during the call) and set the outcome."""
    from appointments.models import Appointment

    log = CallLog.objects.select_related("clinic").filter(id=call_log_id).first()
    if log is None:
        return
    updates = {}
    patient = log.patient or match_patient(log.clinic, caller)
    if patient and not log.patient_id:
        updates["patient"] = patient
    # Outcomes this task sets itself may be refined on a later run (e.g. the booking landed after
    # the first poll); outcomes from provider events are left alone.
    if log.outcome in ("", IN_PROGRESS, "completed", "no_recording"):
        booked = patient is not None and Appointment.objects.filter(
            patient=patient, source="phone", created_at__gte=log.occurred_at).exists()
        updates["outcome"] = "appointment_booked" if booked else ("completed" if got_media else "no_recording")
    if updates:
        for field, value in updates.items():
            setattr(log, field, value)
        log.save(update_fields=[*updates, "updated_at"])
