from rest_framework import serializers

from accounts.models import User

from .models import ReportAccessToken, ReportExecution, ReportTemplate
from .permissions import allowed_roles_for
from django.contrib.auth import get_user_model

User = get_user_model()


class ReportTemplateSerializer(serializers.ModelSerializer):
    """Serializer for ReportTemplate model."""
    report_type_display = serializers.CharField(source='get_report_type_display', read_only=True)
    format_display = serializers.CharField(source='get_format_display', read_only=True)
    allowed_roles_display = serializers.SerializerMethodField()
    access_summary = serializers.SerializerMethodField()

    class Meta:
        model = ReportTemplate
        fields = [
            'id', 'name', 'description', 'report_type', 'report_type_display',
            'format', 'format_display', 'configuration', 'is_active',
            'allowed_roles', 'allowed_roles_display', 'is_scheduled', 'schedule_cron',
            'retention_days', 'access_summary',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_allowed_roles_display(self, obj):
        return [User.Role(role).label for role in obj.allowed_roles or [] if role in User.Role.values]

    def get_access_summary(self, obj):
        return {
            "allowed_roles": obj.allowed_roles or allowed_roles_for(obj.report_type),
            "retention_days": obj.retention_days,
        }


class ReportExecutionSerializer(serializers.ModelSerializer):
    """Serializer for ReportExecution model."""
    template_name = serializers.CharField(source='template.name', read_only=True)
    template_format = serializers.CharField(source='template.format', read_only=True)
    requested_by_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    format_display = serializers.CharField(source='template.get_format_display', read_only=True)
    report_type_display = serializers.CharField(source='template.get_report_type_display', read_only=True)
    file_size_mb = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()
    is_downloadable = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()
    signed_download_url = serializers.SerializerMethodField()

    class Meta:
        model = ReportExecution
        fields = [
            'id', 'template', 'template_name', 'report_type_display',
            'template_format', 'format_display', 'requested_by', 'requested_by_name', 'parameters', 'status',
            'status_display', 'result_file', 'file_size', 'file_size_mb',
            'started_at', 'completed_at', 'error_message', 'expires_at',
            'is_expired', 'is_downloadable', 'download_count', 'download_url', 'signed_download_url',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'template', 'template_name', 'requested_by', 'requested_by_name',
            'template_format', 'format_display', 'status', 'status_display', 'result_file', 'file_size', 'file_size_mb',
            'started_at', 'completed_at', 'error_message', 'expires_at',
            'is_expired', 'is_downloadable', 'download_count', 'download_url', 'signed_download_url',
            'created_at', 'updated_at'
        ]

    def get_requested_by_name(self, obj):
        if obj.requested_by:
            return getattr(obj.requested_by, 'full_name', '') or getattr(obj.requested_by, 'email', '')
        return None

    def get_file_size_mb(self, obj):
        if obj.file_size:
            return round(obj.file_size / (1024 * 1024), 2)
        return None

    def get_is_expired(self, obj):
        return obj.is_expired

    def get_is_downloadable(self, obj):
        return obj.is_downloadable

    def get_download_url(self, obj):
        if obj.is_downloadable:
            return f"/api/v1/reports/executions/{obj.id}/download/"
        return None

    def get_signed_download_url(self, obj):
        if obj.is_downloadable:
            return f"/api/v1/reports/executions/{obj.id}/signed-download/"
        return None


class ReportExecutionCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating report executions."""

    class Meta:
        model = ReportExecution
        fields = ['template', 'parameters']

    def validate_template(self, value):
        """Ensure the template is active, accessible, and belongs to the same clinic."""
        request = self.context.get('request')
        if request and hasattr(request.user, 'clinic'):
            if value.clinic_id != request.clinic.id:
                raise serializers.ValidationError("Template does not belong to your clinic.")
            if not value.is_active:
                raise serializers.ValidationError("Template is not active.")
            if not value.allows_role(request.user.role):
                raise serializers.ValidationError("Your role cannot request this report.")
        return value


class ReportDownloadTokenSerializer(serializers.Serializer):
    download_url = serializers.CharField()
    expires_at = serializers.DateTimeField()