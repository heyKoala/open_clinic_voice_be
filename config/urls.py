"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/auth/", include("accounts.urls")),
    path("api/v1/accounts/", include("accounts.urls")),
    path("api/v1/clinics/", include("clinics.urls")),
    path("api/v1/subscriptions/", include("subscriptions.urls")),
    path("api/v1/patients/", include("patients.urls")),
    path("api/v1/doctors/", include("doctors.urls")),
    path("api/v1/appointments/", include("appointments.urls")),
    path("api/v1/queue/", include("queue_mgmt.urls")),
    path("api/v1/followups/", include("followups.urls")),
    path("api/v1/ai/", include("ai_agent.urls")),
    path("api/v1/clinical/", include("clinical.urls")),
    path("api/v1/reports/", include("reports.urls")),
]