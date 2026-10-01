from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from clinics.models import Clinic


class CrossTenantIsolationTests(TestCase):
    def setUp(self):
        self.clinic_a = Clinic.objects.create(name="Clinic A")
        self.clinic_b = Clinic.objects.create(name="Clinic B")

        self.admin_a = User.objects.create_user(
            clinic=self.clinic_a,
            email="admin.a@example.com",
            full_name="Admin A",
            password="StrongPass!123",
            role=User.Role.CLINIC_ADMIN,
            is_verified=True,
        )
        self.admin_b = User.objects.create_user(
            clinic=self.clinic_b,
            email="admin.b@example.com",
            full_name="Admin B",
            password="StrongPass!123",
            role=User.Role.CLINIC_ADMIN,
            is_verified=True,
        )

        self.client_a = APIClient()
        self.client_a.force_authenticate(self.admin_a)

        self.client_b = APIClient()
        self.client_b.force_authenticate(self.admin_b)

    def test_clinic_read_isolation(self):
        response = self.client_b.get(f"/api/v1/clinics/{self.clinic_a.id}/")
        self.assertEqual(response.status_code, 404)

    def test_clinic_write_isolation(self):
        response = self.client_b.patch(
            f"/api/v1/clinics/{self.clinic_a.id}/",
            {"name": "Tampered Name"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)
