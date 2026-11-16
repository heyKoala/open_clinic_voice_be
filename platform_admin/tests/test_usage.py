"""The per-hospital usage report: counted like billing, scoped to one hospital, read-only."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from ai_agent.models import CallLog
from clinics.models import Clinic
from platform_admin import charging
from platform_admin.tests.test_charging import charging_on
from platform_admin.tests.test_platform_api import call_log, client_for, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def platform_admin():
    return make_user("platform@heykoala.example", is_staff=True)


def usage(user, clinic, days=None):
    suffix = f"?days={days}" if days else ""
    return client_for(user).get(f"/api/v1/platform/hospitals/{clinic.id}/usage/{suffix}")


class TestUsageNumbers:
    def test_calls_minutes_directions_and_credits(self, clinic, platform_admin):
        charging_on(clinic, balance=1000)
        charging.charge_call(call_log(clinic, duration_seconds=61))                                   # 2 min, inbound
        charging.charge_call(call_log(clinic, duration_seconds=30, direction=CallLog.Direction.OUTBOUND))  # 1 min, outbound
        body = usage(platform_admin, clinic).json()
        assert body["calls"]["total"] == 2 and body["calls"]["inbound"] == 1 and body["calls"]["outbound"] == 1
        assert body["minutes"] == {"total": 3, "inbound": 2, "outbound": 1}
        assert body["credits"]["used"] == 30 and body["credits"]["balance"] == 970
        assert body["calls"]["not_charged"] == 0 and body["calls"]["average_seconds"] == 46
        assert body["daily"][-1]["calls"] == 2 and body["daily"][-1]["credits"] == 30 and len(body["daily"]) == 30

    def test_test_calls_and_old_calls_are_left_out(self, clinic, platform_admin):
        call_log(clinic, duration_seconds=120, is_test=True)
        call_log(clinic, duration_seconds=120, occurred_at=timezone.now() - timedelta(days=40))
        call_log(clinic, duration_seconds=60)
        body = usage(platform_admin, clinic, 30).json()
        assert body["calls"]["total"] == 1 and body["calls"]["not_charged"] == 1   # no credit account: nothing charged
        assert usage(platform_admin, clinic, 90).json()["calls"]["total"] == 2

    def test_only_this_hospitals_calls_count_but_its_centres_do(self, clinic, clinic_b, platform_admin):
        centre = Clinic.objects.create(name="Centre", parent=clinic)
        call_log(clinic, duration_seconds=60)
        call_log(centre, duration_seconds=60)
        call_log(clinic_b, duration_seconds=60)
        body = usage(platform_admin, clinic).json()
        assert body["calls"]["total"] == 2 and body["records"]["centres"] == 2

    def test_a_hospital_with_nothing_yet_gets_zeros(self, clinic, platform_admin):
        body = usage(platform_admin, clinic).json()
        assert body["calls"]["total"] == 0 and body["calls"]["average_seconds"] == 0 and body["credits"]["used"] == 0
        assert body["storage"]["files"] == 0


class TestUsageAccess:
    def test_bad_period_is_refused(self, clinic, platform_admin):
        assert usage(platform_admin, clinic, 5).status_code == 400
        assert client_for(platform_admin).get(f"/api/v1/platform/hospitals/{clinic.id}/usage/?days=abc").status_code == 400

    def test_hospital_staff_get_a_404(self, clinic, clinic_admin):
        assert usage(clinic_admin, clinic).status_code == 404

    def test_unknown_hospital_is_404(self, platform_admin):
        assert client_for(platform_admin).get("/api/v1/platform/hospitals/999999/usage/").status_code == 404
