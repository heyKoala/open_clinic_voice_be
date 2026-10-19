"""Frontend and API on different subdomains: the login's CSRF cookie must be readable by the frontend."""
import json

import pytest

from accounts.models import User

pytestmark = pytest.mark.django_db


def _login(client):
    return client.post("/api/v1/auth/login/", data=json.dumps({"email": "desk@example.com", "password": "pw-123456!"}),
                       content_type="application/json")


@pytest.fixture()
def desk_user(clinic):
    user = User.objects.create_user(clinic=clinic, email="desk@example.com", password="pw-123456!", role=User.Role.RECEPTIONIST)
    User.objects.filter(id=user.id).update(is_verified=True)
    return user


def test_csrf_cookie_is_host_only_by_default(client, desk_user, settings):
    settings.CSRF_COOKIE_DOMAIN = None
    res = _login(client)
    assert res.status_code == 200 and res.cookies["csrftoken"]["domain"] == ""


def test_csrf_cookie_uses_the_shared_parent_domain_when_set(client, desk_user, settings):
    settings.CSRF_COOKIE_DOMAIN = ".example.com"
    res = _login(client)
    assert res.status_code == 200 and res.cookies["csrftoken"]["domain"] == ".example.com"
