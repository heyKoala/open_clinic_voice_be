"""The AI receptionist's configurable settings: greeting, instructions and the models it runs on.

Stored per main clinic in ``VoiceAgentSettings``; a clinic without a row runs on the defaults below.
Model choices are validated against the voice provider's catalog when they are saved, and stored
together with the provider's own config keys so that answering a call never needs the catalog.
"""
from __future__ import annotations

from django.conf import settings as django_settings

from ai_agent import inbound, providers
from ai_agent.models import VoiceAgentSettings

# "{clinic_name}" is replaced with the main clinic's name.
DEFAULT_FIRST_MESSAGE = "Welcome to {clinic_name}. How can I help you today?"
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful medical receptionist for {clinic_name} handling incoming calls from patients. "
    "Your main tasks are: "
    "1. Schedule, reschedule, or cancel appointments. "
    "2. If an appointment slot is unavailable or the doctor is away, suggest the next available slot. "
    "3. If the patient wants to book an appointment, DO NOT list all doctors at once. First, ask them what specialty or domain they need (e.g., General Physician, Cardiology, ENT). "
    "4. Once they specify a specialty, use the provided tools to fetch doctors in that domain, and only list those specific doctors to the patient. "
    "5. To cancel an appointment, FIRST use the lookup_appointments tool to search for it using their name or phone number. "
    "6. If the lookup tool returns multiple appointments (e.g., people with the same first name), read out the doctor names, dates, and times, and ask the patient to confirm which one is theirs before you cancel it. "
    "Use the provided tools to fetch available slots and manage appointments. "
    "CRITICAL BEHAVIOR: "
    "1. KEEP YOUR RESPONSES EXTREMELY SHORT AND CONCISE. "
    "2. Never use more than 1 or 2 short sentences. "
    "3. Do not list out all available options unless asked. "
    "4. Respond naturally and quickly, as if you are on a real, fast-paced phone call. "
    "BOOKING RULES: "
    "1. Before booking, you MUST ask for the patient's full name. Never book with a placeholder such as 'Unknown', 'Caller' or 'Patient'. "
    "2. You also need the patient's phone number (see CALLER below). "
    "3. Ask the caller briefly what the visit is for and pass it as reason. If they would rather not say, book without it. "
    "4. Before calling book_appointment, repeat the doctor, centre, date, time and patient name back to the caller and get a yes."
)
DEFAULT_STAGES = {
    "stt": {"provider": "sarvam", "model": "saaras:v3", "language": "unknown",
            "config": {"model": "saaras:v3", "language": "unknown"}},
    "llm": {"provider": "gemini", "model": "gemini-3.1-flash-lite", "temperature": 0.3,
            "config": {"model": "gemini-3.1-flash-lite", "temperature": 0.3}},
    "tts": {"provider": "sarvam", "model": "bulbul:v3", "voice": "simran", "language": "en-IN",
            "config": {"model": "bulbul:v3", "speaker": "simran", "target_language_code": "en-IN"}},
    "realtime": {},
}
STAGES_BY_MODEL_TYPE = {
    VoiceAgentSettings.ModelType.STANDARD: ("stt", "llm", "tts"),
    VoiceAgentSettings.ModelType.REALTIME: ("realtime",),
}
# Call audio and listening. ``listening`` picks a voice-activity-detection preset; the worker gets the numbers.
CALL_DEFAULTS = {
    "noise_cancellation": True,
    "noise_cancellation_level": None,   # dB; None = the worker's default (about 30)
    "recording_disclosure": False,
    "listening": VoiceAgentSettings.Listening.BALANCED,
    "max_silence_seconds": 60,
    "thinking_sound": True,
}
CALL_FIELDS = tuple(CALL_DEFAULTS)
LISTENING_PRESETS = {
    # min_silence_duration: how long the caller must be quiet before the receptionist takes its turn.
    VoiceAgentSettings.Listening.PATIENT: {"min_silence_duration": 0.8, "activation_threshold": 0.45, "min_speech_duration": 0.08},
    VoiceAgentSettings.Listening.BALANCED: {"min_silence_duration": 0.45, "activation_threshold": 0.4, "min_speech_duration": 0.05},
    VoiceAgentSettings.Listening.QUICK: {"min_silence_duration": 0.25, "activation_threshold": 0.35, "min_speech_duration": 0.05},
}
LISTENING_LABELS = {
    VoiceAgentSettings.Listening.PATIENT: "Patient: waits longer before replying, for callers who pause mid-sentence.",
    VoiceAgentSettings.Listening.BALANCED: "Balanced: the usual phone-call rhythm.",
    VoiceAgentSettings.Listening.QUICK: "Quick: replies as soon as the caller stops; may cut in on slow speakers.",
}
NOISE_LEVEL_RANGE = (5, 60)
SILENCE_RANGE = (15, 600)
# Instructions longer than this still save, but the settings page warns that the agent may reply slower:
# the whole prompt is re-read on every turn. The hard limit is settings.AGENT_PROMPT_MAX_CHARS.
LONG_SYSTEM_PROMPT = 25000


