from __future__ import annotations

import os
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.db.models import Avg, Count, Sum, ExpressionWrapper, DurationField
from django.db.models.functions import Coalesce, TruncDate
from django.http import FileResponse, Http404
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
import django_filters
import logging

from accounts.models import User
from accounts.permissions import AuthenticatedAndVerified, IsClinicAdmin
from audit.models import AuthEvent
from appointments.models import Appointment
from followups.models import FollowUp
from queue_mgmt.models import QueueToken
from ai_agent.models import CallLog
from doctors.models import Doctor
from common.api import ClinicScopedModelViewSet
from common.audit import log_data_change

from .models import ReportAccessToken, ReportExecution, ReportTemplate
from .serializers import (
    ReportTemplateSerializer,
    ReportExecutionSerializer,
    ReportExecutionCreateSerializer
)
from .report_generator import ReportGenerator
from .tasks import enqueue_report_generation
from .permissions import allowed_roles_for, role_can_manage
from accounts.services import log_auth_event
from accounts.utils import generate_raw_token, token_hash
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

logger = logging.getLogger(__name__)
download_signer = TimestampSigner(salt="manageopd-report-download")


def _parse_days(request, default=30, maximum=365):
    try:
        days = int(request.query_params.get("days", default))
    except (TypeError, ValueError):
        days = default
    return min(max(days, 1), maximum)


class AIAnalyticsDashboardView(APIView):
    """AI call analytics scoped to the requesting clinic."""
    permission_classes = [IsClinicAdmin]

    def get(self, request):
        days = _parse_days(request)
        since = timezone.now() - timedelta(days=days)
        clinic = request.clinic

        calls = CallLog.objects.filter(clinic=clinic, occurred_at__gte=since)

        total = calls.count()
        inbound = calls.filter(direction=CallLog.Direction.INBOUND).count()
        outbound = calls.filter(direction=CallLog.Direction.OUTBOUND).count()

        avg_dur = calls.aggregate(avg=Avg('duration_seconds'))['avg'] or 0

        booked = calls.filter(outcome__icontains='booked').count()
        cancelled_calls = calls.filter(outcome__icontains='cancel').count()
        info_only = calls.filter(outcome__icontains='info').count()
        unanswered = calls.filter(outcome__icontains='unanswer').count()
        other = total - booked - cancelled_calls - info_only - unanswered

        conversion_rate = round(booked / total * 100, 1) if total else 0

        # Daily breakdown for chart
        daily = (
            calls
            .annotate(day=TruncDate('occurred_at'))
            .values('day')
            .annotate(total=Count('id'), converted=Count('id', filter=Q(outcome__icontains='booked')))
            .order_by('day')
        )

        return Response({
            "period_days": days,
            "summary": {
                "total_calls": total,
                "inbound_calls": inbound,
                "outbound_calls": outbound,
                "avg_duration_seconds": round(avg_dur),
                "booking_conversion_rate": conversion_rate,
                "calls_with_booking": booked,
                "calls_cancelled": cancelled_calls,
                "calls_info_only": info_only,
                "calls_unanswered": unanswered,
                "calls_other": max(other, 0),
            },
            "calls_by_day": [
                {"date": str(row["day"]), "total": row["total"], "converted": row["converted"]}
                for row in daily
            ],
        })


class DoctorAnalyticsDashboardView(APIView):
    """Doctor analytics scoped to the requesting clinic."""
    permission_classes = [IsClinicAdmin]

    def get(self, request):
        days = _parse_days(request)
        since = timezone.now() - timedelta(days=days)
        clinic = request.clinic

        doctors = Doctor.objects.filter(clinic=clinic, is_active=True)
        results = []

        for doctor in doctors:
            apps = Appointment.objects.filter(doctor=doctor, starts_at__gte=since)
            total_apps = apps.count()
            if total_apps == 0:
                continue

            completed_apps = apps.filter(status=Appointment.Status.COMPLETED).count()
            
            queue = QueueToken.objects.filter(doctor=doctor, service_date__gte=timezone.localdate(since), checked_in_at__isnull=False)
            wait_exp = ExpressionWrapper(Coalesce(F('served_at'), timezone.now()) - F('checked_in_at'), output_field=DurationField())
            avg_wait = queue.annotate(w=wait_exp).aggregate(avg=Avg('w'))['avg']
            avg_wait_sec = avg_wait.total_seconds() if avg_wait else 0

            followups = FollowUp.objects.filter(doctor=doctor, created_at__gte=since).count()
            return_rate = round((followups / total_apps) * 100, 1)

            daily_breakdown = (
                apps.annotate(day=TruncDate('starts_at'))
                .values('day')
                .annotate(total=Count('id'))
                .order_by('day')
            )

            results.append({
                "doctor_id": doctor.id,
                "doctor_name": doctor.full_name,
                "specialty": doctor.specialty,
                "degree": doctor.degree,
                "total_appointments": total_apps,
                "completed_appointments": completed_apps,
                "avg_wait_seconds": round(avg_wait_sec),
                "return_rate": return_rate,
                "appointments_by_day": [{"date": str(row['day']), "total": row['total']} for row in daily_breakdown]
            })

        results.sort(key=lambda x: x['total_appointments'], reverse=True)

        return Response({
            "period_days": days,
            "doctors": results
        })

