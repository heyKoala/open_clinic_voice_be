"""The platform admin's tools for one hospital: people, centres, phone number and integrations."""
from __future__ import annotations

import json
from unittest import mock

import pytest
from django.core import mail
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

from accounts.models import Invitation, User
from accounts.services import accept_invitation, mint_tokens
from clinics.models import Clinic, ClinicConfiguration, ClinicIntegration, ClinicPhoneNumber
from clinics.test_phone_numbers import NUMBER_A, NUMBER_B, FakePlivo
from doctors.models import Doctor
from patients.models import Patient
from platform_admin.models import PlatformAuditLog
from platform_admin.tests.test_platform_api import client_for, make_user
from platform_admin.tests.test_provision import GOOD_PASSWORD, signed_token_from_the_email

pytestmark = pytest.mark.django_db

BASE = "/api/v1/platform/hospitals"


@pytest.fixture
def platform_client():
    return client_for(make_user("platform@heykoala.example", is_staff=True))


@pytest.fixture(autouse=True)
def telephony_settings(settings):
    settings.PLIVO_AUTH_ID = "MAUTHID"
    settings.PLIVO_AUTH_TOKEN = "token"
    settings.PLIVO_APP_ID = "23626388960837953"


def receptionist(clinic, email):
    return make_user(email, clinic=clinic, role=User.Role.RECEPTIONIST)


def audit(clinic, action):
    return PlatformAuditLog.objects.filter(clinic=clinic, action=action)


# ── people ────────────────────────────────────────────────────────────────────

class TestUserList:
    def test_lists_people_and_pending_invites_of_this_hospital_only(self, platform_client, clinic, clinic_admin, clinic_b):
        make_user("other@b.example", clinic=clinic_b, role=User.Role.RECEPTIONIST)
        centre = Clinic.objects.create(name="Centre", parent=clinic)
        make_user("atcentre@h.example", clinic=centre, role=User.Role.RECEPTIONIST)
        platform_client.post(f"{BASE}/{clinic.id}/users/", {"email": "new@h.example", "role": "receptionist"}, format="json")

        rows = platform_client.get(f"{BASE}/{clinic.id}/users/").json()
        by_email = {r["email"]: r for r in rows}
        assert set(by_email) == {clinic_admin.email, "atcentre@h.example", "new@h.example"}
        assert by_email[clinic_admin.email]["status"] == "active" and by_email[clinic_admin.email]["id"] > 0
        assert by_email["new@h.example"]["status"] == "invited" and by_email["new@h.example"]["id"] < 0  # an invitation, not a user

    def test_only_the_platform_login_may_look(self, api_client, clinic_admin, clinic):
        assert api_client.get(f"{BASE}/{clinic.id}/users/").status_code == 404
        assert client_for(clinic_admin).get(f"{BASE}/{clinic.id}/users/").status_code == 404


