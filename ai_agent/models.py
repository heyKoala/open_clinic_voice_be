from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
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
    class Sentiment(models.TextChoices): POSITIVE="positive", "Positive"; NEUTRAL="neutral", "Neutral"; NEGATIVE="negative", "Negative"
    patient = models.ForeignKey("patients.Patient", null=True, blank=True, on_delete=models.SET_NULL, related_name="call_logs")
    # The number the call was with: the caller's for inbound, the patient's for outbound ("+91…", "" if unknown).
    # Kept even when no patient record matches, so the front desk can see who called and call back.
    phone = models.CharField(max_length=20, blank=True, db_index=True)
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
    # How the caller felt: from the provider's call analysis, or rated from the transcript (ai_agent.sentiment).
    # Blank until known.
    sentiment = models.CharField(max_length=10, choices=Sentiment.choices, blank=True, db_index=True)
    # Provider call id, so repeated/retried webhook events update one log instead of duplicating it.
    external_call_id = models.CharField(max_length=100, blank=True, db_index=True)
    # Started from the app's "Test call" (staff trying the agent), not by a patient: labelled in the
    # call logs and left out of the call analytics.
    is_test = models.BooleanField(default=False, db_index=True)
    # The appointment the agent booked or cancelled in this call (read from its tool results): links the
    # call to that appointment's doctor, for the doctor filter in Reports.
    appointment = models.ForeignKey(
        "appointments.Appointment", null=True, blank=True, on_delete=models.SET_NULL, related_name="call_logs",
    )
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

    # Call audio and listening (see agent_settings.CALL_DEFAULTS / LISTENING_PRESETS).
    class Listening(models.TextChoices):
        PATIENT = "patient", "Patient"      # waits longer before answering; for callers who pause mid-sentence
        BALANCED = "balanced", "Balanced"
        QUICK = "quick", "Quick"            # answers fast; may cut in on slow speakers

    # The voice worker's denoiser on the caller's audio (per call, and on the hospital number's inbound rule).
    noise_cancellation = models.BooleanField(default=True)
    # Maximum attenuation in dB; null = the worker's default (about 30).
    noise_cancellation_level = models.PositiveSmallIntegerField(null=True, blank=True)
    # The worker's fixed "this call may be recorded" clip before the greeting.
    recording_disclosure = models.BooleanField(default=False)
    listening = models.CharField(max_length=16, choices=Listening.choices, default=Listening.BALANCED)
    # Hang up after this long without anyone speaking.
    max_silence_seconds = models.PositiveSmallIntegerField(default=60)
    # A soft sound while the receptionist is working out its reply.
    thinking_sound = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"Voice agent settings ({self.clinic.name})"


class KnowledgeDocument(ClinicScopedModel):
    """One thing the AI receptionist can look up on a call: an uploaded file, a web page, or pasted text.

    The hospital's knowledge base is simply all of its documents (one knowledge base per main hospital,
    shared by its centres). On upload the text is extracted and split into KnowledgeChunks, which the
    query tool searches (ai_agent.knowledge).
    """

    class Type(models.TextChoices):
        FILE = "file", "File"
        URL = "url", "Web page"
        TEXT = "text", "Text"

    class Status(models.TextChoices):
        PENDING = "pending", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    name = models.CharField(max_length=255)
    type = models.CharField(max_length=10, choices=Type.choices)
    file = models.FileField(upload_to="knowledge/%Y/%m/", blank=True)
    url = models.URLField(blank=True)
    text = models.TextField(blank=True)  # pasted text, or the text extracted from the file / page
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    error = models.CharField(max_length=500, blank=True)
    chunk_count = models.PositiveIntegerField(default=0)
    # Whether the AI may use it on calls (switch a document off without deleting it).
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name


class KnowledgeChunk(models.Model):
    """A searchable piece of a KnowledgeDocument (Postgres full-text search)."""

    document = models.ForeignKey(KnowledgeDocument, on_delete=models.CASCADE, related_name="chunks")
    position = models.PositiveIntegerField()
    text = models.TextField()
    search = SearchVectorField(null=True)

    class Meta:
        ordering = ["document_id", "position"]
        indexes = [GinIndex(fields=["search"], name="knowledgechunk_search_gin")]
