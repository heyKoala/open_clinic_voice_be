"""Create a HeyKoala platform admin: a staff login that belongs to no hospital and signs in to /platform.

    python manage.py create_platform_admin admin@heykoala.in --name "HeyKoala Admin"
    python manage.py create_platform_admin admin@heykoala.in --reset-password

The password is typed at the prompt (asked twice), or read from the environment variable named by ``--password-env``
(default ``PLATFORM_ADMIN_PASSWORD``) when there is no terminal. It is never taken on the command line, where it would
be kept in the shell history. The same strength rules as the app apply.

It refuses an email that already belongs to a hospital's user: a hospital login is never quietly turned into a
platform login. Safe to run again: an existing platform admin is left alone unless ``--reset-password`` is given.
"""
from __future__ import annotations

import getpass
import os
import sys

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User
from accounts.serializers import _enforce_strong_password
from platform_admin.audit import log_platform_action


class Command(BaseCommand):
    help = "Create (or reset the password of) a HeyKoala platform admin login that belongs to no hospital."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--name", default="HeyKoala Admin", help="Full name shown in the audit log.")
        parser.add_argument("--reset-password", action="store_true", help="Set a new password for an existing platform admin.")
        parser.add_argument("--password-env", default="PLATFORM_ADMIN_PASSWORD", help="Environment variable to read the password from when there is no terminal.")

    def handle(self, *args, **options):
        email = User.objects.normalize_email(options["email"]).lower().strip()
        if "@" not in email:
            raise CommandError("Give a valid email address.")

        existing = User.objects.filter(email__iexact=email).first()
        if existing is not None:
            if existing.clinic_id is not None or not existing.is_staff:
                raise CommandError(
                    f"{email} already belongs to a hospital's user. Use a different email for the platform login; "
                    "a hospital login is never turned into a platform login."
                )
            if not options["reset_password"]:
                self.stdout.write(f"{email} is already a platform admin. Nothing changed (use --reset-password to set a new password).")
                return

        password = self._password(options["password_env"])

        with transaction.atomic():
            if existing is None:
                user = User.objects.create_user(
                    email=email,
                    password=password,
                    full_name=options["name"],
                    role=User.Role.CLINIC_ADMIN,   # a required field; a login with no hospital reaches no hospital's pages
                    clinic=None,
                    is_staff=True,
                    is_verified=True,
                    is_active=True,
                    membership_status=User.MembershipStatus.ACTIVE,
                )
                log_platform_action(None, "create_platform_admin", details={"email": email, "by": "manage.py"})
                self.stdout.write(self.style.SUCCESS(f"Platform admin {user.email} created. Sign in at /login; you will land on /platform."))
            else:
                existing.set_password(password)
                existing.is_active = True
                existing.save(update_fields=["password", "is_active"])
                log_platform_action(None, "reset_platform_admin_password", details={"email": email, "by": "manage.py"})
                self.stdout.write(self.style.SUCCESS(f"New password set for {email}."))

    def _password(self, env_name: str) -> str:
        if sys.stdin.isatty():
            first = getpass.getpass("Password: ")
            if first != getpass.getpass("Password (again): "):
                raise CommandError("The two passwords do not match.")
            password = first
        else:
            password = os.environ.get(env_name, "")
            if not password:
                raise CommandError(f"No terminal to type a password: set the {env_name} environment variable.")
        try:
            _enforce_strong_password(password)
        except Exception as error:  # Django or DRF validation error
            messages = getattr(error, "messages", None) or getattr(error, "detail", None) or [str(error)]
            raise CommandError("Password is too weak: " + " ".join(str(m) for m in (messages if isinstance(messages, list) else [messages])))
        return password