class TestAddPerson:
    def test_sends_an_invite_and_creates_no_account_yet(self, platform_client, clinic):
        response = platform_client.post(f"{BASE}/{clinic.id}/users/", {"email": " New@H.example ", "role": "receptionist"}, format="json")
        assert response.status_code == 201, response.content
        body = response.json()
        assert (body["email"], body["role"], body["status"], body["invite_sent"]) == ("new@h.example", "receptionist", "invited", True)
        assert not User.objects.filter(email="new@h.example").exists()
        assert Invitation.objects.filter(clinic=clinic, email="new@h.example", role="receptionist").count() == 1
        assert len(mail.outbox) == 1 and mail.outbox[0].to == ["new@h.example"]
        assert audit(clinic, "add_user").count() == 1

    def test_an_admin_can_be_invited_as_also_a_doctor(self, platform_client, clinic):
        clinic.entitlement.max_admins_override = 3
        clinic.entitlement.save()
        platform_client.post(f"{BASE}/{clinic.id}/users/", {"email": "doc@h.example", "role": "clinic_admin", "is_also_doctor": True}, format="json")
        user = accept_invitation(signed_token=signed_token_from_the_email(), full_name="Dr Admin", password=GOOD_PASSWORD)
        assert user.is_doctor and Doctor.objects.filter(user=user).exists()

    def test_refuses_an_email_that_already_has_an_account(self, platform_client, clinic, clinic_b):
        make_user("taken@b.example", clinic=clinic_b)
        response = platform_client.post(f"{BASE}/{clinic.id}/users/", {"email": "TAKEN@b.example", "role": "doctor"}, format="json")
        assert response.status_code == 400 and "email" in response.json()
        assert Invitation.objects.count() == 0 and len(mail.outbox) == 0

    def test_refuses_when_the_plan_has_no_seat_left_and_says_how_to_fix_it(self, platform_client, clinic, clinic_admin):
        response = platform_client.post(f"{BASE}/{clinic.id}/users/", {"email": "second@h.example", "role": "clinic_admin"}, format="json")
        assert response.status_code == 400
        assert "allows 1 hospital admins" in response.json()["detail"] and "Plan & limits" in response.json()["detail"]
        assert Invitation.objects.count() == 0

    def test_unsent_invites_still_count_so_a_seat_cannot_be_over_booked(self, platform_client, clinic):
        for n in range(2):  # receptionist seats: 2
            assert platform_client.post(f"{BASE}/{clinic.id}/users/", {"email": f"r{n}@h.example", "role": "receptionist"}, format="json").status_code == 201
        assert platform_client.post(f"{BASE}/{clinic.id}/users/", {"email": "r3@h.example", "role": "receptionist"}, format="json").status_code == 400

    @pytest.mark.parametrize("body", [{"email": "nope", "role": "doctor"}, {"email": "a@h.example", "role": "owner"}, {"role": "doctor"}, {"email": "a@h.example"}])
    def test_bad_input_is_refused(self, platform_client, clinic, body):
        assert platform_client.post(f"{BASE}/{clinic.id}/users/", body, format="json").status_code == 400

    def test_a_failed_email_still_saves_the_invite_for_resending(self, platform_client, clinic):
        with mock.patch("accounts.services.send_mail", side_effect=RuntimeError("smtp down")):
            response = platform_client.post(f"{BASE}/{clinic.id}/users/", {"email": "new@h.example", "role": "doctor"}, format="json")
        assert response.status_code == 201 and response.json()["invite_sent"] is False
        assert Invitation.objects.filter(email="new@h.example").exists()


