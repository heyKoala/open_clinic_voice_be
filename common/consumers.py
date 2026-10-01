import json
from channels.generic.websocket import AsyncWebsocketConsumer

class EventConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        
        # Check if user is authenticated
        if not user or not user.is_authenticated:
            await self.close()
            return

        self.groups_to_join = []
        
        # 1. User specific group
        self.groups_to_join.append(f"user_{user.id}")
        
        # 2. Clinic specific group
        if user.clinic_id:
            self.groups_to_join.append(f"clinic_{user.clinic_id}")
            
        # Join all groups
        for group_name in self.groups_to_join:
            await self.channel_layer.group_add(
                group_name,
                self.channel_name
            )

        await self.accept()
        
        # Send a connection success event
        await self.send(text_data=json.dumps({
            "type": "connection.established",
            "message": "Connected to real-time events."
        }))

    async def disconnect(self, close_code):
        # Leave all joined groups
        for group_name in getattr(self, 'groups_to_join', []):
            await self.channel_layer.group_discard(
                group_name,
                self.channel_name
            )

    # Receive message from room group
    async def broadcast_event(self, event):
        # Send message to WebSocket
        # event contains the structured payload dispatched from backend signals
        await self.send(text_data=json.dumps(event["payload"]))
