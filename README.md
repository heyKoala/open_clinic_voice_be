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