class TestChangePerson:
    def test_rename(self, platform_client, clinic, clinic_admin):
        person = receptionist(clinic, "desk@h.example")
        response = platform_client.patch(f"{BASE}/{clinic.id}/users/{person.id}/", {"full_name": "  Asha Rao "}, format="json")
        assert response.status_code == 200 and response.json()["full_name"] == "Asha Rao"
        person.refresh_from_db()
        assert person.full_name == "Asha Rao" and audit(clinic, "update_user").count() == 1
        assert platform_client.patch(f"{BASE}/{clinic.id}/users/{person.id}/", {"full_name": "  "}, format="json").status_code == 400

    def test_suspending_stops_sign_in_ends_sessions_and_keeps_the_account(self, platform_client, clinic, clinic_admin):
        person = receptionist(clinic, "desk@h.example")
        doctor = make_user("doc@h.example", clinic=clinic, role=User.Role.DOCTOR)
        profile = Doctor.objects.create(clinic=clinic, user=doctor, full_name="Dr")
        _, refresh = mint_tokens(person)

        assert platform_client.patch(f"{BASE}/{clinic.id}/users/{person.id}/", {"status": "suspended"}, format="json").json()["status"] == "suspended"
        person.refresh_from_db()
        assert not person.is_active and person.membership_status == "suspended"
        assert BlacklistedToken.objects.filter(token__user=person).exists()
        assert APIClient().post("/api/v1/auth/login/", {"email": person.email, "password": "SomePass12345!"}, format="json").status_code in (401, 403)

        platform_client.patch(f"{BASE}/{clinic.id}/users/{doctor.id}/", {"status": "suspended"}, format="json")
        profile.refresh_from_db()
        assert not profile.is_active  # their calendar stops taking bookings too

        assert platform_client.patch(f"{BASE}/{clinic.id}/users/{person.id}/", {"status": "active"}, format="json").json()["status"] == "active"
        person.refresh_from_db()
        assert person.is_active and person.membership_status == "active"
        assert User.objects.filter(pk=person.pk).exists()  # nothing was ever deleted

    def test_the_only_active_admin_cannot_be_suspended(self, platform_client, clinic, clinic_admin):
        response = platform_client.patch(f"{BASE}/{clinic.id}/users/{clinic_admin.id}/", {"status": "suspended"}, format="json")
        assert response.status_code == 400 and "only active hospital admin" in response.json()["detail"]
        clinic_admin.refresh_from_db()
        assert clinic_admin.is_active

        clinic.entitlement.max_admins_override = 2
        clinic.entitlement.save()
        make_user("second@h.example", clinic=clinic, role=User.Role.CLINIC_ADMIN)
        assert platform_client.patch(f"{BASE}/{clinic.id}/users/{clinic_admin.id}/", {"status": "suspended"}, format="json").status_code == 200

    def test_reactivating_needs_a_free_seat(self, platform_client, clinic, clinic_admin):
        first = receptionist(clinic, "r1@h.example")
        platform_client.patch(f"{BASE}/{clinic.id}/users/{first.id}/", {"status": "suspended"}, format="json")
        receptionist(clinic, "r2@h.example")
        receptionist(clinic, "r3@h.example")  # both receptionist seats are now taken by others
        response = platform_client.patch(f"{BASE}/{clinic.id}/users/{first.id}/", {"status": "active"}, format="json")
        assert response.status_code == 400 and "allows 2 receptionists" in response.json()["detail"]
        first.refresh_from_db()
        assert not first.is_active

    def test_an_invite_is_not_a_person_to_edit(self, platform_client, clinic):
        row = platform_client.post(f"{BASE}/{clinic.id}/users/", {"email": "n@h.example", "role": "doctor"}, format="json").json()
        assert platform_client.patch(f"{BASE}/{clinic.id}/users/{row['id']}/", {"full_name": "X"}, format="json").status_code == 400

    def test_nobody_outside_this_hospital_can_be_reached(self, platform_client, clinic, clinic_b):
        stranger = make_user("stranger@b.example", clinic=clinic_b, role=User.Role.RECEPTIONIST)
        for path, method, body in [
            (f"{BASE}/{clinic.id}/users/{stranger.id}/", "patch", {"full_name": "Hacked"}),
            (f"{BASE}/{clinic.id}/users/{stranger.id}/reset-password/", "post", {}),
            (f"{BASE}/{clinic.id}/users/{stranger.id}/set-password/", "post", {"password": GOOD_PASSWORD}),
            (f"{BASE}/{clinic.id}/users/999999/", "patch", {"full_name": "X"}),
        ]:
            assert getattr(platform_client, method)(path, body, format="json").status_code == 404, path
        stranger.refresh_from_db()
        assert stranger.full_name == "Someone" and stranger.check_password("SomePass12345!")