class DashboardMetricsView(APIView):
    """Admin-only aggregates derived directly from the clinic's records."""
    permission_classes = [IsClinicAdmin]

    def get(self, request):
        today = timezone.localdate()
        appointments = Appointment.objects.filter(clinic=request.clinic)
        followups = FollowUp.objects.filter(clinic=request.clinic)
        queue = QueueToken.objects.filter(clinic=request.clinic, service_date=today)
        appointment_statuses = dict(appointments.values_list("status").annotate(total=Count("id")))
        followup_statuses = dict(followups.values_list("status").annotate(total=Count("id")))
        scheduled = appointment_statuses.get(Appointment.Status.SCHEDULED, 0)
        completed = appointment_statuses.get(Appointment.Status.COMPLETED, 0)
        eligible_followups = sum(followup_statuses.get(status, 0) for status in (FollowUp.Status.COMPLETED, FollowUp.Status.FAILED, FollowUp.Status.CANCELLED))
        converted_followups = followup_statuses.get(FollowUp.Status.COMPLETED, 0)
        return Response({
            "generated_at": timezone.now(), "scope": {"service_date": today},
            "appointments": {"total": appointments.count(), "today": appointments.filter(starts_at__date=today).count(), "by_status": appointment_statuses, "completion_rate": round(completed / (completed + scheduled) * 100, 1) if completed + scheduled else 0},
            "followups": {"total": followups.count(), "due_today": followups.filter(scheduled_for__date=today, status__in=[FollowUp.Status.PENDING, FollowUp.Status.SCHEDULED]).count(), "by_status": followup_statuses, "conversion_rate": round(converted_followups / eligible_followups * 100, 1) if eligible_followups else 0},
            "queue": queue_summary(queue),
        })


def queue_summary(queue):
    statuses = dict(queue.values_list("status").annotate(total=Count("id")))
    wait_expression = ExpressionWrapper(Coalesce(F("served_at"), timezone.now()) - F("checked_in_at"), output_field=DurationField())
    average_wait = queue.filter(checked_in_at__isnull=False).annotate(wait=wait_expression).aggregate(value=Avg("wait"))["value"]
    return {"total": queue.count(), "by_status": statuses, "waiting": statuses.get(QueueToken.Status.WAITING, 0), "serving": statuses.get(QueueToken.Status.IN_CONSULTATION, 0), "average_wait_seconds": round(average_wait.total_seconds()) if average_wait else 0}


