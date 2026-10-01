from django.test import Client
from accounts.models import User
from rest_framework_simplejwt.tokens import RefreshToken
import json

u = User.objects.get(email="admin@manageopd.local")
refresh = RefreshToken.for_user(u)
access_token = str(refresh.access_token)
auth_header = f"Bearer {access_token}"

c = Client()
res = c.post("/api/v1/queue/1436/mark_seen/", HTTP_AUTHORIZATION=auth_header, HTTP_X_ACTIVE_CLINIC_ID="9")
print("STATUS:", res.status_code)
print("CONTENT:", res.content)
