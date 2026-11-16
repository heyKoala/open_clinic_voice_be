from django.urls import path, register_converter

from platform_admin import views, views_agent as agent, views_hospital as people


class SignedInt:
    """A whole number that may be negative: an unused invitation is addressed as ``-<its id>``."""

    regex = r"-?[0-9]+"

    def to_python(self, value):
        return int(value)

    def to_url(self, value):
        return str(value)


register_converter(SignedInt, "signed")

urlpatterns = [
    path("overview/", views.OverviewView.as_view(), name="platform-overview"),
    path("hospitals/", views.HospitalListView.as_view(), name="platform-hospitals"),
    path("hospitals/<int:pk>/", views.HospitalDetailView.as_view(), name="platform-hospital"),
    path("hospitals/<int:pk>/credits/", views.HospitalCreditsView.as_view(), name="platform-credits"),
    path("hospitals/<int:pk>/ledger/", views.HospitalLedgerView.as_view(), name="platform-ledger"),
    path("hospitals/<int:pk>/plan/", views.HospitalPlanView.as_view(), name="platform-plan"),

    path("hospitals/<int:pk>/users/", people.HospitalUsersView.as_view(), name="platform-users"),
    path("hospitals/<int:pk>/users/<signed:ref>/", people.HospitalUserView.as_view(), name="platform-user"),
    path("hospitals/<int:pk>/users/<signed:ref>/resend-invite/", people.ResendInviteView.as_view(), name="platform-user-resend"),
    path("hospitals/<int:pk>/users/<signed:ref>/revoke-invite/", people.RevokeInviteView.as_view(), name="platform-user-revoke"),
    path("hospitals/<int:pk>/users/<signed:ref>/reset-password/", people.ResetPasswordView.as_view(), name="platform-user-reset"),
    path("hospitals/<int:pk>/users/<signed:ref>/set-password/", people.SetPasswordView.as_view(), name="platform-user-set-password"),

    path("hospitals/<int:pk>/centres/", people.HospitalCentresView.as_view(), name="platform-centres"),
    path("hospitals/<int:pk>/centres/<int:cid>/", people.HospitalCentreView.as_view(), name="platform-centre"),

    path("hospitals/<int:pk>/phone/", people.HospitalPhoneView.as_view(), name="platform-phone"),
    path("hospitals/<int:pk>/phone/search/", people.HospitalPhoneSearchView.as_view(), name="platform-phone-search"),
    path("hospitals/<int:pk>/phone/activate/", people.HospitalPhoneActivateView.as_view(), name="platform-phone-activate"),

    path("hospitals/<int:pk>/integrations/", people.HospitalIntegrationsView.as_view(), name="platform-integrations"),
    path("hospitals/<int:pk>/integrations/<str:provider>/", people.HospitalIntegrationView.as_view(), name="platform-integration"),
    path("hospitals/<int:pk>/integrations/<str:provider>/test/", people.HospitalIntegrationTestView.as_view(), name="platform-integration-test"),

    path("hospitals/<int:pk>/agent/", agent.HospitalAgentView.as_view(), name="platform-agent"),
    path("hospitals/<int:pk>/agent/providers/", agent.HospitalAgentProvidersView.as_view(), name="platform-agent-providers"),
    path("hospitals/<int:pk>/agent/voices/", agent.HospitalAgentVoicesView.as_view(), name="platform-agent-voices"),
    path("hospitals/<int:pk>/agent/preview/", agent.HospitalAgentPreviewView.as_view(), name="platform-agent-preview"),

    path("defaults/", views.DefaultsView.as_view(), name="platform-defaults"),
    path("defaults/agent/", agent.TemplateAgentView.as_view(), name="platform-agent-template"),
    path("defaults/agent/providers/", agent.TemplateAgentProvidersView.as_view(), name="platform-agent-template-providers"),
    path("defaults/agent/voices/", agent.TemplateAgentVoicesView.as_view(), name="platform-agent-template-voices"),
    path("defaults/agent/preview/", agent.TemplateAgentPreviewView.as_view(), name="platform-agent-template-preview"),
    path("audit-log/", views.AuditLogView.as_view(), name="platform-audit-log"),
]
