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


def test_unknown_caller_number_is_kept_on_the_log(api_client, clinic, django_capture_on_commit_callbacks):
    """A caller with no patient record is still identifiable by number, so the desk can call back."""
    with mock.patch("ai_agent.call_sessions.enqueue_recording_fetch"):
        res = _post(api_client, clinic, {"room_name": "room-77", "phone_number": "+919972623953", "metadata": {}},
                    django_capture_on_commit_callbacks)
    assert res.status_code == 200
    log = CallLog.objects.get(external_call_id="room-77")
    assert (log.patient_id, log.phone) == (None, "+919972623953")


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


class TestTestCalls:
    """Calls from the app's Test call button are logged as tests: labelled, and kept out of the analytics."""

    def _start(self, client, url, payload, capture):
        with mock.patch("ai_agent.call_sessions.recording_links", return_value={}), capture(execute=True):
            return client.post(url, data=json.dumps(payload), content_type="application/json")

    def test_web_call_hands_the_provider_the_test_webhook(self, admin_client, clinic, clinic_admin, settings):
        settings.ROCK8_API_KEY = "key"
        response = mock.Mock(**{"json.return_value": {"token": "lk-token"}})
        with mock.patch("ai_agent.views.requests.post", return_value=response) as started:
            res = admin_client.get("/api/v1/ai/web-call/start/", {"participant_identity": "voice_assistant_user_7"})
        assert res.status_code == 200
        webhook_url = started.call_args.kwargs["json"]["webhook_url"]
        assert webhook_url.endswith(f"/rock8/{clinic.id}/{rock8_clinic_token(clinic.id)}/test/")

    def test_call_through_the_test_webhook_is_logged_as_a_test(self, api_client, clinic, django_capture_on_commit_callbacks):
        url = f"/api/v1/ai/webhooks/rock8/{clinic.id}/{rock8_clinic_token(clinic.id)}/test/"
        res = self._start(api_client, url, {"room_name": "web-1"}, django_capture_on_commit_callbacks)
        assert res.status_code == 200 and "system_prompt" in res.data  # the agent still gets its setup
        assert CallLog.objects.get(external_call_id="web-1").is_test

    def test_test_caller_name_also_marks_a_test(self, api_client, clinic, django_capture_on_commit_callbacks):
        url = f"/api/v1/ai/webhooks/rock8/{clinic.id}/{rock8_clinic_token(clinic.id)}/"
        self._start(api_client, url, {"room_name": "web-2", "participant_identity": "voice_assistant_user_42"},
                    django_capture_on_commit_callbacks)
        self._start(api_client, url, {"room_name": "phone-1", "phone_number": "+919876543210",
                                      "participant_identity": "sip_+919876543210"}, django_capture_on_commit_callbacks)
        assert CallLog.objects.get(external_call_id="web-2").is_test
        assert not CallLog.objects.get(external_call_id="phone-1").is_test

    def test_tests_are_filterable_and_left_out_of_analytics(self, admin_client, clinic):
        from django.utils import timezone
        now = timezone.now()
        real = CallLog.objects.create(clinic=clinic, direction="inbound", occurred_at=now, outcome="appointment_booked")
        test = CallLog.objects.create(clinic=clinic, direction="inbound", occurred_at=now, outcome="appointment_booked", is_test=True)

        listed = lambda q: [r["id"] for r in admin_client.get(f"/api/v1/ai/call-logs/{q}").data["results"]]
        assert listed("?is_test=false") == [real.id]
        assert listed("?is_test=true") == [test.id]
        assert sorted(listed("")) == sorted([real.id, test.id])

        today = timezone.localdate().isoformat()
        summary = admin_client.get(f"/api/v1/reports/ai-analytics/?start={today}").data["summary"]
        assert (summary["total_calls"], summary["calls_with_booking"], summary["test_calls"]) == (1, 1, 1)
        with_tests = admin_client.get(f"/api/v1/reports/ai-analytics/?start={today}&include_tests=1").data["summary"]
        assert with_tests["total_calls"] == 2


