from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models import User


@receiver(post_save, sender=User)
def ensure_primary_clinic_membership(sender, instance, update_fields=None, **kwargs):
    """Keep the user's primary clinic in their clinic memberships (used for clinic switching)."""
    if not instance.clinic_id:
        return
    if update_fields is not None and "clinic" not in update_fields:
        return
    instance.clinics.add(instance.clinic_id)
