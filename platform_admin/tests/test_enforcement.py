"""A suspended hospital is switched off everywhere; phone numbers are the platform's; names and deletes are safe."""
from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.db.models import ProtectedError
from rest_framework.test import APIClient

from accounts.models import User
from accounts.services import ensure_hospital_active, rotate_refresh_token, mint_tokens
from ai_agent.models import CallLog
from appointments import outbound
from clinics.models import Clinic
from clinics.status import PHONE_LOCKED_MESSAGE, SUSPENDED_MESSAGE
from patients.models import Patient
from platform_admin import credits
from platform_admin.models import CreditAccount, CreditLedger, PlatformAuditLog
from platform_admin.tests.test_platform_api import call_log, client_for, make_user

pytestmark = pytest.mark.django_db


def suspend(clinic, status=Clinic.Status.SUSPENDED):
    clinic.status = status
    clinic.save()


@pytest.fixture
def rock8_headers(settings):
    settings.ROCK8_WEBHOOK_SECRET = "test-secret"
    return {"HTTP_AUTHORIZATION": "Bearer test-secret"}


# ── suspension ────────────────────────────────────────────────────────────────

class TestSuspension:
    @pytest.mark.parametrize("status", [Clinic.Status.SUSPENDED, Clinic.Status.ARCHIVED])
    def test_staff_of_a_blocked_hospital_are_refused_by_every_hospital_endpoint(self, clinic, clinic_admin, receptionist_user, status):
        suspend(clinic, status)
        for user in (clinic_admin, receptionist_user):
            response = client_for(user).get("/api/v1/accounts/access/" if user is clinic_admin else "/api/v1/patients/")
            assert response.status_code == 403, user.role
            assert SUSPENDED_MESSAGE in str(response.data)

    def test_a_centre_follows_its_hospital(self, clinic, clinic_admin):
        centre = Clinic.objects.create(name="Centre", parent=clinic)
        suspend(clinic)
        assert centre.is_blocked

    def test_sign_in_is_refused_with_the_reason(self, clinic, clinic_admin):
        suspend(clinic)
        response = APIClient().post("/api/v1/auth/login/", {"email": clinic_admin.email, "password": "AdminPass123!"}, format="json")
        assert response.status_code in (401, 403)
        assert SUSPENDED_MESSAGE in str(response.data)

    def test_signed_in_sessions_cannot_be_refreshed(self, clinic, clinic_admin):
        _, refresh = mint_tokens(clinic_admin)
        suspend(clinic)
        from rest_framework.exceptions import AuthenticationFailed
        with pytest.raises(AuthenticationFailed):
            rotate_refresh_token(refresh)

    def test_heykoalas_staff_can_still_sign_in_to_put_it_right(self, clinic):
        staff = make_user("staff@h.example", clinic=clinic, is_staff=True)
        suspend(clinic)
        ensure_hospital_active(staff)  # does not raise

    def test_reactivating_restores_everything(self, clinic, clinic_admin):
        suspend(clinic)
        assert client_for(clinic_admin).get("/api/v1/accounts/access/").status_code == 403
        suspend(clinic, Clinic.Status.ACTIVE)
        assert client_for(clinic_admin).get("/api/v1/accounts/access/").status_code == 200

    def test_a_new_call_gets_no_agent_but_call_events_are_still_recorded(self, api_client, clinic, rock8_headers):
        suspend(clinic)
        url = f"/api/v1/ai/webhooks/rock8/{clinic.id}/"

        refused = api_client.post(url, {"room_name": "room-1", "from_number": "919999999999"}, format="json", **rock8_headers)
        assert refused.status_code == 403 and refused.json()["error"] == "SUSPENDED"
        assert CallLog.objects.count() == 0  # no call log was started for it

        event = {"event": "call.ended", "call": {"call_id": "c-1", "direction": "inbound", "from_number": "+919999999999", "duration_seconds": 42}}
        assert api_client.post(url, event, format="json", **rock8_headers).status_code == 200
        assert CallLog.objects.filter(external_call_id="c-1", duration_seconds=42).exists()  # a call that was already running is not lost

    def test_the_agents_tools_stop(self, api_client, clinic, rock8_headers):
        tools = f"/api/v1/ai/tools/rock8/{clinic.id}/doctors/"
        assert api_client.get(tools, **rock8_headers).status_code == 200
        suspend(clinic)
        refused = api_client.get(tools, **rock8_headers)
        assert refused.status_code == 403 and SUSPENDED_MESSAGE in str(refused.data)

    def test_the_dialler_places_nothing(self, clinic):
        suspend(clinic)
        with pytest.raises(outbound.DialError, match="suspended"):
            outbound.place_call(SimpleNamespace(clinic=clinic))


