"""Call events from the voice provider become CallLogs with transcripts and recordings."""
from __future__ import annotations

import json
from unittest import mock

import pytest
from rest_framework import status

from ai_agent.call_events import format_transcript, transcript_duration
from ai_agent.models import CallLog
from ai_agent.tool_views import rock8_clinic_token
from patients.models import Patient

pytestmark = pytest.mark.django_db

CALL_ID = "e21babb7-c056-40aa-8d07-73f5fbc607d4"
TRANSCRIPT = [
    {"role": "agent", "text": "Welcome to Test Clinic. How can I help you today?"},
    {"role": "user", "text": "I want to book an appointment for tomorrow."},
    {"role": "agent", "text": "Sure, Dr. Rao is free at 10 AM. Shall I book it?"},
]


@pytest.fixture(autouse=True)
def webhook_settings(settings, tmp_path):
    settings.ROCK8_WEBHOOK_SECRET = "test-secret"
    settings.MEDIA_ROOT = tmp_path


def _event(event, **extra):
    payload = {
        "id": f"delivery-{event}",
        "event": event,
        "occurred_at": "2026-09-25T08:30:22+00:00",
        "call": {
            "call_id": CALL_ID,
            "direction": "inbound",
            "status": "completed",
            "outcome": None,
            "disposition": "appointment_booked",
            "from_number": "+91 98765 43210",
            "to_number": "+911234567890",
            "started_at": "2026-09-25T08:28:45+00:00",
            "ended_at": "2026-09-25T08:30:22+00:00",
            "duration_seconds": 97,
            "agent": {"id": "a1", "name": "Noora"},
            "contact": {"phone": "+919876543210"},
        },
        "data": {"event": "room_finished"},
    }
    payload.update(extra)
    return payload


def _post(client, clinic, payload, capture):
    with capture(execute=True):
        return client.post(
            f"/api/v1/ai/webhooks/rock8/{clinic.id}/?token={rock8_clinic_token(clinic.id)}",
            data=json.dumps(payload), content_type="application/json",
        )


def _fake_get(url, **kwargs):
    response = mock.MagicMock()
    response.raise_for_status.return_value = None
    response.__enter__.return_value = response
    if "transcript" in url:
        response.json.return_value = TRANSCRIPT
    else:
        response.headers = {"Content-Type": "audio/mpeg"}
        response.iter_content.return_value = [b"ID3fake-mp3-bytes"]
    return response


def test_real_call_lifecycle_creates_one_log_with_transcript_and_recording(
    api_client, admin_client, clinic, django_capture_on_commit_callbacks
):
    patient = Patient.objects.create(clinic=clinic, full_name="Asha Menon", phone="9876543210")

    res = _post(api_client, clinic, _event("call.ended"), django_capture_on_commit_callbacks)
    assert res.status_code == status.HTTP_200_OK
    log = CallLog.objects.get(clinic=clinic, external_call_id=CALL_ID)
    assert (log.duration_seconds, log.direction, log.outcome, log.patient_id) == (97, "inbound", "appointment_booked", patient.id)
    assert log.agent_name == "Noora"

    media = {"recording_url": "https://cdn.example/rec.mp3?X-Amz-Signature=abc", "transcript_url": "https://cdn.example/transcript.json?sig=1", "expires_in": 3600}
    analysis = {"summary": "Caller booked an appointment with Dr. Rao for tomorrow.", "disposition": "appointment_booked"}
    with mock.patch("ai_agent.call_events.requests.get", side_effect=_fake_get):
        res = _post(api_client, clinic, _event("call.analysis_completed", analysis=analysis, media=media), django_capture_on_commit_callbacks)
        assert res.status_code == status.HTTP_200_OK
        # Provider retries the same delivery — must not duplicate.
        _post(api_client, clinic, _event("call.analysis_completed", analysis=analysis, media=media), django_capture_on_commit_callbacks)

    assert CallLog.objects.filter(clinic=clinic).count() == 1
    log.refresh_from_db()
    assert log.summary == analysis["summary"]
    assert log.transcript.splitlines() == [
        "AI: Welcome to Test Clinic. How can I help you today?",
        "Patient: I want to book an appointment for tomorrow.",
        "AI: Sure, Dr. Rao is free at 10 AM. Shall I book it?",
    ]
    assert log.recording_file

    # The Call Logs page gets our stored copy (the provider link expires) and can play it.
    listing = admin_client.get("/api/v1/ai/call-logs/")
    item = listing.data["results"][0]
    assert item["recording_url"].endswith(f"/api/v1/ai/call-logs/{log.id}/recording/")
    audio = admin_client.get(f"/api/v1/ai/call-logs/{log.id}/recording/")
    assert audio.status_code == 200
    assert b"".join(audio.streaming_content) == b"ID3fake-mp3-bytes"


