import json
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from doctors.models import Doctor

class QueueConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")

        # Check if user is authenticated
        if not user or not user.is_authenticated:
            await self.close()
            return

        self.doctor_id = self.scope["url_route"]["kwargs"]["doctor_id"]

        # Authorization: doctors may only subscribe to their own queue; admins and
        # receptionists to any doctor's queue within their active clinic.
        if user.role == "doctor":
            allowed = await self._is_own_doctor_profile(user)
        elif user.role in ("clinic_admin", "receptionist"):
            allowed = await self._doctor_in_clinic(user.clinic_id)
        else:
            allowed = False
        if not allowed:
            await self.close()
            return

        self.group_name = f"doctor_queue_{self.doctor_id}"

        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )
        await self.accept()

        # Send a connection success event
        await self.send(text_data=json.dumps({
            "type": "connection.established",
            "message": "Connected to queue updates."
        }))

    @database_sync_to_async
    def _is_own_doctor_profile(self, user):
        return Doctor.objects.filter(user_id=user.id, id=self.doctor_id, clinic_id=user.clinic_id).exists()

    @database_sync_to_async
    def _doctor_in_clinic(self, clinic_id):
        return clinic_id is not None and Doctor.objects.filter(id=self.doctor_id, clinic_id=clinic_id).exists()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name
            )

    async def queue_message(self, event):
        """
        Handler for messages broadcasted to the group.
        Expected event format: { "type": "queue.message", "event": "queue.updated", "payload": { ... } }
        """
        await self.send(text_data=json.dumps({
            "type": event.get("event", "queue.updated"),
            "payload": event.get("payload", {})
        }))
