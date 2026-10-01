from __future__ import annotations

import pytest
from rest_framework import status

from patients.models import Patient
from audit.models import DataChangeEvent

pytestmark = pytest.mark.django_db


class TestPatientIsolation:
    def test_admin_cannot_see_other_clinic_patients(self, admin_client, clinic, clinic_b):
        # Create a patient in Clinic B
        other_patient = Patient.objects.create(
            clinic=clinic_b,
            full_name="Other Clinic Patient",
            phone="+1234567890"
        )
        
        # Admin from Clinic A tries to get the patient
        res = admin_client.get(f"/api/v1/patients/{other_patient.id}/")
        assert res.status_code == status.HTTP_404_NOT_FOUND

        # Admin from Clinic A tries to list patients
        res_list = admin_client.get("/api/v1/patients/")
        assert res_list.status_code == status.HTTP_200_OK
        assert len(res_list.data["results"]) == 0

    def test_admin_cannot_update_other_clinic_patient(self, admin_client, clinic_b):
        other_patient = Patient.objects.create(
            clinic=clinic_b,
            full_name="Other Clinic Patient",
            phone="+1234567890"
        )
        res = admin_client.patch(f"/api/v1/patients/{other_patient.id}/", {"full_name": "Hacked"})
        assert res.status_code == status.HTTP_404_NOT_FOUND

    def test_admin_cannot_delete_other_clinic_patient(self, admin_client, clinic_b):
        other_patient = Patient.objects.create(
            clinic=clinic_b,
            full_name="Other Clinic Patient",
            phone="+1234567890"
        )
        res = admin_client.delete(f"/api/v1/patients/{other_patient.id}/")
        assert res.status_code == status.HTTP_404_NOT_FOUND

    def test_soft_delete_and_audit(self, admin_client, clinic):
        patient = Patient.objects.create(
            clinic=clinic,
            full_name="Delete Me",
            phone="+1234567890"
        )
        res = admin_client.delete(f"/api/v1/patients/{patient.id}/")
        assert res.status_code == status.HTTP_204_NO_CONTENT
        
        # Verify soft delete
        patient.refresh_from_db()
        assert not patient.is_active
        
        # Verify audit log
        audit = DataChangeEvent.objects.filter(object_id=patient.id, action="delete").first()
        assert audit is not None

    def test_pagination_and_filtering(self, admin_client, clinic):
        for i in range(25):
            Patient.objects.create(
                clinic=clinic,
                full_name=f"Patient {i}",
                phone=f"+100000000{i:02d}",
                gender="female" if i % 2 == 0 else "male"
            )
        
        # Pagination
        res = admin_client.get("/api/v1/patients/?page=2")
        assert res.status_code == status.HTTP_200_OK
        assert len(res.data["results"]) == 5
        
        # Filtering
        res_filter = admin_client.get("/api/v1/patients/?gender=female")
        assert res_filter.status_code == status.HTTP_200_OK
        assert res_filter.data["count"] == 13
