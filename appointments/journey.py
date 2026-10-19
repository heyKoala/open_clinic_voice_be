"""A patient's journey after booking: the reminder call the day before, and the feedback call after the visit.

``sync`` keeps an appointment's calls in step with it: it plans the reminder when the appointment is booked,
moves it when the appointment is rescheduled, drops it when the appointment is cancelled, and plans the
feedback call once the visit is completed. It runs whenever an appointment is saved (appointments.signals)
and every few minutes as a catch-up (appointments.tasks.plan_patient_calls).

``record`` saves what came of a call. The front desk enters it by hand until the AI places the calls.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from appointments.models import Appointment, PatientCall, PatientCallSettings

# A reminder planned less than this before the visit isn't worth making.
MIN_REMINDER_LEAD = timedelta(hours=2)
# Feedback is only asked for recent visits (also bounds the catch-up).
FEEDBACK_WINDOW = timedelta(days=7)

Kind, Status, Result = PatientCall.Kind, PatientCall.Status, PatientCall.Result


def settings_for(clinic) -> PatientCallSettings:
    settings, _ = PatientCallSettings.objects.get_or_create(clinic=clinic.root)
    return settings


def _at(day, time) -> datetime:
    return timezone.make_aware(datetime.combine(day, time))


def reminder_due(appointment, settings, now=None):
    """The day before at the reminder time; sooner if that has passed. None if it's too late to bother."""
    now = now or timezone.now()
    due = _at(timezone.localtime(appointment.starts_at).date() - timedelta(days=1), settings.reminder_time)
    if due >= now:
        return due
    return now if appointment.starts_at - now > MIN_REMINDER_LEAD else None


def feedback_due(appointment, settings, now=None):
    now = now or timezone.now()
    return max(_at(timezone.localtime(appointment.ends_at).date() + timedelta(days=1), settings.feedback_time), now)


def _upcoming(appointment, now) -> bool:
    return appointment.is_active and appointment.status == Appointment.Status.SCHEDULED and appointment.starts_at > now


def sync(appointment, now=None) -> None:
    now = now or timezone.now()
    settings = settings_for(appointment.clinic)
    calls = {c.kind: c for c in PatientCall.objects.filter(appointment=appointment)}

    reminder = calls.get(Kind.REMINDER)
    if _upcoming(appointment, now):
        moved = reminder is not None and reminder.for_starts_at != appointment.starts_at
        if (reminder is None or moved) and settings.reminder_enabled:
            due = reminder_due(appointment, settings, now)
            if due is not None:
                values = {"status": Status.SCHEDULED, "due_at": due, "for_starts_at": appointment.starts_at,
                          "attempts": 0, "last_attempt_at": None, "result": "", "resolved_at": None}
                PatientCall.objects.update_or_create(
                    appointment=appointment, kind=Kind.REMINDER, defaults={**values, "clinic": appointment.clinic})
            elif moved and reminder.status == Status.SCHEDULED:
                _skip(reminder)
    elif reminder is not None and reminder.status == Status.SCHEDULED:
        # Cancelled, no-show, the patient is already here, or the visit time has passed: nobody needs reminding.
        _skip(reminder)

    if (appointment.is_active and appointment.status == Appointment.Status.COMPLETED and settings.feedback_enabled
            and Kind.FEEDBACK not in calls and now - appointment.ends_at < FEEDBACK_WINDOW):
        PatientCall.objects.create(clinic=appointment.clinic, appointment=appointment, kind=Kind.FEEDBACK,
                                   due_at=feedback_due(appointment, settings, now))


def _skip(call) -> None:
    call.status = Status.SKIPPED
    call.save(update_fields=["status", "updated_at"])


def plan_all(now=None) -> int:
    """Catch-up for every appointment that could need a call. Returns how many were looked at."""
    now = now or timezone.now()
    appointments = Appointment.objects.filter(is_active=True).filter(
        starts_at__gte=now - FEEDBACK_WINDOW).select_related("clinic", "clinic__parent")
    count = 0
    for appointment in appointments.iterator():
        sync(appointment, now)
        count += 1
    return count


class RecordError(ValueError):
    pass


# "reschedule": the patient wants another time (the desk finds one); "rescheduled": it was already
# moved during the call (the AI booked the new time and cancelled this appointment).
REMINDER_OUTCOMES = {"confirmed", "cancel", "reschedule", "rescheduled", "no_answer"}
FEEDBACK_OUTCOMES = {"rated", "no_answer"}


@transaction.atomic
def record(call: PatientCall, outcome: str, rating=None, comment: str = "", now=None) -> PatientCall:
    """Save the outcome of a call. An unanswered call is retried until the attempts run out.

    A reminder where the patient asks to cancel cancels the appointment; one where they ask to move it
    flags the appointment for rescheduling, which puts it on the front desk's list.
    """
    now = now or timezone.now()
    allowed = REMINDER_OUTCOMES if call.kind == Kind.REMINDER else FEEDBACK_OUTCOMES
    if outcome not in allowed:
        raise RecordError(f"Choose one of: {', '.join(sorted(allowed))}.")
    if call.status in (Status.SKIPPED,):
        raise RecordError("This call is no longer needed.")

    call.attempts += 1
    call.last_attempt_at = now
    call.comment = (comment or "").strip()
    if outcome == "no_answer":
        settings = settings_for(call.clinic)
        if call.attempts < settings.reminder_attempts:
            call.status, call.due_at = Status.SCHEDULED, now + timedelta(minutes=settings.retry_after_minutes)
        else:
            call.status = Status.NO_ANSWER
        call.save()
        return call

    call.status = Status.DONE
    if call.kind == Kind.FEEDBACK:
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            raise RecordError("Give a rating from 1 to 5.") from None
        if not 1 <= rating <= 5:
            raise RecordError("Give a rating from 1 to 5.")
        call.result, call.rating = Result.RATED, rating
        call.save()
        return call

    call.result = {"confirmed": Result.CONFIRMED, "cancel": Result.CANCEL, "reschedule": Result.RESCHEDULE,
                   "rescheduled": Result.RESCHEDULE}[outcome]
    call.save()
    appointment = call.appointment
    if outcome == "cancel" and appointment.status == Appointment.Status.SCHEDULED:
        appointment.status = Appointment.Status.CANCELLED
        appointment.save(update_fields=["status", "updated_at"])
    elif outcome == "reschedule" and appointment.status == Appointment.Status.SCHEDULED:
        appointment.status = Appointment.Status.NEEDS_RESCHEDULE
        appointment.save(update_fields=["status", "updated_at"])
    return call


def needs(call: PatientCall, settings: PatientCallSettings, now=None) -> str | None:
    """What the front desk has to do about this call now, if anything."""
    now = now or timezone.now()
    if call.status == Status.SCHEDULED:
        # A reminder is only worth making before the visit; afterwards it is simply dropped.
        if call.kind == Kind.REMINDER and call.appointment.starts_at <= now:
            return None
        return "call" if call.due_at <= now else None
    if call.resolved_at:
        return None
    if call.status == Status.NO_ANSWER:
        return "call_back"
    if call.kind == Kind.FEEDBACK and call.result == Result.RATED and call.rating is not None:
        if call.rating < settings.review_min_rating:
            return "unhappy"
        if call.review_sent_at is None and settings.review_url:
            return "send_review"
    return None
