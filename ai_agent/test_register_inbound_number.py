"""register_inbound_number: the ids are never lost, and a stuck registration can be removed by trunk id.
The voice worker is mocked; nothing real is called."""
from io import StringIO
from unittest import mock

import pytest
from django.core.management import CommandError, call_command

pytestmark = pytest.mark.django_db


@pytest.fixture()
def worker(settings, clinic):
    settings.ROCK8_API_KEY = "rock8_test"
    settings.PUBLIC_BASE_URL = "https://clinic-api.example.com"
    settings.VOICE_WORKER_URL = "https://worker.example.com"
    settings.ROCK8_INBOUND_TRUNK_ID = settings.ROCK8_INBOUND_RULE_ID = ""
    return settings


def _response(status, payload=None, text=""):
    res = mock.Mock(status_code=status, text=text)
    res.json.return_value = payload or {}
    return res


def test_ids_are_printed_even_when_backups_is_read_only(worker, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = StringIO()
    with mock.patch("ai_agent.management.commands.register_inbound_number.requests.post",
                    return_value=_response(200, {"sip_trunk_id": "ST_new", "dispatch_rule_id": "SDR_new"})) as post, \
         mock.patch("ai_agent.management.commands.register_inbound_number.os.makedirs", side_effect=PermissionError("read-only")):
        call_command("register_inbound_number", "+918031150501", stdout=out)
    text = out.getvalue()
    assert "ROCK8_INBOUND_TRUNK_ID=ST_new" in text and "ROCK8_INBOUND_RULE_ID=SDR_new" in text
    assert "Could not save a copy" in text
    body = post.call_args.kwargs["json"]
    assert body["webhook_url"].startswith("https://clinic-api.example.com/api/v1/ai/webhooks/rock8/")


def test_remove_trunk_deletes_it_before_registering(worker, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with mock.patch("ai_agent.management.commands.register_inbound_number.requests.delete",
                    return_value=_response(200, text="{}")) as delete, \
         mock.patch("ai_agent.management.commands.register_inbound_number.requests.post",
                    return_value=_response(200, {"sip_trunk_id": "ST_new", "dispatch_rule_id": "SDR_new"})):
        call_command("register_inbound_number", "+918031150501", "--remove-trunk", "ST_stuck", stdout=StringIO())
    assert delete.call_args.args[0] == "https://worker.example.com/inbound/ST_stuck/SDR_unknown"


def test_conflict_names_the_trunk_to_remove(worker):
    conflict = ('{"detail":"Failed to configure inbound number: Conflicting inbound SIP Trunks: \\"<new>\\" and '
                '\\"ST_stuck\\", using the same number(s)"}')
    with mock.patch("ai_agent.management.commands.register_inbound_number.requests.post",
                    return_value=_response(500, text=conflict)):
        with pytest.raises(CommandError, match="--remove-trunk ST_stuck"):
            call_command("register_inbound_number", "+918031150501", stdout=StringIO())
