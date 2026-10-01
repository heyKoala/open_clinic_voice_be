from __future__ import annotations

import pytest
from rest_framework import status
from django.utils import timezone
from datetime import date

from queue_mgmt.models import QueueToken
from patients.models import Patient
from doctors.models import Doctor
from accounts.models import User
from audit.models import DataChangeEvent

pytestmark = pytest.mark.django_db


class TestQueueIsolation:
    def test_receptionist_cannot_see_other_clinic_queue(self, receptionist_client, clinic, clinic_b):
        user_b = User.objects.create_user(clinic=clinic_b, email="b@e.com", password="p", role=User.Role.DOCTOR)
        doctor_b = Doctor.objects.create(clinic=clinic_b, user=user_b, full_name="B")
        patient_b = Patient.objects.create(clinic=clinic_b, full_name="PB")
        
        token = QueueToken.objects.create(
            clinic=clinic_b,
            patient=patient_b,
            doctor=doctor_b,
            service_date=date.today(),
            token_number=1,
            status=QueueToken.Status.WAITING
        )
        
        res = receptionist_client.get(f"/api/v1/queue/{token.id}/")
        assert res.status_code == status.HTTP_404_NOT_FOUND

        res_list = receptionist_client.get("/api/v1/queue/")
        assert res_list.status_code == status.HTTP_200_OK
        assert len(res_list.data["results"]) == 0

    def test_soft_delete_and_audit(self, receptionist_client, clinic):
        user_a = User.objects.create_user(clinic=clinic, email="a@e.com", password="p", role=User.Role.DOCTOR)
        doctor = Doctor.objects.create(clinic=clinic, user=user_a, full_name="A")
        patient = Patient.objects.create(clinic=clinic, full_name="PA")
        
        token = QueueToken.objects.create(
            clinic=clinic,
            patient=patient,
            doctor=doctor,
            service_date=date.today(),
            token_number=1,
            status=QueueToken.Status.WAITING
        )
        
        res = receptionist_client.delete(f"/api/v1/queue/{token.id}/")
        assert res.status_code == status.HTTP_204_NO_CONTENT
        
        token.refresh_from_db()
        assert not token.is_active
        
        audit = DataChangeEvent.objects.filter(object_id=token.id).first()
        assert audit is not None
