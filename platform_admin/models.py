"""HeyKoala's own admin layer: the audit trail of what the platform admin changed, and each hospital's credits."""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import ProtectedError

from common.models import TrackedModel


class PlatformAuditLog(models.Model):
    """What a platform admin did, to which hospital. Written by ``platform_admin.audit.log_platform_action`` only."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # Copied at the time, so the history still reads right if the account or the hospital is renamed.
    actor_email = models.CharField(max_length=254, blank=True)
    action = models.CharField(max_length=80)
    clinic = models.ForeignKey("clinics.Clinic", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    clinic_name = models.CharField(max_length=255, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["clinic", "-created_at"]), models.Index(fields=["action"])]

    def __str__(self) -> str:
        return f"{self.actor_email or 'system'}: {self.action} {self.clinic_name}".strip()


class CreditAccount(TrackedModel):
    """A hospital's prepaid call credits. One per main clinic, shared by its centres.

    ``balance`` is a cached sum of the ledger, kept in step by ``platform_admin.credits`` under a row lock; it can go
    negative (calls are still answered when the credits run out).
    """

    clinic = models.OneToOneField("clinics.Clinic", on_delete=models.PROTECT, related_name="credit_account")
    balance = models.BigIntegerField(default=0)
    low_balance_threshold = models.PositiveIntegerField(default=500)
    # Off by default: the AI keeps answering at zero and the platform admin is alerted instead.
    hard_stop = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"{self.clinic.name}: {self.balance} credits"


class CreditLedger(models.Model):
    """One change to a hospital's credits. Append-only: a mistake is corrected by a new ``adjustment`` entry."""

    class EntryType(models.TextChoices):
        GRANT = "grant", "Added"
        USAGE = "usage", "Call usage"
        ADJUSTMENT = "adjustment", "Adjustment"
        REFUND = "refund", "Refund"

    account = models.ForeignKey(CreditAccount, on_delete=models.PROTECT, related_name="entries")
    entry_type = models.CharField(max_length=16, choices=EntryType.choices)
    # Signed: positive adds credits, negative uses them.
    amount = models.BigIntegerField()
    balance_after = models.BigIntegerField()
    note = models.CharField(max_length=200, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    actor_email = models.CharField(max_length=254, blank=True)
    # The call a ``usage`` entry paid for (so a retried event can never charge a call twice).
    call_log = models.ForeignKey("ai_agent.CallLog", null=True, blank=True, on_delete=models.PROTECT, related_name="credit_entries")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["call_log"],
                condition=models.Q(entry_type="usage", call_log__isnull=False),
                name="uniq_usage_entry_per_call",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("Credit ledger entries can't be changed. Add an adjustment entry instead.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ProtectedError("Credit ledger entries can't be deleted. Add an adjustment entry instead.", {self})

    def __str__(self) -> str:
        return f"{self.entry_type} {self.amount:+d} -> {self.balance_after}"


class PlatformDefaults(models.Model):
    """What a new hospital starts with (the platform admin's "Defaults" page). A single row, ``pk=1``.

    Changing it never touches hospitals that already exist.
    """

    plan = models.CharField(max_length=64, default="growth")
    credits_per_minute = models.PositiveIntegerField(default=10)
    opening_credits = models.PositiveIntegerField(default=0)
    low_balance_threshold = models.PositiveIntegerField(default=500)
    max_admins = models.PositiveIntegerField(default=2)
    max_doctors = models.PositiveIntegerField(default=20)
    max_receptionists = models.PositiveIntegerField(default=5)

    class Meta:
        verbose_name_plural = "platform defaults"

    @classmethod
    def load(cls) -> "PlatformDefaults":
        return cls.objects.get_or_create(pk=1)[0]

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
