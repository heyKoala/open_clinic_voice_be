from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.db.models import ProtectedError
from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from clinics import plivo
from clinics.models import Clinic, ClinicConfiguration, ClinicIntegration, ClinicPhoneNumber, PaymentTransaction
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

# ---------------------------------------------------------------------------------------------
# Integrations: the clinic's own systems (WATI, its doctor/appointment system). One set per main
# clinic, shared by its centres. Credentials are stored encrypted and only ever returned as a hint.
# ---------------------------------------------------------------------------------------------

def _public_base_url(request) -> str:
    # Same rule as the voice webhooks: a tunnel's forwarded host, else BACKEND_URL, else this request's host.
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_host:
        return f"https://{forwarded_host}"
    return getattr(settings, "BACKEND_URL", f"{request.scheme}://{request.get_host()}").rstrip("/")


def _integration_payload(provider, record, root, request):
    from clinics import secrets
    from clinics.integrations import PROVIDERS, razorpay_webhook_path, razorpay_webhook_secret, wati_webhook_path

    secret = secrets.decrypt(record.secret_encrypted) if record else ""
    payload = {
        "provider": provider,
        "label": ClinicIntegration.Provider(provider).label,
        "secret_field": PROVIDERS[provider]["secret"],
        "configured": record is not None,
        "status": record.status if record else "not_connected",
        "settings": record.settings if record else {},
        "has_secret": bool(secret),
        "secret_hint": secrets.hint(secret),
        "last_checked_at": record.last_checked_at if record else None,
        "last_error": record.last_error if record else "",
    }
    if provider == ClinicIntegration.Provider.WATI:
        payload["webhook_url"] = _public_base_url(request) + wati_webhook_path(root.id)
        payload["last_message_at"] = record.last_message_at if record else None
    elif provider == ClinicIntegration.Provider.RAZORPAY:
        # The admin pastes both into Razorpay → Webhooks; the secret is ours, not Razorpay's key.
        payload["webhook_url"] = _public_base_url(request) + razorpay_webhook_path(root.id)
        payload["webhook_secret"] = razorpay_webhook_secret(root.id)
        payload["last_message_at"] = record.last_message_at if record else None
    return payload


def _integration_provider(provider):
    return provider if provider in ClinicIntegration.Provider.values else None


class IntegrationListView(APIView):
    """GET every integration the clinic can set up, with what is saved for each."""
    permission_classes = [IsClinicAdmin]

    def get(self, request):
        root = _owned_root(request)
        if root is None:
            return Response({"detail": "Only the main clinic's admins manage integrations."}, status=status.HTTP_403_FORBIDDEN)
        saved = {r.provider: r for r in ClinicIntegration.objects.filter(clinic=root)}
        return Response([_integration_payload(p, saved.get(p), root, request) for p in ClinicIntegration.Provider.values])


