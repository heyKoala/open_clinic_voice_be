"""The platform admin API: who may use it, hospitals, credits, plans, defaults and the audit log."""
from __future__ import annotations

import pytest
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from ai_agent.models import CallLog
from clinics.models import Clinic
from platform_admin import credits
from platform_admin.models import CreditAccount, CreditLedger, PlatformAuditLog, PlatformDefaults

pytestmark = pytest.mark.django_db

BASE = "/api/v1/platform"


def make_user(email, clinic=None, **extra):
    extra.setdefault("role", User.Role.CLINIC_ADMIN)
    extra.setdefault("is_verified", True)
    return User.objects.create_user(
        clinic=clinic, email=email, full_name="Someone", password="SomePass12345!", membership_status=User.MembershipStatus.ACTIVE, **extra
    )


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def platform_admin():
    """HeyKoala's own login: staff, belonging to no hospital."""
    return make_user("platform@heykoala.example", is_staff=True)


@pytest.fixture
def platform_client(platform_admin):
    return client_for(platform_admin)


def call_log(clinic, **fields):
    fields.setdefault("direction", CallLog.Direction.INBOUND)
    fields.setdefault("occurred_at", timezone.now())
    fields.setdefault("duration_seconds", 61)
    fields.setdefault("transcript", "Agent: hello\nCaller: hi")
    return CallLog.objects.create(clinic=clinic, **fields)


# ── who may use /platform/ ────────────────────────────────────────────────────

class TestAccess:
    URLS = [f"{BASE}/overview/", f"{BASE}/hospitals/", f"{BASE}/defaults/", f"{BASE}/audit-log/"]

    def test_signed_out_gets_a_plain_404(self, api_client):
        for url in self.URLS:
            assert api_client.get(url).status_code == 404, url

    def test_hospital_users_get_a_404_not_a_403(self, clinic_admin, doctor_user, receptionist_user):
        for user in (clinic_admin, doctor_user, receptionist_user):
            for url in self.URLS:
                assert client_for(user).get(url).status_code == 404, (user.role, url)

    def test_platform_login_gets_in(self, platform_client):
        for url in self.URLS:
            assert platform_client.get(url).status_code == 200, url

    def test_staff_login_that_still_belongs_to_a_hospital_gets_in(self, clinic):
        # Gunasheela's existing staff login is attached to its hospital until it is detached.
        assert client_for(make_user("staff@h.example", clinic=clinic, is_staff=True)).get(f"{BASE}/overview/").status_code == 200

    def test_unverified_or_inactive_staff_is_refused(self, clinic):
        assert client_for(make_user("u@h.example", is_staff=True, is_verified=False)).get(f"{BASE}/overview/").status_code == 404
        assert client_for(make_user("i@h.example", is_staff=True, is_active=False)).get(f"{BASE}/overview/").status_code == 404

    def test_a_centre_is_not_a_hospital(self, platform_client, clinic):
        centre = Clinic.objects.create(name="Centre", parent=clinic)
        assert platform_client.get(f"{BASE}/hospitals/{centre.id}/").status_code == 404

    def test_platform_login_cannot_use_a_hospitals_endpoints(self, platform_client):
        response = platform_client.get("/api/v1/accounts/access/")
        assert response.status_code == 403
        assert "not part of a hospital" in str(response.data)


# ── hospitals ─────────────────────────────────────────────────────────────────

