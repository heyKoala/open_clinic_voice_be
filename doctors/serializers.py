from rest_framework import serializers

from doctors.models import Doctor


class DoctorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Doctor
        fields = ["id","user","full_name","degree","specialty","consultation_minutes","max_patients_per_day","available_from","available_to","lunch_from","lunch_to","working_days","receptionists","is_active","created_at","updated_at"]
        # The front desk mapping is admin-only: it is written through the Access page (accounts.UserDetailView).
        read_only_fields = ["id","receptionists","created_at","updated_at"]

    def validate(self, attrs):
        available_from = attrs.get('available_from') or getattr(self.instance, 'available_from', None)
        available_to = attrs.get('available_to') or getattr(self.instance, 'available_to', None)
        
        if available_from and available_to and available_from >= available_to:
            raise serializers.ValidationError({"available_from": "available_from must be before available_to."})
        return attrs
