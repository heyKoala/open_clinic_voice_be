"""An admin editing a team member (and a doctor's profile) from the Access page."""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from accounts.models import User
from audit.models import DataChangeEvent
from doctors.models import Doctor

pytestmark = pytest.mark.django_db


def url(user):
    return f"/api/v1/auth/users/{user.id}/"


@pytest.fixture
def doctor(clinic, doctor_user):
    return Doctor.objects.create(clinic=clinic, user=doctor_user, full_name=doctor_user.full_name)


def test_admin_reads_a_doctor_with_their_profile(admin_client, doctor_user, doctor):
    response = admin_client.get(url(doctor_user))

    assert response.status_code == 200
    assert response.data["email"] == doctor_user.email
    assert response.data["doctor"]["id"] == doctor.id
    assert response.data["doctor"]["working_days"] == [1, 2, 3, 4, 5]


def test_admin_edits_a_doctor(admin_client, doctor_user, doctor):
    response = admin_client.patch(url(doctor_user), {
        "full_name": "Asha Rao", "age": 41, "gender": "female",
        "doctor": {
            "degree": "MBBS, MD", "specialty": "Cardiology", "consultation_minutes": 20, "max_patients_per_day": 25,
            "available_from": "10:00", "available_to": "18:00", "lunch_from": "13:00", "lunch_to": "14:00",
            "working_days": [1, 2, 3],
            # Not editable here: must be ignored.
            "is_active": False, "user": None,
        },
    }, format="json")

    assert response.status_code == 200, response.content
    doctor_user.refresh_from_db()
    doctor.refresh_from_db()
    assert (doctor_user.full_name, doctor_user.age, doctor_user.gender) == ("Asha Rao", 41, "female")
    assert (doctor.full_name, doctor.degree, doctor.specialty) == ("Asha Rao", "MBBS, MD", "Cardiology")
    assert (doctor.consultation_minutes, doctor.max_patients_per_day, doctor.working_days) == (20, 25, [1, 2, 3])
    assert str(doctor.available_from) == "10:00:00" and str(doctor.lunch_to) == "14:00:00"
    assert doctor.is_active and doctor.user_id == doctor_user.id
    assert response.data["doctor"]["specialty"] == "Cardiology"
    assert DataChangeEvent.objects.filter(model_name="User", object_id=str(doctor_user.id), action="update").exists()
    assert DataChangeEvent.objects.filter(model_name="Doctor", object_id=str(doctor.id), action="update").exists()


def test_admin_edits_a_receptionist(admin_client, receptionist_user):
    response = admin_client.patch(url(receptionist_user), {"full_name": "Front Desk", "age": None}, format="json")

    assert response.status_code == 200, response.content
    receptionist_user.refresh_from_db()
    assert receptionist_user.full_name == "Front Desk"
    assert response.data["doctor"] is None


def test_login_and_role_cannot_be_changed(admin_client, receptionist_user):
    response = admin_client.patch(url(receptionist_user), {
        "email": "someone@else.example.com", "role": "clinic_admin", "is_clinic_admin": True,
    }, format="json")

    assert response.status_code == 200
    receptionist_user.refresh_from_db()
    assert receptionist_user.email == "receptionist@testclinic.example.com"
    assert receptionist_user.role == User.Role.RECEPTIONIST and not receptionist_user.is_clinic_admin


def test_invalid_doctor_hours_change_nothing(admin_client, doctor_user, doctor):
    response = admin_client.patch(url(doctor_user), {
        "full_name": "Changed", "doctor": {"available_from": "18:00", "available_to": "09:00"},
    }, format="json")

    assert response.status_code == 400
    assert "available_from" in response.data["doctor"]
    doctor_user.refresh_from_db()
    assert doctor_user.full_name == "Test Doctor"


def test_doctor_fields_for_a_receptionist_are_rejected(admin_client, receptionist_user):
    response = admin_client.patch(url(receptionist_user), {"doctor": {"specialty": "X"}}, format="json")

    assert response.status_code == 400


def test_other_clinics_staff_are_out_of_reach(admin_client, clinic_b):
    outsider = User.objects.create_user(
        clinic=clinic_b, email="desk@other.example.com", full_name="Other Desk", password="x-Pass-123!",
        role=User.Role.RECEPTIONIST, membership_status=User.MembershipStatus.ACTIVE, is_verified=True,
    )

    assert admin_client.get(url(outsider)).status_code == 404
    assert admin_client.patch(url(outsider), {"full_name": "Hacked"}, format="json").status_code == 404
    outsider.refresh_from_db()
    assert outsider.full_name == "Other Desk"


def test_staff_cannot_edit_each_other(receptionist_client, doctor_user):
    assert receptionist_client.get(url(doctor_user)).status_code == 403
    assert receptionist_client.patch(url(doctor_user), {"full_name": "Hacked"}, format="json").status_code == 403


# ---------------------------------------------------------------------------
# Front desk mapping: which receptionists manage a doctor
# ---------------------------------------------------------------------------

def make_receptionist(clinic, email, **extra):
    return User.objects.create_user(
        clinic=clinic, email=email, full_name=email.split("@")[0].title(), password="Desk-Pass-123!",
        role=User.Role.RECEPTIONIST, membership_status=User.MembershipStatus.ACTIVE, is_verified=True, **extra,
    )


def test_admin_maps_a_doctor_to_receptionists(admin_client, clinic, doctor_user, doctor, receptionist_user):
    second = make_receptionist(clinic, "desk2@testclinic.example.com")

    response = admin_client.patch(url(doctor_user), {"doctor": {"receptionists": [second.id]}}, format="json")

    assert response.status_code == 200, response.content
    assert response.data["doctor"]["receptionists"] == [second.id]
    assert list(doctor.receptionists.all()) == [second]
    assert admin_client.get(url(doctor_user)).data["doctor"]["receptionists"] == [second.id]

    # Leaving the mapping out of a later edit keeps it; an empty list clears it.
    admin_client.patch(url(doctor_user), {"doctor": {"specialty": "Cardiology"}}, format="json")
    assert list(doctor.receptionists.all()) == [second]
    admin_client.patch(url(doctor_user), {"doctor": {"receptionists": []}}, format="json")
    assert not doctor.receptionists.exists()


def test_a_doctor_can_only_be_mapped_to_this_centres_active_receptionists(
    admin_client, clinic, clinic_b, clinic_admin, doctor_user, doctor, receptionist_user,
):
    outsider = make_receptionist(clinic_b, "desk@other.example.com")
    inactive = make_receptionist(clinic, "left@testclinic.example.com", is_active=False)

    for ids in ([outsider.id], [inactive.id], [clinic_admin.id], [receptionist_user.id, 999999], "nope", [True]):
        response = admin_client.patch(
            url(doctor_user), {"full_name": "Changed", "doctor": {"receptionists": ids}}, format="json",
        )
        assert response.status_code == 400, ids
        assert "receptionists" in response.data["doctor"]

    doctor_user.refresh_from_db()
    assert doctor_user.full_name == "Test Doctor"
    assert not doctor.receptionists.exists()
