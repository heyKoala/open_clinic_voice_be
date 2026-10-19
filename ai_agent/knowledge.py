"""The hospital's knowledge base: documents the AI receptionist can look things up in during a call.

How it works (modelled on the HeyKoala platform's knowledge base, without its vector store):
- ``index`` takes a KnowledgeDocument, extracts its text (PDF, Word, text, CSV, or a web page), splits
  it into overlapping chunks and stores them with a Postgres full-text search vector.
- On a call the agent has a ``query_knowledge_base`` tool (``tool_spec``). ``answer`` searches the
  hospital's chunks for the caller's question and has the chat model (the one already used for
  sentiment, SENTIMENT_*) write a short answer from those chunks only. Without that model the best
  chunks are returned as they are.
- Deleting a document deletes its chunks, so it stops answering at once.

Full-text search needs no extension and no embedding key; it works best in English and still matches
exact words in other languages. Embeddings can be added later behind the same ``search``.
"""
from __future__ import annotations

import io
import logging
import re

import requests
from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db import transaction

from ai_agent.models import KnowledgeChunk, KnowledgeDocument

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = ("pdf", "docx", "txt", "md", "csv")
CHUNK_CHARS = 800
CHUNK_OVERLAP = 120
MAX_TEXT_CHARS = 400_000     # ~100 pages; enough for a hospital's handbook
TOP_K = 5
SEARCH_CONFIG = "simple"     # no stemming: predictable across English and Indian-language text
FETCH_TIMEOUT = 20


class ExtractError(Exception):
    pass


# --- Text extraction ------------------------------------------------------------------------------

def extract_text(document: KnowledgeDocument) -> str:
    if document.type == KnowledgeDocument.Type.TEXT:
        text = document.text
    elif document.type == KnowledgeDocument.Type.URL:
        text = fetch_page(document.url)
    else:
        if not document.file:
            raise ExtractError("No file was uploaded.")
        with document.file.open("rb") as handle:
            text = extract_file(document.file.name, handle.read())
    text = clean(text)
    if not text:
        raise ExtractError("No readable text was found. Scanned PDFs (images only) can't be read yet.")
    return text[:MAX_TEXT_CHARS]


def extract_file(name: str, data: bytes) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext == "pdf":
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(data))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as exc:  # pypdf raises many types for damaged files
            raise ExtractError("The PDF could not be read. Is it damaged or password-protected?") from exc
    if ext == "docx":
        import docx2txt
        try:
            return docx2txt.process(io.BytesIO(data)) or ""
        except Exception as exc:
            raise ExtractError("The Word file could not be read.") from exc
    if ext in ("txt", "md", "csv"):
        for encoding in ("utf-8", "utf-16", "latin-1"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ExtractError("The text file's encoding isn't recognised.")
    raise ExtractError(f"Unsupported file type .{ext}. Use PDF, Word (.docx), text, Markdown or CSV.")


def fetch_page(url: str) -> str:
    from bs4 import BeautifulSoup
    try:
        response = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0 (hospital AI receptionist)"})
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ExtractError(f"The page could not be fetched ({exc.__class__.__name__}).") from exc
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "svg"]):
        tag.decompose()
    return soup.get_text("\n")


def clean(text: str) -> str:
    text = (text or "").replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


# --- Chunking and indexing ------------------------------------------------------------------------

