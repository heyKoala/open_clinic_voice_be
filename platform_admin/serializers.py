from __future__ import annotations

from rest_framework import serializers

from django.core.exceptions import ValidationError as DjangoValidationError

from accounts.models import User
from accounts.serializers import _enforce_strong_password
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


class NewAdminSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=255)
    email = serializers.EmailField()
    is_also_doctor = serializers.BooleanField(required=False, default=False)
    # invite: they get an email and choose their own password. password: set here and handed over by the platform admin.
    mode = serializers.ChoiceField(choices=["invite", "password"], default="invite")
    password = serializers.CharField(required=False, allow_blank=True, write_only=True, trim_whitespace=False)
    must_change_password = serializers.BooleanField(required=False, default=True)


class NewCommercialsSerializer(serializers.Serializer):
    plan = serializers.ChoiceField(choices=SubscriptionPlan.PlanType.choices)
    credits_per_minute = serializers.IntegerField(min_value=1, max_value=100_000)
    opening_credits = serializers.IntegerField(min_value=0, max_value=100_000_000)
    low_balance_threshold = serializers.IntegerField(min_value=0, max_value=100_000_000)


class NewHospitalSerializer(serializers.Serializer):
    """The create-hospital form. Problems come back under the names the form uses: ``name``, ``admin`` (the email), ``password``."""

    name = serializers.CharField(max_length=255)
    address = serializers.CharField(allow_blank=True, required=False, default="")
    phone = serializers.CharField(max_length=32, allow_blank=True, required=False, default="")
    support_email = serializers.EmailField(allow_blank=True, required=False, default="")
    admin = NewAdminSerializer()
    commercials = NewCommercialsSerializer(required=False)

    def validate_name(self, value):
        return _unique_hospital_name(value)

    def validate(self, attrs):
        admin = attrs["admin"]
        admin["email"] = admin["email"].strip().lower()
        errors = {}
        if User.objects.filter(email__iexact=admin["email"]).exists():
            errors["admin"] = ["This email already has an account."]
        if admin["mode"] == "password":
            password = admin.get("password") or ""
            if not password:
                errors["password"] = ["Set a password for the admin."]
            else:
                try:
                    _enforce_strong_password(password)
                except (serializers.ValidationError, DjangoValidationError) as exc:
                    errors["password"] = [str(m) for m in (exc.detail if hasattr(exc, "detail") else exc.messages)]
        else:
            admin["password"] = ""
            admin["must_change_password"] = False  # they choose their own password when they accept
        if errors:
            raise serializers.ValidationError(errors)

        if "commercials" not in attrs:  # fall back to the platform's defaults
            defaults = PlatformDefaults.load()
            attrs["commercials"] = {
                "plan": defaults.plan, "credits_per_minute": defaults.credits_per_minute,
                "opening_credits": defaults.opening_credits, "low_balance_threshold": defaults.low_balance_threshold,
            }
        return attrs