class TestCallLinkedToAppointment:
    """A call is linked to the appointment its agent booked or cancelled (and so to that doctor)."""

    def _fetch(self, clinic, text):
        from ai_agent.call_events import fetch_media
        log = CallLog.objects.create(clinic=clinic, direction="inbound", occurred_at="2026-10-01T10:00:00Z", external_call_id="room-x")

        def fake_get(url, **kwargs):
            response = _fake_get(url, **kwargs)
            response.json.side_effect = ValueError
            response.text = text
            return response

        with mock.patch("ai_agent.call_events.requests.get", side_effect=fake_get):
            fetch_media(log.id, "https://cdn.example/transcript.txt", None)
        return CallLog.objects.get(id=log.id)

    def _appointment(self, clinic):
        from appointments.models import Appointment
        from doctors.models import Doctor
        from django.utils import timezone
        doctor = Doctor.objects.create(clinic=clinic, full_name="Anvi Pillai")
        patient = Patient.objects.create(clinic=clinic, full_name="Asha", phone="9876543210")
        start = timezone.now() + timezone.timedelta(days=1)
        return Appointment.objects.create(clinic=clinic, doctor=doctor, patient=patient, starts_at=start,
                                          ends_at=start + timezone.timedelta(minutes=15), source="phone")

    def test_booking_call(self, clinic):
        appt = self._appointment(clinic)
        log = self._fetch(clinic, "[10:00:00] AGENT: Hi\n"
                          f"[10:00:20] TOOL RESULT: book_appointment (ok, 5ms) {{\"success\":true,\"appointment_id\":{appt.id}}}\n"
                          "[10:00:30] AGENT: Booked")
        assert log.appointment_id == appt.id

    def test_cancellation_call(self, clinic, api_client):
        appt = self._appointment(clinic)
        # The cancel tool echoes the id so the transcript records it.
        res = api_client.post(f"/api/v1/ai/tools/rock8/{clinic.id}/cancel/?token={rock8_clinic_token(clinic.id)}",
                              {"appointment_id": appt.id}, format="json")
        assert res.data["success"] and res.data["appointment_id"] == appt.id
        log = self._fetch(clinic, "[10:00:00] AGENT: Hi\n"
                          f"[10:00:20] TOOL RESULT: cancel_appointment (ok, 5ms) {{\"success\":true,\"appointment_id\":{appt.id}}}\n"
                          "[10:00:30] AGENT: Cancelled")
        assert log.appointment_id == appt.id

    def test_failed_or_other_clinic_appointments_are_not_linked(self, clinic, clinic_b):
        from appointments.models import Appointment
        appt = self._appointment(clinic)
        other = Appointment.objects.create(clinic=clinic_b, doctor=appt.doctor, patient=appt.patient,
                                           starts_at=appt.starts_at, ends_at=appt.ends_at)
        log = self._fetch(clinic, f"[10:00:20] TOOL RESULT: book_appointment (ok, 5ms) {{\"success\":false,\"appointment_id\":{appt.id}}}\n"
                          f"[10:00:21] TOOL RESULT: book_appointment (ok, 5ms) {{\"success\":true,\"appointment_id\":{other.id}}}\n")
        assert log.appointment_id is None


