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
    # Front desk mapping, set by an admin on the Access page. Only matters once a centre has more
    # than one receptionist: see doctors_for_receptionist().
    receptionists = models.ManyToManyField("accounts.User", blank=True, related_name="assigned_doctors")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["full_name"]

    def __str__(self) -> str:
        return f"Dr. {self.full_name}"


def active_receptionists(clinic):
    from accounts.models import User

    return User.objects.filter(
        clinic=clinic, role=User.Role.RECEPTIONIST, is_active=True, membership_status=User.MembershipStatus.ACTIVE,
    )


def doctors_for_receptionist(queryset, user, clinic):
    """The doctors this receptionist's front desk manages.

    A centre with a single receptionist: all of them. With several, the doctors mapped to this
    receptionist, plus any doctor not mapped to an active receptionist (so nobody goes unmanaged).
    """
    desk = active_receptionists(clinic)
    if desk.count() <= 1:
        return queryset
    return queryset.filter(models.Q(receptionists=user) | ~models.Q(receptionists__in=desk)).distinct()


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
