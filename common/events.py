from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
import logging

logger = logging.getLogger(__name__)

def broadcast_event(group_name: str, event_type: str, data: dict):
    """
    Broadcast a structured event to a specific channel group.
    
    Args:
        group_name (str): The logical group to broadcast to (e.g., 'doctor_5', 'clinic_1', 'user_12')
        event_type (str): The named event (e.g., 'appointment.updated', 'appointment.created')
        data (dict): The payload of the event
    """
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    payload = {
        "type": event_type,
        "data": data
    }
    
    try:
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                # The method name in consumers.py that handles this message
                "type": "broadcast_event",
                "payload": payload
            }
        )
    except Exception:
        # Real-time updates are best-effort; never fail the save that triggered them.
        logger.exception("Failed to broadcast %s to %s", event_type, group_name)
