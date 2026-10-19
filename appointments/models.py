from datetime import time

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


class PatientCallSettings(models.Model):
    """When the hospital's reminder and feedback calls go out, and when a patient is asked for a Google review.

    One row per main hospital (shared by any centres); created with these defaults on first use.
    """
    clinic = models.OneToOneField("clinics.Clinic", on_delete=models.CASCADE, related_name="patient_call_settings")
    reminder_enabled = models.BooleanField(default=True)
    reminder_time = models.TimeField(default=time(10, 0))              # on the day before the appointment
    reminder_attempts = models.PositiveSmallIntegerField(default=2)  # tries before the desk is asked to call
    retry_after_minutes = models.PositiveSmallIntegerField(default=120)
    feedback_enabled = models.BooleanField(default=True)
    feedback_time = models.TimeField(default=time(11, 0))              # on the day after the visit
    review_url = models.URLField(blank=True)                         # the hospital's Google review link
    review_min_rating = models.PositiveSmallIntegerField(default=4)  # 1-5; at or above this, ask for a review
    # The AI places these calls itself (appointments.outbound). Off until the hospital switches it on.
    ai_calls_enabled = models.BooleanField(default=False)
    call_window_start = models.TimeField(default=time(9, 0))   # no calls before this…
    call_window_end = models.TimeField(default=time(20, 0))    # …or after this
    # What the AI says on these calls, edited on the AI Agent page; blank means the default
    # (appointments.outbound.DEFAULT_SCRIPTS). How it saves the outcome is added by the system.
    reminder_first_message = models.CharField(max_length=500, blank=True)
    reminder_script = models.TextField(blank=True)
    feedback_first_message = models.CharField(max_length=500, blank=True)
    feedback_script = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class PatientCall(ClinicScopedModel):
    """A call the hospital makes about one appointment: the reminder the day before, or the feedback call after.

    Planned automatically from the appointment (appointments.journey). Until calls are placed by the AI,
    the front desk makes them and records the outcome here.
    """

    class Kind(models.TextChoices):
        REMINDER = "reminder", "Reminder"
        FEEDBACK = "feedback", "Feedback"

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"   # waiting for due_at, or due now
        CALLING = "calling", "AI calling"      # the AI has dialled; its outcome comes back from the call
        DONE = "done", "Done"
        NO_ANSWER = "no_answer", "Not answered"  # every attempt went unanswered
        SKIPPED = "skipped", "Not needed"       # cancelled, rescheduled away, or already visited

    class Result(models.TextChoices):
        CONFIRMED = "confirmed", "Confirmed"
        CANCEL = "cancel", "Asked to cancel"
        RESCHEDULE = "reschedule", "Asked to reschedule"
        RATED = "rated", "Gave feedback"

    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name="patient_calls")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SCHEDULED)
    due_at = models.DateTimeField(db_index=True)
    # The appointment time this reminder was planned for; a reschedule plans it again.
    for_starts_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    result = models.CharField(max_length=16, choices=Result.choices, blank=True)
    rating = models.PositiveSmallIntegerField(null=True, blank=True)  # feedback, 1-5
    comment = models.TextField(blank=True)
    review_sent_at = models.DateTimeField(null=True, blank=True)
    # When staff dealt with what the call needed of them (an unanswered reminder, an unhappy patient).
    resolved_at = models.DateTimeField(null=True, blank=True)
    call_log = models.ForeignKey("ai_agent.CallLog", null=True, blank=True, on_delete=models.SET_NULL, related_name="patient_calls")
    room_name = models.CharField(max_length=100, blank=True)  # the AI's latest call, at the voice provider

    class Meta:
        ordering = ["due_at"]
        constraints = [models.UniqueConstraint(fields=["appointment", "kind"], name="uniq_patient_call_per_kind")]
        indexes = [models.Index(fields=["clinic", "status", "due_at"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} call for appointment {self.appointment_id}"
