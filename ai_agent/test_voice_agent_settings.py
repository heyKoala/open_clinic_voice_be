"""Admins choose the AI receptionist's greeting, instructions and models; calls then run with them."""
from __future__ import annotations

import json
from unittest import mock

import pytest

from ai_agent import providers
from ai_agent.agent_settings import DEFAULT_SYSTEM_PROMPT
from ai_agent.models import VoiceAgentSettings
from ai_agent.tool_views import rock8_clinic_token

pytestmark = pytest.mark.django_db

URL = "/api/v1/ai/voice-agent/"
CATALOG = {
    ("stt", "deepgram"): {"config_fields": {"model": "model", "language": "language"},
                          "models": [{"id": "nova-3", "languages": [{"code": "en"}, {"code": "hi"}]}]},
    ("llm", "openai"): {"config_fields": {"model": "model"}, "models": [{"id": "gpt-5.4"}]},
    ("tts", "sarvam"): {"config_fields": {"model": "model", "language": "target_language_code", "voice": "speaker"},
                        "models": [{"id": "bulbul:v3", "languages": [{"code": "en-IN"}, {"code": "hi-IN"}]}]},
    ("realtime", "gemini_live"): {"config_fields": {"model": "model", "voice": "voice"},
                                  "models": [{"id": "gemini-live", "voices": [{"id": "Puck"}]}]},
}
STANDARD = {
    "first_message": "Namaste, {clinic_name} here.",
    "system_prompt": "You are the receptionist of {clinic_name}. Be brief.",
    "model_type": "standard",
    "stt": {"provider": "deepgram", "model": "nova-3", "language": "hi"},
    "llm": {"provider": "openai", "model": "gpt-5.4", "temperature": 0.5},
    "tts": {"provider": "sarvam", "model": "bulbul:v3", "voice": "ritu", "language": "hi-IN"},
}


def _entry(kind, provider):
    if (kind, provider) not in CATALOG:
        raise providers.UnknownProvider(f"Unsupported {kind} provider: '{provider}'.")
    return CATALOG[(kind, provider)]


@pytest.fixture(autouse=True)
def catalog(settings):
    settings.ROCK8_WEBHOOK_SECRET = "test-secret"
    with mock.patch("ai_agent.providers.provider_entry", side_effect=_entry):
        yield


@pytest.fixture(autouse=True)
def team_login(clinic_admin):
    """Most tests change the models, which only our team's staff logins may do."""
    clinic_admin.is_staff = True
    clinic_admin.save(update_fields=["is_staff"])
    return clinic_admin


def _hospital_admin(user):
    user.is_staff = False
    user.save(update_fields=["is_staff"])


def _call_config(api_client, clinic):
    url = f"/api/v1/ai/webhooks/rock8/{clinic.id}/{rock8_clinic_token(clinic.id)}/"
    return api_client.post(url, data=json.dumps({}), content_type="application/json").data


def test_defaults_until_something_is_saved(admin_client, api_client, clinic):
    res = admin_client.get(URL)
    assert res.status_code == 200 and res.data["is_customised"] is False
    assert res.data["system_prompt"] == DEFAULT_SYSTEM_PROMPT and "config" not in res.data["tts"]
    assert (res.data["tts"]["provider"], res.data["llm"]["model"]) == ("sarvam", "gemini-3.1-flash-lite")

    config = _call_config(api_client, clinic)
    assert config["first_message"] == "Welcome to Test Clinic. How can I help you today?"
    assert config["model_type"] == "standard" and "realtime_config" not in config
    assert config["tts_config"] == {"provider": "sarvam", "config": {
        "model": "bulbul:v3", "speaker": "simran", "target_language_code": "en-IN"}}


