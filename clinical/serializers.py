from rest_framework import serializers
from .models import (
    ClinicalEncounter, SymptomSummary, ClinicalNote,
    PatientHealthRecord, ClinicalAttachment
)
from patients.models import Patient
from patients.serializers import PatientSerializer
from doctors.serializers import DoctorSerializer
from appointments.serializers import AppointmentSerializer
from queue_mgmt.serializers import QueueTokenSerializer
from ai_agent.serializers import CallLogSerializer
from ai_agent.models import CallLog
from appointments.models import Appointment
from doctors.models import Doctor
from queue_mgmt.models import QueueToken


class ClinicScopedPKField(serializers.PrimaryKeyRelatedField):
    """Primary-key field that only resolves objects in the request's active clinic, so an id from
    another branch is rejected as "does not exist" instead of being linked."""

    def get_queryset(self):
        queryset = super().get_queryset()
        request = self.context.get("request")
        clinic = getattr(request, "clinic", None) if request is not None else None
        return queryset.filter(clinic=clinic) if clinic is not None else queryset.none()


class ClinicalEncounterSerializer(serializers.ModelSerializer):
    patient = ClinicScopedPKField(queryset=Patient.objects.all())
    doctor = ClinicScopedPKField(queryset=Doctor.objects.all())
    appointment = ClinicScopedPKField(queryset=Appointment.objects.all(), required=False, allow_null=True)
    queue_token = ClinicScopedPKField(queryset=QueueToken.objects.all(), required=False, allow_null=True)

    class Meta:
        model = ClinicalEncounter
        fields = [
            'id', 'patient', 'doctor', 'encounter_date',
            'appointment', 'queue_token', 'status',
            'chief_complaint', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate(self, attrs):
        request = self.context.get("request")
        doctor = attrs.get("doctor", getattr(self.instance, "doctor", None))
        if request is not None and request.user.role == "doctor":
            own = getattr(request.user, "doctor_profile", None)
            if own is None or doctor != own:
                raise serializers.ValidationError({"doctor": "Doctors can only record their own encounters."})
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['patient'] = PatientSerializer(instance.patient, context=self.context).data
        data['doctor'] = DoctorSerializer(instance.doctor, context=self.context).data
        data['appointment'] = AppointmentSerializer(instance.appointment, context=self.context).data if instance.appointment else None
        data['queue_token'] = QueueTokenSerializer(instance.queue_token, context=self.context).data if instance.queue_token else None
        return data


def _check_own_encounter(serializer, encounter):
    """Doctors may only attach summaries/notes to their own encounters."""
    request = serializer.context.get("request")
    if request is not None and request.user.role == "doctor":
        own = getattr(request.user, "doctor_profile", None)
        if own is None or encounter.doctor_id != own.id:
            raise serializers.ValidationError("You can only add this to your own encounters.")
    return encounter


class SymptomSummarySerializer(serializers.ModelSerializer):
    encounter = ClinicScopedPKField(queryset=ClinicalEncounter.objects.all())
    call_log = ClinicScopedPKField(queryset=CallLog.objects.all(), required=False, allow_null=True)
    reviewed_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = SymptomSummary
        fields = [
            'id', 'encounter', 'call_log', 'summary_text',
            'ai_model', 'ai_version', 'confidence', 'status',
            'reviewed_by', 'reviewed_at', 'created_at', 'updated_at'
        ]
        # Review state changes only through the approve/reject/edit_summary actions.
        read_only_fields = ['id', 'status', 'reviewed_at', 'created_at', 'updated_at']

    def validate_encounter(self, value):
        return _check_own_encounter(self, value)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['encounter'] = ClinicalEncounterSerializer(instance.encounter, context=self.context).data
        data['call_log'] = CallLogSerializer(instance.call_log, context=self.context).data if instance.call_log else None
        return data


class ClinicalNoteSerializer(serializers.ModelSerializer):
    encounter = ClinicScopedPKField(queryset=ClinicalEncounter.objects.all())
    author = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = ClinicalNote
        fields = [
            'id', 'encounter', 'author', 'note_type',
            'content', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_encounter(self, value):
        return _check_own_encounter(self, value)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['encounter'] = ClinicalEncounterSerializer(instance.encounter, context=self.context).data
        return data


class _ClinicScopedPatientMixin:
    """Accepts ``patient`` (and ``encounter``) as ids on write, returns nested objects on read."""

    def _check_clinic(self, value, field):
        request = self.context.get("request")
        if value is not None and request is not None and value.clinic_id != getattr(getattr(request, "clinic", None), "id", None):
            raise serializers.ValidationError(f"{field.capitalize()} must belong to your clinic.")
        return value

    def validate_patient(self, value):
        return self._check_clinic(value, "patient")

    def validate_encounter(self, value):
        return self._check_clinic(value, "encounter")


class PatientHealthRecordSerializer(_ClinicScopedPatientMixin, serializers.ModelSerializer):
    patient = serializers.PrimaryKeyRelatedField(queryset=Patient.objects.all())
    recorded_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = PatientHealthRecord
        fields = [
            'id', 'patient', 'category', 'name', 'details',
            'start_date', 'end_date', 'recorded_by', 'recorded_at'
        ]
        read_only_fields = ['id', 'recorded_at']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['patient'] = PatientSerializer(instance.patient, context=self.context).data
        return data


class ClinicalAttachmentSerializer(_ClinicScopedPatientMixin, serializers.ModelSerializer):
    encounter = serializers.PrimaryKeyRelatedField(queryset=ClinicalEncounter.objects.all(), required=False, allow_null=True)
    patient = serializers.PrimaryKeyRelatedField(queryset=Patient.objects.all(), required=False, allow_null=True)
    uploaded_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = ClinicalAttachment
        fields = [
            'id', 'encounter', 'patient', 'file', 'filename',
            'file_size', 'mime_type', 'uploaded_by', 'uploaded_at'
        ]
        # Size and type are taken from the uploaded file, not trusted from the client.
        read_only_fields = ['id', 'file_size', 'mime_type', 'uploaded_at']
        extra_kwargs = {'filename': {'required': False}}

    def validate(self, attrs):
        if not attrs.get('patient') and not attrs.get('encounter') and self.instance is None:
            raise serializers.ValidationError("Either patient or encounter is required.")
        upload = attrs.get('file')
        if upload is not None:
            attrs['file_size'] = upload.size
            attrs['mime_type'] = getattr(upload, 'content_type', '') or 'application/octet-stream'
            attrs.setdefault('filename', upload.name)
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['patient'] = PatientSerializer(instance.patient, context=self.context).data if instance.patient else None
        data['encounter'] = ClinicalEncounterSerializer(instance.encounter, context=self.context).data if instance.encounter else None
        return data
