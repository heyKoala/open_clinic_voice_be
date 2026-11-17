# Slot platform admin: go-live runbook

This release adds the HeyKoala platform admin (hospitals, credits, users, phone, usage) to Slot.
Deploy the backend first and run its migrations immediately, then the frontend, then the one-time setup
commands below. Production deploys from the `demo` branch in both repos.

## What changes in this release

No new environment variables and no new Python or npm packages. These are the things operations must know:

| Area | Change | What ops must do |
| --- | --- | --- |
| Database | 12 new migrations (accounts 0010–0011, ai_agent 0015–0016, clinics 0015–0016, patients 0009, subscriptions 0002, platform_admin 0001–0005). They only add tables and columns; nothing is deleted. | Run `migrate` straight after the backend deploys |
| Celery beat | New job `settle-call-credits` (`platform_admin.tasks.settle_unbilled_calls`) every 2 minutes | Restart beat and the worker so they pick it up |
| Media storage | Hospital logos are saved under `MEDIA_ROOT/hospital_logos/` (max 1 MB each) and served by the API at `/api/v1/clinics/<id>/logo/` | Make sure `MEDIA_ROOT` is writable and persistent across deploys |
| CORS | New allowed request header `X-View-As-Hospital` (in code, `config/settings.py`) | Nothing, unless a proxy or CDN strips custom headers: it must pass this one and `X-Active-Clinic-Id` |
| Email | Low-credit and out-of-credit alerts go to platform admins and the hospital admin | Existing `DEFAULT_FROM_EMAIL` / SMTP settings and `FRONTEND_URL` must be correct |
| API | New routes under `/api/v1/platform/` (staff logins only; everyone else gets 404) | Nothing |
| Frontend | New `/platform` area; public signup removed | The SPA must fall back to `index.html` for `/platform/...` deep links, as it already does for `/app/...`. `VITE_PLATFORM_MOCK` must NOT be set in the production build |

## Before deploying

Take a full backup of the production database and keep it until the smoke test passes.

```bash
pg_dump -Fc -d "$DATABASE_URL" -f slot_before_platform_admin_$(date +%Y%m%d_%H%M).dump
```

- Order matters: backend → `migrate` → restart → frontend. The new backend code errors on any page until
  `migrate` has run, so run it as soon as the backend is live.
- Never run `clear_demo`, `trim_demo` or `seed_demo` on production.

## Step 1 – Backend

Merge (repo `open_clinic_voice_be`, remote `origin`):

```bash
git checkout demo
git pull origin demo
git merge --no-ff feature/platform-admin -m "Merge the platform admin into demo"
git push origin demo
```

Once the deploy is live, on the production server:

```bash
pip install -r requirements.txt
python manage.py migrate --plan
python manage.py migrate
python manage.py check --deploy
```

`migrate --plan` should list only the 12 migrations named above. Then restart all three processes: the web
process (Daphne/ASGI), the Celery worker and Celery beat.

## Step 2 – Frontend

Only after Step 1 is migrated and restarted. Repo `open_clinic_voice_fe_simple`; its `demo` branch lives on the
remote named `new`, not `origin`.

```bash
git checkout demo
git pull new demo
git merge --no-ff feature/platform-admin -m "Merge the platform admin into demo"
git push new demo
```

Build as usual (`npm ci && npm run build`) with the existing `VITE_API_BASE_URL`, and confirm
`VITE_PLATFORM_MOCK` is unset. Users may need one hard refresh (Ctrl+Shift+R): the app caches an offline copy.

## Step 3 – One-time setup on production

Run on the backend server, in this order. Each is safe to re-run.

1. **Create the HeyKoala platform login.** The password is typed at the prompt, twice, never on the command
   line. Production has no platform login until this runs.

    ```bash
    python manage.py create_platform_admin <platform-admin-email> --name "HeyKoala Admin"
    ```

2. **Check for demo logins left from setup.** Read-only; it changes nothing.

    ```bash
    python manage.py shell -c "from accounts.models import User; [print(u.email, u.role, 'active' if u.is_active else 'off', 'DEMO-PASSWORD' if u.check_password('password123') else 'changed', u.last_login) for u in User.objects.filter(email__endswith='@manageopd.local')]"
    ```

    If any line says `active` and `DEMO-PASSWORD`, switch them off. This keeps their doctor profiles bookable
    and their history intact:

    ```bash
    python manage.py shell -c "
    from django.db import transaction
    from accounts.models import User
    from doctors.models import Doctor
    with transaction.atomic():
        users = list(User.objects.filter(email__endswith='@manageopd.local'))
        print('accounts:', [u.email for u in users])
        print('doctor profiles unlinked:', Doctor.objects.filter(user__in=users).update(user=None))
        for u in users:
            u.is_active = False; u.membership_status = User.MembershipStatus.SUSPENDED; u.set_unusable_password()
            u.save(update_fields=['is_active', 'membership_status', 'password', 'updated_at'])
    print('done')"
    ```

3. **Remove the old shared staff login** `gunasheela.staff@pacewisdom.com` if it exists. Switch it off the same
   way as the demo logins (change the email filter to that address), or ask the developer.

4. **Find Gunasheela's hospital id.**

    ```bash
    python manage.py shell -c "from clinics.models import Clinic; [print(c.id, c.name) for c in Clinic.objects.filter(parent=None)]"
    ```

5. **Adopt Gunasheela into the platform.** Dry run first and read the output, then run it for real. Calls are
   not charged until `--start-charging` runs, and earlier calls are never charged.

    ```bash
    python manage.py adopt_hospital --clinic-id <id> --dry-run
    python manage.py adopt_hospital --clinic-id <id> --start-charging
    ```

6. **Add opening credits** in the admin panel: sign in as the platform login → Hospitals → Gunasheela →
   Credits → Add credits. At 0 credits calls are still answered, but the balance goes negative and alert
   emails are sent.

## Step 4 – Smoke test

- [ ] Browser console on the app shows no CORS or 403 "CSRF" errors
- [ ] Gunasheela's admin signs in: the sidebar shows the full hospital name and "Credits left X / Y";
      Settings → Team lists only people who can sign in
- [ ] Profile → Security: change password works, then sign in again with the new one
- [ ] Platform login signs in and lands on `/platform`; Gunasheela is listed with the right balance
- [ ] Platform → Gunasheela → View as hospital opens their pages read-only, with the bar at the bottom;
      Exit returns to the platform
- [ ] A test call (AI Agent → Test call, or ring the hospital number) appears in Reports → Call logs within
      ~3 minutes, and in Platform → Gunasheela → Credits as a usage entry
- [ ] Worker and beat logs show `settle_unbilled_calls` running every 2 minutes without errors
- [ ] `/login` works; `/signup` no longer exists

## Rollback

Redeploy the previous `demo` commit in both repos and restart web, worker and beat. The migrations only add
tables and columns, so the previous code runs on the migrated database: no database restore is needed for a
code rollback. Restore the pre-deploy backup only if data itself went wrong. Keep that backup until the smoke
test has passed.

If `migrate` fails in Step 1, stop: do not deploy the frontend, and send the full output to the developer.
