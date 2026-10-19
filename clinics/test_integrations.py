"""Integrations page: saving WATI and the clinic's doctor/appointment system, checking them, keeping keys secret."""
from __future__ import annotations

from unittest import mock

import pytest
from rest_framework import status

from clinics.models import ClinicIntegration

pytestmark = pytest.mark.django_db

WATI = {"api_endpoint": "https://live-mt-server.wati.io/123456", "whatsapp_number": "+91 80 1234 5678"}
TOKEN = "Bearer eyJhbGciOiJIUzI1NiJ9.wati-token-abcd"


def _response(status_code=200, body=None):
    response = mock.Mock(status_code=status_code)
    response.json.return_value = body if body is not None else {}
    return response


def test_lists_every_integration_unconnected(admin_client):
    res = admin_client.get("/api/v1/clinics/integrations/")
    assert res.status_code == 200
    assert [(i["provider"], i["status"], i["configured"]) for i in res.data] == [
        ("wati", "not_connected", False), ("pms", "not_connected", False), ("razorpay", "not_connected", False)]


def test_saving_validates_and_never_returns_the_key(admin_client, clinic):
    bad = admin_client.put("/api/v1/clinics/integrations/wati/", {"settings": {"api_endpoint": "live-mt-server.wati.io"}}, format="json")
    assert bad.status_code == 400 and set(bad.data) == {"api_endpoint", "whatsapp_number", "secret"}

    res = admin_client.put("/api/v1/clinics/integrations/wati/", {"settings": WATI, "secret": TOKEN}, format="json")
    assert res.status_code == 200
    assert (res.data["status"], res.data["has_secret"], res.data["secret_hint"]) == ("untested", True, "••••abcd")
    assert TOKEN not in str(res.data)
    record = ClinicIntegration.objects.get(clinic=clinic, provider="wati")
    assert "wati-token" not in record.secret_encrypted  # stored encrypted

    # Saving again without a key keeps the saved one.
    again = admin_client.put("/api/v1/clinics/integrations/wati/",
                             {"settings": {**WATI, "review_template": "google_review", "reminder_template": "old"}}, format="json")
    assert again.status_code == 200 and again.data["secret_hint"] == "••••abcd"
    assert again.data["settings"]["review_template"] == "google_review"
    assert "reminder_template" not in again.data["settings"]  # reminders are calls, not WhatsApp


def test_wati_webhook_records_patient_messages(admin_client, clinic, api_client):
    listed = {i["provider"]: i for i in admin_client.get("/api/v1/clinics/integrations/").data}
    url = listed["wati"]["webhook_url"]
    path = url[url.index("/api/v1/"):]
    assert path.startswith(f"/api/v1/clinics/wati/webhook/{clinic.id}/")

    message = {"eventType": "message", "owner": False, "waId": "919800000000", "text": "I want to book"}
    assert api_client.post(path, message, format="json").status_code == 404  # not connected yet
    admin_client.put("/api/v1/clinics/integrations/wati/", {"settings": WATI, "secret": TOKEN}, format="json")

    assert api_client.post(f"/api/v1/clinics/wati/webhook/{clinic.id}/wrong-token/", message, format="json").status_code == 404
    api_client.post(path, {**message, "owner": True}, format="json")  # the clinic's own message: not counted
    assert ClinicIntegration.objects.get(clinic=clinic, provider="wati").last_message_at is None

    assert api_client.post(path, message, format="json").status_code == 200
    assert ClinicIntegration.objects.get(clinic=clinic, provider="wati").last_message_at is not None
    assert admin_client.get("/api/v1/clinics/integrations/").data[0]["last_message_at"] is not None


def test_wati_check_lists_approved_templates_and_records_the_result(admin_client, clinic):
    admin_client.put("/api/v1/clinics/integrations/wati/", {"settings": WATI, "secret": TOKEN}, format="json")
    templates = {"messageTemplates": [
        {"elementName": "booking_confirmation", "status": "APPROVED"},
        {"elementName": "visit_reminder", "status": "APPROVED"},
        {"elementName": "draft_one", "status": "PENDING"},
    ]}
    with mock.patch("clinics.integrations.requests.get", return_value=_response(200, templates)) as get:
        res = admin_client.post("/api/v1/clinics/integrations/wati/test/", {}, format="json")
    assert res.data["ok"] and res.data["templates"] == ["booking_confirmation", "visit_reminder"]
    assert get.call_args.args[0] == "https://live-mt-server.wati.io/123456/api/v1/getMessageTemplates"
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer eyJhbGciOiJIUzI1NiJ9.wati-token-abcd"
    assert ClinicIntegration.objects.get(clinic=clinic, provider="wati").status == "connected"

    with mock.patch("clinics.integrations.requests.get", return_value=_response(401)):
        res = admin_client.post("/api/v1/clinics/integrations/wati/test/", {}, format="json")
    record = ClinicIntegration.objects.get(clinic=clinic, provider="wati")
    assert not res.data["ok"] and "rejected the access token" in res.data["message"]
    assert (record.status, record.last_error) == ("error", res.data["message"])


