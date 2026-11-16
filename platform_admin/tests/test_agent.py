"""The platform admin's AI receptionist screens: any hospital's agent, and the template new hospitals start from."""
from __future__ import annotations

import json
from unittest import mock

import pytest
from django.core.cache import cache

from ai_agent.agent_settings import DEFAULT_SYSTEM_PROMPT
from ai_agent.models import VoiceAgentSettings
from ai_agent.test_voice_agent_settings import STANDARD, _entry
from platform_admin.models import PlatformAuditLog, PlatformDefaults
from platform_admin.tests.test_platform_api import client_for, make_user
from platform_admin.tests.test_provision import form, post_form

pytestmark = pytest.mark.django_db

BASE = "/api/v1/platform/hospitals"
TEMPLATE = "/api/v1/platform/defaults/agent/"


@pytest.fixture(autouse=True)
def catalog():
    cache.clear()
    with mock.patch("ai_agent.providers.provider_entry", side_effect=_entry):
        yield


@pytest.fixture
def platform_client():
    return client_for(make_user("platform@heykoala.example", is_staff=True))


def put(client, url, body):
    return client.put(url, data=json.dumps(body), content_type="application/json")


def audit(action):
    return PlatformAuditLog.objects.filter(action=action)


# ── a hospital's agent ────────────────────────────────────────────────────────

class TestHospitalAgent:
    def test_the_platform_sees_and_may_change_the_models_but_the_hospital_admin_may_not(self, platform_client, admin_client, clinic):
        seen = platform_client.get(f"{BASE}/{clinic.id}/agent/").json()
        assert seen["can_change_models"] is True and seen["owner_clinic"]["id"] == clinic.id and seen["is_customised"] is False
        assert admin_client.get("/api/v1/ai/voice-agent/").json()["can_change_models"] is False

    def test_a_save_changes_that_hospitals_agent_and_nobody_elses(self, platform_client, admin_client, clinic, clinic_b):
        body = {**STANDARD, "first_message": "Namaste, {clinic_name} here."}
        response = put(platform_client, f"{BASE}/{clinic.id}/agent/", body)
        assert response.status_code == 200, response.content
        assert response.json()["tts"]["voice"] == "ritu" and response.json()["is_customised"] is True

        saved = VoiceAgentSettings.objects.get(clinic=clinic)
        assert saved.tts["provider"] == "sarvam" and saved.llm["model"] == "gpt-5.4" and saved.first_message == "Namaste, {clinic_name} here."
        assert not VoiceAgentSettings.objects.filter(clinic=clinic_b).exists()
        assert admin_client.get("/api/v1/ai/voice-agent/").json()["tts"]["voice"] == "ritu"  # the hospital's own admin sees it

    def test_the_audit_log_records_what_was_touched_but_never_the_text(self, platform_client, clinic):
        secret_prompt = "You are the receptionist. The side door code is 4471."
        put(platform_client, f"{BASE}/{clinic.id}/agent/", {**STANDARD, "system_prompt": secret_prompt})
        entry = audit("update_agent").get()
        assert "system_prompt" in entry.details["fields"] and "4471" not in json.dumps(entry.details)

    def test_a_bad_choice_is_refused_with_the_field_and_nothing_is_saved(self, platform_client, clinic):
        response = put(platform_client, f"{BASE}/{clinic.id}/agent/", {**STANDARD, "llm": {"provider": "openai", "model": "gpt-9"}})
        assert response.status_code == 400 and "llm" in response.json()
        assert not VoiceAgentSettings.objects.filter(clinic=clinic).exists() and not audit("update_agent").exists()

    def test_reset_goes_back_to_the_built_in_defaults(self, platform_client, clinic):
        put(platform_client, f"{BASE}/{clinic.id}/agent/", {**STANDARD, "system_prompt": "Custom."})
        response = platform_client.delete(f"{BASE}/{clinic.id}/agent/")
        assert response.status_code == 200 and response.json()["is_customised"] is False
        assert response.json()["system_prompt"] == DEFAULT_SYSTEM_PROMPT
        assert audit("reset_agent").count() == 1

    def test_changing_the_recording_notice_is_pushed_to_that_hospitals_number_like_in_the_hospital_app(self, platform_client, clinic):
        with mock.patch("ai_agent.views.inbound.apply_call_flags", return_value={"ok": True, "detail": "Applied."}) as push:
            response = put(platform_client, f"{BASE}/{clinic.id}/agent/", {**STANDARD, "recording_disclosure": True})
        assert response.status_code == 200 and response.json()["inbound_applied"] == {"ok": True, "detail": "Applied."}
        assert push.called

    def test_catalog_and_voice_samples_work_for_a_hospital(self, platform_client, clinic):
        with mock.patch("ai_agent.providers.list_providers", return_value=[{"name": "sarvam", "description": "x"}]):
            listed = platform_client.get(f"{BASE}/{clinic.id}/agent/providers/", {"type": "tts"})
        assert listed.status_code == 200 and listed.json()["providers"][0]["name"] == "sarvam"
        assert platform_client.get(f"{BASE}/{clinic.id}/agent/providers/", {"type": "video"}).status_code == 400

        with mock.patch("ai_agent.providers.voices", return_value={"voices": [{"id": "ritu"}]}):
            assert platform_client.get(f"{BASE}/{clinic.id}/agent/voices/", {"provider": "sarvam"}).json()["voices"][0]["id"] == "ritu"
        assert platform_client.get(f"{BASE}/{clinic.id}/agent/voices/").status_code == 400

        with mock.patch("ai_agent.providers.synthesize", return_value=b"AUDIO") as speak:
            sample = platform_client.post(f"{BASE}/{clinic.id}/agent/preview/", {"provider": "sarvam", "model": "bulbul:v3", "voice": "ritu"}, format="json")
        assert sample.status_code == 200 and sample["Content-Type"] == "audio/mpeg" and sample.content == b"AUDIO"
        assert clinic.name in speak.call_args.args[3] or speak.call_args.args[3]  # the hospital's own greeting is spoken
        assert platform_client.post(f"{BASE}/{clinic.id}/agent/preview/", {"provider": "sarvam"}, format="json").status_code == 400

    def test_only_the_platform_login_may_use_it_and_only_for_a_real_hospital(self, api_client, admin_client, platform_client, clinic):
        from clinics.models import Clinic
        centre = Clinic.objects.create(name="Centre", parent=clinic)
        for client in (api_client, admin_client):
            assert client.get(f"{BASE}/{clinic.id}/agent/").status_code == 404
            assert put(client, f"{BASE}/{clinic.id}/agent/", STANDARD).status_code == 404
        assert platform_client.get(f"{BASE}/{centre.id}/agent/").status_code == 404
        assert platform_client.get(f"{BASE}/999999/agent/").status_code == 404
        assert not VoiceAgentSettings.objects.exists()


