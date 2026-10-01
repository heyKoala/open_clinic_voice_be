from django.urls import reverse
from rest_framework import serializers

from ai_agent.models import AgentConfiguration, CallLog


class AgentConfigurationSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentConfiguration
        fields = [
            "id",
            "purpose",
            "language",
            "first_greeting",
            "system_prompt",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        clinic_id = self.context["request"].user.clinic_id
        instance = getattr(self, "instance", None)
        
        # Check uniqueness constraint for purpose + language per clinic
        queryset = AgentConfiguration.objects.filter(
            clinic_id=clinic_id,
            purpose=attrs.get("purpose", instance.purpose if instance else ""),
            language=attrs.get("language", instance.language if instance else ""),
        )
        if instance:
            queryset = queryset.exclude(id=instance.id)
        
        if queryset.exists():
            raise serializers.ValidationError({
                "purpose": "Agent with this purpose and language already exists for your clinic."
            })
        
        return attrs


class CallLogSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source="patient.full_name", read_only=True)
    recording_url = serializers.SerializerMethodField()

    class Meta:
        model = CallLog
        fields = [
            "id",
            "patient",
            "patient_name",
            "direction",
            "agent_name",
            "language",
            "duration_seconds",
            "outcome",
            "occurred_at",
            "transcript",
            "recording_url",
            "summary",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_recording_url(self, obj):
        # Prefer our stored copy: the provider's signed links expire about an hour after the call.
        if obj.recording_file:
            request = self.context.get("request")
            path = reverse("calllog-recording", kwargs={"pk": obj.pk})
            return request.build_absolute_uri(path) if request else path
        return obj.recording_url or None

    def validate(self, attrs):
        clinic_id = self.context["request"].user.clinic_id
        patient = attrs.get("patient") or getattr(self.instance, "patient", None)
        if patient and patient.clinic_id != clinic_id:
            raise serializers.ValidationError({"patient": "Must belong to your clinic."})
        return attrs
