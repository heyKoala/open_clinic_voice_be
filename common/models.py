from __future__ import annotations

from django.conf import settings
from django.db import models

from common.current_user import get_current_user


class TrackedModel(models.Model):
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)
	changed_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="changed_%(app_label)s_%(class)s_set",
	)

	class Meta:
		abstract = True

	def save(self, *args, **kwargs):
		current_user = get_current_user()
		if current_user is not None and getattr(current_user, "is_authenticated", False):
			self.changed_by = current_user
		super().save(*args, **kwargs)


class ClinicScopedModel(TrackedModel):
	clinic = models.ForeignKey("clinics.Clinic", on_delete=models.CASCADE)

	class Meta:
		abstract = True