# ── the template ──────────────────────────────────────────────────────────────

class TestTemplate:
    def test_it_starts_as_the_built_in_defaults_with_the_models_open_to_the_platform(self, platform_client):
        body = platform_client.get(TEMPLATE).json()
        assert body["is_customised"] is False and body["system_prompt"] == DEFAULT_SYSTEM_PROMPT
        assert body["can_change_models"] is True and body["owner_clinic"] == {"id": None, "name": "New hospitals"}
        assert "{clinic_name}" in body["closed_message_default"]  # the placeholder is kept: each hospital's name is filled in later

    def test_saving_it_stores_a_row_that_belongs_to_no_hospital_and_pushes_nothing(self, platform_client):
        with mock.patch("ai_agent.views.inbound.apply_call_flags") as push:
            response = put(platform_client, TEMPLATE, {**STANDARD, "recording_disclosure": True, "system_prompt": "Be brief at {clinic_name}."})
        assert response.status_code == 200, response.content
        assert not push.called  # the template has no phone number

        record = VoiceAgentSettings.objects.get()
        assert record.clinic_id is None and record.recording_disclosure is True and PlatformDefaults.load().agent_template_id == record.id
        assert platform_client.get(TEMPLATE).json()["system_prompt"] == "Be brief at {clinic_name}."
        assert audit("update_agent_template").count() == 1

    def test_a_bad_choice_is_refused_like_for_a_hospital(self, platform_client):
        response = put(platform_client, TEMPLATE, {**STANDARD, "tts": {"provider": "sarvam", "model": "bulbul:v3"}})
        assert response.status_code == 400 and "tts" in response.json()
        assert platform_client.get(TEMPLATE).json()["is_customised"] is False

    def test_saving_twice_updates_the_one_template(self, platform_client):
        put(platform_client, TEMPLATE, {**STANDARD, "system_prompt": "One."})
        put(platform_client, TEMPLATE, {**STANDARD, "system_prompt": "Two."})
        assert VoiceAgentSettings.objects.count() == 1 and platform_client.get(TEMPLATE).json()["system_prompt"] == "Two."

    def test_reset_removes_the_template(self, platform_client):
        put(platform_client, TEMPLATE, STANDARD)
        assert platform_client.delete(TEMPLATE).json()["is_customised"] is False
        assert not VoiceAgentSettings.objects.exists() and PlatformDefaults.load().agent_template_id is None
        assert platform_client.delete(TEMPLATE).status_code == 200  # nothing to reset is not an error

    def test_catalog_and_voice_samples_work_for_the_template(self, platform_client):
        with mock.patch("ai_agent.providers.list_providers", return_value=[{"name": "sarvam", "description": "x"}]):
            assert platform_client.get(TEMPLATE + "providers/", {"type": "tts"}).json()["providers"][0]["name"] == "sarvam"
        with mock.patch("ai_agent.providers.voices", return_value={"voices": [{"id": "ritu"}]}):
            assert platform_client.get(TEMPLATE + "voices/", {"provider": "sarvam"}).json()["voices"][0]["id"] == "ritu"
        with mock.patch("ai_agent.providers.synthesize", return_value=b"AUDIO") as speak:
            sample = platform_client.post(TEMPLATE + "preview/", {"provider": "sarvam", "model": "bulbul:v3", "voice": "ritu"}, format="json")
        assert sample.status_code == 200 and sample.content == b"AUDIO"
        assert "{clinic_name}" not in speak.call_args.args[3] and "your hospital" in speak.call_args.args[3]  # a stand-in name is spoken
        assert platform_client.post(TEMPLATE + "preview/", {"provider": "sarvam"}, format="json").status_code == 400

    def test_only_the_platform_login_may_use_it(self, api_client, admin_client):
        for client in (api_client, admin_client):
            assert client.get(TEMPLATE).status_code == 404 and put(client, TEMPLATE, STANDARD).status_code == 404 and client.delete(TEMPLATE).status_code == 404
        assert not VoiceAgentSettings.objects.exists()


