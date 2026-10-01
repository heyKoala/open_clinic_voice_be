from django.db import models

from common.models import ClinicScopedModel


class Appointment(ClinicScopedModel):
    class Status(models.TextChoices): SCHEDULED="scheduled", "Scheduled"; CHECKED_IN="checked_in", "Checked in"; COMPLETED="completed", "Completed"; CANCELLED="cancelled", "Cancelled"; NO_SHOW="no_show", "No show"; NEEDS_RESCHEDULE="needs_reschedule", "Needs Reschedule"
    class Priority(models.TextChoices): NORMAL="normal", "Normal"; URGENT="urgent", "Urgent"; EMERGENCY="emergency", "Emergency"
    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="appointments")
    doctor = models.ForeignKey("doctors.Doctor", on_delete=models.PROTECT, related_name="appointments")
    starts_at = models.DateTimeField(db_index=True)
    ends_at = models.DateTimeField()
    reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    priority = models.CharField(max_length=16, choices=Priority.choices, default=Priority.NORMAL)
    source = models.CharField(max_length=32, default="manual")
    is_active = models.BooleanField(default=True)
    reminder_24h_sent = models.BooleanField(default=False)
    reminder_2h_sent = models.BooleanField(default=False)
    class Meta:
        ordering = ["starts_at"]
        indexes = [models.Index(fields=["clinic", "starts_at"]), models.Index(fields=["doctor", "starts_at"])]
