"""Cut the seeded demo data down to what a showcase needs, keeping everything consistent.

Keeps, for the main hospital: the admin and receptionists, a few doctors, about a dozen patients (real test
patients included), a few dozen appointments spread over the past week, today and the coming days, one of them
booked by the AI, the real call logs plus a few seeded ones, and only the follow-ups, queue tokens, clinical
records and messages that belong to what is kept. Reminder and feedback calls are planned again from the kept
appointments. Other hospitals are left untouched.

Always run with --dry-run first; the real run takes a pg_dump backup into backups/ before deleting anything.
"""
from __future__ import annotations

import os
import subprocess
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from ai_agent.models import CallLog
from appointments import journey
from appointments.models import Appointment, PatientCall
from clinical.models import ClinicalAttachment, ClinicalEncounter, ClinicalNote, PatientHealthRecord, SymptomSummary
from clinics.models import Clinic
from doctors.models import Doctor
from followups.models import FollowUp
from patients.models import MessageLog, Patient
from queue_mgmt.models import QueueToken

# How much to keep.
KEEP_DOCTORS = 5
KEEP_PATIENTS = 20
KEEP_PAST_WEEK = 10
KEEP_TODAY = 6
KEEP_TOMORROW = 5
KEEP_LATER = 5
KEEP_CALL_LOGS = 25
KEEP_FOLLOWUPS = 5
# Patients that must stay whatever happens (real test numbers).
PROTECTED_PATIENT_PHONES = ("9632214132",)


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True, help="The main hospital's id (other hospitals are not touched)")
        parser.add_argument("--dry-run", action="store_true", help="Show what would be kept and deleted; change nothing")
        parser.add_argument("--no-backup", action="store_true", help="Skip the pg_dump backup (not recommended)")

    # --- choosing what to keep -------------------------------------------------------------------

    def choose(self, clinic):
        now = timezone.now()
        today = timezone.localdate()
        day = lambda d: (timezone.make_aware(timezone.datetime.combine(d, timezone.datetime.min.time())),
                         timezone.make_aware(timezone.datetime.combine(d, timezone.datetime.max.time())))

        # Doctors: the admin's own profile stays (the admin account points at it); then the busiest ones.
        protected = [p for p in Patient.objects.filter(clinic=clinic) if any(p.phone.replace(" ", "").endswith(x) for x in PROTECTED_PATIENT_PHONES)]
        patient_ids = {p.id for p in protected}
        # Doctors: the admin's own profile stays (the admin account points at it), so do the doctors of the
        # protected patients' appointments; the rest of the quota goes to the busiest.
        doctors = list(Doctor.objects.filter(clinic=clinic, user__role=User.Role.CLINIC_ADMIN))
        doctors += list(Doctor.objects.filter(clinic=clinic, appointments__patient_id__in=patient_ids, appointments__is_active=True).distinct())
        from django.db.models import Count
        busiest = (Doctor.objects.filter(clinic=clinic).exclude(id__in=[d.id for d in doctors])
                   .annotate(n=Count("appointments")).order_by("-n")[:max(0, KEEP_DOCTORS - (len(doctors) - 1))])
        doctors += list(busiest)
        doctor_ids = {d.id for d in doctors}
        appointments = list(Appointment.objects.filter(clinic=clinic, is_active=True, patient_id__in=patient_ids))

        # One AI booking: an appointment a seeded call log already shows as booked, preferably today or upcoming.
        ai_logs = (CallLog.objects.filter(clinic=clinic, outcome__icontains="booked", appointment__isnull=False,
                                          appointment__is_active=True, appointment__doctor_id__in=doctor_ids)
                   .exclude(appointment__status="cancelled").select_related("appointment"))
        ai_log = sorted(ai_logs, key=lambda l: (timezone.localtime(l.appointment.starts_at).date() < today, l.appointment.starts_at))[0] if ai_logs else None
        ai_appointment = ai_log.appointment if ai_log else None
        if ai_appointment:
            appointments.append(ai_appointment)
            patient_ids.add(ai_appointment.patient_id)

        def pick(queryset, limit, statuses=None):
            """Add appointments, preferring ones whose patient is already kept, up to the patient cap."""
            rows = list(queryset.filter(doctor_id__in=doctor_ids).select_related("patient"))
            if statuses:  # keep a mix: round-robin over the wanted statuses
                by_status = {s: [a for a in rows if a.status == s] for s in statuses}
                rows = []
                while len(rows) < limit * 5 and any(by_status.values()):
                    for s in statuses:
                        if by_status[s]:
                            rows.append(by_status[s].pop(0))
            chosen = 0
            for a in sorted(rows, key=lambda a: (a.patient_id not in patient_ids, a.starts_at)):
                if chosen >= limit:
                    break
                if a.patient_id not in patient_ids and len(patient_ids) >= KEEP_PATIENTS:
                    continue
                if a in appointments:
                    continue
                appointments.append(a)
                patient_ids.add(a.patient_id)
                chosen += 1

        start, end = day(today - timedelta(days=7))[0], day(today - timedelta(days=1))[1]
        pick(Appointment.objects.filter(clinic=clinic, is_active=True, starts_at__range=(start, end)), KEEP_PAST_WEEK,
             statuses=["completed", "no_show", "cancelled", "completed"])
        pick(Appointment.objects.filter(clinic=clinic, is_active=True, starts_at__range=day(today)), KEEP_TODAY,
             statuses=["scheduled", "checked_in", "completed", "scheduled"])
        pick(Appointment.objects.filter(clinic=clinic, is_active=True, starts_at__range=day(today + timedelta(days=1)), status="scheduled"), KEEP_TOMORROW)
        pick(Appointment.objects.filter(clinic=clinic, is_active=True, status="scheduled",
                                        starts_at__range=(day(today + timedelta(days=2))[0], day(today + timedelta(days=6))[1])), KEEP_LATER)
        appointment_ids = {a.id for a in appointments}

        # One AI booking: a kept past appointment that a seeded call log already shows as booked.

        # Call logs: every real one (not created in the seed's burst), those about kept appointments, then a
        # spread of seeded ones over the last three weeks with varied outcomes and sentiment.
        seed_burst = CallLog.objects.filter(clinic=clinic).order_by("created_at").values_list("created_at", flat=True)
        seed_burst = list(seed_burst)
        from collections import Counter
        minute = Counter(t.replace(second=0, microsecond=0) for t in seed_burst).most_common(1)[0][0]
        real_logs = CallLog.objects.filter(clinic=clinic).exclude(created_at__range=(minute, minute + timedelta(minutes=2)))
        log_ids = set(real_logs.values_list("id", flat=True))
        log_ids |= set(CallLog.objects.filter(clinic=clinic, appointment_id__in=appointment_ids).values_list("id", flat=True))
        recent = CallLog.objects.filter(clinic=clinic, occurred_at__gte=now - timedelta(days=21)).exclude(id__in=log_ids).order_by("-occurred_at")
        buckets = {}
        for log in recent:
            buckets.setdefault((log.outcome, log.sentiment, log.is_test), []).append(log)
        while len(log_ids) < KEEP_CALL_LOGS and any(buckets.values()):
            for key in list(buckets):
                if buckets[key] and len(log_ids) < KEEP_CALL_LOGS:
                    log_ids.add(buckets[key].pop(0).id)
        # Kept call logs must only point at kept patients and appointments.
        for log in CallLog.objects.filter(id__in=log_ids):
            if log.patient_id and log.patient_id not in patient_ids:
                if len(patient_ids) < KEEP_PATIENTS + 10:
                    patient_ids.add(log.patient_id)
                else:
                    log_ids.discard(log.id)

        followup_ids = list(FollowUp.objects.filter(clinic=clinic, patient_id__in=patient_ids, is_active=True)
                            .order_by("scheduled_for").values_list("id", flat=True)[:KEEP_FOLLOWUPS])
        return {"doctors": doctor_ids, "patients": patient_ids, "appointments": appointment_ids, "ai_appointment": ai_appointment,
                "call_logs": log_ids, "followups": set(followup_ids)}

    # --- doing it ----------------------------------------------------------------------------------

    def backup(self):
        db = settings.DATABASES["default"]
        out = Path(settings.BASE_DIR) / "backups"
        out.mkdir(exist_ok=True)
        path = out / f"{db['NAME']}_before_trim_{timezone.localtime():%Y%m%d_%H%M%S}.dump"
        pg_dump = os.getenv("PG_DUMP", r"C:\Program Files\PostgreSQL\18\bin\pg_dump.exe")
        env = {**os.environ, "PGPASSWORD": db["PASSWORD"]}
        result = subprocess.run([pg_dump, "-Fc", "-h", db["HOST"], "-p", str(db["PORT"]), "-U", db["USER"], "-f", str(path), db["NAME"]],
                                env=env, capture_output=True, text=True)
        if result.returncode != 0:
            raise CommandError(f"Backup failed, nothing deleted: {result.stderr.strip()}")
        return path

    def handle(self, *args, **options):
        clinic = Clinic.objects.get(id=options["clinic"])
        if clinic.parent_id:
            raise CommandError("Give the main hospital's id.")
        keep = self.choose(clinic)
        write = self.stdout.write

        def count(model, **filters):
            return model.objects.filter(clinic=clinic, **filters).count()

        plan = [
            ("doctors", Doctor, Doctor.objects.filter(clinic=clinic).exclude(id__in=keep["doctors"])),
            ("patients", Patient, Patient.objects.filter(clinic=clinic).exclude(id__in=keep["patients"])),
            ("appointments", Appointment, Appointment.objects.filter(clinic=clinic).exclude(id__in=keep["appointments"])),
            ("call logs", CallLog, CallLog.objects.filter(clinic=clinic).exclude(id__in=keep["call_logs"])),
            ("follow-ups", FollowUp, FollowUp.objects.filter(clinic=clinic).exclude(id__in=keep["followups"])),
            ("queue tokens", QueueToken, QueueToken.objects.filter(clinic=clinic).exclude(appointment_id__in=keep["appointments"])),
            ("clinical encounters", ClinicalEncounter, ClinicalEncounter.objects.filter(clinic=clinic).exclude(appointment_id__in=keep["appointments"])),
            ("messages", MessageLog, MessageLog.objects.filter(clinic=clinic).exclude(patient_id__in=keep["patients"])),
            ("health records", PatientHealthRecord, PatientHealthRecord.objects.filter(clinic=clinic).exclude(patient_id__in=keep["patients"])),
        ]
        write(f"\n{clinic.name}: keep {len(keep['doctors'])} doctors, {len(keep['patients'])} patients, "
              f"{len(keep['appointments'])} appointments, {len(keep['call_logs'])} call logs, {len(keep['followups'])} follow-ups")
        if keep["ai_appointment"]:
            a = keep["ai_appointment"]
            write(f"AI-booked appointment: #{a.id} {a.patient.full_name} with Dr. {a.doctor.full_name} on {timezone.localtime(a.starts_at):%d %b %H:%M}")
        write("\nTo delete:")
        for label, model, qs in plan:
            write(f"  {label:20s} {qs.count():5d} of {count(model)}")
        write(f"  {'reminder/feedback':20s} {count(PatientCall):5d} (planned again from the kept appointments)")
        if options["dry_run"]:
            write("\nDry run: nothing changed.")
            return

        if not options["no_backup"]:
            write(f"\nBackup: {self.backup()}")

        with transaction.atomic():
            # Dependents first, so protected foreign keys never block a delete.
            PatientCall.objects.filter(clinic=clinic).delete()
            SymptomSummary.objects.filter(clinic=clinic).exclude(encounter__appointment_id__in=keep["appointments"]).delete()
            ClinicalNote.objects.filter(clinic=clinic).exclude(encounter__appointment_id__in=keep["appointments"]).delete()
            ClinicalAttachment.objects.filter(clinic=clinic).exclude(patient_id__in=keep["patients"]).delete()
            for label, model, qs in plan[5:]:   # tokens, encounters, messages, health records
                qs.delete()
            for label, model, qs in plan[3:5]:  # call logs, follow-ups
                qs.delete()
            plan[2][2].delete()                  # appointments
            QueueToken.objects.filter(clinic=clinic).exclude(patient_id__in=keep["patients"]).delete()
            plan[1][2].delete()                  # patients
            gone_doctor_users = list(Doctor.objects.filter(clinic=clinic).exclude(id__in=keep["doctors"]).values_list("user_id", flat=True))
            plan[0][2].delete()                  # doctors
            User.objects.filter(id__in=[u for u in gone_doctor_users if u]).delete()
            if keep["ai_appointment"]:
                Appointment.objects.filter(id=keep["ai_appointment"].id).update(source="phone")
            planned = journey.plan_all()
        write(f"\nDone. Reminder and feedback calls planned again for {planned} appointments.")
        write(f"Now: {count(Doctor)} doctors, {count(Patient)} patients, {count(Appointment)} appointments, "
              f"{count(CallLog)} call logs, {count(PatientCall)} reminder/feedback calls.")
