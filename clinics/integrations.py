"""The clinic's own systems we connect to: what each needs, and how to check a connection works.

- WhatsApp (WATI): the clinic's WATI number, where patients book by chatting. WATI forwards their messages
  to our webhook (wati_webhook_path); we reply in the chat, which needs no template. Templates are only for
  messages we start ourselves: a confirmation for bookings made by phone or at the front desk, and the
  Google review request. Reminders are phone calls, not WhatsApp.
- Doctor & appointment system (PMS): the clinic's own scheduling system, for syncing doctors and bookings.
  Saved and checked now; the sync itself is built later.
- Razorpay: the clinic's own Razorpay account, for a payment link per booking at the clinic's consultation
  fee. Razorpay texts the link to the patient (in a WhatsApp booking chat we can also reply with it), and
  reports payments to our webhook, signed with a secret we generate (razorpay_webhook_secret).
"""
from __future__ import annotations

import hashlib
import hmac

import requests
from django.conf import settings as django_settings

from clinics.models import ClinicIntegration

TIMEOUT = 15

# Per provider: the plain settings it takes (name -> required?), and what its credential is called.
PROVIDERS = {
    ClinicIntegration.Provider.WATI: {
        "settings": {
            "api_endpoint": True,          # e.g. https://live-mt-server.wati.io/123456
            "whatsapp_number": True,       # the number patients message to book
            "confirmation_template": False,  # sent after a booking made by phone or at the front desk
            "review_template": False,        # Google review request after a positive feedback call
        },
        "secret": "access_token",
    },
    ClinicIntegration.Provider.PMS: {
        "settings": {
            "system_name": False,          # e.g. "Practo", "KIMS HIS"
            "base_url": True,
            "auth_type": False,            # "bearer" (default) or "header"
            "header_name": False,          # for auth_type "header", e.g. X-API-Key
            "doctors_path": False,         # e.g. /api/doctors
            "appointments_path": False,    # e.g. /api/appointments
        },
        "secret": "api_key",
    },
    ClinicIntegration.Provider.RAZORPAY: {
        "settings": {
            "key_id": True,                # rzp_test_… or rzp_live_…
            "consultation_fee": True,      # rupees, charged per booking
            "link_valid_hours": False,     # how long a payment link stays open (default 24)
        },
        "secret": "key_secret",
    },
}

URL_SETTINGS = ("api_endpoint", "base_url")
DEFAULT_LINK_VALID_HOURS = 24


def _derived_secret(label: str, clinic_id) -> str:
    return hmac.new(django_settings.SECRET_KEY.encode(), f"{label}:{clinic_id}".encode(), hashlib.sha256).hexdigest()[:40]


def wati_webhook_token(clinic_id) -> str:
    """Secret part of the clinic's WATI webhook URL. WATI can't sign requests, so the URL carries the proof."""
    return _derived_secret("wati", clinic_id)


def wati_webhook_path(clinic_id) -> str:
    return f"/api/v1/clinics/wati/webhook/{clinic_id}/{wati_webhook_token(clinic_id)}/"


def razorpay_webhook_secret(clinic_id) -> str:
    """The secret the clinic enters in Razorpay → Webhooks; Razorpay signs every event with it."""
    return _derived_secret("razorpay-webhook", clinic_id)


def razorpay_webhook_path(clinic_id) -> str:
    return f"/api/v1/clinics/razorpay/webhook/{clinic_id}/"


def razorpay_signature_ok(clinic_id, body: bytes, signature: str) -> bool:
    expected = hmac.new(razorpay_webhook_secret(clinic_id).encode(), body, hashlib.sha256).hexdigest()
    return bool(signature) and hmac.compare_digest(expected, signature)


class CheckFailed(Exception):
    pass


def clean_settings(provider: str, raw: dict) -> tuple[dict, dict]:
    """(settings, errors): only the provider's own keys, trimmed; URLs must be http(s)."""
    spec = PROVIDERS[provider]["settings"]
    values = {key: str((raw or {}).get(key) or "").strip() for key in spec}
    errors = {}
    for key, required in spec.items():
        if required and not values[key]:
            errors[key] = ["This field is required."]
        elif key in URL_SETTINGS and values[key] and not values[key].startswith(("https://", "http://")):
            errors[key] = ["Enter the full address, starting with https://."]
    if provider == ClinicIntegration.Provider.PMS:
        values["auth_type"] = values["auth_type"] if values["auth_type"] in ("bearer", "header") else "bearer"
        if values["auth_type"] == "header" and not values["header_name"]:
            errors["header_name"] = ["Name the header the key goes in, e.g. X-API-Key."]
    if provider == ClinicIntegration.Provider.RAZORPAY:
        if values["key_id"] and not values["key_id"].startswith(("rzp_test_", "rzp_live_")):
            errors["key_id"] = ["A Razorpay Key ID starts with rzp_test_ or rzp_live_."]
        if values["consultation_fee"]:
            try:
                fee = float(values["consultation_fee"])
            except ValueError:
                fee = 0
            if fee < 1:
                errors["consultation_fee"] = ["Enter the fee in rupees, at least ₹1."]
        if values["link_valid_hours"] and (not values["link_valid_hours"].isdigit() or not 1 <= int(values["link_valid_hours"]) <= 720):
            errors["link_valid_hours"] = ["Enter a number of hours between 1 and 720."]
    return {k: v for k, v in values.items() if v}, errors


