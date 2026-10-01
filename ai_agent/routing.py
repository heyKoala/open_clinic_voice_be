"""Which clinic does a call belong to? Decided by the phone number the patient dialled.

Each main clinic owns one number (``ClinicPhoneNumber``) shared by its centres. The provider's
call-start request isn't publicly documented, so the dialled number is read from any of the field
names commonly used for it; call events carry it as ``call.to_number`` (inbound) or
``call.from_number`` (outbound).
"""
from __future__ import annotations

from clinics.models import ClinicPhoneNumber

# Keys that name the called/dialled number (never the caller). Compared case-insensitively.
DIALLED_KEYS = {
    "to_number", "to", "tonumber", "called_number", "callednumber", "called", "dialled_number",
    "dialed_number", "dialednumber", "did", "trunk_number", "trunk_phone_number", "sip.trunkphonenumber",
    "sip_trunk_phone_number", "trunkphonenumber", "agent_number", "clinic_number", "phone_number_to",
}
NESTED_KEYS = ("call", "data", "metadata", "variables", "attributes", "participant_attributes", "sip", "room", "participant")
MIN_DIGITS = 8


def _digits(value) -> str:
    # SIP URIs such as "sip:+918031805277@sip.example.com" -> "918031805277"
    text = str(value or "")
    if text.lower().startswith("sip:"):
        text = text[4:].split("@", 1)[0]
    return "".join(ch for ch in text if ch.isdigit())


def _search(data, depth=0) -> str:
    if not isinstance(data, dict) or depth > 3:
        return ""
    for key, value in data.items():
        if str(key).lower() in DIALLED_KEYS and not isinstance(value, (dict, list)):
            digits = _digits(value)
            if len(digits) >= MIN_DIGITS:
                return digits
    for key in NESTED_KEYS:
        found = _search(data.get(key), depth + 1)
        if found:
            return found
    return ""


def dialled_number(data, query_params=None) -> str:
    """Digits of the clinic's number that this call is about ("" if not found)."""
    if isinstance(data, dict) and isinstance(data.get("call"), dict):
        call = data["call"]
        # Outbound calls are placed *from* the clinic's number.
        own = call.get("from_number") if call.get("direction") == "outbound" else call.get("to_number")
        if len(_digits(own)) >= MIN_DIGITS:
            return _digits(own)
    found = _search(data)
    if not found and query_params is not None:
        found = _digits(query_params.get("to") or query_params.get("to_number"))
    return found if len(found) >= MIN_DIGITS else ""


def clinic_for_number(number: str):
    """The main clinic that owns this number (matched on the last 10 digits), or None."""
    digits = _digits(number)
    if len(digits) < MIN_DIGITS:
        return None
    tail = digits[-10:]
    matches = [r for r in ClinicPhoneNumber.objects.filter(number__endswith=tail).select_related("clinic")
               if r.number.endswith(tail)]
    return matches[0].clinic if len(matches) == 1 else None
