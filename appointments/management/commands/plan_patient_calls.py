from django.core.management.base import BaseCommand

from appointments import journey


class Command(BaseCommand):
    help = "Plan reminder and feedback calls for existing appointments (what the periodic task does every 5 minutes)."

    def handle(self, *args, **options):
        self.stdout.write(f"Checked {journey.plan_all()} appointments for reminder and feedback calls.")