def max_system_prompt() -> int:
    """The longest instructions that can be saved (AGENT_PROMPT_MAX_CHARS, so it changes without a release)."""
    return django_settings.AGENT_PROMPT_MAX_CHARS


class InvalidSettings(Exception):
    """``errors`` maps a field name to its message, e.g. {"tts": "Choose a voice."}."""

    def __init__(self, errors: dict):
        super().__init__(errors)
        self.errors = errors


def _legacy_voice(root) -> str:
    """The Sarvam voice picked before these settings existed ("sarvam_ritu" -> "ritu")."""
    voice_type = getattr(getattr(root, "configuration", None), "ai_voice_type", "") or ""
    return voice_type[len("sarvam_"):] if voice_type.startswith("sarvam_") else ""


def effective_settings(root) -> dict:
    """The settings a call to ``root`` runs with: what was saved, with defaults for the rest."""
    saved = VoiceAgentSettings.objects.filter(clinic=root).first()
    stages = {}
    for name, default in DEFAULT_STAGES.items():
        stage = dict(getattr(saved, name, None) or default)
        stages[name] = stage
    if saved is None or not saved.tts:
        voice = _legacy_voice(root)
        if voice:
            stages["tts"] = {**stages["tts"], "voice": voice, "config": {**stages["tts"]["config"], "speaker": voice}}
    model_type = saved.model_type if saved else VoiceAgentSettings.ModelType.STANDARD
    if model_type == VoiceAgentSettings.ModelType.REALTIME and not stages["realtime"].get("provider"):
        model_type = VoiceAgentSettings.ModelType.STANDARD
    calls = {name: (getattr(saved, name) if saved else default) for name, default in CALL_DEFAULTS.items()}
    return {
        "first_message": (saved.first_message if saved else "") or DEFAULT_FIRST_MESSAGE,
        "system_prompt": (saved.system_prompt if saved else "") or DEFAULT_SYSTEM_PROMPT,
        "model_type": model_type,
        "is_customised": saved is not None,
        **stages,
        **calls,
    }


def call_flags(root, for_web_call: bool = False) -> dict:
    """The per-call flags for the worker's /call, /webCall and inbound endpoints (noise cancellation and,
    for phone calls, the recording notice)."""
    current = effective_settings(root)
    flags = {"noise_cancellation": bool(current["noise_cancellation"])}
    if current["noise_cancellation"] and current["noise_cancellation_level"] is not None:
        flags["noise_cancellation_level"] = int(current["noise_cancellation_level"])
    if not for_web_call:
        flags["recording_disclosure"] = bool(current["recording_disclosure"])
    return flags


