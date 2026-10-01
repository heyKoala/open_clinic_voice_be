"""Branch isolation: staff of one clinic (branch) can never read or act on another branch's data.

The attacker is a receptionist, doctor or admin at the "Downtown" branch; every target belongs to the "Care Center"
branch. Each test uses real IDs from Care Center, i.e. what someone could guess or copy from a URL.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from ai_agent.models import CallLog
from appointments.models import Appointment
from clinical.models import ClinicalEncounter, ClinicalNote, PatientHealthRecord, SymptomSummary
from doctors.models import Doctor
from followups.models import FollowUp
from patients.models import Patient
from queue_mgmt.models import QueueToken

pytestmark = pytest.mark.django_db

DENIED = {400, 403, 404}


@pytest.fixture
def care(clinic):
    """Care Center branch with a doctor's full day of data."""
    doctor_user = User.objects.create_user(
        clinic=clinic, email="dr@care.example.com", full_name="Dr Care", password="x-Pass-123!",
        role=User.Role.DOCTOR, membership_status=User.MembershipStatus.ACTIVE, is_verified=True,
    )
    doctor = Doctor.objects.create(clinic=clinic, user=doctor_user, full_name="Dr Care", working_days=[1, 2, 3, 4, 5, 6, 7])
    patient = Patient.objects.create(clinic=clinic, full_name="Care Patient", phone="9000000001")
    start = timezone.now().replace(second=0, microsecond=0) + timedelta(hours=2)
    appt = Appointment.objects.create(clinic=clinic, doctor=doctor, patient=patient, starts_at=start,
                                      ends_at=start + timedelta(minutes=15), status="scheduled")
    token = QueueToken.objects.create(clinic=clinic, doctor=doctor, patient=patient, appointment=appt,
                                      service_date=timezone.localdate(), token_number=1)
    encounter = ClinicalEncounter.objects.create(clinic=clinic, patient=patient, doctor=doctor, appointment=appt)
    return {
        "clinic": clinic, "doctor_user": doctor_user, "doctor": doctor, "patient": patient, "appt": appt,
        "token": token, "encounter": encounter,
        "summary": SymptomSummary.objects.create(clinic=clinic, encounter=encounter, summary_text="s",
                                                 ai_model="m", ai_version="1", confidence=0.9),
        "note": ClinicalNote.objects.create(clinic=clinic, encounter=encounter, author=doctor_user, content="n"),
        "record": PatientHealthRecord.objects.create(clinic=clinic, patient=patient, category="allergy", name="Peanuts"),
        "followup": FollowUp.objects.create(clinic=clinic, patient=patient, scheduled_for=start),
        "call": CallLog.objects.create(clinic=clinic, direction="inbound", occurred_at=start, patient=patient),
    }


@pytest.fixture(params=["receptionist", "doctor", "clinic_admin"])
def downtown(request, clinic_b):
    """A Downtown staff member. Admins are the most privileged single-branch role, so if they are
    kept out, isolation doesn't depend on role permissions."""
    role = request.param
    user = User.objects.create_user(
        clinic=clinic_b, email=f"{role}@downtown.example.com", full_name=f"Downtown {role}", password="x-Pass-123!",
        role=role, membership_status=User.MembershipStatus.ACTIVE, is_verified=True,
    )
    doctor = Doctor.objects.create(clinic=clinic_b, user=user if role == "doctor" else None,
                                   full_name="Dr Downtown", working_days=[1, 2, 3, 4, 5, 6, 7])
    patient = Patient.objects.create(clinic=clinic_b, full_name="Downtown Patient", phone="9000000002")
    client = APIClient()
    client.force_authenticate(user=user)
    return {"clinic": clinic_b, "user": user, "doctor": doctor, "patient": patient, "client": client}


