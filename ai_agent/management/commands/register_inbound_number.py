"""Register (or re-register) the hospital's phone number on the voice worker, so calls to it reach this backend.

    python manage.py register_inbound_number +918031150501
    python manage.py register_inbound_number +918031150501 --remove-old   # also delete the ids currently configured
    python manage.py register_inbound_number +918031150501 --remove-trunk ST_xxx [--remove-rule SDR_xxx]
                                                                          # delete a specific earlier registration

Creates the worker's inbound SIP trunk and dispatch rule pointing at this backend's public address (PUBLIC_BASE_URL),
with the call audio settings from the Agent settings page. Prints the new ids: put them in the configuration as
ROCK8_INBOUND_TRUNK_ID / ROCK8_INBOUND_RULE_ID and restart the backend. The ids are printed before anything else can
fail; saving a copy to backups/ is best effort (that folder may be read-only in a container).

The worker allows one registration per number and has no "list" endpoint. If it refuses with "Conflicting inbound
SIP Trunks", the error names the trunk that holds the number: re-run with --remove-trunk <that ST_ id>. Its dispatch
rule id is optional; without it the worker still deletes the trunk (the rule is left unattached and does nothing).
"""
import json
import os
import re

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ai_agent.agent_settings import call_flags
from ai_agent.tool_views import rock8_path
from clinics.models import Clinic

TRUNK_ID = re.compile(r"ST_[A-Za-z0-9]+")


class Command(BaseCommand):
    help = "Register the hospital number on the voice worker (inbound trunk + dispatch rule)."

    def add_arguments(self, parser):
        parser.add_argument("number", help="The hospital number with country code, e.g. +918031150501")
        parser.add_argument("--clinic", type=int, help="Main hospital id (default: the only/first main clinic)")
        parser.add_argument("--remove-old", action="store_true",
                            help="First delete the trunk/rule named by ROCK8_INBOUND_TRUNK_ID / ROCK8_INBOUND_RULE_ID")
        parser.add_argument("--remove-trunk", help="First delete this inbound trunk (ST_...), e.g. one a failed run left")
        parser.add_argument("--remove-rule", help="The dispatch rule (SDR_...) of --remove-trunk, if known")

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
        old = []
        if options["remove_old"] and settings.ROCK8_INBOUND_TRUNK_ID and settings.ROCK8_INBOUND_RULE_ID:
            old.append((settings.ROCK8_INBOUND_TRUNK_ID, settings.ROCK8_INBOUND_RULE_ID))
        if options["remove_trunk"]:
            old.append((options["remove_trunk"].strip(), (options["remove_rule"] or "").strip() or "SDR_unknown"))
        for trunk_id, rule_id in old:
            res = requests.delete(f"{base}/inbound/{trunk_id}/{rule_id}", headers=headers, timeout=60)
            self.stdout.write(f"Removed registration {trunk_id} / {rule_id}: HTTP {res.status_code} {res.text[:200]}")

        webhook = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/v1/ai/webhooks/rock8/{rock8_path(clinic.id, '')}".rstrip("/") + "/"
        body = {"phone_number": number, "webhook_url": webhook, "provider": "plivo", **call_flags(clinic)}
        res = requests.post(f"{base}/inbound/configure", json=body, headers=headers, timeout=60)
        if res.status_code not in (200, 201):
            hint = ""
            holders = TRUNK_ID.findall(res.text)
            if "onflicting" in res.text and holders:
                hint = (f"\nThe number is already registered to trunk {holders[-1]}. If that is not this server's "
                        f"registration, re-run with --remove-trunk {holders[-1]}.")
            raise CommandError(f"The voice worker refused: HTTP {res.status_code} {res.text[:400]}{hint}")

        data = res.json()
        trunk, rule = data.get("sip_trunk_id"), data.get("dispatch_rule_id")
        # Print first, so nothing that fails afterwards can lose the ids.
        self.stdout.write(self.style.SUCCESS(
            f"Registered {number} for {clinic.name} -> {settings.PUBLIC_BASE_URL.rstrip('/')} "
            f"(noise cancellation {'on' if body['noise_cancellation'] else 'off'})."))
        self.stdout.write("Put these in the configuration (AWS secret or .env) and restart the backend:\n"
                          f"ROCK8_INBOUND_TRUNK_ID={trunk}\nROCK8_INBOUND_RULE_ID={rule}")

        record = {"phone_number": number, "webhook_url": webhook, "sip_trunk_id": trunk, "dispatch_rule_id": rule,
                  **{k: v for k, v in body.items() if k.startswith(("noise", "recording"))}}
        try:
            os.makedirs("backups", exist_ok=True)
            path = os.path.join("backups", f"voice_inbound_{number.lstrip('+')}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=1)
            self.stdout.write(f"A copy is saved in {path}.")
        except OSError as exc:
            self.stdout.write(f"(Could not save a copy to backups/: {exc}. Keep the two lines above.)")
