"""The AI places reminder and feedback calls: dialling, the call's script, the outcome tool, retries."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from unittest import mock

import pytest
from django.utils import timezone

from accounts.models import User
from ai_agent.models import CallLog
from ai_agent.tool_views import rock8_path
from appointments import journey, outbound
from appointments.models import Appointment, PatientCall
from clinics.models import ClinicPhoneNumber
from doctors.models import Doctor
from patients.models import Patient

pytestmark = pytest.mark.django_db
Kind, Status = PatientCall.Kind, PatientCall.Status


@pytest.fixture()
def voice(settings, clinic):
    settings.ROCK8_API_KEY = "rock8_test"
    settings.ROCK8_WEBHOOK_SECRET = "secret"
    settings.ROCK8_SIP_TRUNK_ID = "ST_test"
    settings.PUBLIC_BASE_URL = "https://clinic.example.com"
    settings.VOICE_WORKER_URL = "https://worker.example.com"
    ClinicPhoneNumber.objects.create(clinic=clinic, number="918031150501", status=ClinicPhoneNumber.Status.ACTIVE)
    return settings


@pytest.fixture()
def appointment(clinic):
    user = User.objects.create_user(clinic=clinic, email="dr@e.com", password="p", role=User.Role.DOCTOR)
    doctor = Doctor.objects.create(clinic=clinic, user=user, full_name="Asha Rao")
    patient = Patient.objects.create(clinic=clinic, full_name="Ravi Kumar", phone="98000 00001")
    starts = timezone.make_aware(datetime.combine(timezone.localdate() + timedelta(days=3), time(15)))
    return Appointment.objects.create(clinic=clinic, doctor=doctor, patient=patient, starts_at=starts,
                                      ends_at=starts + timedelta(minutes=15), reason="Follow-up scan")


def _reminder(appointment):
    return PatientCall.objects.get(appointment=appointment, kind=Kind.REMINDER)


def _dialled(room="room_abc"):
    response = mock.Mock(status_code=200, text="")
    response.json.return_value = {"results": [{"room_name": room, "sip_call_id": "SCL_1"}]}
    return response


def test_missing_setup_is_explained(settings, clinic, appointment, admin_client):
    settings.PUBLIC_BASE_URL = ""  # not from the developer's .env
    settings.ROCK8_SIP_TRUNK_ID = ""
    assert "ROCK8_SIP_TRUNK_ID" in outbound.missing_setup("https://x.example.com")
    settings.ROCK8_SIP_TRUNK_ID, settings.ROCK8_API_KEY, settings.ROCK8_WEBHOOK_SECRET = "ST_x", "k", "s"
    assert "local address" in outbound.missing_setup("http://localhost:8001")
    settings.ROCK8_SIP_TRUNK_ID = ""
    assert "ROCK8_SIP_TRUNK_ID" in admin_client.get("/api/v1/appointments/patient-calls/settings/").data["ai_calls_problem"]
    settings.ROCK8_SIP_TRUNK_ID = "ST_x"
    res = admin_client.post(f"/api/v1/appointments/patient-calls/{_reminder(appointment).id}/call-now/")
    assert res.status_code == 400 and "phone number" in res.data["detail"]  # the hospital has no number yet


def test_place_call_dials_through_the_trunk(voice, appointment):
    call = _reminder(appointment)
    with mock.patch("appointments.outbound.requests.post", return_value=_dialled()) as post:
        outbound.place_call(call)
    url, payload = post.call_args.args[0], post.call_args.kwargs["json"]
    assert url == "https://worker.example.com/call"
    assert post.call_args.kwargs["headers"]["X-API-Key"] == "rock8_test"
    assert payload["phone_numbers"] == ["+919800000001"] and payload["from_number"] == "+918031150501"
    assert payload["sip_trunk_id"] == "ST_test"
    assert payload["webhook_url"] == (f"https://clinic.example.com/api/v1/ai/webhooks/rock8/"
                                      f"{rock8_path(appointment.clinic_id, 'patient-call', str(call.id))}")
    call.refresh_from_db()
    assert (call.status, call.room_name) == (Status.CALLING, "room_abc")

    with pytest.raises(outbound.DialError):
        outbound.place_call(call)  # already being called


def test_call_script_and_outcome_tool(voice, appointment, api_client):
    call = _reminder(appointment)
    with mock.patch("appointments.outbound.requests.post", return_value=_dialled()):
        outbound.place_call(call)
    path = f"/api/v1/ai/webhooks/rock8/{rock8_path(appointment.clinic_id, 'patient-call', str(call.id))}"

    config = api_client.post(path, {"room_name": "room_abc", "phone_number": "+919800000001"}, format="json").data
    assert "Ravi Kumar" in config["first_message"]
    assert "Dr. Asha Rao" in config["system_prompt"] and f"appointment_id is {appointment.id}" in config["system_prompt"]
    tools = {t["name"]: t for t in config["tools"]}
    assert set(tools) == {"record_call_outcome", "get_available_slots", "book_appointment", "cancel_appointment"}
    log = CallLog.objects.get(external_call_id="room_abc")
    assert (log.direction, log.appointment_id, log.patient_id) == ("outbound", appointment.id, appointment.patient_id)

    outcome_url = tools["record_call_outcome"]["url"].split("clinic.example.com", 1)[-1].replace("http://testserver", "")
    outcome_path = outcome_url[outcome_url.index("/api/v1/"):]
    assert api_client.post(outcome_path.replace(rock8_path(appointment.clinic_id), f"{appointment.clinic_id}/wrong/"),
                           {"outcome": "confirmed"}, format="json").status_code == 403
    assert api_client.post(outcome_path, {"outcome": "maybe"}, format="json").data["success"] is False
    res = api_client.post(outcome_path, {"outcome": "confirmed"}, format="json")
    assert res.data["success"] is True
    call.refresh_from_db()
    assert (call.status, call.result, call.call_log_id) == (Status.DONE, "confirmed", log.id)


def test_hospital_edits_the_script_but_not_how_outcomes_are_saved(voice, appointment, admin_client, api_client):
    res = admin_client.put("/api/v1/appointments/patient-calls/settings/", {
        "reminder_first_message": "Namaskara! {hospital_name} here, is this {patient_name}?",
        "reminder_script": "Remind {patient_name} about Dr. {doctor_name} on {appointment_time}. Speak Kannada first.",
    }, format="json")
    assert res.status_code == 200
    assert res.data["script_defaults"]["reminder"]["script"].startswith("You are the AI receptionist")
    assert "{patient_name}" in res.data["script_placeholders"] or "patient_name" in res.data["script_placeholders"]

    call = _reminder(appointment)
    config = api_client.post(f"/api/v1/ai/webhooks/rock8/{rock8_path(appointment.clinic_id, 'patient-call', str(call.id))}",
                             {"room_name": "room_s"}, format="json").data
    assert config["first_message"] == "Namaskara! Test Clinic here, is this Ravi Kumar?"
    assert config["system_prompt"].startswith("Remind Ravi Kumar about Dr. Asha Rao on ")
    assert "Speak Kannada first." in config["system_prompt"]
    assert "record_call_outcome exactly once" in config["system_prompt"]  # added by the system regardless

    # Saving the default text back stores nothing, so the default keeps applying.
    default = res.data["script_defaults"]["reminder"]["script"]
    res = admin_client.put("/api/v1/appointments/patient-calls/settings/", {"reminder_script": default}, format="json")
    assert res.data["reminder_script"] == ""


def test_feedback_call_records_the_rating(voice, appointment, api_client):
    appointment.status = Appointment.Status.COMPLETED
    appointment.save()
    call = PatientCall.objects.get(appointment=appointment, kind=Kind.FEEDBACK)
    config = api_client.post(f"/api/v1/ai/webhooks/rock8/{rock8_path(appointment.clinic_id, 'patient-call', str(call.id))}",
                             {"room_name": "room_fb"}, format="json").data
    assert [t["name"] for t in config["tools"]] == ["record_call_outcome"]
    assert "rate it from 1" in config["system_prompt"]
    res = api_client.post(f"/api/v1/ai/tools/rock8/{rock8_path(appointment.clinic_id, 'patient-call', str(call.id), 'outcome')}",
                          {"outcome": "rated", "rating": "2", "comment": "Waited an hour"}, format="json")
    assert res.data["success"] is True
    call.refresh_from_db()
    assert (call.rating, call.comment) == (2, "Waited an hour")
    assert journey.needs(call, journey.settings_for(appointment.clinic)) == "unhappy"


def test_dispatcher_only_calls_when_switched_on_and_in_hours(voice, appointment):
    call = _reminder(appointment)
    noon = timezone.make_aware(datetime.combine(timezone.localdate(), time(12)))
    PatientCall.objects.filter(id=call.id).update(due_at=noon - timedelta(minutes=5))
    with mock.patch("appointments.outbound.requests.post", return_value=_dialled()) as post:
        assert outbound.dispatch_due(noon) == 0  # off by default
        settings = journey.settings_for(appointment.clinic)
        settings.ai_calls_enabled = True
        settings.save()
        assert outbound.dispatch_due(noon.replace(hour=22)) == 0  # outside calling hours
        assert outbound.dispatch_due(noon) == 1
    assert post.call_count == 1
    call.refresh_from_db()
    assert call.status == Status.CALLING


def test_unanswered_ai_call_is_retried_then_left_for_the_desk(voice, appointment):
    call = _reminder(appointment)
    start = timezone.now()
    for attempt in (1, 2):
        PatientCall.objects.filter(id=call.id).update(status=Status.SCHEDULED)
        call.refresh_from_db()
        with mock.patch("appointments.outbound.requests.post", return_value=_dialled(f"room_{attempt}")):
            outbound.place_call(call, now=start)
        assert outbound.sweep_stale(start + timedelta(minutes=25)) == 1
        call.refresh_from_db()
    assert (call.status, call.attempts) == (Status.NO_ANSWER, 2)
    assert journey.needs(call, journey.settings_for(appointment.clinic)) == "call_back"


def test_dialling_failure_hands_the_call_to_the_desk(voice, appointment):
    call = _reminder(appointment)
    noon = timezone.make_aware(datetime.combine(timezone.localdate(), time(12)))
    PatientCall.objects.filter(id=call.id).update(due_at=noon - timedelta(minutes=1))
    settings = journey.settings_for(appointment.clinic)
    settings.ai_calls_enabled = True
    settings.save()
    refused = mock.Mock(status_code=402, text="no credits")
    with mock.patch("appointments.outbound.requests.post", return_value=refused):
        assert outbound.dispatch_due(noon) == 0
    call.refresh_from_db()
    assert call.attempts == 1 and "HTTP 402" in call.comment


def test_outbound_call_carries_the_call_audio_settings(voice, appointment, clinic):
    from ai_agent.models import VoiceAgentSettings
    VoiceAgentSettings.objects.create(clinic=clinic, noise_cancellation=True, noise_cancellation_level=25,
                                      recording_disclosure=True, listening="quick", max_silence_seconds=90, thinking_sound=False)
    call = _reminder(appointment)
    with mock.patch("appointments.outbound.requests.post", return_value=_dialled()) as post:
        outbound.place_call(call)
    payload = post.call_args.kwargs["json"]
    assert (payload["noise_cancellation"], payload["noise_cancellation_level"], payload["recording_disclosure"]) == (True, 25, True)

    config = outbound.build_config(call, "https://clinic.example.com")
    assert config["vad_config"]["min_silence_duration"] == 0.25 and config["thinking_sound"] is False
    assert config["max_silence_seconds"] == 30  # capped for outbound calls