class TestInvites:
    def test_resending_cancels_the_old_invite_and_never_double_counts_the_seat(self, platform_client, clinic):
        row = platform_client.post(f"{BASE}/{clinic.id}/users/", {"email": "n@h.example", "role": "receptionist"}, format="json").json()
        first_token = signed_token_from_the_email()
        response = platform_client.post(f"{BASE}/{clinic.id}/users/{row['id']}/resend-invite/")
        assert response.status_code == 200 and response.json() == {"sent": True}

        invites = Invitation.objects.filter(email="n@h.example")
        assert invites.count() == 2 and invites.filter(revoked_at__isnull=True).count() == 1
        assert len(mail.outbox) == 2
        with pytest.raises(DRFValidationError):  # the old link no longer works
            accept_invitation(signed_token=first_token, full_name="N", password=GOOD_PASSWORD)
        user = accept_invitation(signed_token=signed_token_from_the_email(), full_name="N", password=GOOD_PASSWORD)  # the new one does
        assert user.clinic_id == clinic.id

    def test_a_resend_whose_email_fails_says_so_but_keeps_one_live_invite(self, platform_client, clinic):
        row = platform_client.post(f"{BASE}/{clinic.id}/users/", {"email": "n@h.example", "role": "receptionist"}, format="json").json()
        with mock.patch("accounts.services.send_mail", side_effect=RuntimeError("smtp down")):
            response = platform_client.post(f"{BASE}/{clinic.id}/users/{row['id']}/resend-invite/")
        assert response.status_code == 502 and "email could not be sent" in response.json()["detail"]
        assert Invitation.objects.filter(email="n@h.example", revoked_at__isnull=True).count() == 1

    def test_cancelling_kills_the_link_and_frees_the_seat(self, platform_client, clinic):
        row = platform_client.post(f"{BASE}/{clinic.id}/users/", {"email": "n@h.example", "role": "receptionist"}, format="json").json()
        token = signed_token_from_the_email()
        assert platform_client.post(f"{BASE}/{clinic.id}/users/{row['id']}/revoke-invite/").status_code == 200
        with pytest.raises(DRFValidationError):
            accept_invitation(signed_token=token, full_name="N", password=GOOD_PASSWORD)
        assert platform_client.get(f"{BASE}/{clinic.id}/users/").json() == [] or all(r["email"] != "n@h.example" for r in platform_client.get(f"{BASE}/{clinic.id}/users/").json())
        assert audit(clinic, "revoke_invite").count() == 1

    def test_invites_of_other_hospitals_and_joined_people_are_not_touchable(self, platform_client, clinic, clinic_b, clinic_admin):
        other = platform_client.post(f"{BASE}/{clinic_b.id}/users/", {"email": "n@b.example", "role": "receptionist"}, format="json").json()
        assert platform_client.post(f"{BASE}/{clinic.id}/users/{other['id']}/revoke-invite/").status_code == 404
        assert platform_client.post(f"{BASE}/{clinic.id}/users/{other['id']}/resend-invite/").status_code == 404
        assert Invitation.objects.get(email="n@b.example").revoked_at is None
        assert platform_client.post(f"{BASE}/{clinic.id}/users/{clinic_admin.id}/resend-invite/").status_code == 400  # already joined


class TestPasswords:
    def test_a_reset_link_is_emailed(self, platform_client, clinic, clinic_admin):
        assert platform_client.post(f"{BASE}/{clinic.id}/users/{clinic_admin.id}/reset-password/").json() == {"sent": True}
        assert len(mail.outbox) == 1 and "reset-password?token=" in mail.outbox[0].body and mail.outbox[0].to == [clinic_admin.email]
        assert audit(clinic, "reset_password").count() == 1

    def test_a_failed_reset_email_is_reported(self, platform_client, clinic, clinic_admin):
        with mock.patch("platform_admin.views_hospital.send_password_reset_email", side_effect=RuntimeError("smtp down")):
            assert platform_client.post(f"{BASE}/{clinic.id}/users/{clinic_admin.id}/reset-password/").status_code == 502

    def test_setting_a_password_replaces_the_old_one_ends_sessions_and_unlocks(self, platform_client, clinic, clinic_admin):
        person = receptionist(clinic, "desk@h.example")
        mint_tokens(person)
        person.failed_login_attempts = 7
        person.save()
        response = platform_client.post(f"{BASE}/{clinic.id}/users/{person.id}/set-password/", {"password": GOOD_PASSWORD}, format="json")
        assert response.status_code == 200 and GOOD_PASSWORD not in response.content.decode()

        person.refresh_from_db()
        assert person.check_password(GOOD_PASSWORD) and person.must_change_password is True and person.failed_login_attempts == 0
        assert BlacklistedToken.objects.filter(token__user=person).exists()
        assert APIClient().post("/api/v1/auth/login/", {"email": person.email, "password": "SomePass12345!"}, format="json").status_code in (401, 403)
        login = APIClient().post("/api/v1/auth/login/", {"email": person.email, "password": GOOD_PASSWORD}, format="json")
        assert login.status_code == 200 and login.json()["must_change_password"] is True

        entry = audit(clinic, "set_password").get()
        assert GOOD_PASSWORD not in json.dumps(entry.details)  # the password is never written down

    def test_the_change_at_next_sign_in_can_be_waived(self, platform_client, clinic, clinic_admin):
        platform_client.post(f"{BASE}/{clinic.id}/users/{clinic_admin.id}/set-password/", {"password": GOOD_PASSWORD, "must_change_password": False}, format="json")
        clinic_admin.refresh_from_db()
        assert clinic_admin.must_change_password is False

    @pytest.mark.parametrize("password", ["", "short", "alllowercaseletters", "NoNumbersHere!!", "1234567890123"])
    def test_a_weak_password_is_refused_and_nothing_changes(self, platform_client, clinic, clinic_admin, password):
        response = platform_client.post(f"{BASE}/{clinic.id}/users/{clinic_admin.id}/set-password/", {"password": password}, format="json")
        assert response.status_code == 400
        clinic_admin.refresh_from_db()
        assert clinic_admin.check_password("AdminPass123!")


