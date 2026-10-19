"""Endpoints the voice worker calls during an AI reminder or feedback call (appointments.outbound).

POST /ai/webhooks/rock8/<clinic_id>/<token>/patient-call/<call_id>/          the call's script (at call start)
POST /ai/tools/rock8/<clinic_id>/<token>/patient-call/<call_id>/outcome/     the agent saves the outcome

Both carry the clinic's token in the path, like the incoming-call webhook and tools.
"""
from __future__ import annotations

import logging

from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from ai_agent.tool_views import HasRock8WebhookSecret
from appointments import journey, outbound
from appointments.models import PatientCall
from clinics.models import Clinic

logger = logging.getLogger(__name__)

# What the agent may report, mapped to the desk's own outcomes (journey.record).
AGENT_OUTCOMES = {
    "confirmed": "confirmed", "cancel": "cancel", "rescheduled": "rescheduled",
    "reschedule_later": "reschedule", "reschedule": "reschedule", "rated": "rated", "not_reached": "no_answer",
}


class _WorkerView(APIView):
    authentication_classes = []
    throttle_classes = []
    permission_classes = [HasRock8WebhookSecret]

    def get_call(self, kwargs) -> PatientCall:
        clinic = get_object_or_404(Clinic, id=kwargs["clinic_id"])
        return get_object_or_404(
            PatientCall.objects.select_related("appointment", "appointment__patient", "appointment__doctor", "clinic"),
            id=kwargs["call_id"], clinic__in=clinic.group_clinics())


def _base_url(request) -> str:
    forwarded_host = request.headers.get("x-forwarded-host")
    return f"https://{forwarded_host}" if forwarded_host else f"{request.scheme}://{request.get_host()}"


class PatientCallWebhookView(_WorkerView):
    def post(self, request, *args, **kwargs):
        call = self.get_call(kwargs)
        data = request.data if isinstance(request.data, dict) else {}
        try:
            outbound.start_log(call, str(data.get("room_name") or ""))
        except Exception:
            logger.exception("Could not start the call log for AI call %s", call.id)  # never block the call
        return Response(outbound.build_config(call, _base_url(request)))


class PatientCallOutcomeToolView(_WorkerView):
    def post(self, request, *args, **kwargs):
        call = self.get_call(kwargs)
        reported = str(request.data.get("outcome") or "").strip().lower()
        outcome = AGENT_OUTCOMES.get(reported)
        if outcome is None:
            return Response({"success": False, "error": "INVALID_OUTCOME",
                             "message": f"outcome must be one of: {', '.join(sorted(AGENT_OUTCOMES))}."})
        if call.status not in (PatientCall.Status.CALLING, PatientCall.Status.SCHEDULED):
            return Response({"success": True, "message": "The outcome of this call was already saved."})
        try:
            journey.record(call, outcome, request.data.get("rating"), str(request.data.get("comment") or ""))
        except journey.RecordError as exc:
            return Response({"success": False, "error": "INVALID", "message": str(exc)})
        message = {
            "cancel": "Saved. The appointment is cancelled.",
            "reschedule": "Saved. The hospital will call the patient back to fix a new time.",
            "no_answer": "Saved. The hospital will try again later.",
        }.get(outcome, "Saved.")
        return Response({"success": True, "message": message})
