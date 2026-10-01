"""Clinic phone numbers (Plivo) and routing voice calls by the number dialled.

Plivo is mocked: nothing here buys or releases a real number.
"""
from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from ai_agent.models import CallLog
from ai_agent.tool_views import rock8_clinic_token
from appointments.models import Appointment
from clinics.models import Clinic, ClinicPhoneNumber
from doctors.models import Doctor
from patients.models import Patient

pytestmark = pytest.mark.django_db

SECRET = "global-secret"
NUMBER_A = "918031805277"
NUMBER_B = "918031805261"


@pytest.fixture(autouse=True)
def telephony_settings(settings):
    settings.PLIVO_AUTH_ID = "MAUTHID"
    settings.PLIVO_AUTH_TOKEN = "token"
    settings.PLIVO_APP_ID = "23626388960837953"
    settings.ROCK8_WEBHOOK_SECRET = SECRET


class FakePlivo:
    """Records calls to Plivo and answers them like the real API."""

    def __init__(self, buy_status="Success", fail_assign=False):
        self.calls = []
        self.buy_status = buy_status
        self.fail_assign = fail_assign

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url.split("/Account/MAUTHID/")[1], kwargs.get("json"), kwargs.get("params")))
        response = mock.MagicMock()
        response.status_code, response.content = 200, b"{}"
        if method == "GET":
            response.json.return_value = {"meta": {"total_count": 2}, "objects": [
                {"number": "918031790436", "city": "Bangalore", "region": "Bangalore", "sub_type": "local",
                 "monthly_rental_rate": "2.50000", "setup_rate": "0.00000", "voice_enabled": True, "sms_enabled": False,
                 "compliance_requirement": {"business": "/v1/.../ComplianceRequirement/x/", "individual": None}},
                {"number": "918031790999", "voice_enabled": False},  # can't take calls: filtered out
            ]}
        elif method == "POST" and url.rstrip("/").split("/")[-2] == "PhoneNumber":
            response.status_code = 201
            response.json.return_value = {"message": "created", "numbers": [{"number": "x", "status": self.buy_status}],
                                          "status": "fulfilled"}
        elif method == "POST":  # assign app
            if self.fail_assign:
                response.status_code = 400
                response.json.return_value = {"error": "app not found"}
            else:
                response.status_code = 202
                response.json.return_value = {"message": "changed"}
        elif method == "DELETE":
            response.status_code, response.content = 204, b""
        return response


def _admin(clinic, email, *extra_clinics):
    user = User.objects.create_user(
        clinic=clinic, email=email, full_name="Admin", password="x-Pass-123!", role=User.Role.CLINIC_ADMIN,
        membership_status=User.MembershipStatus.ACTIVE, is_verified=True,
    )
    user.clinics.add(*extra_clinics)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def org_a(clinic):
    """Main clinic A with a second centre A2."""
    centre = Clinic.objects.create(name="A Downtown", parent=clinic, address="12 MG Road")
    client = _admin(clinic, "admin@a.example.com", centre)
    return {"root": clinic, "centre": centre, "client": client}


@pytest.fixture
def org_b(clinic_b):
    return {"root": clinic_b, "client": _admin(clinic_b, "admin@b.example.com")}


# --- Buying and managing the number -----------------------------------------------------------

def test_search_returns_voice_numbers_with_prices(org_a):
    fake = FakePlivo()
    with mock.patch("clinics.plivo.requests.request", side_effect=fake):
        res = org_a["client"].get("/api/v1/clinics/phone-number/search/", {"city": "Bangalore"})
    assert res.status_code == 200
    assert res.data["numbers"] == [{
        "number": "918031790436", "city": "Bangalore", "region": "Bangalore", "type": "local",
        "monthly_rental_rate": "2.50000", "setup_rate": "0.00000", "voice_enabled": True, "sms_enabled": False,
        "requires_compliance": True,
    }]
    assert fake.calls[0][:2] == ("GET", "PhoneNumber/")
    assert fake.calls[0][3]["city"] == "Bangalore" and fake.calls[0][3]["country_iso"] == "IN"


