import logging
from celery import shared_task
from django.db import transaction
from patients.models import MessageLog
from patients.whatsapp import WhatsAppService

logger = logging.getLogger(__name__)

# Keeps one run well inside CELERY_TASK_TIME_LIMIT (each send may wait up to 10s).
BATCH_SIZE = 20


def _pending_whatsapp():
    return MessageLog.objects.filter(
        method=MessageLog.Method.WHATSAPP,
        status=MessageLog.Status.PENDING,
        is_sent=False
    )


@shared_task
def process_pending_messages():
    """
    Finds all pending WhatsApp messages in MessageLog and sends them.
    Scheduled every minute by celery beat (see config/celery.py).
    """
    pending_ids = list(_pending_whatsapp().order_by("created_at").values_list("id", flat=True)[:BATCH_SIZE])
    if not pending_ids:
        return "No pending WhatsApp messages."

    whatsapp_service = WhatsAppService()
    if not whatsapp_service.is_configured():
        logger.warning("WhatsApp is not configured; pending messages will be marked as mocked, not delivered.")

    processed = 0
    for log_id in pending_ids:
        # Lock the row for the duration of the send so overlapping runs or other
        # workers skip it instead of sending the same message twice.
        with transaction.atomic():
            log = _pending_whatsapp().select_for_update(skip_locked=True).select_related("patient").filter(id=log_id).first()
            if log is None:
                continue

            template_name = log.metadata.get('template_name', 'manageopd_reschedule_alert')
            template_args = log.metadata.get('template_args', [])

            # Meta requires templates for business-initiated messages outside the 24h window.
            try:
                response = whatsapp_service.send_template_message(
                    to_phone=log.patient.phone,
                    template_name=template_name,
                    variables=template_args
                )
            except Exception as exc:
                logger.exception("Unexpected error sending MessageLog %s", log.id)
                response = {"status": "error", "error": str(exc)}

            if response['status'] in ['sent', 'mocked']:
                log.is_sent = True
                log.status = MessageLog.Status.SENT
                log.metadata['message_id'] = response.get('message_id')
                if response['status'] == 'mocked':
                    log.metadata['mocked'] = True
                if response.get('raw_response'):
                    log.metadata['raw_response'] = response.get('raw_response')
            else:
                log.status = MessageLog.Status.FAILED
                log.metadata['error'] = response.get('error')

            log.save(update_fields=['is_sent', 'status', 'metadata', 'updated_at'])
            processed += 1
            logger.info("Processed MessageLog %s: %s", log.id, log.status)

    return f"Processed {processed} WhatsApp messages."
