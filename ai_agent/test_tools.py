from __future__ import annotations
import pytest
from datetime import datetime, timedelta, date, time
from django.utils import timezone
from rest_framework import status

from clinics.models import Clinic
from doctors.models import Doctor, DoctorAbsence
from patients.models import Patient
from appointments.models import Appointment

pytestmark = pytest.mark.django_db

@pytest.fixture
def rock8_headers(settings):
    settings.ROCK8_WEBHOOK_SECRET = "test-secret"
    return {"HTTP_AUTHORIZATION": "Bearer test-secret"}

@pytest.fixture
def doctor(clinic):
    return Doctor.objects.create(
        clinic=clinic,
        full_name="Test Doctor",
        specialty="General",
        consultation_minutes=15,
        available_from=time(9, 0),
        available_to=time(17, 0),
        working_days=[1, 2, 3, 4, 5, 6, 7]
    )

@pytest.fixture
def patient(clinic):
    return Patient.objects.create(
        clinic=clinic,
        full_name="Test Patient",
        phone="1234567890"
    )

class TestRock8Tools:
    def test_auth_required(self, api_client, clinic):
        res = api_client.get(f"/api/v1/ai/tools/rock8/{clinic.id}/doctors/")
        assert res.status_code == status.HTTP_403_FORBIDDEN
        
    def test_list_doctors_scoped(self, api_client, clinic, clinic_b, rock8_headers):
        doc1 = Doctor.objects.create(clinic=clinic, full_name="Doc A")
        doc2 = Doctor.objects.create(clinic=clinic_b, full_name="Doc B")
        
        res = api_client.get(f"/api/v1/ai/tools/rock8/{clinic.id}/doctors/", **rock8_headers)
        assert res.status_code == status.HTTP_200_OK
        assert len(res.data["doctors"]) == 1
        assert res.data["doctors"][0]["name"] == "Doc A"
        
    def test_available_slots(self, api_client, clinic, doctor, patient, rock8_headers):
        today = timezone.localtime().date()
        target_date = today + timedelta(days=1)
        
        # Doctor has appointment at 10:00 AM
        tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime.combine(target_date, time(10, 0)), tz)
        Appointment.objects.create(
            clinic=clinic, doctor=doctor, patient=patient, starts_at=start,
            ends_at=start + timedelta(minutes=15),
            status="scheduled"
        )
        
        res = api_client.get(
            f"/api/v1/ai/tools/rock8/{clinic.id}/slots/",
            {"doctor_id": doctor.id, "date": target_date.isoformat()},
            **rock8_headers
        )
        
        assert res.status_code == status.HTTP_200_OK
        assert "09:00" in res.data["slots"]
        assert "10:00" not in res.data["slots"]  # Overlapping slot removed
        assert "10:15" in res.data["slots"]
        
    def test_book_appointment_success(self, api_client, clinic, doctor, patient, rock8_headers):
        target_date = timezone.localtime().date() + timedelta(days=1)
        
        res = api_client.post(
            f"/api/v1/ai/tools/rock8/{clinic.id}/book/",
            {
                "doctor_id": doctor.id,
                "date": target_date.isoformat(),
                "time": "14:00",
                "patient_phone": "1234567890",
                "patient_name": "Kavya Rao"
            },
            format='json',
            **rock8_headers
        )
        
        assert res.status_code == status.HTTP_200_OK
        assert res.data["success"] is True
        assert res.data["appointment_id"] is not None
        
        appt = Appointment.objects.get(id=res.data["appointment_id"])
        assert appt.patient == patient
        assert appt.doctor == doctor
        
    def test_book_appointment_auto_create_patient(self, api_client, clinic, doctor, rock8_headers):
        target_date = timezone.localtime().date() + timedelta(days=1)
        
        res = api_client.post(
            f"/api/v1/ai/tools/rock8/{clinic.id}/book/",
            {
                "doctor_id": doctor.id,
                "date": target_date.isoformat(),
                "time": "14:00",
                "patient_phone": "0987654321",
                "patient_name": "New Person"
            },
            format='json',
            **rock8_headers
        )
        
        assert res.status_code == status.HTTP_200_OK
        assert res.data["success"] is True
        
        # Verify patient was created
        new_patient = Patient.objects.filter(clinic=clinic, phone="0987654321").first()
        assert new_patient is not None
        assert new_patient.full_name == "New Person"
        
    def test_book_appointment_conflict(self, api_client, clinic, doctor, patient, rock8_headers):
        target_date = timezone.localtime().date() + timedelta(days=1)
        tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime.combine(target_date, time(14, 0)), tz)
        
        # Existing appointment
        Appointment.objects.create(
            clinic=clinic, doctor=doctor, patient=patient, starts_at=start,
            ends_at=start + timedelta(minutes=15), status="scheduled"
        )
        
        res = api_client.post(
            f"/api/v1/ai/tools/rock8/{clinic.id}/book/",
            {
                "doctor_id": doctor.id,
                "date": target_date.isoformat(),
                "time": "14:00",
                "patient_phone": "1234567890",
                "patient_name": "Kavya Rao"
            },
            format='json',
            **rock8_headers
        )
        
        assert res.status_code == status.HTTP_200_OK
        assert res.data["success"] is False
        assert res.data["error"] == "SLOT_UNAVAILABLE"
        
    def test_cancel_appointment(self, api_client, clinic, doctor, patient, rock8_headers):
        target_date = timezone.localtime().date() + timedelta(days=1)
        tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime.combine(target_date, time(14, 0)), tz)
        
        appt = Appointment.objects.create(
            clinic=clinic, doctor=doctor, patient=patient, starts_at=start,
            ends_at=start + timedelta(minutes=15), status="scheduled"
        )
        
        res = api_client.post(
            f"/api/v1/ai/tools/rock8/{clinic.id}/cancel/",
            {"appointment_id": appt.id},
            format='json',
            **rock8_headers
        )
        
        assert res.status_code == status.HTTP_200_OK
        assert res.data["success"] is True
        
        appt.refresh_from_db()
        assert appt.status == "cancelled"
        
    def test_cancel_cross_clinic(self, api_client, clinic, clinic_b, doctor, patient, rock8_headers):
        target_date = timezone.localtime().date() + timedelta(days=1)
        tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime.combine(target_date, time(14, 0)), tz)
        
        # Appointment belongs to clinic_b
        appt = Appointment.objects.create(
            clinic=clinic_b, doctor=doctor, patient=patient, starts_at=start,
            ends_at=start + timedelta(minutes=15), status="scheduled"
        )
        
        # Try to cancel via clinic's webhook
        res = api_client.post(
            f"/api/v1/ai/tools/rock8/{clinic.id}/cancel/",
            {"appointment_id": appt.id},
            format='json',
            **rock8_headers
        )
        
        assert res.status_code == status.HTTP_200_OK
        assert res.data["success"] is False
        assert res.data["error"] == "NOT_FOUND"
