from django.urls import path

from platform_admin import views

urlpatterns = [
    path("overview/", views.OverviewView.as_view(), name="platform-overview"),
    path("hospitals/", views.HospitalListView.as_view(), name="platform-hospitals"),
    path("hospitals/<int:pk>/", views.HospitalDetailView.as_view(), name="platform-hospital"),
    path("hospitals/<int:pk>/credits/", views.HospitalCreditsView.as_view(), name="platform-credits"),
    path("hospitals/<int:pk>/ledger/", views.HospitalLedgerView.as_view(), name="platform-ledger"),
    path("hospitals/<int:pk>/plan/", views.HospitalPlanView.as_view(), name="platform-plan"),
    path("defaults/", views.DefaultsView.as_view(), name="platform-defaults"),
    path("audit-log/", views.AuditLogView.as_view(), name="platform-audit-log"),
]
