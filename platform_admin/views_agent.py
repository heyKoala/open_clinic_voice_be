"""The platform admin's AI receptionist screens: any hospital's agent, and the template new hospitals start from.

A hospital's agent is edited through the hospital app's own views (so the rules, validation and the push of the recording
notice to its phone number are identical); the platform view fixes the hospital from the URL. A platform login also
carries ``is_staff``, so it is allowed to choose the models, which a hospital's own admins are not.
"""
from __future__ import annotations

from django.db import transaction
from django.http import HttpResponse
from rest_framework import status
from rest_framework.response import Response

from ai_agent import providers
from ai_agent import views as agent_views
from ai_agent.agent_settings import InvalidSettings, effective_settings, render, save_settings
from platform_admin import agent_template
from platform_admin.audit import log_platform_action
from platform_admin.permissions import PlatformAPIView
from platform_admin.views import get_hospital
from platform_admin.views_hospital import act_for


def changed_fields(data) -> list[str]:
    """Which settings a save sent (never their text: a prompt can hold anything)."""
    return sorted(str(key) for key in data) if hasattr(data, "keys") else []


class HospitalAgentView(PlatformAPIView):
    def get(self, request, pk):
        act_for(request, get_hospital(pk))
        return agent_views.VoiceAgentSettingsView().get(request)

    def put(self, request, pk):
        root = get_hospital(pk)
        act_for(request, root)
        response = agent_views.VoiceAgentSettingsView().put(request)
        if response.status_code == status.HTTP_200_OK:
            log_platform_action(request.user, "update_agent", root, {"fields": changed_fields(request.data)})
        return response

    def delete(self, request, pk):
        root = get_hospital(pk)
        act_for(request, root)
        response = agent_views.VoiceAgentSettingsView().delete(request)
        if response.status_code == status.HTTP_200_OK:
            log_platform_action(request.user, "reset_agent", root, {})
        return response


class HospitalAgentProvidersView(PlatformAPIView):
    def get(self, request, pk):
        get_hospital(pk)
        return agent_views.VoiceAgentProvidersView().get(request)


class HospitalAgentVoicesView(PlatformAPIView):
    def get(self, request, pk):
        get_hospital(pk)
        return agent_views.VoiceAgentVoicesView().get(request)


class HospitalAgentPreviewView(PlatformAPIView):
    def post(self, request, pk):
        act_for(request, get_hospital(pk))
        return agent_views.VoiceAgentPreviewView().post(request)


# ── the template ──────────────────────────────────────────────────────────────

class TemplateAgentView(PlatformAPIView):
    def get(self, request):
        return Response(agent_template.template_payload())

    def put(self, request):
        try:
            # Creating the row and validating the save are one step: a refused first save must leave no template behind.
            with transaction.atomic():
                record = agent_template.template_record(create=True)
                save_settings(agent_template.TEMPLATE_ROOT, request.data if isinstance(request.data, dict) else {}, can_change_models=True, target=record)
        except InvalidSettings as exc:
            return Response(exc.errors, status=status.HTTP_400_BAD_REQUEST)
        except providers.ProviderError as exc:
            return agent_views._catalog_error(exc)
        # Nothing is pushed to the voice provider: the template is not a hospital and has no phone number.
        log_platform_action(request.user, "update_agent_template", None, {"fields": changed_fields(request.data)})
        return Response(agent_template.template_payload())

    def delete(self, request):
        if agent_template.reset_template():
            log_platform_action(request.user, "reset_agent_template", None, {})
        return Response(agent_template.template_payload())


class TemplateAgentProvidersView(PlatformAPIView):
    def get(self, request):
        return agent_views.VoiceAgentProvidersView().get(request)


class TemplateAgentVoicesView(PlatformAPIView):
    def get(self, request):
        return agent_views.VoiceAgentVoicesView().get(request)


class TemplateAgentPreviewView(PlatformAPIView):
    """A spoken sample of a voice for the template, with a stand-in name where the greeting says the hospital's."""

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        provider, model, voice = (str(data.get(key) or "").strip() for key in ("provider", "model", "voice"))
        if provider not in providers.PREVIEW_PROVIDERS:
            return Response({"detail": "Voice samples are not available for this provider."}, status=status.HTTP_400_BAD_REQUEST)
        if not model or not voice:
            return Response({"detail": "Choose a model and a voice first."}, status=status.HTTP_400_BAD_REQUEST)
        spoken_root = type("Sample", (), {"name": "your hospital"})()
        current = effective_settings(agent_template.TEMPLATE_ROOT, saved=agent_template.template_record())
        text = str(data.get("text") or "").strip() or current["first_message"]
        try:
            audio = providers.synthesize(provider, model, voice, render(text, spoken_root)[:300], str(data.get("language") or "").strip())
        except providers.ProviderError as exc:
            return agent_views._catalog_error(exc)
        return HttpResponse(audio, content_type="audio/mpeg")
