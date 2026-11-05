from __future__ import annotations

from unittest import mock

import pytest

from leads.models import Lead

URL = "/api/v1/leads/"
VALID = {"name": "Dr. Rao", "email": "rao@hospital.example.com", "phone": "+91 98765 43210", "organisation": "Rao Hospital"}


@pytest.mark.django_db
class TestLeadCreate:
    def test_saves_the_lead_and_emails_our_team(self, api_client, settings, mailoutbox):
        settings.LEAD_NOTIFY_EMAIL = "team@example.com"
        res = api_client.post(URL, VALID, format="json")
        assert res.status_code == 201
        lead = Lead.objects.get()
        assert (lead.name, lead.email, lead.phone, lead.organisation, lead.source) == (
            "Dr. Rao", "rao@hospital.example.com", "+91 98765 43210", "Rao Hospital", "slot-site",
        )
        assert lead.notified is True
        assert len(mailoutbox) == 1 and mailoutbox[0].to == ["team@example.com"]
        assert "Rao Hospital" in mailoutbox[0].subject and "+91 98765 43210" in mailoutbox[0].body

    def test_the_hospital_is_optional(self, api_client):
        res = api_client.post(URL, {k: v for k, v in VALID.items() if k != "organisation"}, format="json")
        assert res.status_code == 201 and Lead.objects.get().organisation == ""

    def test_needs_no_login(self, api_client):
        api_client.cookies["access_token"] = "stale-or-invalid"
        assert api_client.post(URL, VALID, format="json").status_code == 201

    def test_a_filled_hidden_field_is_a_bot_and_saves_nothing(self, api_client, mailoutbox):
        res = api_client.post(URL, {**VALID, "website": "http://spam.example.com"}, format="json")
        assert res.status_code == 201          # looks like success, so bots do not retry
        assert not Lead.objects.exists() and mailoutbox == []

    @pytest.mark.parametrize("change, field", [
        ({"email": "not-an-email"}, "email"),
        ({"phone": "abc"}, "phone"),
        ({"phone": "123"}, "phone"),
        ({"name": "   "}, "name"),
        ({"name": ""}, "name"),
    ])
    def test_bad_input_is_refused(self, api_client, change, field):
        res = api_client.post(URL, {**VALID, **change}, format="json")
        assert res.status_code == 400 and field in res.data
        assert not Lead.objects.exists()

    def test_a_failed_email_does_not_lose_the_lead(self, api_client):
        with mock.patch("leads.views.send_mail", side_effect=OSError("smtp down")):
            res = api_client.post(URL, VALID, format="json")
        assert res.status_code == 201
        assert Lead.objects.get().notified is False

    def test_the_rate_limit_stops_repeated_posts(self, api_client, monkeypatch):
        from leads.throttles import LeadThrottle

        # monkeypatch puts the real rates back afterwards, so other tests are not limited by this one.
        monkeypatch.setattr(LeadThrottle, "THROTTLE_RATES", {**LeadThrottle.THROTTLE_RATES, "leads": "2/hour"})
        assert [api_client.post(URL, VALID, format="json").status_code for _ in range(3)] == [201, 201, 429]