def session_config(current: dict) -> dict:
    """The listening/sound part of the config reply, from ``effective_settings``."""
    preset = LISTENING_PRESETS.get(current["listening"], LISTENING_PRESETS[VoiceAgentSettings.Listening.BALANCED])
    return {
        "thinking_sound": bool(current["thinking_sound"]),
        "max_silence_seconds": int(current["max_silence_seconds"]),
        "background_sound": False,
        "vad_config": dict(preset),
    }


def resolve_call_settings(data: dict, current: dict | None = None) -> dict:
    """Validate the call audio/listening fields. A field left out keeps its ``current`` value (what was saved,
    else the default), so clients that don't know these fields (the original front end) never reset them.
    Raises InvalidSettings."""
    base = {**CALL_DEFAULTS, **(current or {})}
    data = {**base, **{k: v for k, v in data.items() if k in CALL_DEFAULTS}}
    errors, out = {}, {}
    for name in ("noise_cancellation", "recording_disclosure", "thinking_sound"):
        value = data.get(name, CALL_DEFAULTS[name])
        if not isinstance(value, bool):
            errors[name] = "Must be on or off."
        out[name] = value
    level = data.get("noise_cancellation_level", CALL_DEFAULTS["noise_cancellation_level"])
    if level in (None, ""):
        out["noise_cancellation_level"] = None
    else:
        try:
            level = int(level)
        except (TypeError, ValueError):
            errors["noise_cancellation_level"] = "Must be a whole number of dB."
        else:
            if not NOISE_LEVEL_RANGE[0] <= level <= NOISE_LEVEL_RANGE[1]:
                errors["noise_cancellation_level"] = f"Between {NOISE_LEVEL_RANGE[0]} and {NOISE_LEVEL_RANGE[1]} dB."
            out["noise_cancellation_level"] = level
    listening = data.get("listening", CALL_DEFAULTS["listening"])
    if listening not in LISTENING_PRESETS:
        errors["listening"] = "Choose patient, balanced or quick."
    out["listening"] = listening
    try:
        silence = int(data.get("max_silence_seconds", CALL_DEFAULTS["max_silence_seconds"]))
    except (TypeError, ValueError):
        errors["max_silence_seconds"] = "Must be a number of seconds."
    else:
        if not SILENCE_RANGE[0] <= silence <= SILENCE_RANGE[1]:
            errors["max_silence_seconds"] = f"Between {SILENCE_RANGE[0]} and {SILENCE_RANGE[1]} seconds."
        out["max_silence_seconds"] = silence
    if errors:
        raise InvalidSettings(errors)
    return out


def render(text: str, root) -> str:
    return text.replace("{clinic_name}", root.name)


def provider_config(stage: dict) -> dict:
    """A stage in the shape the voice provider expects: ``{"provider", "config"}``."""
    return {"provider": stage.get("provider", ""), "config": dict(stage.get("config") or {})}


def resolve_stage(kind: str, data) -> dict:
    """Validate one stage's choice against the catalog and attach the provider's config keys.

    Raises ``InvalidSettings`` for a bad choice and ``providers.ProviderError`` if the catalog
    cannot be read.
    """
    data = data if isinstance(data, dict) else {}
    provider = str(data.get("provider") or "").strip()
    if not provider:
        raise InvalidSettings({kind: "Choose a provider."})
    try:
        entry = providers.provider_entry(kind, provider)
    except providers.UnknownProvider:
        raise InvalidSettings({kind: f"Unknown provider '{provider}'."})

    fields = entry.get("config_fields") or {}
    models = entry.get("models") or []
    model = str(data.get("model") or "").strip()
    language = str(data.get("language") or "").strip()
    voice = str(data.get("voice") or "").strip()

    chosen = next((m for m in models if m.get("id") == model), None)
    if "model" in fields and models:
        if not model:
            raise InvalidSettings({kind: "Choose a model."})
        if chosen is None:
            raise InvalidSettings({kind: f"'{model}' is not a {provider} model."})
    languages = [item.get("code") for item in (chosen or {}).get("languages") or []]
    if language and languages and language not in languages:
        raise InvalidSettings({kind: f"{model} does not support the language '{language}'."})
    if "voice" in fields and not voice:
        raise InvalidSettings({kind: "Choose a voice."})

    stage = {"provider": provider, "model": model, "language": language, "voice": voice}
    config = {fields[name]: stage[name] for name in ("model", "language", "voice") if name in fields and stage[name]}
    if kind == "llm":
        try:
            temperature = float(data.get("temperature", DEFAULT_STAGES["llm"]["temperature"]))
        except (TypeError, ValueError):
            raise InvalidSettings({kind: "Temperature must be a number."})
        if not 0 <= temperature <= 2:
            raise InvalidSettings({kind: "Temperature must be between 0 and 2."})
        stage["temperature"] = config["temperature"] = temperature
    return {**{key: value for key, value in stage.items() if value != ""}, "config": config}


