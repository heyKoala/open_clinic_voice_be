"""Turning voice-provider (HeyKoala / Rock8) call events into CallLog rows.

Two payload shapes reach the webhook:

* HeyKoala's documented call events (https://voice.heykoala.ai/docs/webhooks)::

      {"id": ..., "event": "call.ended" | "call.analysis_completed" | ..., "occurred_at": ...,
       "call": {"call_id", "direction", "status", "outcome", "disposition", "from_number",
                "to_number", "started_at", "ended_at", "duration_seconds", "contact": {...}},
       "analysis": {"summary", "short_summary", "disposition", ...},      # analysis_completed only
       "media": {"recording_url", "transcript_url", "expires_in"}}        # analysis_completed only

  The transcript is not in the payload: it is behind a signed ``media.transcript_url``.

* The older web-call format: ``{"status": "completed" | "event": "call_ended", "durationSeconds",
  "recordingURL", "callSummary", "transcript": [{"role", "text"}, ...]}``.

Anything else (e.g. the call-start request asking for the agent configuration) is not a call event.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os

import requests
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ai_agent.models import CallLog
from patients.models import Patient

logger = logging.getLogger(__name__)

PROVIDER_EVENTS = {
    "call.started", "call.connected", "call.ended", "call.failed",
    "call.recording_available", "call.analysis_completed",
}
LEGACY_END_EVENTS = {"call_ended", "call.ended"}
MAX_RECORDING_BYTES = 50 * 1024 * 1024
AI_ROLES = {"agent", "assistant", "ai", "bot", "system_agent"}


def is_call_event(data) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("event") in PROVIDER_EVENTS and isinstance(data.get("call"), dict):
        return True
    return data.get("status") == "completed" or data.get("event") in LEGACY_END_EVENTS


def _parse_dt(value):
    if not value:
        return None
    parsed = parse_datetime(str(value))
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _as_int(value, default=0):
    try:
        return max(int(float(value)), 0)
    except (TypeError, ValueError):
        return default


def _digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def match_patient(clinic, *phones):
    """Find the caller among the patients of the clinic's group (the centres sharing its number),
    by phone (last 10 digits; stored numbers may contain +91/spaces)."""
    for phone in phones:
        wanted = _digits(phone)[-10:]
        if len(wanted) < 7:
            continue
        for patient_id, stored in Patient.objects.filter(clinic__in=clinic.group_clinics(), phone__contains=wanted[-4:]).values_list("id", "phone"):
            if _digits(stored).endswith(wanted):
                return Patient.objects.get(id=patient_id)
    return None


def format_transcript(raw) -> str:
    """Normalise a transcript into ``AI: ...`` / ``Patient: ...`` lines (what the Call Logs page renders)."""
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        text = raw.strip()
        if text[:1] in "[{":
            try:
                return format_transcript(json.loads(text))
            except ValueError:
                pass
        return text
    if isinstance(raw, dict):
        for key in ("transcript", "messages", "turns", "conversation", "segments", "items", "data"):
            if key in raw:
                return format_transcript(raw[key])
        return ""
    if isinstance(raw, list):
        lines = []
        for turn in raw:
            if isinstance(turn, str):
                lines.append(turn.strip())
                continue
            if not isinstance(turn, dict):
                continue
            role = str(turn.get("role") or turn.get("speaker") or turn.get("from") or turn.get("participant") or "").lower()
            text = turn.get("text") or turn.get("content") or turn.get("message") or turn.get("transcript") or ""
            if isinstance(text, list):
                text = " ".join(str(part.get("text", part)) if isinstance(part, dict) else str(part) for part in text)
            text = " ".join(str(text).split())
            if not text:
                continue
            speaker = "AI" if role in AI_ROLES or role.startswith("agent") else "Patient"
            lines.append(f"{speaker}: {text}")
        return "\n".join(line for line in lines if line)
    return str(raw)


def _outcome(call: dict, analysis: dict, event: str) -> str:
    if event == "call.failed":
        return "unanswered"
    for value in (analysis.get("disposition"), call.get("disposition"), call.get("outcome"), call.get("outcome_code"), call.get("status")):
        if value:
            return str(value)[:100]
    return "completed"


def record_call_event(clinic, data: dict) -> CallLog | None:
    """Create or update the CallLog for a provider event. Returns the log (None for ignorable events)."""
    if data.get("event") in PROVIDER_EVENTS and isinstance(data.get("call"), dict):
        return _record_provider_event(clinic, data)
    return _record_legacy_event(clinic, data)


def _upsert(clinic, external_id: str, defaults: dict, updates: dict) -> CallLog:
    """Get-or-create by provider call id (safe against concurrent/retried deliveries), then apply updates.

    A new log is filed under the caller's centre when they're a known patient, else under ``clinic``.
    """
    patient = defaults.get("patient")
    home = patient.clinic if patient is not None else clinic
    if not external_id:
        log = CallLog.objects.create(clinic=home, **defaults, **updates)
        return log
    group = clinic.group_clinics()
    with transaction.atomic():
        log = CallLog.objects.select_for_update().filter(clinic__in=group, external_call_id=external_id).first()
        if log is None:
            try:
                with transaction.atomic():
                    log = CallLog.objects.create(clinic=home, external_call_id=external_id, **defaults, **updates)
                return log
            except IntegrityError:
                log = CallLog.objects.select_for_update().get(clinic=home, external_call_id=external_id)
        for field, value in updates.items():
            setattr(log, field, value)
        log.save()
    return log


def _record_provider_event(clinic, data: dict) -> CallLog | None:
    event = data["event"]
    call = data.get("call") or {}
    analysis = data.get("analysis") or {}
    media = data.get("media") or {}
    external_id = str(call.get("call_id") or data.get("call_id") or "")[:100]

    if event in {"call.started", "call.connected"} and not external_id:
        return None

    direction = call.get("direction") if call.get("direction") in CallLog.Direction.values else CallLog.Direction.INBOUND
    contact = call.get("contact") or {}
    caller = call.get("from_number") if direction == CallLog.Direction.INBOUND else call.get("to_number")
    agent = call.get("agent") or {}
    occurred = _parse_dt(call.get("started_at")) or _parse_dt(data.get("occurred_at")) or timezone.now()

    defaults = {
        "direction": direction,
        "agent_name": (agent.get("name") or "ManageOPD AI")[:100],
        "occurred_at": occurred,
        "patient": match_patient(clinic, contact.get("phone"), caller),
        "language": str((call.get("variables") or {}).get("language") or "en")[:32],
    }
    updates = {}
    if event in {"call.ended", "call.failed", "call.analysis_completed"}:
        updates["duration_seconds"] = _as_int(call.get("duration_seconds"))
        updates["outcome"] = _outcome(call, analysis, event)
    if event == "call.analysis_completed":
        summary = analysis.get("summary") or analysis.get("short_summary")
        if summary:
            updates["summary"] = str(summary)
    if media.get("recording_url"):
        updates["recording_url"] = str(media["recording_url"])[:1000]

    log = _upsert(clinic, external_id, defaults, updates)

    transcript_url = media.get("transcript_url")
    recording_url = media.get("recording_url")
    if transcript_url or recording_url:
        enqueue_media_fetch(log.id, transcript_url, recording_url)
    return log


def _record_legacy_event(clinic, data: dict) -> CallLog:
    direction = data.get("direction") if data.get("direction") in CallLog.Direction.values else CallLog.Direction.INBOUND
    external_id = str(data.get("callId") or data.get("call_id") or data.get("roomName") or "")[:100]
    updates = {
        "duration_seconds": _as_int(data.get("durationSeconds") or data.get("duration_seconds")),
        "outcome": str(data.get("outcome") or "completed")[:100],
        "transcript": format_transcript(data.get("transcript")),
        "recording_url": str(data.get("recordingURL") or data.get("recording_url") or "")[:1000],
        "summary": str(data.get("callSummary") or data.get("summary") or ""),
    }
    defaults = {
        "direction": direction,
        "agent_name": "ManageOPD AI",
        "language": str(data.get("language") or "en")[:32],
        "occurred_at": timezone.now(),
        "patient": match_patient(clinic, data.get("phone"), data.get("from_number"), data.get("callerNumber")),
    }
    log = _upsert(clinic, external_id, defaults, updates)
    if updates["recording_url"]:
        enqueue_media_fetch(log.id, None, updates["recording_url"])
    return log


def enqueue_media_fetch(call_log_id: int, transcript_url: str | None, recording_url: str | None) -> None:
    """Fetch media in the background (the provider wants a fast 2xx); fall back to inline if the queue is down."""
    from ai_agent.tasks import fetch_call_media

    def _enqueue():
        try:
            fetch_call_media.delay(call_log_id, transcript_url, recording_url)
        except Exception:
            logger.warning("Task queue unavailable; fetching call media inline for CallLog %s", call_log_id)
            fetch_media(call_log_id, transcript_url, recording_url)

    transaction.on_commit(_enqueue)


def fetch_media(call_log_id: int, transcript_url: str | None, recording_url: str | None) -> None:
    """Download the transcript (stored as text) and the recording (stored as a file). Raises on HTTP errors."""
    log = CallLog.objects.filter(id=call_log_id).first()
    if log is None:
        return
    if transcript_url:
        response = requests.get(transcript_url, timeout=20)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        transcript = format_transcript(payload)
        if transcript:
            CallLog.objects.filter(id=log.id).update(transcript=transcript, updated_at=timezone.now())
    if recording_url and not log.recording_file:
        with requests.get(recording_url, timeout=60, stream=True) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
            ext = mimetypes.guess_extension(content_type) or os.path.splitext(recording_url.split("?")[0])[1] or ".mp3"
            chunks, size = [], 0
            for chunk in response.iter_content(64 * 1024):
                size += len(chunk)
                if size > MAX_RECORDING_BYTES:
                    logger.warning("Recording for CallLog %s exceeds %s bytes; keeping the provider link only", log.id, MAX_RECORDING_BYTES)
                    return
                chunks.append(chunk)
        name = f"{log.external_call_id or log.id}{ext}"
        log.recording_file.save(name, ContentFile(b"".join(chunks)), save=False)
        CallLog.objects.filter(id=log.id).update(recording_file=log.recording_file.name, updated_at=timezone.now())
