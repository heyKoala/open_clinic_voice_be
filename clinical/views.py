from rest_framework import filters as rest_filters
from django_filters.rest_framework import DjangoFilterBackend
import django_filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django.db import models

class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

from common.api import ClinicScopedModelViewSet
from accounts.permissions import IsDoctorOnly, AuthenticatedAndVerified
from .models import (
    ClinicalEncounter, SymptomSummary, ClinicalNote,
    PatientHealthRecord, ClinicalAttachment
)
from .serializers import (
    ClinicalEncounterSerializer, SymptomSummarySerializer,
    ClinicalNoteSerializer, PatientHealthRecordSerializer,
    ClinicalAttachmentSerializer
)
from django.utils import timezone
from common.audit import log_data_change


class ClinicalEncounterFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name="status", lookup_expr="exact")
    encounter_date_after = django_filters.DateTimeFilter(field_name="encounter_date", lookup_expr="gte")
    encounter_date_before = django_filters.DateTimeFilter(field_name="encounter_date", lookup_expr="lte")
    patient = django_filters.NumberFilter(field_name="patient_id")
    doctor = django_filters.NumberFilter(field_name="doctor_id")

    class Meta:
        model = ClinicalEncounter
        fields = ["status", "patient", "doctor"]


class ClinicalEncounterViewSet(ClinicScopedModelViewSet):
    queryset = ClinicalEncounter.objects.select_related(
        "patient", "doctor", "appointment", "queue_token"
    ).prefetch_related(
        "symptom_summary", "clinical_notes"
    )
    serializer_class = ClinicalEncounterSerializer
    permission_classes = [IsDoctorOnly]
    filterset_class = ClinicalEncounterFilter
    search_fields = ["patient__full_name", "doctor__full_name", "chief_complaint"]
    ordering_fields = ["encounter_date", "status", "created_at"]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, rest_filters.SearchFilter, rest_filters.OrderingFilter]

    def get_queryset(self):
        queryset = super().get_queryset()
        # If the user is a doctor, restrict to their own encounters
        if self.request.user.role == 'doctor':
            doctor = getattr(self.request.user, 'doctor_profile', None)
            if not doctor:
                return queryset.none()
            return queryset.filter(doctor=doctor)
        return queryset

    def perform_destroy(self, instance):
        # Soft delete is handled by the base class if the model has is_active
        # ClinicalEncounter doesn't have is_active, so we'll do a hard delete
        # But actually, looking at the base class, it does soft delete if hasattr(instance, 'is_active')
        # Since ClinicalEncounter doesn't have is_active, it will do hard delete
        # For now, we'll keep it as hard delete since encounters should be preserved for history
        super().perform_destroy(instance)


class SymptomSummaryFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name="status", lookup_expr="exact")
    encounter = django_filters.NumberFilter(field_name="encounter_id")
    reviewed_by = django_filters.NumberFilter(field_name="reviewed_by_id")

    class Meta:
        model = SymptomSummary
        fields = ["status", "encounter", "reviewed_by"]


class SymptomSummaryViewSet(ClinicScopedModelViewSet):
    queryset = SymptomSummary.objects.select_related(
        "encounter__patient", "encounter__doctor", "call_log", "reviewed_by"
    )
    serializer_class = SymptomSummarySerializer
    permission_classes = [IsDoctorOnly]
    filterset_class = SymptomSummaryFilter
    search_fields = ["encounter__patient__full_name", "summary_text"]
    ordering_fields = ["created_at", "status", "confidence"]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, rest_filters.SearchFilter, rest_filters.OrderingFilter]

    def get_queryset(self):
        queryset = super().get_queryset()
        # If the user is a doctor, restrict to summaries of encounters they were involved in
        if self.request.user.role == 'doctor':
            doctor = getattr(self.request.user, 'doctor_profile', None)
            if not doctor:
                return queryset.none()
            return queryset.filter(encounter__doctor=doctor)
        return queryset

    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        from common.audit import log_data_change
        log_data_change(self.get_object(), "read", request=request)
        return response

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        summary = self.get_object()
        if summary.status != SymptomSummary.SummaryStatus.PENDING:
            return Response(
                {"detail": "Only pending summaries can be approved."},
                status=400
            )
        summary.status = SymptomSummary.SummaryStatus.APPROVED
        summary.reviewed_by = request.user
        summary.reviewed_at = timezone.now()
        summary.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        # Log the approval as an update
        from common.audit import log_data_change
        log_data_change(summary, "update", request=request)
        return Response(self.get_serializer(summary).data)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        summary = self.get_object()
        if summary.status != SymptomSummary.SummaryStatus.PENDING:
            return Response(
                {"detail": "Only pending summaries can be rejected."},
                status=400
            )
        summary.status = SymptomSummary.SummaryStatus.REJECTED
        summary.reviewed_by = request.user
        summary.reviewed_at = timezone.now()
        summary.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        from common.audit import log_data_change
        log_data_change(summary, "update", request=request)
        return Response(self.get_serializer(summary).data)

    @action(detail=True, methods=['post'])
    def edit_summary(self, request, pk=None):
        summary = self.get_object()
        if summary.status not in [
            SymptomSummary.SummaryStatus.PENDING,
            SymptomSummary.SummaryStatus.APPROVED,
            SymptomSummary.SummaryStatus.REJECTED
        ]:
            return Response(
                {"detail": "Cannot edit summary in current status."},
                status=400
            )
        # We'll update the summary_text and set status to EDITED
        summary_text = request.data.get('summary_text')
        if not summary_text:
            return Response(
                {"detail": "summary_text is required."},
                status=400
            )
        summary.summary_text = summary_text
        summary.status = SymptomSummary.SummaryStatus.EDITED
        summary.reviewed_by = request.user
        summary.reviewed_at = timezone.now()
        summary.save(update_fields=["summary_text", "status", "reviewed_by", "reviewed_at"])
        from common.audit import log_data_change
        log_data_change(summary, "update", request=request)
        return Response(self.get_serializer(summary).data)


