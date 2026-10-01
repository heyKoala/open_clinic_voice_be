"""
Project-level pytest fixtures shared across all apps.

Usage in tests:
    def test_something(clinic_admin, admin_client):
        ...

All fixtures that create DB records use @pytest.mark.django_db implicitly
when used in a test function that is itself marked (or uses django_db_setup).
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from accounts.models import User
from clinics.models import Clinic
from subscriptions.models import ClinicEntitlement, SubscriptionPlan


# ---------------------------------------------------------------------------
# Global throttle override — applied to every test in the project.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def eager_celery():
    """Run Celery tasks in-process so tests never publish to the real broker (a running dev
    worker would otherwise execute them against the development database)."""
    from config.celery import app as celery_app
    # The app uses the CELERY_ settings namespace, so plain attribute overrides do not stick.
    celery_app.conf.update(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
    yield
    celery_app.conf.update(CELERY_TASK_ALWAYS_EAGER=False, CELERY_TASK_EAGER_PROPAGATES=False)


@pytest.fixture(autouse=True)
def clear_cache():
    """Throttle counters live in the cache; reset them so tests don't depend on run order."""
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def disable_throttling(settings):
    """
    Set all throttle rates to a very high value so tests never hit rate limits.

    The views declare view-level throttle_classes that look up named scopes
    from DEFAULT_THROTTLE_RATES. Emptying the dict raises ImproperlyConfigured;
    setting extreme values makes throttling a no-op while keeping the view
    initialisation path intact.

    If you need to test throttle behaviour, use a separate TransactionTestCase
    that overrides the specific rate for the scope under test.
    """
    high_rate = "10000/min"
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_RATES": {
            **settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {}),
            "anon": high_rate,
            "user": high_rate,
            "signup": high_rate,
            "login": high_rate,
            "invite": high_rate,
            "password_reset": high_rate,
        },
    }


# ---------------------------------------------------------------------------
# Clinic / plan fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def trial_plan(db) -> SubscriptionPlan:
    """Return (or create) the default Trial subscription plan."""
    return SubscriptionPlan.get_default_plan(SubscriptionPlan.PlanType.TRIAL)


@pytest.fixture()
def clinic(trial_plan) -> Clinic:
    """A clinic on the Trial plan with a ClinicEntitlement."""
    c = Clinic.objects.create(name="Test Clinic")
    ClinicEntitlement.objects.create(clinic=c, plan=trial_plan)
    return c


@pytest.fixture()
def clinic_b(trial_plan) -> Clinic:
    """A second clinic — used for cross-tenant isolation tests."""
    c = Clinic.objects.create(name="Other Clinic")
    ClinicEntitlement.objects.create(clinic=c, plan=trial_plan)
    return c


# ---------------------------------------------------------------------------
# User fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def clinic_admin(clinic) -> User:
    """A verified clinic_admin user belonging to `clinic`."""
    return User.objects.create_user(
        clinic=clinic,
        email="admin@testclinic.example.com",
        full_name="Clinic Admin",
        password="AdminPass123!",
        role=User.Role.CLINIC_ADMIN,
        membership_status=User.MembershipStatus.ACTIVE,
        is_verified=True,
    )


@pytest.fixture()
def doctor_user(clinic) -> User:
    """A verified doctor user belonging to `clinic`."""
    return User.objects.create_user(
        clinic=clinic,
        email="doctor@testclinic.example.com",
        full_name="Test Doctor",
        password="DoctorPass123!",
        role=User.Role.DOCTOR,
        membership_status=User.MembershipStatus.ACTIVE,
        is_verified=True,
    )


@pytest.fixture()
def receptionist_user(clinic) -> User:
    """A verified receptionist user belonging to `clinic`."""
    return User.objects.create_user(
        clinic=clinic,
        email="receptionist@testclinic.example.com",
        full_name="Test Receptionist",
        password="ReceptionPass123!",
        role=User.Role.RECEPTIONIST,
        membership_status=User.MembershipStatus.ACTIVE,
        is_verified=True,
    )


# ---------------------------------------------------------------------------
# API client fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_client() -> APIClient:
    """Bare unauthenticated DRF APIClient."""
    return APIClient()


@pytest.fixture()
def admin_client(clinic_admin) -> APIClient:
    """APIClient force-authenticated as the clinic_admin."""
    client = APIClient()
    client.force_authenticate(user=clinic_admin)
    return client


@pytest.fixture()
def doctor_client(doctor_user) -> APIClient:
    """APIClient force-authenticated as the doctor."""
    client = APIClient()
    client.force_authenticate(user=doctor_user)
    return client


@pytest.fixture()
def receptionist_client(receptionist_user) -> APIClient:
    """APIClient force-authenticated as the receptionist."""
    client = APIClient()
    client.force_authenticate(user=receptionist_user)
    return client
