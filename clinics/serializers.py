from __future__ import annotations

from rest_framework import serializers

from clinics.models import Clinic, ClinicConfiguration, ClinicHoliday


class ClinicSerializer(serializers.ModelSerializer):
    def validate_name(self, value):
        """A hospital's name is unique among hospitals; a centre's only within its own hospital."""
        value = value.strip()
        if self.instance is not None:
            parent_id, exclude = self.instance.parent_id, self.instance.pk
        else:
            clinic = getattr(self.context.get("request"), "clinic", None)
            parent_id, exclude = (clinic.root.id if clinic else None), None
        taken = Clinic.objects.filter(parent_id=parent_id, name__iexact=value)
        if exclude:
            taken = taken.exclude(pk=exclude)
        if taken.exists():
            raise serializers.ValidationError("A hospital with this name already exists." if parent_id is None else "This hospital already has a centre with this name.")
        return value

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
