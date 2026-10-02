import django_filters
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters as rest_filters
from rest_framework.pagination import PageNumberPagination
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db import transaction
from django.utils import timezone

from accounts.permissions import IsClinicAdmin
from common.api import ClinicScopedModelViewSet
from doctors.models import Doctor, doctors_for_receptionist
from doctors.serializers import DoctorSerializer


class DoctorFilter(django_filters.FilterSet):
    specialty = django_filters.CharFilter(field_name="specialty", lookup_expr="icontains")
    is_active = django_filters.BooleanFilter(field_name="is_active")
    consultation_minutes = django_filters.NumberFilter(field_name="consultation_minutes")
    consultation_minutes_gte = django_filters.NumberFilter(field_name="consultation_minutes", lookup_expr="gte")
    consultation_minutes_lte = django_filters.NumberFilter(field_name="consultation_minutes", lookup_expr="lte")

    class Meta:
        model = Doctor
        fields = ["specialty", "is_active", "consultation_minutes"]


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class DoctorViewSet(ClinicScopedModelViewSet):
    queryset = Doctor.objects.filter(is_active=True).select_related("user")
    serializer_class = DoctorSerializer
    filterset_class = DoctorFilter
    search_fields = ["full_name", "specialty"]
    ordering_fields = ["full_name", "specialty", "created_at", "consultation_minutes"]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, rest_filters.SearchFilter, rest_filters.OrderingFilter]

    def get_queryset(self):
        queryset = super().get_queryset()
        # ?desk=mine: a receptionist's own doctors (the reception dashboard). Everyone else gets all.
        if (self.action == "list" and self.request.query_params.get("desk") == "mine"
                and self.request.user.role == "receptionist"):
            queryset = doctors_for_receptionist(queryset, self.request.user, self.request.clinic)
        return queryset

    def get_permissions(self):
        # Doctor profiles are provisioned through invites; only admins may add or remove them.
        if self.action in {"create", "destroy"}:
            return [IsClinicAdmin()]
        return super().get_permissions()

    def perform_update(self, serializer):
        user = self.request.user
        doctor = self.get_object()
        if user.role != "clinic_admin" and getattr(user, "doctor_profile", None) != doctor:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only the doctor or clinic admin can edit availability settings.")
        super().perform_update(serializer)

    @action(detail=False, methods=["get"])
    def dashboard(self, request):
        doctor = getattr(request.user, "doctor_profile", None)
        if doctor is not None and doctor.clinic_id != request.clinic.id:
            doctor = None
        if not doctor:
            # Only look up doctors within the active clinic.
            clinic_doctors = self.get_queryset()
            doctor_id = request.query_params.get("doctor")
            if doctor_id and str(doctor_id).isdigit():
                doctor = clinic_doctors.filter(id=doctor_id).first()
            if not doctor:
                doctor = clinic_doctors.order_by("id").first()
        if not doctor:
            return Response({
                "metrics": {"today_total": 0, "completed": 0, "remaining": 0, "next_patient": None},
                "appointments": []
            })

        now = timezone.localtime()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timezone.timedelta(days=1)

        from appointments.models import Appointment
        appointments = Appointment.objects.filter(
            doctor=doctor,
            starts_at__gte=start_of_day,
            starts_at__lt=end_of_day,
            is_active=True
        ).select_related("patient").order_by("starts_at")

        data = []
        completed_count = 0
        remaining_count = 0
        next_patient = None

        for appt in appointments:
            if appt.status in ["completed", "cancelled", "no_show"]:
                completed_count += 1
            else:
                remaining_count += 1

        from queue_mgmt.models import QueueToken
        next_token = QueueToken.objects.filter(
            doctor=doctor,
            service_date=now.date(),
            status__in=["waiting", "checked_in", "called", "in_consultation"]
        ).order_by('token_number').first()
        next_patient = next_token.patient.full_name if next_token else None

        for appt in appointments:
            patient = appt.patient
            age = None
            if patient.date_of_birth:
                age = int((now.date() - patient.date_of_birth).days / 365.25)

            last_visit = Appointment.objects.filter(
                patient=patient,
                starts_at__lt=start_of_day,
                status="completed"
            ).order_by("-starts_at").first()

            data.append({
                "id": appt.id,
                "patient_id": patient.id,
                "patient_name": patient.full_name,
                "age": age,
                "gender": patient.gender,
                "starts_at": appt.starts_at,
                "ends_at": appt.ends_at,
                "reason": appt.reason,
                "status": appt.status,
                "last_visit": last_visit.starts_at if last_visit else None,
            })

        metrics = {
            "today_total": len(appointments),
            "completed": completed_count,
            "remaining": remaining_count,
            "next_patient": next_patient,
            "current_token": {
                "id": next_token.id,
                "token_number": next_token.token_number,
                "status": next_token.status,
                "patient_name": next_token.patient.full_name
            } if next_token else None
        }

        return Response({
            "metrics": metrics,
            "appointments": data
        })

    @action(detail=True, methods=["post"], url_path="mark-absent")
    def mark_absent(self, request, pk=None):
        doctor = self.get_object()
        user = request.user

        if user.role != "clinic_admin" and getattr(user, "doctor_profile", None) != doctor:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only the doctor or clinic admin can mark leave / absence.")

        start_date = request.data.get("start_date")
        end_date = request.data.get("end_date") or start_date
        reason = request.data.get("reason", "")

        if not start_date:
            return Response({"error": "start_date is required."}, status=400)

        from django.utils.dateparse import parse_date
        s_date = parse_date(start_date)
        e_date = parse_date(end_date)

        if not s_date or not e_date:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)
        if e_date < s_date:
            return Response({"error": "end_date cannot be before start_date."}, status=400)

        with transaction.atomic():
            return self._mark_absent(doctor, s_date, e_date, reason)

    def _mark_absent(self, doctor, s_date, e_date, reason):
        from doctors.models import DoctorAbsence
        DoctorAbsence.objects.create(
            doctor=doctor,
            start_date=s_date,
            end_date=e_date,
            reason=reason,
            clinic=doctor.clinic
        )

        from appointments.models import Appointment
        from patients.models import MessageLog
        from django.utils import timezone as django_timezone
        import datetime

        # Get all affected appointments ordered by date
        affected = list(Appointment.objects.select_for_update().filter(
            doctor=doctor,
            is_active=True,
            status__in=['scheduled', 'checked_in'],
            starts_at__date__gte=s_date,
            starts_at__date__lte=e_date
        ).select_related("patient").order_by('starts_at'))

        count = len(affected)
        messages_created = 0

        for appt in affected:
            original_date = django_timezone.localtime(appt.starts_at).date()
            # Cascade: each day in absence maps forward by its offset from start_date
            days_offset = (original_date - s_date).days + 1
            new_date = e_date + datetime.timedelta(days=days_offset)

            # Ensure new_date is a working day for this doctor
            if doctor.working_days:
                while new_date.isoweekday() not in doctor.working_days:
                    new_date += datetime.timedelta(days=1)

            # Preserve the original local appointment time, just change the date
            local_start = django_timezone.localtime(appt.starts_at)
            new_start = django_timezone.make_aware(
                datetime.datetime.combine(new_date, local_start.time().replace(tzinfo=None))
            )
            new_end = new_start + (appt.ends_at - appt.starts_at)

            appt.starts_at = new_start
            appt.ends_at = new_end
            appt.status = 'scheduled'
            appt.save(update_fields=['starts_at', 'ends_at', 'status', 'updated_at'])

            time_str = django_timezone.localtime(new_start).strftime("%b %d, %Y at %I:%M %p")
            msg_text = (
                f"Dear {appt.patient.full_name}, Dr. {doctor.full_name} is on leave "
                f"on your original appointment date. Your appointment has been automatically "
                f"moved to {time_str}. Please call the clinic if this slot does not work for you."
            )

            MessageLog.objects.create(
                patient=appt.patient,
                clinic=doctor.clinic,
                method=MessageLog.Method.WHATSAPP,
                message_text=msg_text,
                is_sent=False,  # Will be set True once WhatsApp actually sends
                status=MessageLog.Status.PENDING,
                metadata={
                    "template_name": "appointment_rescheduled",
                    "template_args": [
                        appt.patient.full_name,
                        doctor.full_name,
                        time_str
                    ]
                }
            )
            messages_created += 1

        return Response({
            "message": "Absence marked and appointments cascade-shifted successfully.",
            "affected_appointments": count,
            "messages_queued": messages_created
        })