# ── centres ───────────────────────────────────────────────────────────────────

class TestCentres:
    def test_list_has_the_main_centre_first_with_counts(self, platform_client, clinic):
        Doctor.objects.create(clinic=clinic, full_name="Dr A")
        centre = Clinic.objects.create(name="North", parent=clinic)
        Patient.objects.create(clinic=centre, full_name="P", phone="9999999999")
        rows = platform_client.get(f"{BASE}/{clinic.id}/centres/").json()
        assert [(r["name"], r["is_main"], r["doctors"], r["patients"]) for r in rows] == [("Test Clinic", True, 1, 0), ("North", False, 0, 1)]

    def test_adding_a_centre_sets_it_up_and_lets_the_admins_switch_to_it(self, platform_client, clinic, clinic_admin):
        response = platform_client.post(f"{BASE}/{clinic.id}/centres/", {"name": "Whitefield", "address": "ITPL", "phone": "080"}, format="json")
        assert response.status_code == 201, response.content
        centre = Clinic.objects.get(pk=response.json()["id"])
        assert centre.parent_id == clinic.id and centre.address == "ITPL"
        assert ClinicConfiguration.objects.filter(clinic=centre).exists() and hasattr(centre, "entitlement")
        assert clinic_admin.clinics.filter(pk=centre.pk).exists()
        assert audit(clinic, "add_centre").count() == 1

    def test_names_must_be_unique_inside_the_hospital_only(self, platform_client, clinic, clinic_b):
        assert platform_client.post(f"{BASE}/{clinic.id}/centres/", {"name": "Jayanagar"}, format="json").status_code == 201
        assert platform_client.post(f"{BASE}/{clinic_b.id}/centres/", {"name": "Jayanagar"}, format="json").status_code == 201  # another hospital: fine
        again = platform_client.post(f"{BASE}/{clinic.id}/centres/", {"name": "JAYANAGAR"}, format="json")
        assert again.status_code == 400 and "name" in again.json()
        assert platform_client.post(f"{BASE}/{clinic.id}/centres/", {"address": "x"}, format="json").status_code == 400

    def test_edit_a_centre(self, platform_client, clinic):
        centre = Clinic.objects.create(name="North", parent=clinic)
        response = platform_client.patch(f"{BASE}/{clinic.id}/centres/{centre.id}/", {"name": "North Campus", "phone": "080 1234"}, format="json")
        assert response.status_code == 200 and response.json()["name"] == "North Campus"
        centre.refresh_from_db()
        assert (centre.name, centre.phone) == ("North Campus", "080 1234") and audit(clinic, "update_centre").count() == 1
        Clinic.objects.create(name="South", parent=clinic)
        assert platform_client.patch(f"{BASE}/{clinic.id}/centres/{centre.id}/", {"name": "south"}, format="json").status_code == 400

    def test_the_main_centre_is_the_hospital_so_its_name_must_stay_unique_among_hospitals(self, platform_client, clinic, clinic_b):
        assert platform_client.patch(f"{BASE}/{clinic.id}/centres/{clinic.id}/", {"name": "other clinic"}, format="json").status_code == 400
        assert platform_client.patch(f"{BASE}/{clinic.id}/centres/{clinic.id}/", {"name": "Renamed Hospital"}, format="json").status_code == 200

    def test_deleting_an_empty_centre(self, platform_client, clinic):
        centre = Clinic.objects.create(name="Empty", parent=clinic)
        assert platform_client.delete(f"{BASE}/{clinic.id}/centres/{centre.id}/").status_code == 204
        assert not Clinic.objects.filter(pk=centre.pk).exists() and audit(clinic, "delete_centre").count() == 1

    def test_a_centre_that_holds_records_cannot_be_deleted_and_neither_can_the_main_one(self, platform_client, clinic):
        centre = Clinic.objects.create(name="Busy", parent=clinic)
        Patient.objects.create(clinic=centre, full_name="P", phone="9999999999")
        refused = platform_client.delete(f"{BASE}/{clinic.id}/centres/{centre.id}/")
        assert refused.status_code == 400 and "1 patient" in refused.json()["detail"]
        assert Clinic.objects.filter(pk=centre.pk).exists() and Patient.objects.count() == 1
        assert platform_client.delete(f"{BASE}/{clinic.id}/centres/{clinic.id}/").status_code == 400

    def test_a_centre_of_another_hospital_cannot_be_reached(self, platform_client, clinic, clinic_b):
        foreign = Clinic.objects.create(name="Foreign", parent=clinic_b)
        for method, body in [("patch", {"name": "Hacked"}), ("delete", None)]:
            call = getattr(platform_client, method)
            response = call(f"{BASE}/{clinic.id}/centres/{foreign.id}/", body, format="json") if body else call(f"{BASE}/{clinic.id}/centres/{foreign.id}/")
            assert response.status_code == 404
        foreign.refresh_from_db()
        assert foreign.name == "Foreign"


