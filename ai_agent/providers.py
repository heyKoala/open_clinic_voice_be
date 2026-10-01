"""The voice provider's catalog: which speech-to-text, LLM, text-to-speech and realtime providers,
models, languages and voices the agent can run with (https://worker.heykoala.ai/docs).

Read through the backend so the provider API key never reaches the browser. Responses are cached:
the catalog changes rarely and the admin page asks for the same entries repeatedly.
"""
from __future__ import annotations

from urllib.parse import urlencode

import requests
from django.conf import settings
from django.core.cache import cache

BASE_URL = "https://worker.heykoala.ai"
KINDS = ("stt", "llm", "tts", "realtime")
# Providers the provider's POST /tts endpoint can synthesize a sample with.
PREVIEW_PROVIDERS = ("cartesia", "sarvam")
CACHE_SECONDS = 10 * 60


class ProviderError(Exception):
    """The catalog could not be read (not configured, unreachable, or an upstream error)."""


class UnknownProvider(ProviderError):
    """The provider (or model) does not exist in the catalog."""


def is_configured() -> bool:
    return bool(settings.ROCK8_API_KEY)


def _headers() -> dict:
    if not is_configured():
        raise ProviderError("ROCK8_API_KEY is not configured.")
    return {"accept": "application/json", "X-API-Key": settings.ROCK8_API_KEY}


def _detail(response) -> str:
    try:
        return str(response.json().get("detail") or response.text)[:300]
    except ValueError:
        return response.text[:300]


def _get(path: str, params: dict | None = None) -> dict:
    params = {key: value for key, value in (params or {}).items() if value not in (None, "")}
    cache_key = f"voice-catalog:{path}?{urlencode(sorted(params.items()))}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    headers = _headers()
    try:
        response = requests.get(BASE_URL + path, params=params, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise ProviderError(f"The voice provider could not be reached ({exc.__class__.__name__}).")
    if response.status_code == 404:
        raise UnknownProvider(_detail(response))
    if not response.ok:
        raise ProviderError(f"The voice provider returned {response.status_code}: {_detail(response)}")
    data = response.json()
    cache.set(cache_key, data, CACHE_SECONDS)
    return data


def list_providers(kind: str) -> list[dict]:
    """Every provider of one kind: ``[{name, description, model_count, models_dynamic}]``."""
    return _get("/providers", {"type": kind}).get("providers", [])


def provider_entry(kind: str, provider: str) -> dict:
    """One provider in full: ``config_fields`` and ``models[]`` (each with its ``languages[]``;
    realtime models also carry ``voices[]``)."""
    return _get("/providers", {"type": kind, "provider": provider})


def voices(provider: str, **filters) -> dict:
    """A text-to-speech provider's voices; ``filters``: model, q, language, gender."""
    return _get(f"/providers/tts/{provider}/voices", filters)


def synthesize(provider: str, model: str, voice: str, text: str, language: str = "") -> bytes:
    """A short MP3 sample of ``voice`` saying ``text``."""
    payload = {"provider": provider, "model": model, "voice": voice, "text": text}
    if language:
        payload["language"] = language
    try:
        response = requests.post(f"{BASE_URL}/tts", json=payload, headers=_headers(), timeout=30)
    except requests.RequestException as exc:
        raise ProviderError(f"The voice provider could not be reached ({exc.__class__.__name__}).")
    if not response.ok:
        raise ProviderError(f"The voice provider returned {response.status_code}: {_detail(response)}")
    return response.content
