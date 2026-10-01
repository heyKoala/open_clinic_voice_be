from __future__ import annotations

from rest_framework import serializers

from clinics.models import Clinic, ClinicConfiguration, ClinicHoliday


class ClinicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Clinic
        fields = (
            "id",
            "name",
            "address",
            "clinic_type",
            "subscription_status",
            "trial_ends_at",
            "phone",
            "support_email",
            "website",
            "registration_number",
            "tax_id",
            "description",
            "facilities",
            "holiday_calendar",
            "parent",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "subscription_status", "trial_ends_at", "parent", "created_at", "updated_at")


class ClinicConfigurationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClinicConfiguration
        fields = [
            "id",
            "clinic",
            "ai_enabled",
            "ai_default_language",
            "ai_voice_enabled",
            "ai_transcription_enabled",
            "ai_voice_type",
            "default_consultation_minutes",
            "timezone",
            "working_hours_start",
            "working_hours_end",
            "sms_enabled",
            "email_enabled",
            "whatsapp_enabled",
            "auto_generate_tokens",
            "token_prefix",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "clinic", "created_at", "updated_at"]

    def validate(self, attrs):
        if attrs.get("working_hours_start") and attrs.get("working_hours_end"):
            if attrs["working_hours_end"] <= attrs["working_hours_start"]:
                raise serializers.ValidationError({
                    "working_hours_end": "Must be after working_hours_start."
                })
        return attrs

class ClinicHolidaySerializer(serializers.ModelSerializer):
    class Meta:
        model = ClinicHoliday
        fields = ["id", "clinic", "date", "reason", "created_at"]
        read_only_fields = ["id", "clinic", "created_at"]
