import logging
import mimetypes

import django_filters
import requests
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters as rest_filters
from rest_framework.pagination import PageNumberPagination

from ai_agent.models import AgentConfiguration, CallLog
from ai_agent.serializers import AgentConfigurationSerializer, CallLogSerializer
from common.api import ClinicScopedModelViewSet
from accounts.permissions import IsClinicAdmin, IsAdminOrDoctor
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework.decorators import action
from clinics.models import Clinic
from ai_agent.tool_views import HasRock8WebhookSecret, rock8_path, with_rock8_token
from ai_agent.call_events import is_call_event, match_patient, record_call_event
from ai_agent.call_sessions import caller_number, start_call_log
from ai_agent.routing import clinic_for_number, dialled_number

logger = logging.getLogger(__name__)

def _base_url(request) -> str:
    forwarded_host = request.headers.get('x-forwarded-host')
    if forwarded_host:
        return f"https://{forwarded_host}"
    return getattr(settings, 'BACKEND_URL', f"{request.scheme}://{request.get_host()}")


def build_agent_config(clinic, request, caller: str = "") -> dict:
    """Agent configuration for a call to ``clinic``'s phone number.

    Centres share their main clinic's number, so the agent represents the whole group (the main
    clinic and its centres) and nothing else; the tools are scoped to the same group.
    """
    from django.utils import timezone

    root = clinic.root
    base_url = _base_url(request)
    current_date = timezone.now().strftime('%B %d, %Y (%A)')

    system_prompt = (
        f"You are a helpful medical receptionist for {root.name} handling incoming calls from patients. "
        f"CRITICAL CONTEXT: Today's date is {current_date}. Always base your date calculations (like 'tomorrow' or 'next week') on this exact date. "
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
    if caller:
        known = match_patient(root, caller)
        system_prompt += (
            f"\n\nCALLER: This call is from +{caller}. Use it as patient_phone unless the caller asks to use a "
            "different number (read it back to confirm)."
        )
        if known:
            system_prompt += (
                f" This number belongs to an existing patient named {known.full_name}; confirm with "
                f"\"Am I speaking with {known.full_name}?\" and book for another name only if they say so."
            )
    else:
        system_prompt += "\n\nCALLER: The caller's number is unknown; ask for their phone number and read it back to confirm."

    centres = list(root.group_clinics())
    if len(centres) > 1:
        system_prompt += (
            f"\n\n{root.name} has {len(centres)} centres, all reached through this phone number. "
            "Each doctor works at one centre (the doctor tools say which). When booking, tell the "
            "patient which centre the appointment is at, and ask which centre suits them if they have not said."
        )
    for centre in centres:
        system_prompt += f"\n\nCENTRE: {centre.name}\n"
        system_prompt += f"- Address: {centre.address or 'Not provided'}\n"
        if centre.description:
            system_prompt += f"- About: {centre.description}\n"
        if centre.facilities:
            system_prompt += f"- Facilities & amenities (parking, accessibility, etc): {centre.facilities}\n"
        if centre.holiday_calendar:
            system_prompt += f"- Holidays & closures: {centre.holiday_calendar}\n"
        system_prompt += f"- Phone: {centre.phone or 'Not provided'}\n"
        system_prompt += f"- Support Email: {centre.support_email or 'Not provided'}\n"
        system_prompt += f"- Website: {centre.website or 'Not provided'}\n"
        system_prompt += f"- Registration/License Number: {centre.registration_number or 'Not provided'}\n"

    def tool_url(name):
        # Token in the path: the provider appends its own query parameters for GET tools.
        return f"{base_url}/api/v1/ai/tools/rock8/{rock8_path(root.id, name)}"

    tools = [
        {
            "name": "get_available_slots",
            "description": "Fetch available time slots for a given doctor and date.",
            "method": "GET",
            "url": tool_url("slots"),
            "parameters": [
                {"name": "doctor_id", "type": "string", "description": "The ID of the doctor", "location": "query", "required": True},
                {"name": "date", "type": "string", "description": "The date in YYYY-MM-DD format", "location": "query", "required": True}
            ]
        },
        {
            "name": "book_appointment",
            "description": "Book a new appointment. It is booked at the centre where the doctor works.",
            "method": "POST",
            "url": tool_url("book"),
            "parameters": [
                {"name": "doctor_id", "type": "string", "description": "The ID of the doctor", "location": "body", "required": True},
                {"name": "date", "type": "string", "description": "The date in YYYY-MM-DD", "location": "body", "required": True},
                {"name": "time", "type": "string", "description": "The time of the appointment", "location": "body", "required": True},
                {"name": "patient_phone", "type": "string", "description": "Patient's phone number", "location": "body", "required": True},
                {"name": "patient_name", "type": "string", "description": "Patient's full name", "location": "body", "required": True}
            ]
        },
        {
            "name": "cancel_appointment",
            "description": "Cancel an existing appointment.",
            "method": "POST",
            "url": tool_url("cancel"),
            "parameters": [
                {"name": "appointment_id", "type": "string", "description": "The ID of the appointment", "location": "body", "required": True}
            ]
        },
        {
            "name": "list_doctors",
            "description": "Get a list of all doctors, with the centre each one works at.",
            "method": "GET",
            "url": tool_url("doctors"),
            "parameters": []
        },
        {
            "name": "lookup_appointments",
            "description": "Look up existing appointments for a patient by their name or phone number.",
            "method": "GET",
            "url": tool_url("appointments"),
            "parameters": [
                {"name": "patient_name", "type": "string", "description": "The full or partial name of the patient", "location": "query", "required": False},
                {"name": "patient_phone", "type": "string", "description": "The phone number of the patient", "location": "query", "required": False}
            ]
        }
    ]

    # Map the frontend's 'sarvam_x' to the actual Sarvam speaker ID
    # The frontend uses 'sarvam_shubh', 'sarvam_ritu', etc.
    # Sarvam's bulbul:v3 expects 'shubh', 'ritu', 'simran', 'kavya', 'ratan'.
    sarvam_speaker = "simran"  # default fallback
    try:
        if hasattr(root, 'configuration') and root.configuration.ai_voice_type and root.configuration.ai_voice_type.startswith("sarvam_"):
            sarvam_speaker = root.configuration.ai_voice_type.replace("sarvam_", "")
    except Exception:
        pass

    return {
        "system_prompt": system_prompt,
        "first_message": f"Welcome to {root.name}. How can I help you today?",
        "model_type": "standard",
        "stt_config": {
            "provider": "sarvam",
            "config": {
                "model": "saaras:v3",
                "language": "unknown"
            }
        },
        "llm_config": {
            "provider": "gemini",
            "config": {
                "model": "gemini-3.1-flash-lite",
                "temperature": 0.3
            }
        },
        "tts_config": {
            "provider": "sarvam",
            "config": {
                "model": "bulbul:v3",
                "speaker": sarvam_speaker,
                "target_language_code": "en-IN"
            }
        },
        "tools": tools,
        "thinking_sound": True,
        "max_silence_seconds": 120,
        "background_sound": False,
        "vad_config": {
            "min_silence_duration": 0.45,
            "activation_threshold": 0.4,
            "min_speech_duration": 0.05
        }
    }


def _handle_webhook(clinic, request):
    # Call lifecycle events (call ended, analysis completed, ...) are recorded as CallLogs;
    # any other request is the call-start request asking for the agent configuration.
    data = request.data if isinstance(request.data, dict) else {}
    logger.info("Voice webhook for clinic %s: keys=%s room=%s", clinic.id, sorted(data), data.get("room_name"))
    if is_call_event(request.data):
        try:
            log = record_call_event(clinic, request.data)
        except Exception:
            # 5xx makes the provider retry; recording is idempotent per call id.
            logger.exception("Failed to record call event for clinic %s", clinic.id)
            return Response({"error": "Failed to record call event."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({"status": "recorded", "call_log_id": log.id if log else None}, status=status.HTTP_200_OK)
    caller = caller_number(data)
    try:
        # Inbound numbers never send "call ended" events, so the log is started here.
        start_call_log(clinic, data)
    except Exception:
        logger.exception("Could not start the call log for clinic %s", clinic.id)  # never block the call
    return Response(build_agent_config(clinic, request, caller))


class Rock8WebhookView(APIView):
    """Per-clinic webhook: /api/v1/ai/webhooks/rock8/<clinic_id>/ (clinic token or global secret).
    Kept for existing provider configurations and the in-app test call."""
    authentication_classes = []
    # Server-to-server calls authenticated by secret; the per-IP anonymous throttle would
    # make every concurrent call from the provider share one 30/min budget.
    throttle_classes = []
    permission_classes = [HasRock8WebhookSecret]

    def post(self, request, *args, **kwargs):
        clinic = get_object_or_404(Clinic, id=kwargs.get('clinic_id'))
        return _handle_webhook(clinic, request)


class Rock8NumberWebhookView(APIView):
    """One webhook for every clinic: /api/v1/ai/webhooks/rock8/ (global secret only).

    The clinic is found from the number the patient dialled, so a number only ever reveals its own
    clinic group. Unknown numbers are rejected rather than falling back to some clinic.
    """
    authentication_classes = []
    throttle_classes = []
    permission_classes = [HasRock8WebhookSecret]

    def post(self, request, *args, **kwargs):
        dialled = dialled_number(request.data, request.query_params)
        clinic = clinic_for_number(dialled)
        if clinic is None:
            logger.warning("Voice webhook for unknown number %r", dialled)
            return Response({"error": "UNKNOWN_NUMBER", "message": "This number is not assigned to any clinic."},
                            status=status.HTTP_404_NOT_FOUND)
        return _handle_webhook(clinic, request)


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class AgentConfigurationFilter(django_filters.FilterSet):
    purpose = django_filters.CharFilter(field_name="purpose", lookup_expr="icontains")
    language = django_filters.CharFilter(field_name="language", lookup_expr="exact")
    is_active = django_filters.BooleanFilter(field_name="is_active")

    class Meta:
        model = AgentConfiguration
        fields = ["purpose", "language", "is_active"]


class AgentConfigurationViewSet(ClinicScopedModelViewSet):
    queryset = AgentConfiguration.objects.all()
    serializer_class = AgentConfigurationSerializer
    permission_classes = [IsClinicAdmin]
    filterset_class = AgentConfigurationFilter
    search_fields = ["purpose", "first_greeting"]
    ordering_fields = ["purpose", "language", "created_at"]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, rest_filters.SearchFilter, rest_filters.OrderingFilter]


class CallLogFilter(django_filters.FilterSet):
    direction = django_filters.CharFilter(field_name="direction", lookup_expr="exact")
    language = django_filters.CharFilter(field_name="language", lookup_expr="exact")
    occurred_at_after = django_filters.DateTimeFilter(field_name="occurred_at", lookup_expr="gte")
    occurred_at_before = django_filters.DateTimeFilter(field_name="occurred_at", lookup_expr="lte")
    patient = django_filters.NumberFilter(field_name="patient_id")

    class Meta:
        model = CallLog
        fields = ["direction", "language", "patient"]


class CallLogViewSet(ClinicScopedModelViewSet):
    queryset = CallLog.objects.select_related("patient")
    serializer_class = CallLogSerializer
    permission_classes = [IsAdminOrDoctor]
    filterset_class = CallLogFilter
    search_fields = ["agent_name", "outcome", "transcript"]
    ordering_fields = ["occurred_at", "duration_seconds", "created_at"]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, rest_filters.SearchFilter, rest_filters.OrderingFilter]

    @action(detail=True, methods=["get"])
    def recording(self, request, pk=None):
        """Stream the stored copy of the call recording (the provider's own links expire)."""
        log = self.get_object()
        if not log.recording_file:
            raise Http404("No stored recording for this call.")
        content_type = mimetypes.guess_type(log.recording_file.name)[0] or "audio/mpeg"
        return FileResponse(log.recording_file.open("rb"), content_type=content_type)


class Rock8StartWebCallView(APIView):
    permission_classes = [IsAdminOrDoctor]
    
    def get(self, request, *args, **kwargs):
        api_key = getattr(settings, 'ROCK8_API_KEY', None)
        if not api_key:
            return Response({"error": "ROCK8_API_KEY is not configured."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
        clinic = getattr(request.user, 'clinic', None)
        if not clinic:
            return Response({"error": "User does not belong to a clinic."}, status=status.HTTP_400_BAD_REQUEST)
            
        participant_name = request.query_params.get("participant_name", "Test User")
        participant_identity = request.query_params.get("participant_identity", "test_user_123")
        
        base_url = getattr(settings, 'BACKEND_URL', f"{request.scheme}://{request.get_host()}")
        
        # If frontend sends a base URL from ngrok/localtunnel, use it
        override_base_url = request.query_params.get("webhook_url", None)
        if override_base_url:
            # If they just pasted the domain (e.g. https://xxx.lhr.life)
            if not override_base_url.endswith("/api/v1/ai/webhooks/rock8/") and "webhooks" not in override_base_url:
                override_base_url = override_base_url.rstrip("/")
                webhook_url = f"{override_base_url}/api/v1/ai/webhooks/rock8/{clinic.id}/"
            else:
                webhook_url = override_base_url
        else:
            webhook_url = f"{base_url}/api/v1/ai/webhooks/rock8/{clinic.id}/"
            
        if "token=" not in webhook_url:
            # Token in the path (the provider may not pass query strings through).
            if webhook_url.endswith(f"/rock8/{clinic.id}/"):
                webhook_url = webhook_url[: -len(f"{clinic.id}/")] + rock8_path(clinic.id)
            else:
                webhook_url = with_rock8_token(webhook_url, clinic.id)
        
        payload = {
            "participant_name": participant_name,
            "participant_identity": participant_identity,
            "webhook_url": webhook_url
        }
        
        headers = {
            "accept": "application/json",
            "X-API-Key": api_key,
            "Content-Type": "application/json"
        }
        
        try:
            response = requests.post("https://worker.heykoala.ai/webCall", json=payload, headers=headers, timeout=20)
            response.raise_for_status()
            return Response(response.json(), status=status.HTTP_200_OK)
        except requests.exceptions.RequestException as e:
            err_msg = str(e)
            if hasattr(e, 'response') and e.response is not None:
                err_msg = e.response.text
            return Response({"error": f"Failed to start web call: {err_msg}"}, status=status.HTTP_502_BAD_GATEWAY)