def test_buying_routes_the_number_to_the_agent_and_shares_it_with_all_centres(org_a):
    fake = FakePlivo()
    with mock.patch("clinics.plivo.requests.request", side_effect=fake):
        res = org_a["client"].post("/api/v1/clinics/phone-number/", {"number": "+91 80 3180 5277", "city": "Bangalore"}, format="json")
    assert res.status_code == 201, res.data
    assert [c[:3] for c in fake.calls] == [
        ("POST", f"PhoneNumber/{NUMBER_A}/", {}),
        ("POST", f"Number/{NUMBER_A}/", {"app_id": "23626388960837953"}),
    ]
    assert res.data["phone_number"]["status"] == "active"
    assert set(res.data["shared_with"]) == {org_a["root"].name, "A Downtown"}
    record = ClinicPhoneNumber.objects.get()
    assert (record.clinic_id, record.number, record.app_id) == (org_a["root"].id, NUMBER_A, "23626388960837953")

    # Seen the same from the centre: it's the main clinic's number.
    org_a["client"].credentials(HTTP_X_ACTIVE_CLINIC_ID=str(org_a["centre"].id))
    res = org_a["client"].get("/api/v1/clinics/phone-number/")
    assert res.data["phone_number"]["number"] == NUMBER_A and res.data["owner_clinic"]["id"] == org_a["root"].id


def test_one_number_per_clinic_group_and_numbers_are_not_shared_between_groups(org_a, org_b):
    ClinicPhoneNumber.objects.create(clinic=org_a["root"], number=NUMBER_A, status="active")
    fake = FakePlivo()
    with mock.patch("clinics.plivo.requests.request", side_effect=fake):
        org_a["client"].credentials(HTTP_X_ACTIVE_CLINIC_ID=str(org_a["centre"].id))
        again = org_a["client"].post("/api/v1/clinics/phone-number/", {"number": NUMBER_B}, format="json")
        taken = org_b["client"].post("/api/v1/clinics/phone-number/", {"number": NUMBER_A}, format="json")
    assert again.status_code == 409 and taken.status_code == 409
    assert fake.calls == []  # nothing was bought


def test_pending_purchase_is_not_routed_until_activated(org_a):
    with mock.patch("clinics.plivo.requests.request", side_effect=FakePlivo(buy_status="pending")) :
        res = org_a["client"].post("/api/v1/clinics/phone-number/", {"number": NUMBER_A}, format="json")
    assert res.data["phone_number"]["status"] == "pending"
    fake = FakePlivo()
    with mock.patch("clinics.plivo.requests.request", side_effect=fake):
        res = org_a["client"].post("/api/v1/clinics/phone-number/activate/")
    assert res.status_code == 200 and res.data["phone_number"]["status"] == "active"
    assert fake.calls[0][:2] == ("POST", f"Number/{NUMBER_A}/")


def test_failed_routing_keeps_the_number_and_can_be_retried(org_a):
    with mock.patch("clinics.plivo.requests.request", side_effect=FakePlivo(fail_assign=True)):
        res = org_a["client"].post("/api/v1/clinics/phone-number/", {"number": NUMBER_A}, format="json")
    assert res.status_code == 201
    assert res.data["phone_number"]["status"] == "purchased"
    assert "app not found" in res.data["phone_number"]["last_error"]
    with mock.patch("clinics.plivo.requests.request", side_effect=FakePlivo()):
        res = org_a["client"].post("/api/v1/clinics/phone-number/activate/")
    assert res.data["phone_number"]["status"] == "active" and res.data["phone_number"]["last_error"] == ""


def test_number_is_released_if_it_cannot_be_saved(org_a):
    fake = FakePlivo()
    with mock.patch("clinics.plivo.requests.request", side_effect=fake), \
         mock.patch("clinics.views.ClinicPhoneNumber.objects.create", side_effect=RuntimeError("db down")), \
         pytest.raises(RuntimeError):
        org_a["client"].post("/api/v1/clinics/phone-number/", {"number": NUMBER_A}, format="json")
    assert [c[:2] for c in fake.calls] == [("POST", f"PhoneNumber/{NUMBER_A}/"), ("DELETE", f"Number/{NUMBER_A}/")]


def test_release(org_a):
    ClinicPhoneNumber.objects.create(clinic=org_a["root"], number=NUMBER_A, status="active")
    fake = FakePlivo()
    with mock.patch("clinics.plivo.requests.request", side_effect=fake):
        res = org_a["client"].delete("/api/v1/clinics/phone-number/")
    assert res.status_code == 204
    assert fake.calls[0][:2] == ("DELETE", f"Number/{NUMBER_A}/")
    assert not ClinicPhoneNumber.objects.exists()


def test_plivo_errors_are_reported_not_crashed(org_a):
    response = mock.MagicMock(status_code=401, content=b'{"error": "authentication failed"}')
    response.json.return_value = {"error": "authentication failed"}
    with mock.patch("clinics.plivo.requests.request", return_value=response):
        res = org_a["client"].post("/api/v1/clinics/phone-number/", {"number": NUMBER_A}, format="json")
    assert res.status_code == 502 and "authentication failed" in res.data["detail"]
    assert not ClinicPhoneNumber.objects.exists()


