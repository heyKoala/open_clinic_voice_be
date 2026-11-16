"""Telling people when a hospital's call credits are running low or have run out.

A hospital is in one of three states: ``ok``, ``low`` (at or under its warning level) or ``empty`` (zero or less). An alert
email goes out when a call's charge moves it DOWN a state, never on every call, and the state resets when credits are added,
so one slow drain produces two emails (low, then empty), not hundreds. Adding or setting credits by hand, or changing the
warning level, only updates the state quietly: the person doing it already knows.

Emails are sent after the charge is saved (``on_commit``) and a mail problem is logged, never raised: a billing event must
not fail because an email did. Calls are still answered at zero credits; the alert is how the platform finds out.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from clinics.models import Clinic
from platform_admin import credits
from platform_admin.models import CreditAccount

logger = logging.getLogger(__name__)

OK, LOW, EMPTY = "ok", "low", "empty"
RANK = {OK: 0, LOW: 1, EMPTY: 2}


def state_for(balance: int, threshold: int) -> str:
    if balance <= 0:
        return EMPTY
    return LOW if balance <= threshold else OK


def sync(account: CreditAccount, notify: bool = False) -> str:
    """Bring ``account.alert_state`` in line with its balance (call inside the transaction that changed it).

    With ``notify``, a move to a worse state sends the alert once the transaction commits. Returns the new state."""
    new = state_for(account.balance, account.low_balance_threshold)
    old = account.alert_state
    if new == old:
        return new
    account.alert_state = new
    fields = ["alert_state", "updated_at"]
    if notify and RANK[new] > RANK[old] and account.clinic.status == Clinic.Status.ACTIVE:
        account.last_alert_at = timezone.now()
        fields.append("last_alert_at")
        account_id = account.pk
        transaction.on_commit(lambda: send_alert(account_id, new))
    account.save(update_fields=fields)
    return new


def platform_admin_emails() -> list[str]:
    return sorted({e.lower() for e in User.objects.filter(is_staff=True, is_active=True, is_verified=True).values_list("email", flat=True)})


def hospital_admin_emails(root: Clinic) -> list[str]:
    admins = User.objects.filter(
        clinic__in=root.group_clinics(), role=User.Role.CLINIC_ADMIN, is_active=True,
        membership_status=User.MembershipStatus.ACTIVE, is_verified=True,
    )
    return sorted({e.lower() for e in admins.values_list("email", flat=True)})


def _number(n: int) -> str:
    return f"{n:,}"


def send_alert(account_id: int, state: str) -> None:
    """Email the platform admins and the hospital's own admins. Never raises."""
    try:
        account = CreditAccount.objects.select_related("clinic").get(pk=account_id)
        root = account.clinic
        rate = credits.rate_for(root)
        minutes = credits.minutes_left(account.balance, rate)
        platform_to = platform_admin_emails()
        # Someone who is both (HeyKoala's team attached to a hospital) gets the platform email only.
        hospital_to = [e for e in hospital_admin_emails(root) if e not in platform_to]

        if state == EMPTY:
            headline = f"{root.name} has run out of AI call credits"
            detail = f"Its balance is {_number(account.balance)} credits."
            consequence = "Calls are still being answered, and each one takes the balance further below zero until credits are added."
        else:
            headline = f"{root.name} is running low on AI call credits"
            detail = f"{_number(account.balance)} credits are left (about {_number(minutes)} minutes of calls at {rate} credits per minute)."
            consequence = "Calls are still being answered. Add credits before they run out."

        if platform_to:
            link = f"{settings.FRONTEND_URL.rstrip('/')}/platform/hospitals/{root.id}"
            send_mail(
                subject=headline,
                message=f"{headline}.\n\n{detail}\n{consequence}\n\nAdd credits here: {link}\n",
                from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=platform_to, fail_silently=False,
            )
        if hospital_to:
            send_mail(
                subject=headline,
                message=f"{headline}.\n\n{detail}\n{consequence}\n\nPlease contact HeyKoala to add credits.\n",
                from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=hospital_to, fail_silently=False,
            )
    except Exception:
        logger.exception("Could not send the %s credits alert for account %s", state, account_id)
