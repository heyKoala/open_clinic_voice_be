"""The hospital's knowledge base: documents in, searchable chunks, a spoken answer out, and the agent's tool."""
from __future__ import annotations

import io
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from ai_agent import knowledge
from ai_agent.models import KnowledgeChunk, KnowledgeDocument
from ai_agent.tool_views import rock8_path

pytestmark = pytest.mark.django_db

FEES = ("Consultation fees at Gunasheela Hospital.\n\nGeneral consultation: 500 rupees. Follow-up within 7 days: free. "
        "Fertility consultation with Dr. Devika Gunasheela: 1500 rupees.\n\nOPD timings: Monday to Saturday 9 am to 8 pm, "
        "Sunday closed. Parking is available in the basement; bring your previous reports and ID proof.")


def _model_says(text):
    response = mock.Mock(status_code=200)
    response.json.return_value = {"choices": [{"message": {"content": text}}]}
    response.raise_for_status = mock.Mock()
    return response


@pytest.fixture()
def model(settings):
    settings.SENTIMENT_API_KEY, settings.SENTIMENT_API_BASE, settings.SENTIMENT_MODEL = "k", "https://api.sarvam.ai/v1", "sarvam-105b"
    with mock.patch("ai_agent.knowledge.requests.post", return_value=_model_says("A general consultation is 500 rupees, and a follow-up within 7 days is free.")) as post:
        yield post


def _add_text(client, name=FEES[:20], text=FEES):
    return client.post("/api/v1/ai/knowledge/", {"type": "text", "name": name, "text": text}, format="json")


def test_pasted_text_is_chunked_and_searchable(admin_client, clinic):
    res = _add_text(admin_client, "Fees and timings")
    assert res.status_code == 201, res.data
    assert (res.data["status"], res.data["chunk_count"]) == ("ready", 1)
    assert res.data["name"] == "Fees and timings" and "text" in res.data

    hits = knowledge.search(clinic, "How much is a general consultation?")
    assert hits and "500 rupees" in hits[0].text
    assert knowledge.search(clinic, "swimming pool") == []


def test_long_text_splits_into_overlapping_chunks():
    text = "\n\n".join(f"Paragraph {i}. " + "Some sentence about the hospital. " * 6 for i in range(30))
    chunks = knowledge.split(text)
    assert len(chunks) > 3 and all(len(c) <= knowledge.CHUNK_CHARS + 2 for c in chunks)
    assert chunks[0][-30:] in text and chunks[1][:20] in text


def test_answer_is_written_from_the_extracts_only(admin_client, clinic, model):
    _add_text(admin_client, "Fees")
    result = knowledge.answer(clinic, "How much is a general consultation?")
    assert result["found"] and result["answer"].startswith("A general consultation is 500 rupees")
    assert result["sources"] == ["Fees"]
    sent = model.call_args.kwargs["json"]
    assert sent["reasoning_effort"] is None and "500 rupees" in sent["messages"][1]["content"]

    model.return_value = _model_says("NOT_FOUND")
    result = knowledge.answer(clinic, "Do you have a swimming pool for consultation?")
    assert not result["found"] and result["answer"] == knowledge.NOT_FOUND


def test_without_a_model_the_best_extract_is_returned(admin_client, clinic, settings):
    settings.SENTIMENT_API_KEY = ""
    _add_text(admin_client, "Fees")
    result = knowledge.answer(clinic, "parking")
    assert result["found"] and "Parking is available" in result["answer"]


def test_agent_tool_and_ask_endpoint(admin_client, api_client, clinic, model):
    tool_path = f"/api/v1/ai/tools/rock8/{rock8_path(clinic.id, 'knowledge')}"
    # No documents yet: the call config has no knowledge tool.
    config = api_client.post(f"/api/v1/ai/webhooks/rock8/{rock8_path(clinic.id)}", {"room_name": "r1"}, format="json").data
    assert "query_knowledge_base" not in [t["name"] for t in config["tools"]]

    _add_text(admin_client, "Fees and timings")
    config = api_client.post(f"/api/v1/ai/webhooks/rock8/{rock8_path(clinic.id)}", {"room_name": "r2"}, format="json").data
    tool = next(t for t in config["tools"] if t["name"] == "query_knowledge_base")
    assert "Fees and timings" in tool["description"] and tool["url"].endswith(tool_path)
    assert "KNOWLEDGE BASE" in config["system_prompt"]

    res = api_client.post(tool_path, {"user_query": "How much is a general consultation?"}, format="json")
    assert res.status_code == 200 and res.data["found"] and "500 rupees" in res.data["result"]
    assert api_client.post(f"/api/v1/ai/tools/rock8/{clinic.id}/wrong/knowledge/", {"user_query": "x"}, format="json").status_code == 403

    res = admin_client.post("/api/v1/ai/knowledge/ask/", {"question": "How much is a general consultation?"}, format="json")
    assert res.status_code == 200 and res.data["sources"] == ["Fees and timings"]