# ── phone numbers belong to the platform ──────────────────────────────────────

class TestPhoneNumbersArePlatformOnly:
    def test_a_hospital_admin_can_see_the_number_but_change_nothing(self, admin_client):
        read = admin_client.get("/api/v1/clinics/phone-number/")
        assert read.status_code == 200 and read.json()["can_purchase"] is False

        for method, url, body in [
            ("post", "/api/v1/clinics/phone-number/", {"number": "918031150501"}),
            ("delete", "/api/v1/clinics/phone-number/", None),
            ("post", "/api/v1/clinics/phone-number/activate/", None),
            ("get", "/api/v1/clinics/phone-number/search/?city=Bangalore", None),
        ]:
            response = getattr(admin_client, method)(url, body, format="json") if body else getattr(admin_client, method)(url)
            assert response.status_code == 403, (method, url)
            assert response.json()["detail"] == PHONE_LOCKED_MESSAGE

    def test_platform_staff_get_past_the_lock(self, clinic):
        staff = client_for(make_user("staff@h.example", clinic=clinic, is_staff=True))
        # An invalid number is rejected by the number check, which only runs once the lock is passed.
        response = staff.post("/api/v1/clinics/phone-number/", {"number": "12"}, format="json")
        assert response.status_code == 400 and "number" in response.json()


# ── names and deletes ─────────────────────────────────────────────────────────

class TestNamesAndDeletes:
    def test_two_hospitals_can_each_have_a_centre_with_the_same_name(self, admin_client, clinic_b):
        other_admin = client_for(make_user("admin@other.example", clinic=clinic_b))
        assert admin_client.post("/api/v1/clinics/", {"name": "Jayanagar"}, format="json").status_code == 201
        assert other_admin.post("/api/v1/clinics/", {"name": "Jayanagar"}, format="json").status_code == 201
        assert Clinic.objects.filter(name="Jayanagar").count() == 2

    def test_a_hospital_cannot_repeat_a_centre_name(self, admin_client):
        assert admin_client.post("/api/v1/clinics/", {"name": "Jayanagar"}, format="json").status_code == 201
        again = admin_client.post("/api/v1/clinics/", {"name": "jayanagar"}, format="json")
        assert again.status_code == 400 and "name" in again.json()

    def test_a_centre_cannot_take_another_hospitals_name(self, admin_client, clinic_b):
        response = admin_client.post("/api/v1/clinics/", {"name": clinic_b.name}, format="json")
        assert response.status_code == 201  # a centre's name only has to be unique inside its own hospital

    def test_the_database_itself_refuses_a_duplicate_hospital(self, clinic):
        from django.db import IntegrityError, transaction
        with pytest.raises(IntegrityError), transaction.atomic():
            Clinic.objects.create(name="TEST CLINIC")

    def test_a_hospital_can_never_be_deleted(self, clinic):
        from django.db import transaction
        # Each refusal gets its own savepoint: Django sends delete signals inside a transaction it then abandons.
        with pytest.raises(ProtectedError), transaction.atomic():
            clinic.delete()
        with pytest.raises(ProtectedError), transaction.atomic():
            Clinic.objects.filter(pk=clinic.pk).delete()  # bulk deletes (e.g. the Django admin) too
        assert Clinic.objects.filter(pk=clinic.pk).exists()

    def test_a_centre_with_patients_cannot_be_deleted(self, clinic):
        centre = Clinic.objects.create(name="Centre", parent=clinic)
        Patient.objects.create(clinic=centre, full_name="P", phone="9999999999")
        with pytest.raises(ProtectedError):
            centre.delete()

    def test_call_logs_outlive_a_clinic_delete(self, clinic):
        centre = Clinic.objects.create(name="Centre", parent=clinic)
        call_log(centre)
        with pytest.raises(ProtectedError):
            centre.delete()
        assert CallLog.objects.count() == 1


