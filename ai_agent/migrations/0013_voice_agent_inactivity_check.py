from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai_agent", "0012_call_log_caller_name")]
    operations = [
        migrations.AddField(model_name="voiceagentsettings", name="inactivity_timeout",
                            field=models.PositiveSmallIntegerField(default=10)),
        migrations.AddField(model_name="voiceagentsettings", name="inactivity_message",
                            field=models.CharField(default="Are you still there?", max_length=200)),
        migrations.AddField(model_name="voiceagentsettings", name="inactivity_max_prompts",
                            field=models.PositiveSmallIntegerField(default=2)),
        migrations.AddField(model_name="voiceagentsettings", name="inactivity_end_message",
                            field=models.CharField(default="I'll end the call now since I haven't heard from you. Goodbye!",
                                                   max_length=200)),
    ]
