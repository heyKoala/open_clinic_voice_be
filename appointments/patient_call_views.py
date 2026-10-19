"""API for reminder and feedback calls (appointments.journey).

GET  /appointments/patient-calls/?appointment=<id>  an appointment's calls (its journey)
GET  /appointments/patient-calls/?todo=1            calls the front desk has to act on now
POST /appointments/patient-calls/<id>/record/       {"outcome", "rating"?, "comment"?} what came of a call
POST /appointments/patient-calls/<id>/review-sent/  the Google review link went to the patient
POST /appointments/patient-calls/<id>/resolve/      staff dealt with it (called back, spoke to an unhappy patient)
GET/PUT /appointments/patient-calls/settings/       timings, the review link and threshold (PUT: admins)
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.permissions import IsAdminOrReceptionist, IsClinicAdmin
from appointments import journey, outbound
from appointments.models import PatientCall, PatientCallSettings
from appointments.outbound_views import _base_url


class PatientCallSerializer(serializers.ModelSerializer):
    patient = serializers.IntegerField(source="appointment.patient_id", read_only=True)
    patient_name = serializers.CharField(source="appointment.patient.full_name", read_only=True)
    patient_phone = serializers.CharField(source="appointment.patient.phone", read_only=True)
    doctor_name = serializers.CharField(source="appointment.doctor.full_name", read_only=True)
    appointment_starts_at = serializers.DateTimeField(source="appointment.starts_at", read_only=True)
    appointment_status = serializers.CharField(source="appointment.status", read_only=True)
    needs = serializers.SerializerMethodField()

    class Meta:
        model = PatientCall
        fields = ["id", "appointment", "kind", "status", "due_at", "for_starts_at", "attempts", "last_attempt_at",
                  "result", "rating", "comment", "review_sent_at", "resolved_at", "call_log",
                  "patient", "patient_name", "patient_phone", "doctor_name", "appointment_starts_at",
                  "appointment_status", "needs"]
        read_only_fields = fields

    def get_needs(self, call):
        return journey.needs(call, self.context["call_settings"])


def _script(value):
    value = (value or "").strip()
    if len(value) > 8000:
        raise serializers.ValidationError("Keep the script under 8,000 characters.")
    # Saving the default unchanged stores nothing, so later improvements to the default still apply.
    return "" if value in {t["script"] for t in outbound.DEFAULT_SCRIPTS.values()} else value


def _first_message(value):
    value = (value or "").strip()
    return "" if value in {t["first_message"] for t in outbound.DEFAULT_SCRIPTS.values()} else value


class PatientCallSettingsSerializer(serializers.ModelSerializer):
    # Whether this server can place AI calls at all, and if not, why (read-only).
    ai_calls_problem = serializers.SerializerMethodField()
    # For the script editor (read-only): the defaults, the part the system adds, and the placeholders.
    script_defaults = serializers.SerializerMethodField()
    script_system_parts = serializers.SerializerMethodField()
    script_placeholders = serializers.SerializerMethodField()

    class Meta:
        model = PatientCallSettings
        fields = ["reminder_enabled", "reminder_time", "reminder_attempts", "retry_after_minutes",
                  "feedback_enabled", "feedback_time", "review_url", "review_min_rating",
                  "ai_calls_enabled", "call_window_start", "call_window_end", "ai_calls_problem",
                  "reminder_first_message", "reminder_script", "feedback_first_message", "feedback_script",
                  "script_defaults", "script_system_parts", "script_placeholders"]

    def get_ai_calls_problem(self, obj):
        return outbound.missing_setup(self.context.get("base_url", ""))

    def get_script_defaults(self, obj):
        return {str(kind): dict(texts) for kind, texts in outbound.DEFAULT_SCRIPTS.items()}

    def get_script_system_parts(self, obj):
        return {str(kind): text for kind, text in outbound.SYSTEM_PART.items()}

    def get_script_placeholders(self, obj):
        return outbound.PLACEHOLDERS

    def validate_reminder_script(self, value):
        return _script(value)

    def validate_reminder_first_message(self, value):
        return _first_message(value)

    def validate_feedback_first_message(self, value):
        return _first_message(value)

    def validate_feedback_script(self, value):
        return _script(value)

    def validate(self, attrs):
        start = attrs.get("call_window_start", getattr(self.instance, "call_window_start", None))
        end = attrs.get("call_window_end", getattr(self.instance, "call_window_end", None))
        if start and end and start >= end:
            raise serializers.ValidationError({"call_window_end": "Must be after the start of calling hours."})
        return attrs

    def validate_reminder_attempts(self, value):
        if not 1 <= value <= 5:
            raise serializers.ValidationError("Between 1 and 5 tries.")
        return value

    def validate_retry_after_minutes(self, value):
        if not 15 <= value <= 24 * 60:
            raise serializers.ValidationError("Between 15 minutes and 24 hours.")
        return value

    def validate_review_min_rating(self, value):
        if not 1 <= value <= 5:
            raise serializers.ValidationError("A rating from 1 to 5.")
        return value


class PatientCallViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = PatientCallSerializer
    permission_classes = [IsAdminOrReceptionist]
    pagination_class = None

    def get_permissions(self):
        if self.action == "call_settings" and self.request.method == "PUT":
            return [IsClinicAdmin()]
        return super().get_permissions()

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "call_settings": journey.settings_for(self.request.clinic)}

    def get_queryset(self):
        from clinics.context import scope_clinics
        queryset = PatientCall.objects.filter(clinic__in=scope_clinics(self.request), appointment__is_active=True).select_related(
            "appointment", "appointment__patient", "appointment__doctor")
        params = self.request.query_params
        if params.get("appointment"):
            queryset = queryset.filter(appointment_id=params["appointment"])
        if params.get("kind"):
            queryset = queryset.filter(kind=params["kind"])
        if params.get("todo"):
            # Due now, or finished with something left for the desk; `needs` (serializer) says what.
            now = timezone.now()
            queryset = queryset.filter(
                Q(status=PatientCall.Status.SCHEDULED, due_at__lte=now)
                | Q(resolved_at__isnull=True, status=PatientCall.Status.NO_ANSWER, due_at__gte=now - timedelta(days=7))
                | Q(resolved_at__isnull=True, kind=PatientCall.Kind.FEEDBACK, status=PatientCall.Status.DONE,
                    due_at__gte=now - timedelta(days=14))
            )
        return queryset

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        if request.query_params.get("todo"):
            response.data = [row for row in response.data if row["needs"]]
        return response

    def _reply(self, call):
        return Response(self.get_serializer(call).data)

    @action(detail=True, methods=["post"])
    def record(self, request, pk=None):
        call = self.get_object()
        try:
            journey.record(call, str(request.data.get("outcome") or ""), request.data.get("rating"),
                           str(request.data.get("comment") or ""))
        except journey.RecordError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return self._reply(call)

    @action(detail=True, methods=["post"], url_path="call-now")
    def call_now(self, request, pk=None):
        """The AI dials this patient now (also when automatic calling is off): to try it, or to call one patient."""
        call = self.get_object()
        try:
            outbound.place_call(call, _base_url(request))
        except outbound.DialError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return self._reply(call)

    @action(detail=True, methods=["post"], url_path="review-sent")
    def review_sent(self, request, pk=None):
        call = self.get_object()
        call.review_sent_at = timezone.now()
        call.save(update_fields=["review_sent_at", "updated_at"])
        return self._reply(call)

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        call = self.get_object()
        call.resolved_at = timezone.now()
        call.save(update_fields=["resolved_at", "updated_at"])
        return self._reply(call)

    @action(detail=False, methods=["get", "put"], url_path="settings")
    def call_settings(self, request):
        settings = journey.settings_for(request.clinic)
        context = {"base_url": _base_url(request)}
        if request.method == "PUT":
            serializer = PatientCallSettingsSerializer(settings, data=request.data, partial=True, context=context)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            # New timings apply to calls that haven't been made yet.
            for call in PatientCall.objects.filter(clinic__in=request.clinic.root.group_clinics(),
                                                   status=PatientCall.Status.SCHEDULED, attempts=0).select_related("appointment"):
                due = (journey.reminder_due if call.kind == PatientCall.Kind.REMINDER else journey.feedback_due)(call.appointment, settings)
                if due is not None and due != call.due_at:
                    call.due_at = due
                    call.save(update_fields=["due_at", "updated_at"])
        return Response(PatientCallSettingsSerializer(settings, context=context).data)
