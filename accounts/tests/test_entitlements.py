from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from django.db import connection
from django.test import TransactionTestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from clinics.models import Clinic
from subscriptions.models import ClinicEntitlement, SubscriptionPlan


class Phase2EntitlementAndRolesTests(TransactionTestCase):
    def setUp(self):
        self.clinic_a = Clinic.objects.create(name="Clinic Alpha")
        self.trial_plan = SubscriptionPlan.get_default_plan(SubscriptionPlan.PlanType.TRIAL)
        self.entitlement_a = ClinicEntitlement.objects.create(
            clinic=self.clinic_a,
            plan=self.trial_plan,
        )

        self.admin_a = User.objects.create_user(
            clinic=self.clinic_a,
            email="admin.alpha@example.com",
            full_name="Admin Alpha",
            password="Password123!",
            role=User.Role.CLINIC_ADMIN,
            is_verified=True,
        )

        self.clinic_b = Clinic.objects.create(name="Clinic Beta")
        self.entitlement_b = ClinicEntitlement.objects.create(
            clinic=self.clinic_b,
            plan=self.trial_plan,
        )
        self.admin_b = User.objects.create_user(
            clinic=self.clinic_b,
            email="admin.beta@example.com",
            full_name="Admin Beta",
            password="Password123!",
            role=User.Role.CLINIC_ADMIN,
            is_verified=True,
        )

        self.client_a = APIClient()
        self.client_a.force_authenticate(self.admin_a)

        self.client_b = APIClient()
        self.client_b.force_authenticate(self.admin_b)

    def test_admin_only_invite_creation(self):
        # Create non-admin user
        doctor = User.objects.create_user(
            clinic=self.clinic_a,
            email="doctor.unauth@example.com",
            full_name="Doctor Unauth",
            password="Password123!",
            role=User.Role.DOCTOR,
            is_verified=True,
        )
        doc_client = APIClient()
        doc_client.force_authenticate(doctor)

        res = doc_client.post("/api/v1/accounts/invites/", {"email": "test.invite@example.com", "role": "doctor"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_seat_limit_reached_structured_response(self):
        # Trial plan allows 3 doctors max.
        # Send 3 invites
        for i in range(3):
            res = self.client_a.post(
                "/api/v1/accounts/invites/",
                {"email": f"doc{i}@example.com", "role": "doctor"},
                format="json",
            )
            self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        # 4th invite should fail with seat_limit_reached
        res_exceeded = self.client_a.post(
            "/api/v1/accounts/invites/",
            {"email": "doc_exceeded@example.com", "role": "doctor"},
            format="json",
        )
        self.assertEqual(res_exceeded.status_code, status.HTTP_403_FORBIDDEN)
        data = res_exceeded.json()
        self.assertEqual(data["error_code"], "seat_limit_reached")
        self.assertEqual(data["role"], "doctor")
        self.assertEqual(data["current_active"], 3)
        self.assertEqual(data["max_allowed"], 3)

    def test_user_deactivation_reclaims_seat(self):
        # Create 2 active doctor users on trial (limit 3)
        doc1 = User.objects.create_user(
            clinic=self.clinic_a,
            email="doc1@example.com",
            full_name="Doc 1",
            password="Password123!",
            role=User.Role.DOCTOR,
            is_verified=True,
        )
        User.objects.create_user(
            clinic=self.clinic_a,
            email="doc2@example.com",
            full_name="Doc 2",
            password="Password123!",
            role=User.Role.DOCTOR,
            is_verified=True,
        )
        # Create 1 invite (3 total active seats consumed)
        self.client_a.post("/api/v1/accounts/invites/", {"email": "doc3@example.com", "role": "doctor"}, format="json")

        # 4th invite fails
        res = self.client_a.post("/api/v1/accounts/invites/", {"email": "doc4@example.com", "role": "doctor"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        # Deactivate doc1
        deact_res = self.client_a.post(f"/api/v1/accounts/users/{doc1.id}/deactivate/")
        self.assertEqual(deact_res.status_code, status.HTTP_200_OK)

        # Now 4th invite succeeds
        res_reclaimed = self.client_a.post("/api/v1/accounts/invites/", {"email": "doc4@example.com", "role": "doctor"}, format="json")
        self.assertEqual(res_reclaimed.status_code, status.HTTP_201_CREATED)

    def test_disallow_role_escalation(self):
        doctor = User.objects.create_user(
            clinic=self.clinic_a,
            email="doctor.escalate@example.com",
            full_name="Doctor Escalate",
            password="Password123!",
            role=User.Role.DOCTOR,
            is_verified=True,
        )
        client = APIClient()
        client.force_authenticate(doctor)

        res = client.patch("/api/v1/accounts/me/", {"role": "clinic_admin"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        doctor.refresh_from_db()
        self.assertEqual(doctor.role, User.Role.DOCTOR)

    def test_cross_clinic_deactivation_denied(self):
        user_b = User.objects.create_user(
            clinic=self.clinic_b,
            email="user.beta@example.com",
            full_name="User Beta",
            password="Password123!",
            role=User.Role.RECEPTIONIST,
            is_verified=True,
        )

        res = self.client_a.post(f"/api/v1/accounts/users/{user_b.id}/deactivate/")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_concurrent_invites_do_not_exceed_plan_limit(self):
        # 8 parallel workers trying to create doctor invites on a 3-doctor trial plan
        results = []

        def send_invite(idx):
            client = APIClient()
            client.force_authenticate(self.admin_a)
            try:
                r = client.post(
                    "/api/v1/accounts/invites/",
                    {"email": f"parallel_doc_{idx}@example.com", "role": "doctor"},
                    format="json",
                )
                return r.status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(send_invite, range(8)))

        success_count = results.count(status.HTTP_201_CREATED)
        forbidden_count = results.count(status.HTTP_403_FORBIDDEN)

        self.assertEqual(success_count, 3)
        self.assertEqual(forbidden_count, 5)
