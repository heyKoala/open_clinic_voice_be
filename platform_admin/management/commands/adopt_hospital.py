"""Bring an existing hospital (made before the platform admin existed) into the platform: an active plan, credit account, limits.

    python manage.py adopt_hospital --clinic-id 5 --dry-run     # show what would change
    python manage.py adopt_hospital --clinic-id 5               # do it

Safe to run more than once. It only ever adds or updates settings rows for that one hospital: it never deletes, never
touches patients, appointments or call logs, and starts the credit balance at ``--opening`` (default 0).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.services import get_or_create_clinic_entitlement
from clinics.models import Clinic
from platform_admin import credits, presenters
from platform_admin.audit import log_platform_action
from platform_admin.models import PlatformDefaults
from subscriptions.models import SubscriptionPlan


class Command(BaseCommand):
    help = "Adopt an existing hospital into the platform admin (active plan, credit account, seat limits)."

    def add_arguments(self, parser):
        defaults = PlatformDefaults()
        parser.add_argument("--clinic-id", type=int, required=True, help="The hospital's (main clinic's) id.")
        parser.add_argument("--plan", default="growth", choices=[p for p, _ in SubscriptionPlan.PlanType.choices])
        parser.add_argument("--rate", type=int, default=defaults.credits_per_minute, help="Credits per minute.")
        parser.add_argument("--opening", type=int, default=0, help="Opening credits (0 if you will add them from the admin page).")
        parser.add_argument("--low-balance", type=int, default=defaults.low_balance_threshold)
        parser.add_argument("--max-admins", type=int, default=None, help="Default: the platform default, but never below how many it has.")
        parser.add_argument("--max-doctors", type=int, default=None)
        parser.add_argument("--max-receptionists", type=int, default=None)
        parser.add_argument("--dry-run", action="store_true", help="Show what would change and change nothing.")

    def handle(self, *args, **options):
        root = Clinic.objects.filter(pk=options["clinic_id"], parent__isnull=True).first()
        if root is None:
            raise CommandError(f"Clinic {options['clinic_id']} is not a hospital (a main clinic).")

        platform_defaults = PlatformDefaults.load()
        usage = presenters.seat_usage(root)
        wanted = {
            "max_admins": options["max_admins"],
            "max_doctors": options["max_doctors"],
            "max_receptionists": options["max_receptionists"],
        }
        fallback = {"max_admins": platform_defaults.max_admins, "max_doctors": platform_defaults.max_doctors, "max_receptionists": platform_defaults.max_receptionists}
        roles = {"max_admins": "clinic_admin", "max_doctors": "doctor", "max_receptionists": "receptionist"}
        # Never set a limit below the people it already has: that would lock the hospital out of inviting anyone.
        limits = {
            key: max(wanted[key] if wanted[key] is not None else fallback[key], usage[roles[key]]["used"])
            for key in wanted
        }

        entitlement = get_or_create_clinic_entitlement(root)
        account = credits.peek_account(root)
        self.stdout.write(f"Hospital: {root.name} (id {root.id})")
        self.stdout.write(f"  status            : {root.status}  | billing status: {root.subscription_status} -> active")
        self.stdout.write(f"  plan              : {entitlement.plan.name} -> {options['plan']}")
        self.stdout.write(f"  credits per minute: {credits.rate_for(root)} -> {options['rate']}")
        self.stdout.write(f"  seat limits       : {limits}  (in use: { {r: usage[r]['used'] for r in usage} })")
        self.stdout.write(f"  credit account    : {'exists, balance ' + str(account.balance) if account else 'will be created'}"
                          f"{', opening credits ' + str(options['opening']) if options['opening'] and not account else ''}")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run: nothing was changed."))
            return

        with transaction.atomic():
            root.subscription_status = Clinic.SubscriptionStatus.ACTIVE
            root.save(update_fields=["subscription_status", "updated_at"])

            plan = SubscriptionPlan.get_default_plan(options["plan"])
            entitlement.plan = plan
            entitlement.credits_per_minute_override = None if options["rate"] == plan.credits_per_minute else options["rate"]
            entitlement.max_admins_override = limits["max_admins"]
            entitlement.max_doctors_override = limits["max_doctors"]
            entitlement.max_receptionists_override = limits["max_receptionists"]
            entitlement.save()

            created = account is None
            account = credits.account_for(root)
            account.low_balance_threshold = options["low_balance"]
            account.save(update_fields=["low_balance_threshold", "updated_at"])
            if created and options["opening"] > 0:
                credits.add_credits(account, options["opening"], note="Opening credits")
            log_platform_action(None, "adopt_hospital", root, {"plan": options["plan"], "rate": options["rate"], "limits": limits, "opening": options["opening"] if created else 0})
        self.stdout.write(self.style.SUCCESS("Done."))
