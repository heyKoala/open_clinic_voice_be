from django.db.models.signals import post_init, post_save
from django.dispatch import receiver
from appointments.models import Appointment
from common.events import broadcast_event


@receiver(post_init, sender=Appointment)
def remember_original_values(sender, instance, **kwargs):
    # Lets post_save tell a reschedule apart from a plain status change.
    instance._original_starts_at = instance.__dict__.get("starts_at")
    instance._original_status = instance.__dict__.get("status")


@receiver(post_save, sender=Appointment)
def notify_appointment_change(sender, instance, created, **kwargs):
    event_type = 'appointment.created' if created else 'appointment.updated'

    data = {
        "appointment_id": instance.pk,
        "doctor_id": instance.doctor_id,
        "patient_name": instance.patient.full_name if instance.patient else "Unknown",
        "starts_at": instance.starts_at.isoformat() if instance.starts_at else None,
        "status": instance.status,
        # None means the field was deferred when loaded, so we can't tell; don't claim a change.
        "rescheduled": not created and instance._original_starts_at is not None
        and instance.starts_at != instance._original_starts_at,
        "status_changed": not created and instance._original_status is not None
        and instance.status != instance._original_status,
    }
    instance._original_starts_at = instance.starts_at
    instance._original_status = instance.status

    # One broadcast to the clinic group: every connected user (doctors included) is in it,
    # so also sending to the doctor's group delivered each event twice.
    if instance.clinic_id:
        broadcast_event(f"clinic_{instance.clinic_id}", event_type, data)
