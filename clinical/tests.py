from __future__ import annotations

import pytest
from rest_framework import status
from django.utils import timezone

from patients.models import Patient
from doctors.models import Doctor
from accounts.models import User
from clinical.models import ClinicalEncounter, SymptomSummary
from audit.models import DataChangeEvent

pytestmark = pytest.mark.django_db


class TestClinicalIsolation:
    def test_doctor_cannot_see_other_clinic_encounter(self, doctor_client, doctor_user, clinic, clinic_b):
        # Create doctor in Clinic B
        user_b = User.objects.create_user(clinic=clinic_b, email="docb@e.com", password="p", role=User.Role.DOCTOR)
        doctor_b = Doctor.objects.create(clinic=clinic_b, user=user_b, full_name="B")
        patient_b = Patient.objects.create(clinic=clinic_b, full_name="PB")
        
        encounter = ClinicalEncounter.objects.create(
            clinic=clinic_b,
            patient=patient_b,
            doctor=doctor_b,
        )
        
        # doctor_client is Doctor A in Clinic A
        Doctor.objects.create(clinic=clinic, user=doctor_user, full_name="A")
        res = doctor_client.get(f"/api/v1/clinical/encounters/{encounter.id}/")
        assert res.status_code == status.HTTP_404_NOT_FOUND

        res_list = doctor_client.get("/api/v1/clinical/encounters/")
        assert len(res_list.data["results"]) == 0

    def test_doctor_cannot_see_unassigned_encounter_in_same_clinic(self, doctor_client, doctor_user, clinic):
        # doctor_client is Doctor A. Let's create Doctor C in same clinic.
        user_c = User.objects.create_user(clinic=clinic, email="docc@e.com", password="p", role=User.Role.DOCTOR)
        doctor_c = Doctor.objects.create(clinic=clinic, user=user_c, full_name="C")
        patient = Patient.objects.create(clinic=clinic, full_name="PA")
        
        encounter = ClinicalEncounter.objects.create(
            clinic=clinic,
            patient=patient,
            doctor=doctor_c,
        )
        Doctor.objects.create(clinic=clinic, user=doctor_user, full_name="A")
        
        res = doctor_client.get(f"/api/v1/clinical/encounters/{encounter.id}/")
        assert res.status_code == status.HTTP_404_NOT_FOUND

        res_list = doctor_client.get("/api/v1/clinical/encounters/")
        assert len(res_list.data["results"]) == 0


class TestSymptomSummaryWorkflow:
    def test_read_audit_log(self, doctor_client, doctor_user, clinic):
        doctor = Doctor.objects.create(clinic=clinic, user=doctor_user, full_name="A")
        patient = Patient.objects.create(clinic=clinic, full_name="PA")
        encounter = ClinicalEncounter.objects.create(clinic=clinic, patient=patient, doctor=doctor)
        
        summary = SymptomSummary.objects.create(
            clinic=clinic,
            encounter=encounter,
            summary_text="Patient has a headache.",
            ai_model="gpt-4",
            ai_version="1.0",
            confidence=0.9,
        )
        
        # Read the summary
        res = doctor_client.get(f"/api/v1/clinical/symptom-summaries/{summary.id}/")
        assert res.status_code == status.HTTP_200_OK
        
        # Verify read audit log
        audit = DataChangeEvent.objects.filter(object_id=summary.id, action="read").first()
        assert audit is not None
        assert audit.user == doctor_user

    def test_approve_workflow_and_audit(self, doctor_client, doctor_user, clinic):
        doctor = Doctor.objects.create(clinic=clinic, user=doctor_user, full_name="A")
        patient = Patient.objects.create(clinic=clinic, full_name="PA")
        encounter = ClinicalEncounter.objects.create(clinic=clinic, patient=patient, doctor=doctor)
        
        summary = SymptomSummary.objects.create(
            clinic=clinic,
            encounter=encounter,
            summary_text="Patient has a headache.",
            ai_model="gpt-4",
            ai_version="1.0",
            confidence=0.9,
        )
        
        res = doctor_client.post(f"/api/v1/clinical/symptom-summaries/{summary.id}/approve/")
        assert res.status_code == status.HTTP_200_OK
        
        summary.refresh_from_db()
        assert summary.status == SymptomSummary.SummaryStatus.APPROVED
        assert summary.reviewed_by == doctor_user
        
        # Verify update audit log
        audit = DataChangeEvent.objects.filter(object_id=summary.id, action="update").first()
        assert audit is not None

    def test_edit_workflow_and_audit(self, doctor_client, doctor_user, clinic):
        doctor = Doctor.objects.create(clinic=clinic, user=doctor_user, full_name="A")
        patient = Patient.objects.create(clinic=clinic, full_name="PA")
        encounter = ClinicalEncounter.objects.create(clinic=clinic, patient=patient, doctor=doctor)
        
        summary = SymptomSummary.objects.create(
            clinic=clinic,
            encounter=encounter,
            summary_text="Patient has a headache.",
            ai_model="gpt-4",
            ai_version="1.0",
            confidence=0.9,
        )
        
        res = doctor_client.post(
            f"/api/v1/clinical/symptom-summaries/{summary.id}/edit_summary/",
            {"summary_text": "Patient has a severe headache."}
        )
        assert res.status_code == status.HTTP_200_OK
        
        summary.refresh_from_db()
        assert summary.status == SymptomSummary.SummaryStatus.EDITED
        assert summary.summary_text == "Patient has a severe headache."
        assert summary.reviewed_by == doctor_user
        
        audit = DataChangeEvent.objects.filter(object_id=summary.id, action="update").first()
        assert audit is not None
