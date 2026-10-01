from rest_framework import serializers

from followups.models import Campaign, FollowUp


class CampaignSerializer(serializers.ModelSerializer):
    class Meta:
        model = Campaign
        fields = [
            "id",
            "name",
            "description",
            "status",
            "start_date",
            "end_date",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        if attrs.get("end_date") and attrs.get("start_date"):
            if attrs["end_date"] < attrs["start_date"]:
                raise serializers.ValidationError({"end_date": "Must be after start_date."})
        return attrs


class FollowUpSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source="patient.full_name", read_only=True)
    doctor_name = serializers.CharField(source="doctor.full_name", read_only=True)

    class Meta:
        model = FollowUp
        fields = [
            "id",
            "campaign",
            "patient",
            "patient_name",
            "doctor",
            "doctor_name",
            "appointment",
            "scheduled_for",
            "method",
            "status",
            "notes",
            "outcome",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        clinic_id = self.context["request"].user.clinic_id
        for field in ("patient", "doctor", "appointment", "campaign"):
            value = attrs.get(field) or getattr(self.instance, field, None)
            if value and hasattr(value, "clinic_id") and value.clinic_id != clinic_id:
                raise serializers.ValidationError({field: "Must belong to your clinic."})
        return attrs