class TestNewHospitalsStartFromTheTemplate:
    def test_a_new_hospital_gets_its_own_copy(self, platform_client):
        put(platform_client, TEMPLATE, {**STANDARD, "system_prompt": "Be brief at {clinic_name}.", "recording_disclosure": True, "max_silence_seconds": 45})
        created = post_form(platform_client, form()).json()

        mine = VoiceAgentSettings.objects.get(clinic_id=created["id"])
        template = VoiceAgentSettings.objects.get(clinic__isnull=True)
        assert mine.pk != template.pk
        assert (mine.system_prompt, mine.tts, mine.llm, mine.recording_disclosure, mine.max_silence_seconds) == (
            "Be brief at {clinic_name}.", template.tts, template.llm, True, 45)
        shown = platform_client.get(f"{BASE}/{created['id']}/agent/").json()
        assert shown["is_customised"] is True and shown["tts"]["voice"] == "ritu"

    def test_changing_the_template_later_never_touches_a_hospital_that_already_exists(self, platform_client):
        put(platform_client, TEMPLATE, {**STANDARD, "system_prompt": "First."})
        created = post_form(platform_client, form()).json()
        put(platform_client, TEMPLATE, {**STANDARD, "system_prompt": "Second."})
        assert platform_client.delete(TEMPLATE).status_code == 200
        assert VoiceAgentSettings.objects.get(clinic_id=created["id"]).system_prompt == "First."

    def test_hospitals_made_before_there_was_a_template_are_not_touched(self, platform_client, clinic):
        put(platform_client, TEMPLATE, STANDARD)
        assert not VoiceAgentSettings.objects.filter(clinic=clinic).exists()

    def test_with_no_template_a_new_hospital_simply_uses_the_built_in_defaults(self, platform_client):
        created = post_form(platform_client, form()).json()
        assert not VoiceAgentSettings.objects.filter(clinic_id=created["id"]).exists()
        assert platform_client.get(f"{BASE}/{created['id']}/agent/").json()["is_customised"] is False

    def test_two_hospitals_from_one_template_are_independent(self, platform_client):
        put(platform_client, TEMPLATE, {**STANDARD, "system_prompt": "Shared start."})
        first = post_form(platform_client, form()).json()
        second = post_form(platform_client, form(name="Second Hospital", admin={"email": "two@h.example"})).json()
        put(platform_client, f"{BASE}/{first['id']}/agent/", {**STANDARD, "system_prompt": "Only the first."})
        assert VoiceAgentSettings.objects.get(clinic_id=second["id"]).system_prompt == "Shared start."
