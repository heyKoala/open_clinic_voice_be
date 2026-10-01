from __future__ import annotations

import pytest
from rest_framework import status
from django.utils import timezone
from datetime import timedelta

from appointments.models import Appointment
from patients.models import Patient
from doctors.models import Doctor
from accounts.models import User
from audit.models import DataChangeEvent

pytestmark = pytest.mark.django_db


class TestAppointmentIsolation:
    def test_receptionist_cannot_see_other_clinic_appointments(self, receptionist_client, clinic, clinic_b):
        user_b = User.objects.create_user(clinic=clinic_b, email="b@e.com", password="p", role=User.Role.DOCTOR)
        doctor_b = Doctor.objects.create(clinic=clinic_b, user=user_b, full_name="B")
        patient_b = Patient.objects.create(clinic=clinic_b, full_name="PB")
        
        appt_b = Appointment.objects.create(
            clinic=clinic_b,
            patient=patient_b,
            doctor=doctor_b,
            starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(minutes=15),
            reason="Checkup"
        )
        
        res = receptionist_client.get(f"/api/v1/appointments/{appt_b.id}/")
        assert res.status_code == status.HTTP_404_NOT_FOUND

        res_list = receptionist_client.get("/api/v1/appointments/")
        assert res_list.status_code == status.HTTP_200_OK
        assert len(res_list.data["results"]) == 0

    def test_soft_delete_and_audit(self, receptionist_client, clinic):
        user_a = User.objects.create_user(clinic=clinic, email="a@e.com", password="p", role=User.Role.DOCTOR)
        doctor = Doctor.objects.create(clinic=clinic, user=user_a, full_name="A")
        patient = Patient.objects.create(clinic=clinic, full_name="PA")
        
        appt = Appointment.objects.create(
            clinic=clinic,
            patient=patient,
            doctor=doctor,
            starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(minutes=15),
            reason="Checkup"
        )
        
        res = receptionist_client.delete(f"/api/v1/appointments/{appt.id}/")
        assert res.status_code == status.HTTP_204_NO_CONTENT
        
        appt.refresh_from_db()
        assert not appt.is_active
        
        audit = DataChangeEvent.objects.filter(object_id=appt.id).first()
        assert audit is not None


class TestWalkinEngine:
    def test_suggest_and_confirm_walkin_shifts(self, receptionist_client, clinic):
        user_doc = User.objects.create_user(clinic=clinic, email="doc@e.com", password="p", role=User.Role.DOCTOR)
        doctor = Doctor.objects.create(clinic=clinic, user=user_doc, full_name="Dr Test")
        patient_1 = Patient.objects.create(clinic=clinic, full_name="Patient 1", phone="1234567890")
        patient_walkin = Patient.objects.create(clinic=clinic, full_name="Walkin Patient", phone="0987654321")

        now = timezone.now().replace(minute=0, second=0, microsecond=0)
        # Create an existing appointment at 'now' lasting 30 mins
        appt_1 = Appointment.objects.create(
            clinic=clinic,
            patient=patient_1,
            doctor=doctor,
            starts_at=now,
            ends_at=now + timedelta(minutes=30),
            reason="Routine checkup",
            status=Appointment.Status.SCHEDULED,
        )

        # 1. Test suggest_walkin overlapping at 'now' for 15 mins
        suggest_res = receptionist_client.post(
            "/api/v1/appointments/walkin/suggest/",
            {
                "doctor": doctor.id,
                "starts_at": now.isoformat(),
                "duration_minutes": 15,
            },
            format="json",
        )
        assert suggest_res.status_code == status.HTTP_200_OK
        shifts = suggest_res.data["shifts"]
        assert len(shifts) == 1
        assert shifts[0]["appointment_id"] == appt_1.id
        # Expect new start time to be now + 15 mins
        expected_new_start = (now + timedelta(minutes=15)).isoformat()
        assert shifts[0]["new_starts_at"] == expected_new_start

        # 2. Test confirm_walkin with the suggested shifts
        confirm_res = receptionist_client.post(
            "/api/v1/appointments/walkin/confirm/",
            {
                "doctor": doctor.id,
                "patient": patient_walkin.id,
                "starts_at": now.isoformat(),
                "duration_minutes": 15,
                "reason": "Urgent consultation",
                "priority": "urgent",
                "confirmed_shifts": shifts,
            },
            format="json",
        )
        assert confirm_res.status_code == status.HTTP_200_OK
        assert confirm_res.data["shifted_count"] == 1
        assert confirm_res.data["walkin"]["priority"] == "urgent"
        assert confirm_res.data["walkin"]["source"] == "walk_in"

        # Verify appt_1 was shifted in database
        appt_1.refresh_from_db()
        assert appt_1.starts_at == now + timedelta(minutes=15)
        assert appt_1.ends_at == now + timedelta(minutes=45)  # 30m duration preserved

        # Verify SMS MessageLog created
        from patients.models import MessageLog
        msg = MessageLog.objects.filter(patient=patient_1).first()
        assert msg is not None
        assert "has been shifted" in msg.message_text

