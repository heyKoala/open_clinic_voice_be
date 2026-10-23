# ManageOPD Backend (`manageopd-be`)

This directory contains the Django REST Framework API that powers the ManageOPD application. It manages data persistence, multi-tenant clinic isolation, and business logic.

## Technology Stack
- **Framework**: Django 4.x + Django REST Framework (DRF)
- **Database**: PostgreSQL (via `psycopg2-binary`)
- **Authentication**: JWT (JSON Web Tokens) via `djangorestframework-simplejwt`
- **Filtering**: `django-filter`
- **Language**: Python 3.10+

## Django Apps Architecture

The backend is modularized into highly focused apps to separate concerns:

- **`common/`**: Contains utility functions, common models (e.g., BaseModel with auditing fields), and the custom API viewsets (`ClinicScopedModelViewSet`).
- **`accounts/`**: Manages the custom `User` model, authentication logic, invitation lifecycle, and password resets.
- **`clinics/`**: The core multi-tenancy app. Manages the `Clinic` model. Almost all entities link back to a specific clinic.
- **`patients/` & `doctors/`**: Manages the core entities of the platform. Note that `Doctor` is a profile model tied 1-to-1 with a `User`.
- **`appointments/`**: Scheduling and calendar slot reservations.
- **`queue_mgmt/`**: Real-time waiting room logic. Connects Patients, Doctors, and Appointments into a `QueueToken`.
- **`clinical/`**: Holds clinical encounters, AI-generated symptom summaries, and clinical notes/attachments.
- **`ai_agent/`**: Configurations for the regional language AI models and call logs of autonomous interactions.
- **`followups/`**: Models for automated post-consultation campaigns and targeted communication.
- **`subscriptions/`**: Logic for SaaS billing, tracking feature flags, and role-based seat limits.
- **`audit/`**: Strict logging of critical events (logins, logouts, data changes) for HIPAA-style compliance.
- **`reports/`**: Configurable templates to extract JSON/CSV data for the Admin console.

## Security & Multi-Tenancy

ManageOPD is a multi-tenant system. Data privacy between clinics is paramount.

### Custom Middleware
1. **`CurrentUserMiddleware`**: Captures the HTTP request user and attaches them to thread-local storage (`threading.local()`). This allows our models to automatically inject the `changed_by` field on every save without explicitly passing the request object.
2. **`ClinicContextMiddleware`**: Retrieves the active `Clinic` associated with the request user and attaches it to thread-local storage.

### `ClinicScopedModelViewSet`
Instead of using standard DRF `ModelViewSet`s, all domain views inherit from `ClinicScopedModelViewSet` (found in `common/api.py`).
- **Queryset filtering**: It automatically overrides `get_queryset()` to append `.filter(clinic=thread_local_clinic)`. A user can *never* query a record belonging to another clinic.
- **Creation injection**: It automatically overrides `perform_create()` to inject the user's clinic ID into the serializer before saving.

### Cookie-based JWT Auth
Tokens are not sent in JSON bodies for the frontend to store in LocalStorage. Instead:
- Access and Refresh tokens are minted and sent directly to the browser via `HttpOnly` cookies (`auth_token` and `auth_refresh`).
- CSRF validation (`csrftoken`) is enforced via standard Django middleware to protect against Cross-Site Request Forgery.

## Configuration & Secrets (AWS Secrets Manager)

Settings are resolved by `get_secret()` in `config/aws_secrets.py`, which only `config/settings.py` may call
(everything else reads `django.conf.settings`). Lookup order for every key:

1. the JSON secret in **AWS Secrets Manager**, when `USE_AWS_SECRETS` is on;
2. the process environment, which the local `.env` file is loaded into;
3. the default written in `settings.py`.

A key missing from the AWS secret falls back to the `.env` / default, so a partial secret never breaks a deploy.
If the switch is on and the secret cannot be fetched (wrong name or region, missing IAM permission),
startup fails with `ConnectionError` instead of booting with half the configuration.

### Servers (DEV / PROD)

The `.env` on a server holds only the three bootstrap lines. Everything else lives in the secret, which the
EC2 instance role reads with `secretsmanager:GetSecretValue` (no AWS keys on disk).

