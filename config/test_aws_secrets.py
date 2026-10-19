"""config.aws_secrets: the AWS Secrets Manager payload wins, the environment / .env is the fallback."""

from __future__ import annotations

import json

import pytest
from django.core.exceptions import ImproperlyConfigured

from config import aws_secrets
from config.aws_secrets import get_secret, load_aws_secrets


class FakeSecretsManager:
    """Stands in for boto3's secretsmanager client: records the request, returns a canned secret."""

    def __init__(self, payload=None, error: Exception | None = None, secret_string: str | None = None):
        self.payload = payload
        self.error = error
        self.secret_string = secret_string
        self.requested: list[str] = []
        self.region: str | None = None
        self.service: str | None = None

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3's keyword
        self.requested.append(SecretId)
        if self.error:
            raise self.error
        if self.secret_string is not None:
            return {"SecretString": self.secret_string}
        return {"SecretString": json.dumps(self.payload)}


@pytest.fixture()
def fake_aws(monkeypatch):
    """Patch boto3's session so no network call is made; returns the fake client to assert against."""
    fake = FakeSecretsManager()

    class FakeSession:
        def client(self, service_name, region_name):
            fake.service = service_name
            fake.region = region_name
            return fake

    import boto3

    monkeypatch.setattr(boto3.session, "Session", FakeSession)
    return fake


@pytest.fixture(autouse=True)
def isolated_payload(monkeypatch):
    """Each test starts with no AWS payload loaded and leaves the module as it found it."""
    monkeypatch.setattr(aws_secrets, "_secrets", {})
    for name in ("USE_AWS_SECRETS", "AWS_SECRETS_NAME", "AWS_REGION_NAME"):
        monkeypatch.delenv(name, raising=False)


# --- the switch ----------------------------------------------------------------------------------


def test_switch_off_reads_env_and_never_touches_aws(monkeypatch, fake_aws):
    monkeypatch.setenv("USE_AWS_SECRETS", "0")
    monkeypatch.setenv("DB_PASSWORD", "from-dotenv")

    assert load_aws_secrets() == {}
    assert fake_aws.requested == []
    assert get_secret("DB_PASSWORD") == "from-dotenv"
    assert get_secret("MISSING", "fallback") == "fallback"


def test_switch_unset_means_off(fake_aws):
    assert load_aws_secrets() == {}
    assert fake_aws.requested == []


@pytest.mark.parametrize("flag", ["1", "true", "True", "yes", "on"])
def test_switch_on_fetches_the_named_secret(monkeypatch, fake_aws, flag):
    monkeypatch.setenv("USE_AWS_SECRETS", flag)
    monkeypatch.setenv("AWS_SECRETS_NAME", "manageopd/dev")
    fake_aws.payload = {"DB_PASSWORD": "from-aws"}

    assert load_aws_secrets() == {"DB_PASSWORD": "from-aws"}
    assert fake_aws.requested == ["manageopd/dev"]
    assert fake_aws.service == "secretsmanager"


def test_region_defaults_to_ap_south_1(monkeypatch, fake_aws):
    monkeypatch.setenv("USE_AWS_SECRETS", "True")
    monkeypatch.setenv("AWS_SECRETS_NAME", "manageopd/dev")
    fake_aws.payload = {}

    load_aws_secrets()

    assert fake_aws.region == "ap-south-1"


def test_region_can_be_overridden(monkeypatch, fake_aws):
    monkeypatch.setenv("USE_AWS_SECRETS", "True")
    monkeypatch.setenv("AWS_SECRETS_NAME", "manageopd/dev")
    monkeypatch.setenv("AWS_REGION_NAME", "ap-southeast-1")
    fake_aws.payload = {}

    load_aws_secrets()

    assert fake_aws.region == "ap-southeast-1"


def test_secret_name_is_mandatory(monkeypatch, fake_aws):
    """Without a project-specific name we could silently read another project's secret."""
    monkeypatch.setenv("USE_AWS_SECRETS", "True")
    monkeypatch.setenv("AWS_SECRETS_NAME", "   ")

    with pytest.raises(ImproperlyConfigured, match="AWS_SECRETS_NAME"):
        load_aws_secrets()
    assert fake_aws.requested == []


# --- failing closed ------------------------------------------------------------------------------


def test_fetch_failure_stops_startup(monkeypatch, fake_aws):
    monkeypatch.setenv("USE_AWS_SECRETS", "True")
    monkeypatch.setenv("AWS_SECRETS_NAME", "manageopd/prod")
    fake_aws.error = RuntimeError("AccessDeniedException")

    with pytest.raises(ConnectionError, match="manageopd/prod"):
        load_aws_secrets()
    assert aws_secrets._secrets == {}


