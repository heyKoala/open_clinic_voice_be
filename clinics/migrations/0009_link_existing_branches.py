from django.db import migrations


def link_branches(apps, schema_editor):
    """Before centres were linked, a branch was only an extra clinic in its admin's membership list.
    Treat each multi-clinic admin's primary clinic as the main clinic and the clinics created after
    it as its centres."""
    User = apps.get_model("accounts", "User")
    Clinic = apps.get_model("clinics", "Clinic")
    admins = User.objects.filter(role="clinic_admin", clinic__isnull=False, clinic__parent__isnull=True)
    for admin in admins:
        Clinic.objects.filter(
            id__in=admin.clinics.values("id"), id__gt=admin.clinic_id, parent__isnull=True,
        ).exclude(branches__isnull=False).update(parent_id=admin.clinic_id)


class Migration(migrations.Migration):
    dependencies = [
        ("clinics", "0008_clinic_parent_phone_number"),
        ("accounts", "0009_backfill_user_clinics"),
    ]

    operations = [migrations.RunPython(link_branches, migrations.RunPython.noop)]
