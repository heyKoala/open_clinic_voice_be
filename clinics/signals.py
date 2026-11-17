from __future__ import annotations

from django.db.models import ProtectedError
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from clinics.models import Clinic


@receiver(pre_delete, sender=Clinic)
def never_delete_a_hospital(sender, instance, **kwargs):
    """A hospital (a main clinic) is archived, never deleted: its ledger, call logs and patients must survive.

    Covers every route (the Django admin, bulk deletes, shell). Only a centre can be deleted, and the centre
    views already refuse while it holds records.
    """
    if instance.parent_id is None:
        raise ProtectedError("A hospital can't be deleted. Archive it from the platform admin instead.", {instance})
