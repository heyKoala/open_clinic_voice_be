import django_filters
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters as rest_filters
from rest_framework.pagination import PageNumberPagination

from common.api import ClinicScopedModelViewSet
from common.audit import log_data_change
from patients.models import Patient
from patients.serializers import PatientSerializer
import hashlib
import hmac
import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from django.conf import settings
from patients.models import MessageLog

logger = logging.getLogger(__name__)
class PatientFilter(django_filters.FilterSet):
    gender = django_filters.CharFilter(field_name="gender", lookup_expr="exact")
    is_active = django_filters.BooleanFilter(field_name="is_active")
    date_of_birth_after = django_filters.DateFilter(field_name="date_of_birth", lookup_expr="gte")
    date_of_birth_before = django_filters.DateFilter(field_name="date_of_birth", lookup_expr="lte")
    created_at_after = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_at_before = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="lte")
    preferred_language = django_filters.CharFilter(field_name="preferred_language", lookup_expr="exact")
    disease = django_filters.CharFilter(method='filter_by_disease')
    last_visit_after = django_filters.DateFilter(method='filter_by_last_visit_after')
    last_visit_before = django_filters.DateFilter(method='filter_by_last_visit_before')

    def filter_by_disease(self, queryset, name, value):
        return queryset.filter(
            health_records__category='condition',
            health_records__name__icontains=value
        ).distinct()

    def filter_by_last_visit_after(self, queryset, name, value):
        return queryset.filter(
            clinicalencounter__encounter_date__gte=value,
            clinicalencounter__status='completed'
        ).distinct()

    def filter_by_last_visit_before(self, queryset, name, value):
        return queryset.filter(
            clinicalencounter__encounter_date__lte=value,
            clinicalencounter__status='completed'
        ).distinct()

    class Meta:
        model = Patient
        fields = ["gender", "is_active", "preferred_language"]


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class PatientViewSet(ClinicScopedModelViewSet):
    queryset = Patient.objects.filter(is_active=True)
    serializer_class = PatientSerializer
    filterset_class = PatientFilter
    search_fields = ["full_name", "phone", #"abha_number",
    "email"]
    ordering_fields = ["full_name", "created_at", "date_of_birth"]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, rest_filters.SearchFilter, rest_filters.OrderingFilter]

    def get_queryset(self):
        queryset = super().get_queryset()
        # If the user is a doctor, restrict to patients they have encountered
        if self.request.user.role == 'doctor':
            doctor = getattr(self.request.user, 'doctor_profile', None)
            if not doctor:
                return queryset.none()
            # Import ClinicalEncounter here to avoid circular import
            from clinical.models import ClinicalEncounter
            # Get patient IDs from encounters where this doctor was involved
            patient_ids = ClinicalEncounter.objects.filter(
                doctor=doctor
            ).values_list('patient_id', flat=True)
            return queryset.filter(id__in=patient_ids)
        return queryset

    def perform_destroy(self, instance):
        # Soft delete: set is_active=False instead of hard delete
        instance.is_active = False
        instance.save()
        # Log the soft delete as a deletion action
        log_data_change(instance, "delete", request=self.request)

class WhatsAppWebhookView(APIView):
    authentication_classes = []
    # Server-to-server calls authenticated by signature; the per-IP anonymous throttle would
    # make every concurrent call from the provider share one 30/min budget.
    throttle_classes = []
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        """Handle Meta's webhook verification challenge."""
        verify_token = getattr(settings, 'WHATSAPP_WEBHOOK_VERIFY_TOKEN', '')
        mode = request.query_params.get('hub.mode')
        token = request.query_params.get('hub.verify_token') or ''
        challenge = request.query_params.get('hub.challenge')

        if mode == 'subscribe' and verify_token and hmac.compare_digest(token, verify_token):
            logger.info("WhatsApp webhook verified successfully.")
            # Must return the challenge directly as an integer/string, not JSON
            from django.http import HttpResponse
            return HttpResponse(challenge, status=200)

        return Response({"error": "Invalid verification token"}, status=403)

    def _has_valid_signature(self, request):
        """Verify Meta's X-Hub-Signature-256 header against WHATSAPP_APP_SECRET."""
        app_secret = getattr(settings, 'WHATSAPP_APP_SECRET', '')
        if not app_secret:
            # Unsigned events are only tolerated in local development.
            return settings.DEBUG
        signature = request.headers.get('X-Hub-Signature-256', '')
        expected = 'sha256=' + hmac.new(app_secret.encode(), request.body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def post(self, request, *args, **kwargs):
        """Handle incoming webhook events from WhatsApp Cloud API."""
        if not self._has_valid_signature(request):
            return Response({"error": "Invalid signature"}, status=403)
        try:
            body = request.data

            # Check if this is a WhatsApp API event
            if body.get('object') == 'whatsapp_business_account':
                for entry in body.get('entry', []):
                    for change in entry.get('changes', []):
                        value = change.get('value', {})

                        # Handle message statuses (delivered, read, failed)
                        if 'statuses' in value:
                            for status in value['statuses']:
                                message_id = status.get('id')
                                status_text = status.get('status')

                                # Try to find the matching MessageLog
                                try:
                                    # We search in metadata for the message_id we saved
                                    log = MessageLog.objects.filter(metadata__message_id=message_id).first()
                                    if log and status_text in MessageLog.Status.values:
                                        log.status = status_text
                                        log.save(update_fields=['status'])
                                        logger.info(f"Updated MessageLog {log.id} status to {status_text}")
                                except Exception as e:
                                    logger.error(f"Error updating message status: {e}")

                        # Handle incoming messages (replies from patients)
                        if 'messages' in value:
                            for message in value['messages']:
                                from_phone = message.get('from')
                                msg_text = message.get('text', {}).get('body', '')
                                logger.info(f"Received WhatsApp message from {from_phone}: {msg_text}")
                                # Future: implement auto-reply or store incoming message

            return Response({"status": "ok"}, status=200)
        except Exception as e:
            logger.error(f"WhatsApp Webhook Error: {e}")
            return Response({"error": "Internal Server Error"}, status=500)
