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
