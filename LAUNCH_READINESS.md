# Launch readiness

## Deployment gate

Use a managed secret store for `DJANGO_SECRET_KEY`, database credentials, SMTP credentials and broker credentials. Never place production values in `.env` or source control. Production requires `DJANGO_DEBUG=0`, explicit HTTPS hosts/origins, TLS database connectivity, and HTTPS-only auth cookies.

Deploy in this order: take and verify an encrypted database backup; run `manage.py migrate --plan`; run `manage.py migrate`; run `manage.py check --deploy`; perform smoke tests; then switch traffic. Roll back application code only when migrations are backward compatible. For destructive migrations, ship expand/backfill/contract releases and restore the verified backup if rollback is needed.

## Backup and disaster recovery

Create encrypted daily database backups, retain them according to the approved policy, and store encryption keys separately from backup storage. Monthly: restore to an isolated environment, run migrations, verify a tenant-scoped patient/appointment/queue query, and record RPO/RTO results. Incident runbook: declare incident, stop writes if integrity is at risk, preserve logs/audit events, restore to a clean database, validate tenant isolation and queue state, rotate affected secrets, then communicate recovery.

## Privacy and operations gates

Set written retention periods for recordings, transcripts, audit logs, exports, and clinical records before ingesting production data. Implement scheduled deletion only after the clinic's legal retention requirement has been approved. Obtain reviewed privacy notice and explicit call-recording/AI consent language, and complete local healthcare/privacy counsel review before launch.

Configure error tracking, uptime checks for `/api/v1/reports/live-queue/`, alerting, and load tests for concurrent booking and `call_next` transitions. Staging acceptance requires: `check --deploy`, role/tenant tests, successful encrypted-backup restore, booking/queue load test, and role-specific end-to-end tests. Do not mark staging approved until all have recorded evidence.
