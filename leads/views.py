from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.authentication import PublicEndpointAuthentication
from accounts.permissions import PublicEndpointPermission
from leads.models import Lead
from leads.serializers import LeadSerializer
from leads.throttles import LeadThrottle

logger = logging.getLogger(__name__)

THANKS = {"detail": "Thanks. We'll be in touch within 24 hours."}


def _notify_team(lead: Lead) -> bool:
    """Email the lead to our team. Returns whether it went out; a failure never loses the lead."""
    body = (
        f"Name: {lead.name}\n"
        f"Email: {lead.email}\n"
        f"Phone: {lead.phone}\n"
        f"Hospital / clinic: {lead.organisation or '-'}\n"
        f"Source: {lead.source}\n"
    )
    try:
        send_mail(
            subject=f"New Slot enquiry: {lead.organisation or lead.name}",
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[settings.LEAD_NOTIFY_EMAIL],
        )
    except Exception:
        logger.exception("Could not email the lead notification for lead %s", lead.pk)
        return False
    return True


class LeadCreateView(APIView):
    """Public: the "Talk to us" form on the Slot website."""

    authentication_classes = [PublicEndpointAuthentication]
    permission_classes = [PublicEndpointPermission]
    throttle_classes = [LeadThrottle]

    def post(self, request):
        # A hidden field real visitors never fill in: bots do. Answer as if it worked, and save nothing.
        if str(request.data.get("website", "")).strip():
            return Response(THANKS, status=status.HTTP_201_CREATED)

        serializer = LeadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        lead = Lead.objects.create(**serializer.validated_data)
        lead.notified = _notify_team(lead)
        lead.save(update_fields=["notified"])
        return Response(THANKS, status=status.HTTP_201_CREATED)
