"""Charging a hospital's credits for its calls.

When a call is over (its length is known), the hospital is charged ``whole minutes (each call rounded up) x rate``, once.
A call's length can reach us several ways (the provider's end-of-call event, or the transcript fetched a couple of
minutes later) and a later report can correct an earlier one, so:

* ``charge_call`` is safe to call as often as you like. The first call posts one ``usage`` entry; a later, different
  length posts an ``adjustment`` for the difference (at the rate the call was first charged at).
* It is called when a call's length is stored, and ``settle_unbilled`` (a scheduled task) catches anything that slipped
  past, so a finished call can never go uncharged for long.
* Only calls that started after the hospital's ``charging_from`` are charged: older calls are never billed retroactively,
  and a hospital with no credit account (e.g. a demo hospital) is never charged.
* Staff test calls and calls with no length are never charged. The balance may go negative: calls are still answered.
* A billing problem is logged and never allowed to disturb the call's own record.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from ai_agent.models import CallLog
from platform_admin import credits
from platform_admin.models import CreditAccount, CreditLedger

logger = logging.getLogger(__name__)

Entry = CreditLedger.EntryType

# A call whose record changed this recently may still be getting its final length: the sweep leaves it a moment.
QUIET = timedelta(minutes=3)
RECHECK_WINDOW = timedelta(hours=24)


def whole_minutes(seconds: int) -> int:
    return math.ceil(seconds / 60) if seconds and seconds > 0 else 0


def charging_account(call_log: CallLog) -> CreditAccount | None:
    """The account this call is charged to, or None when it must not be charged at all."""
    account = CreditAccount.objects.filter(clinic=call_log.clinic.root).first()
    if account is None or account.charging_from is None or call_log.occurred_at < account.charging_from:
        return None
    return account


def charge_call(call_log: CallLog) -> CreditLedger | None:
    """Charge (or correct the charge for) one finished call. Returns the entry posted, or None if nothing was needed."""
    if call_log.is_test or call_log.duration_seconds <= 0:
        return None
    account = charging_account(call_log)
    if account is None:
        return None
    minutes = whole_minutes(call_log.duration_seconds)

    try:
        with transaction.atomic():
            # One call at a time per hospital, so two workers can't both decide the same call still needs charging.
            CreditAccount.objects.select_for_update().get(pk=account.pk)
            entries = list(CreditLedger.objects.filter(call_log=call_log))
            usage = next((e for e in entries if e.entry_type == Entry.USAGE), None)

            if usage is None:
                rate = credits.rate_for(call_log.clinic)
                return credits.post_entry(
                    account, Entry.USAGE, -minutes * rate, note=f"{minutes} min x {rate} credits", call_log=call_log, minutes=minutes, rate=rate,
                )

            billed = sum(e.minutes or 0 for e in entries if e.entry_type in (Entry.USAGE, Entry.ADJUSTMENT))
            if minutes == billed:
                return None
            change = minutes - billed
            return credits.post_entry(
                account, Entry.ADJUSTMENT, -change * (usage.rate or credits.DEFAULT_RATE),
                note=f"Call length updated: {billed} to {minutes} min", call_log=call_log, minutes=change,
            )
    except IntegrityError:
        return None  # another worker charged this very call a moment ago


def charge_call_safely(call_log_id: int) -> CreditLedger | None:
    """Charge a call by id from inside call handling: whatever goes wrong here is logged, never raised."""
    try:
        call_log = CallLog.objects.select_related("clinic").filter(pk=call_log_id).first()
        return charge_call(call_log) if call_log is not None else None
    except Exception:
        logger.exception("Could not charge credits for call %s", call_log_id)
        return None


def settle_unbilled(now=None, limit: int = 300) -> int:
    """Charge finished calls nothing charged yet, and re-check recent ones whose length may have changed. Returns entries posted."""
    now = now or timezone.now()
    posted = 0
    for account in CreditAccount.objects.exclude(charging_from=None).select_related("clinic"):
        calls = CallLog.objects.filter(
            clinic__in=list(account.clinic.group_clinics()), occurred_at__gte=account.charging_from, is_test=False, duration_seconds__gt=0,
        ).select_related("clinic")

        never_charged = calls.filter(updated_at__lte=now - QUIET).exclude(credit_entries__entry_type=Entry.USAGE).order_by("id")[:limit]
        recently_charged = calls.filter(
            credit_entries__entry_type=Entry.USAGE, credit_entries__created_at__gte=now - RECHECK_WINDOW
        ).distinct().order_by("id")[:limit]

        for call in [*never_charged, *recently_charged]:
            try:
                if charge_call(call) is not None:
                    posted += 1
            except Exception:
                logger.exception("Could not charge credits for call %s", call.id)
    return posted
