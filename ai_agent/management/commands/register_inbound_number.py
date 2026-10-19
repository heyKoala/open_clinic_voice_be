"""Register (or re-register) the hospital's phone number on the voice worker, so calls to it reach this backend.

    python manage.py register_inbound_number +918031150501
    python manage.py register_inbound_number +918031150501 --remove-old   # also delete the ids currently in .env

Creates the worker's inbound SIP trunk and dispatch rule pointing at this backend's public address (PUBLIC_BASE_URL),
with the call audio settings from the Agent settings page. Prints the new ids: put them in .env as
ROCK8_INBOUND_TRUNK_ID / ROCK8_INBOUND_RULE_ID and restart the backend. They are also saved to backups/.
"""
import json
import os

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ai_agent.agent_settings import call_flags
from ai_agent.tool_views import rock8_path
from clinics.models import Clinic


class Command(BaseCommand):
    help = "Register the hospital number on the voice worker (inbound trunk + dispatch rule)."

    def add_arguments(self, parser):
        parser.add_argument("number", help="The hospital number with country code, e.g. +918031150501")
        parser.add_argument("--clinic", type=int, help="Main hospital id (default: the only/first main clinic)")
        parser.add_argument("--remove-old", action="store_true",
                            help="First delete the trunk/rule named by ROCK8_INBOUND_TRUNK_ID / ROCK8_INBOUND_RULE_ID")

    def handle(self, *args, **options):
        if not settings.ROCK8_API_KEY:
            raise CommandError("ROCK8_API_KEY is not set.")
        if not settings.PUBLIC_BASE_URL or "localhost" in settings.PUBLIC_BASE_URL:
            raise CommandError("PUBLIC_BASE_URL must be this backend's public address (tunnel or server).")
        number = options["number"].strip()
        if not number.startswith("+"):
            raise CommandError("Give the number with a leading '+', e.g. +918031150501.")
        clinics = Clinic.objects.filter(parent__isnull=True).order_by("id")
        clinic = clinics.filter(id=options["clinic"]).first() if options["clinic"] else clinics.first()
        if clinic is None:
            raise CommandError("No main hospital found.")

        base = settings.VOICE_WORKER_URL.rstrip("/")
        headers = {"accept": "application/json", "X-API-Key": settings.ROCK8_API_KEY}
        if options["remove_old"] and settings.ROCK8_INBOUND_TRUNK_ID and settings.ROCK8_INBOUND_RULE_ID:
            res = requests.delete(f"{base}/inbound/{settings.ROCK8_INBOUND_TRUNK_ID}/{settings.ROCK8_INBOUND_RULE_ID}",
                                  headers=headers, timeout=60)
            self.stdout.write(f"Removed old registration: HTTP {res.status_code} {res.text[:200]}")

        webhook = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/v1/ai/webhooks/rock8/{rock8_path(clinic.id, '')}".rstrip("/") + "/"
        body = {"phone_number": number, "webhook_url": webhook, "provider": "plivo", **call_flags(clinic)}
        res = requests.post(f"{base}/inbound/configure", json=body, headers=headers, timeout=60)
        if res.status_code not in (200, 201):
            raise CommandError(f"The voice worker refused: HTTP {res.status_code} {res.text[:400]}")
        data = res.json()
        trunk, rule = data.get("sip_trunk_id"), data.get("dispatch_rule_id")
        record = {"phone_number": number, "webhook_url": webhook, "sip_trunk_id": trunk, "dispatch_rule_id": rule,
                  **{k: v for k, v in body.items() if k.startswith(("noise", "recording"))}}
        os.makedirs("backups", exist_ok=True)
        path = os.path.join("backups", f"voice_inbound_{number.lstrip('+')}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=1)
        self.stdout.write(self.style.SUCCESS(f"Registered {number} for {clinic.name} (noise cancellation "
                                             f"{'on' if body['noise_cancellation'] else 'off'})."))
        self.stdout.write(f"Saved to {path}. Now put these in .env and restart the backend:\n"
                          f"ROCK8_INBOUND_TRUNK_ID={trunk}\nROCK8_INBOUND_RULE_ID={rule}")
