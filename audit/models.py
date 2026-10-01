from __future__ import annotations

from django.conf import settings
from django.db import models

from common.models import TrackedModel


class AuthEvent(TrackedModel):
    class EventType(models.TextChoices):
        LOGIN_SUCCESS = "login_success", "Login Success"
        LOGIN_FAILURE = "login_failure", "Login Failure"
        LOGOUT = "logout", "Logout"
        PASSWORD_RESET = "password_reset", "Password Reset"
        SIGNUP = "signup", "Signup"
        INVITE_CREATED = "invite_created", "Invite Created"
        INVITE_ACCEPTED = "invite_accepted", "Invite Accepted"
        ROLE_CHANGED = "role_changed", "Role Changed"
        USER_DEACTIVATED = "user_deactivated", "User Deactivated"
        USER_REACTIVATED = "user_reactivated", "User Reactivated"
        DATA_EXPORTED = "data_exported", "Data Exported"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    email = models.EmailField(null=True, blank=True)
    event_type = models.CharField(max_length=64, choices=EventType.choices)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

class DeviceSession(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    device_id = models.CharField(max_length=64, unique=True)
    user_agent = models.TextField()
    ip_address = models.GenericIPAddressField()
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_seen']


class DataChangeEvent(TrackedModel):
    class Action(models.TextChoices):
        CREATE = "create", "Create"
        READ = "read", "Read"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"
        DEACTIVATE = "deactivate", "Deactivate"
        REACTIVATE = "reactivate", "Reactivate"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    clinic = models.ForeignKey("clinics.Clinic", null=True, blank=True, on_delete=models.SET_NULL)
    model_name = models.CharField(max_length=64)  # e.g., "Patient", "Doctor", "Appointment"
    object_id = models.CharField(max_length=64)  # ID of the affected object
    action = models.CharField(max_length=20, choices=Action.choices)
    field_name = models.CharField(max_length=64, null=True, blank=True)  # Specific field changed (for updates)
    old_value = models.TextField(null=True, blank=True)  # Previous value
    new_value = models.TextField(null=True, blank=True)  # New value
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["clinic", "model_name", "created_at"]),
            models.Index(fields=["model_name", "object_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.model_name} {self.object_id} by {self.user}"
