"""Starting a browser call with the voice worker: used by the app's Test call and by the website widget."""
from __future__ import annotations

import requests
from django.conf import settings

from ai_agent.agent_settings import call_flags

WEB_CALL_URL = "https://worker.heykoala.ai/webCall"


class WebCallError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


def public_base_url(request=None) -> str:
    """The address the voice worker reaches this backend on: the public one (tunnel or deployment) first,
    then whatever the request came in on."""
    base = getattr(settings, "PUBLIC_BASE_URL", "") or getattr(settings, "BACKEND_URL", "")
    if not base and request is not None:
        base = f"{request.scheme}://{request.get_host()}"
    return base.rstrip("/")


def start_web_call(root, webhook_url: str, participant_name: str, participant_identity: str) -> dict:
    """Ask the worker for a browser call that fetches its agent from ``webhook_url``. Returns the worker's
    reply (the LiveKit token) with ``serverUrl`` filled in, so the browser knows where to connect."""
    api_key = getattr(settings, "ROCK8_API_KEY", None)
    if not api_key:
        raise WebCallError("ROCK8_API_KEY is not configured.", status=500)
    payload = {
        "participant_name": participant_name,
        "participant_identity": participant_identity,
        "webhook_url": webhook_url,
        **call_flags(root, for_web_call=True),
    }
    headers = {"accept": "application/json", "X-API-Key": api_key, "Content-Type": "application/json"}
    try:
        response = requests.post(WEB_CALL_URL, json=payload, headers=headers, timeout=20)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        detail = exc.response.text if getattr(exc, "response", None) is not None else str(exc)
        raise WebCallError(f"Failed to start web call: {detail}") from exc
    data = response.json()
    if isinstance(data, dict) and not data.get("serverUrl"):
        data["serverUrl"] = settings.LIVEKIT_URL
    return data
