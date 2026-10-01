from __future__ import annotations

import pytest
from rest_framework import status
from django.utils import timezone
from datetime import timedelta

from followups.models import FollowUp
from patients.models import Patient
from doctors.models import Doctor
from accounts.models import User
from audit.models import DataChangeEvent

pytestmark = pytest.mark.django_db


class TestFollowUpIsolation:
    def test_receptionist_cannot_see_other_clinic_followups(self, receptionist_client, clinic, clinic_b):
        user_b = User.objects.create_user(clinic=clinic_b, email="b@e.com", password="p", role=User.Role.DOCTOR)
        doctor_b = Doctor.objects.create(clinic=clinic_b, user=user_b, full_name="B")
        patient_b = Patient.objects.create(clinic=clinic_b, full_name="PB")
        
        followup = FollowUp.objects.create(
            clinic=clinic_b,
            patient=patient_b,
            doctor=doctor_b,
            scheduled_for=timezone.now() + timedelta(days=1),
            method=FollowUp.Method.PHONE,
            status=FollowUp.Status.PENDING,
            notes="Followup notes"
        )
        
        res = receptionist_client.get(f"/api/v1/followups/followups/{followup.id}/")
        assert res.status_code == status.HTTP_404_NOT_FOUND

        res_list = receptionist_client.get("/api/v1/followups/followups/")
        assert res_list.status_code == status.HTTP_200_OK
        assert len(res_list.data["results"]) == 0

    def test_soft_delete_and_audit(self, receptionist_client, clinic):
        user_a = User.objects.create_user(clinic=clinic, email="a@e.com", password="p", role=User.Role.DOCTOR)
        doctor = Doctor.objects.create(clinic=clinic, user=user_a, full_name="A")
        patient = Patient.objects.create(clinic=clinic, full_name="PA")
        
        followup = FollowUp.objects.create(
            clinic=clinic,
            patient=patient,
            doctor=doctor,
            scheduled_for=timezone.now() + timedelta(days=1),
            method=FollowUp.Method.PHONE,
            status=FollowUp.Status.PENDING,
            notes="Followup notes"
        )
        
        res = receptionist_client.delete(f"/api/v1/followups/followups/{followup.id}/")
        assert res.status_code == status.HTTP_204_NO_CONTENT
        
        followup.refresh_from_db()
        assert not followup.is_active
        
        audit = DataChangeEvent.objects.filter(object_id=followup.id).first()
        assert audit is not None
