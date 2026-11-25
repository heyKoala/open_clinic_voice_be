"""The voice widget a hospital puts on its website.

One row is one widget: a "Talk to us" button that starts a browser call with the hospital's AI receptionist
(the same agent, tools and knowledge as its phone number). The website loads our script with the widget's
public key; calls it starts are CallLogs with ``source="widget"``.
"""
from __future__ import annotations

import secrets

from django.db import models

from common.models import ClinicScopedModel

DEFAULT_BRANDING = {"primary_color": "#111827", "position": "bottom-right", "button_label": "Talk to us"}


def generate_public_key() -> str:
    """The key in the embed snippet. It only names the widget, so it is safe to publish; "wd_" makes it
    easy to tell from other tokens in logs."""
    return "wd_" + secrets.token_urlsafe(24)


class WebWidget(ClinicScopedModel):
    """``clinic`` is always the main clinic: like the phone number, a widget belongs to the whole hospital."""

    class Mode(models.TextChoices):
        WIDGET = "widget", "Widget"  # our button and call panel
        HEADLESS = "headless", "Headless"  # no UI: the website uses its own button and window.HeyKoalaVoice

    name = models.CharField(max_length=120)
    public_key = models.CharField(max_length=64, unique=True, default=generate_public_key)
    # Websites allowed to use the widget, as exact origins ("https://www.example.com"). "*" allows any site.
    # Empty means no site, so a new widget does nothing until a site is added.
    allowed_origins = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.WIDGET)
    # Lets the visitor type an answer during the call (names and numbers without mishearing).
    show_text_input = models.BooleanField(default=True)
    # {primary_color, position, button_label}; checked by the serializer.
    branding = models.JSONField(default=dict, blank=True)
    # Shown in a small bubble next to the button before the visitor starts a call.
    greeting_message = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.public_key})"

    @property
    def resolved_branding(self) -> dict:
        merged = dict(DEFAULT_BRANDING)
        if isinstance(self.branding, dict):
            merged.update({key: value for key, value in self.branding.items() if key in DEFAULT_BRANDING and value})
        return merged

    def origin_allowed(self, origin: str | None) -> bool:
        origins = self.allowed_origins or []
        if "*" in origins:
            return True
        return bool(origin) and origin in origins

    def rotate_public_key(self) -> str:
        self.public_key = generate_public_key()
        self.save(update_fields=["public_key", "updated_at"])
        return self.public_key
