from celery import shared_task
from django.db import transaction
from django.utils import timezone
from appointments.models import Appointment
from queue_mgmt.models import QueueToken

OPEN_TOKEN_STATUSES = [
    QueueToken.Status.WAITING,
    QueueToken.Status.CHECKED_IN,
    QueueToken.Status.CALLED,
    QueueToken.Status.IN_CONSULTATION,
]


def _close_expired(from_status, to_status, token_status, cutoff_time, now):
    """Move expired appointments to ``to_status`` and close their open queue tokens.

    Saves each appointment individually (not queryset.update) so the post_save
    signal broadcasts the change to live dashboards.
    """
    count = 0
    with transaction.atomic():
        expired = Appointment.objects.select_for_update(skip_locked=True).filter(
            status=from_status,
            ends_at__lte=cutoff_time,
            is_active=True
        )
        for appt in expired:
            appt.status = to_status
            appt.save(update_fields=['status', 'updated_at'])
            token_fields = {"status": token_status, "updated_at": now}
            if token_status == QueueToken.Status.COMPLETED:
                token_fields["served_at"] = now
            QueueToken.objects.filter(appointment=appt, status__in=OPEN_TOKEN_STATUSES).update(**token_fields)
            count += 1
    return count


@shared_task
def auto_complete_expired_appointments():
    """
    Automatically marks appointments that have ended more than 2 hours ago as completed (if they were checked in)
    or no-show (if they were just scheduled).
    """
    now = timezone.now()
    cutoff_time = now - timezone.timedelta(hours=2)

    # 1. Appointments that were checked in but never formally "completed" by the doctor
    completed = _close_expired(
        Appointment.Status.CHECKED_IN, Appointment.Status.COMPLETED, QueueToken.Status.COMPLETED, cutoff_time, now
    )
    # 2. Appointments that were scheduled but the patient never showed up
    no_shows = _close_expired(
        Appointment.Status.SCHEDULED, Appointment.Status.NO_SHOW, QueueToken.Status.SKIPPED, cutoff_time, now
    )
    return f"Completed {completed} appointments and marked {no_shows} as no-shows."
