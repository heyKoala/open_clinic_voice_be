from rest_framework.routers import DefaultRouter

from queue_mgmt.views import QueueTokenViewSet

router = DefaultRouter()
router.register("", QueueTokenViewSet, basename="queue-token")

urlpatterns = router.urls
