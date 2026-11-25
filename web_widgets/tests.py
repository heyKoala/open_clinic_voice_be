import json
from unittest import mock

import pytest
from django.utils import timezone

from ai_agent.models import CallLog
from ai_agent.tool_views import rock8_clinic_token
from clinics.models import Clinic
from platform_admin.models import CreditAccount
from web_widgets.models import WebWidget
from web_widgets.throttles import PublicWidgetTokenThrottle

SITE = "https://www.hospital.example"


@pytest.fixture()
def widget(clinic) -> WebWidget:
    return WebWidget.objects.create(clinic=clinic, name="Website", allowed_origins=[SITE])


def _token(client, widget, origin=SITE, **extra):
    worker = mock.Mock(**{"json.return_value": {"token": "lk-token"}})
    with mock.patch("ai_agent.web_call.requests.post", return_value=worker) as started:
        res = client.post(f"/api/v1/widgets/public/{widget.public_key}/token/", data={}, format="json",
                          **({"HTTP_ORIGIN": origin} if origin else {}), **extra)
    return res, started


@pytest.mark.django_db
class TestPublicEndpoints:
    def test_config_is_only_what_the_button_needs(self, api_client, widget):
        res = api_client.get(f"/api/v1/widgets/public/{widget.public_key}/config/", HTTP_ORIGIN=SITE)
        assert res.status_code == 200
        assert set(res.json()) == {"name", "branding", "mode", "show_text_input", "greeting_message", "greeting_audio_url"}
        assert res["Access-Control-Allow-Origin"] == SITE and res["Access-Control-Allow-Credentials"] == "false"

    def test_other_sites_are_refused(self, api_client, widget):
        for origin in ("https://evil.example", None):
            res = api_client.get(f"/api/v1/widgets/public/{widget.public_key}/config/", **({"HTTP_ORIGIN": origin} if origin else {}))
            assert res.status_code == 403 and "Access-Control-Allow-Origin" not in res
            res, started = _token(api_client, widget, origin=origin)
            assert res.status_code == 403 and not started.called

    def test_a_new_widget_allows_no_site_until_one_is_added(self, api_client, clinic):
        widget = WebWidget.objects.create(clinic=clinic, name="New")
        assert _token(api_client, widget)[0].status_code == 403

    def test_star_allows_any_site(self, api_client, widget):
        widget.allowed_origins = ["*"]
        widget.save()
        res, _ = _token(api_client, widget, origin="https://anything.example")
        assert res.status_code == 200 and res["Access-Control-Allow-Origin"] == "https://anything.example"

    def test_unknown_or_switched_off_widget_is_not_found(self, api_client, widget):
        assert api_client.get("/api/v1/widgets/public/wd_nope/config/", HTTP_ORIGIN=SITE).status_code == 404
        widget.is_active = False
        widget.save()
        assert _token(api_client, widget)[0].status_code == 404

    def test_preflight_answers_only_allowed_sites(self, api_client, widget):
        url = f"/api/v1/widgets/public/{widget.public_key}/token/"
        allowed = api_client.options(url, HTTP_ORIGIN=SITE, HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST")
        assert allowed.status_code == 204 and allowed["Access-Control-Allow-Origin"] == SITE
        other = api_client.options(url, HTTP_ORIGIN="https://evil.example", HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST")
        assert "Access-Control-Allow-Origin" not in other

    def test_token_starts_a_call_through_the_widgets_webhook(self, api_client, widget, clinic, settings):
        settings.ROCK8_API_KEY = "key"
        settings.PUBLIC_BASE_URL = "https://api.example"
        res, started = _token(api_client, widget)
        assert res.status_code == 200
        body = res.json()
        assert body["token"] == "lk-token" and body["serverUrl"] == settings.LIVEKIT_URL and body["session_id"]
        sent = started.call_args.kwargs["json"]
        assert sent["webhook_url"] == (
            f"https://api.example/api/v1/ai/webhooks/rock8/{clinic.id}/{rock8_clinic_token(clinic.id)}/widget/{widget.id}/"
        )
        assert sent["participant_identity"].startswith("widget_")

    def test_suspended_hospital_gets_no_call(self, api_client, widget, clinic, settings):
        settings.ROCK8_API_KEY = "key"
        clinic.status = Clinic.Status.SUSPENDED
        clinic.save()
        res, started = _token(api_client, widget)
        assert res.status_code == 403 and not started.called

    def test_no_credits_refuses_only_when_that_is_switched_on(self, api_client, widget, clinic, settings):
        settings.ROCK8_API_KEY = "key"
        account = CreditAccount.objects.create(clinic=clinic, balance=0, charging_from=timezone.now())
        assert _token(api_client, widget)[0].status_code == 200  # like the phone: still answered
        settings.WIDGET_CALLS_REQUIRE_CREDITS = True
        res, started = _token(api_client, widget)
        assert res.status_code == 402 and not started.called
        account.balance = 100
        account.save()
        assert _token(api_client, widget)[0].status_code == 200

    def test_switch_turns_the_endpoints_off(self, api_client, widget, settings):
        settings.WIDGETS_PUBLIC_ENABLED = False
        assert _token(api_client, widget)[0].status_code == 503

    def test_token_requests_are_limited_per_visitor(self, api_client, widget, settings):
        settings.ROCK8_API_KEY = "key"
        with mock.patch.object(PublicWidgetTokenThrottle, "rate", "2/min"):
            codes = [_token(api_client, widget)[0].status_code for _ in range(3)]
        assert codes == [200, 200, 429]


@pytest.mark.django_db
class TestWidgetCalls:
    def _start(self, client, url, payload, capture):
        with mock.patch("ai_agent.call_sessions.recording_links", return_value={}), capture(execute=True):
            return client.post(url, data=json.dumps(payload), content_type="application/json")

    def _url(self, clinic, widget):
        return f"/api/v1/ai/webhooks/rock8/{clinic.id}/{rock8_clinic_token(clinic.id)}/widget/{widget.id}/"

    def test_call_is_logged_under_the_widget_and_is_not_a_test(self, api_client, clinic, widget, django_capture_on_commit_callbacks):
        res = self._start(api_client, self._url(clinic, widget), {"room_name": "room-w1"}, django_capture_on_commit_callbacks)
        assert res.status_code == 200 and "website" in res.data["system_prompt"]
        log = CallLog.objects.get(external_call_id="room-w1")
        assert (log.source, log.widget_id, log.is_test) == (CallLog.Source.WIDGET, widget.id, False)

    def test_widget_is_answered_outside_the_answering_hours(self, api_client, clinic, widget, django_capture_on_commit_callbacks):
        with mock.patch("ai_agent.views.answering_now", return_value=False):
            phone = self._start(api_client, f"/api/v1/ai/webhooks/rock8/{clinic.id}/{rock8_clinic_token(clinic.id)}/",
                                {"room_name": "room-p1"}, django_capture_on_commit_callbacks)
            web = self._start(api_client, self._url(clinic, widget), {"room_name": "room-w2"}, django_capture_on_commit_callbacks)
        assert phone.data["tools"] == [] and web.data["tools"]

    def test_a_click_that_never_became_a_call_is_not_listed(self, api_client, admin_client, clinic, widget, django_capture_on_commit_callbacks):
        # The voice service announces the call when the button is clicked, before anyone has connected.
        self._start(api_client, self._url(clinic, widget), {"room_name": "room-ghost"}, django_capture_on_commit_callbacks)
        self._start(api_client, self._url(clinic, widget), {"room_name": "room-real"}, django_capture_on_commit_callbacks)
        CallLog.objects.filter(external_call_id="room-real").update(transcript="Caller: hello", duration_seconds=19, outcome="")
        listed = admin_client.get("/api/v1/ai/call-logs/", {"source": "widget"}).data["results"]
        assert [row["duration_seconds"] for row in listed] == [19]
        assert admin_client.get(f"/api/v1/widgets/{widget.id}/stats/").data["total_calls"] == 1

    def test_another_hospitals_widget_is_not_accepted(self, api_client, clinic, clinic_b, django_capture_on_commit_callbacks):
        other = WebWidget.objects.create(clinic=clinic_b, name="Other")
        res = self._start(api_client, self._url(clinic, other), {"room_name": "room-x"}, django_capture_on_commit_callbacks)
        assert res.status_code == 404 and not CallLog.objects.exists()


@pytest.mark.django_db
class TestManaging:
    def test_admin_creates_edits_and_lists(self, admin_client, clinic):
        res = admin_client.post("/api/v1/widgets/", {
            "name": " Main site ", "allowed_origins": ["https://WWW.Hospital.example/", "https://www.hospital.example"],
            "branding": {"primary_color": "#0055aa"},
        }, format="json")
        assert res.status_code == 201, res.data
        assert res.data["name"] == "Main site" and res.data["allowed_origins"] == [SITE]
        assert res.data["branding"] == {"primary_color": "#0055aa", "position": "bottom-right", "button_label": "Talk to AI"}
        assert res.data["public_key"].startswith("wd_") and res.data["public_key"] in res.data["embed_snippet"]
        assert WebWidget.objects.get().clinic_id == clinic.id
        patched = admin_client.patch(f"/api/v1/widgets/{res.data['id']}/", {"is_active": False}, format="json")
        assert patched.status_code == 200 and patched.data["is_active"] is False
        assert len(admin_client.get("/api/v1/widgets/").data) == 1

    @pytest.mark.parametrize("payload", [
        {"allowed_origins": ["www.hospital.example"]},
        {"allowed_origins": ["https://www.hospital.example/contact"]},
        {"branding": {"primary_color": "blue"}},
        {"branding": {"position": "top"}},
        {"name": "  "},
    ])
    def test_bad_values_are_rejected(self, admin_client, payload):
        res = admin_client.post("/api/v1/widgets/", {"name": "Site", **payload}, format="json")
        assert res.status_code == 400

    def test_new_key_stops_the_old_one(self, admin_client, api_client, widget):
        old = widget.public_key
        res = admin_client.post(f"/api/v1/widgets/{widget.id}/rotate-key/")
        assert res.status_code == 200 and res.data["public_key"] != old
        assert api_client.get(f"/api/v1/widgets/public/{old}/config/", HTTP_ORIGIN=SITE).status_code == 404

    def test_other_hospitals_widgets_are_out_of_reach(self, admin_client, clinic_b):
        other = WebWidget.objects.create(clinic=clinic_b, name="Other")
        assert admin_client.get("/api/v1/widgets/").data == []
        assert admin_client.patch(f"/api/v1/widgets/{other.id}/", {"name": "Mine"}, format="json").status_code == 404
        assert admin_client.delete(f"/api/v1/widgets/{other.id}/").status_code == 404

    def test_only_admins_change_widgets(self, doctor_client, receptionist_client, widget):
        assert doctor_client.get("/api/v1/widgets/").status_code == 200
        assert doctor_client.post("/api/v1/widgets/", {"name": "X"}, format="json").status_code == 403
        assert receptionist_client.get("/api/v1/widgets/").status_code == 403

    def test_deleting_keeps_the_calls_as_website_calls(self, admin_client, clinic, widget):
        log = CallLog.objects.create(clinic=clinic, direction="inbound", occurred_at=timezone.now(), duration_seconds=65,
                                     source=CallLog.Source.WIDGET, widget=widget, transcript="Caller: hi")
        stats = admin_client.get(f"/api/v1/widgets/{widget.id}/stats/").data
        assert stats == {"total_calls": 1, "total_minutes": 1.1, "appointments_booked": 0}
        listed = admin_client.get("/api/v1/ai/call-logs/", {"source": "widget"}).data
        assert [row["widget_name"] for row in (listed["results"] if isinstance(listed, dict) else listed)] == ["Website"]
        assert admin_client.delete(f"/api/v1/widgets/{widget.id}/").status_code == 204
        log.refresh_from_db()
        assert log.widget_id is None and log.source == CallLog.Source.WIDGET
