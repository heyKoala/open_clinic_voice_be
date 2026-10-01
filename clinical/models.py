from django.db import models
from django.utils import timezone
from django.conf import settings

from common.models import ClinicScopedModel
from ai_agent.models import CallLog


class ClinicalEncounter(ClinicScopedModel):
    class EncounterStatus(models.TextChoices):
        SCHEDULED = 'scheduled', 'Scheduled'
        IN_PROGRESS = 'in_progress', 'In Progress'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'

    patient = models.ForeignKey('patients.Patient', on_delete=models.CASCADE)
    doctor = models.ForeignKey('doctors.Doctor', on_delete=models.CASCADE)
    encounter_date = models.DateTimeField(default=timezone.now)
    appointment = models.OneToOneField(
        'appointments.Appointment', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='clinical_encounter'
    )
    queue_token = models.OneToOneField(
        'queue_mgmt.QueueToken', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='clinical_encounter'
    )
    status = models.CharField(
        max_length=20, choices=EncounterStatus.choices,
        default=EncounterStatus.SCHEDULED
    )
    chief_complaint = models.TextField(blank=True)

    class Meta:
        ordering = ['-encounter_date']
        verbose_name = 'Clinical Encounter'
        verbose_name_plural = 'Clinical Encounters'

    def __str__(self):
        return f"Encounter for {self.patient.full_name} with Dr. {self.doctor.full_name} on {self.encounter_date}"


class SymptomSummary(ClinicScopedModel):
    class SummaryStatus(models.TextChoices):
        PENDING = 'pending', 'Pending Review'
        APPROVED = 'approved', 'Approved'
        EDITED = 'edited', 'Edited'
        REJECTED = 'rejected', 'Rejected'

    encounter = models.OneToOneField(
        'clinical.ClinicalEncounter', on_delete=models.CASCADE,
        related_name='symptom_summary'
    )
    call_log = models.ForeignKey(
        CallLog, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='symptom_summary'
    )
    summary_text = models.TextField(
        help_text="Structured clinical summary derived from the call"
    )
    ai_model = models.CharField(
        max_length=100, help_text="AI model used (e.g., gpt-4)"
    )
    ai_version = models.CharField(
        max_length=50, help_text="Version of the AI model"
    )
    confidence = models.FloatField(
        help_text="Confidence score of the AI summary (0.0 to 1.0)"
    )
    status = models.CharField(
        max_length=20, choices=SummaryStatus.choices,
        default=SummaryStatus.PENDING
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='reviewed_symptom_summaries'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Symptom Summary'
        verbose_name_plural = 'Symptom Summaries'

    def __str__(self):
        return f"Symptom summary for encounter {self.encounter.id}"


class ClinicalNote(ClinicScopedModel):
    class NoteType(models.TextChoices):
        PROGRESS = 'progress', 'Progress Note'
        CONSULTATION = 'consultation', 'Consultation Note'
        DISCHARGE = 'discharge', 'Discharge Summary'
        PROCEDURE = 'procedure', 'Procedure Note'

    encounter = models.ForeignKey(
        'clinical.ClinicalEncounter', on_delete=models.CASCADE,
        related_name='clinical_notes'
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        # PROTECT, not CASCADE or SET_NULL:
        # - CASCADE would silently destroy all notes if a user were hard-deleted (wrong).
        # - SET_NULL would erase authorship provenance (DPDP violation, see permission_matrix §9).
        # - PROTECT forces an explicit error if code ever attempts to hard-delete a user,
        #   acting as a safety net. Deactivation uses is_active=False + membership_status=archived,
        #   never a hard delete, so this constraint is never hit in normal operation.
        on_delete=models.PROTECT,
        related_name="clinical_notes"
    )
    note_type = models.CharField(
        max_length=20, choices=NoteType.choices,
        default=NoteType.PROGRESS
    )
    content = models.TextField()

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Clinical Note'
        verbose_name_plural = 'Clinical Notes'

    def __str__(self):
        return f"{self.note_type} by {self.author.get_full_name()} for encounter {self.encounter.id}"


class PatientHealthRecord(ClinicScopedModel):
    class RecordCategory(models.TextChoices):
        ALLERGY = 'allergy', 'Allergy'
        MEDICATION = 'medication', 'Medication'
        CONDITION = 'condition', 'Medical Condition'
        PROCEDURE = 'procedure', 'Procedure'
        IMMUNIZATION = 'immunization', 'Immunization'
        VITAL = 'vital', 'Vital Signs'
        LAB_RESULT = 'lab_result', 'Laboratory Result'

    patient = models.ForeignKey(
        'patients.Patient', on_delete=models.CASCADE,
        related_name='health_records'
    )
    category = models.CharField(
        max_length=20, choices=RecordCategory.choices
    )
    name = models.CharField(max_length=200)
    details = models.TextField(blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='recorded_health_records'
    )
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-recorded_at']
        verbose_name = 'Patient Health Record'
        verbose_name_plural = 'Patient Health Records'

    def __str__(self):
        return f"{self.get_category_display()}: {self.name} for {self.patient.first_name}"


class ClinicalAttachment(ClinicScopedModel):
    encounter = models.ForeignKey(
        'clinical.ClinicalEncounter', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='attachments'
    )
    patient = models.ForeignKey(
        'patients.Patient', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='attachments'
    )
    file = models.FileField(upload_to='clinical_attachments/')
    filename = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField(help_text="File size in bytes")
    mime_type = models.CharField(max_length=100)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='uploaded_attachments'
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Clinical Attachment'
        verbose_name_plural = 'Clinical Attachments'

    def __str__(self):
        return f"Attachment: {self.filename}"