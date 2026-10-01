from django.urls import path, include
from rest_framework.routers import DefaultRouter

from followups.views import CampaignViewSet, FollowUpViewSet

router = DefaultRouter()
router.register(r"campaigns", CampaignViewSet, basename="campaign")
router.register(r"followups", FollowUpViewSet, basename="followup")

urlpatterns = [
    path("", include(router.urls)),
]
