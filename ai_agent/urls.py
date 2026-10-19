from django.urls import path, include
from rest_framework.routers import DefaultRouter

from ai_agent.views import (
    AgentConfigurationViewSet, CallLogViewSet, Rock8WebhookView, Rock8NumberWebhookView, Rock8StartWebCallView,
    VoiceAgentPreviewView, VoiceAgentProvidersView, VoiceAgentSettingsView, VoiceAgentVoicesView,
)
from ai_agent.tool_views import (
    AIToolAvailableSlotsView,
    AIToolBookAppointmentView,
    AIToolCancelAppointmentView,
    AIToolListDoctorsView,
    AIToolLookupAppointmentsView,
    AIToolSendWhatsAppConfirmationView,
)
from ai_agent.knowledge_views import AIToolKnowledgeView, KnowledgeDocumentViewSet
from appointments.outbound_views import PatientCallOutcomeToolView, PatientCallWebhookView

router = DefaultRouter()
router.register(r"agent-configurations", AgentConfigurationViewSet, basename="agentconfiguration")
router.register(r"call-logs", CallLogViewSet, basename="calllog")
router.register(r"knowledge", KnowledgeDocumentViewSet, basename="knowledge")

urlpatterns = [
    path("web-call/start/", Rock8StartWebCallView.as_view(), name="rock8-start-web-call"),
    path("voice-agent/", VoiceAgentSettingsView.as_view(), name="voice-agent-settings"),
    path("voice-agent/providers/", VoiceAgentProvidersView.as_view(), name="voice-agent-providers"),
    path("voice-agent/voices/", VoiceAgentVoicesView.as_view(), name="voice-agent-voices"),
    path("voice-agent/preview/", VoiceAgentPreviewView.as_view(), name="voice-agent-preview"),
    path("webhooks/rock8/", Rock8NumberWebhookView.as_view(), name="rock8-number-webhook"),
    path("webhooks/rock8/<int:clinic_id>/", Rock8WebhookView.as_view(), name="rock8-webhook"),
    # Same endpoints with the token in the path instead of ?token= (the voice provider's inbound
    # configuration only takes a plain URL).
    path("webhooks/rock8/<int:clinic_id>/<str:token>/", Rock8WebhookView.as_view(), name="rock8-webhook-path-token"),
    # The in-app Test call: same webhook, but the call is logged as a test.
    path("webhooks/rock8/<int:clinic_id>/<str:token>/test/", Rock8WebhookView.as_view(), {"test": True}, name="rock8-webhook-test"),
    # The AI's reminder and feedback calls (appointments.outbound): the call's script, and its outcome tool.
    path("webhooks/rock8/<int:clinic_id>/<str:token>/patient-call/<int:call_id>/", PatientCallWebhookView.as_view(), name="rock8-patient-call-webhook"),
    path("tools/rock8/<int:clinic_id>/<str:token>/patient-call/<int:call_id>/outcome/", PatientCallOutcomeToolView.as_view(), name="rock8-tool-patient-call-outcome"),
    path("tools/rock8/<int:clinic_id>/<str:token>/knowledge/", AIToolKnowledgeView.as_view(), name="rock8-tool-knowledge"),
    path("tools/rock8/<int:clinic_id>/<str:token>/slots/", AIToolAvailableSlotsView.as_view(), name="rock8-tool-slots-path-token"),
    path("tools/rock8/<int:clinic_id>/<str:token>/book/", AIToolBookAppointmentView.as_view(), name="rock8-tool-book-path-token"),
    path("tools/rock8/<int:clinic_id>/<str:token>/cancel/", AIToolCancelAppointmentView.as_view(), name="rock8-tool-cancel-path-token"),
    path("tools/rock8/<int:clinic_id>/<str:token>/doctors/", AIToolListDoctorsView.as_view(), name="rock8-tool-doctors-path-token"),
    path("tools/rock8/<int:clinic_id>/<str:token>/appointments/", AIToolLookupAppointmentsView.as_view(), name="rock8-tool-lookup-appointments-path-token"),
    path("tools/rock8/<int:clinic_id>/<str:token>/whatsapp-confirmation/", AIToolSendWhatsAppConfirmationView.as_view(), name="rock8-tool-whatsapp-confirmation-path-token"),
    path("tools/rock8/<int:clinic_id>/slots/", AIToolAvailableSlotsView.as_view(), name="rock8-tool-slots"),
    path("tools/rock8/<int:clinic_id>/book/", AIToolBookAppointmentView.as_view(), name="rock8-tool-book"),
    path("tools/rock8/<int:clinic_id>/cancel/", AIToolCancelAppointmentView.as_view(), name="rock8-tool-cancel"),
    path("tools/rock8/<int:clinic_id>/doctors/", AIToolListDoctorsView.as_view(), name="rock8-tool-doctors"),
    path("tools/rock8/<int:clinic_id>/appointments/", AIToolLookupAppointmentsView.as_view(), name="rock8-tool-lookup-appointments"),
    path("tools/rock8/<int:clinic_id>/whatsapp-confirmation/", AIToolSendWhatsAppConfirmationView.as_view(), name="rock8-tool-whatsapp-confirmation"),
    path("", include(router.urls)),
]