def test_secret_must_be_a_json_object(monkeypatch, fake_aws):
    monkeypatch.setenv("USE_AWS_SECRETS", "True")
    monkeypatch.setenv("AWS_SECRETS_NAME", "manageopd/prod")
    fake_aws.secret_string = '["not", "an", "object"]'

    with pytest.raises(ConnectionError, match="JSON object"):
        load_aws_secrets()


def test_unparseable_secret_stops_startup(monkeypatch, fake_aws):
    monkeypatch.setenv("USE_AWS_SECRETS", "True")
    monkeypatch.setenv("AWS_SECRETS_NAME", "manageopd/prod")
    fake_aws.secret_string = "DB_PASSWORD=plain-text-not-json"

    with pytest.raises(ConnectionError):
        load_aws_secrets()


# --- lookup order --------------------------------------------------------------------------------


def test_aws_value_beats_env_value(monkeypatch):
    monkeypatch.setattr(aws_secrets, "_secrets", {"DJANGO_SECRET_KEY": "aws"})
    monkeypatch.setenv("DJANGO_SECRET_KEY", "dotenv")

    assert get_secret("DJANGO_SECRET_KEY", "default") == "aws"


def test_key_missing_from_aws_falls_back_to_env_then_default(monkeypatch):
    """A partial secret rollout must never break a deploy."""
    monkeypatch.setattr(aws_secrets, "_secrets", {"OTHER": "x"})
    monkeypatch.setenv("ROCK8_API_KEY", "dotenv")
    monkeypatch.delenv("PLIVO_APP_ID", raising=False)  # the developer's .env may set it

    assert get_secret("ROCK8_API_KEY", "") == "dotenv"
    assert get_secret("PLIVO_APP_ID", "") == ""
    assert get_secret("PLIVO_APP_ID") is None


def test_empty_string_in_aws_is_a_value_not_a_miss(monkeypatch):
    monkeypatch.setattr(aws_secrets, "_secrets", {"WHATSAPP_API_TOKEN": ""})
    monkeypatch.setenv("WHATSAPP_API_TOKEN", "dotenv")

    assert get_secret("WHATSAPP_API_TOKEN", "default") == ""


# --- casting ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("1", True),
        ("0", False),
        ("True", True),
        ("false", False),
        ("yes", True),
        ("", False),
        (True, True),
        (False, False),
    ],
)
def test_bool_cast_accepts_strings_and_json_booleans(monkeypatch, stored, expected):
    monkeypatch.setattr(aws_secrets, "_secrets", {"DJANGO_DEBUG": stored})

    assert get_secret("DJANGO_DEBUG", True, cast=bool) is expected


def test_bool_cast_applies_to_env_fallback_too(monkeypatch):
    monkeypatch.setenv("CELERY_TASK_ALWAYS_EAGER", "1")

    assert get_secret("CELERY_TASK_ALWAYS_EAGER", False, cast=bool) is True


@pytest.mark.parametrize(("stored", "expected"), [("5432", 5432), (5432, 5432), (" 60 ", 60)])
def test_int_cast(monkeypatch, stored, expected):
    monkeypatch.setattr(aws_secrets, "_secrets", {"DB_CONN_MAX_AGE": stored})

    assert get_secret("DB_CONN_MAX_AGE", 0, cast=int) == expected


def test_int_cast_treats_blank_as_default(monkeypatch):
    monkeypatch.setattr(aws_secrets, "_secrets", {"EMAIL_PORT": ""})

    assert get_secret("EMAIL_PORT", 587, cast=int) == 587


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("https://a.example, https://b.example,", ["https://a.example", "https://b.example"]),
        (["https://a.example", " https://b.example "], ["https://a.example", "https://b.example"]),
        ("", []),
    ],
)
def test_list_cast_accepts_comma_strings_and_json_arrays(monkeypatch, stored, expected):
    monkeypatch.setattr(aws_secrets, "_secrets", {"CORS_ALLOWED_ORIGINS": stored})

    assert get_secret("CORS_ALLOWED_ORIGINS", [], cast=list) == expected


def test_list_default_is_returned_untouched_when_nothing_is_set(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)  # the developer's .env may set it
    default = ["http://localhost:5173"]

    assert get_secret("CORS_ALLOWED_ORIGINS", default, cast=list) is default


def test_uncast_values_are_strings(monkeypatch):
    """Secrets Manager may store numbers natively; Django expects DB_PORT etc. as strings."""
    monkeypatch.setattr(aws_secrets, "_secrets", {"DB_PORT": 5432})

    assert get_secret("DB_PORT", "5432") == "5432"

