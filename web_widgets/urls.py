"""Mounted at /api/v1/widgets/.

Hospital admin (signed in):
    GET, POST   /                  list, create
    GET, PATCH, DELETE  /<id>/     one widget
    POST        /<id>/rotate-key/  new public key
    GET         /<id>/stats/       its calls and minutes

The widget script on the hospital's website (no login):
    GET   /public/<public_key>/config/
    POST  /public/<public_key>/token/
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from web_widgets.views import WebWidgetViewSet
from web_widgets.views_public import PublicWidgetConfigView, PublicWidgetTokenView

router = DefaultRouter()
router.register(r"", WebWidgetViewSet, basename="web-widget")

urlpatterns = [
    # Before the router, so its detail route does not take "public".
    path("public/<str:public_key>/config/", PublicWidgetConfigView.as_view(), name="web-widget-public-config"),
    path("public/<str:public_key>/token/", PublicWidgetTokenView.as_view(), name="web-widget-public-token"),
    path("", include(router.urls)),
]
