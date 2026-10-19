from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai_agent", "0010_calllog_phone")]
    operations = [
        migrations.AddField(model_name="voiceagentsettings", name="noise_cancellation", field=models.BooleanField(default=True)),
        migrations.AddField(model_name="voiceagentsettings", name="noise_cancellation_level",
                            field=models.PositiveSmallIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="voiceagentsettings", name="recording_disclosure", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="voiceagentsettings", name="listening",
                            field=models.CharField(choices=[("patient", "Patient"), ("balanced", "Balanced"), ("quick", "Quick")],
                                                   default="balanced", max_length=16)),
        migrations.AddField(model_name="voiceagentsettings", name="max_silence_seconds",
                            field=models.PositiveSmallIntegerField(default=60)),
        migrations.AddField(model_name="voiceagentsettings", name="thinking_sound", field=models.BooleanField(default=True)),
    ]
