import re

from django.db import migrations, models


def backfill(apps, schema_editor):
    """Older logs: take the number from the call-start placeholder ("Call from +91…") or the matched patient."""
    CallLog = apps.get_model("ai_agent", "CallLog")
    for log in CallLog.objects.filter(phone="").select_related("patient").iterator():
        number = ""
        found = re.match(r"Call from \+?(\d{10,15})", log.summary or "")
        if found:
            number = "+" + found.group(1)
        elif log.patient_id and log.patient.phone:
            digits = re.sub(r"\D", "", log.patient.phone)
            number = "+" + digits if len(digits) >= 10 else ""
        if number:
            CallLog.objects.filter(id=log.id).update(phone=number)


class Migration(migrations.Migration):
    dependencies = [("ai_agent", "0009_knowledge_base"), ("patients", "0001_initial")]
    operations = [
        migrations.AddField(model_name="calllog", name="phone", field=models.CharField(blank=True, db_index=True, max_length=20)),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
