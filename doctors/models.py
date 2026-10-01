from __future__ import annotations

from django.db import models

from common.models import ClinicScopedModel


def default_working_days():
    return [1, 2, 3, 4, 5]


class Doctor(ClinicScopedModel):
    user = models.OneToOneField(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="doctor_profile",
    )
    full_name = models.CharField(max_length=255)
    degree = models.CharField(max_length=120, blank=True)
    specialty = models.CharField(max_length=120, default="General Practice")
    consultation_minutes = models.PositiveSmallIntegerField(default=15)
    max_patients_per_day = models.PositiveSmallIntegerField(null=True, blank=True)
    available_from = models.TimeField(null=True, blank=True)
    available_to = models.TimeField(null=True, blank=True)
    lunch_from = models.TimeField(null=True, blank=True)
    lunch_to = models.TimeField(null=True, blank=True)
    working_days = models.JSONField(default=default_working_days)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["full_name"]

    def __str__(self) -> str:
        return f"Dr. {self.full_name}"

class DoctorAbsence(ClinicScopedModel):
    doctor = models.ForeignKey(Doctor, on_delete=models.CASCADE, related_name="absences")
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(db_index=True)
    reason = models.CharField(max_length=255, blank=True)
    
    class Meta:
        ordering = ["start_date"]
        indexes = [
            models.Index(fields=["doctor", "start_date", "end_date"]),
        ]
