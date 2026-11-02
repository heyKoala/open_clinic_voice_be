import datetime

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai_agent", "0013_voice_agent_inactivity_check")]
    operations = [
        migrations.AddField(model_name="voiceagentsettings", name="answer_hours_only", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="voiceagentsettings", name="answer_from",
                            field=models.TimeField(default=datetime.time(20, 0))),
        migrations.AddField(model_name="voiceagentsettings", name="answer_until",
                            field=models.TimeField(default=datetime.time(8, 0))),
        migrations.AddField(model_name="voiceagentsettings", name="closed_message",
                            field=models.CharField(blank=True, max_length=300)),
    ]