class TestHospitals:
    def test_list_and_detail(self, platform_client, clinic, clinic_admin, clinic_b):
        Clinic.objects.create(name="North Centre", parent=clinic)
        rows = {row["name"]: row for row in platform_client.get(f"{BASE}/hospitals/").json()}

        assert set(rows) == {"Test Clinic", "Other Clinic"}  # centres are not listed as hospitals
        assert rows["Test Clinic"]["centres"] == 2
        assert rows["Test Clinic"]["status"] == "active"
        assert rows["Test Clinic"]["balance"] == 0

        detail = platform_client.get(f"{BASE}/hospitals/{clinic.id}/").json()
        assert detail["admin_email"] == clinic_admin.email
        assert detail["limits"]["clinic_admin"] == {"used": 1, "limit": 1}
        assert detail["timezone"] == "Asia/Kolkata"
        assert detail["phone_number"] is None

    def test_reading_never_creates_credit_accounts(self, platform_client, clinic):
        platform_client.get(f"{BASE}/hospitals/")
        platform_client.get(f"{BASE}/hospitals/{clinic.id}/")
        platform_client.get(f"{BASE}/overview/")
        assert CreditAccount.objects.count() == 0

    def test_edit_details_and_hours(self, platform_client, clinic):
        response = platform_client.patch(f"{BASE}/hospitals/{clinic.id}/", {
            "address": "MG Road", "website": "https://h.example", "timezone": "Asia/Dubai",
            "working_hours_start": "08:00", "working_hours_end": "18:30", "support_email": "help@h.example",
        }, format="json")
        assert response.status_code == 200, response.content
        body = response.json()
        assert (body["address"], body["timezone"], body["working_hours_start"], body["working_hours_end"]) == ("MG Road", "Asia/Dubai", "08:00", "18:30")
        clinic.refresh_from_db()
        assert clinic.website == "https://h.example"
        assert PlatformAuditLog.objects.filter(action="update_details", clinic=clinic).exists()

    @pytest.mark.parametrize("payload,field", [
        ({"timezone": "Mars/Base"}, "timezone"),
        ({"working_hours_start": "18:00", "working_hours_end": "09:00"}, "working_hours_end"),
        ({"website": "not a url"}, "website"),
        ({"support_email": "nope"}, "support_email"),
    ])
    def test_bad_details_are_refused(self, platform_client, clinic, payload, field):
        response = platform_client.patch(f"{BASE}/hospitals/{clinic.id}/", payload, format="json")
        assert response.status_code == 400 and field in response.json()

    def test_names_are_unique_across_hospitals_ignoring_case(self, platform_client, clinic, clinic_b):
        response = platform_client.patch(f"{BASE}/hospitals/{clinic.id}/", {"name": "OTHER CLINIC"}, format="json")
        assert response.status_code == 400 and "name" in response.json()
        assert platform_client.patch(f"{BASE}/hospitals/{clinic.id}/", {"name": "Renamed Clinic"}, format="json").status_code == 200

    def test_status_changes_follow_the_allowed_steps_and_are_audited(self, platform_client, platform_admin, clinic):
        url = f"{BASE}/hospitals/{clinic.id}/"
        assert platform_client.patch(url, {"status": "archived"}, format="json").status_code == 400  # active -> archived directly
        assert platform_client.patch(url, {"status": "suspended"}, format="json").json()["status"] == "suspended"
        assert platform_client.patch(url, {"status": "archived"}, format="json").json()["status"] == "archived"
        assert platform_client.patch(url, {"status": "active"}, format="json").status_code == 400  # archived -> active directly
        assert platform_client.patch(url, {"status": "suspended"}, format="json").json()["status"] == "suspended"
        assert platform_client.patch(url, {"status": "active"}, format="json").json()["status"] == "active"

        actions = list(PlatformAuditLog.objects.filter(clinic=clinic).order_by("id").values_list("action", flat=True))
        assert actions == ["suspend_hospital", "archive_hospital", "suspend_hospital", "reactivate_hospital"]
        assert PlatformAuditLog.objects.first().actor_email == platform_admin.email

    def test_overview_counts_real_calls_only(self, platform_client, clinic):
        call_log(clinic, duration_seconds=61)                       # 2 minutes (each call rounds up)
        call_log(clinic, duration_seconds=60)                       # 1 minute
        call_log(clinic, duration_seconds=500, is_test=True)        # a staff test call: not counted
        call_log(clinic, duration_seconds=0, transcript="")         # an empty call: not counted
        body = platform_client.get(f"{BASE}/overview/").json()
        assert (body["calls_30d"], body["minutes_30d"]) == (2, 3)
        assert body["hospitals"] == 1 and body["active"] == 1
        assert [row["id"] for row in body["low_balance"]] == [clinic.id]  # balance 0 is under the 500 warning level


# ── credits ───────────────────────────────────────────────────────────────────

class TestCredits:
    def url(self, clinic):
        return f"{BASE}/hospitals/{clinic.id}/credits/"

    def test_add_then_set(self, platform_client, platform_admin, clinic):
        assert platform_client.post(self.url(clinic), {"mode": "add", "amount": 10000, "note": "Invoice 1"}, format="json").json() == {"balance": 10000}
        assert platform_client.post(self.url(clinic), {"mode": "set", "amount": 4000}, format="json").json() == {"balance": 4000}

        entries = list(CreditLedger.objects.filter(account__clinic=clinic).order_by("id"))
        assert [(e.entry_type, e.amount, e.balance_after) for e in entries] == [("grant", 10000, 10000), ("adjustment", -6000, 4000)]
        assert entries[0].note == "Invoice 1" and entries[0].actor_email == platform_admin.email
        assert sum(e.amount for e in entries) == CreditAccount.objects.get(clinic=clinic).balance  # the ledger adds up to the balance

    def test_set_to_the_same_balance_writes_nothing(self, platform_client, clinic):
        platform_client.post(self.url(clinic), {"mode": "add", "amount": 500}, format="json")
        platform_client.post(self.url(clinic), {"mode": "set", "amount": 500}, format="json")
        assert CreditLedger.objects.count() == 1

    @pytest.mark.parametrize("payload", [
        {"mode": "add", "amount": 0}, {"mode": "add", "amount": -5}, {"mode": "add", "amount": "lots"},
        {"mode": "refund", "amount": 5}, {"mode": "add"}, {"amount": 5},
    ])
    def test_bad_requests_change_nothing(self, platform_client, clinic, payload):
        assert platform_client.post(self.url(clinic), payload, format="json").status_code == 400
        assert CreditLedger.objects.count() == 0

    def test_ledger_is_newest_first_and_paged(self, platform_client, clinic):
        for n in range(1, 31):
            platform_client.post(self.url(clinic), {"mode": "add", "amount": n}, format="json")
        first = platform_client.get(f"{BASE}/hospitals/{clinic.id}/ledger/").json()
        assert first["count"] == 30 and len(first["results"]) == 25
        assert first["results"][0]["amount"] == 30
        second = platform_client.get(f"{BASE}/hospitals/{clinic.id}/ledger/?page=2").json()
        assert len(second["results"]) == 5

    def test_ledger_is_append_only(self, clinic):
        account = credits.account_for(clinic)
        entry = credits.add_credits(account, 100)
        entry.note = "changed"
        with pytest.raises(ValueError):
            entry.save()
        with pytest.raises(ProtectedError):
            entry.delete()
        with pytest.raises(ProtectedError):
            account.delete()  # the account (and so its history) can't be removed either

    def test_a_call_can_only_be_charged_once(self, clinic):
        account = credits.account_for(clinic)
        call = call_log(clinic)
        credits.post_entry(account, CreditLedger.EntryType.USAGE, -20, call_log=call)
        with pytest.raises(IntegrityError):
            credits.post_entry(account, CreditLedger.EntryType.USAGE, -20, call_log=call)
        assert CreditAccount.objects.get(pk=account.pk).balance == -20  # the failed charge rolled back

    def test_balance_may_go_negative(self, clinic):
        account = credits.account_for(clinic)
        credits.post_entry(account, CreditLedger.EntryType.USAGE, -50)
        assert CreditAccount.objects.get(pk=account.pk).balance == -50

    def test_an_unknown_hospital_is_not_found(self, platform_client):
        assert platform_client.post(f"{BASE}/hospitals/999999/credits/", {"mode": "add", "amount": 5}, format="json").status_code == 404


