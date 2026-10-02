"""The Saudi German Hospital Dubai seed: one centre, five doctors, one receptionist; safe to re-run."""
from __future__ import annotations

import pytest
from django.core.management import call_command

from accounts.models import User
from ai_agent.agent_settings import effective_settings, render
from clinics.models import Clinic
from doctors.models import Doctor

pytestmark = pytest.mark.django_db


def test_seed_creates_the_hospital_and_can_run_twice():
    call_command("seed_saudi_german_dubai")
    # A changed password is put back by the next run.
    doctor_login = User.objects.get(email="ashok.kumar@sghdubai.local")
    doctor_login.set_password("changed-Pass-1!")
    doctor_login.save()
    call_command("seed_saudi_german_dubai")

    clinic = Clinic.objects.get(name="Saudi German Hospital Dubai")
    assert clinic.parent_id is None and clinic.is_onboarded
    assert clinic.configuration.timezone == "Asia/Dubai"
    assert clinic.entitlement.get_limit("doctor") >= 5

    staff = User.objects.filter(clinic=clinic)
    assert staff.filter(role=User.Role.DOCTOR).count() == 5
    assert staff.filter(role=User.Role.RECEPTIONIST).count() == 1
    assert staff.filter(role=User.Role.CLINIC_ADMIN).count() == 1
    assert all(user.is_verified and user.is_active and user.check_password("password123") for user in staff)
    # Every doctor has a login linked to their doctor profile.
    assert all(Doctor.objects.filter(user=user, clinic=clinic).exists() for user in staff.filter(role=User.Role.DOCTOR))

    doctors = Doctor.objects.filter(clinic=clinic, is_active=True)
    assert doctors.count() == 5
    assert len(set(doctors.values_list("specialty", flat=True))) == 5
    assert all(d.available_from < d.lunch_from < d.lunch_to < d.available_to for d in doctors)

    agent = effective_settings(clinic)
    assert render(agent["first_message"], clinic).startswith("Thank you for calling Saudi German Hospital Dubai")
    assert "Saudi German Hospital Dubai" in render(agent["system_prompt"], clinic)
