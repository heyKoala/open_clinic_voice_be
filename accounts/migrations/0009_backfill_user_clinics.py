from django.db import migrations


def backfill_user_clinics(apps, schema_editor):
    """Ensure every user's primary clinic is part of their clinic memberships."""
    User = apps.get_model("accounts", "User")
    Membership = User.clinics.through
    existing = set(Membership.objects.values_list("user_id", "clinic_id"))
    Membership.objects.bulk_create(
        [
            Membership(user_id=user_id, clinic_id=clinic_id)
            for user_id, clinic_id in User.objects.filter(clinic__isnull=False).values_list("id", "clinic_id")
            if (user_id, clinic_id) not in existing
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0008_user_clinics"),
    ]

    operations = [
        migrations.RunPython(backfill_user_clinics, migrations.RunPython.noop),
    ]
