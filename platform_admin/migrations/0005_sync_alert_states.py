"""Accounts that existed before alerts were added all got alert_state "ok". Set each one to where its balance really
stands, quietly: no email is sent, so a hospital that is already at zero is not told about it on its next call."""
from django.db import migrations


def sync_states(apps, schema_editor):
    CreditAccount = apps.get_model("platform_admin", "CreditAccount")
    for account in CreditAccount.objects.all():
        if account.balance <= 0:
            state = "empty"
        elif account.balance <= account.low_balance_threshold:
            state = "low"
        else:
            state = "ok"
        if account.alert_state != state:
            CreditAccount.objects.filter(pk=account.pk).update(alert_state=state)


class Migration(migrations.Migration):
    dependencies = [("platform_admin", "0004_credit_alerts")]
    operations = [migrations.RunPython(sync_states, migrations.RunPython.noop)]
