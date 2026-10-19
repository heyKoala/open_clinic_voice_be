"""Fetch the transcript and recording again for calls that ended up without them.

    python manage.py refetch_recordings            # calls from the last 24 hours marked no_recording / in_progress
    python manage.py refetch_recordings --id 326   # one call
    python manage.py refetch_recordings --hours 72

Useful when the backend was down or the provider uploaded the files late. Each call is polled once (no waiting).
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from ai_agent.call_sessions import poll_recording
from ai_agent.models import CallLog


class Command(BaseCommand):
    help = "Fetch missing transcripts and recordings for recent calls from the voice provider."

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, help="Only this call log")
        parser.add_argument("--hours", type=int, default=24, help="Look back this many hours (default 24)")

    def handle(self, *args, **options):
        logs = CallLog.objects.exclude(external_call_id="")
        if options["id"]:
            logs = logs.filter(id=options["id"])
        else:
            logs = logs.filter(outcome__in=["no_recording", "in_progress"], transcript="",
                               occurred_at__gte=timezone.now() - timedelta(hours=options["hours"]))
        found = 0
        for log in logs.order_by("occurred_at"):
            caller = log.phone.lstrip("+") if log.phone else ""
            ok = poll_recording(log.id, log.external_call_id, caller, attempts=1)
            found += ok
            self.stdout.write(f"CallLog {log.id} ({log.external_call_id}): {'fetched' if ok else 'still nothing'}")
        self.stdout.write(f"{found} of {logs.count()} call(s) now have their media.")