def test_only_admins_manage_numbers(org_a):
    receptionist = User.objects.create_user(
        clinic=org_a["root"], email="desk@a.example.com", full_name="Desk", password="x-Pass-123!",
        role=User.Role.RECEPTIONIST, membership_status=User.MembershipStatus.ACTIVE, is_verified=True,
    )
    client = APIClient()
    client.force_authenticate(user=receptionist)
    with mock.patch("clinics.plivo.requests.request", side_effect=FakePlivo()) as plivo_call:
        assert client.get("/api/v1/clinics/phone-number/search/").status_code == 403
        assert client.post("/api/v1/clinics/phone-number/", {"number": NUMBER_A}, format="json").status_code == 403
        assert client.delete("/api/v1/clinics/phone-number/").status_code == 403
    assert plivo_call.call_count == 0


def test_new_centre_belongs_to_the_main_clinic(org_a):
    org_a["client"].credentials(HTTP_X_ACTIVE_CLINIC_ID=str(org_a["centre"].id))
    res = org_a["client"].post("/api/v1/clinics/", {"name": "A Whitefield"}, format="json")
    assert res.status_code == 201, res.data
    assert Clinic.objects.get(name="A Whitefield").parent_id == org_a["root"].id  # not nested under the centre


# --- Calls are routed by the number dialled ---------------------------------------------------

@pytest.fixture
def two_orgs(org_a, org_b):
    ClinicPhoneNumber.objects.create(clinic=org_a["root"], number=NUMBER_A, status="active")
    ClinicPhoneNumber.objects.create(clinic=org_b["root"], number=NUMBER_B, status="active")
    doctors = {
        "a": Doctor.objects.create(clinic=org_a["root"], full_name="Dr Asha", specialty="ENT", working_days=list(range(1, 8)),
                                   available_from="00:00", available_to="23:59"),
        "a2": Doctor.objects.create(clinic=org_a["centre"], full_name="Dr Arun", specialty="Cardiology", working_days=list(range(1, 8)),
                                    available_from="00:00", available_to="23:59"),
        "b": Doctor.objects.create(clinic=org_b["root"], full_name="Dr Bela", specialty="ENT", working_days=list(range(1, 8)),
                                   available_from="00:00", available_to="23:59"),
    }
    return {"a": org_a, "b": org_b, "doctors": doctors}


def _webhook(payload, token=SECRET):
    client = APIClient()
    return client.post("/api/v1/ai/webhooks/rock8/", data=json.dumps(payload), content_type="application/json",
                       HTTP_AUTHORIZATION=f"Bearer {token}")


@pytest.mark.parametrize("payload", [
    {"to_number": "+918031805277", "from_number": "+919876543210"},
    {"call": {"direction": "inbound", "to_number": NUMBER_A, "from_number": "919876543210"}},
    {"participant_attributes": {"sip.trunkPhoneNumber": "+918031805277", "sip.phoneNumber": "+919876543210"}},
    {"data": {"to": "sip:+918031805277@sip.heykoala.ai"}},
])
def test_each_number_gets_only_its_own_clinic(two_orgs, payload):
    res = _webhook(payload)
    assert res.status_code == 200, res.data
    prompt = res.data["system_prompt"]
    a, b = two_orgs["a"], two_orgs["b"]
    assert a["root"].name in prompt and "A Downtown" in prompt and "12 MG Road" in prompt
    assert b["root"].name not in prompt
    urls = [tool["url"] for tool in res.data["tools"]]
    assert all(f"/tools/rock8/{a['root'].id}/" in url and rock8_clinic_token(a["root"].id) in url for url in urls)

    res = _webhook({"to_number": NUMBER_B})
    assert b["root"].name in res.data["system_prompt"] and a["root"].name not in res.data["system_prompt"]


def test_unknown_or_missing_number_is_refused(two_orgs):
    assert _webhook({"to_number": "+918000000000"}).status_code == 404
    assert _webhook({"participant_identity": "caller"}).status_code == 404


def test_shared_webhook_needs_the_global_secret(two_orgs):
    assert _webhook({"to_number": NUMBER_A}, token="").status_code == 403
    # A clinic's own token only works on that clinic's URL, never on the shared one.
    assert _webhook({"to_number": NUMBER_A}, token=rock8_clinic_token(two_orgs["a"]["root"].id)).status_code == 403


