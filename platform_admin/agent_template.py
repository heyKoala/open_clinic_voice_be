"""The AI receptionist template: what every NEW hospital's agent starts as.

It is a ``VoiceAgentSettings`` row that belongs to no hospital (so it is validated, stored and shown by exactly the same
code as a real hospital's settings), linked from ``PlatformDefaults.agent_template``. Creating a hospital copies it into
the new hospital's own row; after that the two are independent, so editing the template never changes a hospital that
already exists. It is never pushed to the voice provider: it is only a starting point.
"""
from __future__ import annotations

from types import SimpleNamespace

from ai_agent.agent_settings import settings_payload
from ai_agent.models import VoiceAgentSettings
from platform_admin.models import PlatformDefaults

# Stands in for "the hospital" while the template is edited. Its name is the placeholder itself, so anything written
# with {clinic_name} reads naturally and is filled in with each hospital's real name when it is used.
TEMPLATE_ROOT = SimpleNamespace(id=None, name="{clinic_name}", configuration=None)

# What is copied to a new hospital: every setting, none of the bookkeeping.
NOT_COPIED = {"id", "clinic", "created_at", "updated_at", "changed_by"}


def template_record(create: bool = False) -> VoiceAgentSettings | None:
    defaults = PlatformDefaults.load()
    if defaults.agent_template_id:
        return defaults.agent_template
    if not create:
        return None
    record = VoiceAgentSettings.objects.create(clinic=None)
    defaults.agent_template = record
    defaults.save(update_fields=["agent_template"])
    return record


def reset_template() -> bool:
    """Back to the built-in defaults. Returns whether there was a customised template."""
    record = template_record()
    if record is None:
        return False
    record.delete()  # the link on PlatformDefaults is cleared by the database (SET_NULL)
    return True


def template_payload() -> dict:
    payload = settings_payload(TEMPLATE_ROOT, saved=template_record())
    payload["owner_clinic"] = {"id": None, "name": "New hospitals"}
    payload["can_change_models"] = True
    return payload


def copy_into(clinic) -> bool:
    """Give a new hospital its own copy of the template. Does nothing (the built-in defaults apply) if there is none."""
    template = template_record()
    if template is None:
        return False
    fields = {f.name: getattr(template, f.name) for f in VoiceAgentSettings._meta.concrete_fields if f.name not in NOT_COPIED}
    VoiceAgentSettings.objects.update_or_create(clinic=clinic, defaults=fields)
    return True