class TestSummary:
    """Calls on the hospital line get no summary from the provider: one is written from the transcript."""

    @staticmethod
    def _transcript(text):
        def fake_get(url, **kwargs):
            response = mock.Mock(status_code=200)
            response.raise_for_status = lambda: None
            response.json.side_effect = ValueError
            response.text = text
            return response
        return fake_get

    def test_placeholder_summary_is_written_from_the_transcript(self, clinic):
        from django.utils import timezone
        from ai_agent.call_events import fetch_media
        log = CallLog.objects.create(clinic=clinic, direction="inbound", occurred_at=timezone.now(), outcome="in_progress",
                                     external_call_id="sum-1", summary="Call from +919876543210.")
        transcript = "AI: Welcome.\nPatient: Book me with Dr. Devika tomorrow.\nAI: Done, 10 AM."
        with mock.patch("ai_agent.call_events.requests.get", side_effect=self._transcript(transcript)), \
             mock.patch("ai_agent.call_events.summarize_transcript", return_value="Patient booked Dr. Devika for tomorrow at 10 AM.") as summarise, \
             mock.patch("ai_agent.call_events.rate_transcript", return_value="positive"):
            fetch_media(log.id, "https://cdn.example/transcript.txt", None)
        log.refresh_from_db()
        assert summarise.called
        assert log.summary == "Patient booked Dr. Devika for tomorrow at 10 AM."

    def test_a_real_summary_is_kept(self, clinic):
        from django.utils import timezone
        from ai_agent.call_events import fetch_media
        log = CallLog.objects.create(clinic=clinic, direction="inbound", occurred_at=timezone.now(), outcome="in_progress",
                                     external_call_id="sum-2", summary="Provider summary.")
        with mock.patch("ai_agent.call_events.requests.get", side_effect=self._transcript("AI: Hello.\nPatient: Hi.")), \
             mock.patch("ai_agent.call_events.summarize_transcript") as summarise, \
             mock.patch("ai_agent.call_events.rate_transcript", return_value=""):
            fetch_media(log.id, "https://cdn.example/transcript.txt", None)
        assert not summarise.called
        assert CallLog.objects.get(id=log.id).summary == "Provider summary."


class TestSentiment:
    """Caller sentiment: from the provider's analysis when sent, else rated from the transcript (if enabled)."""

    def _model_says(self, content):
        response = mock.Mock()
        response.json.return_value = {"choices": [{"message": {"content": content}}]}
        return mock.patch("ai_agent.sentiment.requests.post", return_value=response)

    def test_rating_is_off_without_a_key(self, settings):
        from ai_agent.sentiment import rate_transcript
        settings.SENTIMENT_API_KEY = ""
        with mock.patch("ai_agent.sentiment.requests.post") as post:
            assert rate_transcript("AI: Hi\nPatient: Thanks!") == ""
        post.assert_not_called()

    def test_rating_reads_the_models_answer(self, settings):
        from ai_agent.sentiment import rate_transcript
        settings.SENTIMENT_API_KEY = "sk-test"
        with self._model_says('```json\n{"sentiment": "Negative"}\n```') as post:
            assert rate_transcript("AI: Hi\nPatient: This is useless.") == "negative"
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer sk-test"
        assert "reasoning_effort" not in post.call_args.kwargs["json"]  # OpenAI rejects it on these models
        with self._model_says("not json"):
            assert rate_transcript("AI: Hi") == ""
        with self._model_says('{"sentiment": "ecstatic"}'):
            assert rate_transcript("AI: Hi") == ""

    def test_sarvam_answers_without_thinking(self, settings):
        from ai_agent.sentiment import rate_transcript
        settings.SENTIMENT_API_KEY = "sarvam-key"
        settings.SENTIMENT_API_BASE = "https://api.sarvam.ai/v1/"
        settings.SENTIMENT_MODEL = "sarvam-105b"
        with self._model_says('{"sentiment": "neutral"}') as post:
            assert rate_transcript("AI: Hi\nPatient: Ok.") == "neutral"
        assert post.call_args.args[0] == "https://api.sarvam.ai/v1/chat/completions"
        sent = post.call_args.kwargs["json"]
        assert (sent["model"], sent["reasoning_effort"]) == ("sarvam-105b", None)

    def test_provider_analysis_sentiment_is_kept(self, api_client, clinic, django_capture_on_commit_callbacks, settings):
        settings.SENTIMENT_API_KEY = "sk-test"
        payload = _event("call.analysis_completed", analysis={"summary": "Booked a visit.", "sentiment": "Positive"})
        with mock.patch("ai_agent.sentiment.requests.post") as post:
            _post(api_client, clinic, payload, django_capture_on_commit_callbacks)
        log = CallLog.objects.get(external_call_id=CALL_ID)
        assert (log.sentiment, log.summary) == ("positive", "Booked a visit.")
        post.assert_not_called()  # the provider already said; no second opinion

    def test_transcript_is_rated_and_placeholder_summary_written(self, clinic, settings):
        from ai_agent.call_events import fetch_media
        settings.SENTIMENT_API_KEY = "sk-test"
        log = CallLog.objects.create(clinic=clinic, direction="inbound", occurred_at="2026-10-01T10:00:00Z",
                                     summary="Call from +919876543210.")
        with self._model_says('{"sentiment": "positive"}'),              mock.patch("ai_agent.call_events.summarize_transcript", return_value="Booked a visit with Dr. Rao."),              mock.patch("ai_agent.call_events.requests.get", side_effect=_fake_get):
            fetch_media(log.id, "https://cdn.example/transcript.json", None)
        log.refresh_from_db()
        # The call-start placeholder ("Call from ...") is replaced by a summary written from the transcript.
        assert (log.sentiment, log.summary) == ("positive", "Booked a visit with Dr. Rao.")

    def test_filter_and_breakdown(self, admin_client, clinic):
        from django.utils import timezone
        now = timezone.now()
        for sentiment in ("positive", "positive", "negative", ""):
            CallLog.objects.create(clinic=clinic, direction="inbound", occurred_at=now, sentiment=sentiment)
        assert len(admin_client.get("/api/v1/ai/call-logs/?sentiment=positive").data["results"]) == 2
        assert len(admin_client.get("/api/v1/ai/call-logs/?sentiment=none").data["results"]) == 1
        today = timezone.localdate().isoformat()
        breakdown = admin_client.get(f"/api/v1/reports/ai-analytics/?start={today}").data["sentiment_breakdown"]
        assert breakdown == {"positive": 2, "neutral": 0, "negative": 1, "not_rated": 1}


