import django_filters
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters as rest_filters
from rest_framework.pagination import PageNumberPagination

from common.api import ClinicScopedModelViewSet
from accounts.permissions import IsClinicAdmin, IsDoctorOrReceptionistOnly
from followups.models import Campaign, FollowUp
from followups.serializers import CampaignSerializer, FollowUpSerializer


class CampaignFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name="status", lookup_expr="exact")
    start_date_after = django_filters.DateFilter(field_name="start_date", lookup_expr="gte")
    start_date_before = django_filters.DateFilter(field_name="start_date", lookup_expr="lte")
    is_active = django_filters.BooleanFilter(field_name="is_active")

    class Meta:
        model = Campaign
        fields = ["status", "start_date", "end_date", "is_active"]


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class CampaignViewSet(ClinicScopedModelViewSet):
    queryset = Campaign.objects.filter(is_active=True)
    serializer_class = CampaignSerializer
    permission_classes = [IsClinicAdmin | IsDoctorOrReceptionistOnly]
    filterset_class = CampaignFilter
    search_fields = ["name", "description"]
    ordering_fields = ["name", "created_at", "start_date", "status"]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, rest_filters.SearchFilter, rest_filters.OrderingFilter]

    def perform_destroy(self, instance):
        # Soft delete: set is_active=False
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at", "changed_by"])
        # Log the deletion
        from common.audit import log_data_change
        log_data_change(instance, "delete", request=self.request)


class FollowUpFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name="status", lookup_expr="exact")
    method = django_filters.CharFilter(field_name="method", lookup_expr="exact")
    scheduled_for_after = django_filters.DateTimeFilter(field_name="scheduled_for", lookup_expr="gte")
    scheduled_for_before = django_filters.DateTimeFilter(field_name="scheduled_for", lookup_expr="lte")
    patient = django_filters.NumberFilter(field_name="patient_id")
    doctor = django_filters.NumberFilter(field_name="doctor_id")
    campaign = django_filters.NumberFilter(field_name="campaign_id")
    is_active = django_filters.BooleanFilter(field_name="is_active")

    class Meta:
        model = FollowUp
        fields = ["status", "method", "patient", "doctor", "campaign", "is_active"]


class FollowUpViewSet(ClinicScopedModelViewSet):
    queryset = FollowUp.objects.filter(is_active=True).select_related("patient", "doctor", "campaign", "appointment")
    serializer_class = FollowUpSerializer
    permission_classes = [IsClinicAdmin | IsDoctorOrReceptionistOnly]
    filterset_class = FollowUpFilter
    search_fields = ["patient__full_name", "notes", "outcome"]
    ordering_fields = ["scheduled_for", "status", "created_at"]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, rest_filters.SearchFilter, rest_filters.OrderingFilter]

    def get_queryset(self):
        queryset = super().get_queryset()
        # If the user is a doctor, restrict to their own follow-ups
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
        from common.audit import log_data_change
        log_data_change(instance, "delete", request=self.request)