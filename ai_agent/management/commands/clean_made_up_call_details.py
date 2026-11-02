"""Clear call details the model made up instead of reading them from the caller.

    python manage.py clean_made_up_call_details --dry-run   # list them, change nothing
    python manage.py clean_made_up_call_details

* Calls where the caller never spoke (only the agent's greeting in the transcript): their summary, sentiment and
  caller name were written from the greeting alone (e.g. a summary of a "knee injury" call nobody made).
* Caller names the caller never said but the agent did: read from its "Nice to hear from you again, <name>"
  greeting, so a wrong name was saved again on every call.

New calls no longer get them (see call_events.caller_spoke and sentiment.caller_name_from_transcript); this fixes
the ones logged before.
"""
from django.core.management.base import BaseCommand

from ai_agent.call_events import caller_spoke, is_placeholder_summary
from ai_agent.models import CallLog


def _lines(transcript: str, speaker: str) -> str:
    return "\n".join(line for line in transcript.splitlines() if line.startswith(f"{speaker}:")).lower()


def echoed_name(transcript: str, name: str) -> bool:
    """The name (any of its words) is in the agent's lines but not in the caller's."""
    words = [word.lower() for word in name.split() if len(word.rstrip(".")) > 1]
    agent, caller = _lines(transcript, "AI"), _lines(transcript, "Patient")
    return any(word in agent for word in words) and not any(word in caller for word in words)


class Command(BaseCommand):
    help = "Clear made-up summaries, sentiments and caller names on logged calls."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List the calls without changing them")

    def handle(self, *args, **options):
        changed = 0
        for log in CallLog.objects.exclude(transcript="").order_by("occurred_at"):
            updates = {}
            if not caller_spoke(log.transcript):
                if not is_placeholder_summary(log.summary):
                    updates["summary"] = ""
                if log.sentiment:
                    updates["sentiment"] = ""
                if log.caller_name:
                    updates["caller_name"] = ""
            elif log.caller_name and echoed_name(log.transcript, log.caller_name):
                updates["caller_name"] = ""
            if not updates:
                continue
            changed += 1
            name = f" (caller name {log.caller_name!r})" if "caller_name" in updates else ""
            self.stdout.write(f"CallLog {log.id} ({log.occurred_at:%d %b %H:%M}){name}: clearing {', '.join(updates)}")
            if not options["dry_run"]:
                CallLog.objects.filter(id=log.id).update(**updates)
        self.stdout.write(f"{changed} call(s) {'to update' if options['dry_run'] else 'updated'}.")
