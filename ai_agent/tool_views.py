import hashlib
import hmac

from rest_framework.permissions import BasePermission
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from datetime import datetime, timedelta
from django.utils import timezone
from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404

from doctors.models import Doctor, DoctorAbsence
from appointments.models import Appointment
from patients.models import Patient
from clinics.models import Clinic


def rock8_clinic_token(clinic_id) -> str:
    """Per-clinic token derived from ROCK8_WEBHOOK_SECRET.

    Embedded in the webhook/tool URLs handed to Rock8, so a leaked URL only grants
    access to that one clinic rather than every clinic.
    """
    secret = getattr(settings, "ROCK8_WEBHOOK_SECRET", "")
    return hmac.new(secret.encode(), f"rock8:{clinic_id}".encode(), hashlib.sha256).hexdigest()


def rock8_path(clinic_id, *parts) -> str:
    """URL path with the clinic's token inside the path, e.g. /8/<token>/slots/.
    Used for URLs handed to the voice provider, which can't send query strings or headers."""
    return "/".join([str(clinic_id), rock8_clinic_token(clinic_id), *parts]) + "/"


def with_rock8_token(url: str, clinic_id) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}token={rock8_clinic_token(clinic_id)}"


class HasRock8WebhookSecret(BasePermission):
    """Accepts ROCK8_WEBHOOK_SECRET, or the clinic's derived token, as a Bearer header, ?token=,
    or inside the URL path (/<clinic_id>/<token>/...) for providers that can't send either."""

    message = "Invalid or missing token."

    def has_permission(self, request, view):
        secret = getattr(settings, "ROCK8_WEBHOOK_SECRET", "")
        if not secret:
            self.message = "Webhook secret is not configured on the server."
            return False

        token = request.headers.get("Authorization", "")
        if token.startswith("Bearer "):
            token = token[len("Bearer "):]
        token = token or request.query_params.get("token", "") or view.kwargs.get("token", "")
        if not token:
            return False

        if hmac.compare_digest(token, secret):
            return True
        clinic_id = view.kwargs.get("clinic_id")
        return clinic_id is not None and hmac.compare_digest(token, rock8_clinic_token(clinic_id))


PLACEHOLDER_WORDS = {"unknown", "caller", "patient", "customer", "user", "n/a", "na", "none", "null", "test", "anonymous", "name"}


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class AIToolBaseView(APIView):
    authentication_classes = []
    # Server-to-server calls authenticated by secret; the per-IP anonymous throttle would
    # make every concurrent call from the provider share one 30/min budget.
    throttle_classes = []
    permission_classes = [HasRock8WebhookSecret]

    def get_clinic(self, kwargs):
        clinic_id = kwargs.get('clinic_id')
        return get_object_or_404(Clinic, id=clinic_id)

    def get_group(self, kwargs):
        """All centres that share this clinic's phone number (its main clinic and branches).
        Tools may act on any of them, and on nothing outside the group."""
        return self.get_clinic(kwargs).group_clinics()


