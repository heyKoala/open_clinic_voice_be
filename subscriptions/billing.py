"""Monthly usage billing: a flat platform fee that bundles voice minutes, then pay-as-you-go over that.

    bill = platform fee + (voice minutes beyond the bundle x per-minute rate) + (WhatsApp messages x per-message rate)

All amounts are INR, excluding GST. Usage is read from what was actually recorded (call logs and message
logs) for the current calendar month, so the bundle starts over on the 1st of every month with nothing to
reset or store.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from django.db.models import Sum
from django.utils import timezone

from ai_agent.models import EMPTY_CALL, CallLog
from patients.models import MessageLog

MONTHLY_PLATFORM_FEE_INR = 25_000
BUNDLED_VOICE_MINUTES = 500
EXTRA_VOICE_RATE_INR_PER_MIN = 10
WHATSAPP_RATE_INR_PER_MESSAGE = 2

# A message that was handed to WhatsApp. Pending ones haven't gone out yet and failed ones never did.
BILLABLE_MESSAGE_STATUSES = (MessageLog.Status.SENT, MessageLog.Status.DELIVERED, MessageLog.Status.READ)


def billing_period(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Start (inclusive) and end (exclusive) of the calendar month containing ``now``, in local time."""
    now = timezone.localtime(now or timezone.now())
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1))
    return start, end


def compute_bill(voice_seconds: int, whatsapp_messages: int) -> dict:
    """The month's bill for the given usage. Voice is billed in whole minutes, rounded up over the month."""
    total_minutes = math.ceil(voice_seconds / 60) if voice_seconds > 0 else 0
    bundled_used = min(total_minutes, BUNDLED_VOICE_MINUTES)
    extra_minutes = total_minutes - bundled_used
    extra_voice_charge = extra_minutes * EXTRA_VOICE_RATE_INR_PER_MIN
    whatsapp_charge = whatsapp_messages * WHATSAPP_RATE_INR_PER_MESSAGE
    return {
        "voice": {
            "total_minutes": total_minutes,
            "bundled_minutes": BUNDLED_VOICE_MINUTES,
            "bundled_used": bundled_used,
            "bundled_remaining": BUNDLED_VOICE_MINUTES - bundled_used,
            "extra_minutes": extra_minutes,
            "extra_rate": EXTRA_VOICE_RATE_INR_PER_MIN,
            "extra_charge": extra_voice_charge,
        },
        "whatsapp": {
            "messages": whatsapp_messages,
            "rate": WHATSAPP_RATE_INR_PER_MESSAGE,
            "charge": whatsapp_charge,
        },
        "platform_fee": MONTHLY_PLATFORM_FEE_INR,
        "total": MONTHLY_PLATFORM_FEE_INR + extra_voice_charge + whatsapp_charge,
    }


def clinic_billing_summary(clinic, now: datetime | None = None) -> dict:
    """This month's usage and bill for the clinic and the other centres in its group (as the call logs show)."""
    start, end = billing_period(now)
    clinics = clinic.root.group_clinics()

    # Same calls the call logs and analytics count: no staff test calls, no empty calls.
    voice_seconds = (
        CallLog.objects.filter(clinic__in=clinics, occurred_at__gte=start, occurred_at__lt=end, is_test=False)
        .exclude(EMPTY_CALL)
        .aggregate(total=Sum("duration_seconds"))["total"]
        or 0
    )
    whatsapp_messages = MessageLog.objects.filter(
        clinic__in=clinics,
        method=MessageLog.Method.WHATSAPP,
        status__in=BILLABLE_MESSAGE_STATUSES,
        created_at__gte=start,
        created_at__lt=end,
    ).count()

    return {
        "period_start": start.date().isoformat(),
        "period_end": (end - timedelta(days=1)).date().isoformat(),
        "currency": "INR",
        "gst_included": False,
        **compute_bill(voice_seconds, whatsapp_messages),
    }