def test_file_upload_pdf_and_text(admin_client, clinic):
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, "Visiting hours are 4 pm to 7 pm every day. Only two visitors per patient.")
    pdf.save()
    res = admin_client.post("/api/v1/ai/knowledge/", {"type": "file", "file": SimpleUploadedFile("visiting.pdf", buffer.getvalue(), "application/pdf")}, format="multipart")
    assert res.status_code == 201, res.data
    assert (res.data["name"], res.data["status"]) == ("visiting.pdf", "ready") and res.data["file_name"].endswith(".pdf")
    assert knowledge.search(clinic, "visiting hours")[0].document.name == "visiting.pdf"

    res = admin_client.post("/api/v1/ai/knowledge/", {"type": "file", "file": SimpleUploadedFile("notes.txt", b"Ambulance number: 108.", "text/plain")}, format="multipart")
    assert res.status_code == 201 and res.data["chunk_count"] == 1
    bad = admin_client.post("/api/v1/ai/knowledge/", {"type": "file", "file": SimpleUploadedFile("x.exe", b"x")}, format="multipart")
    assert bad.status_code == 400 and "file" in bad.data


def test_web_page_is_fetched_and_can_be_refreshed(admin_client, clinic):
    page = mock.Mock(status_code=200, text="<html><head><script>x()</script></head><body><h1>Departments</h1><p>Cardiology on the 2nd floor.</p></body></html>")
    page.raise_for_status = mock.Mock()
    with mock.patch("ai_agent.knowledge.requests.get", return_value=page):
        res = admin_client.post("/api/v1/ai/knowledge/", {"type": "url", "url": "https://gunasheela.in/departments"}, format="json")
    assert res.status_code == 201 and res.data["status"] == "ready" and "x()" not in res.data["text"]
    assert knowledge.search(clinic, "cardiology floor")

    page.text = "<body><p>Cardiology moved to the 3rd floor.</p></body>"
    with mock.patch("ai_agent.knowledge.requests.get", return_value=page):
        res = admin_client.post(f"/api/v1/ai/knowledge/{res.data['id']}/reindex/")
    assert "3rd floor" in res.data["text"]
    assert "3rd floor" in knowledge.search(clinic, "cardiology")[0].text


def test_failed_document_is_reported_not_raised(admin_client, clinic):
    res = admin_client.post("/api/v1/ai/knowledge/", {"type": "file", "file": SimpleUploadedFile("broken.pdf", b"not a pdf", "application/pdf")}, format="multipart")
    assert res.status_code == 201 and res.data["status"] == "failed" and "PDF" in res.data["error"]
    assert knowledge.tool_spec(clinic, "u") is None  # failed documents don't count


def test_switching_off_and_deleting_stop_answers(admin_client, clinic, receptionist_client):
    doc_id = _add_text(admin_client, "Fees").data["id"]
    assert knowledge.search(clinic, "consultation")
    admin_client.patch(f"/api/v1/ai/knowledge/{doc_id}/", {"is_active": False}, format="json")
    assert knowledge.search(clinic, "consultation") == [] and knowledge.tool_spec(clinic, "u") is None
    admin_client.patch(f"/api/v1/ai/knowledge/{doc_id}/", {"is_active": True}, format="json")
    assert admin_client.delete(f"/api/v1/ai/knowledge/{doc_id}/").status_code == 204
    assert not KnowledgeChunk.objects.exists() and not KnowledgeDocument.objects.exists()
    assert receptionist_client.get("/api/v1/ai/knowledge/").status_code == 403
