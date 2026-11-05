from django.contrib import admin

from leads.models import Lead


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("created_at", "name", "organisation", "phone", "email", "source", "notified")
    list_filter = ("source", "notified")
    search_fields = ("name", "email", "phone", "organisation")
    readonly_fields = ("created_at",)
