"""View-as: HeyKoala staff read a hospital through its own pages, read-only, and only staff can."""
from __future__ import annotations

import pytest

from clinics.models import Clinic
from doctors.models import Doctor
from platform_admin.models import PlatformAuditLog
from platform_admin.tests.test_platform_api import client_for, make_user

pytestmark = pytest.mark.django_db

DOCTORS = "/api/v1/doctors/"


@pytest.fixture
def platform_admin():
    return make_user("platform@heykoala.example", is_staff=True)


def view_as(user, hospital, method="get", url=DOCTORS, **extra):
    headers = {"HTTP_X_VIEW_AS_HOSPITAL": str(hospital)} if hospital is not None else {}
    return getattr(client_for(user), method)(url, **headers, **extra)


def names(response):
    data = response.json()
    rows = data["results"] if isinstance(data, dict) else data
    return {row["full_name"] for row in rows}


class TestReading:
    def test_staff_see_the_hospitals_own_records_and_not_another_hospitals(self, clinic, clinic_b, platform_admin):
        Doctor.objects.create(clinic=clinic, full_name="Dr Here", specialty="GP")
        Doctor.objects.create(clinic=clinic_b, full_name="Dr Elsewhere", specialty="GP")
        response = view_as(platform_admin, clinic.id)
        assert response.status_code == 200 and names(response) == {"Dr Here"}

    def test_a_suspended_hospital_can_still_be_looked_at(self, clinic, platform_admin):
        clinic.status = Clinic.Status.SUSPENDED
        clinic.save()
        assert view_as(platform_admin, clinic.id).status_code == 200

    def test_centres_of_the_hospital_are_included(self, clinic, platform_admin):
        centre = Clinic.objects.create(name="Centre", parent=clinic)
        Doctor.objects.create(clinic=centre, full_name="Dr Centre", specialty="GP")
        assert names(view_as(platform_admin, clinic.id)) == {"Dr Centre"}


class TestReadOnly:
    @pytest.mark.parametrize("method", ["post", "patch", "put", "delete"])
    def test_every_change_is_refused(self, clinic, platform_admin, method):
        doctor = Doctor.objects.create(clinic=clinic, full_name="Dr Here", specialty="GP")
        response = view_as(platform_admin, clinic.id, method, f"{DOCTORS}{doctor.id}/", data={"full_name": "X"}, format="json")
        assert response.status_code == 403 and "read-only" in response.json()["detail"]
        doctor.refresh_from_db()
        assert doctor.full_name == "Dr Here"


class TestWhoMayUseIt:
    def test_hospital_staff_cannot_use_the_header_to_reach_another_hospital(self, clinic, clinic_b, clinic_admin):
        Doctor.objects.create(clinic=clinic, full_name="Dr Mine", specialty="GP")
        Doctor.objects.create(clinic=clinic_b, full_name="Dr Theirs", specialty="GP")
        response = view_as(clinic_admin, clinic_b.id)
        assert response.status_code == 200 and names(response) == {"Dr Mine"}   # the header is ignored

    def test_hospital_staff_are_not_made_read_only_by_the_header(self, clinic, clinic_admin):
        assert view_as(clinic_admin, clinic.id, "post", data={"full_name": "Dr New", "specialty": "GP"}, format="json").status_code != 403

    def test_an_unknown_or_centre_or_garbled_id_is_not_found(self, clinic, platform_admin):
        centre = Clinic.objects.create(name="Centre", parent=clinic)
        for bad in ("999999", str(centre.id), "abc", "1; drop"):
            assert view_as(platform_admin, bad).status_code == 404

    def test_without_the_header_a_platform_login_still_has_no_hospital(self, platform_admin):
        assert view_as(platform_admin, None).status_code == 403


class TestNothingLeaksOntoTheStaffLogin:
    def test_me_shows_the_hospital_but_the_staff_row_is_untouched(self, clinic, platform_admin):
        platform_admin.role = "doctor"
        platform_admin.save()
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken
        client = APIClient()   # the real sign-in path (JWT), not forced authentication
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(platform_admin)}", HTTP_X_VIEW_AS_HOSPITAL=str(clinic.id))
        body = client.get("/api/v1/auth/me/").json()
        assert body["clinic"] == clinic.id and body["role"] == "clinic_admin"
        platform_admin.refresh_from_db()
        assert platform_admin.clinic_id is None and platform_admin.role == "doctor" and not platform_admin.is_clinic_admin

    def test_a_save_during_the_request_does_not_persist_the_borrowed_role(self, clinic, platform_admin):
        from clinics.context import bind_view_as
        from rest_framework.test import APIRequestFactory
        request = APIRequestFactory().get(DOCTORS, HTTP_X_VIEW_AS_HOSPITAL=str(clinic.id))
        assert bind_view_as(request, platform_admin)
        platform_admin.save()
        platform_admin.refresh_from_db()
        assert platform_admin.clinic_id is None and not platform_admin.is_clinic_admin


class TestAudit:
    def test_starting_a_session_is_recorded_with_who_and_which_hospital(self, clinic, platform_admin):
        response = client_for(platform_admin).post(f"/api/v1/platform/hospitals/{clinic.id}/view-as/")
        assert response.status_code == 200 and response.json()["name"] == clinic.name
        entry = PlatformAuditLog.objects.get(action="view_as_hospital")
        assert entry.actor_email == platform_admin.email and entry.clinic_id == clinic.id

    def test_hospital_staff_cannot_start_one(self, clinic, clinic_admin):
        assert client_for(clinic_admin).post(f"/api/v1/platform/hospitals/{clinic.id}/view-as/").status_code == 404
        assert not PlatformAuditLog.objects.filter(action="view_as_hospital").exists()
