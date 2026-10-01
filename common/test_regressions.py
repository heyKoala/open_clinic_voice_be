"""Regression tests for endpoint bugs fixed during the 2026-09 debugging pass."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from ai_agent.tool_views import rock8_clinic_token
from appointments.models import Appointment
from clinics.models import ClinicHoliday
from doctors.models import Doctor
from patients.models import Patient
from queue_mgmt.models import QueueToken

pytestmark = pytest.mark.django_db


@pytest.fixture
def doctor(clinic):
    return Doctor.objects.create(clinic=clinic, full_name="Dr Home", working_days=[1, 2, 3, 4, 5, 6, 7])


@pytest.fixture
def other_doctor(clinic_b):
    return Doctor.objects.create(clinic=clinic_b, full_name="Dr Away")


@pytest.fixture
def patient(clinic):
    return Patient.objects.create(clinic=clinic, full_name="Home Patient", phone="9876543210")


class TestClinicContext:
    def test_force_authenticated_request_resolves_clinic(self, receptionist_client, patient):
        res = receptionist_client.get(f"/api/v1/patients/{patient.id}/")
        assert res.status_code == status.HTTP_200_OK

    def test_user_primary_clinic_is_a_membership(self, clinic_admin, clinic):
        assert clinic_admin.clinics.filter(id=clinic.id).exists()

    def test_profile_update_does_not_move_primary_clinic(self, clinic_admin, clinic, clinic_b):
        clinic_admin.clinics.add(clinic_b)
        client = APIClient()
        client.force_authenticate(user=clinic_admin)
        res = client.patch("/api/v1/auth/me/", {"full_name": "Renamed"}, format="json", HTTP_X_ACTIVE_CLINIC_ID=str(clinic_b.id))
        assert res.status_code == status.HTTP_200_OK
        clinic_admin.refresh_from_db()
        assert clinic_admin.clinic_id == clinic.id


class TestQueue:
    def test_call_next_does_not_crash(self, receptionist_client, doctor, patient):
        QueueToken.objects.create(
            clinic=doctor.clinic, doctor=doctor, patient=patient,
            service_date=timezone.localdate(), token_number=1,
        )
        res = receptionist_client.post("/api/v1/queue/call_next/", {"doctor": doctor.id}, format="json")
        assert res.status_code == status.HTTP_200_OK
        assert res.data["status"] == QueueToken.Status.CALLED

    def test_create_rejects_other_clinic_patient(self, receptionist_client, doctor, clinic_b):
        foreign = Patient.objects.create(clinic=clinic_b, full_name="Foreign", phone="5555555555")
        res = receptionist_client.post(
            "/api/v1/queue/",
            {"doctor": doctor.id, "patient": foreign.id, "service_date": str(timezone.localdate())},
            format="json",
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST

    def test_appointment_check_in_numbers_tokens_sequentially(self, receptionist_client, doctor, patient):
        start = timezone.now() + timedelta(hours=1)
        appts = [
            Appointment.objects.create(
                clinic=doctor.clinic, doctor=doctor, patient=patient,
                starts_at=start + timedelta(minutes=30 * i), ends_at=start + timedelta(minutes=30 * i + 15),
            )
            for i in range(2)
        ]
        for appt in appts:
            res = receptionist_client.post(f"/api/v1/appointments/{appt.id}/check_in/")
            assert res.status_code == status.HTTP_200_OK
        numbers = sorted(QueueToken.objects.filter(doctor=doctor).values_list("token_number", flat=True))
        assert numbers == [1, 2]
        res = receptionist_client.post(f"/api/v1/appointments/{appts[0].id}/check_in/")
        assert res.status_code == status.HTTP_400_BAD_REQUEST


class TestCrossClinicIsolation:
    def test_doctor_dashboard_ignores_other_clinic_doctor(self, receptionist_client, other_doctor):
        res = receptionist_client.get(f"/api/v1/doctors/dashboard/?doctor={other_doctor.id}")
        assert res.status_code == status.HTTP_200_OK
        assert res.data["appointments"] == []

    def test_walkin_cannot_shift_other_clinic_appointments(self, receptionist_client, doctor, patient, other_doctor, clinic_b):
        foreign_patient = Patient.objects.create(clinic=clinic_b, full_name="Foreign", phone="5555555555")
        start = timezone.now() + timedelta(hours=2)
        foreign_appt = Appointment.objects.create(
            clinic=clinic_b, doctor=other_doctor, patient=foreign_patient,
            starts_at=start, ends_at=start + timedelta(minutes=15),
        )
        res = receptionist_client.post("/api/v1/appointments/walkin/confirm/", {
            "doctor": doctor.id,
            "patient": patient.id,
            "starts_at": start.isoformat(),
            "duration_minutes": 15,
            "confirmed_shifts": [{
                "appointment_id": foreign_appt.id,
                "new_starts_at": (start + timedelta(days=1)).isoformat(),
                "new_ends_at": (start + timedelta(days=1, minutes=15)).isoformat(),
            }],
        }, format="json")
        assert res.status_code == status.HTTP_200_OK
        assert res.data["shifted_count"] == 0
        foreign_appt.refresh_from_db()
        assert foreign_appt.starts_at == start

    def test_walkin_rejects_bad_duration(self, receptionist_client, doctor, patient):
        res = receptionist_client.post("/api/v1/appointments/walkin/suggest/", {
            "doctor": doctor.id, "starts_at": timezone.now().isoformat(), "duration_minutes": "abc",
        }, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST


class TestClinics:
    def test_list_clinics(self, admin_client, clinic):
        res = admin_client.get("/api/v1/clinics/")
        assert res.status_code == status.HTTP_200_OK
        assert [c["id"] for c in res.data] == [clinic.id]

    def test_create_branch(self, admin_client, clinic_admin):
        res = admin_client.post("/api/v1/clinics/", {"name": "Second Branch"}, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        assert clinic_admin.clinics.filter(id=res.data["id"]).exists()

    def test_create_holiday_without_clinic_field(self, admin_client, clinic):
        day = timezone.localdate() + timedelta(days=10)
        res = admin_client.post("/api/v1/clinics/holidays/", {"date": str(day), "reason": "Festival"}, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        assert ClinicHoliday.objects.filter(clinic=clinic, date=day).exists()
        res = admin_client.post("/api/v1/clinics/holidays/", {"date": str(day), "reason": "Again"}, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST

    def test_delete_holiday_with_invalid_date_is_404(self, admin_client):
        res = admin_client.delete("/api/v1/clinics/holidays/not-a-date/")
        assert res.status_code == status.HTTP_404_NOT_FOUND


class TestAuth:
    def test_invalid_refresh_token_returns_401(self, api_client):
        api_client.cookies["mvx_refresh"] = "garbage"
        res = api_client.post("/api/v1/auth/refresh/")
        assert res.status_code == status.HTTP_401_UNAUTHORIZED


class TestRock8:
    def test_clinic_token_grants_only_its_clinic(self, api_client, settings, clinic, clinic_b):
        settings.ROCK8_WEBHOOK_SECRET = "test-secret"
        token = rock8_clinic_token(clinic.id)
        assert api_client.get(f"/api/v1/ai/tools/rock8/{clinic.id}/doctors/?token={token}").status_code == 200
        assert api_client.get(f"/api/v1/ai/tools/rock8/{clinic_b.id}/doctors/?token={token}").status_code == 403

    def test_webhook_tool_urls_carry_clinic_token(self, api_client, settings, clinic):
        settings.ROCK8_WEBHOOK_SECRET = "test-secret"
        res = api_client.post(f"/api/v1/ai/webhooks/rock8/{clinic.id}/?token={rock8_clinic_token(clinic.id)}")
        assert res.status_code == status.HTTP_200_OK
        assert all(f"/{clinic.id}/{rock8_clinic_token(clinic.id)}/" in tool["url"] for tool in res.data["tools"])

    def test_lookup_requires_specific_phone(self, api_client, settings, clinic):
        settings.ROCK8_WEBHOOK_SECRET = "test-secret"
        res = api_client.get(
            f"/api/v1/ai/tools/rock8/{clinic.id}/appointments/?patient_phone=9",
            HTTP_AUTHORIZATION="Bearer test-secret",
        )
        assert res.data["success"] is False

    def test_booking_on_holiday_is_rejected(self, api_client, settings, clinic, doctor):
        settings.ROCK8_WEBHOOK_SECRET = "test-secret"
        day = timezone.localdate() + timedelta(days=3)
        ClinicHoliday.objects.create(clinic=clinic, date=day)
        res = api_client.post(f"/api/v1/ai/tools/rock8/{clinic.id}/book/", {
            "doctor_id": doctor.id, "date": str(day), "time": "10:00",
            "patient_phone": "9123456780", "patient_name": "Neha Joshi",
        }, format="json", HTTP_AUTHORIZATION="Bearer test-secret")
        assert res.data.get("error") == "CLINIC_CLOSED", res.data
        assert not Patient.objects.filter(phone="9123456780").exists()


class TestFollowUpsAndDoctors:
    def test_admin_can_list_followups(self, admin_client):
        assert admin_client.get("/api/v1/followups/followups/").status_code == status.HTTP_200_OK

    def test_receptionist_cannot_delete_doctor(self, receptionist_client, doctor):
        assert receptionist_client.delete(f"/api/v1/doctors/{doctor.id}/").status_code == status.HTTP_403_FORBIDDEN


class TestSubscriptions:
    def test_enterprise_upgrade_returns_400_not_500(self, admin_client):
        res = admin_client.post("/api/v1/subscriptions/upgrade/", {"plan": "enterprise"}, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST



class TestLiveUpdates:
    """Marking a patient seen must not look like a reschedule, and must reach the live queue."""

    def _appointment(self, clinic, doctor, patient):
        start = timezone.now().replace(microsecond=0) + timedelta(hours=1)
        return Appointment.objects.create(clinic=clinic, doctor=doctor, patient=patient, starts_at=start,
                                          ends_at=start + timedelta(minutes=15), status="checked_in")

    def test_status_change_is_broadcast_once_and_not_as_a_reschedule(self, clinic, doctor, patient):
        from unittest import mock
        appt = self._appointment(clinic, doctor, patient)
        appt = Appointment.objects.get(pk=appt.pk)
        with mock.patch("appointments.signals.broadcast_event") as sent:
            appt.status = "completed"
            appt.save(update_fields=["status", "updated_at"])
        assert sent.call_count == 1
        group, event, data = sent.call_args.args
        assert (group, event) == (f"clinic_{clinic.id}", "appointment.updated")
        assert data["rescheduled"] is False and data["status_changed"] is True

    def test_moving_an_appointment_is_flagged_as_rescheduled(self, clinic, doctor, patient):
        from unittest import mock
        appt = Appointment.objects.get(pk=self._appointment(clinic, doctor, patient).pk)
        with mock.patch("appointments.signals.broadcast_event") as sent:
            appt.starts_at += timedelta(minutes=30)
            appt.save()
        assert sent.call_args.args[2]["rescheduled"] is True

    def test_completing_an_appointment_pushes_the_queue_update(self, admin_client, clinic, doctor, patient):
        from unittest import mock
        appt = self._appointment(clinic, doctor, patient)
        token = QueueToken.objects.create(clinic=clinic, doctor=doctor, patient=patient, appointment=appt,
                                          token_number=1, service_date=timezone.localdate(),
                                          status=QueueToken.Status.IN_CONSULTATION)
        with mock.patch("queue_mgmt.views.broadcast_queue_update") as pushed:
            res = admin_client.post(f"/api/v1/appointments/{appt.id}/complete/", {"notes": ""}, format="json")
        assert res.status_code == status.HTTP_200_OK
        pushed.assert_called_once()
        doctor_id, event, payload = pushed.call_args.args
        assert (doctor_id, event, payload["id"]) == (doctor.id, "queue.completed", token.id)


@pytest.mark.django_db(transaction=True)  # the middleware reads the user from another thread
class TestWebSocketAuth:
    """A browser may send an expired access cookie alongside a fresh WebSocket ticket."""

    def _who(self, cookie, query):
        from asgiref.sync import async_to_sync
        from accounts.middleware import JWTAuthMiddleware

        seen = {}

        async def inner(scope, receive, send):
            seen["user"] = scope["user"]

        scope = {"type": "websocket", "headers": [(b"cookie", cookie.encode())] if cookie else [],
                 "query_string": query.encode()}
        async_to_sync(JWTAuthMiddleware(inner))(scope, None, None)
        return seen["user"]

    def test_valid_ticket_wins_over_expired_cookie(self, clinic_admin):
        from rest_framework_simplejwt.tokens import AccessToken

        ticket = str(AccessToken.for_user(clinic_admin))
        expired = AccessToken.for_user(clinic_admin)
        expired.set_exp(lifetime=-timedelta(minutes=5))
        assert self._who(f"mvx_access={expired}", f"token={ticket}").id == clinic_admin.id
        assert self._who(f"mvx_access={ticket}", "").id == clinic_admin.id  # cookie alone still works
        assert not self._who(f"mvx_access={expired}", "token=garbage").is_authenticated
