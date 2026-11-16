"""Credits are charged when a call ends: once, rounded up to whole minutes, at the hospital's rate."""
from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.db import IntegrityError
from django.utils import timezone

from ai_agent.call_sessions import finish_call_log
from ai_agent.models import CallLog
from clinics.models import Clinic
from platform_admin import charging, credits
from platform_admin.models import CreditAccount, CreditLedger, PlatformAuditLog
from platform_admin.tasks import settle_unbilled_calls
from platform_admin.tests.test_platform_api import call_log, client_for, make_user

pytestmark = pytest.mark.django_db

Entry = CreditLedger.EntryType


def charging_on(clinic, since=None, balance=0):
    """A hospital whose calls are charged, from a day ago unless told otherwise."""
    account = credits.account_for(clinic)
    account.charging_from = since or timezone.now() - timedelta(days=1)
    account.save()
    if balance:
        credits.add_credits(account, balance)
    return account


def ledger(clinic):
    return list(CreditLedger.objects.filter(account__clinic=clinic.root).order_by("id"))


def balance(clinic):
    return CreditAccount.objects.get(clinic=clinic.root).balance


# ── one call ──────────────────────────────────────────────────────────────────

class TestChargeACall:
    def test_a_finished_call_is_charged_in_whole_minutes_at_the_rate(self, clinic):
        charging_on(clinic, balance=1000)
        call = call_log(clinic, duration_seconds=61)  # 1m 01s rounds up to 2 minutes

        entry = charging.charge_call(call)

        assert (entry.entry_type, entry.amount, entry.minutes, entry.rate) == ("usage", -20, 2, 10)
        assert entry.call_log_id == call.id and entry.balance_after == 980 and "2 min" in entry.note
        assert balance(clinic) == 980

    @pytest.mark.parametrize("seconds,minutes", [(1, 1), (59, 1), (60, 1), (61, 2), (119, 2), (120, 2), (121, 3), (600, 10)])
    def test_every_call_rounds_up_to_the_next_minute(self, clinic, seconds, minutes):
        charging_on(clinic)
        assert charging.charge_call(call_log(clinic, duration_seconds=seconds)).minutes == minutes

    def test_the_hospitals_own_rate_is_used(self, clinic):
        charging_on(clinic)
        clinic.entitlement.credits_per_minute_override = 12
        clinic.entitlement.save()
        assert charging.charge_call(call_log(clinic, duration_seconds=60)).amount == -12

    def test_a_centres_call_is_charged_to_the_hospital(self, clinic):
        charging_on(clinic)
        centre = Clinic.objects.create(name="Centre", parent=clinic)
        charging.charge_call(call_log(centre, duration_seconds=60))
        assert balance(clinic) == -10 and len(ledger(clinic)) == 1

    def test_charging_the_same_call_again_changes_nothing(self, clinic):
        charging_on(clinic)
        call = call_log(clinic)
        charging.charge_call(call)
        assert charging.charge_call(call) is None and charging.charge_call(call) is None
        assert len(ledger(clinic)) == 1 and balance(clinic) == -20

    def test_the_balance_may_go_negative_and_still_adds_up(self, clinic):
        charging_on(clinic, balance=10)
        for _ in range(3):
            charging.charge_call(call_log(clinic, duration_seconds=60))
        assert balance(clinic) == -20
        assert sum(e.amount for e in ledger(clinic)) == balance(clinic)


class TestWhatIsNeverCharged:
    def test_staff_test_calls_calls_with_no_length_and_empty_calls(self, clinic):
        charging_on(clinic)
        for call in (call_log(clinic, is_test=True, duration_seconds=300), call_log(clinic, duration_seconds=0), call_log(clinic, duration_seconds=0, transcript="")):
            assert charging.charge_call(call) is None
        assert ledger(clinic) == []

    def test_calls_from_before_charging_started(self, clinic):
        charging_on(clinic, since=timezone.now() - timedelta(hours=1))
        old = call_log(clinic, occurred_at=timezone.now() - timedelta(days=3))
        new = call_log(clinic, occurred_at=timezone.now() - timedelta(minutes=5))
        assert charging.charge_call(old) is None and charging.charge_call(new) is not None

    def test_a_hospital_that_is_not_charging_yet_or_has_no_account(self, clinic, clinic_b):
        credits.account_for(clinic)  # an account, but charging has not been switched on
        assert charging.charge_call(call_log(clinic)) is None
        assert charging.charge_call(call_log(clinic_b)) is None  # no account at all (e.g. a demo hospital)
        assert CreditLedger.objects.count() == 0 and CreditAccount.objects.count() == 1  # and it did not create one

    def test_another_hospitals_calls_never_touch_this_hospitals_credits(self, clinic, clinic_b):
        charging_on(clinic, balance=500)
        charging_on(clinic_b, balance=500)
        charging.charge_call(call_log(clinic_b, duration_seconds=120))
        assert (balance(clinic), balance(clinic_b)) == (500, 480)