class AIToolAvailableSlotsView(AIToolBaseView):
    def get(self, request, *args, **kwargs):
        group = self.get_group(kwargs)
        doctor_id = request.query_params.get('doctor_id')
        date_str = request.query_params.get('date')

        if not doctor_id or not date_str:
            return Response({
                "success": False,
                "error": "MISSING_PARAMS",
                "message": "doctor_id and date are required."
            })

        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response({
                "success": False,
                "error": "INVALID_DATE",
                "message": "Date must be in YYYY-MM-DD format."
            })

        doctor = Doctor.objects.filter(id=_as_int(doctor_id), clinic__in=group, is_active=True).first() if _as_int(doctor_id) else None
        if not doctor:
            return Response({
                "success": False,
                "error": "DOCTOR_NOT_FOUND",
                "message": "Doctor not found."
            })

        from clinics.models import ClinicHoliday
        is_holiday = ClinicHoliday.objects.filter(clinic_id=doctor.clinic_id, date=target_date).exists()
        if is_holiday:
            return Response({
                "success": True,
                "slots": [],
                "message": "The clinic is closed on this date (Holiday)."
            })

        if target_date.isoweekday() not in doctor.working_days:
            return Response({
                "success": True,
                "slots": [],
                "message": "Doctor does not work on this day."
            })

        is_absent = DoctorAbsence.objects.filter(
            doctor=doctor,
            start_date__lte=target_date,
            end_date__gte=target_date
        ).exists()

        if is_absent:
            return Response({
                "success": True,
                "slots": [],
                "message": "Doctor is absent on this date."
            })

        appointments_count = Appointment.objects.filter(
            doctor=doctor,
            starts_at__date=target_date,
            status__in=['scheduled', 'checked_in', 'completed']
        ).count()
        if doctor.max_patients_per_day and appointments_count >= doctor.max_patients_per_day:
            return Response({
                "success": True,
                "slots": [],
                "message": "Doctor is fully booked for this day."
            })

        if not doctor.available_from or not doctor.available_to:
             return Response({
                 "success": True,
                 "slots": [],
                 "message": "Doctor availability hours not set."
             })

        slots = []
        tz = timezone.get_current_timezone()
        current_time = timezone.make_aware(datetime.combine(target_date, doctor.available_from), tz)
        end_time = timezone.make_aware(datetime.combine(target_date, doctor.available_to), tz)

        lunch_start = timezone.make_aware(datetime.combine(target_date, doctor.lunch_from), tz) if doctor.lunch_from else None
        lunch_end = timezone.make_aware(datetime.combine(target_date, doctor.lunch_to), tz) if doctor.lunch_to else None

        existing_appts = list(Appointment.objects.filter(
            doctor=doctor,
            starts_at__date=target_date,
            status__in=['scheduled', 'checked_in', 'completed', 'in_consultation']
        ))

        now = timezone.localtime()

        while current_time + timedelta(minutes=doctor.consultation_minutes) <= end_time:
            slot_end = current_time + timedelta(minutes=doctor.consultation_minutes)

            if current_time <= now:
                current_time = slot_end
                continue

            if lunch_start and lunch_end:
                if current_time >= lunch_start and current_time < lunch_end:
                    current_time = slot_end
                    continue

            overlap = False
            for appt in existing_appts:
                appt_start = timezone.localtime(appt.starts_at)
                appt_end = timezone.localtime(appt.ends_at)

                if current_time < appt_end and slot_end > appt_start:
                    overlap = True
                    break

            if not overlap:
                slots.append(current_time.strftime("%H:%M"))

            current_time = slot_end

        return Response({
            "success": True,
            "date": date_str,
            "slots": slots
        })


