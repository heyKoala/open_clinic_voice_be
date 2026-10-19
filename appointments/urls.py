from rest_framework.routers import DefaultRouter, SimpleRouter

from appointments.patient_call_views import PatientCallViewSet
from appointments.views import AppointmentViewSet

# Reminder and feedback calls first: the appointments route below would otherwise read
# "patient-calls" as an appointment id. (SimpleRouter: no API root page to hide the appointment list.)
calls_router = SimpleRouter()
calls_router.register("patient-calls", PatientCallViewSet, basename="patient-call")

router = DefaultRouter()
router.register("", AppointmentViewSet, basename="appointment")
urlpatterns = calls_router.urls + router.urls
