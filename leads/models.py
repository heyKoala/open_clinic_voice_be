from __future__ import annotations

from django.db import models


class Lead(models.Model):
    """Someone who asked to hear from us on the Slot website ("Talk to us")."""

    name = models.CharField(max_length=120)
    email = models.EmailField()
    phone = models.CharField(max_length=32)
    organisation = models.CharField(max_length=200, blank=True)
    # Which page or form it came from, so more entry points can share this table.
    source = models.CharField(max_length=40, default="slot-site")
    # False if our team's notification email could not be sent: the lead is still saved here.
    notified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.organisation or self.email})"