def test_saved_settings_drive_the_call(admin_client, api_client, clinic):
    res = admin_client.put(URL, STANDARD, format="json")
    assert res.status_code == 200 and res.data["is_customised"] is True
    assert res.data["tts"] == {"provider": "sarvam", "model": "bulbul:v3", "voice": "ritu", "language": "hi-IN"}

    config = _call_config(api_client, clinic)
    assert config["first_message"] == "Namaste, Test Clinic here."
    assert config["system_prompt"].startswith("You are the receptionist of Test Clinic. Be brief.")
    # The system still adds what the admin cannot know: the date and the centres.
    assert "Today's date is" in config["system_prompt"] and "CENTRE: Test Clinic" in config["system_prompt"]
    assert config["stt_config"] == {"provider": "deepgram", "config": {"model": "nova-3", "language": "hi"}}
    assert config["llm_config"] == {"provider": "openai", "config": {"model": "gpt-5.4", "temperature": 0.5}}
    assert config["tts_config"] == {"provider": "sarvam", "config": {
        "model": "bulbul:v3", "speaker": "ritu", "target_language_code": "hi-IN"}}
    assert len(config["tools"]) == 5


def test_realtime_replaces_the_three_stage_pipeline(admin_client, api_client, clinic):
    admin_client.put(URL, STANDARD, format="json")
    res = admin_client.put(URL, {"model_type": "realtime", "realtime": {
        "provider": "gemini_live", "model": "gemini-live", "voice": "Puck"}}, format="json")
    assert res.status_code == 200
    # The standard choices are kept for switching back.
    assert res.data["tts"]["voice"] == "ritu"

    config = _call_config(api_client, clinic)
    assert config["model_type"] == "realtime"
    assert config["realtime_config"] == {"provider": "gemini_live", "config": {"model": "gemini-live", "voice": "Puck"}}
    assert not {"stt_config", "llm_config", "tts_config"} & set(config)


@pytest.mark.parametrize("change, field", [
    ({"tts": {"provider": "sarvam", "model": "bulbul:v3", "voice": ""}}, "tts"),
    ({"tts": {"provider": "sarvam", "model": "bulbul:v9", "voice": "ritu"}}, "tts"),
    ({"stt": {"provider": "deepgram", "model": "nova-3", "language": "ta"}}, "stt"),
    ({"stt": {"provider": "nope", "model": "x"}}, "stt"),
    ({"llm": {"provider": "openai", "model": "gpt-5.4", "temperature": 9}}, "llm"),
    ({"model_type": "turbo"}, "model_type"),
])
def test_invalid_choices_are_rejected(admin_client, clinic, change, field):
    res = admin_client.put(URL, {**STANDARD, **change}, format="json")
    assert res.status_code == 400 and field in res.data
    assert not VoiceAgentSettings.objects.exists()


def test_reset_returns_to_the_defaults(admin_client, clinic):
    admin_client.put(URL, STANDARD, format="json")
    res = admin_client.delete(URL)
    assert res.status_code == 200 and res.data["is_customised"] is False
    assert res.data["system_prompt"] == DEFAULT_SYSTEM_PROMPT


def test_only_admins_can_read_or_change_the_agent(receptionist_client, api_client, clinic):
    assert receptionist_client.get(URL).status_code == 403
    assert receptionist_client.put(URL, STANDARD, format="json").status_code == 403
    assert api_client.get(URL).status_code in (401, 403)
    assert receptionist_client.get(URL + "providers/?type=tts").status_code == 403


def test_catalog_is_proxied_with_the_server_side_key(admin_client, clinic, settings):
    settings.ROCK8_API_KEY = "server-key"
    upstream = mock.MagicMock(status_code=200, ok=True)
    upstream.json.return_value = {"type": "tts", "provider": "sarvam", "voices": [{"id": "ritu", "label": "Ritu"}]}
    with mock.patch("ai_agent.providers.requests.get", return_value=upstream) as get:
        res = admin_client.get(URL + "voices/?provider=sarvam&model=bulbul:v3")
        again = admin_client.get(URL + "voices/?provider=sarvam&model=bulbul:v3")
    assert res.status_code == 200 and res.data["voices"][0]["id"] == "ritu" and again.data == res.data
    assert get.call_count == 1  # cached
    assert get.call_args.kwargs["headers"]["X-API-Key"] == "server-key"
    assert get.call_args.kwargs["params"] == {"model": "bulbul:v3"}
    assert admin_client.get(URL + "providers/?type=video").status_code == 400