def _assert_unchanged(care):
    care["appt"].refresh_from_db()
    care["token"].refresh_from_db()
    care["doctor"].refresh_from_db()
    care["patient"].refresh_from_db()
    assert care["appt"].status == "scheduled" and care["appt"].is_active
    assert care["token"].status == QueueToken.Status.WAITING and care["token"].is_active
    assert care["doctor"].is_active and care["doctor"].full_name == "Dr Care"
    assert care["patient"].full_name == "Care Patient"


# --- Direct access by ID ------------------------------------------------------------------------

@pytest.mark.parametrize("method, path", [
    ("get", "/api/v1/appointments/{appt}/"),
    ("patch", "/api/v1/appointments/{appt}/"),
    ("delete", "/api/v1/appointments/{appt}/"),
    ("post", "/api/v1/appointments/{appt}/check_in/"),
    ("post", "/api/v1/appointments/{appt}/complete/"),
    ("get", "/api/v1/queue/{token}/"),
    ("patch", "/api/v1/queue/{token}/"),
    ("delete", "/api/v1/queue/{token}/"),
    ("post", "/api/v1/queue/{token}/check_in/"),
    ("post", "/api/v1/queue/{token}/mark_seen/"),
    ("get", "/api/v1/doctors/{doctor}/"),
    ("patch", "/api/v1/doctors/{doctor}/"),
    ("delete", "/api/v1/doctors/{doctor}/"),
    ("post", "/api/v1/doctors/{doctor}/mark-absent/"),
    ("get", "/api/v1/patients/{patient}/"),
    ("patch", "/api/v1/patients/{patient}/"),
    ("delete", "/api/v1/patients/{patient}/"),
    ("get", "/api/v1/clinical/encounters/{encounter}/"),
    ("patch", "/api/v1/clinical/encounters/{encounter}/"),
    ("get", "/api/v1/clinical/symptom-summaries/{summary}/"),
    ("post", "/api/v1/clinical/symptom-summaries/{summary}/approve/"),
    ("post", "/api/v1/clinical/symptom-summaries/{summary}/reject/"),
    ("post", "/api/v1/clinical/symptom-summaries/{summary}/edit_summary/"),
    ("get", "/api/v1/clinical/clinical-notes/{note}/"),
    ("get", "/api/v1/clinical/health-records/{record}/"),
    ("patch", "/api/v1/clinical/health-records/{record}/"),
    ("get", "/api/v1/followups/followups/{followup}/"),
    ("patch", "/api/v1/followups/followups/{followup}/"),
    ("get", "/api/v1/ai/call-logs/{call}/"),
    ("get", "/api/v1/ai/call-logs/{call}/recording/"),
    ("post", "/api/v1/auth/users/{doctor_user}/deactivate/"),
    ("post", "/api/v1/auth/users/{doctor_user}/toggle-doctor/"),
    ("get", "/api/v1/clinics/{clinic}/"),
    ("patch", "/api/v1/clinics/{clinic}/"),
])
def test_other_branch_objects_are_unreachable(care, downtown, method, path):
    ids = {k: v.id for k, v in care.items()}
    body = {"status": "cancelled", "full_name": "Hacked", "notes": "x", "summary_text": "x",
            "start_date": str(timezone.localdate()), "name": "Hacked"}
    res = getattr(downtown["client"], method)(path.format(**ids), body, format="json")
    assert res.status_code in DENIED, f"{method.upper()} {path} -> {res.status_code}: {getattr(res, 'data', '')}"
    _assert_unchanged(care)


# --- Switching into the other branch ------------------------------------------------------------