def _tool(method, clinic, name, **data):
    client = APIClient()
    url = f"/api/v1/ai/tools/rock8/{clinic.id}/{name}/"
    token = rock8_clinic_token(clinic.id)
    if method == "get":
        res = client.get(url, {**data, "token": token})
    else:
        res = client.post(f"{url}?token={token}", data, format="json")
    assert res.status_code == 200, res.data
    return res


def test_tools_cover_every_centre_of_the_group_and_nothing_else(two_orgs):
    a, doctors = two_orgs["a"], two_orgs["doctors"]
    listed = _tool("get", a["root"], "doctors").data["doctors"]
    assert {(d["name"], d["centre"]) for d in listed} == {("Dr Asha", a["root"].name), ("Dr Arun", "A Downtown")}

    day = (timezone.localdate() + timedelta(days=2)).isoformat()
    booked = _tool("post", a["root"], "book", doctor_id=doctors["a2"].id, date=day, time="10:00",
                   patient_phone="9876500001", patient_name="Ravi")
    assert booked.data["success"] is True and booked.data["centre"] == "A Downtown"
    appt = Appointment.objects.get(id=booked.data["appointment_id"])
    assert appt.clinic_id == a["centre"].id and appt.patient.clinic_id == a["centre"].id

    other = _tool("post", a["root"], "book", doctor_id=doctors["b"].id, date=day, time="11:00",
                  patient_phone="9876500002", patient_name="Mallory")
    assert other.data["error"] == "DOCTOR_NOT_FOUND"
    assert _tool("get", a["root"], "slots", doctor_id=doctors["b"].id, date=day).data["error"] == "DOCTOR_NOT_FOUND"

    b_patient = Patient.objects.create(clinic=two_orgs["b"]["root"], full_name="Bob", phone="9876500003")
    start = timezone.now() + timedelta(days=1)
    b_appt = Appointment.objects.create(clinic=two_orgs["b"]["root"], doctor=doctors["b"], patient=b_patient,
                                        starts_at=start, ends_at=start + timedelta(minutes=15))
    assert _tool("post", a["root"], "cancel", appointment_id=b_appt.id).data["error"] == "NOT_FOUND"
    assert _tool("get", a["root"], "appointments", patient_phone="9876500003").data.get("appointments", []) == []
    b_appt.refresh_from_db()
    assert b_appt.status == "scheduled"


def test_call_log_is_filed_under_the_callers_centre(two_orgs, django_capture_on_commit_callbacks):
    a = two_orgs["a"]
    Patient.objects.create(clinic=a["centre"], full_name="Meera", phone="+91 98765 11111")
    event = {"id": "d1", "event": "call.ended", "call": {
        "call_id": "c-1", "direction": "inbound", "status": "completed", "to_number": NUMBER_A,
        "from_number": "919876511111", "duration_seconds": 30, "started_at": timezone.now().isoformat()}}
    with django_capture_on_commit_callbacks(execute=True):
        res = _webhook(event)
        _webhook(event)  # retried delivery
    assert res.status_code == 200
    log = CallLog.objects.get()
    assert log.clinic_id == a["centre"].id and log.patient.full_name == "Meera"

    stranger = dict(event, call=dict(event["call"], call_id="c-2", from_number="919999999999"))
    with django_capture_on_commit_callbacks(execute=True):
        _webhook(stranger)
    assert CallLog.objects.get(external_call_id="c-2").clinic_id == a["root"].id


def test_linked_number_is_only_disconnected_never_released(org_a):
    ClinicPhoneNumber.objects.create(clinic=org_a["root"], number=NUMBER_A, status="active", bought_here=False)
    fake = FakePlivo()
    with mock.patch("clinics.plivo.requests.request", side_effect=fake):
        res = org_a["client"].delete("/api/v1/clinics/phone-number/")
    assert res.status_code == 204
    assert fake.calls == []  # nothing sent to Plivo: the number stays on the account
    assert not ClinicPhoneNumber.objects.exists()


def test_link_command_links_an_existing_number_to_the_main_clinic(org_a):
    from django.core.management import call_command

    info = mock.MagicMock(status_code=200, content=b"{}")
    info.json.return_value = {"number": NUMBER_A, "application": "/v1/Account/MAUTHID/Zentrunk/Trunk/23626388960837953/"}
    with mock.patch("clinics.plivo.requests.request", return_value=info) as plivo_call:
        call_command("link_phone_number", str(org_a["centre"].id), "+91 80 3180 5277")
    assert [c.args[0] for c in plivo_call.call_args_list] == ["GET"]  # read-only
    record = ClinicPhoneNumber.objects.get()
    assert (record.clinic_id, record.number, record.status, record.bought_here) == (org_a["root"].id, NUMBER_A, "active", False)
