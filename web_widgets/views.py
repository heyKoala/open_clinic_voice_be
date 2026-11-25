"""The hospital admin's side of the website widget: create, edit, switch off, new key, and its numbers."""
from django.db.models import Count, Q, Sum
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response

from accounts.permissions import IsAdminOrDoctor, IsClinicAdmin
from ai_agent.models import EMPTY_CALL, CallLog
from clinics.views import _owned_root
from common.audit import log_data_change
from web_widgets.models import WebWidget
from web_widgets.serializers import WebWidgetSerializer

NOT_OWNER = "Only the main clinic's admins can manage the website widget."


class WebWidgetViewSet(viewsets.ModelViewSet):
    """/api/v1/widgets/: the widgets of the signed-in admin's hospital. A widget belongs to the main clinic and
    is shared by its centres, like the phone number and the agent."""

    serializer_class = WebWidgetSerializer
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_permissions(self):
        # Doctors can look (they can already use the Test call); only admins change a widget.
        return [IsAdminOrDoctor()] if self.request.method in SAFE_METHODS else [IsClinicAdmin()]

    def _root(self):
        root = _owned_root(self.request)
        if root is None:
            raise PermissionDenied(NOT_OWNER)
        return root

    def get_queryset(self):
        return WebWidget.objects.filter(clinic=self._root())

    def perform_create(self, serializer):
        log_data_change(serializer.save(clinic=self._root()), "create", request=self.request)

    def perform_update(self, serializer):
        log_data_change(serializer.save(), "update", request=self.request)

    def perform_destroy(self, instance):
        # Its past calls stay in the call logs (still marked as website calls), without the widget link.
        log_data_change(instance, "delete", request=self.request)
        instance.delete()

    @action(detail=True, methods=["post"], url_path="rotate-key")
    def rotate_key(self, request, pk=None):
        """A new public key. The old one stops starting calls at once (calls in progress carry on), so the
        website must be given the new snippet."""
        widget = self.get_object()
        widget.rotate_public_key()
        log_data_change(widget, "update", request=request)
        return Response(self.get_serializer(widget).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"])
    def stats(self, request, pk=None):
        """Calls and minutes from this widget, counted like billing counts (calls nobody spoke on left out)."""
        calls = CallLog.objects.filter(widget=self.get_object()).exclude(EMPTY_CALL)
        totals = calls.aggregate(
            total_calls=Count("id"),
            seconds=Sum("duration_seconds"),
            booked=Count("id", filter=Q(outcome="appointment_booked")),
        )
        return Response({
            "total_calls": totals["total_calls"],
            "total_minutes": round((totals["seconds"] or 0) / 60, 1),
            "appointments_booked": totals["booked"],
        })
