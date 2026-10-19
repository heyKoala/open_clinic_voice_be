"""Call logs for calls whose only contact with us is the call-start config request.

Inbound numbers configured on the voice provider (HeyKoala) never send call lifecycle events: the
only request we get is the config request at call start, ``{room_name, phone_number, metadata}``.
So the CallLog is created then, and a background task later fetches the recording and transcript
from the provider's ``GET /recordings?room_name=`` endpoint once the call has ended.
"""
from __future__ import annotations

import logging
import time

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
# The app's Test call joins as "voice_assistant_user_<n>" (frontend CallAgent page).
TEST_CALL_IDENTITY_PREFIX = "voice_assistant_user_"


def is_test_call(data) -> bool:
    """A call from the app's Test call button rather than a patient's phone (by its participant name)."""
    return isinstance(data, dict) and str(data.get("participant_identity") or "").startswith(TEST_CALL_IDENTITY_PREFIX)


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


def start_call_log(clinic, data, is_test: bool = False) -> CallLog | None:
    """Create the CallLog when a call starts, and schedule fetching its recording/transcript.
    ``is_test`` marks a call from the app's Test call button (it is also recognised from ``data``)."""
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
        "phone": f"+{caller}" if caller else "",
        "language": "en",
        "outcome": IN_PROGRESS,
    }, updates={"is_test": True} if (is_test or is_test_call(data)) else {})
    if caller and not log.summary:
        CallLog.objects.filter(id=log.id).update(summary=f"Call from +{caller}.")
    if caller and not log.phone:
        CallLog.objects.filter(id=log.id).update(phone=f"+{caller}")
    enqueue_recording_fetch(log.id, room, caller)
    return log


def enqueue_recording_fetch(call_log_id: int, room_name: str, caller: str, countdown: int = 90) -> None:
    """Fetch the call's transcript and recording once the call has had time to finish.

    With a task queue this is a delayed Celery task. Without one (CELERY_TASK_ALWAYS_EAGER, local setups)
    an eager apply_async would run the fetch right here, inside the voice worker's config request, and the
    worker gives up waiting; so it runs on a background timer thread instead and the request returns at once.
    """
    from ai_agent.tasks import fetch_call_recording

    def _run_later():
        import threading
        from django.db import connection

        def _job():
            try:
                # Not the Celery task: in eager mode its retry() re-runs at once and ignores the countdown,
                # so all the polls would happen within a second, while the call is still going.
                poll_recording(call_log_id, room_name, caller)
            except Exception:
                logger.warning("Background recording fetch for CallLog %s failed", call_log_id, exc_info=True)
            finally:
                connection.close()

        threading.Timer(countdown, _job).start()

    def _enqueue():
        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) and getattr(settings, "RECORDING_FETCH_IN_THREAD", True):
            _run_later()
            return
        try:
            fetch_call_recording.apply_async((call_log_id, room_name, caller), countdown=countdown)
        except Exception:
            # No broker: the log still exists; its media just won't be fetched automatically.
            logger.warning("Task queue unavailable; recording for CallLog %s will not be fetched", call_log_id)

    transaction.on_commit(_enqueue)


POLL_ATTEMPTS = 20
POLL_WAIT_SECONDS = 120


def poll_recording(call_log_id: int, room_name: str, caller: str, attempts: int = POLL_ATTEMPTS,
                   wait_seconds: int = POLL_WAIT_SECONDS) -> bool:
    """Poll the provider until the call's transcript exists (up to ~40 minutes), then store the media and
    finish the log. Returns True when media was found. Used by the background thread (see
    enqueue_recording_fetch) and by the refetch_recordings command."""
    from ai_agent.call_events import fetch_media

    links = {}
    for attempt in range(attempts):
        try:
            links = recording_links(room_name)
        except requests.RequestException:
            logger.warning("Could not reach the recordings endpoint for CallLog %s", call_log_id, exc_info=True)
            links = {}
        if links.get("transcript_url"):
            break
        if attempt < attempts - 1:
            time.sleep(wait_seconds)  # call still in progress, or files not uploaded yet
    transcript_url, recording_url = links.get("transcript_url"), links.get("recording_url")
    if transcript_url or recording_url:
        fetch_media(call_log_id, transcript_url, recording_url)
    finish_call_log(call_log_id, caller, got_media=bool(transcript_url or recording_url))
    return bool(transcript_url or recording_url)


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
    # An outcome classified from the transcript (call_events.classify_outcome) stands. Otherwise: booked on
    # this call (the linked appointment, or one the caller's patient record got during the call), else no tag
    # once the media is in, or "no_recording" while it is still missing (so the fetch can be retried).
    if log.outcome in ("", IN_PROGRESS, "completed", "no_recording"):
        booked = (log.appointment_id is not None and Appointment.objects.filter(id=log.appointment_id, source="phone").exists()) or (
            patient is not None and Appointment.objects.filter(patient=patient, source="phone", created_at__gte=log.occurred_at).exists())
        updates["outcome"] = "appointment_booked" if booked else ("" if got_media else "no_recording")
    if updates:
        for field, value in updates.items():
            setattr(log, field, value)
        log.save(update_fields=[*updates, "updated_at"])
