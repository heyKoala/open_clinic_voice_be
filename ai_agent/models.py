from django.db import models

from common.models import ClinicScopedModel


class AgentConfiguration(ClinicScopedModel):
    purpose = models.CharField(max_length=64)
    language = models.CharField(max_length=32, default="en")
    first_greeting = models.TextField()
    system_prompt = models.TextField()
    is_active = models.BooleanField(default=True)
    class Meta: constraints=[models.UniqueConstraint(fields=["clinic","purpose","language"], name="uniq_agent_purpose_language")]

class CallLog(ClinicScopedModel):
    class Direction(models.TextChoices): INBOUND="inbound", "Inbound"; OUTBOUND="outbound", "Outbound"
    patient = models.ForeignKey("patients.Patient", null=True, blank=True, on_delete=models.SET_NULL, related_name="call_logs")
    direction = models.CharField(max_length=16, choices=Direction.choices)
    agent_name = models.CharField(max_length=100, blank=True)
    language = models.CharField(max_length=32, default="en")
    duration_seconds = models.PositiveIntegerField(default=0)
    outcome = models.CharField(max_length=100, blank=True)
    occurred_at = models.DateTimeField(db_index=True)
    transcript = models.TextField(blank=True)
    recording_url = models.URLField(blank=True, max_length=1000)
    # Copy of the provider's recording: their signed links expire after about an hour.
    recording_file = models.FileField(upload_to="call_recordings/%Y/%m/", blank=True)
    summary = models.TextField(blank=True)
    # Provider call id, so repeated/retried webhook events update one log instead of duplicating it.
    external_call_id = models.CharField(max_length=100, blank=True, db_index=True)
    class Meta:
        ordering=["-occurred_at"]
        constraints=[models.UniqueConstraint(
            fields=["clinic", "external_call_id"], condition=~models.Q(external_call_id=""), name="uniq_calllog_external_call",
        )]