# ── phone number (the hospital app's own rules, acting for the hospital in the URL) ──

class TestPhone:
    def test_search_buy_and_the_number_belongs_to_the_hospital_not_to_the_platform_login(self, platform_client, clinic):
        fake = FakePlivo()
        with mock.patch("clinics.plivo.requests.request", side_effect=fake):
            found = platform_client.get(f"{BASE}/{clinic.id}/phone/search/", {"city": "Bangalore"})
            bought = platform_client.post(f"{BASE}/{clinic.id}/phone/", {"number": "+91 80 3180 5277", "city": "Bangalore"}, format="json")
        assert found.status_code == 200 and found.json()["numbers"][0]["number"] == "918031790436"
        assert bought.status_code == 201, bought.content
        assert bought.json()["phone_number"]["status"] == "active"
        record = ClinicPhoneNumber.objects.get()
        assert (record.clinic_id, record.number, record.bought_here) == (clinic.id, NUMBER_A, True)
        assert audit(clinic, "buy_phone").get().details == {"number": NUMBER_A}

    def test_one_number_per_hospital_and_never_one_that_is_taken(self, platform_client, clinic, clinic_b):
        ClinicPhoneNumber.objects.create(clinic=clinic_b, number=NUMBER_B, status="active")
        with mock.patch("clinics.plivo.requests.request", side_effect=FakePlivo()):
            assert platform_client.post(f"{BASE}/{clinic.id}/phone/", {"number": NUMBER_B}, format="json").status_code == 409
            assert platform_client.post(f"{BASE}/{clinic.id}/phone/", {"number": NUMBER_A}, format="json").status_code == 201
            assert platform_client.post(f"{BASE}/{clinic.id}/phone/", {"number": "918031805299"}, format="json").status_code == 409

    def test_a_provider_error_is_reported_and_nothing_is_saved(self, platform_client, clinic):
        response = mock.MagicMock(status_code=401, content=b'{"error": "authentication failed"}')
        response.json.return_value = {"error": "authentication failed"}
        with mock.patch("clinics.plivo.requests.request", return_value=response):
            result = platform_client.post(f"{BASE}/{clinic.id}/phone/", {"number": NUMBER_A}, format="json")
        assert result.status_code == 502 and "authentication failed" in result.json()["detail"]
        assert not ClinicPhoneNumber.objects.exists() and not audit(clinic, "buy_phone").exists()

    def test_connecting_retries_the_routing(self, platform_client, clinic):
        with mock.patch("clinics.plivo.requests.request", side_effect=FakePlivo(fail_assign=True)):
            assert platform_client.post(f"{BASE}/{clinic.id}/phone/", {"number": NUMBER_A}, format="json").json()["phone_number"]["status"] == "purchased"
        with mock.patch("clinics.plivo.requests.request", side_effect=FakePlivo()):
            assert platform_client.post(f"{BASE}/{clinic.id}/phone/activate/").json()["phone_number"]["status"] == "active"

    def test_activating_a_hospital_with_no_number_is_a_404(self, platform_client, clinic):
        assert platform_client.post(f"{BASE}/{clinic.id}/phone/activate/").status_code == 404

    def test_releasing_a_bought_number_gives_it_back_and_a_linked_one_is_only_disconnected(self, platform_client, clinic, clinic_b):
        ClinicPhoneNumber.objects.create(clinic=clinic, number=NUMBER_A, status="active")
        ClinicPhoneNumber.objects.create(clinic=clinic_b, number=NUMBER_B, status="active", bought_here=False)
        fake = FakePlivo()
        with mock.patch("clinics.plivo.requests.request", side_effect=fake):
            assert platform_client.delete(f"{BASE}/{clinic.id}/phone/").status_code == 204
            assert [c[:2] for c in fake.calls] == [("DELETE", f"Number/{NUMBER_A}/")]
            fake.calls.clear()
            assert platform_client.delete(f"{BASE}/{clinic_b.id}/phone/").status_code == 204
            assert fake.calls == []  # the linked number stays on the account
        assert not ClinicPhoneNumber.objects.exists()
        assert audit(clinic, "release_phone").exists() and audit(clinic_b, "disconnect_phone").exists()

    def test_linking_a_number_already_on_the_account(self, platform_client, clinic):
        info = {"number": NUMBER_A, "city": "Bangalore", "application": "/v1/Account/MAUTHID/Zentrunk/Trunk/23626388960837953/"}
        with mock.patch("clinics.plivo._request", return_value=info) as lookup:
            response = platform_client.put(f"{BASE}/{clinic.id}/phone/", {"number": "+91 80 3180 5277"}, format="json")
        assert response.status_code == 200, response.content
        assert lookup.call_args.args[:2] == ("GET", f"Number/{NUMBER_A}/")  # read-only
        record = ClinicPhoneNumber.objects.get()
        assert (record.clinic_id, record.number, record.status, record.bought_here) == (clinic.id, NUMBER_A, "active", False)
        assert ClinicConfiguration.objects.get(clinic=clinic).ai_phone_number == f"+{NUMBER_A}"

    def test_linking_refuses_what_it_should(self, platform_client, clinic, clinic_b):
        from clinics import plivo
        with mock.patch("clinics.plivo._request", side_effect=plivo.PlivoError("not found")):
            missing = platform_client.put(f"{BASE}/{clinic.id}/phone/", {"number": NUMBER_A}, format="json")
        assert missing.status_code == 400 and "not on the telephone account" in str(missing.json()["number"])
        assert platform_client.put(f"{BASE}/{clinic.id}/phone/", {"number": "12"}, format="json").status_code == 400

        ClinicPhoneNumber.objects.create(clinic=clinic_b, number=NUMBER_B, status="active")
        assert platform_client.put(f"{BASE}/{clinic.id}/phone/", {"number": NUMBER_B}, format="json").status_code == 409  # another hospital's

        ClinicPhoneNumber.objects.create(clinic=clinic, number=NUMBER_A, status="active")
        assert platform_client.put(f"{BASE}/{clinic.id}/phone/", {"number": "918031805299"}, format="json").status_code == 409  # already has one
        assert ClinicPhoneNumber.objects.count() == 2

    def test_hospital_admins_and_signed_out_visitors_cannot_use_any_of_it(self, api_client, clinic_admin, clinic):
        for client in (api_client, client_for(clinic_admin)):
            assert client.get(f"{BASE}/{clinic.id}/phone/search/").status_code == 404
            assert client.post(f"{BASE}/{clinic.id}/phone/", {"number": NUMBER_A}, format="json").status_code == 404
            assert client.delete(f"{BASE}/{clinic.id}/phone/").status_code == 404
        assert not ClinicPhoneNumber.objects.exists()


