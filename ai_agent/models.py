from django.db import models

from common.models import ClinicScopedModel, TrackedModel


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


class VoiceAgentSettings(TrackedModel):
    """How the AI receptionist sounds and thinks. Owned by a main clinic and shared by its centres
    (they share one phone number). No row means the built-in defaults."""

    class ModelType(models.TextChoices):
        # Speech-to-text -> LLM -> text-to-speech, each chosen separately.
        STANDARD = "standard", "Standard"
        # One speech-to-speech model.
        REALTIME = "realtime", "Realtime"

    clinic = models.OneToOneField("clinics.Clinic", on_delete=models.CASCADE, related_name="voice_agent")
    # Blank: use the built-in greeting / instructions.
    first_message = models.TextField(blank=True)
    system_prompt = models.TextField(blank=True)
    model_type = models.CharField(max_length=16, choices=ModelType.choices, default=ModelType.STANDARD)
    # Each stage: {"provider", "model", "language", "voice", ..., "config": {...}}, where "config" is
    # the same choice under the provider's own key names (what the voice provider is sent).
    stt = models.JSONField(default=dict, blank=True)
    llm = models.JSONField(default=dict, blank=True)
    tts = models.JSONField(default=dict, blank=True)
    realtime = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"Voice agent settings ({self.clinic.name})"
