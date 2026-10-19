"""Reminder and feedback calls: planned from the appointment, recorded by the desk, and what the desk must do."""
from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest
from django.utils import timezone

from accounts.models import User
from appointments import journey
from appointments.models import Appointment, PatientCall
from doctors.models import Doctor
from patients.models import Patient

pytestmark = pytest.mark.django_db

Kind, Status = PatientCall.Kind, PatientCall.Status


@pytest.fixture()
def doctor(clinic):
    user = User.objects.create_user(clinic=clinic, email="dr@e.com", password="p", role=User.Role.DOCTOR)
    return Doctor.objects.create(clinic=clinic, user=user, full_name="Asha Rao")


@pytest.fixture()
def patient(clinic):
    return Patient.objects.create(clinic=clinic, full_name="Ravi Kumar", phone="+919800000001")


def _local(days, hour, minute=0):
    day = timezone.localdate() + timedelta(days=days)
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


def _book(clinic, doctor, patient, starts_at, **extra):
    return Appointment.objects.create(clinic=clinic, doctor=doctor, patient=patient, starts_at=starts_at,
                                      ends_at=starts_at + timedelta(minutes=15), **extra)


def _call(appointment, kind):
    return PatientCall.objects.filter(appointment=appointment, kind=kind).first()


def test_booking_plans_a_reminder_the_day_before(clinic, doctor, patient):
    appointment = _book(clinic, doctor, patient, _local(3, 15))
    reminder = _call(appointment, Kind.REMINDER)
    assert reminder.status == Status.SCHEDULED
    assert reminder.due_at == _local(2, 10)  # 10:00 the day before
    assert _call(appointment, Kind.FEEDBACK) is None


def test_reschedule_moves_the_reminder_and_cancel_drops_it(clinic, doctor, patient):
    appointment = _book(clinic, doctor, patient, _local(3, 15))
    appointment.starts_at, appointment.ends_at = _local(5, 9), _local(5, 9, 15)
    appointment.save()
    assert _call(appointment, Kind.REMINDER).due_at == _local(4, 10)

    appointment.status = Appointment.Status.CANCELLED
    appointment.save()
    assert _call(appointment, Kind.REMINDER).status == Status.SKIPPED


def test_late_booking_is_reminded_now_but_not_within_two_hours(clinic, doctor, patient):
    soon = _book(clinic, doctor, patient, timezone.now() + timedelta(hours=5))
    assert _call(soon, Kind.REMINDER).due_at <= timezone.now()
    too_soon = _book(clinic, doctor, patient, timezone.now() + timedelta(hours=1))
    assert _call(too_soon, Kind.REMINDER) is None


def test_completed_visit_plans_feedback_the_next_day(clinic, doctor, patient):
    appointment = _book(clinic, doctor, patient, _local(0, 9) if timezone.now() > _local(0, 9, 30) else _local(-1, 9))
    appointment.status = Appointment.Status.COMPLETED
    appointment.save()
    feedback = _call(appointment, Kind.FEEDBACK)
    visit_day = timezone.localtime(appointment.ends_at).date()
    assert feedback.due_at == max(timezone.make_aware(datetime.combine(visit_day + timedelta(days=1), time(11))),
                                  feedback.due_at)  # next day 11:00, or now if that has passed
    assert feedback.status == Status.SCHEDULED


def test_unanswered_reminder_is_retried_then_left_for_the_desk(clinic, doctor, patient):
    reminder = _call(_book(clinic, doctor, patient, _local(3, 15)), Kind.REMINDER)
    journey.record(reminder, "no_answer")
    assert (reminder.status, reminder.attempts) == (Status.SCHEDULED, 1)  # retried in 2 hours
    journey.record(reminder, "no_answer")
    assert reminder.status == Status.NO_ANSWER
    assert journey.needs(reminder, journey.settings_for(clinic)) == "call_back"