def test_active_clinic_header_cannot_switch_into_a_branch_you_do_not_belong_to(care, downtown):
    client = downtown["client"]
    client.credentials(HTTP_X_ACTIVE_CLINIC_ID=str(care["clinic"].id))
    for path in ["/api/v1/appointments/", "/api/v1/queue/", "/api/v1/doctors/", "/api/v1/patients/",
                 "/api/v1/clinical/encounters/", "/api/v1/followups/followups/", "/api/v1/ai/call-logs/"]:
        res = client.get(path)
        if res.status_code == 403:  # role can't list this at all (e.g. receptionists and clinical records)
            continue
        assert res.status_code == 200, path
        rows = res.data["results"] if isinstance(res.data, dict) and "results" in res.data else res.data
        assert all(row.get("clinic") in (None, downtown["clinic"].id) for row in rows), path
        assert not any(row.get("id") in {care["appt"].id, care["token"].id, care["doctor"].id, care["patient"].id}
                       and row.get("clinic") == care["clinic"].id for row in rows), path
    res = client.post(f"/api/v1/appointments/{care['appt'].id}/complete/", {}, format="json")
    assert res.status_code in DENIED
    _assert_unchanged(care)


def test_my_clinics_lists_only_own_branch(care, downtown):
    res = downtown["client"].get("/api/v1/auth/my-clinics/")
    assert res.status_code == 200
    rows = res.data["results"] if isinstance(res.data, dict) and "results" in res.data else res.data
    assert {row["id"] for row in rows} == {downtown["clinic"].id}


# --- Creating things that point at the other branch ---------------------------------------------

def _start():
    return (timezone.now() + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)


@pytest.mark.parametrize("path, make_body", [
    ("/api/v1/appointments/", lambda c, d: {"doctor": c["doctor"].id, "patient": d["patient"].id,
                                            "starts_at": _start().isoformat(), "ends_at": (_start() + timedelta(minutes=15)).isoformat()}),
    ("/api/v1/appointments/", lambda c, d: {"doctor": d["doctor"].id, "patient": c["patient"].id,
                                            "starts_at": _start().isoformat(), "ends_at": (_start() + timedelta(minutes=15)).isoformat()}),
    ("/api/v1/appointments/walkin/suggest/", lambda c, d: {"doctor": c["doctor"].id, "starts_at": _start().isoformat()}),
    ("/api/v1/appointments/walkin/confirm/", lambda c, d: {"doctor": c["doctor"].id, "patient": d["patient"].id,
                                                           "starts_at": _start().isoformat()}),
    ("/api/v1/appointments/walkin/confirm/", lambda c, d: {"doctor": d["doctor"].id, "patient": c["patient"].id,
                                                           "starts_at": _start().isoformat()}),
    ("/api/v1/queue/", lambda c, d: {"doctor": c["doctor"].id, "patient": d["patient"].id, "service_date": str(timezone.localdate())}),
    ("/api/v1/queue/", lambda c, d: {"doctor": d["doctor"].id, "patient": c["patient"].id, "service_date": str(timezone.localdate())}),
    ("/api/v1/queue/call_next/", lambda c, d: {"doctor": c["doctor"].id}),
    ("/api/v1/clinical/encounters/", lambda c, d: {"patient": c["patient"].id, "doctor": d["doctor"].id}),
    ("/api/v1/clinical/encounters/", lambda c, d: {"patient": d["patient"].id, "doctor": c["doctor"].id}),
    ("/api/v1/clinical/encounters/", lambda c, d: {"patient": d["patient"].id, "doctor": d["doctor"].id, "appointment": c["appt"].id}),
    ("/api/v1/clinical/symptom-summaries/", lambda c, d: {"encounter": c["encounter"].id, "summary_text": "x",
                                                          "ai_model": "m", "ai_version": "1", "confidence": 0.5}),
    ("/api/v1/clinical/clinical-notes/", lambda c, d: {"encounter": c["encounter"].id, "content": "x"}),
    ("/api/v1/clinical/health-records/", lambda c, d: {"patient": c["patient"].id, "category": "allergy", "name": "x"}),
    ("/api/v1/followups/followups/", lambda c, d: {"patient": c["patient"].id, "scheduled_for": _start().isoformat()}),
    ("/api/v1/ai/call-logs/", lambda c, d: {"patient": c["patient"].id, "direction": "inbound", "occurred_at": _start().isoformat()}),
])
def test_cannot_create_records_that_reference_the_other_branch(care, downtown, path, make_body):
    before = {m: m.objects.count() for m in (Appointment, QueueToken, ClinicalEncounter, SymptomSummary,
                                            ClinicalNote, PatientHealthRecord, FollowUp, CallLog)}
    res = downtown["client"].post(path, make_body(care, downtown), format="json")
    assert res.status_code in DENIED, f"POST {path} -> {res.status_code}: {getattr(res, 'data', '')}"
    assert before == {m: m.objects.count() for m in before}
    _assert_unchanged(care)


