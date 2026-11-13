"""The platform admin API (/api/v1/platform/). Contract: docs/PLATFORM_API.md in the frontend repo.

Every view is for HeyKoala's own staff login only (anyone else gets a 404) and works on a hospital named in the
URL, never on "the signed-in user's clinic". Every write is recorded in the audit log in the same transaction.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from accounts.services import get_or_create_clinic_entitlement
from clinics.models import Clinic, ClinicConfiguration
from platform_admin import credits, presenters
from platform_admin.audit import log_platform_action
from platform_admin.models import CreditAccount, CreditLedger, PlatformAuditLog, PlatformDefaults
from platform_admin.permissions import PlatformAPIView
from platform_admin.serializers import (
    CreditChangeSerializer, DefaultsSerializer, HospitalUpdateSerializer, PlanChangeSerializer,
)
from subscriptions.models import SubscriptionPlan

PROFILE_FIELDS = ("address", "phone", "support_email", "website", "registration_number", "tax_id", "description", "facilities")
CONFIGURATION_FIELDS = ("timezone", "working_hours_start", "working_hours_end")

STATUS_ACTION = {
    Clinic.Status.ACTIVE: "reactivate_hospital",
    Clinic.Status.SUSPENDED: "suspend_hospital",
    Clinic.Status.ARCHIVED: "archive_hospital",
}


class Pager(PageNumberPagination):
    page_size = 25
    page_size_query_param = None


def paged(queryset, request, present) -> Response:
    pager = Pager()
    page = pager.paginate_queryset(queryset, request)
    return Response({"count": pager.page.paginator.count, "results": [present(item) for item in page]})


def get_hospital(pk) -> Clinic:
    """A hospital is a main clinic; a centre's id is not a hospital id."""
    return get_object_or_404(Clinic.objects.filter(parent__isnull=True), pk=pk)


class OverviewView(PlatformAPIView):
    def get(self, request):
        roots = list(presenters.hospitals())
        since = timezone.now() - timedelta(days=30)
        month = presenters.usage_since(Clinic.objects.all(), since)
        used = CreditLedger.objects.filter(entry_type=CreditLedger.EntryType.USAGE, created_at__gte=since).aggregate(total=Sum("amount"))["total"] or 0

        low_balance = []
        for root in roots:
            if root.status == Clinic.Status.ARCHIVED:
                continue
            row = presenters.summary(root)
            if row["balance"] <= row["low_balance_threshold"]:
                low_balance.append({k: row[k] for k in ("id", "name", "balance", "credits_per_minute")})
        low_balance.sort(key=lambda row: row["balance"])

        return Response({
            "hospitals": len(roots),
            "active": sum(1 for r in roots if r.status == Clinic.Status.ACTIVE),
            "suspended": sum(1 for r in roots if r.status == Clinic.Status.SUSPENDED),
            "calls_30d": month["calls"],
            "minutes_30d": month["minutes"],
            "credits_used_30d": abs(used),
            "low_balance": low_balance,
        })


class HospitalListView(PlatformAPIView):
    def get(self, request):
        return Response([presenters.summary(root) for root in presenters.hospitals()])


class HospitalDetailView(PlatformAPIView):
    def get(self, request, pk):
        return Response(presenters.detail(get_hospital(pk)))

    def patch(self, request, pk):
        root = get_hospital(pk)
        serializer = HospitalUpdateSerializer(instance=root, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            previous_status = root.status
            changed = []
            for field in ("name", "status", *PROFILE_FIELDS):
                if field in data and data[field] != getattr(root, field):
                    setattr(root, field, data[field])
                    changed.append(field)
            if changed:
                root.save()

            configuration = ClinicConfiguration.objects.get_or_create(clinic=root)[0]
            config_changed = [f for f in CONFIGURATION_FIELDS if f in data and data[f] != getattr(configuration, f)]
            for field in config_changed:
                setattr(configuration, field, data[field])
            if config_changed:
                configuration.save()

            if "status" in changed:
                log_platform_action(request.user, STATUS_ACTION[root.status], root, {"from": previous_status, "to": root.status})
            details = [f for f in changed + config_changed if f != "status"]
            if details:
                log_platform_action(request.user, "update_details", root, {"fields": details})
        return Response(presenters.detail(root))


class HospitalCreditsView(PlatformAPIView):
    def post(self, request, pk):
        root = get_hospital(pk)
        serializer = CreditChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mode, amount, note = serializer.validated_data["mode"], serializer.validated_data["amount"], serializer.validated_data["note"]

        with transaction.atomic():
            account = credits.account_for(root)
            before = account.balance
            if mode == "add":
                credits.add_credits(account, amount, note=note, actor=request.user)
            else:
                credits.set_balance(account, amount, note=note, actor=request.user)
            log_platform_action(request.user, "add_credits" if mode == "add" else "set_credits", root,
                                {"mode": mode, "amount": amount, "balance_before": before, "balance_after": account.balance, "note": note})
        return Response({"balance": account.balance})


class HospitalLedgerView(PlatformAPIView):
    def get(self, request, pk):
        root = get_hospital(pk)
        entries = CreditLedger.objects.filter(account__clinic=root).select_related("account")
        return paged(entries, request, presenters.ledger_entry)


class HospitalPlanView(PlatformAPIView):
    SEAT_FIELDS = {"max_admins": "max_admins_override", "max_doctors": "max_doctors_override", "max_receptionists": "max_receptionists_override"}

    def patch(self, request, pk):
        root = get_hospital(pk)
        serializer = PlanChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            entitlement = get_or_create_clinic_entitlement(root)
            if "plan" in data:
                entitlement.plan = SubscriptionPlan.get_default_plan(data["plan"])
            if "credits_per_minute" in data:
                entitlement.credits_per_minute_override = (
                    None if data["credits_per_minute"] == entitlement.plan.credits_per_minute else data["credits_per_minute"]
                )
            for field, override in self.SEAT_FIELDS.items():
                if field in data:
                    setattr(entitlement, override, data[field])
            entitlement.save()
            if "low_balance_threshold" in data:
                account = credits.account_for(root)
                account.low_balance_threshold = data["low_balance_threshold"]
                account.save(update_fields=["low_balance_threshold", "updated_at"])
            log_platform_action(request.user, "change_plan", root, dict(data))
        return Response(presenters.detail(root))


class DefaultsView(PlatformAPIView):
    def get(self, request):
        return Response(DefaultsSerializer(PlatformDefaults.load()).data)

    def put(self, request):
        serializer = DefaultsSerializer(PlatformDefaults.load(), data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            serializer.save()
            log_platform_action(request.user, "update_defaults", None, dict(serializer.validated_data))
        return Response(serializer.data)


class AuditLogView(PlatformAPIView):
    def get(self, request):
        entries = PlatformAuditLog.objects.all()
        hospital = request.query_params.get("hospital")
        if hospital:
            entries = entries.filter(clinic_id=hospital) if str(hospital).isdigit() else entries.none()
        return paged(entries, request, presenters.audit_entry)
