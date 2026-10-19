"""The hospital's phone number on the voice worker.

Noise cancellation and the recording notice for calls *to* the hospital live on the number's inbound
dispatch rule at the worker, not in the per-call config, so when those settings change they are pushed
with ``PUT /inbound/update``. The rule's ids come from registering the number (ROCK8_INBOUND_TRUNK_ID and
ROCK8_INBOUND_RULE_ID in .env; see backups/voice_inbound_*.json).
"""
from __future__ import annotations

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(settings.ROCK8_API_KEY and settings.ROCK8_INBOUND_TRUNK_ID and settings.ROCK8_INBOUND_RULE_ID)


def describe() -> dict:
    """For the settings page: whether changes can be pushed to the hospital number."""
    return {"configured": is_configured()}


def apply_call_flags(flags: dict) -> dict:
    """Push the inbound flags to the worker. Returns {"ok": bool, "detail": str}; never raises."""
    if not is_configured():
        return {"ok": False, "detail": "The hospital number's inbound ids are not set (ROCK8_INBOUND_TRUNK_ID / "
                                       "ROCK8_INBOUND_RULE_ID), so this applies to outbound and test calls only."}
    body = {"sip_trunk_id": settings.ROCK8_INBOUND_TRUNK_ID, "dispatch_rule_id": settings.ROCK8_INBOUND_RULE_ID, **flags}
    try:
        response = requests.put(f"{settings.VOICE_WORKER_URL.rstrip('/')}/inbound/update", json=body, timeout=20,
                                headers={"accept": "application/json", "X-API-Key": settings.ROCK8_API_KEY})
    except requests.RequestException as exc:
        logger.warning("Could not update the inbound number", exc_info=True)
        return {"ok": False, "detail": f"Couldn't reach the voice service ({exc.__class__.__name__}); "
                                       "the hospital number keeps its previous audio settings."}
    if response.status_code not in (200, 201):
        logger.warning("Inbound update refused: %s %s", response.status_code, response.text[:300])
        try:
            reason = str((response.json() or {}).get("detail") or "")[:200]
        except ValueError:
            reason = ""
        hint = (" The number must be registered again: python manage.py register_inbound_number <number>."
                if "not found" in reason.lower() else "")
        return {"ok": False, "detail": f"The voice service refused the change (HTTP {response.status_code}"
                                       f"{': ' + reason if reason else ''}); the hospital number keeps its previous audio settings.{hint}"}
    return {"ok": True, "detail": "Applied to calls on the hospital number."}
