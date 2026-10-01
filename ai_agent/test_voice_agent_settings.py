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
    ({"first_message": "x" * 501}, "first_message"),
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