def save_settings(root, data: dict) -> VoiceAgentSettings:
    """Validate and store the settings for ``root``. Only the stages the chosen model type uses
    are validated and replaced; the others keep what was saved before."""
    errors = {}
    model_type = data.get("model_type", VoiceAgentSettings.ModelType.STANDARD)
    if model_type not in VoiceAgentSettings.ModelType.values:
        raise InvalidSettings({"model_type": "Choose standard or realtime."})
    first_message = str(data.get("first_message") or "").strip()
    system_prompt = str(data.get("system_prompt") or "").strip()
    if len(system_prompt) > max_system_prompt():
        errors["system_prompt"] = f"Keep the prompt under {max_system_prompt()} characters."

    stages = {}
    for kind in STAGES_BY_MODEL_TYPE[model_type]:
        try:
            stages[kind] = resolve_stage(kind, data.get(kind))
        except InvalidSettings as exc:
            errors.update(exc.errors)
    saved = VoiceAgentSettings.objects.filter(clinic=root).first()
    current = {name: getattr(saved, name) for name in CALL_FIELDS} if saved else None
    try:
        calls = resolve_call_settings(data, current)
    except InvalidSettings as exc:
        errors.update(exc.errors)
    if errors:
        raise InvalidSettings(errors)

    record, _ = VoiceAgentSettings.objects.get_or_create(clinic=root)
    # The built-in texts are stored as blank so that later improvements to them still apply.
    record.first_message = "" if first_message == DEFAULT_FIRST_MESSAGE else first_message
    record.system_prompt = "" if system_prompt == DEFAULT_SYSTEM_PROMPT else system_prompt
    record.model_type = model_type
    for kind, stage in stages.items():
        setattr(record, kind, stage)
    for name, value in calls.items():
        setattr(record, name, value)
    record.save()
    return record


def settings_payload(root) -> dict:
    """What the settings page shows (stages without the provider-side ``config``)."""
    current = effective_settings(root)
    payload = {key: current[key] for key in ("first_message", "system_prompt", "model_type", "is_customised", *CALL_FIELDS)}
    payload["call_defaults"] = dict(CALL_DEFAULTS)
    payload["listening_options"] = [{"id": key, "label": label} for key, label in LISTENING_LABELS.items()]
    payload["inbound_number"] = inbound.describe()
    for kind in DEFAULT_STAGES:
        payload[kind] = {key: value for key, value in current[kind].items() if key != "config"}
    payload["defaults"] = {"first_message": DEFAULT_FIRST_MESSAGE, "system_prompt": DEFAULT_SYSTEM_PROMPT}
    payload["limits"] = {"system_prompt": max_system_prompt(), "system_prompt_long": LONG_SYSTEM_PROMPT}
    payload["owner_clinic"] = {"id": root.id, "name": root.name}
    payload["catalog_available"] = providers.is_configured()
    payload["preview_providers"] = list(providers.PREVIEW_PROVIDERS)
    return payload
