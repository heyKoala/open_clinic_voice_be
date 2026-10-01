from __future__ import annotations

from django.db import transaction
from django.db.models import Max

from doctors.models import Doctor
from queue_mgmt.models import QueueToken


@transaction.atomic
def issue_queue_token(*, clinic, doctor, patient, service_date, appointment=None, **fields) -> QueueToken:
    """Create the next token for a doctor's daily queue.

    Locks the doctor row so concurrent check-ins cannot allocate the same number
    (which would violate ``uniq_daily_doctor_token``).
    """
    Doctor.objects.select_for_update().filter(pk=doctor.pk).first()
    max_token = QueueToken.objects.filter(
        clinic=clinic, doctor=doctor, service_date=service_date
    ).aggregate(Max("token_number"))["token_number__max"] or 0
    return QueueToken.objects.create(
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        appointment=appointment,
        service_date=service_date,
        token_number=max_token + 1,
        **fields,
    )
