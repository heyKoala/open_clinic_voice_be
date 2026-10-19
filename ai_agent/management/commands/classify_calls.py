"""Give already-logged calls their outcome tag (booked / cancelled / rescheduled / information only / needs callback).

    python manage.py classify_calls             # inbound calls with a transcript and no tag yet
    python manage.py classify_calls --all       # re-decide every inbound call with a transcript
    python manage.py classify_calls --id 328

Tool-based outcomes are exact; the rest come from the chat model (SENTIMENT_API_KEY), one request per call.
"""
from django.core.management.base import BaseCommand

from ai_agent.call_events import PLACEHOLDER_OUTCOMES, classify_outcome
from ai_agent.models import CallLog


class Command(BaseCommand):
    help = "Classify the outcome of logged calls from their transcripts."

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, help="Only this call log")
        parser.add_argument("--all", action="store_true", help="Also re-decide calls that already have a tag")

    def handle(self, *args, **options):
        logs = CallLog.objects.filter(direction=CallLog.Direction.INBOUND).exclude(transcript="")
        if options["id"]:
            logs = logs.filter(id=options["id"])
        elif not options["all"]:
            logs = logs.filter(outcome__in=PLACEHOLDER_OUTCOMES)
        changed = 0
        for log in logs.order_by("occurred_at"):
            # The stored transcript is the formatted one; tool results are only in the raw text, so a stored
            # transcript can give tool-based outcomes only through the linked appointment.
            outcome = classify_outcome(log.transcript, log.transcript)
            if outcome is None:
                self.stdout.write(f"CallLog {log.id}: could not classify (model off or failed)")
                continue
            if not outcome and log.appointment_id:
                outcome = "appointment_booked" if log.appointment.source == "phone" else outcome
            if outcome != log.outcome:
                CallLog.objects.filter(id=log.id).update(outcome=outcome)
                changed += 1
            self.stdout.write(f"CallLog {log.id}: {outcome or '(no tag)'}")
        self.stdout.write(f"{changed} call(s) updated.")
