"""Low-balance alerts: one email when a call moves a hospital down a state, to the right people, never a flood."""
from __future__ import annotations

from unittest import mock

import pytest
from django.core import mail
from django.db import transaction

from accounts.models import User
from clinics.models import Clinic
from platform_admin import alerts, charging, credits
from platform_admin.models import CreditAccount
from platform_admin.tests.test_charging import charging_on
from platform_admin.tests.test_platform_api import call_log, client_for, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def platform_admin():
    return make_user("platform@heykoala.example", is_staff=True)


@pytest.fixture
def run(django_capture_on_commit_callbacks):
    """Charge a call of ``seconds`` and let the after-commit emails go out, as they do in real life."""
    def _run(clinic, seconds):
        with django_capture_on_commit_callbacks(execute=True):
            return charging.charge_call(call_log(clinic, duration_seconds=seconds))
    return _run


def subjects():
    return [m.subject for m in mail.outbox]


def state(clinic):
    return CreditAccount.objects.get(clinic=clinic).alert_state


# ── the states ────────────────────────────────────────────────────────────────

class TestStates:
    @pytest.mark.parametrize("balance,threshold,expected", [
        (1000, 500, "ok"), (501, 500, "ok"), (500, 500, "low"), (1, 500, "low"), (0, 500, "empty"), (-30, 500, "empty"), (5, 0, "ok"), (0, 0, "empty"),
    ])
    def test_where_a_balance_stands(self, balance, threshold, expected):
        assert alerts.state_for(balance, threshold) == expected


# ── what is sent, and when ────────────────────────────────────────────────────

class TestAlertsFollowTheDrain:
    def test_a_slow_drain_sends_one_low_email_then_one_empty_email_and_nothing_else(self, clinic, clinic_admin, platform_admin, run):
        charging_on(clinic, balance=600)

        run(clinic, 300)                        # 5 min: 550, still fine
        assert mail.outbox == [] and state(clinic) == "ok"

        run(clinic, 600)                        # 10 min: 500, exactly the warning level
        assert state(clinic) == "low"
        assert len(mail.outbox) == 2 and all("running low" in s for s in subjects())  # one for HeyKoala, one for the hospital's admin

        run(clinic, 60); run(clinic, 120)       # still low: nothing more
        assert len(mail.outbox) == 2

        run(clinic, 4500)                       # 75 min: well below zero
        assert state(clinic) == "empty"
        assert len(mail.outbox) == 4 and all("run out" in s for s in subjects()[2:])

        run(clinic, 600); run(clinic, 600)      # deeper into the red: still no flood
        assert len(mail.outbox) == 4

    def test_adding_credits_resets_it_so_the_next_drain_warns_again(self, clinic, clinic_admin, platform_admin, run):
        account = charging_on(clinic, balance=600)
        run(clinic, 600)
        assert len(mail.outbox) == 2
        with_topup = credits.add_credits(account, 5000)  # back above the warning level
        assert with_topup and state(clinic) == "ok" and len(mail.outbox) == 2  # topping up sends nothing
        run(clinic, 36000)                                # a long call: 600 minutes = 6,000 credits
        assert state(clinic) == "empty" and len(mail.outbox) == 4

    def test_a_balance_that_goes_straight_to_empty_sends_only_the_empty_email(self, clinic, clinic_admin, platform_admin, run):
        charging_on(clinic, balance=1000)
        run(clinic, 7200)                                 # 120 minutes: 1,200 credits
        assert state(clinic) == "empty" and len(mail.outbox) == 2 and all("run out" in s for s in subjects())

    def test_setting_or_adding_credits_by_hand_never_emails_even_when_it_drops_to_zero(self, clinic, clinic_admin, platform_admin, run):
        account = charging_on(clinic, balance=600)
        credits.set_balance(account, 0)
        assert state(clinic) == "empty" and mail.outbox == []
        run(clinic, 60)                                   # already empty: not news
        assert mail.outbox == []

    def test_changing_the_warning_level_only_updates_the_state(self, clinic, clinic_admin, platform_admin, django_capture_on_commit_callbacks):
        charging_on(clinic, balance=600)
        with django_capture_on_commit_callbacks(execute=True):
            response = client_for(platform_admin).patch(f"/api/v1/platform/hospitals/{clinic.id}/plan/", {"low_balance_threshold": 10000}, format="json")
        assert response.status_code == 200 and state(clinic) == "low" and mail.outbox == []

    def test_a_charge_that_is_rolled_back_sends_nothing(self, clinic, clinic_admin, platform_admin, django_capture_on_commit_callbacks):
        charging_on(clinic, balance=600)
        with django_capture_on_commit_callbacks(execute=True):
            with pytest.raises(RuntimeError), transaction.atomic():
                charging.charge_call(call_log(clinic, duration_seconds=6000))
                raise RuntimeError("the surrounding work failed")
        assert mail.outbox == [] and CreditAccount.objects.get(clinic=clinic).balance == 600

    def test_a_correction_to_a_call_counts_like_the_call(self, clinic, clinic_admin, platform_admin, django_capture_on_commit_callbacks):
        from ai_agent.models import CallLog
        charging_on(clinic, balance=600)
        call = call_log(clinic, duration_seconds=60)       # 10 credits: 590
        charging.charge_call(call)
        CallLog.objects.filter(pk=call.pk).update(duration_seconds=900)   # really 15 minutes: 150 credits, 450 left
        call.refresh_from_db()
        with django_capture_on_commit_callbacks(execute=True):
            charging.charge_call(call)
        assert state(clinic) == "low" and len(mail.outbox) == 2

    def test_a_call_arriving_by_the_providers_webhook_raises_the_alert_too(self, api_client, clinic, clinic_admin, platform_admin, settings, django_capture_on_commit_callbacks):
        settings.ROCK8_WEBHOOK_SECRET = "test-secret"
        charging_on(clinic, balance=600)
        event = {"event": "call.ended", "call": {"call_id": "c-1", "direction": "inbound", "from_number": "+919999999999", "duration_seconds": 6000}}
        with django_capture_on_commit_callbacks(execute=True):
            assert api_client.post(f"/api/v1/ai/webhooks/rock8/{clinic.id}/", event, format="json", HTTP_AUTHORIZATION="Bearer test-secret").status_code == 200
        assert state(clinic) == "empty" and len(mail.outbox) == 2


