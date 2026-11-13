"""Creating a hospital from the platform admin, its first admin, its logo, and the change-password flow."""
from __future__ import annotations

import copy
import io
import json
import re
from unittest import mock

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from accounts.models import Invitation, User
from accounts.services import accept_invitation
from clinics.models import Clinic, ClinicConfiguration
from doctors.models import Doctor
from patients.models import Patient
from platform_admin.models import CreditAccount, CreditLedger, PlatformAuditLog, PlatformDefaults
from platform_admin.tests.test_platform_api import client_for, make_user
from subscriptions.models import ClinicEntitlement

pytestmark = pytest.mark.django_db

BASE = "/api/v1/platform"
GOOD_PASSWORD = "Str0ng!Passw0rd"


@pytest.fixture
def platform_admin():
    return make_user("platform@heykoala.example", is_staff=True)


@pytest.fixture
def platform_client(platform_admin):
    return client_for(platform_admin)


def form(**changes) -> dict:
    data = {
        "name": "Lakeview Hospital", "address": "MG Road", "phone": "", "support_email": "help@lakeview.example",
        "admin": {"full_name": "Meera Rao", "email": "meera@lakeview.example", "is_also_doctor": False, "mode": "invite"},
        "commercials": {"plan": "growth", "credits_per_minute": 10, "opening_credits": 0, "low_balance_threshold": 500},
    }
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return data


def post_form(client, body, logo=None):
    fields = {"payload": json.dumps(body)}
    if logo is not None:
        fields["logo"] = logo
    return client.post(f"{BASE}/hospitals/", fields, format="multipart")


