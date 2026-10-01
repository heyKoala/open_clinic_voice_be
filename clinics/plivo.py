"""Minimal Plivo client for clinic phone numbers: search, buy, route to the SIP app, release.

API reference: https://www.plivo.com/docs/numbers/api/phone-number (search / buy) and
https://www.plivo.com/docs/numbers/api/account-phone-number (update / unrent).
"""
from __future__ import annotations

import requests
from django.conf import settings

API_BASE = "https://api.plivo.com/v1/Account"
TIMEOUT = 20


class PlivoError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def is_configured() -> bool:
    return bool(settings.PLIVO_AUTH_ID and settings.PLIVO_AUTH_TOKEN)


def _request(method: str, path: str, **kwargs) -> dict:
    if not is_configured():
        raise PlivoError("Phone numbers are not configured on the server (PLIVO_AUTH_ID / PLIVO_AUTH_TOKEN).")
    url = f"{API_BASE}/{settings.PLIVO_AUTH_ID}/{path}"
    try:
        response = requests.request(method, url, auth=(settings.PLIVO_AUTH_ID, settings.PLIVO_AUTH_TOKEN),
                                    timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise PlivoError(f"Could not reach Plivo: {exc.__class__.__name__}") from exc
    if response.status_code == 204 or not response.content:
        data = {}
    else:
        try:
            data = response.json()
        except ValueError:
            data = {"error": response.text[:300]}
    if response.status_code >= 400:
        message = data.get("error") or data.get("message") or f"Plivo returned HTTP {response.status_code}"
        raise PlivoError(str(message), response.status_code)
    return data


def normalize_number(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def search_numbers(*, country_iso: str = "IN", number_type: str = "local", city: str = "",
                   pattern: str = "", limit: int = 20, offset: int = 0) -> dict:
    params = {"country_iso": country_iso, "type": number_type, "limit": limit, "offset": offset}
    if city:
        params["city"] = city
    if pattern:
        params["pattern"] = pattern
    data = _request("GET", "PhoneNumber/", params=params)
    numbers = [
        {
            "number": item.get("number"),
            "city": item.get("city") or item.get("region") or "",
            "region": item.get("region") or "",
            "type": item.get("sub_type") or item.get("type") or number_type,
            "monthly_rental_rate": item.get("monthly_rental_rate"),
            "setup_rate": item.get("setup_rate"),
            "voice_enabled": bool(item.get("voice_enabled")),
            "sms_enabled": bool(item.get("sms_enabled")),
            "requires_compliance": bool((item.get("compliance_requirement") or {}).get("business")
                                        or (item.get("compliance_requirement") or {}).get("individual")),
        }
        for item in data.get("objects", [])
        if item.get("voice_enabled")  # the AI receptionist needs voice
    ]
    return {"numbers": numbers, "total_count": (data.get("meta") or {}).get("total_count", len(numbers))}


def buy_number(number: str) -> str:
    """Rent the number. Returns Plivo's status for it: "Success" or "pending" (awaiting approval)."""
    data = _request("POST", f"PhoneNumber/{normalize_number(number)}/", json={})
    entries = data.get("numbers") or []
    if entries and isinstance(entries[0], dict) and entries[0].get("status"):
        return str(entries[0]["status"])
    return str(data.get("status") or "Success")


def assign_app(number: str, app_id: str) -> None:
    """Route the number's calls to the Plivo application (the SIP trunk to the voice agent)."""
    _request("POST", f"Number/{normalize_number(number)}/", json={"app_id": app_id})


def release_number(number: str) -> None:
    """Give the number back to Plivo. Already-released numbers count as success."""
    try:
        _request("DELETE", f"Number/{normalize_number(number)}/")
    except PlivoError as exc:
        if exc.status_code != 404:
            raise