class TestBackgroundPolling:
    """Without a task queue the fetch runs on a thread: it must wait between polls instead of
    burning all its attempts at once while the call is still going."""

    def test_poll_waits_until_the_transcript_exists(self, clinic):
        from django.utils import timezone
        from ai_agent.call_sessions import poll_recording
        log = CallLog.objects.create(clinic=clinic, direction="inbound", occurred_at=timezone.now(), outcome="in_progress",
                                     external_call_id="room-poll")
        links = {"recording_url": "https://cdn.example/rec.mp3", "transcript_url": "https://cdn.example/transcript.txt"}
        with mock.patch("ai_agent.call_sessions.recording_links", side_effect=[{}, {}, links]) as polled, \
             mock.patch("ai_agent.call_sessions.time.sleep") as sleep, \
             mock.patch("ai_agent.call_events.requests.get", side_effect=_fake_get):
            assert poll_recording(log.id, "room-poll", "", attempts=5, wait_seconds=7) is True
        assert polled.call_count == 3 and sleep.call_count == 2
        sleep.assert_called_with(7)
        assert CallLog.objects.get(id=log.id).outcome == ""  # media in, nothing worth tagging

    def test_poll_gives_up_after_its_attempts(self, clinic):
        from django.utils import timezone
        from ai_agent.call_sessions import poll_recording
        log = CallLog.objects.create(clinic=clinic, direction="inbound", occurred_at=timezone.now(), outcome="in_progress",
                                     external_call_id="room-none")
        with mock.patch("ai_agent.call_sessions.recording_links", return_value={}) as polled, \
             mock.patch("ai_agent.call_sessions.time.sleep") as sleep:
            assert poll_recording(log.id, "room-none", "", attempts=3, wait_seconds=1) is False
        assert polled.call_count == 3 and sleep.call_count == 2
        assert CallLog.objects.get(id=log.id).outcome == "no_recording"


