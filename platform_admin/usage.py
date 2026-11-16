"""One hospital's usage over a period: calls, minutes, credits, records and recording storage (read-only)."""
from __future__ import annotations

from datetime import timedelta

from django.core.files.storage import default_storage
from django.db.models import Count, FloatField, Sum
from django.db.models.functions import Cast, Ceil, TruncDate
from django.utils import timezone

from accounts.models import User
from ai_agent.models import EMPTY_CALL, CallLog
from appointments.models import Appointment
from clinics.models import Clinic
from doctors.models import Doctor
from patients.models import Patient
from platform_admin import credits
from platform_admin.models import CreditLedger

PERIODS = (7, 30, 90)
# Measuring recording storage means looking at each file; past this many the figure is marked as a lower bound.
STORAGE_FILE_LIMIT = 5000

Entry = CreditLedger.EntryType
_MINUTES = Sum(Ceil(Cast("duration_seconds", FloatField()) / 60.0))


def _whole(value) -> int:
    return int(value or 0)


def _recording_storage(clinics) -> dict:
    names = list(
        CallLog.objects.filter(clinic__in=clinics).exclude(recording_file="").values_list("recording_file", flat=True)[: STORAGE_FILE_LIMIT + 1]
    )
    capped = len(names) > STORAGE_FILE_LIMIT
    total = files = 0
    for name in names[:STORAGE_FILE_LIMIT]:
        try:
            total += default_storage.size(name)
            files += 1
        except (OSError, ValueError):
            continue  # a file that is gone is not counted
    return {"files": files, "megabytes": round(total / (1024 * 1024), 1), "at_least": capped}


def hospital_usage(root: Clinic, days: int = 30) -> dict:
    """Usage for the last ``days`` days (today included), counted the way billing counts: no staff test calls, no empty calls."""
    group = list(root.group_clinics())
    today = timezone.localtime().date()
    first_day = today - timedelta(days=days - 1)
    start = timezone.make_aware(timezone.datetime.combine(first_day, timezone.datetime.min.time()))

    calls = CallLog.objects.filter(clinic__in=group, occurred_at__gte=start, is_test=False).exclude(EMPTY_CALL)
    totals = calls.aggregate(calls=Count("id"), minutes=_MINUTES, seconds=Sum("duration_seconds"))
    by_direction = {row["direction"]: row for row in calls.values("direction").annotate(calls=Count("id"), minutes=_MINUTES)}
    per_day = {row["day"]: row for row in calls.annotate(day=TruncDate("occurred_at")).values("day").annotate(calls=Count("id"), minutes=_MINUTES)}

    ledger = CreditLedger.objects.filter(account__clinic=root, created_at__gte=start)
    call_entries = ledger.filter(entry_type__in=[Entry.USAGE, Entry.ADJUSTMENT], call_log__isnull=False)
    credits_by_day = {
        row["day"]: row["total"]
        for row in call_entries.annotate(day=TruncDate("created_at")).values("day").annotate(total=Sum("amount"))
    }
    used = -_whole(call_entries.aggregate(total=Sum("amount"))["total"])
    added = _whole(ledger.filter(entry_type=Entry.GRANT).aggregate(total=Sum("amount"))["total"])

    daily = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        row = per_day.get(day, {})
        daily.append({"date": day.isoformat(), "calls": row.get("calls", 0), "minutes": _whole(row.get("minutes")), "credits": -_whole(credits_by_day.get(day))})

    account = credits.peek_account(root)
    seconds = _whole(totals["seconds"])
    total_calls = totals["calls"] or 0
    return {
        "period": {"days": days, "from": first_day.isoformat(), "to": today.isoformat()},
        "calls": {
            "total": total_calls,
            "inbound": (by_direction.get(CallLog.Direction.INBOUND) or {}).get("calls", 0),
            "outbound": (by_direction.get(CallLog.Direction.OUTBOUND) or {}).get("calls", 0),
            "average_seconds": round(seconds / total_calls) if total_calls else 0,
            # Calls counted above that no credits were taken for (made before charging started, or not charged yet).
            "not_charged": calls.exclude(credit_entries__entry_type=Entry.USAGE).count(),
        },
        "minutes": {
            "total": _whole(totals["minutes"]),
            "inbound": _whole((by_direction.get(CallLog.Direction.INBOUND) or {}).get("minutes")),
            "outbound": _whole((by_direction.get(CallLog.Direction.OUTBOUND) or {}).get("minutes")),
        },
        "credits": {"used": used, "added": added, "balance": account.balance if account else 0},
        "daily": daily,
        "records": {
            "centres": len(group),
            "users": User.objects.filter(clinic__in=group, is_active=True).count(),
            "doctors": Doctor.objects.filter(clinic__in=group, is_active=True).count(),
            "patients": Patient.objects.filter(clinic__in=group).count(),
            "appointments": Appointment.objects.filter(clinic__in=group).count(),
        },
        "storage": _recording_storage(group),
    }
