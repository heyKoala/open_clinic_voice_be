"""How the caller felt on a call: positive, neutral or negative.

Calls whose provider sends a call analysis carry a sentiment already (see call_events). For the others (the
clinic number and the in-app Test call, where only the transcript arrives), an OpenAI-compatible chat model
rates the transcript, the way the HeyKoala platform does. It is optional: with no SENTIMENT_API_KEY the
sentiment stays blank. The same model also writes a short summary for calls whose provider sent none
(summarize_transcript); a summary the provider did send is left alone, and so is the transcript.
"""
from __future__ import annotations

import json
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

VALUES = ("positive", "neutral", "negative")
MAX_TRANSCRIPT_CHARS = 12000

PROMPT = """You read the transcript of a phone call between a clinic's AI receptionist ("AI") and a caller ("Patient").
Rate the caller's overall sentiment over the call: positive, neutral or negative. Judge the caller only, not the AI.
Respond ONLY with JSON: {"sentiment": "positive" | "neutral" | "negative"}"""


def normalize_sentiment(value) -> str:
    """'Positive' / ' NEGATIVE ' -> 'positive' / 'negative'; anything else -> ''."""
    text = str(value or "").strip().lower()
    return text if text in VALUES else ""


def is_enabled() -> bool:
    return bool(getattr(settings, "SENTIMENT_API_KEY", ""))


SUMMARY_PROMPT = """You read the transcript of a phone call between a hospital's AI receptionist ("AI") and a caller ("Patient").
Write a summary for the front desk in 2 to 3 plain sentences: who called and why, what was booked, cancelled or
rescheduled (doctor, date, time), what was asked, and anything staff must follow up. No preamble, no headings."""
MAX_SUMMARY_CHARS = 600


def _chat(system_prompt: str, user_content: str, timeout: int = 30) -> str:
    """One chat completion; raises on any failure."""
    base = settings.SENTIMENT_API_BASE.rstrip("/")
    body = {"model": settings.SENTIMENT_MODEL, "temperature": 0,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]}
    if "sarvam.ai" in base:
        body["reasoning_effort"] = None
    response = requests.post(f"{base}/chat/completions",
                             headers={"Authorization": f"Bearer {settings.SENTIMENT_API_KEY}", "Content-Type": "application/json"},
                             json=body, timeout=timeout)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


CLASSIFY_PROMPT = """You read the transcript of a phone call between a hospital's AI receptionist ("AI") and a caller ("Patient").
Nothing was booked, cancelled or rescheduled on this call. Classify what the caller wanted:
- "information": they asked about doctors, timings, fees, services, directions, reports or anything else and got an answer
- "callback": they asked to speak to a person, or had a request the AI could not complete and staff must follow up
- "nothing": no real conversation (silence, a wrong number, hung up at the greeting, only greetings)
Respond ONLY with JSON: {"outcome": "information" | "callback" | "nothing"}"""
CLASSIFIED_OUTCOMES = {"information": "information_only", "callback": "needs_callback", "nothing": ""}


def classify_transcript(transcript: str) -> str | None:
    """'information_only', 'needs_callback' or '' (nothing to tag) from the transcript; None when the model
    is off or fails, so the caller can leave the outcome alone."""
    if not is_enabled() or not (transcript or "").strip():
        return None
    try:
        content = _chat(CLASSIFY_PROMPT, transcript[-MAX_TRANSCRIPT_CHARS:])
        start, end = content.find("{"), content.rfind("}")
        value = json.loads(content[start:end + 1]).get("outcome") if start >= 0 and end > start else content
        key = str(value or "").strip().strip('"').lower()
        return CLASSIFIED_OUTCOMES.get(key)
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError, AttributeError):
        logger.warning("Could not classify call", exc_info=True)
        return None


def summarize_transcript(transcript: str) -> str:
    """A short summary of the call for the front desk, or '' when off, empty, or the model fails."""
    if not is_enabled() or not (transcript or "").strip():
        return ""
    try:
        text = _chat(SUMMARY_PROMPT, transcript[-MAX_TRANSCRIPT_CHARS:]).strip()
        text = text.strip('"').strip()
        return text[:MAX_SUMMARY_CHARS]
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError, AttributeError):
        logger.warning("Could not summarise call", exc_info=True)
        return ""


def rate_transcript(transcript: str) -> str:
    """The caller's sentiment for this transcript, or '' when off, the transcript is empty, or the model fails."""
    if not is_enabled() or not (transcript or "").strip():
        return ""
    base = settings.SENTIMENT_API_BASE.rstrip("/")
    body = {
        "model": settings.SENTIMENT_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": transcript[-MAX_TRANSCRIPT_CHARS:]},
        ],
    }
    if "sarvam.ai" in base:
        # Sarvam's models think before answering by default; a one-word rating doesn't need it (faster,
        # fewer tokens). Only for Sarvam: OpenAI rejects reasoning_effort on non-reasoning models.
        body["reasoning_effort"] = None
    try:
        response = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {settings.SENTIMENT_API_KEY}", "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        # Models sometimes wrap the JSON in a code fence.
        content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return normalize_sentiment(json.loads(content).get("sentiment"))
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError, AttributeError):
        logger.warning("Could not rate call sentiment", exc_info=True)
        return ""