# ---------------------------------------------------------------------------------------------
# Call audio and listening: noise cancellation, recording notice, listening preset, silence limit.
# ---------------------------------------------------------------------------------------------

def _inbound_ok():
    response = mock.Mock(status_code=200, text="")
    response.json.return_value = {"ok": True}
    return response


def test_call_audio_defaults_are_sent_with_every_call(admin_client, api_client, clinic, settings):
    settings.ROCK8_INBOUND_TRUNK_ID = settings.ROCK8_INBOUND_RULE_ID = ""  # not from the developer's .env
    res = admin_client.get(URL)
    assert res.data["noise_cancellation"] is True and res.data["recording_disclosure"] is False
    assert res.data["listening"] == "balanced" and res.data["max_silence_seconds"] == 60
    assert [o["id"] for o in res.data["listening_options"]] == ["patient", "balanced", "quick"]
    assert res.data["inbound_number"] == {"configured": False}

    config = _call_config(api_client, clinic)
    assert config["max_silence_seconds"] == 60 and config["thinking_sound"] is True
    assert config["vad_config"]["min_silence_duration"] == 0.45
    # Before the hang-up: "Are you still there?" at 10 s and 20 s, then the goodbye.
    assert (config["inactivity_timeout"], config["inactivity_max_prompts"]) == (10, 2)
    assert config["inactivity_message"] == "Are you still there?"
    assert config["inactivity_end_message"] == "I'll end the call now since I haven't heard from you. Goodbye!"


def test_saved_inactivity_check_is_sent_with_every_call(admin_client, api_client, clinic, settings):
    settings.ROCK8_INBOUND_TRUNK_ID = settings.ROCK8_INBOUND_RULE_ID = ""
    body = {**STANDARD, "inactivity_timeout": 15, "inactivity_max_prompts": 1,
            "inactivity_message": "  Hello, can you hear me? ", "inactivity_end_message": "Please call us back. Bye!"}
    res = admin_client.put(URL, data=json.dumps(body), content_type="application/json")
    assert res.status_code == 200, res.data
    config = _call_config(api_client, clinic)
    assert (config["inactivity_timeout"], config["inactivity_max_prompts"]) == (15, 1)
    assert (config["inactivity_message"], config["inactivity_end_message"]) == ("Hello, can you hear me?", "Please call us back. Bye!")


# ---------------------------------------------------------------------------------------------
# Models are our team's (staff logins); the hospital's admins only pick the voice.
# ---------------------------------------------------------------------------------------------

def test_only_staff_are_told_they_can_change_models(admin_client, clinic_admin):
    assert admin_client.get(URL).data["can_change_models"] is True
    assert admin_client.get("/api/v1/accounts/me/").data["is_staff"] is True
    _hospital_admin(clinic_admin)
    assert admin_client.get(URL).data["can_change_models"] is False
    assert admin_client.get("/api/v1/accounts/me/").data["is_staff"] is False


def test_a_hospital_admin_changes_the_voice_but_not_the_models(admin_client, api_client, clinic, clinic_admin):
    assert admin_client.put(URL, data=json.dumps(STANDARD), content_type="application/json").status_code == 200
    _hospital_admin(clinic_admin)
    sneaky = {**STANDARD, "first_message": "Hello from {clinic_name}.",
              "llm": {"provider": "openai", "model": "gpt-5.4", "temperature": 1.5},
              "tts": {"provider": "sarvam", "model": "bulbul:v3", "voice": "anushka", "language": "en-IN"}}
    res = admin_client.put(URL, data=json.dumps(sneaky), content_type="application/json")
    assert res.status_code == 200, res.data
    # The voice and the greeting changed; the language model's temperature and the speaking language did not.
    assert res.data["tts"]["voice"] == "anushka" and res.data["tts"]["language"] == "hi-IN"
    assert res.data["llm"]["temperature"] == 0.5 and res.data["first_message"] == "Hello from {clinic_name}."
    config = _call_config(api_client, clinic)
    assert config["tts_config"]["config"]["speaker"] == "anushka" and config["llm_config"]["config"]["temperature"] == 0.5


