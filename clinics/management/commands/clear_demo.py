"""Remove the seeded demo data now that the hospital runs on real data.

Keeps, for the main hospital: every doctor, the admin and receptionists (demo receptionists are renamed
"Front desk 1/2"), the real call logs (those with a voice-service room id), the patients those calls were with
or booked for, the patients with a protected phone number, their appointments, and the reminder/feedback calls
planned from those appointments. Everything else that the seed created goes: demo patients and their appointments,
seeded call logs, all follow-ups, and the queue tokens, clinical records and messages that belonged to them.
Settings, scripts, knowledge base and integrations are not touched.

Always run with --dry-run first; the real run takes a pg_dump backup into backups/ before deleting anything.

    python manage.py clear_demo --clinic 5 --dry-run
    python manage.py clear_demo --clinic 5
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import User
from ai_agent.models import CallLog
from appointments import journey
from appointments.models import Appointment, PatientCall
from clinical.models import ClinicalAttachment, ClinicalEncounter, ClinicalNote, PatientHealthRecord, SymptomSummary
from clinics.models import Clinic
from followups.models import FollowUp
from patients.models import MessageLog, Patient
from queue_mgmt.models import QueueToken

# Patients that must stay whatever happens (real test numbers), by the last 10 digits.
PROTECTED_PATIENT_PHONES = ("9632214132",)
# The first real call on the hospital line; AI bookings from this day on are real.
REAL_SINCE = "2026-10-16"
DEMO_EMAIL = re.compile(r"^(receptionist|doctor|invited)\d+_c\d+@manageopd\.local$")


def _digits(value) -> str:
    return re.sub(r"\D", "", value or "")[-10:]


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True, help="The main hospital's id")
        parser.add_argument("--dry-run", action="store_true", help="Show what would be kept and deleted; change nothing")
        parser.add_argument("--no-backup", action="store_true", help="Skip the pg_dump backup (not recommended)")

    def choose(self, clinic):
        group = list(clinic.group_clinics())
        real_logs = CallLog.objects.filter(clinic__in=group).exclude(external_call_id="")
        real_phones = {_digits(p) for p in real_logs.values_list("phone", flat=True) if _digits(p)}
        real_phones.update(PROTECTED_PATIENT_PHONES)
        real_since = timezone.make_aware(timezone.datetime.fromisoformat(REAL_SINCE))

        patients = set(real_logs.exclude(patient=None).values_list("patient_id", flat=True))
        patients.update(Appointment.objects.filter(clinic__in=group, source="phone", created_at__gte=real_since)
                        .values_list("patient_id", flat=True))
        for patient_id, phone in Patient.objects.filter(clinic__in=group).values_list("id", "phone"):
            if _digits(phone) in real_phones:
                patients.add(patient_id)
        appointments = set(Appointment.objects.filter(clinic__in=group, patient_id__in=patients).values_list("id", flat=True))
        return {"patients": patients, "appointments": appointments, "call_logs": set(real_logs.values_list("id", flat=True))}

    def backup(self):
        db = settings.DATABASES["default"]
        out = Path(settings.BASE_DIR) / "backups"
        out.mkdir(exist_ok=True)
        path = out / f"{db['NAME']}_before_clear_demo_{timezone.localtime():%Y%m%d_%H%M%S}.dump"
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
        group = list(clinic.group_clinics())
        keep = self.choose(clinic)
        write = self.stdout.write

        def count(model, **filters):
            return model.objects.filter(clinic__in=group, **filters).count()

        plan = [
            ("patients", Patient, Patient.objects.filter(clinic__in=group).exclude(id__in=keep["patients"])),
            ("appointments", Appointment, Appointment.objects.filter(clinic__in=group).exclude(id__in=keep["appointments"])),
            ("call logs", CallLog, CallLog.objects.filter(clinic__in=group).exclude(id__in=keep["call_logs"])),
            ("follow-ups", FollowUp, FollowUp.objects.filter(clinic__in=group)),
            ("queue tokens", QueueToken, QueueToken.objects.filter(clinic__in=group).exclude(appointment_id__in=keep["appointments"])),
            ("clinical encounters", ClinicalEncounter, ClinicalEncounter.objects.filter(clinic__in=group).exclude(appointment_id__in=keep["appointments"])),
            ("messages", MessageLog, MessageLog.objects.filter(clinic__in=group).exclude(patient_id__in=keep["patients"])),
            ("health records", PatientHealthRecord, PatientHealthRecord.objects.filter(clinic__in=group).exclude(patient_id__in=keep["patients"])),
        ]
        demo_staff = User.objects.filter(clinic__in=group, role=User.Role.RECEPTIONIST).filter(
            Q(email__regex=DEMO_EMAIL.pattern)).order_by("id")

        write(f"\n{clinic.name}: keeping {len(keep['patients'])} patients, {len(keep['appointments'])} appointments, "
              f"{len(keep['call_logs'])} call logs, all {count(Appointment.doctor.field.related_model)} doctors")
        for p in Patient.objects.filter(id__in=keep["patients"]).order_by("id"):
            write(f"  keep patient #{p.id} {p.full_name} {p.phone}")
        write("\nTo delete:")
        for label, model, qs in plan:
            write(f"  {label:20s} {qs.count():5d} of {count(model)}")
        write(f"  {'reminder/feedback':20s} {count(PatientCall):5d} (planned again from the kept appointments)")
        for i, user in enumerate(demo_staff, 1):
            write(f"  rename receptionist {user.full_name} ({user.email}) -> Front desk {i}")
        if options["dry_run"]:
            write("\nDry run: nothing changed.")
            return

        if not options["no_backup"]:
            write(f"\nBackup: {self.backup()}")

        with transaction.atomic():
            PatientCall.objects.filter(clinic__in=group).delete()
            SymptomSummary.objects.filter(clinic__in=group).exclude(encounter__appointment_id__in=keep["appointments"]).delete()
            ClinicalNote.objects.filter(clinic__in=group).exclude(encounter__appointment_id__in=keep["appointments"]).delete()
            ClinicalAttachment.objects.filter(clinic__in=group).exclude(patient_id__in=keep["patients"]).delete()
            for label, model, qs in plan[4:]:   # tokens, encounters, messages, health records
                qs.delete()
            for label, model, qs in plan[2:4]:  # call logs, follow-ups
                qs.delete()
            plan[1][2].delete()                  # appointments
            QueueToken.objects.filter(clinic__in=group).exclude(patient_id__in=keep["patients"]).delete()
            plan[0][2].delete()                  # patients
            for i, user in enumerate(demo_staff, 1):
                user.full_name = f"Front desk {i}"
                user.save(update_fields=["full_name"])
            planned = journey.plan_all()
        write(f"\nDone. Reminder and feedback calls planned again for {planned} appointments.")
        write(f"Now: {count(Patient)} patients, {count(Appointment)} appointments, {count(CallLog)} call logs, "
              f"{count(PatientCall)} reminder/feedback calls.")
