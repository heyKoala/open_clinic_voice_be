from __future__ import annotations

from rest_framework import serializers

from clinics.models import Clinic
from platform_admin.models import PlatformDefaults
from subscriptions.models import SubscriptionPlan

PROFILE_TEXT = ("address", "phone", "registration_number", "tax_id", "description", "facilities")


def _unique_hospital_name(name: str, exclude_id: int | None = None) -> str:
    name = name.strip()
    taken = Clinic.objects.filter(parent__isnull=True, name__iexact=name)
    if exclude_id:
        taken = taken.exclude(pk=exclude_id)
    if taken.exists():
        raise serializers.ValidationError("A hospital with this name already exists.")
    return name


class HospitalUpdateSerializer(serializers.Serializer):
    """Everything a platform admin can change on a hospital's own record. Every field is optional."""

    name = serializers.CharField(max_length=255, required=False)
    status = serializers.ChoiceField(choices=Clinic.Status.choices, required=False)
    address = serializers.CharField(allow_blank=True, required=False)
    phone = serializers.CharField(max_length=32, allow_blank=True, required=False)
    support_email = serializers.EmailField(allow_blank=True, required=False)
    website = serializers.URLField(allow_blank=True, required=False)
    registration_number = serializers.CharField(max_length=120, allow_blank=True, required=False)
    tax_id = serializers.CharField(max_length=120, allow_blank=True, required=False)
    description = serializers.CharField(allow_blank=True, required=False)
    facilities = serializers.CharField(allow_blank=True, required=False)
    timezone = serializers.CharField(max_length=64, required=False)
    working_hours_start = serializers.TimeField(required=False)
    working_hours_end = serializers.TimeField(required=False)

    def validate_name(self, value):
        return _unique_hospital_name(value, exclude_id=getattr(self.instance, "pk", None))

    def validate_timezone(self, value):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise serializers.ValidationError("Enter a valid time zone, e.g. Asia/Kolkata.")
        return value

    def validate_status(self, value):
        current = self.instance.status if self.instance else Clinic.Status.ACTIVE
        allowed = {
            Clinic.Status.ACTIVE: {Clinic.Status.SUSPENDED},
            Clinic.Status.SUSPENDED: {Clinic.Status.ACTIVE, Clinic.Status.ARCHIVED},
            Clinic.Status.ARCHIVED: {Clinic.Status.SUSPENDED},
        }[current]
        if value != current and value not in allowed:
            raise serializers.ValidationError(f"A {current} hospital can't go straight to {value}.")
        return value

    def validate(self, attrs):
        start = attrs.get("working_hours_start")
        end = attrs.get("working_hours_end")
        if start and end and start >= end:
            raise serializers.ValidationError({"working_hours_end": "Closing time must be after opening time."})
        return attrs


class CreditChangeSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=["add", "set"])
    amount = serializers.IntegerField(min_value=0, max_value=100_000_000)
    note = serializers.CharField(max_length=200, allow_blank=True, required=False, default="")

    def validate(self, attrs):
        if attrs["mode"] == "add" and attrs["amount"] == 0:
            raise serializers.ValidationError({"amount": "Enter more than 0 credits to add."})
        return attrs


class PlanChangeSerializer(serializers.Serializer):
    plan = serializers.ChoiceField(choices=SubscriptionPlan.PlanType.choices, required=False)
    credits_per_minute = serializers.IntegerField(min_value=1, max_value=100_000, required=False)
    low_balance_threshold = serializers.IntegerField(min_value=0, max_value=100_000_000, required=False)
    max_admins = serializers.IntegerField(min_value=0, max_value=999, required=False)
    max_doctors = serializers.IntegerField(min_value=0, max_value=999, required=False)
    max_receptionists = serializers.IntegerField(min_value=0, max_value=999, required=False)


class DefaultsSerializer(serializers.ModelSerializer):
    plan = serializers.ChoiceField(choices=SubscriptionPlan.PlanType.choices)
    credits_per_minute = serializers.IntegerField(min_value=1, max_value=100_000)

    class Meta:
        model = PlatformDefaults
        fields = ("plan", "credits_per_minute", "opening_credits", "low_balance_threshold", "max_admins", "max_doctors", "max_receptionists")
