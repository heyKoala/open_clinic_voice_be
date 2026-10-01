import django_filters
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters as rest_filters
from rest_framework.pagination import PageNumberPagination

from appointments.models import Appointment
from appointments.serializers import AppointmentSerializer
from common.api import ClinicScopedModelViewSet
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.utils.dateparse import parse_datetime


def _parse_walkin_params(data):
    """Parse starts_at/duration_minutes; returns (starts_at, duration, error)."""
    starts_at = parse_datetime(str(data.get("starts_at") or ""))
    if starts_at is None:
        return None, None, "starts_at must be an ISO-8601 datetime."
    if timezone.is_naive(starts_at):
        starts_at = timezone.make_aware(starts_at)
    try:
        duration = int(data.get("duration_minutes", 15))
    except (TypeError, ValueError):
        return None, None, "duration_minutes must be an integer."
    if not 1 <= duration <= 480:
        return None, None, "duration_minutes must be between 1 and 480."
    return starts_at, duration, None


class AppointmentFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name="status", lookup_expr="exact")
    source = django_filters.CharFilter(field_name="source", lookup_expr="exact")
    starts_at_after = django_filters.DateTimeFilter(field_name="starts_at", lookup_expr="gte")
    starts_at_before = django_filters.DateTimeFilter(field_name="starts_at", lookup_expr="lte")
    ends_at_after = django_filters.DateTimeFilter(field_name="ends_at", lookup_expr="gte")
    ends_at_before = django_filters.DateTimeFilter(field_name="ends_at", lookup_expr="lte")
    patient = django_filters.NumberFilter(field_name="patient_id")
    doctor = django_filters.NumberFilter(field_name="doctor_id")

    class Meta:
        model = Appointment
        fields = ["status", "source", "patient", "doctor"]


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 5000