def test_checking_a_draft_saves_nothing(admin_client, clinic):
    with mock.patch("clinics.integrations.requests.get", return_value=_response(200, {"messageTemplates": []})):
        res = admin_client.post("/api/v1/clinics/integrations/wati/test/", {"settings": WATI, "secret": TOKEN}, format="json")
    assert res.data["ok"] and res.data["integration"] is None
    assert not ClinicIntegration.objects.exists()


def test_pms_sends_the_key_the_way_it_is_told(admin_client):
    settings = {"system_name": "KIMS HIS", "base_url": "https://his.example.com", "auth_type": "header",
                "header_name": "X-API-Key", "doctors_path": "/api/doctors"}
    missing = admin_client.put("/api/v1/clinics/integrations/pms/", {"settings": {**settings, "header_name": ""}, "secret": "k"}, format="json")
    assert missing.status_code == 400 and "header_name" in missing.data
    admin_client.put("/api/v1/clinics/integrations/pms/", {"settings": settings, "secret": "pms-key-1234"}, format="json")
    with mock.patch("clinics.integrations.requests.get", return_value=_response(200, [])) as get:
        res = admin_client.post("/api/v1/clinics/integrations/pms/test/", {}, format="json")
    assert res.data["ok"]
    assert get.call_args.args[0] == "https://his.example.com/api/doctors"
    assert get.call_args.kwargs["headers"]["X-API-Key"] == "pms-key-1234"


RAZORPAY = {"key_id": "rzp_test_AbCdEf123456", "consultation_fee": "500"}


def test_razorpay_validates_and_checks_the_keys(admin_client):
    bad = admin_client.put("/api/v1/clinics/integrations/razorpay/",
                           {"settings": {"key_id": "key_123", "consultation_fee": "0", "link_valid_hours": "abc"}, "secret": "s"},
                           format="json")
    assert bad.status_code == 400 and set(bad.data) == {"key_id", "consultation_fee", "link_valid_hours"}

    admin_client.put("/api/v1/clinics/integrations/razorpay/", {"settings": RAZORPAY, "secret": "rzp-secret-9876"}, format="json")
    with mock.patch("clinics.integrations.requests.get", return_value=_response(200, {"payment_links": []})) as get:
        res = admin_client.post("/api/v1/clinics/integrations/razorpay/test/", {}, format="json")
    assert res.data["ok"] and "test mode" in res.data["message"]
    assert get.call_args.kwargs["auth"] == ("rzp_test_AbCdEf123456", "rzp-secret-9876")

    with mock.patch("clinics.integrations.requests.get", return_value=_response(401)):
        res = admin_client.post("/api/v1/clinics/integrations/razorpay/test/", {}, format="json")
    assert not res.data["ok"] and "rejected the keys" in res.data["message"]


def test_razorpay_webhook_needs_the_signature(admin_client, clinic, api_client):
    import hashlib, hmac, json

    info = {i["provider"]: i for i in admin_client.get("/api/v1/clinics/integrations/").data}["razorpay"]
    path = info["webhook_url"][info["webhook_url"].index("/api/v1/"):]
    body = json.dumps({"event": "payment_link.paid", "payload": {}}).encode()
    signed = hmac.new(info["webhook_secret"].encode(), body, hashlib.sha256).hexdigest()
    post = lambda signature: api_client.generic("POST", path, body, content_type="application/json",
                                                HTTP_X_RAZORPAY_SIGNATURE=signature)

    assert post(signed).status_code == 404  # not connected yet
    admin_client.put("/api/v1/clinics/integrations/razorpay/", {"settings": RAZORPAY, "secret": "rzp-secret-9876"}, format="json")
    assert post("forged").status_code == 400
    assert post(signed).status_code == 200
    assert ClinicIntegration.objects.get(clinic=clinic, provider="razorpay").last_message_at is not None


def test_disconnect_and_access(admin_client, receptionist_client, clinic):
    admin_client.put("/api/v1/clinics/integrations/wati/", {"settings": WATI, "secret": TOKEN}, format="json")
    assert admin_client.delete("/api/v1/clinics/integrations/wati/").status_code == status.HTTP_204_NO_CONTENT
    assert not ClinicIntegration.objects.exists()
    assert receptionist_client.get("/api/v1/clinics/integrations/").status_code == status.HTTP_403_FORBIDDEN
    assert admin_client.get("/api/v1/clinics/integrations/unknown/").status_code in (404, 405)