def split(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Overlapping chunks, cut at a paragraph or sentence end where there is one nearby."""
    chunks, start, n = [], 0, len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if cut > size // 2:
                end = start + cut + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


@transaction.atomic
def index(document: KnowledgeDocument) -> KnowledgeDocument:
    """Extract, chunk and index one document; records failure on the document instead of raising."""
    try:
        text = extract_text(document)
    except ExtractError as exc:
        document.status, document.error, document.chunk_count = KnowledgeDocument.Status.FAILED, str(exc)[:500], 0
        document.chunks.all().delete()
        document.save(update_fields=["status", "error", "chunk_count", "updated_at"])
        return document
    except Exception:
        logger.exception("Could not index knowledge document %s", document.id)
        document.status, document.error, document.chunk_count = KnowledgeDocument.Status.FAILED, "Something went wrong while reading it.", 0
        document.chunks.all().delete()
        document.save(update_fields=["status", "error", "chunk_count", "updated_at"])
        return document

    document.chunks.all().delete()
    chunks = split(text)
    KnowledgeChunk.objects.bulk_create([
        KnowledgeChunk(document=document, position=i, text=chunk) for i, chunk in enumerate(chunks)])
    KnowledgeChunk.objects.filter(document=document).update(search=SearchVector("text", config=SEARCH_CONFIG))
    if document.type != KnowledgeDocument.Type.TEXT:
        document.text = text
    document.status, document.error, document.chunk_count = KnowledgeDocument.Status.READY, "", len(chunks)
    document.save(update_fields=["text", "status", "error", "chunk_count", "updated_at"])
    return document


# --- Answering ------------------------------------------------------------------------------------

def ready_documents(clinic):
    return KnowledgeDocument.objects.filter(
        clinic__in=clinic.root.group_clinics(), is_active=True, status=KnowledgeDocument.Status.READY)


def search(clinic, question: str, top_k: int = TOP_K) -> list[KnowledgeChunk]:
    """The chunks that best match the question, best first (all words, then any word)."""
    words = [w for w in re.findall(r"\w+", question or "") if len(w) > 1]
    if not words:
        return []
    chunks = KnowledgeChunk.objects.filter(document__in=ready_documents(clinic)).select_related("document")
    for search_type in ("plain", "websearch"):
        if search_type == "websearch":
            query = SearchQuery(" OR ".join(words), config=SEARCH_CONFIG, search_type="websearch")
        else:
            query = SearchQuery(" ".join(words), config=SEARCH_CONFIG, search_type="plain")
        hits = list(chunks.filter(search=query).annotate(rank=SearchRank("search", query)).order_by("-rank", "document_id", "position")[:top_k])
        if hits:
            return hits
    return []


SYNTHESIS_PROMPT = """You help a hospital's AI phone receptionist answer a caller's question using ONLY the extracts below from the hospital's own documents.
Rules:
- Use only the extracts. Never add facts of your own.
- If the question asks for a list (all doctors, all services, timings for each day), include every matching item in the extracts.
- If the extracts don't answer the question, reply exactly: NOT_FOUND
- Answer in 1-3 short spoken sentences, plain words, no headings, no bullet points, no mention of "extracts" or "documents".
- Answer in the language of the question."""

NOT_FOUND = "I don't have that information. The front desk can help with it."


def answer(clinic, question: str) -> dict:
    """{"answer", "found", "sources"}: a short spoken answer from the hospital's documents."""
    hits = search(clinic, question)
    if not hits:
        return {"answer": NOT_FOUND, "found": False, "sources": []}
    sources = sorted({h.document.name for h in hits})
    context = "\n\n---\n\n".join(f"[{h.document.name}]\n{h.text}" for h in hits)
    written = synthesize(question, context)
    if written is None:
        # No chat model: hand the agent the best extract and let it phrase the answer.
        return {"answer": hits[0].text[:600], "found": True, "sources": sources}
    if written.strip().upper().startswith("NOT_FOUND"):
        return {"answer": NOT_FOUND, "found": False, "sources": sources}
    return {"answer": written.strip(), "found": True, "sources": sources}


def synthesize(question: str, context: str) -> str | None:
    """A short answer from the chat model used for sentiment; None when it's not set up or fails."""
    key, base = getattr(settings, "SENTIMENT_API_KEY", ""), getattr(settings, "SENTIMENT_API_BASE", "")
    if not key or not base:
        return None
    body = {
        "model": settings.SENTIMENT_MODEL,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": SYNTHESIS_PROMPT},
            {"role": "user", "content": f"EXTRACTS:\n{context}\n\nQUESTION: {question}"},
        ],
    }
    if "sarvam.ai" in base:
        body["reasoning_effort"] = None  # see ai_agent.sentiment
    try:
        response = requests.post(f"{base.rstrip('/')}/chat/completions",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                 json=body, timeout=30)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError):
        logger.warning("Knowledge base answer could not be written", exc_info=True)
        return None


# --- The agent's tool -----------------------------------------------------------------------------

def tool_spec(clinic, url: str) -> dict | None:
    """The query_knowledge_base tool for a call, or None when the hospital has no documents."""
    names = list(ready_documents(clinic).order_by("name").values_list("name", flat=True)[:30])
    if not names:
        return None
    return {
        "name": "query_knowledge_base",
        "description": (
            f"Look up the hospital's own documents ({', '.join(names)}) to answer a caller's question about the "
            "hospital: services, fees, timings, doctors' specialities, directions, what to bring, policies. "
            "Use it whenever a question isn't about booking and you don't already know the answer. "
            "Read the result to the caller; if it says the information isn't available, say so and offer the front desk."
        ),
        "method": "POST",
        "url": url,
        "parameters": [
            {"name": "user_query", "type": "string", "location": "body", "required": True,
             "description": "The caller's question, in their words."},
        ],
    }


def prompt_note(clinic) -> str:
    """A line for the system prompt when the hospital has documents."""
    return ("\n\nKNOWLEDGE BASE: For questions about the hospital's services, fees, timings, doctors or policies, "
            "call query_knowledge_base before answering; don't guess.") if ready_documents(clinic).exists() else ""
