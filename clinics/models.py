from __future__ import annotations

from datetime import timedelta

from django.db import models
from django.utils import timezone

from common.models import TrackedModel


def default_trial_ends_at():
	return timezone.now() + timedelta(days=14)


class Clinic(TrackedModel):
	class SubscriptionStatus(models.TextChoices):
		TRIAL = "trial", "Trial"
		ACTIVE = "active", "Active"
		PAST_DUE = "past_due", "Past Due"
		CANCELED = "canceled", "Canceled"

	class ClinicType(models.TextChoices):
		SINGLE_DOCTOR = "single_doctor", "Single Doctor"
		MULTI_DOCTOR = "multi_doctor", "Multi Doctor"

	name = models.CharField(max_length=255, unique=True)
	address = models.TextField(blank=True)
	clinic_type = models.CharField(
		max_length=32, 
		choices=ClinicType.choices, 
		default=ClinicType.MULTI_DOCTOR
	)
	subscription_status = models.CharField(
		max_length=32,
		choices=SubscriptionStatus.choices,
		default=SubscriptionStatus.TRIAL,
	)
	trial_ends_at = models.DateTimeField(default=default_trial_ends_at)

	# Additional Clinic Profile Details
	phone = models.CharField(max_length=32, blank=True)
	support_email = models.EmailField(blank=True)
	website = models.URLField(blank=True)
	registration_number = models.CharField(max_length=120, blank=True)
	tax_id = models.CharField(max_length=120, blank=True)
	description = models.TextField(blank=True)
	facilities = models.TextField(blank=True)
	holiday_calendar = models.TextField(blank=True)

	# Onboarding State
	is_onboarded = models.BooleanField(default=False)

	# A centre (branch) points at its main clinic; the main clinic has no parent. Centres of one
	# clinic share the main clinic's phone number and AI receptionist.
	parent = models.ForeignKey(
		"self", null=True, blank=True, on_delete=models.PROTECT, related_name="branches"
	)

	def __str__(self) -> str:
		return self.name

	@property
	def root(self) -> "Clinic":
		"""The main clinic of this clinic's group (itself if it is the main clinic)."""
		return self.parent if self.parent_id else self

	def group_clinics(self):
		"""The main clinic and all of its centres (main clinic first, then centres by name)."""
		root_id = self.parent_id or self.id
		return Clinic.objects.filter(models.Q(id=root_id) | models.Q(parent_id=root_id)).order_by(
			models.F("parent_id").asc(nulls_first=True), "name"
		)


class ClinicConfiguration(TrackedModel):
	clinic = models.OneToOneField(Clinic, on_delete=models.CASCADE, related_name="configuration")
	
	# AI Settings
	ai_enabled = models.BooleanField(default=True)
	ai_default_language = models.CharField(max_length=32, default="en")
	ai_voice_enabled = models.BooleanField(default=True)
	ai_transcription_enabled = models.BooleanField(default=True)
	ai_voice_type = models.CharField(max_length=50, default="basic")
	ai_phone_number = models.CharField(max_length=32, blank=True)
	
	# Clinic Settings
	default_consultation_minutes = models.PositiveSmallIntegerField(default=15)
	timezone = models.CharField(max_length=64, default="UTC")
	working_hours_start = models.TimeField(default="09:00")
	working_hours_end = models.TimeField(default="17:00")
	
	# Notification Settings
	sms_enabled = models.BooleanField(default=True)
	email_enabled = models.BooleanField(default=True)
	whatsapp_enabled = models.BooleanField(default=False)
	
	# Queue Settings
	auto_generate_tokens = models.BooleanField(default=True)
	token_prefix = models.CharField(max_length=10, default="")
	
	class Meta:
		verbose_name = "Clinic Configuration"
		verbose_name_plural = "Clinic Configurations"

	def __str__(self) -> str:
		return f"{self.clinic.name} Configuration"


class ClinicHoliday(TrackedModel):
	clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="holidays")
	date = models.DateField(db_index=True)
	reason = models.CharField(max_length=255, blank=True)

	class Meta:
		ordering = ["date"]
		unique_together = ("clinic", "date")

	def __str__(self) -> str:
		return f"{self.clinic.name} Holiday - {self.date}"


class PaymentTransaction(TrackedModel):
	class Status(models.TextChoices):
		CREATED = "created", "Created"
		CAPTURED = "captured", "Captured"
		FAILED = "failed", "Failed"

	clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="payment_transactions")
	razorpay_order_id = models.CharField(max_length=255, unique=True)
	razorpay_payment_id = models.CharField(max_length=255, blank=True)
	razorpay_signature = models.CharField(max_length=255, blank=True)
	amount = models.DecimalField(max_digits=10, decimal_places=2)
	plan = models.CharField(max_length=120)
	status = models.CharField(max_length=32, choices=Status.choices, default=Status.CREATED)

	class Meta:
		ordering = ["-created_at"]

	def __str__(self) -> str:
		return f"Payment {self.razorpay_order_id} - {self.status}"


class ClinicPhoneNumber(TrackedModel):
	"""The phone number patients call. Owned by a main clinic and shared by all of its centres."""

	class Status(models.TextChoices):
		# Bought, but Plivo is holding it (e.g. awaiting KYC/compliance approval).
		PENDING = "pending", "Pending approval"
		# Bought, but not yet routed to the voice agent's SIP app.
		PURCHASED = "purchased", "Purchased"
		ACTIVE = "active", "Active"

	clinic = models.OneToOneField(Clinic, on_delete=models.CASCADE, related_name="phone_number")
	number = models.CharField(max_length=20, unique=True, help_text="Digits only, with country code, e.g. 918031805277.")
	provider = models.CharField(max_length=20, default="plivo")
	country_iso = models.CharField(max_length=2, default="IN")
	city = models.CharField(max_length=100, blank=True)
	monthly_rental_rate = models.CharField(max_length=20, blank=True)
	app_id = models.CharField(max_length=64, blank=True)
	status = models.CharField(max_length=20, choices=Status.choices, default=Status.PURCHASED)
	last_error = models.TextField(blank=True)
	# False for a number that was already on the Plivo account and only linked to the clinic:
	# removing it then just disconnects it and never releases (unrents) it at Plivo.
	bought_here = models.BooleanField(default=True)

	def __str__(self) -> str:
		return f"+{self.number} ({self.clinic.name})"
