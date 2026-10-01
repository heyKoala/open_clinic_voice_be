from rest_framework import serializers
from django.core.validators import RegexValidator

from patients.models import Patient


phone_validator = RegexValidator(
    regex=r'^\+?[\d\s-]{10,20}$',
    message="Phone number must be 10-20 digits and can include +, spaces, and hyphens."
)


class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = ["id","full_name","date_of_birth","gender","blood_group","phone","email","emergency_contact_name","emergency_contact_phone","preferred_language",#"abha_number",
        "notes","is_active","created_at","updated_at"]
        read_only_fields = ["id","created_at","updated_at"]

    def validate_phone(self, value):
        phone_validator(value)
        return value

    def validate_email(self, value):
        if value and not value.strip():
            raise serializers.ValidationError("Email cannot be blank if provided.")
        return value

    def validate(self, attrs):
        # Check for duplicate phone in the same clinic
        phone = attrs.get("phone")
        instance = getattr(self, "instance", None)
        clinic_id = self.context["request"].user.clinic_id
        
        if phone:
            queryset = Patient.objects.filter(clinic_id=clinic_id, phone=phone)
            if instance:
                queryset = queryset.exclude(id=instance.id)
            if queryset.exists():
                raise serializers.ValidationError({"phone": "A patient with this phone number already exists in your clinic."})
        
        return attrs
