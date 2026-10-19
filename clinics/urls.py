from django.urls import path

from clinics.views import (
    ClinicListCreateView, ClinicDetailView, ClinicConfigurationView, ClinicHolidayListCreateView, ClinicHolidayDestroyView,
    CompleteOnboardingView, IntegrationDetailView, IntegrationListView, IntegrationTestView, PhoneNumberView,
    PhoneNumberSearchView, PhoneNumberActivateView, RazorpayWebhookView, WatiWebhookView,
)

urlpatterns = [
    path("", ClinicListCreateView.as_view(), name="clinic-list-create"),
    path("<int:pk>/", ClinicDetailView.as_view(), name="clinic-detail"),
    path("configuration/", ClinicConfigurationView.as_view(), name="clinic-configuration"),
    path("holidays/", ClinicHolidayListCreateView.as_view(), name="clinic-holidays"),
    path("holidays/<str:date>/", ClinicHolidayDestroyView.as_view(), name="clinic-holidays-detail"),
    path("phone-number/", PhoneNumberView.as_view(), name="clinic-phone-number"),
    path("phone-number/search/", PhoneNumberSearchView.as_view(), name="clinic-phone-number-search"),
    path("phone-number/activate/", PhoneNumberActivateView.as_view(), name="clinic-phone-number-activate"),
    path("complete-onboarding/", CompleteOnboardingView.as_view(), name="clinic-complete-onboarding"),
    path("integrations/", IntegrationListView.as_view(), name="clinic-integrations"),
    path("integrations/<str:provider>/", IntegrationDetailView.as_view(), name="clinic-integration"),
    path("integrations/<str:provider>/test/", IntegrationTestView.as_view(), name="clinic-integration-test"),
    path("wati/webhook/<int:clinic_id>/<str:token>/", WatiWebhookView.as_view(), name="clinic-wati-webhook"),
    path("razorpay/webhook/<int:clinic_id>/", RazorpayWebhookView.as_view(), name="clinic-razorpay-webhook"),
]
