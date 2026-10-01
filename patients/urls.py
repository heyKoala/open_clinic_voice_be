from django.urls import path
from rest_framework.routers import DefaultRouter

from patients.views import PatientViewSet, WhatsAppWebhookView

router = DefaultRouter()
router.register("", PatientViewSet, basename="patient")

urlpatterns = [
    path('webhooks/whatsapp/', WhatsAppWebhookView.as_view(), name='whatsapp-webhook'),
] + router.urls
