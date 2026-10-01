from django.core.management.base import BaseCommand
from django.utils import timezone
from django.core.files.storage import default_storage
from reports.models import ReportAccessToken, ReportExecution
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Clean up expired report files and records'

    def handle(self, *args, **options):
        self.stdout.write(
            self.style.SUCCESS('Starting cleanup of expired reports')
        )

        now = timezone.now()
        ReportAccessToken.objects.filter(expires_at__lt=now).delete()
        expired_executions = ReportExecution.objects.filter(expires_at__lt=now)
        count = 0

        for execution in expired_executions:
            try:
                # Delete the file if it exists
                if execution.result_file:
                    if default_storage.exists(execution.result_file.name):
                        default_storage.delete(execution.result_file.name)
                        logger.info(f"Deleted file for expired report execution {execution.id}")
                # Delete the record
                execution.delete()
                count += 1
            except Exception as e:
                logger.error(f"Error cleaning up report execution {execution.id}: {str(e)}")
                self.stdout.write(
                    self.style.ERROR(f'Error cleaning up report execution {execution.id}: {str(e)}')
                )

        self.stdout.write(
            self.style.SUCCESS(f'Successfully cleaned up {count} expired reports')
        )