from django.urls import path

from clinics.views import ClinicListCreateView, ClinicDetailView, ClinicConfigurationView, ClinicHolidayListCreateView, ClinicHolidayDestroyView, CompleteOnboardingView, PhoneNumberView, PhoneNumberSearchView, PhoneNumberActivateView

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
]