class LiveQueueView(APIView):
    """Authenticated polling endpoint; clients should poll every 10 seconds."""
    permission_classes = [AuthenticatedAndVerified]

    def get(self, request):
        today = timezone.localdate()
        if request.query_params.get("service_date"):
            try:
                today = parse_date(request.query_params["service_date"])
            except ValueError:
                today = None
            if today is None:
                return Response({"error": "service_date must be YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        queue = QueueToken.objects.filter(clinic=request.clinic, service_date=today).select_related("patient", "doctor").order_by("doctor_id", "token_number")
        # Doctors only see their own queue.
        if request.user.role == User.Role.DOCTOR:
            doctor = getattr(request.user, "doctor_profile", None)
            queue = queue.filter(doctor=doctor) if doctor else queue.none()
        return Response({"poll_after_seconds": 10, "generated_at": timezone.now(), "summary": queue_summary(queue), "tokens": [{"id": token.id, "doctor_id": token.doctor_id, "doctor_name": token.doctor.full_name, "patient_id": token.patient_id, "patient_name": token.patient.full_name, "token_number": token.token_number, "status": token.status, "checked_in_at": token.checked_in_at, "served_at": token.served_at} for token in queue]})


class ReportTemplateFilter(django_filters.FilterSet):
    report_type = django_filters.CharFilter(field_name="report_type", lookup_expr="exact")
    format = django_filters.CharFilter(field_name="format", lookup_expr="exact")
    is_active = django_filters.BooleanFilter(field_name="is_active")
    is_scheduled = django_filters.BooleanFilter(field_name="is_scheduled")

    class Meta:
        model = ReportTemplate
        fields = ["report_type", "format", "is_active", "is_scheduled"]


class ReportTemplateViewSet(ClinicScopedModelViewSet):
    """ViewSet for managing report templates."""
    queryset = ReportTemplate.objects.all()
    serializer_class = ReportTemplateSerializer
    filterset_class = ReportTemplateFilter
    search_fields = ["name", "description"]
    ordering_fields = ["name", "created_at", "updated_at"]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]

    def get_permissions(self):
        if self.action in {"create", "update", "partial_update", "destroy"}:
            return [IsClinicAdmin()]
        return [AuthenticatedAndVerified()]

    def get_queryset(self):
        queryset = super().get_queryset().filter(clinic_id=self.request.clinic.id)
        if self.request.user.role != User.Role.CLINIC_ADMIN:
            queryset = queryset.filter(Q(allowed_roles__contains=[self.request.user.role]))
        return queryset

    def perform_create(self, serializer):
        instance = serializer.save(clinic_id=self.request.clinic.id)
        log_data_change(instance, "create", request=self.request)

    def perform_update(self, serializer):
        instance = serializer.save()
        log_data_change(instance, "update", request=self.request)

    def perform_destroy(self, instance):
        # Log deletion
        log_data_change(instance, "delete", request=self.request)
        instance.delete()

    @action(detail=False, methods=['get'])
    def definitions(self, request):
        return Response({
            "report_types": [
                {"value": choice[0], "label": choice[1], "allowed_roles": allowed_roles_for(choice[0])}
                for choice in ReportTemplate.ReportType.choices
            ],
            "allowed_roles": [
                {"value": choice[0], "label": choice[1]}
                for choice in User.Role.choices
            ]
        })


class ReportExecutionFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name="status", lookup_expr="exact")
    template = django_filters.NumberFilter(field_name="template_id")
    requested_by = django_filters.NumberFilter(field_name="requested_by_id")
    date_after = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="gte")
    date_before = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = ReportExecution
        fields = ["status", "template", "requested_by", "date_after", "date_before"]