class ClinicalNoteFilter(django_filters.FilterSet):
    note_type = django_filters.CharFilter(field_name="note_type", lookup_expr="exact")
    encounter = django_filters.NumberFilter(field_name="encounter_id")
    author = django_filters.NumberFilter(field_name="author_id")

    class Meta:
        model = ClinicalNote
        fields = ["note_type", "encounter", "author"]


class ClinicalNoteViewSet(ClinicScopedModelViewSet):
    queryset = ClinicalNote.objects.select_related(
        "encounter__patient", "encounter__doctor", "author"
    )
    serializer_class = ClinicalNoteSerializer
    permission_classes = [IsDoctorOnly]
    filterset_class = ClinicalNoteFilter
    search_fields = ["content", "encounter__patient__full_name"]
    ordering_fields = ["created_at", "note_type"]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, rest_filters.SearchFilter, rest_filters.OrderingFilter]

    def perform_create(self, serializer):
        instance = serializer.save(clinic=self.request.clinic, author=self.request.user)
        log_data_change(instance, "create", request=self.request)

    def get_queryset(self):
        queryset = super().get_queryset()
        # If the user is a doctor, restrict to notes they authored
        if self.request.user.role == 'doctor':
            return queryset.filter(author=self.request.user)
        return queryset

    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        from common.audit import log_data_change
        log_data_change(self.get_object(), "read", request=request)
        return response


class PatientHealthRecordFilter(django_filters.FilterSet):
    category = django_filters.CharFilter(field_name="category", lookup_expr="exact")
    patient = django_filters.NumberFilter(field_name="patient_id")

    class Meta:
        model = PatientHealthRecord
        fields = ["category", "patient"]


class PatientHealthRecordViewSet(ClinicScopedModelViewSet):
    queryset = PatientHealthRecord.objects.select_related(
        "patient", "recorded_by"
    )
    serializer_class = PatientHealthRecordSerializer
    permission_classes = [AuthenticatedAndVerified]
    filterset_class = PatientHealthRecordFilter
    search_fields = ["patient__full_name", "name", "details"]
    ordering_fields = ["recorded_at", "category"]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, rest_filters.SearchFilter, rest_filters.OrderingFilter]

    def get_queryset(self):
        queryset = super().get_queryset()
        # If the user is a doctor, restrict to health records of patients they have encountered
        if self.request.user.role == 'doctor':
            doctor = getattr(self.request.user, 'doctor_profile', None)
            if not doctor:
                return queryset.none()
            # Get patient IDs from encounters where this doctor was involved
            patient_ids = ClinicalEncounter.objects.filter(
                doctor=doctor
            ).values_list('patient_id', flat=True)
            return queryset.filter(patient_id__in=patient_ids)
        return queryset

    def perform_create(self, serializer):
        instance = serializer.save(clinic=self.request.clinic, recorded_by=self.request.user)
        log_data_change(instance, "create", request=self.request)


class ClinicalAttachmentFilter(django_filters.FilterSet):
    encounter = django_filters.NumberFilter(field_name="encounter_id")
    patient = django_filters.NumberFilter(field_name="patient_id")

    class Meta:
        model = ClinicalAttachment
        fields = ["encounter", "patient"]


class ClinicalAttachmentViewSet(ClinicScopedModelViewSet):
    queryset = ClinicalAttachment.objects.select_related(
        "encounter__patient", "encounter__doctor", "patient", "uploaded_by"
    )
    serializer_class = ClinicalAttachmentSerializer
    permission_classes = [AuthenticatedAndVerified]
    filterset_class = ClinicalAttachmentFilter
    search_fields = ["filename", "encounter__patient__full_name"]
    ordering_fields = ["uploaded_at"]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, rest_filters.SearchFilter, rest_filters.OrderingFilter]

    def get_queryset(self):
        queryset = super().get_queryset()
        # If the user is a doctor, restrict to attachments related to encounters they were involved in
        # or attachments of patients they have encountered
        if self.request.user.role == 'doctor':
            doctor = getattr(self.request.user, 'doctor_profile', None)
            if not doctor:
                return queryset.none()
            # Get encounter IDs for this doctor
            encounter_ids = ClinicalEncounter.objects.filter(
                doctor=doctor
            ).values_list('id', flat=True)
            # Get patient IDs from encounters of this doctor
            patient_ids = ClinicalEncounter.objects.filter(
                doctor=doctor
            ).values_list('patient_id', flat=True)
            return queryset.filter(
                models.Q(encounter_id__in=encounter_ids) |
                models.Q(patient_id__in=patient_ids)
            )
        return queryset

    def perform_create(self, serializer):
        instance = serializer.save(clinic=self.request.clinic, uploaded_by=self.request.user)
        log_data_change(instance, "create", request=self.request)