class AIToolBookAppointmentView(AIToolBaseView):
    def post(self, request, *args, **kwargs):
        group = self.get_group(kwargs)
        doctor_id = request.data.get('doctor_id')
        date_str = request.data.get('date')
        time_str = request.data.get('time')
        patient_phone = request.data.get('patient_phone')
        patient_name = request.data.get('patient_name')

        if not all([doctor_id, date_str, time_str, patient_phone, patient_name]):
            return Response({
                "success": False,
                "error": "MISSING_PARAMS",
                "message": "doctor_id, date, time, patient_phone, and patient_name are required."
            })

        # The model sometimes fills required fields with placeholders instead of asking the caller.
        name_words = {w.strip(".,").lower() for w in str(patient_name).split()}
        if len(str(patient_name).strip()) < 2 or name_words & PLACEHOLDER_WORDS:
            return Response({
                "success": False,
                "error": "PATIENT_NAME_REQUIRED",
                "message": "Ask the caller for the patient's full name, then try again."
            })
        if len("".join(ch for ch in str(patient_phone) if ch.isdigit())) < 10:
            return Response({
                "success": False,
                "error": "PATIENT_PHONE_REQUIRED",
                "message": "Ask the caller for their 10-digit phone number (or use the number they are calling from), then try again."
            })

        doctor = Doctor.objects.filter(id=_as_int(doctor_id), clinic__in=group, is_active=True).select_related("clinic").first() if _as_int(doctor_id) else None
        if not doctor:
            return Response({
                "success": False,
                "error": "DOCTOR_NOT_FOUND",
                "message": "Doctor not found or inactive."
            })
        # The appointment (and the patient record) belong to the centre the doctor works at.
        clinic = doctor.clinic

        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            target_time = datetime.strptime(time_str, '%H:%M').time()
        except ValueError:
            return Response({
                "success": False,
                "error": "INVALID_DATE_TIME",
                "message": "Invalid date or time format."
            })

        tz = timezone.get_current_timezone()
        starts_at = timezone.make_aware(datetime.combine(target_date, target_time), tz)
        ends_at = starts_at + timedelta(minutes=doctor.consultation_minutes)

        if starts_at <= timezone.localtime():
            return Response({
                "success": False,
                "error": "INVALID_TIME",
                "message": "Cannot book appointments in the past."
            })

        from clinics.models import ClinicHoliday
        if ClinicHoliday.objects.filter(clinic=clinic, date=target_date).exists():
            return Response({
                "success": False,
                "error": "CLINIC_CLOSED",
                "message": "The clinic is closed on this date (Holiday)."
            })

        if target_date.isoweekday() not in doctor.working_days:
            return Response({
                "success": False,
                "error": "DOCTOR_UNAVAILABLE",
                "message": "Doctor does not work on this day."
            })

        is_absent = DoctorAbsence.objects.filter(
            doctor=doctor,
            start_date__lte=target_date,
            end_date__gte=target_date
        ).exists()
        if is_absent:
            return Response({
                "success": False,
                "error": "DOCTOR_UNAVAILABLE",
                "message": "Doctor is absent on this date."
            })

        if doctor.available_from and doctor.available_to:
            if target_time < doctor.available_from or ends_at.time() > doctor.available_to:
                return Response({
                    "success": False,
                    "error": "OUTSIDE_HOURS",
                    "message": "Requested time is outside doctor's available hours."
                })

        if doctor.lunch_from and doctor.lunch_to:
            if target_time < doctor.lunch_to and timezone.localtime(ends_at).time() > doctor.lunch_from:
                return Response({
                    "success": False,
                    "error": "LUNCH_TIME",
                    "message": "Requested time overlaps with doctor's lunch."
                })

        # Serialise bookings per doctor so two callers cannot take the same slot.
        with transaction.atomic():
            Doctor.objects.select_for_update().filter(pk=doctor.pk).first()
            return self._book_locked(clinic, doctor, patient_name, patient_phone, target_date, starts_at, ends_at)

    def _book_locked(self, clinic, doctor, patient_name, patient_phone, target_date, starts_at, ends_at):
        overlap = Appointment.objects.filter(
            doctor=doctor,
            status__in=['scheduled', 'checked_in', 'completed', 'in_consultation'],
            starts_at__lt=ends_at,
            ends_at__gt=starts_at
        ).exists()
        if overlap:
            return Response({
                "success": False,
                "error": "SLOT_UNAVAILABLE",
                "message": "That appointment slot is no longer available."
            })

        count = Appointment.objects.filter(
            doctor=doctor, starts_at__date=target_date, status__in=['scheduled', 'checked_in', 'completed']
        ).count()
        if doctor.max_patients_per_day and count >= doctor.max_patients_per_day:
            return Response({
                "success": False,
                "error": "CAPACITY_REACHED",
                "message": "Doctor is fully booked for this day."
            })

        patient = Patient.objects.filter(clinic=clinic, phone=patient_phone).first()
        if not patient:
            patient = Patient.objects.create(
                clinic=clinic,
                full_name=patient_name,
                phone=patient_phone
            )

        appt = Appointment.objects.create(
            clinic=clinic,
            patient=patient,
            doctor=doctor,
            starts_at=starts_at,
            ends_at=ends_at,
            reason="AI booked appointment",
            status=Appointment.Status.SCHEDULED,
            source="phone"
        )

        return Response({
            "success": True,
            "appointment_id": appt.id,
            "patient": patient.full_name,
            "doctor": doctor.full_name,
            "centre": clinic.name,
            "centre_address": clinic.address,
            "starts_at": appt.starts_at.isoformat(),
            "ends_at": appt.ends_at.isoformat(),
            "message": "Appointment booked successfully."
        })


