"""The AI places reminder and feedback calls (appointments.journey) itself.

How a call goes:
1. ``place_call`` asks the voice worker (HeyKoala) to dial the patient: ``POST {VOICE_WORKER_URL}/call``
   with the hospital's number, the outbound SIP trunk and a webhook URL for this one call.
2. When the call connects, the worker POSTs ``{room_name, phone_number}`` to that webhook
   (``PatientCallWebhookView``), which answers with the agent's script for a reminder or a feedback call.
3. During the call the agent reports the outcome with the ``record_call_outcome`` tool
   (``PatientCallOutcomeToolView``), which goes through ``journey.record`` like the desk's own entry.
4. A call that ends without an outcome (not answered, hung up) is treated as unanswered after
   ``OUTCOME_WAIT`` and retried, or left for the desk once the tries run out (``sweep_stale``).

The hospital switches automatic calling on in Settings (``PatientCallSettings.ai_calls_enabled``);
staff can also place one call from the app (``Call with AI``) to try it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import requests
from django.conf import settings as django_settings
from django.utils import timezone

from appointments import journey
from appointments.models import PatientCall

logger = logging.getLogger(__name__)

# How long a dialled call may go without reporting an outcome before it counts as unanswered.
OUTCOME_WAIT = timedelta(minutes=20)
# Calls placed per run of the scheduled dispatcher (it runs every minute).
BATCH = 10

Kind, Status = PatientCall.Kind, PatientCall.Status


class DialError(Exception):
    pass


def public_base_url(request_base_url: str = "") -> str:
    """Where the voice worker reaches this backend: PUBLIC_BASE_URL when set (staff usually open the app on a
    local address), else the address the request came in on."""
    return (getattr(django_settings, "PUBLIC_BASE_URL", "") or request_base_url or "").rstrip("/")


def missing_setup(base_url: str = "") -> str:
    """Why the AI can't place calls on this server, or "" when it can."""
    if not getattr(django_settings, "ROCK8_API_KEY", ""):
        return "The voice provider key (ROCK8_API_KEY) is not set."
    if not getattr(django_settings, "ROCK8_SIP_TRUNK_ID", ""):
        return "The outbound SIP trunk (ROCK8_SIP_TRUNK_ID) is not set."
    if not getattr(django_settings, "ROCK8_WEBHOOK_SECRET", ""):
        return "The voice webhook secret (ROCK8_WEBHOOK_SECRET) is not set."
    base = public_base_url(base_url)
    if not base:
        return "PUBLIC_BASE_URL is not set, so the voice service can't fetch the call's script."
    if any(host in base for host in ("localhost", "127.0.0.1", "0.0.0.0")):
        return "This server is on a local address the voice service can't reach. Use a public (tunnel) address."
    return ""


def hospital_number(clinic) -> str:
    phone = getattr(clinic.root, "phone_number", None)
    if phone is None or phone.status != phone.Status.ACTIVE:
        raise DialError("The hospital has no active phone number to call from (AI Agent → Phone number).")
    return f"+{phone.number}"


def e164(phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) < 10:
        raise DialError("The patient has no valid phone number.")
    return f"+{digits}"


def webhook_url(call: PatientCall, base_url: str) -> str:
    from ai_agent.tool_views import rock8_path
    return f"{base_url.rstrip('/')}/api/v1/ai/webhooks/rock8/{rock8_path(call.clinic.root.id, 'patient-call', str(call.id))}"


def place_call(call: PatientCall, base_url: str = "", now=None) -> PatientCall:
    """Have the AI dial the patient now. Raises DialError (nothing changes) if it can't."""
    now = now or timezone.now()
    base_url = public_base_url(base_url)
    problem = missing_setup(base_url)
    if problem:
        raise DialError(problem)
    if call.status not in (Status.SCHEDULED, Status.NO_ANSWER):
        raise DialError("This call has already been made.")
    if call.appointment.status in ("cancelled",) or not call.appointment.is_active:
        raise DialError("The appointment was cancelled.")

    from ai_agent.agent_settings import call_flags
    payload = {
        "phone_numbers": [e164(call.appointment.patient.phone)],
        "from_number": hospital_number(call.clinic),
        "sip_trunk_id": django_settings.ROCK8_SIP_TRUNK_ID,
        "webhook_url": webhook_url(call, base_url),
        **call_flags(call.clinic.root),
    }
    url = f"{django_settings.VOICE_WORKER_URL.rstrip('/')}/call"
    try:
        response = requests.post(url, json=payload, timeout=30, headers={
            "accept": "application/json", "X-API-Key": django_settings.ROCK8_API_KEY})
    except requests.RequestException as exc:
        raise DialError(f"Couldn't reach the voice service ({exc.__class__.__name__}).") from exc
    if response.status_code not in (200, 201):
        logger.warning("Outbound call %s failed: %s %s", call.id, response.status_code, response.text[:300])
        raise DialError(f"The voice service refused the call (HTTP {response.status_code}).")
    try:
        results = (response.json() or {}).get("results") or []
    except ValueError:
        results = []
    room = str((results[0] or {}).get("room_name") or "") if results else ""

    call.status, call.last_attempt_at, call.room_name = Status.CALLING, now, room[:100]
    call.save(update_fields=["status", "last_attempt_at", "room_name", "updated_at"])
    logger.info("AI %s call %s dialled (room %s)", call.kind, call.id, room or "?")
    return call


