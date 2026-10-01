from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from django.db import connection
from django.test import TransactionTestCase

from accounts.models import Invitation, User
from accounts.services import create_invitation
from clinics.models import Clinic
from subscriptions.models import ClinicEntitlement, SubscriptionPlan


class InvitationTokenUniquenessTests(TransactionTestCase):
    def setUp(self):
        self.clinic = Clinic.objects.create(name="Concurrency Clinic")
        self.trial_plan = SubscriptionPlan.get_default_plan(SubscriptionPlan.PlanType.TRIAL)
        ClinicEntitlement.objects.create(
            clinic=self.clinic,
            plan=self.trial_plan,
            max_doctors_override=50,
        )
        self.admin = User.objects.create_user(
            clinic=self.clinic,
            email="admin@concurrency.example.com",
            full_name="Concurrency Admin",
            password="StrongPass!123",
            role=User.Role.CLINIC_ADMIN,
            is_verified=True,
        )

    def test_invitation_token_hashes_unique_under_parallel_creation(self):
        if connection.vendor == "sqlite":
            self.skipTest("Parallel write concurrency test is only reliable on PostgreSQL.")

        emails = [f"doctor{i}@example.com" for i in range(20)]

        def create(email):
            try:
                create_invitation(
                    clinic=self.clinic,
                    invited_by=self.admin,
                    email=email,
                    role=User.Role.DOCTOR,
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(create, emails))

        token_hashes = list(Invitation.objects.values_list("token_hash", flat=True))
        self.assertEqual(len(token_hashes), len(set(token_hashes)))