class AIToolCancelAppointmentView(AIToolBaseView):
    def post(self, request, *args, **kwargs):
        group = self.get_group(kwargs)
        appointment_id = request.data.get('appointment_id')

        if not appointment_id:
            return Response({
                "success": False,
                "error": "MISSING_PARAMS",
                "message": "appointment_id is required."
            })

        appt = Appointment.objects.filter(id=_as_int(appointment_id), clinic__in=group).first() if _as_int(appointment_id) else None
        if not appt:
            return Response({
                "success": False,
                "error": "NOT_FOUND",
                "message": "Appointment not found."
            })

        if appt.status in ['cancelled', 'completed', 'no_show']:
            return Response({
                "success": False,
                "error": "INVALID_STATE",
                "message": f"Appointment is already {appt.status}."
            })

        appt.status = 'cancelled'
        appt.save(update_fields=['status', 'updated_at'])
        from queue_mgmt.models import QueueToken
        QueueToken.objects.filter(
            appointment=appt,
            status__in=[QueueToken.Status.WAITING, QueueToken.Status.CHECKED_IN],
        ).update(status=QueueToken.Status.SKIPPED, updated_at=timezone.now())

        return Response({
            "success": True,
            "message": "Appointment cancelled successfully."
        })


class AIToolListDoctorsView(AIToolBaseView):
    def get(self, request, *args, **kwargs):
        group = self.get_group(kwargs)
        doctors = Doctor.objects.filter(clinic__in=group, is_active=True).select_related("clinic").order_by("clinic__name", "full_name")

        data = []
        for d in doctors:
            data.append({
                "id": d.id,
                "name": d.full_name,
                "specialty": d.specialty,
                "centre": d.clinic.name,
                "centre_address": d.clinic.address,
            })

        return Response({
            "success": True,
            "doctors": data
        })

class AIToolLookupAppointmentsView(AIToolBaseView):
    def get(self, request, *args, **kwargs):
        group = self.get_group(kwargs)
        patient_name = request.query_params.get('patient_name', '').strip()
        patient_phone = request.query_params.get('patient_phone', '').strip()

        if not patient_name and not patient_phone:
            return Response({
                "success": False,
                "error": "MISSING_PARAMS",
                "message": "Either patient_name or patient_phone is required."
            })

        phone_digits = "".join(ch for ch in patient_phone if ch.isdigit())
        if patient_phone and len(phone_digits) < 7:
            return Response({
                "success": False,
                "error": "INVALID_PARAMS",
                "message": "Please provide the patient's full phone number."
            })
        if not patient_phone and len(patient_name) < 3:
            return Response({
                "success": False,
                "error": "INVALID_PARAMS",
                "message": "Please provide the patient's full name."
            })

        appointments = Appointment.objects.filter(
            clinic__in=group,
            status__in=['scheduled', 'checked_in']
        ).select_related("clinic")

        if patient_phone:
            # Narrow in SQL, then compare digits only (stored numbers may contain spaces or +91).
            wanted = phone_digits[-10:]
            matching_ids = [
                appt_id for appt_id, phone in appointments.filter(patient__phone__contains=wanted[-4:]).values_list("id", "patient__phone")
                if "".join(ch for ch in phone if ch.isdigit()).endswith(wanted)
            ]
            appointments = appointments.filter(id__in=matching_ids)
        elif patient_name:
            appointments = appointments.filter(patient__full_name__icontains=patient_name)

        appointments = appointments.order_by('starts_at')[:10]

        data = []
        for appt in appointments:
            data.append({
                "appointment_id": appt.id,
                "patient_name": appt.patient.full_name,
                "patient_phone": appt.patient.phone,
                "doctor_name": appt.doctor.full_name,
                "centre": appt.clinic.name,
                "date": timezone.localtime(appt.starts_at).strftime('%Y-%m-%d'),
                "time": timezone.localtime(appt.starts_at).strftime('%H:%M')
            })

        return Response({
            "success": True,
            "appointments": data
        })
