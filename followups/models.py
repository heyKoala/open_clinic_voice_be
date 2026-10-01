from django.db import models

from common.models import ClinicScopedModel


class Campaign(ClinicScopedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        COMPLETED = "completed", "Completed"

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["clinic", "status"])]

    def __str__(self) -> str:
        return self.name


class FollowUp(ClinicScopedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SCHEDULED = "scheduled", "Scheduled"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    class Method(models.TextChoices):
        PHONE = "phone", "Phone"
        SMS = "sms", "SMS"
        EMAIL = "email", "Email"
        WHATSAPP = "whatsapp", "WhatsApp"
        IN_PERSON = "in_person", "In Person"

    campaign = models.ForeignKey(
        Campaign, null=True, blank=True, on_delete=models.SET_NULL, related_name="followups"
    )
    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="followups")
    doctor = models.ForeignKey("doctors.Doctor", null=True, blank=True, on_delete=models.SET_NULL, related_name="followups")
    appointment = models.ForeignKey(
        "appointments.Appointment", null=True, blank=True, on_delete=models.SET_NULL, related_name="followups"
    )
    scheduled_for = models.DateTimeField(db_index=True)
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.PHONE)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    notes = models.TextField(blank=True)
    outcome = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["scheduled_for"]
        indexes = [
            models.Index(fields=["clinic", "scheduled_for"]),
            models.Index(fields=["clinic", "status"]),
            models.Index(fields=["patient", "scheduled_for"]),
        ]

    def __str__(self) -> str:
        patient_name = self.patient.full_name if self.patient else "Unknown"
        return f"Follow-up for {patient_name} - {self.scheduled_for}"
