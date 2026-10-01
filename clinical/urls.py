from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ClinicalEncounterViewSet,
    SymptomSummaryViewSet,
    ClinicalNoteViewSet,
    PatientHealthRecordViewSet,
    ClinicalAttachmentViewSet
)

router = DefaultRouter()
router.register(r'encounters', ClinicalEncounterViewSet, basename='clinical-encounter')
router.register(r'symptom-summaries', SymptomSummaryViewSet, basename='symptom-summary')
router.register(r'clinical-notes', ClinicalNoteViewSet, basename='clinical-note')
router.register(r'health-records', PatientHealthRecordViewSet, basename='patient-health-record')
router.register(r'attachments', ClinicalAttachmentViewSet, basename='clinical-attachment')

urlpatterns = [
    path('', include(router.urls)),
]