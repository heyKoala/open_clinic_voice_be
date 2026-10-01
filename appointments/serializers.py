from rest_framework import serializers

from appointments.models import Appointment


class AppointmentSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source='patient.full_name', read_only=True)
    patient_phone = serializers.CharField(source='patient.phone', read_only=True)
    patient_email = serializers.CharField(source='patient.email', read_only=True)
    doctor_name = serializers.CharField(source='doctor.user.full_name', read_only=True)
    doctor_notes = serializers.CharField(source='queue_token.notes', read_only=True)

    class Meta:
        model = Appointment
        fields = ["id","patient","patient_name","patient_phone","patient_email","doctor","doctor_name","doctor_notes","starts_at","ends_at","reason","status","priority","source","created_at","updated_at"]
        read_only_fields = ["id","created_at","updated_at"]
    def validate(self, attrs):
        if attrs.get("ends_at") and attrs.get("starts_at") and attrs["ends_at"] <= attrs["starts_at"]: raise serializers.ValidationError({"ends_at":"Must be after starts_at."})
        clinic_id = self.context["request"].user.clinic_id
        for field in ("patient", "doctor"):
            value = attrs.get(field) or getattr(self.instance, field, None)
            if value and value.clinic_id != clinic_id:
                raise serializers.ValidationError({field: "Must belong to your clinic."})

        doctor = attrs.get("doctor") or getattr(self.instance, "doctor", None)
        starts_at = attrs.get("starts_at") or getattr(self.instance, "starts_at", None)
        ends_at = attrs.get("ends_at") or getattr(self.instance, "ends_at", None)

        status = attrs.get("status") or getattr(self.instance, "status", None)

        if doctor and starts_at and ends_at and status != 'cancelled':
            from django.utils import timezone
            local_starts_at = timezone.localtime(starts_at)
            local_ends_at = timezone.localtime(ends_at)
            if doctor.available_from and local_starts_at.time() < doctor.available_from:
                time_str = doctor.available_from.strftime("%I:%M %p").lstrip("0")
                raise serializers.ValidationError({"non_field_errors": f"Cannot book an appointment because the doctor is not available before {time_str}"})
            if doctor.available_to and local_ends_at.time() > doctor.available_to:
                time_str = doctor.available_to.strftime("%I:%M %p").lstrip("0")
                raise serializers.ValidationError({"non_field_errors": f"Cannot book an appointment because the doctor is not available after {time_str}"})

            if doctor.lunch_from and doctor.lunch_to:
                if local_starts_at.time() < doctor.lunch_to and local_ends_at.time() > doctor.lunch_from:
                    lunch_start_str = doctor.lunch_from.strftime("%I:%M %p").lstrip("0")
                    lunch_end_str = doctor.lunch_to.strftime("%I:%M %p").lstrip("0")
                    raise serializers.ValidationError({"non_field_errors": f"Cannot book an appointment because it overlaps with the doctor's lunch time ({lunch_start_str} - {lunch_end_str})"})

            if doctor.working_days is not None and local_starts_at.date() >= timezone.localtime(timezone.now()).date():
                if local_starts_at.isoweekday() not in doctor.working_days:
                    raise serializers.ValidationError({"non_field_errors": "Doctor does not work on this day of the week."})

            if doctor.max_patients_per_day is not None:
                date = local_starts_at.date()
                qs = Appointment.objects.filter(doctor=doctor, starts_at__date=date).exclude(status='cancelled')
                if self.instance:
                    qs = qs.exclude(id=self.instance.id)
                # TODO: Implement locking/transaction hardening to fix potential race condition here
                if qs.count() >= doctor.max_patients_per_day:
                    raise serializers.ValidationError({"non_field_errors": "Doctor capacity reached for this day."})

            from doctors.models import DoctorAbsence
            is_absent = DoctorAbsence.objects.filter(
                doctor=doctor,
                start_date__lte=local_starts_at.date(),
                end_date__gte=local_starts_at.date()
            ).exists()
            if is_absent:
                raise serializers.ValidationError({"non_field_errors": "Doctor is marked as absent for this date."})

            overlap_qs = Appointment.objects.filter(
                doctor=doctor,
                status__in=['scheduled', 'checked_in', 'needs_reschedule'],
                starts_at__lt=ends_at,
                ends_at__gt=starts_at
            )
            if self.instance:
                overlap_qs = overlap_qs.exclude(id=self.instance.id)
            if overlap_qs.exists():
                raise serializers.ValidationError({"non_field_errors": "This time slot overlaps with an existing appointment."})

        return attrs
