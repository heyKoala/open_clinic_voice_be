from rest_framework import serializers

from queue_mgmt.models import QueueToken


class QueueTokenSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source='patient.full_name', read_only=True)

    class Meta:
        model = QueueToken
        fields = ["id","appointment","patient","patient_name","doctor","service_date","token_number","status","checked_in_at","served_at","notes","is_active","created_at"]
        read_only_fields = ["id","token_number","status","created_at","is_active"]

    def validate(self, attrs):
        request = self.context.get("request")
        clinic_id = request.user.clinic_id if request else None
        for field in ("patient", "doctor", "appointment"):
            value = attrs.get(field)
            if value is not None and value.clinic_id != clinic_id:
                raise serializers.ValidationError({field: "Must belong to your clinic."})
        return attrs