def test_a_hospital_admin_cannot_switch_to_realtime(admin_client, clinic_admin):
    _hospital_admin(clinic_admin)
    res = admin_client.put(URL, data=json.dumps({**STANDARD, "model_type": "realtime",
                                                 "realtime": {"provider": "gemini_live", "model": "gemini-live", "voice": "Puck"}}),
                           content_type="application/json")
    assert res.status_code == 200 and res.data["model_type"] == "standard"


def test_a_hospital_admins_reset_keeps_the_models(admin_client, clinic, clinic_admin):
    assert admin_client.put(URL, data=json.dumps({**STANDARD, "max_silence_seconds": 90}),
                            content_type="application/json").status_code == 200
    _hospital_admin(clinic_admin)
    res = admin_client.delete(URL)
    assert res.status_code == 200
    assert res.data["max_silence_seconds"] == 60 and res.data["first_message"] != STANDARD["first_message"]
    assert (res.data["llm"]["model"], res.data["tts"]["voice"]) == ("gpt-5.4", "ritu")


# ---------------------------------------------------------------------------------------------
# Answering hours: off by default (the hospital's phone system forwards the calls it wants answered).
# ---------------------------------------------------------------------------------------------

def _at(hour, minute=0):
    """The hospital's local time today at hour:minute."""
    from django.utils import timezone
    return timezone.localtime().replace(hour=hour, minute=minute, second=0, microsecond=0)


NIGHTS = {**STANDARD, "answer_hours_only": True, "answer_from": "20:00", "answer_until": "08:00"}


def test_calls_are_answered_at_any_hour_by_default(admin_client, api_client, clinic):
    res = admin_client.get(URL)
    assert (res.data["answer_hours_only"], res.data["answer_from"], res.data["answer_until"]) == (False, "20:00", "08:00")
    assert res.data["answering_now"] is True
    with mock.patch("django.utils.timezone.now", return_value=_at(14)):
        assert _call_config(api_client, clinic)["tools"]


@pytest.mark.parametrize("hour, minute, answered", [
    (20, 0, True), (23, 30, True), (2, 0, True), (7, 59, True),   # 8 PM to 8 AM, across midnight
    (8, 0, False), (14, 0, False), (19, 59, False),
])
def test_only_the_set_hours_are_answered(admin_client, api_client, clinic, hour, minute, answered):
    assert admin_client.put(URL, data=json.dumps(NIGHTS), content_type="application/json").status_code == 200
    with mock.patch("django.utils.timezone.now", return_value=_at(hour, minute)):
        config = _call_config(api_client, clinic)
    assert bool(config["tools"]) is answered
    if not answered:
        assert config["first_message"] == (f"Thank you for calling {clinic.name}. Our AI assistant answers calls from "
                                           "8 PM to 8 AM. Please call the hospital during the day.")
        assert (config["inactivity_max_prompts"], config["inactivity_end_message"]) == (0, "Goodbye!")


def test_daytime_hours_and_a_custom_closed_message(admin_client, api_client, clinic):
    body = {**NIGHTS, "answer_from": "09:30", "answer_until": "18:00", "closed_message": "We are closed. Call back at 9:30."}
    res = admin_client.put(URL, data=json.dumps(body), content_type="application/json")
    assert res.status_code == 200 and res.data["answer_from"] == "09:30"
    with mock.patch("django.utils.timezone.now", return_value=_at(9, 30)):
        assert _call_config(api_client, clinic)["tools"]
    with mock.patch("django.utils.timezone.now", return_value=_at(18)):
        assert _call_config(api_client, clinic)["first_message"] == "We are closed. Call back at 9:30."