def _in_window(settings, now) -> bool:
    local = timezone.localtime(now).time()
    return settings.call_window_start <= local < settings.call_window_end


def dispatch_due(now=None) -> int:
    """Place the calls that are due, for hospitals that switched AI calling on. Returns how many were dialled."""
    now = now or timezone.now()
    sweep_stale(now)
    if missing_setup():
        return 0
    from appointments.models import PatientCallSettings
    placed = 0
    for call_settings in PatientCallSettings.objects.filter(ai_calls_enabled=True).select_related("clinic"):
        if not _in_window(call_settings, now):
            continue
        due = PatientCall.objects.filter(
            clinic__in=call_settings.clinic.group_clinics(), status=Status.SCHEDULED, due_at__lte=now,
            appointment__is_active=True,
        ).select_related("appointment", "appointment__patient", "clinic").order_by("due_at")[:BATCH]
        for call in due:
            try:
                place_call(call, now=now)
                placed += 1
            except DialError as exc:
                # Can't be dialled (no number, refused): count it as a try so the desk takes it over.
                logger.warning("Could not place AI call %s: %s", call.id, exc)
                journey.record(call, "no_answer", comment=f"AI call not placed: {exc}", now=now)
    return placed


def sweep_stale(now=None) -> int:
    """Dialled calls that never reported an outcome: not answered, or the patient hung up."""
    now = now or timezone.now()
    stale = PatientCall.objects.filter(status=Status.CALLING, last_attempt_at__lt=now - OUTCOME_WAIT)
    count = 0
    for call in stale.select_related("appointment", "clinic"):
        journey.record(call, "no_answer", comment="The AI call ended without an answer.", now=now)
        count += 1
    return count


# --- The agent's script --------------------------------------------------------------------------

# Words in braces are filled in for each call.
PLACEHOLDERS = {
    "hospital_name": "The hospital's name",
    "patient_name": "The patient's name",
    "doctor_name": "The doctor's name",
    "appointment_time": "Day, date and time of the appointment",
    "reason": "What the visit is for",
    "review_min_rating": "The rating that counts as good (Settings)",
}

# What the AI says: the hospital edits these on the AI Agent page (blank there = these defaults).
DEFAULT_SCRIPTS = {
    Kind.REMINDER: {
        "first_message": "Hello, this is {hospital_name} calling. Am I speaking with {patient_name}?",
        "script": (
            "You are the AI receptionist of {hospital_name}, calling {patient_name} to remind them of their "
            "appointment with Dr. {doctor_name} on {appointment_time}.\n\n"
            "1. Make sure you are speaking with {patient_name}. If not, politely ask to speak to them.\n"
            "2. Remind them of the appointment and ask if they will be able to come.\n"
            "3. If they will come, thank them and wish them a good day.\n"
            "4. If they want to cancel, confirm that they really want to cancel and ask briefly why.\n"
            "5. If they would like a different time, ask which day suits them and offer the free times on that day.\n\n"
            "Be warm, brief and polite. Speak simply. Don't discuss medical details."
        ),
    },
    Kind.FEEDBACK: {
        "first_message": "Hello, this is {hospital_name} calling. Am I speaking with {patient_name}?",
        "script": (
            "You are the AI receptionist of {hospital_name}, calling {patient_name} about their visit with "
            "Dr. {doctor_name} on {appointment_time}.\n\n"
            "1. Make sure you are speaking with {patient_name}. If not, politely ask to speak to them.\n"
            "2. Ask how the visit went, and ask them to rate it from 1 (very poor) to 5 (excellent).\n"
            "3. Ask if there is anything the hospital could do better.\n"
            "4. If they rate it {review_min_rating} or higher, thank them warmly and say the hospital would be grateful "
            "for a Google review, and that a link will be shared with them.\n"
            "5. If they rate it lower, apologise sincerely and say someone from the hospital will call them back.\n\n"
            "Keep the call short. Don't argue or give medical advice."
        ),
    },
}