class IntegrationDetailView(APIView):
    """PUT {"settings": {...}, "secret": "..."} to save (a blank secret keeps the saved one); DELETE to disconnect."""
    permission_classes = [IsClinicAdmin]

    def _target(self, request, provider):
        root = _owned_root(request)
        if root is None:
            return None, Response({"detail": "Only the main clinic's admins manage integrations."}, status=status.HTTP_403_FORBIDDEN)
        if not _integration_provider(provider):
            return None, Response({"detail": "Unknown integration."}, status=status.HTTP_404_NOT_FOUND)
        return root, None

    def put(self, request, provider):
        from clinics import secrets
        from clinics.integrations import clean_settings

        root, error = self._target(request, provider)
        if error:
            return error
        record = ClinicIntegration.objects.filter(clinic=root, provider=provider).first()
        values, errors = clean_settings(provider, request.data.get("settings") or {})
        secret = str(request.data.get("secret") or "").strip() or (secrets.decrypt(record.secret_encrypted) if record else "")
        if not secret:
            errors["secret"] = ["This field is required."]
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)
        record, _ = ClinicIntegration.objects.update_or_create(
            clinic=root, provider=provider,
            defaults={"settings": values, "secret_encrypted": secrets.encrypt(secret),
                      "status": ClinicIntegration.Status.UNTESTED, "last_error": ""},
        )
        # The credential itself is never written to the audit log.
        log_data_change(record, "update", request=request, metadata={"provider": provider, "settings": values})
        return Response(_integration_payload(provider, record, root, request))

    def delete(self, request, provider):
        root, error = self._target(request, provider)
        if error:
            return error
        record = ClinicIntegration.objects.filter(clinic=root, provider=provider).first()
        if record:
            log_data_change(record, "delete", request=request, metadata={"provider": provider})
            record.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class IntegrationTestView(APIView):
    """POST to check the saved connection. Body {"settings", "secret"} checks those instead (before saving);
    a blank secret uses the saved one. A saved integration records the result."""
    permission_classes = [IsClinicAdmin]

    def post(self, request, provider):
        from django.utils import timezone
        from clinics import secrets
        from clinics.integrations import CHECKS, CheckFailed, clean_settings

        root = _owned_root(request)
        if root is None:
            return Response({"detail": "Only the main clinic's admins manage integrations."}, status=status.HTTP_403_FORBIDDEN)
        if not _integration_provider(provider):
            return Response({"detail": "Unknown integration."}, status=status.HTTP_404_NOT_FOUND)
        record = ClinicIntegration.objects.filter(clinic=root, provider=provider).first()
        draft = request.data.get("settings")
        values, errors = clean_settings(provider, draft if draft is not None else (record.settings if record else {}))
        secret = str(request.data.get("secret") or "").strip() or (secrets.decrypt(record.secret_encrypted) if record else "")
        if not secret:
            errors["secret"] = ["This field is required."]
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = CHECKS[provider](values, secret)
            ok, message = True, result["message"]
        except CheckFailed as exc:
            ok, message, result = False, str(exc), {"templates": []}

        # Record the outcome when this checked what is saved (not an unsaved draft).
        if record and draft is None:
            record.status = ClinicIntegration.Status.CONNECTED if ok else ClinicIntegration.Status.ERROR
            record.last_error = "" if ok else message
            record.last_checked_at = timezone.now()
            record.save(update_fields=["status", "last_error", "last_checked_at", "updated_at"])
        return Response({"ok": ok, "message": message, "templates": result.get("templates", []),
                         "integration": _integration_payload(provider, record, root, request) if record else None})


class WatiWebhookView(APIView):
    """WATI posts here when a patient messages the clinic's WhatsApp number (set up in WATI → Webhooks).

    The URL carries a per-clinic token instead of a login. For now this records that a message arrived,
    which shows on the Integrations page; answering the patient and booking from the chat come next.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, clinic_id, token):
        import hmac
        from django.utils import timezone
        from clinics.integrations import wati_webhook_token

        if not hmac.compare_digest(token, wati_webhook_token(clinic_id)):
            return Response({"detail": "Unknown webhook."}, status=status.HTTP_404_NOT_FOUND)
        record = ClinicIntegration.objects.filter(clinic_id=clinic_id, provider=ClinicIntegration.Provider.WATI).first()
        if record is None:
            return Response({"detail": "WhatsApp is not connected for this clinic."}, status=status.HTTP_404_NOT_FOUND)
        body = request.data if isinstance(request.data, dict) else {}
        # Only patients' messages count; WATI also reports the clinic's own sent and delivered messages.
        if body.get("eventType", "message") == "message" and not body.get("owner"):
            record.last_message_at = timezone.now()
            record.save(update_fields=["last_message_at", "updated_at"])
        return Response({"ok": True})


class RazorpayWebhookView(APIView):
    """Razorpay posts payment-link events here (set up in Razorpay → Webhooks with our secret).

    Each event is signed (X-Razorpay-Signature, HMAC-SHA256 of the body). For now this records that an
    event arrived, which shows on the Integrations page; marking the booking paid comes with payment links.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, clinic_id):
        from django.utils import timezone
        from clinics.integrations import razorpay_signature_ok

        if not razorpay_signature_ok(clinic_id, request.body, request.headers.get("X-Razorpay-Signature", "")):
            return Response({"detail": "Bad signature."}, status=status.HTTP_400_BAD_REQUEST)
        record = ClinicIntegration.objects.filter(clinic_id=clinic_id, provider=ClinicIntegration.Provider.RAZORPAY).first()
        if record is None:
            return Response({"detail": "Razorpay is not connected for this clinic."}, status=status.HTTP_404_NOT_FOUND)
        record.last_message_at = timezone.now()
        record.save(update_fields=["last_message_at", "updated_at"])
        return Response({"ok": True})