def test_test_calls_are_answered_outside_the_hours(admin_client, api_client, clinic):
    assert admin_client.put(URL, data=json.dumps(NIGHTS), content_type="application/json").status_code == 200
    url = f"/api/v1/ai/webhooks/rock8/{clinic.id}/{rock8_clinic_token(clinic.id)}/test/"
    with mock.patch("django.utils.timezone.now", return_value=_at(14)):
        config = api_client.post(url, data=json.dumps({}), content_type="application/json").data
    assert config["tools"]


@pytest.mark.parametrize("change, field", [
    ({"answer_from": "25:00"}, "answer_from"),
    ({"answer_until": "20:00"}, "answer_until"),   # same as the start
    ({"closed_message": "x" * 301}, "closed_message"),
])
def test_invalid_answering_hours_are_rejected(admin_client, clinic, change, field):
    res = admin_client.put(URL, data=json.dumps({**NIGHTS, **change}), content_type="application/json")
    assert res.status_code == 400 and field in res.data


def test_the_goodbye_must_come_before_the_hang_up(admin_client, clinic):
    # 30 s x (2 check-ins + the goodbye) = 90 s, but the line hangs up at 60 s.
    res = admin_client.put(URL, data=json.dumps({**STANDARD, "inactivity_timeout": 30}), content_type="application/json")
    assert res.status_code == 400 and "90 s" in res.data["inactivity_timeout"]
    ok = {**STANDARD, "inactivity_timeout": 30, "max_silence_seconds": 120}
    assert admin_client.put(URL, data=json.dumps(ok), content_type="application/json").status_code == 200


def test_saved_call_audio_changes_the_call_config_and_is_pushed_to_the_number(admin_client, api_client, clinic, settings):
    settings.ROCK8_API_KEY, settings.ROCK8_INBOUND_TRUNK_ID, settings.ROCK8_INBOUND_RULE_ID = "k", "ST_in", "SDR_in"
    settings.VOICE_WORKER_URL = "https://worker.example.com"
    body = {**STANDARD, "noise_cancellation": True, "noise_cancellation_level": 20, "recording_disclosure": True,
            "listening": "patient", "max_silence_seconds": 45, "thinking_sound": False}
    with mock.patch("ai_agent.inbound.requests.put", return_value=_inbound_ok()) as put:
        res = admin_client.put(URL, data=json.dumps(body), content_type="application/json")
    assert res.status_code == 200, res.data
    assert res.data["inbound_applied"] == {"ok": True, "detail": "Applied to calls on the hospital number."}
    assert put.call_args.args[0] == "https://worker.example.com/inbound/update"
    assert put.call_args.kwargs["json"] == {"sip_trunk_id": "ST_in", "dispatch_rule_id": "SDR_in", "noise_cancellation": True,
                                            "noise_cancellation_level": 20, "recording_disclosure": True}

    config = _call_config(api_client, clinic)
    assert (config["max_silence_seconds"], config["thinking_sound"]) == (45, False)
    assert config["vad_config"]["min_silence_duration"] == 0.8

    # Saving without changing the inbound flags (model, voice, prompt, silence limit) leaves the number alone.
    with mock.patch("ai_agent.inbound.requests.put") as put:
        res = admin_client.put(URL, data=json.dumps({**body, "max_silence_seconds": 50}), content_type="application/json")
    assert res.status_code == 200 and "inbound_applied" not in res.data and not put.called


