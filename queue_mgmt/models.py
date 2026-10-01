from django.db import models

from common.models import ClinicScopedModel


class QueueToken(ClinicScopedModel):
    class Status(models.TextChoices): WAITING="waiting", "Waiting"; CHECKED_IN="checked_in", "Checked In"; CALLED="called", "Called"; IN_CONSULTATION="in_consultation", "In Consultation"; COMPLETED="completed", "Completed"; SKIPPED="skipped", "Skipped"
    appointment = models.OneToOneField("appointments.Appointment", null=True, blank=True, on_delete=models.SET_NULL, related_name="queue_token")
    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="queue_tokens")
    doctor = models.ForeignKey("doctors.Doctor", on_delete=models.PROTECT, related_name="queue_tokens")
    service_date = models.DateField(db_index=True)
    token_number = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.WAITING)
    notes = models.TextField(blank=True, null=True)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    served_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    class Meta:
        ordering=["service_date", "token_number"]
        constraints=[models.UniqueConstraint(fields=["clinic","doctor","service_date","token_number"], name="uniq_daily_doctor_token")]