# ── who gets it ───────────────────────────────────────────────────────────────

class TestRecipients:
    def test_platform_admins_get_a_link_and_the_hospitals_admins_are_told_to_contact_heykoala(self, clinic, clinic_admin, platform_admin, run):
        charging_on(clinic, balance=600)
        run(clinic, 600)
        by_recipient = {tuple(m.to): m for m in mail.outbox}
        platform_mail = by_recipient[(platform_admin.email,)]
        hospital_mail = by_recipient[(clinic_admin.email,)]
        assert f"/platform/hospitals/{clinic.id}" in platform_mail.body and "500 credits" in platform_mail.body and "50 minutes" in platform_mail.body
        assert "contact HeyKoala" in hospital_mail.body and "/platform/" not in hospital_mail.body  # the hospital is never sent the platform link
        assert "Test Clinic" in platform_mail.subject

    def test_only_the_right_people_are_emailed(self, clinic, clinic_admin, clinic_b, platform_admin, doctor_user, receptionist_user, run):
        make_user("inactive.staff@heykoala.example", is_staff=True, is_active=False)
        make_user("unverified.staff@heykoala.example", is_staff=True, is_verified=False)
        make_user("admin@other.example", clinic=clinic_b)                    # another hospital's admin
        centre = Clinic.objects.create(name="Centre", parent=clinic)
        make_user("centre.admin@h.example", clinic=centre)                   # an admin at one of this hospital's centres
        charging_on(clinic, balance=600)
        run(clinic, 600)
        recipients = {addr for m in mail.outbox for addr in m.to}
        assert recipients == {platform_admin.email, clinic_admin.email, "centre.admin@h.example"}

    def test_someone_who_is_both_gets_one_email_not_two(self, clinic, clinic_admin, run):
        clinic_admin.is_staff = True                      # HeyKoala's team attached to the hospital, like Gunasheela's staff login
        clinic_admin.save()
        charging_on(clinic, balance=600)
        run(clinic, 600)
        assert [m.to for m in mail.outbox] == [[clinic_admin.email]]

    def test_nobody_to_tell_is_fine(self, clinic, run):
        charging_on(clinic, balance=600)
        run(clinic, 600)
        assert mail.outbox == [] and state(clinic) == "low"

    def test_a_suspended_or_archived_hospital_is_not_emailed_but_its_state_is_kept(self, clinic, clinic_admin, platform_admin, run):
        charging_on(clinic, balance=600)
        clinic.status = Clinic.Status.SUSPENDED
        clinic.save()
        run(clinic, 600)
        assert mail.outbox == [] and state(clinic) == "low"


# ── when email itself fails ───────────────────────────────────────────────────

class TestEmailFailure:
    def test_a_mail_problem_never_stops_the_charge_or_the_state(self, clinic, clinic_admin, platform_admin, run):
        charging_on(clinic, balance=600)
        with mock.patch("platform_admin.alerts.send_mail", side_effect=RuntimeError("smtp down")):
            entry = run(clinic, 600)
        assert entry is not None and CreditAccount.objects.get(clinic=clinic).balance == 500 and state(clinic) == "low"
        assert mail.outbox == []

    def test_the_overview_says_which_hospitals_are_low_and_which_are_out(self, clinic, clinic_b, clinic_admin, platform_admin, run):
        charging_on(clinic, balance=600)
        charging_on(clinic_b, balance=600)
        run(clinic, 600)       # low
        run(clinic_b, 9000)    # empty
        rows = {r["name"]: r for r in client_for(platform_admin).get("/api/v1/platform/overview/").json()["low_balance"]}
        assert (rows["Test Clinic"]["state"], rows["Other Clinic"]["state"]) == ("low", "empty")