# ── plan, limits, defaults ────────────────────────────────────────────────────

class TestPlanAndDefaults:
    def test_change_plan_rate_limits_and_warning_level(self, platform_client, clinic):
        response = platform_client.patch(f"{BASE}/hospitals/{clinic.id}/plan/", {
            "plan": "enterprise", "credits_per_minute": 12, "low_balance_threshold": 900, "max_admins": 3, "max_doctors": 40,
        }, format="json")
        assert response.status_code == 200, response.content
        body = response.json()
        assert body["plan"] == "enterprise" and body["credits_per_minute"] == 12 and body["low_balance_threshold"] == 900
        assert body["limits"]["clinic_admin"]["limit"] == 3 and body["limits"]["doctor"]["limit"] == 40
        assert credits.rate_for(clinic) == 12

    def test_the_plans_own_rate_needs_no_override(self, platform_client, clinic):
        platform_client.patch(f"{BASE}/hospitals/{clinic.id}/plan/", {"credits_per_minute": 12}, format="json")
        platform_client.patch(f"{BASE}/hospitals/{clinic.id}/plan/", {"credits_per_minute": 10}, format="json")
        clinic.entitlement.refresh_from_db()
        assert clinic.entitlement.credits_per_minute_override is None and credits.rate_for(clinic) == 10

    @pytest.mark.parametrize("payload", [{"plan": "gold"}, {"credits_per_minute": 0}, {"max_doctors": -1}, {"low_balance_threshold": "x"}])
    def test_bad_plan_changes_are_refused(self, platform_client, clinic, payload):
        assert platform_client.patch(f"{BASE}/hospitals/{clinic.id}/plan/", payload, format="json").status_code == 400

    def test_defaults_round_trip_and_never_touch_existing_hospitals(self, platform_client, clinic):
        assert platform_client.get(f"{BASE}/defaults/").json()["credits_per_minute"] == 10
        body = {"plan": "growth", "credits_per_minute": 14, "opening_credits": 0, "low_balance_threshold": 300, "max_admins": 2, "max_doctors": 25, "max_receptionists": 6}
        assert platform_client.put(f"{BASE}/defaults/", body, format="json").status_code == 200
        assert PlatformDefaults.load().credits_per_minute == 14
        assert credits.rate_for(clinic) == 10
        assert platform_client.put(f"{BASE}/defaults/", {**body, "credits_per_minute": 0}, format="json").status_code == 400

    def test_audit_log_filters_by_hospital(self, platform_client, clinic, clinic_b):
        platform_client.post(f"{BASE}/hospitals/{clinic.id}/credits/", {"mode": "add", "amount": 5}, format="json")
        platform_client.post(f"{BASE}/hospitals/{clinic_b.id}/credits/", {"mode": "add", "amount": 7}, format="json")
        everything = platform_client.get(f"{BASE}/audit-log/").json()
        mine = platform_client.get(f"{BASE}/audit-log/?hospital={clinic.id}").json()
        assert everything["count"] == 2 and mine["count"] == 1
        assert mine["results"][0]["action"] == "add_credits" and mine["results"][0]["target"] == "Test Clinic"
        assert platform_client.get(f"{BASE}/audit-log/?hospital=abc").json()["count"] == 0
