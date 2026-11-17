"""create_platform_admin: a staff login with no hospital, a strong password never taken on the command line."""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from accounts.models import User
from platform_admin.models import PlatformAuditLog
from platform_admin.tests.test_platform_api import client_for, make_user

pytestmark = pytest.mark.django_db

STRONG = "Platform#Admin2026"


@pytest.fixture(autouse=True)
def no_terminal(monkeypatch):
    """Tests have no terminal, so the password comes from the environment variable, as in a script."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)


def run(*args):
    out = StringIO()
    call_command("create_platform_admin", *args, stdout=out)
    return out.getvalue()


class TestCreate:
    def test_makes_a_verified_staff_login_with_no_hospital_that_can_use_the_platform(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_ADMIN_PASSWORD", STRONG)
        assert "created" in run("Admin@HeyKoala.example", "--name", "Ops")
        user = User.objects.get(email="admin@heykoala.example")
        assert user.is_staff and user.is_verified and user.is_active and user.clinic_id is None and not user.is_superuser
        assert user.check_password(STRONG) and user.full_name == "Ops"
        assert client_for(user).get("/api/v1/platform/overview/").status_code == 200
        assert PlatformAuditLog.objects.filter(action="create_platform_admin").count() == 1

    def test_running_it_again_changes_nothing(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_ADMIN_PASSWORD", STRONG)
        run("admin@heykoala.example")
        monkeypatch.setenv("PLATFORM_ADMIN_PASSWORD", "Another#Pass2026")
        assert "already a platform admin" in run("admin@heykoala.example")
        assert User.objects.get(email="admin@heykoala.example").check_password(STRONG)

    def test_reset_password(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_ADMIN_PASSWORD", STRONG)
        run("admin@heykoala.example")
        monkeypatch.setenv("PLATFORM_ADMIN_PASSWORD", "Another#Pass2026")
        assert "New password" in run("admin@heykoala.example", "--reset-password")
        assert User.objects.get(email="admin@heykoala.example").check_password("Another#Pass2026")


class TestRefusals:
    def test_a_hospital_users_email_is_refused_and_left_alone(self, clinic, monkeypatch):
        monkeypatch.setenv("PLATFORM_ADMIN_PASSWORD", STRONG)
        hospital_user = make_user("someone@hospital.example", clinic=clinic)
        with pytest.raises(CommandError, match="belongs to a hospital"):
            run("someone@hospital.example")
        hospital_user.refresh_from_db()
        assert not hospital_user.is_staff and hospital_user.clinic_id == clinic.id

    def test_hospital_attached_staff_login_is_refused_too(self, clinic, monkeypatch):
        monkeypatch.setenv("PLATFORM_ADMIN_PASSWORD", STRONG)
        make_user("staff@hospital.example", clinic=clinic, is_staff=True)
        with pytest.raises(CommandError, match="belongs to a hospital"):
            run("staff@hospital.example")

    @pytest.mark.parametrize("weak", ["short1!", "alllowercase#2026", "NoDigitsHere!!", "NoSpecial2026Abc"])
    def test_a_weak_password_is_refused(self, weak, monkeypatch):
        monkeypatch.setenv("PLATFORM_ADMIN_PASSWORD", weak)
        with pytest.raises(CommandError, match="too weak"):
            run("admin@heykoala.example")
        assert not User.objects.filter(email="admin@heykoala.example").exists()

    def test_no_password_given_is_refused(self, monkeypatch):
        monkeypatch.delenv("PLATFORM_ADMIN_PASSWORD", raising=False)
        with pytest.raises(CommandError, match="PLATFORM_ADMIN_PASSWORD"):
            run("admin@heykoala.example")

    def test_a_bad_email_is_refused(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_ADMIN_PASSWORD", STRONG)
        with pytest.raises(CommandError, match="valid email"):
            run("not-an-email")
