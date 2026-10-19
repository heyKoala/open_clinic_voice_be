# Deploying the backend (Gunasheela)

The backend serves the Gunasheela front-desk app (`heyKoala/open_clinic_voice_fe_simple`, branch `dev`)
and the AI receptionist's phone calls. Deploy it **before** the frontend: the frontend is static files and
needs this backend on a public HTTPS address.

Branch to deploy: `gunasheela-hospital`.
Configuration and secrets: see **Configuration & Secrets** in `README.md` (AWS Secrets Manager, key list).

## 1. Processes

Python 3.13, PostgreSQL, Redis. Four processes, all from the repo root with the same configuration:

| Process | Command | Why |
| --- | --- | --- |
| Web (HTTP + WebSocket) | `daphne -b 0.0.0.0 -p 8001 config.asgi:application` | API, voice-provider webhooks, `/ws/` live updates |
| Worker | `celery -A config worker -l info` | fetches call recordings and transcripts, places AI calls |
| Scheduler | `celery -A config beat -l info` | every minute: dials due reminder/feedback calls; every 5 min: plans them |
| Redis | managed or local | Celery broker/results and the Channels layer |

Run exactly **one** beat process (two would dial every call twice). Beat keeps its schedule file in
`.celery/` under the repo root (created on start-up), which must be writable.

Required settings besides the ones in the README template: `CELERY_TASK_ALWAYS_EAGER=0`, `CHANNEL_LAYER=redis`,
`REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`.

## 2. Reverse proxy

HTTPS ends at the proxy. The app trusts `X-Forwarded-Proto` (`SECURE_PROXY_SSL_HEADER`), so the proxy
**must set it** on every request, overwriting anything the client sent; without it every request
redirects to itself.

Recommended: frontend and backend on one domain, so login cookies and CSRF just work:

```nginx
server {
    server_name desk.example.com;
    root /var/www/gunasheela/dist;                         # the frontend build

    location /api/ { proxy_pass http://127.0.0.1:8001;
                     proxy_set_header Host $host;
                     proxy_set_header X-Forwarded-Proto $scheme;
                     proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
                     client_max_body_size 25m; }               # knowledge-base uploads
    location /ws/  { proxy_pass http://127.0.0.1:8001;
                     proxy_http_version 1.1;
                     proxy_set_header Upgrade $http_upgrade;
                     proxy_set_header Connection "upgrade";
                     proxy_set_header Host $host;
                     proxy_set_header X-Forwarded-Proto $scheme;
                     proxy_read_timeout 3600; }

    location = /index.html { add_header Cache-Control "no-cache"; }
    location = /sw.js      { add_header Cache-Control "no-cache"; }
    location /assets/      { add_header Cache-Control "public, max-age=31536000, immutable"; }
    location /             { try_files $uri /index.html; }
}
```

With this layout: `DJANGO_ALLOWED_HOSTS=desk.example.com`, `FRONTEND_URL`, `CSRF_TRUSTED_ORIGINS` and
`CORS_ALLOWED_ORIGINS` = `https://desk.example.com`, and `PUBLIC_BASE_URL=https://desk.example.com`.

If the API must live on its own host, keep it a sibling subdomain (`api.example.com` next to
`desk.example.com`; login cookies are SameSite=Lax), set `DJANGO_ALLOWED_HOSTS` / `PUBLIC_BASE_URL` to the
API host, and build the frontend with `VITE_API_BASE_URL=https://api.example.com/api/v1`.
Also set `CSRF_COOKIE_DOMAIN=.example.com` (the shared parent domain), so the frontend can read the CSRF
cookie it must send back on every save; without it every POST/PUT/DELETE fails with 403 "CSRF failed".

`PUBLIC_BASE_URL` must be reachable from the internet: the voice provider calls it at the start of every
phone call (for the agent's configuration) and during the call (booking tools).

## 3. Database and data

```
python manage.py migrate --plan
python manage.py migrate
python manage.py check --deploy
```

The hospital's setup (details, doctors and hours, the AI receptionist's greeting, instructions, voice and
call-audio settings, reminder/feedback scripts, knowledge-base documents, users) lives in the database the
app was configured on. To carry it over, restore the handover dump into the **empty** production database
before running `migrate` (migrate then only applies anything newer):

```
pg_restore --no-owner --no-privileges -d "<production database URL>" manageopd_<date>.dump
```

and unzip `media_<date>.zip` into `MEDIA_ROOT` (knowledge-base files and call recordings). Both come from the
developer as a handover folder (`backups/handover_<date>/`, never committed). The dump was made with
PostgreSQL 18's `pg_dump`; restore with `pg_restore` 18 or newer.

After restoring, change every password from the local `password123` (Settings → Team, or
`python manage.py changepassword <email>`), and remove any test patients you don't want.

Never run `clear_demo`, `trim_demo` or `seed_demo` in production; they are local tools that delete or
generate data.

## 4. Point the hospital number at this server

The phone number (+91 80311 50501) reaches the backend through the HeyKoala voice service, which holds the
webhook address. Once `PUBLIC_BASE_URL` is live:

```
python manage.py register_inbound_number +918031150501 --remove-old
```

It prints `ROCK8_INBOUND_TRUNK_ID=…` and `ROCK8_INBOUND_RULE_ID=…`: put both into the configuration and
restart the web process. Until this runs, calls still go to wherever the number was registered before.

## 5. Smoke test

1. Open the app, sign in as the admin, reload a deep link such as `/app/visits` (should load, not 404).
2. Browser console: no CORS errors, no 403 "CSRF" errors.
3. AI Agent → Test call: talk to the agent, hang up; within ~3 minutes the call is in Reports → Call logs
   with transcript, recording, summary and an outcome.
4. Call the hospital number from a phone: same result, with the caller's number shown.
5. Book an appointment for tomorrow: a reminder call appears for today at the configured time, and the
   worker dials it (watch the worker log).

## Rollback

Code-only releases roll back by redeploying the previous commit. Migrations in this branch only add columns
and tables, so the previous code runs on the migrated database. Keep the pre-deploy database backup until the
smoke test has passed.