def test_reminder_outcomes_cancel_or_flag_the_appointment(clinic, doctor, patient):
    cancel = _book(clinic, doctor, patient, _local(3, 15))
    journey.record(_call(cancel, Kind.REMINDER), "cancel")
    cancel.refresh_from_db()
    assert cancel.status == Appointment.Status.CANCELLED

    move = _book(clinic, doctor, patient, _local(3, 16))
    journey.record(_call(move, Kind.REMINDER), "reschedule")
    move.refresh_from_db()
    assert move.status == Appointment.Status.NEEDS_RESCHEDULE


def test_feedback_rating_decides_review_or_call_back(clinic, doctor, patient):
    settings = journey.settings_for(clinic)
    settings.review_url = "https://g.page/r/gunasheela/review"
    settings.save()

    def feedback_call(hour):
        appointment = _book(clinic, doctor, patient, _local(-1, hour))
        appointment.status = Appointment.Status.COMPLETED
        appointment.save()
        return _call(appointment, Kind.FEEDBACK)

    happy = journey.record(feedback_call(9), "rated", rating=5)
    assert journey.needs(happy, settings) == "send_review"
    unhappy = journey.record(feedback_call(10), "rated", rating=2, comment="Long wait")
    assert journey.needs(unhappy, settings) == "unhappy"
    with pytest.raises(journey.RecordError):
        journey.record(feedback_call(11), "rated", rating=9)


def test_todo_list_and_actions_over_the_api(receptionist_client, admin_client, clinic, doctor, patient):
    due_now = _book(clinic, doctor, patient, timezone.now() + timedelta(hours=5))
    _book(clinic, doctor, patient, _local(4, 15))  # reminder not due yet

    todo = receptionist_client.get("/api/v1/appointments/patient-calls/", {"todo": 1}).data
    assert [(row["appointment"], row["kind"], row["needs"]) for row in todo] == [(due_now.id, "reminder", "call")]
    assert todo[0]["patient_name"] == "Ravi Kumar" and todo[0]["doctor_name"] == "Asha Rao"

    res = receptionist_client.post(f"/api/v1/appointments/patient-calls/{todo[0]['id']}/record/", {"outcome": "confirmed"})
    assert res.status_code == 200 and res.data["result"] == "confirmed" and res.data["needs"] is None
    assert receptionist_client.post(f"/api/v1/appointments/patient-calls/{todo[0]['id']}/record/",
                                    {"outcome": "rated"}).status_code == 400
    assert receptionist_client.get("/api/v1/appointments/patient-calls/", {"todo": 1}).data == []

    journey_rows = receptionist_client.get("/api/v1/appointments/patient-calls/", {"appointment": due_now.id}).data
    assert [row["kind"] for row in journey_rows] == ["reminder"]
    # The appointment list still works next to the new route.
    assert receptionist_client.get("/api/v1/appointments/").status_code == 200


def test_settings_admin_only_and_moves_planned_calls(receptionist_client, admin_client, clinic, doctor, patient):
    appointment = _book(clinic, doctor, patient, _local(3, 15))
    assert receptionist_client.get("/api/v1/appointments/patient-calls/settings/").data["reminder_time"] == "10:00:00"
    assert receptionist_client.put("/api/v1/appointments/patient-calls/settings/", {"reminder_time": "09:00"},
                                   format="json").status_code == 403

    res = admin_client.put("/api/v1/appointments/patient-calls/settings/", {"reminder_time": "18:00", "review_min_rating": 9},
                           format="json")
    assert res.status_code == 400 and "review_min_rating" in res.data
    res = admin_client.put("/api/v1/appointments/patient-calls/settings/",
                           {"reminder_time": "18:00", "review_url": "https://g.page/r/x/review"}, format="json")
    assert res.status_code == 200 and res.data["reminder_time"] == "18:00:00"
    assert _call(appointment, Kind.REMINDER).due_at == _local(2, 18)
