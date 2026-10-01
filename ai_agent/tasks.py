import logging

import requests
from celery import shared_task

from ai_agent.call_events import fetch_media

logger = logging.getLogger(__name__)


@shared_task(
    autoretry_for=(requests.RequestException,),
    retry_backoff=10,
    retry_kwargs={"max_retries": 4},
)
def fetch_call_media(call_log_id, transcript_url=None, recording_url=None):
    """Download a call's transcript and recording from the provider's signed (expiring) links."""
    fetch_media(call_log_id, transcript_url, recording_url)
    return f"Fetched media for CallLog {call_log_id}"


@shared_task(bind=True, max_retries=20)
def fetch_call_recording(self, call_log_id, room_name, caller=""):
    """Poll the provider until the call's recording/transcript exist, then store them.

    Runs a few minutes after the call starts and retries every 2 minutes (~40 minutes in total).
    """
    from ai_agent.call_sessions import finish_call_log, recording_links

    try:
        links = recording_links(room_name)
    except requests.RequestException as exc:
        raise self.retry(exc=exc, countdown=120)
    transcript_url, recording_url = links.get("transcript_url"), links.get("recording_url")
    if not transcript_url and self.request.retries < self.max_retries:
        raise self.retry(countdown=120)  # call still in progress, or files not uploaded yet
    if transcript_url or recording_url:
        fetch_media(call_log_id, transcript_url, recording_url)
    finish_call_log(call_log_id, caller, got_media=bool(transcript_url or recording_url))
    return f"CallLog {call_log_id}: transcript={'yes' if transcript_url else 'no'}, recording={'yes' if recording_url else 'no'}"
