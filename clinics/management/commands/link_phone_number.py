"""Link a number that is already on the Plivo account to a clinic (instead of buying a new one).

    python manage.py link_phone_number <clinic_id> <number>

The number is linked to the clinic's main clinic, so all of its centres share it. Linked numbers
are marked ``bought_here=False``: removing them in the app only disconnects them and never
releases them at Plivo.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from clinics import plivo
from clinics.models import Clinic, ClinicConfiguration, ClinicPhoneNumber


class Command(BaseCommand):
    help = "Link a phone number already on the Plivo account to a clinic (and its centres)."

    def add_arguments(self, parser):
        parser.add_argument("clinic_id", type=int)
        parser.add_argument("number", help="With country code, e.g. +91 80 3115 0501")

    def handle(self, clinic_id, number, **options):
        clinic = Clinic.objects.filter(id=clinic_id).first()
        if clinic is None:
            raise CommandError(f"No clinic with id {clinic_id}.")
        root = clinic.root
        digits = plivo.normalize_number(number)

        try:
            info = plivo._request("GET", f"Number/{digits}/")
        except plivo.PlivoError as exc:
            raise CommandError(f"+{digits} is not on the Plivo account ({exc}).")

        existing = ClinicPhoneNumber.objects.filter(number=digits).select_related("clinic").first()
        if existing and existing.clinic_id != root.id:
            raise CommandError(f"+{digits} is already linked to {existing.clinic.name}.")
        other = ClinicPhoneNumber.objects.filter(clinic=root).exclude(number=digits).first()
        if other:
            raise CommandError(f"{root.name} already has +{other.number}. Remove it in AI Settings first.")

        app_id = settings.PLIVO_APP_ID
        routed = bool(app_id) and app_id in (info.get("application") or "")
        record, created = ClinicPhoneNumber.objects.update_or_create(
            number=digits,
            defaults={
                "clinic": root,
                "bought_here": existing.bought_here if existing else False,
                "city": info.get("city") or info.get("region") or "",
                "monthly_rental_rate": str(info.get("monthly_rental_rate") or ""),
                "app_id": app_id if routed else "",
                "status": ClinicPhoneNumber.Status.ACTIVE if routed else ClinicPhoneNumber.Status.PURCHASED,
                "last_error": "" if routed else "Not routed to the voice agent's SIP app yet.",
            },
        )
        config, _ = ClinicConfiguration.objects.get_or_create(clinic=root)
        config.ai_phone_number = f"+{digits}"
        config.save(update_fields=["ai_phone_number", "updated_at"])

        centres = ", ".join(root.group_clinics().values_list("name", flat=True))
        self.stdout.write(self.style.SUCCESS(
            f"{'Linked' if created else 'Updated'} +{digits} -> {root.name} (shared by: {centres}); status {record.status}."
        ))
        if not routed:
            self.stdout.write(self.style.WARNING(
                f"Plivo routes this number to {info.get('application') or 'nothing'}, not the voice agent "
                f"app {app_id}. Use 'Connect to AI receptionist' in AI Settings to route it."
            ))
