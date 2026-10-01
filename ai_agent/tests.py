from __future__ import annotations

import pytest
from rest_framework import status

from ai_agent.models import AgentConfiguration
from audit.models import DataChangeEvent

pytestmark = pytest.mark.django_db


class TestAgentConfigurationIsolation:
    def test_admin_cannot_see_other_clinic_config(self, admin_client, clinic, clinic_b):
        config_b = AgentConfiguration.objects.create(
            clinic=clinic_b,
            purpose="Inbound calls",
            first_greeting="Hello from B"
        )
        
        res = admin_client.get(f"/api/v1/ai/agent-configurations/{config_b.id}/")
        assert res.status_code == status.HTTP_404_NOT_FOUND

        res_list = admin_client.get("/api/v1/ai/agent-configurations/")
        assert res_list.status_code == status.HTTP_200_OK
        assert len(res_list.data["results"]) == 0

    def test_soft_delete_and_audit(self, admin_client, clinic):
        config = AgentConfiguration.objects.create(
            clinic=clinic,
            purpose="Inbound calls",
            first_greeting="Hello from A"
        )
        
        res = admin_client.delete(f"/api/v1/ai/agent-configurations/{config.id}/")
        assert res.status_code == status.HTTP_204_NO_CONTENT
        
        config.refresh_from_db()
        assert not config.is_active
        
        audit = DataChangeEvent.objects.filter(object_id=config.id).first()
        assert audit is not None

class TestRock8Webhook:
    def test_missing_token(self, api_client, clinic):
        res = api_client.post(f"/api/v1/ai/webhooks/rock8/{clinic.id}/")
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert "Invalid or missing token" in res.data["detail"] or "Webhook secret" in res.data["detail"]

    def test_valid_token(self, api_client, settings, clinic):
        settings.ROCK8_WEBHOOK_SECRET = "test-secret"
        res = api_client.post(
            f"/api/v1/ai/webhooks/rock8/{clinic.id}/",
            HTTP_AUTHORIZATION="Bearer test-secret"
        )
        assert res.status_code == status.HTTP_200_OK
        assert "system_prompt" in res.data
        assert "tools" in res.data
        assert len(res.data["tools"]) == 5

    def test_invalid_token(self, api_client, settings, clinic):
        settings.ROCK8_WEBHOOK_SECRET = "test-secret"
        res = api_client.post(
            f"/api/v1/ai/webhooks/rock8/{clinic.id}/",
            HTTP_AUTHORIZATION="Bearer wrong-secret"
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN
