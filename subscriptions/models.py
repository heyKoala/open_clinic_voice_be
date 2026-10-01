from __future__ import annotations

from django.db import models

from common.models import TrackedModel


class SubscriptionPlan(TrackedModel):
    class PlanType(models.TextChoices):
        TRIAL = "trial", "Trial"
        GROWTH = "growth", "Growth"
        ENTERPRISE = "enterprise", "Enterprise"

    name = models.CharField(max_length=64, choices=PlanType.choices, unique=True)
    max_admins = models.PositiveIntegerField(default=1)
    max_receptionists = models.PositiveIntegerField(default=2)
    max_doctors = models.PositiveIntegerField(default=3)
    feature_flags = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return self.get_name_display()

    @classmethod
    def get_default_plan(cls, plan_type: str = PlanType.TRIAL) -> SubscriptionPlan:
        defaults = {
            cls.PlanType.TRIAL: {"max_admins": 1, "max_receptionists": 2, "max_doctors": 3},
            cls.PlanType.GROWTH: {"max_admins": 1, "max_receptionists": 2, "max_doctors": 20},
            cls.PlanType.ENTERPRISE: {"max_admins": 999, "max_receptionists": 999, "max_doctors": 999},
        }
        plan_data = defaults.get(plan_type, defaults[cls.PlanType.TRIAL])
        plan, _ = cls.objects.get_or_create(
            name=plan_type,
            defaults=plan_data,
        )
        return plan


class ClinicEntitlement(TrackedModel):
    clinic = models.OneToOneField(
        "clinics.Clinic",
        on_delete=models.CASCADE,
        related_name="entitlement",
    )
    plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,
        related_name="clinic_entitlements",
    )
    max_admins_override = models.PositiveIntegerField(null=True, blank=True)
    max_receptionists_override = models.PositiveIntegerField(null=True, blank=True)
    max_doctors_override = models.PositiveIntegerField(null=True, blank=True)
    feature_flags_override = models.JSONField(default=dict, blank=True)

    def get_limit(self, role: str) -> int:
        if role == "clinic_admin":
            return self.max_admins_override if self.max_admins_override is not None else self.plan.max_admins
        elif role == "receptionist":
            return self.max_receptionists_override if self.max_receptionists_override is not None else self.plan.max_receptionists
        elif role == "doctor":
            return self.max_doctors_override if self.max_doctors_override is not None else self.plan.max_doctors
        return 0

    def get_feature_flags(self) -> dict:
        flags = dict(self.plan.feature_flags)
        flags.update(self.feature_flags_override)
        return flags

    def __str__(self) -> str:
        return f"Entitlement for {self.clinic.name} ({self.plan.name})"
