"""Settings values from AWS Secrets Manager, with the local .env as the fallback.

This is the company-standard pattern (see the DevOps "AWS Secrets Manager Integration Guide"):

1. ``USE_AWS_SECRETS`` is the single switch. It is off by default, so a developer's laptop keeps reading
   the local ``.env`` exactly as before.
2. When it is on, one JSON secret (``AWS_SECRETS_NAME`` in ``AWS_REGION_NAME``) is fetched once at process
   start. Credentials come from boto3's default chain: the EC2 instance role on servers, or
   ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` in the environment for a laptop test against a sandbox
   secret.
3. ``get_secret()`` looks a key up in that payload first, then in the environment (which the ``.env`` file
   has been loaded into), then falls back to the default. A key missing from the secret therefore never
   breaks a deploy.
4. If the switch is on and the secret cannot be fetched, startup fails with ``ConnectionError``. A service
   silently running with missing secrets is worse than one that refuses to boot.

Only ``config/settings.py`` may call ``get_secret()``. Everything else reads ``django.conf.settings``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

DEFAULT_REGION = "ap-south-1"
_TRUE_WORDS = {"1", "true", "yes", "on"}

# The JSON payload of the AWS secret, keyed by setting name. Empty while USE_AWS_SECRETS is off.
_secrets: dict[str, Any] = {}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE_WORDS


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def load_aws_secrets(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Fetch the environment's secret from AWS Secrets Manager when USE_AWS_SECRETS is on.

    Returns the parsed payload (empty when the switch is off) and keeps it for ``get_secret()``.
    """
    global _secrets
    env = os.environ if environ is None else environ

    if not _as_bool(env.get("USE_AWS_SECRETS", "0")):
        _secrets = {}
        return _secrets

    secret_name = env.get("AWS_SECRETS_NAME", "").strip()
    if not secret_name:
        # Mandatory per DevOps: without a project-specific name we would read some other project's secret.
        raise ImproperlyConfigured("USE_AWS_SECRETS is on but AWS_SECRETS_NAME is not set.")
    region_name = env.get("AWS_REGION_NAME", "").strip() or DEFAULT_REGION

    try:
        import boto3  # imported here so .env-only setups need not install it
    except ImportError as exc:
        raise ImproperlyConfigured("USE_AWS_SECRETS is on but boto3 is not installed (pip install boto3).") from exc

    try:
        client = boto3.session.Session().client(service_name="secretsmanager", region_name=region_name)
        response = client.get_secret_value(SecretId=secret_name)
        payload = json.loads(response.get("SecretString") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("the secret must be a JSON object of key/value pairs")
    except Exception as exc:  # fail closed: never boot with half the configuration
        raise ConnectionError(
            f"Failed to fetch secrets from AWS Secrets Manager '{secret_name}' in {region_name}: {exc}"
        ) from exc

    _secrets = payload
    logger.info("Loaded %d settings from AWS Secrets Manager secret '%s'", len(payload), secret_name)
    return _secrets


def get_secret(key: str, default: Any = None, cast: type | None = None) -> Any:
    """Resolve a setting: AWS secret payload, then the environment / .env, then ``default``.

    ``cast`` is ``bool``, ``int`` or ``list`` for typed settings; anything else is returned as a string.
    Secrets Manager stores values as JSON strings ("False", "5432", "a,b"), but native JSON types
    (false, 5432, ["a", "b"]) are accepted too.
    """
    value = _secrets.get(key)
    if value is None:
        value = os.environ.get(key)
    if value is None:
        return default

    if cast is bool:
        return _as_bool(value)
    if cast is int:
        text = str(value).strip()
        return int(text) if text else default
    if cast is list:
        return _as_list(value)
    return str(value)
