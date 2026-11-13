"""A hospital's call credits: the balance, the rate per minute, and every change to either.

All changes go through ``post_entry`` / ``set_balance``, which lock the account row, so two calls ending at the same
moment (or an admin adding credits during a call) can never lose an update, and the ledger always adds up to the balance.
"""
from __future__ import annotations

from django.db import transaction

from platform_admin.models import CreditAccount, CreditLedger

DEFAULT_RATE = 10
DEFAULT_LOW_BALANCE = 500

Entry = CreditLedger.EntryType


def account_for(clinic) -> CreditAccount:
    """The credit account of the hospital this clinic belongs to (created empty on first use)."""
    root = clinic.root
    account = CreditAccount.objects.filter(clinic=root).first()
    if account is None:
        account, _ = CreditAccount.objects.get_or_create(clinic=root)
    return account


def peek_account(clinic) -> CreditAccount | None:
    """The hospital's credit account if it has one (never creates it: reading a list must not write)."""
    return CreditAccount.objects.filter(clinic=clinic.root).first()


def rate_for(clinic) -> int:
    """Credits one minute of calls costs this hospital: its own override, else its plan's rate."""
    from subscriptions.models import ClinicEntitlement

    entitlement = ClinicEntitlement.objects.filter(clinic=clinic.root).select_related("plan").first()
    return entitlement.get_credits_per_minute() if entitlement else DEFAULT_RATE


def minutes_left(balance: int, rate: int) -> int:
    return max(0, balance // rate) if rate > 0 else 0


def _actor_fields(actor) -> dict:
    authenticated = getattr(actor, "is_authenticated", False)
    return {"actor": actor if authenticated else None, "actor_email": (getattr(actor, "email", "") or "") if authenticated else ""}


@transaction.atomic
def post_entry(account: CreditAccount, entry_type: str, amount: int, *, note: str = "", actor=None, call_log=None) -> CreditLedger:
    """Add one signed entry and move the balance by the same amount, atomically."""
    locked = CreditAccount.objects.select_for_update().get(pk=account.pk)
    locked.balance += amount
    locked.save(update_fields=["balance", "updated_at"])
    entry = CreditLedger.objects.create(
        account=locked, entry_type=entry_type, amount=amount, balance_after=locked.balance, note=note[:200],
        call_log=call_log, **_actor_fields(actor),
    )
    account.balance = locked.balance
    return entry


@transaction.atomic
def add_credits(account: CreditAccount, amount: int, *, note: str = "", actor=None) -> CreditLedger:
    if amount <= 0:
        raise ValueError("Add more than 0 credits.")
    return post_entry(account, Entry.GRANT, amount, note=note, actor=actor)


@transaction.atomic
def set_balance(account: CreditAccount, target: int, *, note: str = "", actor=None) -> CreditLedger | None:
    """Make the balance exactly ``target`` with one adjustment entry for the difference (None if already equal)."""
    locked = CreditAccount.objects.select_for_update().get(pk=account.pk)
    delta = target - locked.balance
    if delta == 0:
        account.balance = locked.balance
        return None
    entry = post_entry(locked, Entry.ADJUSTMENT, delta, note=note, actor=actor)
    account.balance = locked.balance
    return entry
