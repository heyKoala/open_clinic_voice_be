from __future__ import annotations

import pytest
from rest_framework import status

from doctors.models import Doctor
from accounts.models import User
from audit.models import DataChangeEvent

pytestmark = pytest.mark.django_db


class TestDoctorIsolation:
    def test_admin_cannot_see_other_clinic_doctors(self, admin_client, clinic_b):
        user_b = User.objects.create_user(
            clinic=clinic_b,
            email="otherdoc@example.com",
            full_name="Other Doc",
            password="pass",
            role=User.Role.DOCTOR
        )
        other_doctor = Doctor.objects.create(
            clinic=clinic_b,
            user=user_b,
            full_name="Other Doc",
            specialty="General"
        )
        
        res = admin_client.get(f"/api/v1/doctors/{other_doctor.id}/")
        assert res.status_code == status.HTTP_404_NOT_FOUND

        res_list = admin_client.get("/api/v1/doctors/")
        assert res_list.status_code == status.HTTP_200_OK
        assert len(res_list.data["results"]) == 0

    def test_admin_cannot_update_other_clinic_doctor(self, admin_client, clinic_b):
        user_b = User.objects.create_user(
            clinic=clinic_b,
            email="otherdoc2@example.com",
            full_name="Other Doc",
            password="pass",
            role=User.Role.DOCTOR
        )
        other_doctor = Doctor.objects.create(
            clinic=clinic_b,
            user=user_b,
            full_name="Other Doc",
            specialty="General"
        )
        res = admin_client.patch(f"/api/v1/doctors/{other_doctor.id}/", {"specialty": "Hacked"})
        assert res.status_code == status.HTTP_404_NOT_FOUND

    def test_soft_delete_and_audit(self, admin_client, clinic):
        user_a = User.objects.create_user(
            clinic=clinic,
            email="mydoc@example.com",
            full_name="My Doc",
            password="pass",
            role=User.Role.DOCTOR
        )
        doctor = Doctor.objects.create(
            clinic=clinic,
            user=user_a,
            full_name="My Doc",
            specialty="General"
        )
        res = admin_client.delete(f"/api/v1/doctors/{doctor.id}/")
        assert res.status_code == status.HTTP_204_NO_CONTENT
        
        doctor.refresh_from_db()
        assert not doctor.is_active
        
        audit = DataChangeEvent.objects.filter(object_id=doctor.id).first()
        assert audit is not None


class TestFrontDeskDoctors:
    """GET /doctors/?desk=mine: the doctors a receptionist's front desk manages."""

    URL = "/api/v1/doctors/?desk=mine"

    @pytest.fixture
    def doctors(self, clinic):
        return [Doctor.objects.create(clinic=clinic, full_name=name) for name in ("Asha", "Bela", "Chetan")]

    @pytest.fixture
    def second_receptionist(self, clinic):
        return User.objects.create_user(
            clinic=clinic, email="desk2@testclinic.example.com", full_name="Second Desk", password="Desk-Pass-123!",
            role=User.Role.RECEPTIONIST, membership_status=User.MembershipStatus.ACTIVE, is_verified=True,
        )

    @staticmethod
    def names(response):
        assert response.status_code == status.HTTP_200_OK
        return [row["full_name"] for row in response.data["results"]]

    def test_a_single_receptionist_manages_every_doctor(self, receptionist_client, doctors):
        assert self.names(receptionist_client.get(self.URL)) == ["Asha", "Bela", "Chetan"]

    def test_with_several_receptionists_each_sees_their_own_and_unmapped_doctors(
        self, receptionist_client, receptionist_user, second_receptionist, doctors,
    ):
        asha, bela, _chetan = doctors
        asha.receptionists.add(receptionist_user)
        bela.receptionists.add(second_receptionist)

        assert self.names(receptionist_client.get(self.URL)) == ["Asha", "Chetan"]

        second_client = receptionist_client.__class__()
        second_client.force_authenticate(user=second_receptionist)
        assert self.names(second_client.get(self.URL)) == ["Bela", "Chetan"]

    def test_a_doctor_shared_by_both_receptionists_is_listed_once(
        self, receptionist_client, receptionist_user, second_receptionist, doctors,
    ):
        doctors[0].receptionists.add(receptionist_user, second_receptionist)

        assert self.names(receptionist_client.get(self.URL)) == ["Asha", "Bela", "Chetan"]

    def test_a_deactivated_receptionists_doctors_fall_back_to_everyone(
        self, clinic, receptionist_client, second_receptionist, doctors,
    ):
        left = User.objects.create_user(
            clinic=clinic, email="left@testclinic.example.com", full_name="Left Desk", password="Desk-Pass-123!",
            role=User.Role.RECEPTIONIST, membership_status=User.MembershipStatus.SUSPENDED, is_active=False,
        )
        doctors[0].receptionists.add(left)
        doctors[1].receptionists.add(second_receptionist)

        assert self.names(receptionist_client.get(self.URL)) == ["Asha", "Chetan"]

    def test_the_full_list_and_other_roles_are_not_narrowed(
        self, receptionist_client, admin_client, second_receptionist, doctors,
    ):
        doctors[0].receptionists.add(second_receptionist)

        assert self.names(receptionist_client.get("/api/v1/doctors/")) == ["Asha", "Bela", "Chetan"]
        assert self.names(admin_client.get(self.URL)) == ["Asha", "Bela", "Chetan"]

    def test_a_doctor_cannot_change_their_own_front_desk(self, clinic, doctor_client, doctor_user, receptionist_user):
        doctor = Doctor.objects.create(clinic=clinic, user=doctor_user, full_name="Test Doctor")

        res = doctor_client.patch(f"/api/v1/doctors/{doctor.id}/", {"receptionists": [receptionist_user.id]}, format="json")

        assert res.status_code == status.HTTP_200_OK
        assert not doctor.receptionists.exists()
