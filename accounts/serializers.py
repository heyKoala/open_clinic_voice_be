import re

from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from accounts.models import Invitation, User


def _enforce_strong_password(value: str):
    """Require at least one uppercase letter, one digit, and one special character."""
    validate_password(value)  # runs Django's built-in checks first (length etc.)
    if not re.search(r'[A-Z]', value):
        raise serializers.ValidationError('Password must contain at least one uppercase letter (A–Z).')
    if not re.search(r'[0-9]', value):
        raise serializers.ValidationError('Password must contain at least one number (0–9).')
    if not re.search(r'[^A-Za-z0-9]', value):
        raise serializers.ValidationError('Password must contain at least one special character (e.g. !@#$%).')


class SignupSerializer(serializers.Serializer):
    clinic_name = serializers.CharField(max_length=255)
    full_name = serializers.CharField(max_length=255)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=10)
    is_also_doctor = serializers.BooleanField(required=False, default=False)

    def validate_password(self, value):
        _enforce_strong_password(value)
        return value


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class VerifyEmailSerializer(serializers.Serializer):
    token = serializers.CharField()


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ResetPasswordSerializer(serializers.Serializer):
    token = serializers.CharField()
    password = serializers.CharField(write_only=True, min_length=10)

    def validate_password(self, value):
        _enforce_strong_password(value)
        return value


class InviteCreateSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=[User.Role.DOCTOR, User.Role.RECEPTIONIST])


class InvitePreviewSerializer(serializers.ModelSerializer):
    clinic_name = serializers.CharField(source="clinic.name")

    class Meta:
        model = Invitation
        fields = ("clinic_name", "email", "role", "expires_at")


class InviteAcceptSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=255)
    password = serializers.CharField(write_only=True, min_length=10)

    def validate_password(self, value):
        _enforce_strong_password(value)
        return value


class UserUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("full_name", "age", "gender")

    def update(self, instance, validated_data):
        # Save only the edited fields: request.user carries the in-memory active clinic,
        # which must not be persisted as the user's primary clinic.
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save(update_fields=[*validated_data, "updated_at"])
        return instance


class UserMeSerializer(serializers.ModelSerializer):
    clinic_name = serializers.SerializerMethodField()
    doctor_profile_id = serializers.SerializerMethodField()
    clinic_plan = serializers.SerializerMethodField()
    clinic_is_onboarded = serializers.SerializerMethodField()
    is_doctor = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("id", "email", "full_name", "role", "membership_status", "is_clinic_admin", "is_doctor", "is_active", "is_verified", "clinic", "clinic_name", "doctor_profile_id", "clinic_plan", "clinic_is_onboarded", "age", "gender")

    def get_clinic_plan(self, obj):
        if obj.clinic_id:
            from subscriptions.models import ClinicEntitlement
            entitlement = ClinicEntitlement.objects.filter(clinic_id=obj.clinic_id).first()
            if entitlement:
                return entitlement.plan.name
        return "trial"

    def get_clinic_name(self, obj):
        request = self.context.get('request')
        if request and getattr(request, 'clinic', None):
            return request.clinic.name
        return obj.clinic.name if obj.clinic else None

    def get_clinic_is_onboarded(self, obj):
        request = self.context.get('request')
        if request and getattr(request, 'clinic', None):
            return request.clinic.is_onboarded
        return obj.clinic.is_onboarded if obj.clinic else False

    def get_doctor_profile_id(self, obj):
        request = self.context.get('request')
        clinic = request.clinic if request and getattr(request, 'clinic', None) else obj.clinic
        
        from doctors.models import Doctor
        doc = Doctor.objects.filter(user=obj, clinic=clinic).first()
        return doc.id if doc else None

    def get_is_doctor(self, obj):
        request = self.context.get('request')
        clinic = request.clinic if request and getattr(request, 'clinic', None) else obj.clinic
        
        from doctors.models import Doctor
        return Doctor.objects.filter(user=obj, clinic=clinic, is_active=True).exists()