class TestOutcomeClassification:
    """Calls on the hospital line get their outcome from what the agent's tools did, else from the transcript."""

    def _fetch(self, clinic, lines, classified=None):
        from ai_agent.call_events import fetch_media
        text = "\n".join(lines)
        log = CallLog.objects.create(clinic=clinic, direction="inbound", occurred_at="2026-10-01T10:00:00Z",
                                     external_call_id=f"room-{abs(hash(text)) % 10**6}", outcome="in_progress")

        def fake_get(url, **kwargs):
            response = _fake_get(url, **kwargs)
            response.json.side_effect = ValueError
            response.text = text
            return response

        with mock.patch("ai_agent.call_events.requests.get", side_effect=fake_get), \
             mock.patch("ai_agent.call_events.classify_transcript", return_value=classified) as classify, \
             mock.patch("ai_agent.call_events.rate_transcript", return_value=""), \
             mock.patch("ai_agent.call_events.summarize_transcript", return_value=""):
            fetch_media(log.id, "https://cdn.example/transcript.txt", None)
        return CallLog.objects.get(id=log.id), classify

    CANCEL_OK = '[10:00:20] TOOL RESULT: cancel_appointment (ok, 5ms) {"success":true,"appointment_id":901}'
    BOOK_OK = '[10:00:40] TOOL RESULT: book_appointment (ok, 5ms) {"success":true,"appointment_id":902}'

    def test_booked_and_cancelled_in_one_call_is_rescheduled(self, clinic):
        log, classify = self._fetch(clinic, ["[10:00:00] AGENT: Hi", "[10:00:05] USER: Move my visit.", self.CANCEL_OK,
                                             self.BOOK_OK, "[10:00:50] AGENT: Done"])
        assert log.outcome == "rescheduled" and not classify.called

    def test_cancel_only(self, clinic):
        log, _ = self._fetch(clinic, ["[10:00:00] AGENT: Hi", "[10:00:05] USER: Cancel it.", self.CANCEL_OK])
        assert log.outcome == "appointment_cancelled"

    def test_failed_tool_does_not_count(self, clinic):
        log, classify = self._fetch(clinic, ["[10:00:00] AGENT: Hi", "[10:00:05] USER: Book me.",
                                             '[10:00:20] TOOL RESULT: book_appointment (error, 5ms) {"success":false}'],
                                    classified="information_only")
        assert log.outcome == "information_only" and classify.called

    def test_information_and_callback_come_from_the_model(self, clinic):
        log, _ = self._fetch(clinic, ["[10:00:00] AGENT: Hi", "[10:00:05] USER: What time does OPD open?", "[10:00:10] AGENT: 9 AM."],
                             classified="information_only")
        assert log.outcome == "information_only"
        log, _ = self._fetch(clinic, ["[10:00:00] AGENT: Hi", "[10:00:05] USER: I need to talk to a nurse now.",
                                      "[10:00:10] AGENT: I will have staff call you."], classified="needs_callback")
        assert log.outcome == "needs_callback"

    def test_silent_caller_gets_no_tag_without_asking_the_model(self, clinic):
        log, classify = self._fetch(clinic, ["[10:00:00] AGENT: Welcome.", "[10:00:30] AGENT: Hello?"], classified="information_only")
        assert log.outcome == "" and not classify.called

    def test_model_failure_leaves_the_outcome_alone(self, clinic):
        log, _ = self._fetch(clinic, ["[10:00:00] AGENT: Hi", "[10:00:05] USER: Fees?"], classified=None)
        assert log.outcome == "in_progress"

    def test_outbound_calls_are_not_classified(self, clinic):
        from ai_agent.call_events import fetch_media
        log = CallLog.objects.create(clinic=clinic, direction="outbound", occurred_at="2026-10-01T10:00:00Z",
                                     external_call_id="room-out", outcome="in_progress")
        with mock.patch("ai_agent.call_events.requests.get", side_effect=_fake_get), \
             mock.patch("ai_agent.call_events.classify_transcript") as classify, \
             mock.patch("ai_agent.call_events.rate_transcript", return_value=""):
            fetch_media(log.id, "https://cdn.example/transcript.txt", None)
        assert not classify.called


class TestClassifier:
    def test_model_reply_is_mapped(self, settings):
        from ai_agent.sentiment import classify_transcript
        settings.SENTIMENT_API_KEY = "k"
        transcript = "AI: Hi" + "\n" + "Patient: Fees?"
        for reply, expected in [('{"outcome": "information"}', "information_only"), ('{"outcome":"callback"}', "needs_callback"),
                                ('{"outcome": "nothing"}', ""), ("garbage", None)]:
            with mock.patch("ai_agent.sentiment._chat", return_value=reply):
                assert classify_transcript(transcript) == expected
        settings.SENTIMENT_API_KEY = ""
        assert classify_transcript(transcript) is None
