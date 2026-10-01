from __future__ import annotations

import logging
import os

from celery import shared_task
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from common.audit import log_data_change

from .models import ReportAccessToken, ReportExecution
from .report_generator import ReportGenerator

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def generate_report_task(self, execution_id):
    """Asynchronously generate a report and update the execution record."""
    try:
        execution = ReportExecution.objects.select_related('template').get(id=execution_id)
        execution.status = ReportExecution.Status.PROCESSING
        execution.started_at = timezone.now()
        execution.save(update_fields=['status', 'started_at', 'updated_at'])
        log_data_change(execution, "update", request=None, metadata={"stage": "processing"})

        generator = ReportGenerator(execution)
        result_file_path = generator.generate()

        if result_file_path:
            file_size = os.path.getsize(result_file_path) if os.path.exists(result_file_path) else 0
            with open(result_file_path, 'rb') as f:
                django_file = File(f)
                filename = os.path.basename(result_file_path)
                execution.result_file.save(filename, django_file, save=False)

            execution.file_size = file_size
            execution.status = ReportExecution.Status.COMPLETED
            execution.completed_at = timezone.now()
        else:
            execution.status = ReportExecution.Status.FAILED
            execution.error_message = "Report generation failed - no output file"

        execution.save()
        log_data_change(execution, "update", request=None, metadata={"stage": execution.status})

        if result_file_path and os.path.exists(result_file_path):
            os.remove(result_file_path)

        return f"Report {execution_id} generated successfully"

    except ReportExecution.DoesNotExist:
        logger.error(f"ReportExecution {execution_id} does not exist")
        return f"ReportExecution {execution_id} not found"
    except Exception as e:
        logger.error(f"Error generating report {execution_id}: {str(e)}")
        try:
            execution = ReportExecution.objects.get(id=execution_id)
            execution.status = ReportExecution.Status.FAILED
            execution.error_message = str(e)
            execution.save(update_fields=['status', 'error_message', 'updated_at'])
            log_data_change(execution, "update", request=None, metadata={"stage": "failed"})
        except Exception:
            pass
        return f"Error generating report {execution_id}: {str(e)}"


def enqueue_report_generation(execution_id):
    """Queue report generation; mark the execution failed if the broker is unreachable."""
    try:
        generate_report_task.delay(execution_id)
    except Exception as exc:
        logger.exception("Could not queue report generation for execution %s", execution_id)
        ReportExecution.objects.filter(id=execution_id).update(
            status=ReportExecution.Status.FAILED,
            error_message="Report queue is unavailable. Please try again later.",
            updated_at=timezone.now(),
        )
        return exc
    return None


@shared_task
def cleanup_expired_reports():
    """Clean up expired report files, access tokens, and records."""
    now = timezone.now()
    ReportAccessToken.objects.filter(expires_at__lt=now).delete()

    expired_executions = ReportExecution.objects.filter(expires_at__lt=now)
    count = 0

    for execution in expired_executions:
        try:
            if execution.result_file:
                if default_storage.exists(execution.result_file.name):
                    default_storage.delete(execution.result_file.name)
            execution.delete()
            count += 1
        except Exception as e:
            logger.error(f"Error cleaning up report execution {execution.id}: {str(e)}")

    return f"Cleaned up {count} expired reports"