def image_bytes(fmt="PNG", size=(800, 600)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(out, format=fmt)
    return out.getvalue()


def upload(data: bytes, name="logo.png", content_type="image/png"):
    return SimpleUploadedFile(name, data, content_type=content_type)


def signed_token_from_the_email() -> str:
    return re.search(r"token=(\S+)", mail.outbox[-1].body).group(1)


SAFE_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#c0392b"/></svg>'


# ── the whole creation ────────────────────────────────────────────────────────

class TestCreateWithInvite:
    def test_creates_the_hospital_and_everything_it_needs(self, platform_client, platform_admin):
        response = post_form(platform_client, form(commercials={"opening_credits": 5000, "credits_per_minute": 12, "low_balance_threshold": 800}))
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["name"] == "Lakeview Hospital" and body["status"] == "active" and body["invite_sent"] is True
        assert (body["plan"], body["balance"], body["credits_per_minute"], body["low_balance_threshold"]) == ("growth", 5000, 12, 800)
        assert body["admin_email"] == "meera@lakeview.example"  # the pending invite, until they accept

        clinic = Clinic.objects.get(pk=body["id"])
        assert clinic.parent_id is None and clinic.is_onboarded and clinic.subscription_status == Clinic.SubscriptionStatus.ACTIVE
        assert ClinicConfiguration.objects.get(clinic=clinic).timezone == "Asia/Kolkata"
        entitlement = ClinicEntitlement.objects.get(clinic=clinic)
        assert entitlement.plan.name == "growth" and entitlement.credits_per_minute_override == 12
        assert (entitlement.max_admins_override, entitlement.max_doctors_override, entitlement.max_receptionists_override) == (2, 20, 5)

        assert CreditAccount.objects.get(clinic=clinic).balance == 5000
        grant = CreditLedger.objects.get(account__clinic=clinic)
        assert (grant.entry_type, grant.amount, grant.note, grant.actor_email) == ("grant", 5000, "Opening credits", platform_admin.email)

        assert PlatformAuditLog.objects.filter(clinic=clinic, action="create_hospital").count() == 1
        assert not User.objects.filter(email="meera@lakeview.example").exists()  # nobody exists until they accept
        assert Invitation.objects.filter(clinic=clinic, email="meera@lakeview.example", role="clinic_admin").count() == 1
        assert len(mail.outbox) == 1 and mail.outbox[0].to == ["meera@lakeview.example"]

    def test_no_opening_credits_means_no_ledger_entry(self, platform_client):
        clinic = Clinic.objects.get(pk=post_form(platform_client, form()).json()["id"])
        assert CreditAccount.objects.get(clinic=clinic).balance == 0
        assert CreditLedger.objects.count() == 0

    def test_the_admin_accepts_the_invite_and_lands_in_their_own_hospital(self, platform_client, clinic):
        Patient.objects.create(clinic=clinic, full_name="Someone Else's Patient", phone="9999999999")
        created = post_form(platform_client, form()).json()
        user = accept_invitation(signed_token=signed_token_from_the_email(), full_name="Meera Rao", password=GOOD_PASSWORD)

        assert user.clinic_id == created["id"] and user.role == "clinic_admin" and user.is_verified
        client = client_for(user)
        assert client.get("/api/v1/accounts/access/").status_code == 200
        listed = client.get("/api/v1/patients/").json()
        assert (listed["results"] if isinstance(listed, dict) else listed) == []  # sees none of the other hospital's patients
        assert client.get("/api/v1/subscriptions/credits/").json()["balance"] == 0

    def test_an_admin_who_is_also_a_doctor_gets_a_doctor_profile_on_accepting(self, platform_client):
        post_form(platform_client, form(admin={"is_also_doctor": True}))
        user = accept_invitation(signed_token=signed_token_from_the_email(), full_name="Meera Rao", password=GOOD_PASSWORD)
        assert user.is_doctor and Doctor.objects.filter(user=user, clinic=user.clinic).exists()

    def test_a_failed_invite_email_does_not_undo_the_hospital(self, platform_client):
        with mock.patch("accounts.services.send_mail", side_effect=RuntimeError("smtp down")):
            response = post_form(platform_client, form())
        assert response.status_code == 201 and response.json()["invite_sent"] is False
        assert Clinic.objects.filter(name="Lakeview Hospital").exists()

    def test_omitted_commercials_come_from_the_platform_defaults(self, platform_client):
        PlatformDefaults.objects.update_or_create(pk=1, defaults={"plan": "enterprise", "credits_per_minute": 14, "opening_credits": 250, "low_balance_threshold": 100})
        body = form()
        del body["commercials"]
        created = platform_client.post(f"{BASE}/hospitals/", body, format="json").json()
        assert (created["plan"], created["credits_per_minute"], created["balance"], created["low_balance_threshold"]) == ("enterprise", 14, 250, 100)


class TestCreateWithPassword:
    def test_the_admin_can_sign_in_straight_away_and_must_change_the_password(self, platform_client):
        body = form(admin={"mode": "password", "password": GOOD_PASSWORD, "must_change_password": True})
        response = post_form(platform_client, body)
        assert response.status_code == 201, response.content
        assert response.json()["admin_email"] == "meera@lakeview.example" and len(mail.outbox) == 0  # no email: it was handed over

        user = User.objects.get(email="meera@lakeview.example")
        assert user.is_verified and user.role == "clinic_admin" and user.is_clinic_admin and user.must_change_password
        assert user.clinic_id == response.json()["id"]

        login = APIClient().post("/api/v1/auth/login/", {"email": "meera@lakeview.example", "password": GOOD_PASSWORD}, format="json")
        assert login.status_code == 200 and login.json()["must_change_password"] is True

    def test_the_change_can_be_waived(self, platform_client):
        post_form(platform_client, form(admin={"mode": "password", "password": GOOD_PASSWORD, "must_change_password": False}))
        assert User.objects.get(email="meera@lakeview.example").must_change_password is False

    def test_also_a_doctor_gets_the_profile_straight_away(self, platform_client):
        post_form(platform_client, form(admin={"mode": "password", "password": GOOD_PASSWORD, "is_also_doctor": True}))
        user = User.objects.get(email="meera@lakeview.example")
        assert user.is_doctor and Doctor.objects.filter(user=user).exists()

    @pytest.mark.parametrize("password,field", [("", "password"), ("short", "password"), ("alllowercaseletters", "password"), ("1234567890123", "password")])
    def test_a_weak_or_missing_password_is_refused_and_nothing_is_created(self, platform_client, password, field):
        response = post_form(platform_client, form(admin={"mode": "password", "password": password}))
        assert response.status_code == 400 and field in response.json()
        assert not Clinic.objects.filter(name="Lakeview Hospital").exists() and not User.objects.filter(email="meera@lakeview.example").exists()


class TestCreateValidation:
    def test_a_duplicate_hospital_name_is_refused_ignoring_case(self, platform_client, clinic):
        before = Clinic.objects.count()
        response = post_form(platform_client, form(name="TEST CLINIC"))
        assert response.status_code == 400 and "name" in response.json()
        assert Clinic.objects.count() == before and CreditAccount.objects.count() == 0

    def test_an_email_that_already_has_an_account_is_refused(self, platform_client, clinic_admin):
        response = post_form(platform_client, form(admin={"email": clinic_admin.email.upper()}))
        assert response.status_code == 400 and "admin" in response.json()
        assert not Clinic.objects.filter(name="Lakeview Hospital").exists()

    @pytest.mark.parametrize("change", [
        {"name": ""}, {"admin": {"email": "not-an-email"}}, {"admin": {"full_name": ""}},
        {"commercials": {"credits_per_minute": 0}}, {"commercials": {"opening_credits": -1}}, {"commercials": {"plan": "gold"}},
        {"support_email": "nope"},
    ])
    def test_bad_form_values_are_refused(self, platform_client, change):
        assert post_form(platform_client, form(**change)).status_code == 400
        assert not Clinic.objects.filter(name="Lakeview Hospital").exists()

    def test_a_failure_part_way_leaves_nothing_behind(self, platform_client):
        before = (Clinic.objects.count(), User.objects.count(), CreditAccount.objects.count(), PlatformAuditLog.objects.count())
        with mock.patch("platform_admin.provision.credits.add_credits", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                post_form(platform_client, form(commercials={"opening_credits": 100}, admin={"mode": "password", "password": GOOD_PASSWORD}),
                          logo=upload(image_bytes()))
        assert (Clinic.objects.count(), User.objects.count(), CreditAccount.objects.count(), PlatformAuditLog.objects.count()) == before
        from django.core.files.storage import default_storage
        assert not default_storage.exists("hospital_logos") or default_storage.listdir("hospital_logos")[1] == []  # no orphaned logo file

    def test_only_the_platform_login_may_create_hospitals(self, api_client, clinic_admin):
        assert post_form(api_client, form()).status_code == 404
        assert post_form(client_for(clinic_admin), form()).status_code == 404
        assert not Clinic.objects.filter(name="Lakeview Hospital").exists()

    def test_the_form_can_also_be_posted_as_plain_json(self, platform_client):
        assert platform_client.post(f"{BASE}/hospitals/", form(), format="json").status_code == 201

    def test_unreadable_payload_text_is_a_clean_400(self, platform_client):
        assert platform_client.post(f"{BASE}/hospitals/", {"payload": "{not json"}, format="multipart").status_code == 400


# ── the logo ──────────────────────────────────────────────────────────────────

class TestLogo:
    def test_a_png_is_stored_resized_and_served_publicly(self, platform_client, api_client):
        response = post_form(platform_client, form(), logo=upload(image_bytes("PNG", (1600, 1200))))
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["logo_url"].startswith(f"/api/v1/clinics/{body['id']}/logo/")

        served = api_client.get(body["logo_url"])  # signed out: an <img> tag sends no credentials
        assert served.status_code == 200 and served["Content-Type"] == "image/png"
        assert served["X-Content-Type-Options"] == "nosniff" and "sandbox" in served["Content-Security-Policy"]
        picture = Image.open(io.BytesIO(b"".join(served.streaming_content)))
        assert picture.format == "PNG" and max(picture.size) <= 512  # re-encoded and small

    def test_a_jpeg_is_accepted_and_stored_as_a_png(self, platform_client, api_client):
        body = post_form(platform_client, form(), logo=upload(image_bytes("JPEG"), "logo.jpg", "image/jpeg")).json()
        assert api_client.get(body["logo_url"])["Content-Type"] == "image/png"

    def test_a_plain_svg_is_accepted_and_served_locked_down(self, platform_client, api_client):
        body = post_form(platform_client, form(), logo=upload(SAFE_SVG, "logo.svg", "image/svg+xml")).json()
        served = api_client.get(body["logo_url"])
        assert served["Content-Type"] == "image/svg+xml" and "default-src 'none'" in served["Content-Security-Policy"]

    @pytest.mark.parametrize("data", [
        pytest.param(b"hello, not a picture", id="a text file called .png"),
        pytest.param(b"", id="an empty file"),
        pytest.param(b"\x89PNG\r\n\x1a\n" + b"garbage" * 20, id="a broken png"),
        pytest.param(b"\x89PNG\r\n\x1a\n" + b"0" * (1024 * 1024 + 10), id="a file over 1 MB"),
        pytest.param(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', id="an svg with a script"),
        pytest.param(b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><rect/></svg>', id="an svg with an event handler"),
        pytest.param(b'<svg xmlns="http://www.w3.org/2000/svg"><a href="https://evil.example"><rect/></a></svg>', id="an svg with a link"),
        pytest.param(b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://evil.example/x.png"/></svg>', id="an svg loading something"),
        pytest.param(b'<svg xmlns="http://www.w3.org/2000/svg"><style>rect{fill:url(https://evil.example)}</style><rect/></svg>', id="an svg with a style"),
        pytest.param(b"<html><script>alert(1)</script></html>", id="html pretending to be a logo"),
    ])
    def test_unsafe_or_unreadable_logos_are_refused_and_the_hospital_is_not_created(self, platform_client, data):
        response = post_form(platform_client, form(), logo=upload(data, "logo.png", "image/png"))
        assert response.status_code == 400 and "logo" in response.json()
        assert not Clinic.objects.filter(name="Lakeview Hospital").exists()

    def test_changing_the_logo_later_replaces_and_removes_the_old_file(self, platform_client, api_client):
        created = post_form(platform_client, form(), logo=upload(image_bytes())).json()
        clinic = Clinic.objects.get(pk=created["id"])
        old_name = clinic.logo_file.name

        response = platform_client.patch(f"{BASE}/hospitals/{created['id']}/", {"logo": upload(image_bytes(size=(300, 300)))}, format="multipart")
        assert response.status_code == 200, response.content
        clinic.refresh_from_db()
        assert clinic.logo_file.name != old_name and response.json()["logo_url"] != created["logo_url"]
        assert not clinic.logo_file.storage.exists(old_name)
        assert api_client.get(response.json()["logo_url"]).status_code == 200
        assert PlatformAuditLog.objects.filter(clinic=clinic, action="update_logo").exists()

    def test_a_bad_replacement_changes_nothing(self, platform_client):
        created = post_form(platform_client, form(), logo=upload(image_bytes())).json()
        before = Clinic.objects.get(pk=created["id"]).logo_file.name
        response = platform_client.patch(f"{BASE}/hospitals/{created['id']}/", {"logo": upload(b"nope")}, format="multipart")
        assert response.status_code == 400 and "logo" in response.json()
        assert Clinic.objects.get(pk=created["id"]).logo_file.name == before

    def test_no_logo_and_centres_give_a_404_and_suspended_hospitals_still_show_theirs(self, platform_client, api_client):
        without = post_form(platform_client, form()).json()
        assert api_client.get(f"/api/v1/clinics/{without['id']}/logo/").status_code == 404
        centre = Clinic.objects.create(name="Centre", parent=Clinic.objects.get(pk=without["id"]))
        assert api_client.get(f"/api/v1/clinics/{centre.id}/logo/").status_code == 404

        with_logo = post_form(platform_client, form(name="Second Hospital", admin={"email": "two@h.example"}), logo=upload(image_bytes())).json()
        platform_client.patch(f"{BASE}/hospitals/{with_logo['id']}/", {"status": "suspended"}, format="json")
        assert api_client.get(with_logo["logo_url"]).status_code == 200


# ── change password ───────────────────────────────────────────────────────────

class TestChangePassword:
    URL = "/api/v1/auth/change-password/"

    @pytest.fixture
    def person(self, clinic):
        return make_user("person@h.example", clinic=clinic, must_change_password=True)

    def test_choosing_a_new_password_clears_the_flag_and_replaces_the_old_one(self, person):
        response = client_for(person).post(self.URL, {"current_password": "SomePass12345!", "new_password": GOOD_PASSWORD}, format="json")
        assert response.status_code == 200
        person.refresh_from_db()
        assert person.must_change_password is False and person.check_password(GOOD_PASSWORD)
        assert APIClient().post("/api/v1/auth/login/", {"email": person.email, "password": "SomePass12345!"}, format="json").status_code == 401
        assert APIClient().post("/api/v1/auth/login/", {"email": person.email, "password": GOOD_PASSWORD}, format="json").status_code == 200

    @pytest.mark.parametrize("current,new,field", [
        ("wrong-password", GOOD_PASSWORD, "current_password"),
        ("SomePass12345!", "SomePass12345!", "new_password"),
        ("SomePass12345!", "short", "new_password"),
        ("SomePass12345!", "", "new_password"),
    ])
    def test_wrong_input_is_refused_and_changes_nothing(self, person, current, new, field):
        response = client_for(person).post(self.URL, {"current_password": current, "new_password": new}, format="json")
        assert response.status_code == 400 and field in response.json()
        person.refresh_from_db()
        assert person.check_password("SomePass12345!") and person.must_change_password is True

    def test_it_needs_a_signed_in_user(self, api_client):
        assert api_client.post(self.URL, {"current_password": "x", "new_password": GOOD_PASSWORD}, format="json").status_code in (401, 403)

    def test_me_reports_the_flag(self, person):
        assert client_for(person).get("/api/v1/auth/me/").json()["must_change_password"] is True