# ── a later report of the call's length ───────────────────────────────────────

class TestCorrections:
    def set_length(self, call, seconds):
        CallLog.objects.filter(pk=call.pk).update(duration_seconds=seconds)
        call.refresh_from_db()

    def test_a_longer_length_is_charged_the_difference_and_a_shorter_one_refunded(self, clinic):
        charging_on(clinic)
        call = call_log(clinic, duration_seconds=61)         # 2 min
        charging.charge_call(call)
        self.set_length(call, 130)                           # 3 min: 1 more
        assert charging.charge_call(call).amount == -10
        self.set_length(call, 50)                            # 1 min: 2 fewer
        correction = charging.charge_call(call)
        assert (correction.entry_type, correction.amount, correction.minutes) == ("adjustment", 20, -2)
        assert balance(clinic) == -10  # one minute, once
        assert sum(e.amount for e in ledger(clinic)) == balance(clinic)
        assert charging.charge_call(call) is None  # and now it is settled

    def test_a_correction_uses_the_rate_the_call_was_first_charged_at(self, clinic):
        charging_on(clinic)
        call = call_log(clinic, duration_seconds=60)
        charging.charge_call(call)
        clinic.entitlement.credits_per_minute_override = 50  # the rate changes after the call
        clinic.entitlement.save()
        self.set_length(call, 120)
        assert charging.charge_call(call).amount == -10  # 1 more minute at the original 10, not 50

    def test_a_length_that_later_reads_zero_does_not_refund_the_call(self, clinic):
        charging_on(clinic)
        call = call_log(clinic, duration_seconds=90)
        charging.charge_call(call)
        self.set_length(call, 0)
        assert charging.charge_call(call) is None and balance(clinic) == -20


# ── when it goes wrong ────────────────────────────────────────────────────────

class TestSafety:
    def test_two_workers_charging_the_same_call_at_once_charge_it_once(self, clinic):
        charging_on(clinic)
        with mock.patch("platform_admin.charging.credits.post_entry", side_effect=IntegrityError("duplicate")):
            assert charging.charge_call(call_log(clinic)) is None  # the loser of the race quietly does nothing

    def test_a_billing_failure_never_reaches_the_caller(self, clinic):
        charging_on(clinic)
        call = call_log(clinic)
        with mock.patch("platform_admin.charging.charge_call", side_effect=RuntimeError("boom")):
            assert charging.charge_call_safely(call.id) is None
        assert charging.charge_call_safely(999999) is None  # an unknown call is not an error either


# ── the routes a call's length arrives by ─────────────────────────────────────

EVENT = {"event": "call.ended", "call": {"call_id": "c-1", "direction": "inbound", "from_number": "+919999999999", "duration_seconds": 61}}


class TestWhereTheLengthArrives:
    @pytest.fixture
    def rock8_headers(self, settings):
        settings.ROCK8_WEBHOOK_SECRET = "test-secret"
        return {"HTTP_AUTHORIZATION": "Bearer test-secret"}

    def url(self, clinic):
        return f"/api/v1/ai/webhooks/rock8/{clinic.id}/"

    def test_the_providers_end_of_call_event_charges_the_call_once_even_if_delivered_twice(self, api_client, clinic, rock8_headers):
        charging_on(clinic)
        for _ in range(2):
            assert api_client.post(self.url(clinic), EVENT, format="json", **rock8_headers).status_code == 200
        entries = ledger(clinic)
        assert [(e.entry_type, e.amount) for e in entries] == [("usage", -20)] and CallLog.objects.count() == 1

    def test_a_call_that_has_only_started_is_not_charged_yet(self, api_client, clinic, rock8_headers):
        charging_on(clinic)
        started = {"event": "call.started", "call": {"call_id": "c-2", "direction": "inbound", "from_number": "+919999999999"}}
        assert api_client.post(self.url(clinic), started, format="json", **rock8_headers).status_code == 200
        assert ledger(clinic) == []

    def test_a_hospital_without_credits_set_up_is_not_charged_and_gets_no_account(self, api_client, clinic_b, rock8_headers):
        assert api_client.post(self.url(clinic_b), EVENT, format="json", **rock8_headers).status_code == 200
        assert CallLog.objects.count() == 1 and CreditAccount.objects.count() == 0

    def test_a_call_that_ended_before_the_hospital_was_suspended_is_still_charged(self, api_client, clinic, rock8_headers):
        charging_on(clinic)
        clinic.status = Clinic.Status.SUSPENDED
        clinic.save()
        assert api_client.post(self.url(clinic), EVENT, format="json", **rock8_headers).status_code == 200
        assert balance(clinic) == -20

    def test_finishing_a_call_log_after_its_transcript_charges_it(self, clinic):
        charging_on(clinic)
        call = call_log(clinic, duration_seconds=95, outcome="in_progress")
        finish_call_log(call.id, caller="", got_media=True)
        assert [(e.amount, e.minutes) for e in ledger(clinic)] == [(-20, 2)]
        finish_call_log(call.id, caller="", got_media=True)  # running it again is harmless
        assert len(ledger(clinic)) == 1


