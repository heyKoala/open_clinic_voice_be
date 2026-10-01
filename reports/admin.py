from django.contrib import admin
from .models import ReportAccessToken, ReportExecution, ReportTemplate

@admin.register(ReportTemplate)
class ReportTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'report_type', 'format', 'is_active', 'is_scheduled', 'retention_days', 'created_at')
    list_filter = ('report_type', 'format', 'is_active', 'is_scheduled', 'created_at')
    search_fields = ('name', 'description')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ReportExecution)
class ReportExecutionAdmin(admin.ModelAdmin):
    list_display = ('template', 'status', 'requested_by', 'download_count', 'expires_at', 'created_at', 'completed_at')
    list_filter = ('status', 'template__report_type', 'created_at', 'completed_at')
    search_fields = ('template__name', 'requested_by__email', 'requested_by__full_name')
    readonly_fields = ('template', 'requested_by', 'parameters', 'status', 'result_file', 'file_size',
                      'started_at', 'completed_at', 'error_message', 'expires_at', 'download_count',
                      'created_at', 'updated_at')


@admin.register(ReportAccessToken)
class ReportAccessTokenAdmin(admin.ModelAdmin):
    list_display = ('execution', 'user', 'purpose', 'expires_at', 'used_at', 'created_at')
    list_filter = ('purpose', 'expires_at', 'used_at')
    search_fields = ('execution__template__name', 'user__email')
    readonly_fields = ('execution', 'user', 'purpose', 'token_hash', 'expires_at', 'used_at', 'created_at', 'updated_at')