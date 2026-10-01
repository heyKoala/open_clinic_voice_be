from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import AIAnalyticsDashboardView, DashboardMetricsView, DoctorAnalyticsDashboardView, LiveQueueView, ReportTemplateViewSet, ReportExecutionViewSet

router = DefaultRouter()
router.register(r'templates', ReportTemplateViewSet, basename='report-template')
router.register(r'executions', ReportExecutionViewSet, basename='report-execution')

urlpatterns = [
    path('dashboard/', DashboardMetricsView.as_view(), name='dashboard-metrics'),
    path('ai-analytics/', AIAnalyticsDashboardView.as_view(), name='ai-analytics'),
    path('doctor-analytics/', DoctorAnalyticsDashboardView.as_view(), name='doctor-analytics'),
    path('live-queue/', LiveQueueView.as_view(), name='live-queue'),
    path('', include(router.urls)),
]
