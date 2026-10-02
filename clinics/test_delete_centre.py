"""Deleting a centre: only a centre (never the main clinic), and only while it holds no records."""
from __future__ import annotations

import pytest

from accounts.models import User
from audit.models import DataChangeEvent
from clinics.context import ACTIVE_CLINIC_HEADER
from clinics.models import Clinic, ClinicConfiguration
from doctors.models import Doctor
from patients.models import Patient

pytestmark = pytest.mark.django_db


@pytest.fixture
def centre(admin_client, clinic):
    """A centre of the test clinic, created the way the app creates one."""
    response = admin_client.post("/api/v1/clinics/", {"name": "Test Clinic North"}, format="json")
    assert response.status_code == 201, response.content
    return Clinic.objects.get(id=response.data["id"])


def delete(client, target):
    return client.delete(f"/api/v1/clinics/{target.id}/", **{f"HTTP_{ACTIVE_CLINIC_HEADER.upper().replace('-', '_')}": str(target.id)})


def test_admin_deletes_an_empty_centre(admin_client, clinic_admin, clinic, centre):
    # Set-up data that goes with the centre: its configuration and a doctor profile without a login.
    Doctor.objects.create(clinic=centre, full_name="Dr North", working_days=[1, 2, 3])

    response = delete(admin_client, centre)

    assert response.status_code == 204, response.content
    assert not Clinic.objects.filter(id=centre.id).exists()
    assert not ClinicConfiguration.objects.filter(clinic_id=centre.id).exists()
    assert not Doctor.objects.filter(clinic_id=centre.id).exists()
    # The admin and the main clinic are untouched.
    clinic_admin.refresh_from_db()
    assert clinic_admin.clinic_id == clinic.id
    assert list(clinic_admin.clinics.values_list("id", flat=True)) == [clinic.id]
    assert DataChangeEvent.objects.filter(model_name="Clinic", object_id=str(centre.id), action="delete").exists()


def test_main_clinic_cannot_be_deleted(admin_client, clinic, centre):
    response = delete(admin_client, clinic)

    assert response.status_code == 400
    assert Clinic.objects.filter(id=clinic.id).exists()


def test_centre_with_patients_or_staff_is_kept(admin_client, centre):
    Patient.objects.create(clinic=centre, full_name="North Patient", phone="9000000003")
    staff = User.objects.create_user(
        clinic=centre, email="desk@north.example.com", full_name="North Desk", password="x-Pass-123!",
        role=User.Role.RECEPTIONIST, membership_status=User.MembershipStatus.ACTIVE, is_verified=True,
    )

    response = delete(admin_client, centre)

    assert response.status_code == 409
    assert "1 patient" in response.data["detail"] and "1 staff account" in response.data["detail"]
    assert Clinic.objects.filter(id=centre.id).exists()
    assert User.objects.filter(id=staff.id).exists()
    assert not DataChangeEvent.objects.filter(model_name="Clinic", action="delete").exists()


def test_only_the_active_centre_can_be_deleted(admin_client, clinic, centre):
    # No active-clinic header: the admin is acting in the main clinic, so the centre's URL is a 404.
    response = admin_client.delete(f"/api/v1/clinics/{centre.id}/")

    assert response.status_code == 404
    assert Clinic.objects.filter(id=centre.id).exists()


def test_receptionist_cannot_delete(receptionist_user, receptionist_client, centre):
    receptionist_user.clinics.add(centre)

    response = delete(receptionist_client, centre)

    assert response.status_code == 403
    assert Clinic.objects.filter(id=centre.id).exists()