```
USE_AWS_SECRETS=True
AWS_SECRETS_NAME=<secret name DevOps gives for this project and environment>
AWS_REGION_NAME=ap-south-1
```

`AWS_SECRETS_NAME` is mandatory when the switch is on: there is no default, so the app can never read another
project's secret by accident.

### Laptops

Leave `USE_AWS_SECRETS=0` (or unset) and keep using `.env` as before. To try the AWS path locally against
the dev secret, set `USE_AWS_SECRETS=True`, the secret name, and `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
in `.env`; boto3 picks those up through its normal credential chain. Never put long-lived keys in a server `.env`.

### What goes in the secret

One JSON object per environment, keyed by the same names as `.env.example`. Values are strings
(Secrets Manager's default); booleans accept `True`/`False`/`1`/`0`, lists are comma-separated.
A template of the keys the backend reads:

```json
{
  "DJANGO_SECRET_KEY": "...",
  "DJANGO_DEBUG": "0",
  "DJANGO_ALLOWED_HOSTS": "api.example.com",
  "DJANGO_ENV": "production",
  "DB_ENGINE": "django.db.backends.postgresql",
  "DB_NAME": "...", "DB_USER": "...", "DB_PASSWORD": "...", "DB_HOST": "...", "DB_PORT": "5432",
  "DB_CONN_MAX_AGE": "60", "DB_SSLMODE": "require",
  "FRONTEND_URL": "https://app.example.com",
  "CSRF_TRUSTED_ORIGINS": "https://app.example.com",
  "CORS_ALLOWED_ORIGINS": "https://app.example.com",
  "MEDIA_URL": "/media/", "MEDIA_ROOT": "media",
  "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
  "EMAIL_HOST": "...", "EMAIL_PORT": "587", "EMAIL_HOST_USER": "...", "EMAIL_HOST_PASSWORD": "...",
  "EMAIL_USE_TLS": "1", "DEFAULT_FROM_EMAIL": "noreply@example.com",
  "CELERY_BROKER_URL": "redis://...", "CELERY_RESULT_BACKEND": "redis://...",
  "CELERY_TASK_ALWAYS_EAGER": "0", "CELERY_TASK_TIME_LIMIT": "300", "CELERY_TASK_SOFT_TIME_LIMIT": "240",
  "CELERY_RESULT_EXPIRES": "86400",
  "CHANNEL_LAYER": "redis", "REDIS_URL": "redis://...",
  "LOG_LEVEL": "INFO",
  "ENABLE_SRS_V03_AI_FIRST": "0", "VOICE_AGENT_ESCALATION_POLICY": "conservative",
  "ROCK8_API_KEY": "...", "ROCK8_WEBHOOK_SECRET": "...", "ROCK8_SIP_TRUNK_ID": "...",
  "ROCK8_INBOUND_TRUNK_ID": "...", "ROCK8_INBOUND_RULE_ID": "...",
  "VOICE_WORKER_URL": "https://worker.heykoala.ai", "PUBLIC_BASE_URL": "https://api.example.com",
  "SENTIMENT_API_KEY": "...", "SENTIMENT_API_BASE": "https://api.openai.com/v1", "SENTIMENT_MODEL": "gpt-4o-mini",
  "AGENT_PROMPT_MAX_CHARS": "100000",
  "PLIVO_AUTH_ID": "...", "PLIVO_AUTH_TOKEN": "...", "PLIVO_APP_ID": "...",
  "WHATSAPP_API_TOKEN": "...", "WHATSAPP_PHONE_NUMBER_ID": "...", "WHATSAPP_WEBHOOK_VERIFY_TOKEN": "...",
  "WHATSAPP_APP_SECRET": "...", "WHATSAPP_CONFIRMATION_WEBHOOK_URL": "...",
  "RAZORPAY_KEY_ID": "...", "RAZORPAY_KEY_SECRET": "..."
}
```

Adding a new setting: read it in `settings.py` with `get_secret("NAME", default, cast=...)`, add it to
`.env.example`, and ask DevOps to add the key to each environment's secret.