def _bearer(token: str) -> str:
    # WATI shows its token with the "Bearer " prefix already; accept it either way.
    return token[7:].strip() if token.lower().startswith("bearer ") else token.strip()


def _describe(response) -> str:
    try:
        body = response.json()
        detail = body.get("message") or body.get("detail") or body.get("error") if isinstance(body, dict) else None
    except ValueError:
        detail = None
    return f"HTTP {response.status_code}" + (f": {detail}" if detail else "")


def check_wati(settings: dict, secret: str) -> dict:
    """Ask WATI for the account's message templates. Returns {"message", "templates": [approved names]}."""
    url = f"{settings['api_endpoint'].rstrip('/')}/api/v1/getMessageTemplates"
    try:
        response = requests.get(url, headers={"Authorization": f"Bearer {_bearer(secret)}"},
                                params={"pageSize": 200}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise CheckFailed(f"Couldn't reach WATI at that address ({exc.__class__.__name__}).") from exc
    if response.status_code in (401, 403):
        raise CheckFailed("WATI rejected the access token. Copy it again from WATI → API Docs.")
    if response.status_code >= 400:
        raise CheckFailed(f"WATI answered with an error ({_describe(response)}). Check the API endpoint.")
    try:
        templates = response.json().get("messageTemplates") or []
    except (ValueError, AttributeError) as exc:
        raise CheckFailed("That address answered, but not like WATI. Check the API endpoint.") from exc
    approved = sorted({t.get("elementName") for t in templates
                       if isinstance(t, dict) and t.get("elementName") and str(t.get("status", "")).upper() == "APPROVED"})
    return {"message": f"Connected to WATI. {len(approved)} approved message template{'s' if len(approved) != 1 else ''}.",
            "templates": approved}


def check_pms(settings: dict, secret: str) -> dict:
    """Call the system's doctors endpoint (or its base address) with the key. Any non-error answer passes."""
    url = settings["base_url"].rstrip("/") + (settings.get("doctors_path") or "")
    if settings.get("auth_type") == "header":
        headers = {settings["header_name"]: secret}
    else:
        headers = {"Authorization": f"Bearer {_bearer(secret)}"}
    try:
        response = requests.get(url, headers={**headers, "Accept": "application/json"}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise CheckFailed(f"Couldn't reach the system at that address ({exc.__class__.__name__}).") from exc
    if response.status_code in (401, 403):
        raise CheckFailed("The system rejected the key. Check the key and how it is sent.")
    if response.status_code >= 400:
        raise CheckFailed(f"The system answered with an error ({_describe(response)}). Check the address and paths.")
    return {"message": f"Connected. The system answered ({response.status_code}).", "templates": []}


def check_razorpay(settings: dict, secret: str) -> dict:
    """List one payment link with the keys: proves the keys work and Payment Links is on for the account."""
    try:
        response = requests.get("https://api.razorpay.com/v1/payment_links", params={"count": 1},
                                auth=(settings["key_id"], secret), timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise CheckFailed(f"Couldn't reach Razorpay ({exc.__class__.__name__}). Try again.") from exc
    if response.status_code == 401:
        raise CheckFailed("Razorpay rejected the keys. Copy the Key ID and Key Secret again from Razorpay → API Keys.")
    if response.status_code >= 400:
        raise CheckFailed(f"Razorpay answered with an error ({_describe(response)}).")
    mode = "test" if settings["key_id"].startswith("rzp_test_") else "live"
    return {"message": f"Connected to Razorpay ({mode} mode).", "templates": []}


CHECKS = {
    ClinicIntegration.Provider.WATI: check_wati,
    ClinicIntegration.Provider.PMS: check_pms,
    ClinicIntegration.Provider.RAZORPAY: check_razorpay,
}
