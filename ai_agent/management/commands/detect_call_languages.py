"""Set the language of already-logged calls from what the caller actually spoke.

    python manage.py detect_call_languages             # calls with a transcript still marked English
    python manage.py detect_call_languages --all       # re-detect every call with a transcript
    python manage.py detect_call_languages --id 335

Calls used to be logged as English whatever the caller spoke; new calls are detected after the call
(ai_agent.call_events.fetch_media). Uses the chat model (SENTIMENT_API_KEY), one request per call, or the
script of the caller's lines when the model is off.
"""
from django.core.management.base import BaseCommand

from ai_agent.models import CallLog
from ai_agent.sentiment import language_from_transcript


class Command(BaseCommand):
    help = "Detect the language of logged calls from their transcripts."

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, help="Only this call log")
        parser.add_argument("--all", action="store_true", help="Also re-detect calls not marked English")

    def handle(self, *args, **options):
        logs = CallLog.objects.exclude(transcript="")
        if options["id"]:
            logs = logs.filter(id=options["id"])
        elif not options["all"]:
            logs = logs.filter(language="en")
        changed = 0
        for log in logs.order_by("occurred_at"):
            language = language_from_transcript(log.transcript)
            if language and language != log.language:
                CallLog.objects.filter(id=log.id).update(language=language)
                changed += 1
                self.stdout.write(f"CallLog {log.id}: {log.language} -> {language}")
        self.stdout.write(self.style.SUCCESS(f"Updated {changed} call(s)."))
