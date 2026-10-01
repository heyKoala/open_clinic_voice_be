from django.db import models

from common.models import ClinicScopedModel


class Patient(ClinicScopedModel):
    class Gender(models.TextChoices): MALE="male", "Male"; FEMALE="female", "Female"; OTHER="other", "Other"; UNSPECIFIED="unspecified", "Unspecified"
    class BloodGroup(models.TextChoices): A_POS="A+", "A+"; A_NEG="A-", "A-"; B_POS="B+", "B+"; B_NEG="B-", "B-"; O_POS="O+", "O+"; O_NEG="O-", "O-"; AB_POS="AB+", "AB+"; AB_NEG="AB-", "AB-"; UNKNOWN="unknown", "Unknown"
    
    full_name = models.CharField(max_length=255)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=16, choices=Gender.choices, default=Gender.UNSPECIFIED)
    blood_group = models.CharField(max_length=8, choices=BloodGroup.choices, default=BloodGroup.UNKNOWN)
    phone = models.CharField(max_length=32, db_index=True)
    email = models.EmailField(blank=True)
    emergency_contact_name = models.CharField(max_length=255, blank=True)
    emergency_contact_phone = models.CharField(max_length=32, blank=True)
    preferred_language = models.CharField(max_length=32, default="en")
    abha_number = models.CharField(max_length=32, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    class Meta:
        ordering = ["full_name", "id"]
        constraints = [models.UniqueConstraint(fields=["clinic", "phone"], name="uniq_patient_phone_per_clinic")]
        indexes = [models.Index(fields=["clinic", "full_name"])]

class MessageLog(ClinicScopedModel):
    class Method(models.TextChoices): SMS="sms", "SMS"; WHATSAPP="whatsapp", "WhatsApp"; EMAIL="email", "Email"
    class Status(models.TextChoices): PENDING="pending", "Pending"; SENT="sent", "Sent"; DELIVERED="delivered", "Delivered"; READ="read", "Read"; FAILED="failed", "Failed"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="messages")
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.SMS)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    message_text = models.TextField()
    is_sent = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
