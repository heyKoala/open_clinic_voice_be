from django.db import transaction
from django.utils import timezone
import django_filters
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters as rest_filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination

from common.api import ClinicScopedModelViewSet
from common.audit import log_data_change
from accounts.permissions import IsDoctorOnly, IsDoctorOrReceptionistOnly, IsReceptionistOnly, IsClinicAdmin
from queue_mgmt.models import QueueToken
from queue_mgmt.serializers import QueueTokenSerializer
from queue_mgmt.services import issue_queue_token

from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
import logging

logger = logging.getLogger(__name__)

def broadcast_queue_update(doctor_id, event_type, payload):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            f"doctor_queue_{doctor_id}",
            {
                "type": "queue_message",  # Must match the method in consumer
                "event": event_type,
                "payload": payload
            }
        )
    except Exception:
        # Real-time updates are best-effort; never fail the request that triggered them.
        logger.exception("Failed to broadcast %s for doctor %s", event_type, doctor_id)


class QueueTokenFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name="status", lookup_expr="exact")
    service_date_after = django_filters.DateFilter(field_name="service_date", lookup_expr="gte")
    service_date_before = django_filters.DateFilter(field_name="service_date", lookup_expr="lte")
    patient = django_filters.NumberFilter(field_name="patient_id")
    doctor = django_filters.NumberFilter(field_name="doctor_id")
    is_active = django_filters.BooleanFilter(field_name="is_active")

    class Meta:
        model = QueueToken
        fields = ["status", "patient", "doctor", "is_active"]


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class QueueTokenViewSet(ClinicScopedModelViewSet):
    queryset = QueueToken.objects.select_related("patient", "doctor", "appointment")
    serializer_class = QueueTokenSerializer
    permission_classes = [IsClinicAdmin | IsDoctorOrReceptionistOnly]
    filterset_class = QueueTokenFilter
    search_fields = ["patient__full_name", "doctor__full_name"]
    ordering_fields = ["service_date", "token_number", "status", "created_at"]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, rest_filters.SearchFilter, rest_filters.OrderingFilter]

    def get_permissions(self):
        if self.action in {"check_in", "call_next"}:
            return [(IsClinicAdmin | IsReceptionistOnly)()]
        if self.action == "mark_seen":
            return [IsDoctorOnly()]
        return super().get_permissions()

    def perform_create(self, serializer):
        serializer.instance = issue_queue_token(clinic=self.request.clinic, **serializer.validated_data)
        log_data_change(serializer.instance, "create", request=self.request)
        broadcast_queue_update(serializer.instance.doctor_id, "queue.created", self.get_serializer(serializer.instance).data)

    def get_queryset(self):
        queryset = super().get_queryset()
        service_date = self.request.query_params.get("service_date")
        if service_date:
            queryset = queryset.filter(service_date=service_date)
        # If the user is a doctor, restrict to their own queue tokens
        if self.request.user.role == 'doctor':
            doctor = getattr(self.request.user, 'doctor_profile', None)
            if not doctor:
                return queryset.none()
            return queryset.filter(doctor=doctor)
        return queryset

    def perform_destroy(self, instance):
        # Soft delete: set is_active=False
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at", "changed_by"])
        # Log the deletion
        log_data_change(instance, "delete", request=self.request)

    @action(detail=True, methods=["post"])
    def check_in(self, request, pk=None):
        token = self.get_object()
        if token.status != QueueToken.Status.WAITING:
            return Response({"detail": "Only waiting tokens can be checked in."}, status=409)
        token.status = QueueToken.Status.CHECKED_IN
        token.checked_in_at = timezone.now()
        token.save(update_fields=["status", "checked_in_at", "updated_at", "changed_by"])
        
        data = self.get_serializer(token).data
        broadcast_queue_update(token.doctor_id, "queue.updated", data)
        return Response(data)

    @action(detail=True, methods=["post"])
    def mark_seen(self, request, pk=None):
        token = self.get_object()
        if token.status in (QueueToken.Status.COMPLETED, QueueToken.Status.SKIPPED):
            return Response({"detail": "Token is already completed or skipped."}, status=409)
            
        notes = request.data.get('notes', '')
        if notes:
            token.notes = notes
            
        token.status, token.served_at = QueueToken.Status.COMPLETED, timezone.now()
        token.save(update_fields=["status", "served_at", "updated_at", "changed_by", "notes"])

        if token.appointment:
            token.appointment.status = 'completed'
            token.appointment.save(update_fields=['status', 'updated_at'])
            
        data = self.get_serializer(token).data
        broadcast_queue_update(token.doctor_id, "queue.completed", data)
        return Response(data)

    @action(detail=False, methods=["post"])
    def call_next(self, request):
        doctor_id = request.data.get("doctor")
        service_date = request.data.get("service_date") or timezone.localdate().isoformat()
        if not doctor_id:
            return Response({"detail": "doctor is required."}, status=400)
        with transaction.atomic():
            queryset = self.get_queryset().select_for_update(of=("self",)).filter(doctor_id=doctor_id, service_date=service_date)
            # Update currently called/in consultation to completed if any
            active = queryset.filter(status__in=[QueueToken.Status.CALLED, QueueToken.Status.IN_CONSULTATION])
            for active_token in active:
                active_token.status = QueueToken.Status.COMPLETED
                active_token.served_at = timezone.now()
                active_token.save(update_fields=["status", "served_at", "updated_at", "changed_by"])
                broadcast_queue_update(doctor_id, "queue.completed", self.get_serializer(active_token).data)
                
            next_token = queryset.filter(status__in=[QueueToken.Status.WAITING, QueueToken.Status.CHECKED_IN]).order_by("token_number").first()
            if next_token is None:
                return Response({"detail": "No waiting tokens."}, status=404)
            next_token.status = QueueToken.Status.CALLED
            next_token.save(update_fields=["status", "updated_at", "changed_by"])
            
        data = self.get_serializer(next_token).data
        broadcast_queue_update(doctor_id, "queue.updated", data)
        return Response(data)