@pytest.mark.parametrize("field, target", [("doctor", "doctor"), ("patient", "patient")])
def test_cannot_move_own_appointment_onto_the_other_branch(care, downtown, field, target):
    start = _start()
    appt = Appointment.objects.create(clinic=downtown["clinic"], doctor=downtown["doctor"], patient=downtown["patient"],
                                      starts_at=start, ends_at=start + timedelta(minutes=15))
    res = downtown["client"].patch(f"/api/v1/appointments/{appt.id}/", {field: care[target].id}, format="json")
    assert res.status_code in DENIED, res.data
    appt.refresh_from_db()
    assert appt.doctor_id == downtown["doctor"].id and appt.patient_id == downtown["patient"].id


def test_live_queue_report_hides_other_branch_doctor(care, downtown):
    res = downtown["client"].get("/api/v1/reports/live-queue/", {"doctor": care["doctor"].id})
    assert res.status_code in DENIED | {200}
    if res.status_code == 200:
        assert care["patient"].full_name not in str(res.data)


# --- Real-time channels -------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_queue_websocket_rejects_other_branch_doctor(care, downtown):
    from asgiref.sync import async_to_sync
    from channels.testing import WebsocketCommunicator
    from queue_mgmt.consumers import QueueConsumer

    async def attempt(doctor_id):
        comm = WebsocketCommunicator(QueueConsumer.as_asgi(), f"/ws/queue/{doctor_id}/")
        comm.scope["user"] = downtown["user"]
        comm.scope["url_route"] = {"kwargs": {"doctor_id": doctor_id}}
        connected, _ = await comm.connect()
        await comm.disconnect()
        return connected

    assert async_to_sync(attempt)(downtown["doctor"].id) is True
    assert async_to_sync(attempt)(care["doctor"].id) is False


def test_doctor_can_still_record_clinical_data_in_own_branch(clinic_b):
    user = User.objects.create_user(
        clinic=clinic_b, email="own@downtown.example.com", full_name="Dr Own", password="x-Pass-123!",
        role=User.Role.DOCTOR, membership_status=User.MembershipStatus.ACTIVE, is_verified=True,
    )
    doctor = Doctor.objects.create(clinic=clinic_b, user=user, full_name="Dr Own")
    patient = Patient.objects.create(clinic=clinic_b, full_name="Own Patient", phone="9000000003")
    client = APIClient()
    client.force_authenticate(user=user)

    res = client.post("/api/v1/clinical/encounters/", {"patient": patient.id, "doctor": doctor.id, "chief_complaint": "Fever"}, format="json")
    assert res.status_code == 201, res.data
    assert res.data["patient"]["id"] == patient.id and res.data["doctor"]["id"] == doctor.id
    encounter_id = res.data["id"]

    res = client.post("/api/v1/clinical/clinical-notes/", {"encounter": encounter_id, "content": "Rest and fluids"}, format="json")
    assert res.status_code == 201, res.data
    assert ClinicalNote.objects.get(id=res.data["id"]).author_id == user.id

    res = client.post("/api/v1/clinical/symptom-summaries/", {"encounter": encounter_id, "summary_text": "Fever 2 days",
                                                              "ai_model": "m", "ai_version": "1", "confidence": 0.8}, format="json")
    assert res.status_code == 201, res.data
    assert res.data["status"] == "pending"
