"""The front desk's "All centres" view (X-Active-Clinic-Id: all): one list across the hospital's centres."""
from __future__ import annotations

from datetime import time, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from appointments.models import Appointment
from doctors.models import Doctor
from patients.models import Patient

pytestmark = pytest.mark.django_db


@pytest.fixture()
def hospital(clinic, clinic_admin):
    """A main hospital with one centre; the admin belongs to both."""
    from clinics.models import Clinic
    centre = Clinic.objects.create(name="Centre K", parent=clinic)
    clinic_admin.clinics.add(clinic, centre)
    main_doc = Doctor.objects.create(clinic=clinic, full_name="Main Doc", available_from=time(9), available_to=time(17), working_days=[1, 2, 3, 4, 5, 6, 7])
    centre_doc = Doctor.objects.create(clinic=centre, full_name="Centre Doc", available_from=time(9), available_to=time(17), working_days=[1, 2, 3, 4, 5, 6, 7])
    patient = Patient.objects.create(clinic=clinic, full_name="Asha", phone="+919999900001")
    start = timezone.now() + timedelta(days=2)
    a_main = Appointment.objects.create(clinic=clinic, doctor=main_doc, patient=patient, starts_at=start, ends_at=start + timedelta(minutes=20))
    centre_patient = Patient.objects.create(clinic=centre, full_name="Bina", phone="+919999900002")
    a_centre = Appointment.objects.create(clinic=centre, doctor=centre_doc, patient=centre_patient, starts_at=start, ends_at=start + timedelta(minutes=20))
    return dict(main=clinic, centre=centre, main_doc=main_doc, centre_doc=centre_doc, patient=patient, a_main=a_main, a_centre=a_centre)


def test_all_centres_lists_every_centre_and_names_it(admin_client, hospital):
    one = admin_client.get("/api/v1/appointments/", {"page_size": 50}).data["results"]
    assert {a["id"] for a in one} == {hospital["a_main"].id}

    every = admin_client.get("/api/v1/appointments/", {"page_size": 50}, HTTP_X_ACTIVE_CLINIC_ID="all").data["results"]
    assert {a["id"] for a in every} == {hospital["a_main"].id, hospital["a_centre"].id}
    assert {a["clinic_name"] for a in every} == {hospital["main"].name, "Centre K"}

    doctors = admin_client.get("/api/v1/doctors/", HTTP_X_ACTIVE_CLINIC_ID="all").data
    rows = doctors.get("results", doctors)
    assert {d["full_name"] for d in rows} >= {"Main Doc", "Centre Doc"}


def test_booking_from_all_centres_lands_in_the_doctors_centre(admin_client, hospital):
    start = (timezone.now() + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
    res = admin_client.post("/api/v1/appointments/", {
        "doctor": hospital["centre_doc"].id, "patient": hospital["patient"].id,
        "starts_at": start.isoformat(), "ends_at": (start + timedelta(minutes=20)).isoformat(), "reason": "x", "source": "front_desk",
    }, format="json", HTTP_X_ACTIVE_CLINIC_ID="all")
    assert res.status_code == 201, res.data
    booked = Appointment.objects.get(id=res.data["id"])
    assert booked.clinic == hospital["centre"]
    # The patient was known only at the main hospital: a matching record now exists at the centre.
    assert booked.patient.clinic == hospital["centre"] and booked.patient.phone == hospital["patient"].phone
    assert res.data["clinic_name"] == "Centre K"


def test_a_centre_only_user_cannot_widen_to_the_whole_hospital(hospital):
    user = User.objects.create_user(clinic=hospital["centre"], email="desk@centre.example.com", password="p", role=User.Role.RECEPTIONIST,
                                    membership_status=User.MembershipStatus.ACTIVE, is_verified=True)
    client = APIClient(); client.force_authenticate(user=user)
    rows = client.get("/api/v1/appointments/", {"page_size": 50}, HTTP_X_ACTIVE_CLINIC_ID="all").data["results"]
    assert {a["id"] for a in rows} == {hospital["a_centre"].id}