# ── integrations ──────────────────────────────────────────────────────────────

WATI = {"api_endpoint": "https://live-mt-server.wati.io/123456", "whatsapp_number": "+91 80 1234 5678"}
TOKEN = "Bearer eyJhbGciOiJIUzI1NiJ9.wati-token-abcd"


class TestIntegrations:
    def test_lists_all_three_for_the_hospital_in_the_url(self, platform_client, clinic):
        rows = platform_client.get(f"{BASE}/{clinic.id}/integrations/").json()
        assert [(r["provider"], r["status"], r["configured"]) for r in rows] == [("wati", "not_connected", False), ("pms", "not_connected", False), ("razorpay", "not_connected", False)]
        wati = rows[0]
        assert f"/clinics/wati/webhook/{clinic.id}/" in wati["webhook_url"]  # the hospital's own webhook, not the platform login's

    def test_saving_is_validated_never_returns_the_key_and_is_audited_without_it(self, platform_client, clinic):
        bad = platform_client.put(f"{BASE}/{clinic.id}/integrations/wati/", {"settings": {"api_endpoint": "nope"}}, format="json")
        assert bad.status_code == 400 and "secret" in bad.json()

        saved = platform_client.put(f"{BASE}/{clinic.id}/integrations/wati/", {"settings": WATI, "secret": TOKEN}, format="json")
        assert saved.status_code == 200 and saved.json()["has_secret"] is True and TOKEN not in saved.content.decode()
        record = ClinicIntegration.objects.get(clinic=clinic, provider="wati")
        assert "wati-token" not in record.secret_encrypted
        assert TOKEN not in json.dumps(audit(clinic, "save_integration").get().details)

    def test_checking_a_saved_connection_records_the_result(self, platform_client, clinic):
        platform_client.put(f"{BASE}/{clinic.id}/integrations/wati/", {"settings": WATI, "secret": TOKEN}, format="json")
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"messageTemplates": [{"elementName": "booking_confirmation", "status": "APPROVED"}]}
        with mock.patch("clinics.integrations.requests.get", return_value=ok):
            result = platform_client.post(f"{BASE}/{clinic.id}/integrations/wati/test/", {}, format="json").json()
        assert result["ok"] and result["templates"] == ["booking_confirmation"]
        assert ClinicIntegration.objects.get(clinic=clinic, provider="wati").status == "connected"

        refused = mock.Mock(status_code=401)
        refused.json.return_value = {}
        with mock.patch("clinics.integrations.requests.get", return_value=refused):
            failed = platform_client.post(f"{BASE}/{clinic.id}/integrations/wati/test/", {}, format="json").json()
        assert not failed["ok"] and ClinicIntegration.objects.get(clinic=clinic, provider="wati").status == "error"

    def test_disconnecting(self, platform_client, clinic):
        platform_client.put(f"{BASE}/{clinic.id}/integrations/wati/", {"settings": WATI, "secret": TOKEN}, format="json")
        assert platform_client.delete(f"{BASE}/{clinic.id}/integrations/wati/").status_code == 204
        assert not ClinicIntegration.objects.exists() and audit(clinic, "disconnect_integration").exists()
        assert platform_client.delete(f"{BASE}/{clinic.id}/integrations/wati/").status_code == 204  # nothing to disconnect is not an error

    def test_an_unknown_provider_is_a_404_and_hospitals_do_not_see_each_others_settings(self, platform_client, clinic, clinic_b):
        assert platform_client.put(f"{BASE}/{clinic.id}/integrations/carrier-pigeon/", {"settings": {}, "secret": "x"}, format="json").status_code == 404
        platform_client.put(f"{BASE}/{clinic_b.id}/integrations/wati/", {"settings": WATI, "secret": TOKEN}, format="json")
        mine = platform_client.get(f"{BASE}/{clinic.id}/integrations/").json()
        assert all(not r["configured"] for r in mine)

    def test_only_the_platform_login_may_use_them(self, api_client, clinic_admin, clinic):
        for client in (api_client, client_for(clinic_admin)):
            assert client.get(f"{BASE}/{clinic.id}/integrations/").status_code == 404
            assert client.put(f"{BASE}/{clinic.id}/integrations/wati/", {"settings": WATI, "secret": TOKEN}, format="json").status_code == 404
        assert not ClinicIntegration.objects.exists()
