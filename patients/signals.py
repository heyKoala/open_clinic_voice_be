from django.db.models.signals import post_save
from django.dispatch import receiver
from patients.models import Patient
from common.events import broadcast_event

@receiver(post_save, sender=Patient)
def notify_patient_change(sender, instance, created, **kwargs):
    event_type = 'patient.created' if created else 'patient.updated'
    data = {
        "patient_id": instance.pk,
        "full_name": instance.full_name,
        "phone": instance.phone,
    }
    if instance.clinic_id:
        broadcast_event(f"clinic_{instance.clinic_id}", event_type, data)