# Added after the hospital's script on every call: the facts only the system knows and how the AI saves the
# outcome. Not editable, so an edited script can't stop outcomes from being saved.
SYSTEM_PART = {
    Kind.REMINDER: (
        "CALL DETAILS (added by the system): Today's date is {today}. The appointment_id is {appointment_id} and the "
        "doctor_id is {doctor_id}. The phone number you called is {phone}.\n"
        "SAVING THE RESULT: Before the call ends, call record_call_outcome exactly once:\n"
        "- outcome 'confirmed' if they will come;\n"
        "- outcome 'cancel' (with their reason as comment) if they want to cancel; this cancels the appointment;\n"
        "- for a different time: find free times with get_available_slots (doctor_id above), book the time they pick "
        "with book_appointment (the patient's name and the number you called), cancel the old appointment with "
        "cancel_appointment (appointment_id above), then outcome 'rescheduled';\n"
        "- outcome 'reschedule_later' if no time suits them (tell them the hospital will call back to fix a time);\n"
        "- outcome 'not_reached' if you can't speak to {patient_name} (wrong number, not available); then end the call."
    ),
    Kind.FEEDBACK: (
        "CALL DETAILS (added by the system): Today's date is {today}.\n"
        "SAVING THE RESULT: Before the call ends, call record_call_outcome exactly once: outcome 'rated' with the "
        "rating (1 to 5) and their comments, or outcome 'not_reached' if you can't speak to {patient_name} "
        "(wrong number, not available); then end the call."
    ),
}


def fill(text: str, values: dict) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def scripts_for(call_settings, kind) -> tuple[str, str]:
    """(script, first message) for this kind of call: the hospital's own, else the default."""
    default = DEFAULT_SCRIPTS[kind]
    if kind == Kind.REMINDER:
        return (call_settings.reminder_script or default["script"],
                call_settings.reminder_first_message or default["first_message"])
    return (call_settings.feedback_script or default["script"],
            call_settings.feedback_first_message or default["first_message"])

def _when(dt) -> str:
    local = timezone.localtime(dt)
    return local.strftime("%A, %d %B at %I:%M %p").replace(" 0", " ")