# ── the safety-net sweep ──────────────────────────────────────────────────────

class TestSweep:
    def quiet(self, call, minutes=10):
        CallLog.objects.filter(pk=call.pk).update(updated_at=timezone.now() - timedelta(minutes=minutes))
        return call

    def test_charges_finished_calls_that_were_missed_and_leaves_the_rest(self, clinic):
        charging_on(clinic, since=timezone.now() - timedelta(hours=2))
        missed = self.quiet(call_log(clinic, duration_seconds=60))
        still_settling = call_log(clinic, duration_seconds=60)                         # touched a moment ago: wait a little
        not_finished = self.quiet(call_log(clinic, duration_seconds=0, outcome="in_progress"))
        a_test = self.quiet(call_log(clinic, duration_seconds=60, is_test=True))
        before = self.quiet(call_log(clinic, duration_seconds=60, occurred_at=timezone.now() - timedelta(days=5)))

        assert charging.settle_unbilled() == 1
        assert list(CreditLedger.objects.values_list("call_log_id", flat=True)) == [missed.id]
        assert charging.settle_unbilled() == 0  # nothing left to do
        for untouched in (still_settling, not_finished, a_test, before):
            assert not CreditLedger.objects.filter(call_log=untouched).exists()

    def test_it_also_picks_up_a_length_that_changed_after_charging(self, clinic):
        charging_on(clinic)
        call = call_log(clinic, duration_seconds=60)
        charging.charge_call(call)
        CallLog.objects.filter(pk=call.pk).update(duration_seconds=180)  # a path that raises no signal
        assert charging.settle_unbilled() == 1 and balance(clinic) == -30

    def test_each_hospital_is_settled_against_its_own_account_and_only_if_charging(self, clinic, clinic_b):
        charging_on(clinic)
        credits.account_for(clinic_b)  # an account that is not charging yet
        self.quiet(call_log(clinic, duration_seconds=60))
        self.quiet(call_log(clinic_b, duration_seconds=60))
        assert charging.settle_unbilled() == 1
        assert (balance(clinic), balance(clinic_b)) == (-10, 0)

    def test_the_scheduled_task_runs_it(self, clinic):
        charging_on(clinic)
        self.quiet(call_log(clinic, duration_seconds=60))
        assert "Posted 1" in settle_unbilled_calls()

    def test_the_task_is_on_the_schedule(self):
        from config.celery import app
        assert app.conf.beat_schedule["settle-call-credits"]["task"] == "platform_admin.tasks.settle_unbilled_calls"


# ── switching charging on ─────────────────────────────────────────────────────

class TestSwitchingItOn:
    def test_a_new_hospital_is_charged_from_the_start(self):
        from platform_admin.tests.test_provision import form, post_form
        platform = client_for(make_user("platform@heykoala.example", is_staff=True))
        created = post_form(platform, form()).json()
        assert CreditAccount.objects.get(clinic_id=created["id"]).charging_from is not None
        assert created["charging_from"] is not None

    def run(self, clinic, **options):
        out = StringIO()
        call_command("adopt_hospital", clinic_id=clinic.id, stdout=out, **options)
        return out.getvalue()

    def test_adoption_does_not_start_charging_unless_asked_and_never_backdates(self, clinic):
        assert "off (use --start-charging)" in self.run(clinic)
        assert CreditAccount.objects.get(clinic=clinic).charging_from is None

        assert "will start now" in self.run(clinic, start_charging=True, dry_run=True)
        assert CreditAccount.objects.get(clinic=clinic).charging_from is None  # a dry run changes nothing

        self.run(clinic, start_charging=True)
        started = CreditAccount.objects.get(clinic=clinic).charging_from
        assert started is not None
        self.run(clinic, start_charging=True)
        assert CreditAccount.objects.get(clinic=clinic).charging_from == started  # asking again does not move the start

    def test_only_calls_after_the_switch_are_charged(self, clinic):
        self.run(clinic, start_charging=True)
        before = call_log(clinic, occurred_at=timezone.now() - timedelta(hours=1))
        after = call_log(clinic, occurred_at=timezone.now() + timedelta(seconds=5))
        assert charging.charge_call(before) is None and charging.charge_call(after) is not None

    def test_the_ledger_endpoint_shows_minutes_and_rate(self, clinic):
        charging_on(clinic)
        charging.charge_call(call_log(clinic, duration_seconds=61))
        platform = client_for(make_user("platform@heykoala.example", is_staff=True))
        row = platform.get(f"/api/v1/platform/hospitals/{clinic.id}/ledger/").json()["results"][0]
        assert (row["entry_type"], row["amount"], row["minutes"], row["rate"], row["call_id"] is not None) == ("usage", -20, 2, 10, True)
