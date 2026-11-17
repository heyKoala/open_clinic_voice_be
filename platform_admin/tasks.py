from celery import shared_task

from platform_admin import charging


@shared_task
def settle_unbilled_calls():
    """Safety net for call charging: charge any finished call that was missed, every couple of minutes."""
    return f"Posted {charging.settle_unbilled()} credit entries for calls."