class ReportExecutionViewSet(ClinicScopedModelViewSet):
    """ViewSet for managing report executions."""
    queryset = ReportExecution.objects.all()
    serializer_class = ReportExecutionSerializer
    filterset_class = ReportExecutionFilter
    search_fields = ["template__name"]
    ordering_fields = ["created_at", "started_at", "completed_at"]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]

    def get_serializer_class(self):
        if self.action == 'create':
            return ReportExecutionCreateSerializer
        return ReportExecutionSerializer

    def get_queryset(self):
        queryset = super().get_queryset().select_related("template", "requested_by")
        queryset = queryset.filter(clinic_id=self.request.clinic.id)
        if self.request.user.role != User.Role.CLINIC_ADMIN:
            queryset = queryset.filter(template__allowed_roles__contains=[self.request.user.role])
        return queryset

    def perform_create(self, serializer):
        instance = serializer.save(
            clinic_id=self.request.clinic.id,
            requested_by=self.request.user
        )
        log_data_change(instance, "create", request=self.request)
        transaction.on_commit(lambda: enqueue_report_generation(instance.id))

    def perform_destroy(self, instance):
        if instance.result_file:
            try:
                if instance.result_file.storage.exists(instance.result_file.name):
                    instance.result_file.storage.delete(instance.result_file.name)
            except Exception as e:
                logger.error(f"Error deleting report file {instance.result_file.name}: {str(e)}")

        log_data_change(instance, "delete", request=self.request)
        instance.delete()

    @action(detail=False, methods=['get'])
    def definitions(self, request):
        return Response(
            {
                "report_types": [
                    {
                        "value": choice[0],
                        "label": choice[1],
                        "allowed_roles": allowed_roles_for(choice[0]),
                    }
                    for choice in ReportTemplate.ReportType.choices
                ],
                "allowed_roles": [
                    {"value": choice[0], "label": choice[1]}
                    for choice in User.Role.choices
                ],
            }
        )

    @action(detail=True, methods=['post'], url_path='signed-download')
    def signed_download(self, request, pk=None):
        execution = self.get_object()
        if not execution.is_downloadable:
            return Response({"error": "Report is not ready for download"}, status=status.HTTP_400_BAD_REQUEST)

        raw_token = generate_raw_token()
        token_obj = ReportAccessToken.objects.create(
            clinic_id=execution.clinic_id,
            execution=execution,
            user=request.user,
            token_hash=token_hash(raw_token),
            expires_at=timezone.now() + timezone.timedelta(minutes=30),
        )
        signed_token = download_signer.sign(f"{token_obj.id}:{raw_token}")
        download_url = request.build_absolute_uri(
            reverse("report-execution-download", kwargs={"pk": execution.pk})
        ) + f"?token={signed_token}"
        log_auth_event(
            request,
            AuthEvent.EventType.DATA_EXPORTED,
            user=request.user,
            email=request.user.email,
            metadata={"execution_id": execution.id, "template": execution.template.name, "stage": "link_issued"},
        )
        return Response({"download_url": download_url, "expires_at": token_obj.expires_at})

    @action(detail=True, methods=['get'])
    def download(self, request, pk=None):
        report_execution = self.get_object()
        signed_token = request.query_params.get("token")
        if not signed_token:
            return Response({"error": "Download token is required"}, status=status.HTTP_403_FORBIDDEN)

        try:
            payload = download_signer.unsign(signed_token, max_age=30 * 60)
        except (BadSignature, SignatureExpired):
            return Response({"error": "Download token is invalid or expired"}, status=status.HTTP_403_FORBIDDEN)

        token_id, _, raw = payload.partition(":")
        token_obj = ReportAccessToken.objects.select_related("execution", "user").filter(pk=token_id).first()
        if not token_obj or token_obj.execution_id != report_execution.id or token_obj.user_id != request.user.id:
            return Response({"error": "Download token is invalid"}, status=status.HTTP_403_FORBIDDEN)
        if token_obj.token_hash != token_hash(raw) or not token_obj.is_usable:
            return Response({"error": "Download token is invalid or expired"}, status=status.HTTP_403_FORBIDDEN)

        if not report_execution.is_downloadable:
            if report_execution.status == ReportExecution.Status.FAILED:
                return Response({"error": "Report generation failed"}, status=status.HTTP_400_BAD_REQUEST)
            if report_execution.is_expired:
                return Response({"error": "Report has expired"}, status=status.HTTP_410_GONE)
            return Response({"error": "Report is not ready for download"}, status=status.HTTP_400_BAD_REQUEST)

        ReportExecution.objects.filter(pk=report_execution.pk).update(
            download_count=F("download_count") + 1
        )
        report_execution.refresh_from_db(fields=['download_count'])
        token_obj.used_at = timezone.now()
        token_obj.save(update_fields=["used_at", "updated_at"])

        log_data_change(report_execution, "download", request=request, metadata={"token_id": token_obj.id})
        log_auth_event(
            request,
            AuthEvent.EventType.DATA_EXPORTED,
            user=request.user,
            email=request.user.email,
            metadata={"execution_id": report_execution.id, "template": report_execution.template.name, "stage": "downloaded"},
        )

        try:
            file_name = os.path.basename(report_execution.result_file.name)
            content_type = 'application/octet-stream'  # default
            if file_name.lower().endswith('.pdf'):
                content_type = 'application/pdf'
            elif file_name.lower().endswith('.csv'):
                content_type = 'text/csv'
            elif file_name.lower().endswith(('.xlsx', '.xls')):
                content_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            elif file_name.lower().endswith('.txt'):
                content_type = 'text/plain'

            response = FileResponse(
                report_execution.result_file.open('rb'),
                content_type=content_type
            )
            response['Content-Disposition'] = f'attachment; filename="{file_name}"'
            return response
        except FileNotFoundError:
            raise Http404("Report file not found")
        except Exception as e:
            logger.error(f"Error serving report file: {str(e)}")
            return Response(
                {"error": "Unable to retrieve report file"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['post'])
    def generate_from_template(self, request):
        template_id = request.data.get('template_id')
        parameters = request.data.get('parameters', {})

        if not template_id:
            return Response(
                {"error": "template_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            template = ReportTemplate.objects.get(
                id=template_id,
                clinic_id=request.clinic.id,
                is_active=True
            )
        except ReportTemplate.DoesNotExist:
            return Response(
                {"error": "Template not found or not accessible"},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = ReportExecutionCreateSerializer(
            data={'template': template.id, 'parameters': parameters},
            context={'request': request}
        )
        if serializer.is_valid():
            report_execution = serializer.save(
                clinic_id=request.clinic.id,
                requested_by=request.user
            )

            enqueue_report_generation(report_execution.id)
            report_execution.refresh_from_db()

            return Response(
                ReportExecutionSerializer(report_execution, context={'request': request}).data,
                status=status.HTTP_202_ACCEPTED
            )
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