def test_failed_call_is_logged_as_unanswered(api_client, clinic, django_capture_on_commit_callbacks):
    payload = _event("call.failed")
    payload["call"].update(status="failed", duration_seconds=0)
    res = _post(api_client, clinic, payload, django_capture_on_commit_callbacks)
    assert res.status_code == 200
    assert CallLog.objects.get(external_call_id=CALL_ID).outcome == "unanswered"


def test_web_call_format_is_still_recorded(api_client, clinic, django_capture_on_commit_callbacks):
    payload = {"status": "completed", "durationSeconds": 42, "callSummary": "Asked about timings.",
               "transcript": [{"role": "agent", "text": "Hello"}, {"role": "user", "text": "What are your hours?"}]}
    res = _post(api_client, clinic, payload, django_capture_on_commit_callbacks)
    assert res.status_code == 200
    log = CallLog.objects.get(clinic=clinic)
    assert (log.duration_seconds, log.summary) == (42, "Asked about timings.")
    assert log.transcript == "AI: Hello\nPatient: What are your hours?"


def test_call_start_request_still_returns_agent_config(api_client, clinic, django_capture_on_commit_callbacks):
    res = _post(api_client, clinic, {"participant_identity": "caller"}, django_capture_on_commit_callbacks)
    assert res.status_code == 200
    assert "system_prompt" in res.data and "tools" in res.data
    assert not CallLog.objects.exists()


def test_event_without_token_is_rejected(api_client, clinic):
    res = api_client.post(f"/api/v1/ai/webhooks/rock8/{clinic.id}/", data=json.dumps(_event("call.ended")), content_type="application/json")
    assert res.status_code == status.HTTP_403_FORBIDDEN
    assert not CallLog.objects.exists()


@pytest.mark.parametrize("raw, expected", [
    ([{"speaker": "assistant", "content": "Hi"}, {"speaker": "customer", "content": "Hey"}], "AI: Hi\nPatient: Hey"),
    ({"messages": [{"role": "bot", "message": "Hi"}]}, "AI: Hi"),
    ('[{"role": "agent", "text": "Hi"}]', "AI: Hi"),
    ("AI: Hi\nPatient: Hello", "AI: Hi\nPatient: Hello"),
    ("[11:32:09] AGENT: Hi\n[11:32:14] USER: Book me in\n[11:32:25] TOOL CALL: get_available_slots {\"date\":\n"
     " \"2026-10-02\"}\n[11:32:26] TOOL RESULT: get_available_slots (ok, 1.0ms) {}\n[11:32:35] AGENT: Done",
     "AI: Hi\nPatient: Book me in\nAI: Done"),
    (None, ""),
])
def test_format_transcript_variants(raw, expected):
    assert format_transcript(raw) == expected


def test_token_can_be_in_the_url_path_instead_of_a_query_string(api_client, clinic):
    """The provider's inbound setup only takes a plain URL, so the token may sit in the path."""
    good = f"/api/v1/ai/webhooks/rock8/{clinic.id}/{rock8_clinic_token(clinic.id)}/"
    res = api_client.post(good, data=json.dumps({"participant_identity": "caller"}), content_type="application/json")
    assert res.status_code == 200 and "system_prompt" in res.data
    # Tool URLs handed to the provider carry no query string either, and work as given.
    tool_url = next(t["url"] for t in res.data["tools"] if t["name"] == "list_doctors")
    assert "?" not in tool_url and rock8_clinic_token(clinic.id) in tool_url
    assert api_client.get(tool_url.split("testserver", 1)[1]).status_code == 200

    bad = f"/api/v1/ai/webhooks/rock8/{clinic.id}/{'0' * 64}/"
    assert api_client.post(bad, data="{}", content_type="application/json").status_code == 403
    other_clinic_token = f"/api/v1/ai/webhooks/rock8/{clinic.id}/{rock8_clinic_token(clinic.id + 1)}/"
    assert api_client.post(other_clinic_token, data="{}", content_type="application/json").status_code == 403


