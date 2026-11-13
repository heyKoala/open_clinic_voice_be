"""The shapes the platform admin screens read (see docs/PLATFORM_API.md in the frontend repo)."""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, FloatField, Sum
from django.db.models.functions import Cast, Ceil
from django.utils import timezone

from accounts.models import Invitation, User
from ai_agent.models import EMPTY_CALL, CallLog
from clinics.models import Clinic
from clinics.views import _phone_number_payload
from platform_admin import credits
from subscriptions.models import ClinicEntitlement, SubscriptionPlan

ROLES = (User.Role.CLINIC_ADMIN, User.Role.DOCTOR, User.Role.RECEPTIONIST)
DEFAULT_TIMEZONE = "Asia/Kolkata"


def hospitals():
    """Every hospital (main clinic), by name."""
    return Clinic.objects.filter(parent__isnull=True).order_by("name")


def _entitlement(root):
    return ClinicEntitlement.objects.filter(clinic=root).select_related("plan").first()


def usage_since(clinics, since) -> dict:
    """Calls and whole minutes (each call rounded up) the hospital's patients used since ``since``.

    Counts the same calls billing will: no staff test calls and no empty calls.
    """
    calls = CallLog.objects.filter(clinic__in=clinics, occurred_at__gte=since, is_test=False).exclude(EMPTY_CALL)
    totals = calls.aggregate(
        calls=Count("id"),
        minutes=Sum(Ceil(Cast("duration_seconds", FloatField()) / 60.0)),
    )
    return {"calls": totals["calls"] or 0, "minutes": int(totals["minutes"] or 0)}


def seat_usage(root, entitlement=None) -> dict:
    """Per role: people using a seat (active, or holding an unused invite) and the limit the plan gives."""
    entitlement = entitlement or _entitlement(root)
    group = list(root.group_clinics())
    now = timezone.now()
    out = {}
    for role in ROLES:
        active = User.objects.filter(
            clinic__in=group, role=role, is_active=True, membership_status=User.MembershipStatus.ACTIVE
        ).count()
        invited = Invitation.objects.filter(
            clinic__in=group, role=role, used_at__isnull=True, revoked_at__isnull=True, expires_at__gte=now
        ).count()
        out[role] = {"used": active + invited, "limit": entitlement.get_limit(role) if entitlement else 0}
    return out


def summary(root: Clinic) -> dict:
    group = list(root.group_clinics())
    entitlement = _entitlement(root)
    account = credits.peek_account(root)
    month = usage_since(group, timezone.now() - timedelta(days=30))
    return {
        "id": root.id,
        "name": root.name,
        "logo_url": root.logo_url,
        "status": root.status,
        "plan": entitlement.plan.name if entitlement else SubscriptionPlan.PlanType.TRIAL,
        "balance": account.balance if account else 0,
        "credits_per_minute": credits.rate_for(root),
        "low_balance_threshold": account.low_balance_threshold if account else credits.DEFAULT_LOW_BALANCE,
        "centres": len(group),
        "users": User.objects.filter(clinic__in=group).count(),
        "calls_30d": month["calls"],
        "minutes_30d": month["minutes"],
        "created_at": root.created_at,
    }


def detail(root: Clinic) -> dict:
    configuration = getattr(root, "configuration", None)
    admin = (
        User.objects.filter(clinic=root, role=User.Role.CLINIC_ADMIN, is_active=True).order_by("id").first()
    )
    invited = None
    if admin is None:
        invited = Invitation.objects.filter(
            clinic=root, role=User.Role.CLINIC_ADMIN, used_at__isnull=True, revoked_at__isnull=True
        ).order_by("-id").first()
    start = getattr(configuration, "working_hours_start", None)
    end = getattr(configuration, "working_hours_end", None)
    return {
        **summary(root),
        "address": root.address,
        "phone": root.phone,
        "support_email": root.support_email,
        "website": root.website,
        "registration_number": root.registration_number,
        "tax_id": root.tax_id,
        "description": root.description,
        "facilities": root.facilities,
        "timezone": getattr(configuration, "timezone", "") or DEFAULT_TIMEZONE,
        "working_hours_start": _hhmm(start, "09:00"),
        "working_hours_end": _hhmm(end, "17:00"),
        "admin_email": admin.email if admin else (invited.email if invited else ""),
        "phone_number": _phone_number_payload(root)["phone_number"],
        "limits": seat_usage(root),
    }


def _hhmm(value, default: str) -> str:
    return value.strftime("%H:%M") if hasattr(value, "strftime") else (str(value)[:5] if value else default)


def ledger_entry(entry) -> dict:
    return {
        "id": entry.id,
        "created_at": entry.created_at,
        "entry_type": entry.entry_type,
        "amount": entry.amount,
        "balance_after": entry.balance_after,
        "note": entry.note,
        "actor": entry.actor_email or None,
        "call_id": entry.call_log_id,
    }


def audit_entry(entry) -> dict:
    return {
        "id": entry.id,
        "created_at": entry.created_at,
        "actor": entry.actor_email or "system",
        "action": entry.action,
        "target": entry.clinic_name,
        "details": entry.details,
    }
