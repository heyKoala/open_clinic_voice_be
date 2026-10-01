from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone

from common.models import TrackedModel


def default_invite_expiry():
	return timezone.now() + timedelta(days=7)


def default_verify_expiry():
	return timezone.now() + timedelta(days=3)


def default_password_reset_expiry():
	return timezone.now() + timedelta(hours=2)


class UserManager(BaseUserManager):
	use_in_migrations = True

	def _create_user(self, email, password, **extra_fields):
		if not email:
			raise ValueError("Email is required")
		email = self.normalize_email(email).lower()
		user = self.model(email=email, **extra_fields)
		user.set_password(password)
		user.save(using=self._db)
		return user

	def create_user(self, email, password=None, **extra_fields):
		extra_fields.setdefault("is_staff", False)
		extra_fields.setdefault("is_superuser", False)
		return self._create_user(email, password, **extra_fields)

	def create_superuser(self, email, password, **extra_fields):
		extra_fields.setdefault("is_staff", True)
		extra_fields.setdefault("is_superuser", True)
		extra_fields.setdefault("is_verified", True)
		return self._create_user(email, password, **extra_fields)


class User(TrackedModel, AbstractBaseUser, PermissionsMixin):
	class Role(models.TextChoices):
		CLINIC_ADMIN = "clinic_admin", "Clinic Admin"
		DOCTOR = "doctor", "Doctor"
		RECEPTIONIST = "receptionist", "Receptionist"

	class MembershipStatus(models.TextChoices):
		ACTIVE = "active", "Active"
		INVITED = "invited", "Invited"
		SUSPENDED = "suspended", "Suspended"
		ARCHIVED = "archived", "Archived"

	class GenderChoices(models.TextChoices):
		MALE = "male", "Male"
		FEMALE = "female", "Female"
		OTHER = "other", "Other"
		UNSPECIFIED = "unspecified", "Unspecified"

	clinic = models.ForeignKey("clinics.Clinic", null=True, blank=True, on_delete=models.CASCADE)
	clinics = models.ManyToManyField("clinics.Clinic", related_name="users", blank=True)
	email = models.EmailField(unique=True)
	full_name = models.CharField(max_length=255)
	role = models.CharField(max_length=32, choices=Role.choices)
	age = models.PositiveSmallIntegerField(null=True, blank=True)
	gender = models.CharField(
		max_length=32, 
		choices=GenderChoices.choices, 
		default=GenderChoices.UNSPECIFIED
	)
	membership_status = models.CharField(
		max_length=32,
		choices=MembershipStatus.choices,
		default=MembershipStatus.ACTIVE,
	)
	is_clinic_admin = models.BooleanField(default=False)
	is_doctor = models.BooleanField(default=False)
	is_active = models.BooleanField(default=True)
	is_staff = models.BooleanField(default=False)
	is_verified = models.BooleanField(default=False)
	email_verified_at = models.DateTimeField(null=True, blank=True)
	failed_login_attempts = models.PositiveIntegerField(default=0)
	lock_until = models.DateTimeField(null=True, blank=True)

	USERNAME_FIELD = "email"
	REQUIRED_FIELDS = []

	objects = UserManager()

	def __str__(self) -> str:
		return self.email


class Invitation(TrackedModel):
	clinic = models.ForeignKey("clinics.Clinic", on_delete=models.CASCADE)
	invited_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sent_invitations")
	email = models.EmailField()
	role = models.CharField(max_length=32, choices=User.Role.choices)
	token_hash = models.CharField(max_length=64, unique=True)
	expires_at = models.DateTimeField(default=default_invite_expiry)
	used_at = models.DateTimeField(null=True, blank=True)
	revoked_at = models.DateTimeField(null=True, blank=True)

	class Meta:
		indexes = [models.Index(fields=["clinic", "email"])]

	@property
	def is_usable(self) -> bool:
		return self.used_at is None and self.revoked_at is None and self.expires_at >= timezone.now()


class EmailVerificationToken(TrackedModel):
	user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="email_verification_tokens")
	token_hash = models.CharField(max_length=64, unique=True)
	expires_at = models.DateTimeField(default=default_verify_expiry)
	used_at = models.DateTimeField(null=True, blank=True)

	@property
	def is_usable(self) -> bool:
		return self.used_at is None and self.expires_at >= timezone.now()


class PasswordResetToken(TrackedModel):
	user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="password_reset_tokens")
	token_hash = models.CharField(max_length=64, unique=True)
	expires_at = models.DateTimeField(default=default_password_reset_expiry)
	used_at = models.DateTimeField(null=True, blank=True)

	@property
	def is_usable(self) -> bool:
		return self.used_at is None and self.expires_at >= timezone.now()