def build_config(call: PatientCall, base_url: str) -> dict:
    """The agent configuration for one reminder or feedback call (same shape as an incoming call's)."""
    from ai_agent.agent_settings import effective_settings, provider_config, session_config
    from ai_agent.models import VoiceAgentSettings
    from ai_agent.tool_views import rock8_path

    appointment = call.appointment
    root = call.clinic.root
    patient = appointment.patient.full_name
    doctor = appointment.doctor.full_name
    agent = effective_settings(root)
    today = timezone.localtime(timezone.now()).strftime("%B %d, %Y (%A)")
    base_url = base_url.rstrip("/")

    def tool_url(*parts):
        return f"{base_url}/api/v1/ai/tools/rock8/{rock8_path(root.id, *parts)}"

    outcome_tool = {
        "name": "record_call_outcome",
        "description": "Save what came of this call. Call it exactly once, before ending the call.",
        "method": "POST",
        "url": tool_url("patient-call", str(call.id), "outcome"),
        "parameters": [
            {"name": "outcome", "type": "string", "location": "body", "required": True,
             "description": ("One of: confirmed, cancel, rescheduled, reschedule_later, not_reached"
                             if call.kind == Kind.REMINDER else "One of: rated, not_reached")},
            {"name": "rating", "type": "string", "location": "body", "required": False,
             "description": "The patient's rating of the visit, 1 to 5 (feedback calls)."},
            {"name": "comment", "type": "string", "location": "body", "required": False,
             "description": "A short note in the patient's own words (why they cancel, what they said about the visit)."},
        ],
    }

    call_settings = journey.settings_for(call.clinic)
    values = {
        "hospital_name": root.name, "patient_name": patient, "doctor_name": doctor,
        "appointment_time": _when(appointment.starts_at), "reason": appointment.reason or "a consultation",
        "review_min_rating": str(call_settings.review_min_rating),
    }
    script, first_message = scripts_for(call_settings, call.kind)
    # The hospital's own script (AI Agent → Call scripts), then what only the system knows and does.
    system_prompt = (
        f"{fill(script, values)}\n\n"
        f"{fill(SYSTEM_PART[call.kind], {**values, 'today': today, 'appointment_id': str(appointment.id), 'doctor_id': str(appointment.doctor_id), 'phone': appointment.patient.phone})}"
    )
    first_message = fill(first_message, values)

    if call.kind == Kind.REMINDER:
        tools = [
            outcome_tool,
            {"name": "get_available_slots", "description": "Free time slots for the doctor on a date.", "method": "GET",
             "url": tool_url("slots"), "parameters": [
                 {"name": "doctor_id", "type": "string", "description": "The doctor's ID", "location": "query", "required": True},
                 {"name": "date", "type": "string", "description": "The date, YYYY-MM-DD", "location": "query", "required": True}]},
            {"name": "book_appointment", "description": "Book the new appointment time.", "method": "POST",
             "url": tool_url("book"), "parameters": [
                 {"name": "doctor_id", "type": "string", "description": "The doctor's ID", "location": "body", "required": True},
                 {"name": "date", "type": "string", "description": "YYYY-MM-DD", "location": "body", "required": True},
                 {"name": "time", "type": "string", "description": "The time, e.g. 10:30", "location": "body", "required": True},
                 {"name": "patient_phone", "type": "string", "description": "The patient's phone number", "location": "body", "required": True},
                 {"name": "patient_name", "type": "string", "description": "The patient's full name", "location": "body", "required": True},
                 {"name": "reason", "type": "string", "description": "Reason for the visit", "location": "body", "required": False}]},
            {"name": "cancel_appointment", "description": "Cancel an appointment (the old one, after rebooking).",
             "method": "POST", "url": tool_url("cancel"), "parameters": [
                 {"name": "appointment_id", "type": "string", "description": "The appointment's ID", "location": "body", "required": True}]},
        ]
        # Patients being reminded often ask where to come or what to bring.
        from ai_agent import knowledge
        knowledge_tool = knowledge.tool_spec(root, tool_url("knowledge"))
        if knowledge_tool:
            tools.append(knowledge_tool)
            system_prompt += knowledge.prompt_note(root)
    else:
        tools = [outcome_tool]

    config = {
        "system_prompt": system_prompt,
        "first_message": first_message,
        "model_type": agent["model_type"],
        "tools": tools,
        # Listening and sounds as set on the Agent settings page; a shorter silence limit than inbound calls,
        # since a patient who has gone quiet on a reminder call is not coming back.
        **{**session_config(agent), "max_silence_seconds": min(30, session_config(agent)["max_silence_seconds"])},
    }
    if agent["model_type"] == VoiceAgentSettings.ModelType.REALTIME:
        config["realtime_config"] = provider_config(agent["realtime"])
    else:
        for stage in ("stt", "llm", "tts"):
            config[f"{stage}_config"] = provider_config(agent[stage])
    return config


def start_log(call: PatientCall, room_name: str):
    """File the outgoing call in Call logs (recording and transcript are fetched like any call's)."""
    from ai_agent.call_events import _upsert
    from ai_agent.call_sessions import enqueue_recording_fetch
    from ai_agent.models import CallLog

    room = (room_name or call.room_name)[:100]
    patient = call.appointment.patient
    purpose = "Reminder" if call.kind == Kind.REMINDER else "Feedback"
    log = _upsert(call.clinic.root, room, defaults={
        "direction": CallLog.Direction.OUTBOUND,
        "agent_name": "AI receptionist",
        "occurred_at": timezone.now(),
        "patient": patient,
        "phone": patient.phone,
        "appointment": call.appointment,
        "language": "en",
        "outcome": "in_progress",
        "summary": f"{purpose} call to {patient.full_name} about the appointment on "
                   f"{datetime.strftime(timezone.localtime(call.appointment.starts_at), '%d %b, %I:%M %p')}.",
    }, updates={})
    updates = ["call_log", "updated_at"]
    call.call_log = log
    if room and not call.room_name:
        call.room_name = room
        updates.append("room_name")
    call.save(update_fields=updates)
    # Without a task queue (CELERY_TASK_ALWAYS_EAGER) the fetch would run right here, polling for a recording
    # of a call that hasn't happened yet, and the worker hangs up before it gets the script. Skip it then.
    if room:
        enqueue_recording_fetch(log.id, room, "".join(ch for ch in patient.phone if ch.isdigit()))
    return log
