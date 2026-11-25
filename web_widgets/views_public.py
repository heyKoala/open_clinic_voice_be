"""The two endpoints the widget script calls from the hospital's website. No login: the widget is named by its
public key, and only the websites on its list may use it (checked here, on the server, from the Origin header).
They answer with plain JSON and their own CORS headers."""
from __future__ import annotations

import logging
import secrets

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from rest_framework import permissions
from rest_framework.views import APIView

from ai_agent.tool_views import rock8_path
from ai_agent.web_call import WebCallError, public_base_url, start_web_call
from web_widgets.models import WebWidget
from web_widgets.throttles import PublicWidgetConfigThrottle, PublicWidgetTokenThrottle
from web_widgets.utils import apply_widget_cors, validate_origin

logger = logging.getLogger(__name__)


def _widget(public_key: str) -> WebWidget | None:
    return WebWidget.objects.filter(public_key=public_key, is_active=True).select_related("clinic").first()


def _refused(request, public_key):
    """The widget if this website may use it, else the response to send instead."""
    if not getattr(settings, "WIDGETS_PUBLIC_ENABLED", True):
        return None, JsonResponse({"detail": "Widget endpoints are disabled."}, status=503)
    widget = _widget(public_key)
    if widget is None:
        return None, JsonResponse({"detail": "Widget not found."}, status=404)
    if not validate_origin(widget, request):
        return None, JsonResponse({"detail": "Origin not allowed."}, status=403)
    return widget, None


def _out_of_credits(root) -> bool:
    """Whether new widget calls are refused for lack of credits: only when that is switched on (for everyone
    with WIDGET_CALLS_REQUIRE_CREDITS, or for this hospital with its account's hard stop) and it is being charged."""
    from platform_admin.credits import peek_account

    account = peek_account(root)
    if account is None or account.charging_from is None:
        return False
    if not (getattr(settings, "WIDGET_CALLS_REQUIRE_CREDITS", False) or account.hard_stop):
        return False
    return account.balance <= 0


class _PublicWidgetView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def options(self, request, public_key, *args, **kwargs):
        # CORS preflight. An unknown key and a site that is not allowed look the same: no headers.
        response = HttpResponse(status=204)
        widget = _widget(public_key)
        if widget is not None and validate_origin(widget, request):
            apply_widget_cors(response, request, widget)
        return response


class PublicWidgetConfigView(_PublicWidgetView):
    """What the script needs to draw the button. Nothing about the hospital beyond what the page shows."""

    throttle_classes = [PublicWidgetConfigThrottle]

    def get(self, request, public_key):
        widget, refusal = _refused(request, public_key)
        if refusal is not None:
            return refusal
        return apply_widget_cors(JsonResponse({
            "name": widget.name,
            "branding": widget.resolved_branding,
            "mode": widget.mode,
            "show_text_input": widget.show_text_input,
            "greeting_message": widget.greeting_message,
            "greeting_audio_url": "",
        }), request, widget)


class PublicWidgetTokenView(_PublicWidgetView):
    """Start a call: returns the LiveKit token and server address for the visitor's browser."""

    throttle_classes = [PublicWidgetTokenThrottle]

    def post(self, request, public_key):
        widget, refusal = _refused(request, public_key)
        if refusal is not None:
            return refusal
        root = widget.clinic.root
        if root.is_blocked:
            return apply_widget_cors(JsonResponse({"detail": "This service is not available right now."}, status=403), request, widget)
        if _out_of_credits(root):
            return apply_widget_cors(JsonResponse({"detail": "This service is not available right now."}, status=402), request, widget)

        data = request.data if isinstance(request.data, dict) else {}
        session_id = str(data.get("session_id") or "")[:64] or "sess_" + secrets.token_urlsafe(12)
        webhook_url = f"{public_base_url(request)}/api/v1/ai/webhooks/rock8/{rock8_path(root.id, 'widget', str(widget.id))}"
        try:
            # A fresh identity per call: the worker names the room after it, and the room is the call log's id.
            payload = start_web_call(root, webhook_url, "Website visitor", f"widget_{secrets.token_hex(6)}")
        except WebCallError as exc:
            logger.warning("Widget %s could not start a call: %s", widget.id, exc.message)
            return apply_widget_cors(JsonResponse({"detail": "The voice service could not be reached."}, status=502), request, widget)
        payload.setdefault("session_id", session_id)
        return apply_widget_cors(JsonResponse(payload), request, widget)
