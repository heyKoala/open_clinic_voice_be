from django.core.management.base import BaseCommand

from appointments import outbound


class Command(BaseCommand):
    help = ("Let the AI dial the reminder and feedback calls that are due (what the periodic task does every "
            "minute). Only for hospitals that switched AI calls on in Settings, within their calling hours.")

    def handle(self, *args, **options):
        problem = outbound.missing_setup()
        if problem:
            self.stdout.write(self.style.WARNING(f"AI calls can't be placed: {problem}"))
        self.stdout.write(f"Placed {outbound.dispatch_due()} AI calls.")
