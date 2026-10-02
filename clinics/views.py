from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.db.models import ProtectedError
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from clinics import plivo
from clinics.models import Clinic, ClinicConfiguration, ClinicPhoneNumber, PaymentTransaction
from clinics.permissions import IsClinicAdmin
from clinics.serializers import ClinicSerializer, ClinicConfigurationSerializer, ClinicHolidaySerializer
from clinics.models import ClinicHoliday
from accounts.services import get_or_create_clinic_entitlement
from common.audit import log_data_change

logger = logging.getLogger(__name__)


class ClinicListCreateView(generics.ListCreateAPIView):
    serializer_class = ClinicSerializer
    permission_classes = [IsClinicAdmin]

    def get_queryset(self):
        # Admin can only see clinics they belong to
        return self.request.user.clinics.all().order_by("name")

    def perform_create(self, serializer):
        # A new centre belongs to the active clinic's main clinic and shares its phone number.
        clinic = serializer.save(parent=self.request.clinic.root if self.request.clinic else None)

        # Add the clinic to the user's allowed clinics
        self.request.user.clinics.add(clinic)

        # Create a default configuration and entitlement for the new clinic
        ClinicConfiguration.objects.get_or_create(clinic=clinic)
        get_or_create_clinic_entitlement(clinic)

        log_data_change(clinic, "create", request=self.request)
        return clinic

def _centre_delete_blockers(clinic) -> list[str]:
    """What a centre still holds that deleting it must never wipe (e.g. ["3 patients"])."""
    from accounts.models import User
    from ai_agent.models import CallLog
    from patients.models import Patient

    counts = [
        # Patients carry the appointments, queue tokens, follow-ups and clinical records.
        (Patient.objects.filter(clinic=clinic).count(), "patient", "patients"),
        # Staff whose account belongs to this centre would be deleted along with it.
        (User.objects.filter(clinic=clinic).count(), "staff account", "staff accounts"),
        (CallLog.objects.filter(clinic=clinic).count(), "call log", "call logs"),
        (PaymentTransaction.objects.filter(clinic=clinic, status=PaymentTransaction.Status.CAPTURED).count(),
         "payment", "payments"),
    ]
    return [f"{count} {one if count == 1 else many}" for count, one, many in counts if count]