def test_inbound_call_is_logged_from_the_config_request_and_media_fetched_later(
    api_client, clinic, django_capture_on_commit_callbacks
):
    """Inbound numbers send no call-ended events: the log starts at the config request and the
    recording/transcript are pulled from the provider afterwards."""
    from appointments.models import Appointment
    from doctors.models import Doctor
    from django.utils import timezone

    patient = Patient.objects.create(clinic=clinic, full_name="Asha Menon", phone="+91 98765 43210")
    links = {"recording_url": "https://cdn.example/rec.mp3", "transcript_url": "https://cdn.example/transcript.json"}
    with mock.patch("ai_agent.call_sessions.recording_links", return_value=links) as polled, \
         mock.patch("ai_agent.call_events.requests.get", side_effect=_fake_get):
        res = _post(api_client, clinic, {"room_name": "room-42", "phone_number": "+919876543210", "metadata": {}},
                    django_capture_on_commit_callbacks)
        # The agent books during the call.
        doctor = Doctor.objects.create(clinic=clinic, full_name="Dr Rao")
        start = timezone.now() + timezone.timedelta(days=1)
        Appointment.objects.create(clinic=clinic, doctor=doctor, patient=patient, starts_at=start,
                                   ends_at=start + timezone.timedelta(minutes=15), source="phone")
        from ai_agent.tasks import fetch_call_recording
        fetch_call_recording.apply(args=(CallLog.objects.get().id, "room-42", "919876543210"))

    assert res.status_code == 200
    assert "+919876543210" in res.data["system_prompt"] and "Asha Menon" in res.data["system_prompt"]
    polled.assert_called_with("room-42")
    log = CallLog.objects.get()
    assert (log.external_call_id, log.patient_id, log.outcome) == ("room-42", patient.id, "appointment_booked")
    assert log.transcript.startswith("AI: Welcome to Test Clinic") and log.recording_file


def test_booking_with_placeholder_patient_details_is_refused(api_client, clinic):
    from doctors.models import Doctor
    from django.utils import timezone

    doctor = Doctor.objects.create(clinic=clinic, full_name="Dr Rao", working_days=list(range(1, 8)))
    url = f"/api/v1/ai/tools/rock8/{clinic.id}/book/?token={rock8_clinic_token(clinic.id)}"
    day = (timezone.localdate() + timezone.timedelta(days=2)).isoformat()
    base = {"doctor_id": doctor.id, "date": day, "time": "10:00"}
    bad_name = api_client.post(url, {**base, "patient_name": "Unknown Patient", "patient_phone": "9876543210"}, format="json")
    bad_phone = api_client.post(url, {**base, "patient_name": "Ravi Kumar", "patient_phone": "Unknown Phone"}, format="json")
    assert bad_name.data["error"] == "PATIENT_NAME_REQUIRED"
    assert bad_phone.data["error"] == "PATIENT_PHONE_REQUIRED"
    assert not Patient.objects.exists()


def test_web_call_patient_and_duration_come_from_the_provider_transcript(
    api_client, clinic, django_capture_on_commit_callbacks
):
    """Web calls have no caller number: the patient is the one the agent booked during the call."""
    from appointments.models import Appointment
    from doctors.models import Doctor
    from django.utils import timezone
    from ai_agent.tasks import fetch_call_recording

    links = {"recording_url": "https://cdn.example/rec.mp3", "transcript_url": "https://cdn.example/transcript.txt"}
    with mock.patch("ai_agent.call_sessions.recording_links", return_value=links):
        _post(api_client, clinic, {"room_name": "webcall-1"}, django_capture_on_commit_callbacks)
        patient = Patient.objects.create(clinic=clinic, full_name="Chinmaya", phone="7259414272")
        doctor = Doctor.objects.create(clinic=clinic, full_name="Dr Rao")
        start = timezone.now() + timezone.timedelta(days=1)
        appointment = Appointment.objects.create(clinic=clinic, doctor=doctor, patient=patient, starts_at=start,
                                                 ends_at=start + timezone.timedelta(minutes=15), source="phone")
        text = (
            "[23:59:50] AGENT: Welcome to Test Clinic.\n"
            "[23:59:55] USER: Book me with Dr Rao.\n"
            "[00:00:10] TOOL CALL: book_appointment {\"doctor_id\": \"1\"}\n"
            f"[00:00:11] TOOL RESULT: book_appointment (ok, 7.0ms) {{\"success\":true,\"appointment_id\":{appointment.id}}}\n"
            "[00:00:28] AGENT: You're booked. Goodbye."
        )

        def fake_get(url, **kwargs):
            response = _fake_get(url, **kwargs)
            if "transcript" in url:
                response.json.side_effect = ValueError
                response.text = text
            return response

        with mock.patch("ai_agent.call_events.requests.get", side_effect=fake_get):
            fetch_call_recording.apply(args=(CallLog.objects.get().id, "webcall-1", ""))

    log = CallLog.objects.get()
    assert (log.patient_id, log.outcome, log.duration_seconds) == (patient.id, "appointment_booked", 38)
    assert log.transcript == "AI: Welcome to Test Clinic.\nPatient: Book me with Dr Rao.\nAI: You're booked. Goodbye."
    assert transcript_duration("AI: Hi") == 0