# ── adopting an existing hospital ─────────────────────────────────────────────

class TestAdoptHospital:
    def run(self, clinic, **options):
        out = StringIO()
        call_command("adopt_hospital", clinic_id=clinic.id, stdout=out, **options)
        return out.getvalue()

    def test_dry_run_changes_nothing(self, clinic):
        before = (clinic.subscription_status, clinic.entitlement.plan.name)
        assert "Dry run" in self.run(clinic, dry_run=True)
        clinic.refresh_from_db()
        clinic.entitlement.refresh_from_db()
        assert (clinic.subscription_status, clinic.entitlement.plan.name) == before
        assert CreditAccount.objects.count() == 0 and PlatformAuditLog.objects.count() == 0

    def test_adopts_and_is_safe_to_repeat(self, clinic, clinic_admin, doctor_user):
        self.run(clinic, opening=250)
        self.run(clinic, opening=250)  # the second run must not grant the opening credits again

        clinic.refresh_from_db()
        clinic.entitlement.refresh_from_db()
        assert clinic.subscription_status == Clinic.SubscriptionStatus.ACTIVE and clinic.status == Clinic.Status.ACTIVE
        assert clinic.entitlement.plan.name == "growth" and credits.rate_for(clinic) == 10
        assert CreditAccount.objects.get(clinic=clinic).balance == 250
        assert list(CreditLedger.objects.values_list("entry_type", "amount")) == [("grant", 250)]
        assert clinic.entitlement.max_admins_override >= 1 and clinic.entitlement.max_doctors_override >= 1

    def test_never_sets_a_limit_below_the_people_already_there(self, clinic, clinic_admin):
        for n in range(3):
            make_user(f"extra{n}@h.example", clinic=clinic, role=User.Role.CLINIC_ADMIN)
        self.run(clinic, max_admins=1)
        clinic.entitlement.refresh_from_db()
        assert clinic.entitlement.max_admins_override == 4  # 4 admins exist

    def test_a_centre_is_not_a_hospital(self, clinic):
        from django.core.management.base import CommandError
        centre = Clinic.objects.create(name="Centre", parent=clinic)
        with pytest.raises(CommandError):
            call_command("adopt_hospital", clinic_id=centre.id, stdout=StringIO())


# ── the hospital admin's own credit box ───────────────────────────────────────

class TestHospitalCreditBalance:
    URL = "/api/v1/subscriptions/credits/"

    def test_a_hospital_admin_sees_balance_rate_and_minutes_left(self, admin_client, clinic):
        credits.add_credits(credits.account_for(clinic), 1234)
        body = admin_client.get(self.URL).json()
        assert body == {"balance": 1234, "credits_per_minute": 10, "low_balance_threshold": 500, "minutes_left": 123}

    def test_no_account_yet_reads_as_zero_and_creates_nothing(self, admin_client):
        assert admin_client.get(self.URL).json()["balance"] == 0
        assert CreditAccount.objects.count() == 0

    def test_a_negative_balance_never_shows_negative_minutes(self, admin_client, clinic):
        credits.post_entry(credits.account_for(clinic), CreditLedger.EntryType.USAGE, -30)
        assert admin_client.get(self.URL).json()["minutes_left"] == 0

    def test_only_the_hospitals_admin_may_look(self, receptionist_client, doctor_client, api_client):
        for client in (receptionist_client, doctor_client):
            assert client.get(self.URL).status_code == 403
        assert api_client.get(self.URL).status_code in (401, 403)

    def test_a_hospital_sees_only_its_own_credits(self, admin_client, clinic_b):
        credits.add_credits(credits.account_for(clinic_b), 9999)
        assert admin_client.get(self.URL).json()["balance"] == 0