class AppointmentViewSet(ClinicScopedModelViewSet):
    queryset = Appointment.objects.filter(is_active=True).select_related("patient", "doctor", "doctor__user")
    serializer_class = AppointmentSerializer
    filterset_class = AppointmentFilter
    search_fields = ["patient__full_name", "doctor__full_name", "reason"]
    ordering_fields = ["starts_at", "status", "created_at"]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, rest_filters.SearchFilter, rest_filters.OrderingFilter]

    def get_queryset(self):
        queryset = super().get_queryset()
        # If the user is a doctor, restrict to their own appointments
        if self.request.user.role == 'doctor':
            doctor = getattr(self.request.user, 'doctor_profile', None)
            if not doctor:
                return queryset.none()
            return queryset.filter(doctor=doctor)
        return queryset

    @action(detail=True, methods=["post"])
    def check_in(self, request, pk=None):
        appointment = self.get_object()
        if appointment.status != 'scheduled':
            return Response({"detail": "Only scheduled appointments can be checked in."}, status=400)

        from django.db import transaction
        from queue_mgmt.models import QueueToken
        from queue_mgmt.serializers import QueueTokenSerializer
        from queue_mgmt.services import issue_queue_token
        from queue_mgmt.views import broadcast_queue_update

        if QueueToken.objects.filter(appointment=appointment).exists():
            return Response({"detail": "This appointment already has a queue token."}, status=409)

        with transaction.atomic():
            token = issue_queue_token(
                clinic=appointment.clinic,
                appointment=appointment,
                patient=appointment.patient,
                doctor=appointment.doctor,
                service_date=timezone.localdate(),
                status=QueueToken.Status.WAITING,
            )
            appointment.status = 'checked_in'
            appointment.save(update_fields=['status', 'updated_at'])

        broadcast_queue_update(appointment.doctor_id, "queue.created", QueueTokenSerializer(token).data)

        return Response({"detail": "Checked in successfully."})

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        appointment = self.get_object()
        if appointment.status not in ('scheduled', 'checked_in'):
            return Response({"detail": f"Cannot complete an appointment that is {appointment.status}."}, status=400)
        appointment.status = 'completed'
        appointment.save(update_fields=['status', 'updated_at'])

        from queue_mgmt.models import QueueToken
        token = QueueToken.objects.filter(appointment=appointment).exclude(
            status__in=[QueueToken.Status.COMPLETED, QueueToken.Status.SKIPPED]
        ).first()
        if token:
            token.status, token.served_at = QueueToken.Status.COMPLETED, timezone.now()
            token.save(update_fields=["status", "served_at", "updated_at"])
            from queue_mgmt.serializers import QueueTokenSerializer
            from queue_mgmt.views import broadcast_queue_update
            broadcast_queue_update(token.doctor_id, "queue.completed", QueueTokenSerializer(token).data)

        notes = request.data.get('notes', '')
        if notes:
            from clinical.models import ClinicalEncounter
            ClinicalEncounter.objects.create(
                clinic=appointment.clinic,
                patient=appointment.patient,
                doctor=appointment.doctor,
                appointment=appointment,
                chief_complaint=notes,
                status=ClinicalEncounter.EncounterStatus.COMPLETED
            )

        return Response({"detail": "Appointment marked over and details saved."})

    @action(detail=False, methods=["post"], url_path="walkin/suggest")
    def suggest_walkin(self, request):
        doctor_id = request.data.get("doctor")
        if not doctor_id or not request.data.get("starts_at"):
            return Response({"error": "doctor and starts_at are required."}, status=400)
        starts_at, duration_minutes, error = _parse_walkin_params(request.data)
        if error:
            return Response({"error": error}, status=400)

        from doctors.models import Doctor
        from appointments.services import suggest_walkin_shifts

        doctor = Doctor.objects.filter(id=doctor_id, clinic=request.clinic).first()
        if not doctor:
            return Response({"error": "Doctor not found."}, status=404)

        shifts = suggest_walkin_shifts(doctor, starts_at, duration_minutes)
        return Response({"shifts": shifts})

    @action(detail=False, methods=["post"], url_path="walkin/confirm")
    def confirm_walkin(self, request):
        doctor_id = request.data.get("doctor")
        patient_id = request.data.get("patient")
        reason = request.data.get("reason", "")
        priority = request.data.get("priority", "normal")
        confirmed_shifts = request.data.get("confirmed_shifts") or []

        if not doctor_id or not patient_id or not request.data.get("starts_at"):
            return Response({"error": "doctor, patient, and starts_at are required."}, status=400)
        starts_at, duration_minutes, error = _parse_walkin_params(request.data)
        if error:
            return Response({"error": error}, status=400)
        if priority not in Appointment.Priority.values:
            return Response({"error": "Invalid priority."}, status=400)
        if not isinstance(confirmed_shifts, list) or not all(
            isinstance(s, dict) and {"appointment_id", "new_starts_at", "new_ends_at"} <= s.keys() for s in confirmed_shifts
        ):
            return Response({"error": "confirmed_shifts must be a list of shift objects."}, status=400)

        from doctors.models import Doctor
        from patients.models import Patient
        from appointments.services import execute_walkin_and_shifts
        from queue_mgmt.views import broadcast_queue_update

        doctor = Doctor.objects.filter(id=doctor_id, clinic=request.clinic).first()
        patient = Patient.objects.filter(id=patient_id, clinic=request.clinic).first()
        if not doctor or not patient:
            return Response({"error": "Doctor or Patient not found."}, status=404)

        try:
            walkin, shifted_appts = execute_walkin_and_shifts(
                clinic=request.clinic,
                doctor=doctor,
                patient=patient,
                starts_at=starts_at,
                duration_minutes=duration_minutes,
                reason=reason,
                priority=priority,
                confirmed_shifts=confirmed_shifts,
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)

        walkin_data = self.get_serializer(walkin).data

        from queue_mgmt.models import QueueToken
        from queue_mgmt.serializers import QueueTokenSerializer
        qtoken = QueueToken.objects.filter(appointment=walkin).first()
        if qtoken:
            broadcast_queue_update(doctor.id, "queue.created", QueueTokenSerializer(qtoken).data)

        broadcast_queue_update(doctor.id, "appointment.created", walkin_data)

        for appt in shifted_appts:
            appt_data = self.get_serializer(appt).data
            broadcast_queue_update(doctor.id, "appointment.shifted", appt_data)

        return Response({
            "walkin": walkin_data,
            "shifted_count": len(shifted_appts)
        })
