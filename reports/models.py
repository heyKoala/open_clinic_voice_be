from __future__ import annotations

import os

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from accounts.models import User
from common.models import ClinicScopedModel


def validate_file_extension(value):
	allowed_extensions = {".pdf", ".csv", ".xlsx", ".xls", ".txt"}
	extension = os.path.splitext(value.name)[1].lower()
	if extension not in allowed_extensions:
		raise ValidationError("Unsupported file extension.")


def report_file_path(instance, filename):
	current_time = timezone.now()
	report_id = getattr(instance, "pk", None) or "pending"
	return f"reports/{instance.clinic_id}/{current_time.year}/{current_time.month:02d}/{report_id}_{filename}"


class ReportTemplate(ClinicScopedModel):
    """Template for generating reports."""

    class ReportType(models.TextChoices):
        PATIENT_SUMMARY = 'patient_summary', 'Patient Summary'
        APPOINTMENT_ANALYTICS = 'appointment_analytics', 'Appointment Analytics'
        CLINICAL_ENCOUNTERS = 'clinical_encounters', 'Clinical Encounters'
        FOLLOW_UP_STATUS = 'follow_up_status', 'Follow-up Status'
        QUEUE_PERFORMANCE = 'queue_performance', 'Queue Performance'
        REVENUE_REPORT = 'revenue_report', 'Revenue Report'
        CUSTOM = 'custom', 'Custom Report'

    class ReportFormat(models.TextChoices):
        PDF = 'pdf', 'PDF'
        CSV = 'csv', 'CSV'
        EXCEL = 'excel', 'Excel'
        TEXT = 'text', 'Plain Text'

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    report_type = models.CharField(
        max_length=30,
        choices=ReportType.choices,
        default=ReportType.CUSTOM
    )
    format = models.CharField(
        max_length=10,
        choices=ReportFormat.choices,
        default=ReportFormat.PDF
    )
    allowed_roles = models.JSONField(default=list, blank=True)
    configuration = models.JSONField(
        default=dict,
        help_text="JSON configuration for report generation (filters, columns, grouping, etc.)"
    )
    is_active = models.BooleanField(default=True)
    is_scheduled = models.BooleanField(
        default=False,
        help_text="Whether this report should be generated automatically on a schedule"
    )
    schedule_cron = models.CharField(
        max_length=100,
        blank=True,
        help_text="Cron expression for scheduled reports (e.g., '0 9 * * MON')"
    )
    retention_days = models.PositiveSmallIntegerField(
        default=7,
        help_text="How long report files and links remain available.",
    )

    class Meta:
        verbose_name = "Report Template"
        verbose_name_plural = "Report Templates"
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.get_report_type_display()})"

    def clean(self):
        if self.allowed_roles:
            invalid_roles = [role for role in self.allowed_roles if role not in User.Role.values]
            if invalid_roles:
                raise ValidationError({"allowed_roles": f"Invalid roles: {', '.join(invalid_roles)}"})

    def save(self, *args, **kwargs):
        if not self.allowed_roles:
            from .permissions import allowed_roles_for

            self.allowed_roles = allowed_roles_for(self.report_type)
        super().save(*args, **kwargs)

    def allows_role(self, role: str) -> bool:
        return role in (self.allowed_roles or [])

    @property
    def allowed_roles_display(self):
        return [User.Role(role).label for role in self.allowed_roles or [] if role in User.Role.values]


class ReportExecution(ClinicScopedModel):
    """Record of a report generation execution."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'
        EXPIRED = 'expired', 'Expired'

    template = models.ForeignKey(
        ReportTemplate,
        on_delete=models.CASCADE,
        related_name='executions'
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='requested_reports'
    )
    parameters = models.JSONField(
        default=dict,
        help_text="Parameters used for this specific report execution"
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    result_file = models.FileField(
        upload_to=report_file_path,
        validators=[validate_file_extension],
        null=True,
        blank=True,
        help_text="Generated report file"
    )
    file_size = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        help_text="Size of the generated file in bytes"
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    expires_at = models.DateTimeField(
        help_text="When this report file should be deleted"
    )
    download_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of times this report has been downloaded"
    )
    task_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="ID of the async task (e.g., Celery task ID)"
    )

    class Meta:
        verbose_name = "Report Execution"
        verbose_name_plural = "Report Executions"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.template.name} - {self.created_at.strftime('%Y-%m-%d %H:%M')} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        if not self.expires_at:
            retention_days = self.template.retention_days if self.template_id and self.template else 7
            self.expires_at = timezone.now() + timezone.timedelta(days=retention_days)
        super().save(*args, **kwargs)

    @property
    def is_expired(self):
        """Check if the report has expired"""
        return timezone.now() > self.expires_at

    @property
    def is_downloadable(self):
        """Check if the report can be downloaded."""
        return (
            self.status == self.Status.COMPLETED and
            self.result_file and
            not self.is_expired
        )


class ReportAccessToken(ClinicScopedModel):
    class Purpose(models.TextChoices):
        DOWNLOAD = "download", "Download"

    execution = models.ForeignKey(ReportExecution, on_delete=models.CASCADE, related_name="access_tokens")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="report_access_tokens")
    purpose = models.CharField(max_length=32, choices=Purpose.choices, default=Purpose.DOWNLOAD)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["clinic", "execution", "purpose"]),
            models.Index(fields=["expires_at"]),
        ]

    @property
    def is_usable(self) -> bool:
        return self.used_at is None and self.expires_at >= timezone.now()