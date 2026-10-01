import datetime
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from appointments.models import Appointment
from patients.models import MessageLog


def suggest_walkin_shifts(doctor, starts_at, duration_minutes):
    """
    Given a proposed walk-in start time and duration, check for overlapping
    appointments on that day and calculate cascading shifts while preserving
    exact appointment durations.
    """
    ends_at = starts_at + datetime.timedelta(minutes=duration_minutes)
    date = timezone.localdate(starts_at)

    # Get all scheduled/checked_in appointments for this doctor on this date
    appts = list(
        Appointment.objects.filter(
            doctor=doctor,
            status__in=[Appointment.Status.SCHEDULED, Appointment.Status.CHECKED_IN],
            starts_at__date=date,
            is_active=True,
        )
        .select_related("patient")
        .order_by("starts_at")
    )

    shifts = []
    current_end = ends_at

    for appt in appts:
        # Check if this appointment overlaps with [starts_at, current_end)
        if appt.starts_at < current_end and appt.ends_at > starts_at:
            duration = appt.ends_at - appt.starts_at
            new_starts_at = max(appt.starts_at, current_end)
            new_ends_at = new_starts_at + duration

            shifts.append({
                "appointment_id": appt.id,
                "patient_id": appt.patient_id,
                "patient_name": appt.patient.full_name,
                "old_starts_at": appt.starts_at.isoformat(),
                "old_ends_at": appt.ends_at.isoformat(),
                "new_starts_at": new_starts_at.isoformat(),
                "new_ends_at": new_ends_at.isoformat(),
            })

            current_end = new_ends_at

    return shifts


@transaction.atomic
def execute_walkin_and_shifts(clinic, doctor, patient, starts_at, duration_minutes, reason, priority, confirmed_shifts=None):
    """
    Creates a walk-in appointment and applies confirmed shifts to existing appointments
    using select_for_update() and transaction.atomic(). Also creates MessageLog alerts.
    """
    ends_at = starts_at + datetime.timedelta(minutes=duration_minutes)

    shifted_appts = []
    if confirmed_shifts:
        appt_ids = [s["appointment_id"] for s in confirmed_shifts]
        # Only this doctor's active appointments in this clinic may be shifted.
        locked_appts = {
            appt.id: appt
            for appt in Appointment.objects.select_for_update()
            .select_related("patient")
            .filter(id__in=appt_ids, clinic=clinic, doctor=doctor, is_active=True)
        }

        for shift_data in confirmed_shifts:
            appt_id = shift_data["appointment_id"]
            if appt_id in locked_appts:
                appt = locked_appts[appt_id]
                new_start = parse_datetime(str(shift_data["new_starts_at"]))
                new_end = parse_datetime(str(shift_data["new_ends_at"]))
                if new_start is None or new_end is None or new_end <= new_start:
                    raise ValueError(f"Invalid shift times for appointment {appt_id}.")
                if timezone.is_naive(new_start):
                    new_start = timezone.make_aware(new_start)
                if timezone.is_naive(new_end):
                    new_end = timezone.make_aware(new_end)

                appt.starts_at = new_start
                appt.ends_at = new_end
                appt.save(update_fields=["starts_at", "ends_at", "updated_at"])
                shifted_appts.append(appt)

                # Send MessageLog notification
                time_str = timezone.localtime(new_start).strftime("%b %d, %Y at %I:%M %p")
                msg_text = (
                    f"Dear {appt.patient.full_name}, your appointment with Dr. {doctor.full_name} "
                    f"has been shifted to {time_str} due to an urgent walk-in adjustment. "
                    "Please contact the clinic if you have questions."
                )
                MessageLog.objects.create(
                    patient=appt.patient,
                    clinic=clinic,
                    method=MessageLog.Method.SMS,
                    message_text=msg_text,
                    is_sent=True,
                )

    # Create Walk-in appointment
    walkin = Appointment.objects.create(
        clinic=clinic,
        patient=patient,
        doctor=doctor,
        starts_at=starts_at,
        ends_at=ends_at,
        reason=reason or "Walk-in consultation",
        status=Appointment.Status.CHECKED_IN,
        priority=priority or Appointment.Priority.NORMAL,
        source="walk_in",
    )

    from queue_mgmt.models import QueueToken
    from queue_mgmt.services import issue_queue_token

    issue_queue_token(
        clinic=clinic,
        appointment=walkin,
        patient=patient,
        doctor=doctor,
        service_date=timezone.localdate(starts_at),
        status=QueueToken.Status.WAITING,
    )

    return walkin, shifted_appts