def test_noise_cancellation_off_drops_the_level_and_warns_without_inbound_ids(admin_client, clinic, settings):
    settings.ROCK8_INBOUND_TRUNK_ID = settings.ROCK8_INBOUND_RULE_ID = ""
    body = {**STANDARD, "noise_cancellation": False, "noise_cancellation_level": 40}
    with mock.patch("ai_agent.inbound.requests.put") as put:
        res = admin_client.put(URL, data=json.dumps(body), content_type="application/json")
    assert res.status_code == 200 and not put.called
    assert res.data["inbound_applied"]["ok"] is False and "ROCK8_INBOUND_TRUNK_ID" in res.data["inbound_applied"]["detail"]
    from ai_agent.agent_settings import call_flags
    assert call_flags(clinic) == {"noise_cancellation": False, "recording_disclosure": False}
    assert call_flags(clinic, for_web_call=True) == {"noise_cancellation": False}


@pytest.mark.parametrize("change, field", [
    ({"noise_cancellation_level": 99}, "noise_cancellation_level"),
    ({"noise_cancellation_level": "loud"}, "noise_cancellation_level"),
    ({"listening": "eager"}, "listening"),
    ({"max_silence_seconds": 5}, "max_silence_seconds"),
    ({"thinking_sound": "yes"}, "thinking_sound"),
    ({"inactivity_timeout": 2}, "inactivity_timeout"),
    ({"inactivity_max_prompts": 9}, "inactivity_max_prompts"),
    ({"inactivity_message": "   "}, "inactivity_message"),
    ({"inactivity_end_message": "x" * 201}, "inactivity_end_message"),
])
def test_invalid_call_audio_is_rejected(admin_client, clinic, change, field):
    res = admin_client.put(URL, data=json.dumps({**STANDARD, **change}), content_type="application/json")
    assert res.status_code == 400 and field in res.data


def test_a_save_without_call_audio_fields_keeps_them(admin_client, clinic, settings):
    """The original front end only sends greeting, prompt and models: the call audio settings must survive."""
    settings.ROCK8_INBOUND_TRUNK_ID = settings.ROCK8_INBOUND_RULE_ID = ""
    tuned = {**STANDARD, "noise_cancellation_level": 20, "recording_disclosure": True, "listening": "patient",
             "max_silence_seconds": 90, "thinking_sound": False, "inactivity_timeout": 20, "inactivity_message": "Hello?"}
    assert admin_client.put(URL, data=json.dumps(tuned), content_type="application/json").status_code == 200
    res = admin_client.put(URL, data=json.dumps(STANDARD), content_type="application/json")
    assert res.status_code == 200
    saved = VoiceAgentSettings.objects.get(clinic=clinic)
    assert (saved.noise_cancellation_level, saved.recording_disclosure, saved.listening, saved.max_silence_seconds,
            saved.thinking_sound, saved.inactivity_timeout, saved.inactivity_message) == (20, True, "patient", 90, False, 20, "Hello?")


def test_instructions_limit_is_a_setting_and_is_shown_on_the_page(admin_client, clinic, settings):
    """The limit can be raised without a release; the page gets it, and the soft 'long' mark, to show a count."""
    settings.AGENT_PROMPT_MAX_CHARS = 30000
    limits = admin_client.get(URL).data["limits"]
    assert limits == {"system_prompt": 30000, "system_prompt_long": 25000}

    long_prompt = "Be brief. " * 2800  # 28,000 characters: over the soft mark, under the limit
    assert admin_client.put(URL, {**STANDARD, "system_prompt": long_prompt}, format="json").status_code == 200

    res = admin_client.put(URL, {**STANDARD, "system_prompt": long_prompt + "x" * 3000}, format="json")
    assert res.status_code == 400 and "30000" in str(res.data["system_prompt"])

    settings.AGENT_PROMPT_MAX_CHARS = 40000
    assert admin_client.put(URL, {**STANDARD, "system_prompt": long_prompt + "x" * 3000}, format="json").status_code == 200


def test_greeting_has_no_length_limit(admin_client, clinic):
    res = admin_client.put(URL, {**STANDARD, "first_message": "Welcome to {clinic_name}. " * 100}, format="json")
    assert res.status_code == 200
