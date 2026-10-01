"""The AI receptionist's configurable settings: greeting, instructions and the models it runs on.

Stored per main clinic in ``VoiceAgentSettings``; a clinic without a row runs on the defaults below.
Model choices are validated against the voice provider's catalog when they are saved, and stored
together with the provider's own config keys so that answering a call never needs the catalog.
"""
from __future__ import annotations

from ai_agent import providers
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
    "3. Before calling book_appointment, repeat the doctor, centre, date, time and patient name back to the caller and get a yes."
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
MAX_FIRST_MESSAGE = 500
MAX_SYSTEM_PROMPT = 20000


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
    return {
        "first_message": (saved.first_message if saved else "") or DEFAULT_FIRST_MESSAGE,
        "system_prompt": (saved.system_prompt if saved else "") or DEFAULT_SYSTEM_PROMPT,
        "model_type": model_type,
        "is_customised": saved is not None,
        **stages,
    }


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
    if len(first_message) > MAX_FIRST_MESSAGE:
        errors["first_message"] = f"Keep the greeting under {MAX_FIRST_MESSAGE} characters."
    if len(system_prompt) > MAX_SYSTEM_PROMPT:
        errors["system_prompt"] = f"Keep the prompt under {MAX_SYSTEM_PROMPT} characters."

    stages = {}
    for kind in STAGES_BY_MODEL_TYPE[model_type]:
        try:
            stages[kind] = resolve_stage(kind, data.get(kind))
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
    record.save()
    return record


def settings_payload(root) -> dict:
    """What the settings page shows (stages without the provider-side ``config``)."""
    current = effective_settings(root)
    payload = {key: current[key] for key in ("first_message", "system_prompt", "model_type", "is_customised")}
    for kind in DEFAULT_STAGES:
        payload[kind] = {key: value for key, value in current[kind].items() if key != "config"}
    payload["defaults"] = {"first_message": DEFAULT_FIRST_MESSAGE, "system_prompt": DEFAULT_SYSTEM_PROMPT}
    payload["owner_clinic"] = {"id": root.id, "name": root.name}
    payload["catalog_available"] = providers.is_configured()
    payload["preview_providers"] = list(providers.PREVIEW_PROVIDERS)
    return payload