class ClinicDetailView(generics.RetrieveUpdateDestroyAPIView):
    """GET/PATCH the active clinic; DELETE removes it if it is a centre that holds no records."""
    serializer_class = ClinicSerializer
    permission_classes = [IsClinicAdmin]

    def get_queryset(self):
        return Clinic.objects.filter(id=self.request.clinic.id)

    def destroy(self, request, *args, **kwargs):
        clinic = self.get_object()
        if clinic.parent_id is None:
            return Response({"detail": "The main clinic can't be deleted. Only its centres can."},
                            status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            # Lock the centre so nothing is added to it between the check and the delete.
            Clinic.objects.select_for_update().filter(id=clinic.id).first()
            blockers = _centre_delete_blockers(clinic)
            if blockers:
                return Response(
                    {"detail": f"{clinic.name} can't be deleted because it still has {', '.join(blockers)}."},
                    status=status.HTTP_409_CONFLICT,
                )
            log_data_change(clinic, "delete", request=request, metadata={"name": clinic.name})
            try:
                clinic.delete()
            except ProtectedError:
                transaction.set_rollback(True)
                return Response({"detail": f"{clinic.name} can't be deleted because other records still depend on it."},
                                status=status.HTTP_409_CONFLICT)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ClinicConfigurationView(generics.RetrieveUpdateAPIView):
    serializer_class = ClinicConfigurationSerializer
    permission_classes = [IsClinicAdmin]

    def get_queryset(self):
        return ClinicConfiguration.objects.filter(clinic_id=self.request.clinic.id)

    def get_object(self):
        # Get or create the configuration for the current user's clinic
        obj, created = ClinicConfiguration.objects.get_or_create(
            clinic_id=self.request.clinic.id
        )
        return obj

    def perform_create(self, serializer):
        # Set the clinic to the current user's clinic and log creation
        instance = serializer.save(clinic_id=self.request.clinic.id)
        log_data_change(instance, "create", request=self.request)
        return instance

    def perform_update(self, serializer):
        # Save and log the update
        instance = serializer.save()
        log_data_change(instance, "update", request=self.request)
        return instance

class ClinicHolidayListCreateView(generics.ListCreateAPIView):
    serializer_class = ClinicHolidaySerializer
    permission_classes = [IsClinicAdmin]

    def get_queryset(self):
        return ClinicHoliday.objects.filter(clinic_id=self.request.clinic.id)

    def perform_create(self, serializer):
        from appointments.models import Appointment
        from patients.models import MessageLog
        from django.db import transaction
        from rest_framework.exceptions import ValidationError

        date = serializer.validated_data["date"]
        if ClinicHoliday.objects.filter(clinic_id=self.request.clinic.id, date=date).exists():
            raise ValidationError({"date": "This date is already marked as a holiday."})

        with transaction.atomic():
            holiday = serializer.save(clinic_id=self.request.clinic.id)
            log_data_change(holiday, "create", request=self.request)

            # Cancel appointments on this date
            appointments = Appointment.objects.select_for_update().filter(
                clinic_id=self.request.clinic.id,
                is_active=True,
                starts_at__date=holiday.date,
                status__in=['scheduled', 'checked_in']
            ).select_related("patient")

            for appt in appointments:
                appt.status = 'cancelled'
                appt.save(update_fields=["status", "updated_at"])

                # Send WhatsApp message
                msg_text = f"Dear {appt.patient.full_name}, your appointment on {holiday.date.strftime('%B %d, %Y')} has been cancelled due to a clinic holiday. We apologize for the inconvenience."
                MessageLog.objects.create(
                    clinic_id=self.request.clinic.id,
                    patient=appt.patient,
                    method='whatsapp',
                    message_text=msg_text,
                    is_sent=True
                )

class ClinicHolidayDestroyView(generics.DestroyAPIView):
    serializer_class = ClinicHolidaySerializer
    permission_classes = [IsClinicAdmin]

    def get_queryset(self):
        return ClinicHoliday.objects.filter(clinic_id=self.request.clinic.id)

    def get_object(self):
        # We delete by date
        from django.http import Http404
        from django.shortcuts import get_object_or_404
        from django.utils.dateparse import parse_date
        try:
            holiday_date = parse_date(self.kwargs.get('date', ''))
        except ValueError:
            holiday_date = None
        if holiday_date is None:
            raise Http404("Invalid date.")
        return get_object_or_404(self.get_queryset(), date=holiday_date)


class CompleteOnboardingView(APIView):
    permission_classes = [IsClinicAdmin]

    def post(self, request):
        clinic = request.clinic
        if not clinic:
            return Response({"detail": "No clinic found."}, status=status.HTTP_400_BAD_REQUEST)

        data = request.data

        # Step 1: Update clinic profile
        clinic.description = data.get("description", clinic.description)
        clinic.address = data.get("address", clinic.address)
        clinic.clinic_type = data.get("clinic_type", clinic.clinic_type)
        clinic.phone = data.get("phone", clinic.phone)
        clinic.is_onboarded = True
        clinic.save()

        # Step 2 & 3: Update configuration
        config, _ = ClinicConfiguration.objects.get_or_create(clinic=clinic)
        config.ai_phone_number = data.get("ai_phone_number", config.ai_phone_number)
        config.ai_voice_type = data.get("ai_voice_type", config.ai_voice_type)
        config.save()

        return Response({
            "detail": "Onboarding completed successfully.",
            "is_onboarded": True
        })


# ---------------------------------------------------------------------------------------------
# Phone number: one per main clinic, shared by all of its centres.
# ---------------------------------------------------------------------------------------------

NOT_OWNER = {"detail": "Only the main clinic's admins can manage its phone number."}


def _owned_root(request):
    """The main clinic of the active clinic, if this admin belongs to it (else None)."""
    clinic = request.clinic
    if clinic is None:
        return None
    root = clinic.root
    if root.id != clinic.id and not request.user.clinics.filter(id=root.id).exists():
        return None
    return root


def _phone_number_payload(root):
    record = ClinicPhoneNumber.objects.filter(clinic=root).first()
    payload = {
        "phone_number": None,
        "owner_clinic": {"id": root.id, "name": root.name},
        "shared_with": list(root.group_clinics().values_list("name", flat=True)),
        "can_purchase": plivo.is_configured(),
    }
    if record is not None:
        payload["phone_number"] = {
            "number": record.number,
            "display": f"+{record.number}",
            "status": record.status,
            "city": record.city,
            "monthly_rental_rate": record.monthly_rental_rate,
            "last_error": record.last_error,
            "purchased_at": record.created_at,
            "bought_here": record.bought_here,
        }
    return payload


def _route_to_agent(record) -> None:
    """Point the number at the voice agent's SIP app; keep any failure so it can be retried."""
    app_id = settings.PLIVO_APP_ID
    if not app_id:
        record.status, record.last_error = ClinicPhoneNumber.Status.PURCHASED, "PLIVO_APP_ID is not configured."
    else:
        try:
            plivo.assign_app(record.number, app_id)
            record.status, record.app_id, record.last_error = ClinicPhoneNumber.Status.ACTIVE, app_id, ""
        except plivo.PlivoError as exc:
            logger.warning("Assigning the voice agent app to %s failed: %s", record.number, exc)
            record.status, record.last_error = ClinicPhoneNumber.Status.PURCHASED, str(exc)
    record.save(update_fields=["status", "app_id", "last_error", "updated_at"])


class PhoneNumberView(APIView):
    """GET the clinic's number; POST {"number"} to buy one; DELETE to release it."""
    permission_classes = [IsClinicAdmin]

    def get(self, request):
        root = _owned_root(request)
        if root is None:
            return Response(NOT_OWNER, status=status.HTTP_403_FORBIDDEN)
        return Response(_phone_number_payload(root))

    def post(self, request):
        root = _owned_root(request)
        if root is None:
            return Response(NOT_OWNER, status=status.HTTP_403_FORBIDDEN)
        number = plivo.normalize_number(request.data.get("number"))
        if not 8 <= len(number) <= 15:
            return Response({"number": ["Enter a valid phone number with country code."]}, status=status.HTTP_400_BAD_REQUEST)
        if ClinicPhoneNumber.objects.filter(number=number).exclude(clinic=root).exists():
            return Response({"number": ["This number already belongs to another clinic."]}, status=status.HTTP_409_CONFLICT)

        with transaction.atomic():
            # Serialise purchases per clinic group so a double click can't buy two numbers.
            Clinic.objects.select_for_update().filter(id=root.id).first()
            if ClinicPhoneNumber.objects.filter(clinic=root).exists():
                return Response({"detail": "This clinic already has a phone number. Release it first."},
                                status=status.HTTP_409_CONFLICT)
            try:
                provider_status = plivo.buy_number(number)
            except plivo.PlivoError as exc:
                return Response({"detail": f"Could not buy the number: {exc}"}, status=status.HTTP_502_BAD_GATEWAY)
            try:
                with transaction.atomic():
                    record = ClinicPhoneNumber.objects.create(
                        clinic=root,
                        number=number,
                        country_iso=str(request.data.get("country_iso") or "IN")[:2].upper(),
                        city=str(request.data.get("city") or "")[:100],
                        monthly_rental_rate=str(request.data.get("monthly_rental_rate") or "")[:20],
                        status=(ClinicPhoneNumber.Status.PENDING if "pending" in provider_status.lower()
                                else ClinicPhoneNumber.Status.PURCHASED),
                    )
            except Exception:
                # Plivo has charged for it but we couldn't record it: give it back rather than leak it.
                logger.exception("Saving bought number %s failed; releasing it", number)
                try:
                    plivo.release_number(number)
                except plivo.PlivoError:
                    logger.exception("Releasing %s after a failed save also failed", number)
                raise

        if record.status == ClinicPhoneNumber.Status.PURCHASED:
            _route_to_agent(record)
        log_data_change(record, "create", request=request)
        return Response(_phone_number_payload(root), status=status.HTTP_201_CREATED)

    def delete(self, request):
        root = _owned_root(request)
        if root is None:
            return Response(NOT_OWNER, status=status.HTTP_403_FORBIDDEN)
        record = ClinicPhoneNumber.objects.filter(clinic=root).first()
        if record is None:
            return Response({"detail": "This clinic has no phone number."}, status=status.HTTP_404_NOT_FOUND)
        # A linked number wasn't bought here: only disconnect it, never unrent it at Plivo.
        if record.bought_here:
            try:
                plivo.release_number(record.number)
            except plivo.PlivoError as exc:
                return Response({"detail": f"Could not release the number: {exc}"}, status=status.HTTP_502_BAD_GATEWAY)
        log_data_change(record, "delete", request=request)
        record.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PhoneNumberActivateView(APIView):
    """Retry routing a bought number to the voice agent (after a failure or once KYC is approved)."""
    permission_classes = [IsClinicAdmin]

    def post(self, request):
        root = _owned_root(request)
        if root is None:
            return Response(NOT_OWNER, status=status.HTTP_403_FORBIDDEN)
        record = ClinicPhoneNumber.objects.filter(clinic=root).first()
        if record is None:
            return Response({"detail": "This clinic has no phone number."}, status=status.HTTP_404_NOT_FOUND)
        _route_to_agent(record)
        ok = record.status == ClinicPhoneNumber.Status.ACTIVE
        return Response(_phone_number_payload(root), status=status.HTTP_200_OK if ok else status.HTTP_502_BAD_GATEWAY)


class PhoneNumberSearchView(APIView):
    """Search Plivo's stock: ?city=Bangalore&pattern=80&type=local&country_iso=IN&offset=0"""
    permission_classes = [IsClinicAdmin]

    def get(self, request):
        params = request.query_params
        number_type = params.get("type", "local")
        if number_type not in {"local", "mobile", "tollfree", "national", "fixed", "any"}:
            return Response({"type": ["Unsupported number type."]}, status=status.HTTP_400_BAD_REQUEST)
        try:
            offset = max(int(params.get("offset", 0)), 0)
        except ValueError:
            offset = 0
        try:
            result = plivo.search_numbers(
                country_iso=(params.get("country_iso") or "IN")[:2].upper(),
                number_type=number_type,
                city=params.get("city", "").strip()[:60],
                pattern=plivo.normalize_number(params.get("pattern"))[:10],
                limit=20,
                offset=offset,
            )
        except plivo.PlivoError as exc:
            return Response({"detail": f"Number search failed: {exc}"}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)