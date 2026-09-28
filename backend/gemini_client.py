"""
Minimal Google Gemini client (REST, v1beta generateContent).

- The API key is read from GEMINI_API_KEY and sent only in the
  ``x-goog-api-key`` header — never in the URL, logs or error messages.
- Only the configured model is used (default ``gemini-2.5-pro``). Transient
  failures (429 / 5xx / network) are retried with exponential backoff; when the
  retries are exhausted a GeminiUnavailable error is raised. There is no silent
  fallback to another model and no placeholder output.
"""

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from config import get_settings, redact

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Gemini counts "thinking" tokens against maxOutputTokens. Pro models can think for
# thousands of tokens, so this is added on top of what the caller needs for the answer.
# Only generated tokens are billed, so the higher cap costs nothing unless used.
THINKING_TOKENS = 16384

# Indirection so tests can skip real sleeping.
_sleep = time.sleep


class GeminiError(RuntimeError):
    """Base class for Gemini failures. Messages are safe to show to users."""


class GeminiNotConfigured(GeminiError):
    pass


class GeminiUnavailable(GeminiError):
    pass


class GeminiQuotaExceeded(GeminiUnavailable):
    """The API key's quota (e.g. the free tier's daily request cap) is used up."""


# Circuit breaker: after a quota or availability failure, later calls fail fast
# for a while instead of each spending minutes on retries (matters for repo runs).
_breaker: dict = {"until": 0.0, "error": None}
QUOTA_COOLDOWN = 900
UNAVAILABLE_COOLDOWN = 60


def reset_breaker() -> None:
    _breaker.update(until=0.0, error=None)


def _trip(error: GeminiError, seconds: float) -> GeminiError:
    _breaker.update(until=time.monotonic() + seconds, error=error)
    return error


def _quota_details(resp: requests.Response) -> tuple[bool, bool, float]:
    """Return (no_quota, is_daily_quota, retry_delay_seconds) from a 429 response body.

    no_quota means the key has no quota at all for the model ("limit: 0"), e.g. a
    paid-only model on a free-tier project; waiting will not help.
    """
    try:
        error = resp.json().get("error", {})
    except ValueError:
        return False, False, 0.0
    no_quota = re.search(r"\blimit: 0\b", str(error.get("message", ""))) is not None
    details = error.get("details", [])
    daily, delay = False, 0.0
    for d in details:
        kind = d.get("@type", "")
        if kind.endswith("QuotaFailure"):
            daily = any("PerDay" in v.get("quotaId", "") for v in d.get("violations", []))
        elif kind.endswith("RetryInfo"):
            try:
                delay = float(str(d.get("retryDelay", "0")).rstrip("s"))
            except ValueError:
                delay = 0.0
    return no_quota, daily, delay


@dataclass
class GeminiResult:
    text: str
    model: str
    finish_reason: str = ""


def _extract_text(payload: dict) -> tuple[str, str]:
    candidates = payload.get("candidates") or []
    if not candidates:
        block = (payload.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise GeminiError(f"Gemini refused the request (blockReason={block}).")
        raise GeminiError("Gemini returned no candidates.")
    cand = candidates[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    return text, cand.get("finishReason", "")


def generate(
    system: str,
    prompt: str,
    *,
    json_mode: bool = False,
    temperature: float = 0.2,
    max_output_tokens: int = 16384,
    timeout: int = 180,
) -> GeminiResult:
    settings = get_settings()
    if not settings.gemini_api_key:
        raise GeminiNotConfigured(
            "GEMINI_API_KEY is not set on the server, so the AI review step was skipped."
        )

    model = settings.gemini_model
    url = f"{API_ROOT}/models/{model}:generateContent"
    generation_config: dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens + THINKING_TOKENS,
    }
    if json_mode:
        generation_config["responseMimeType"] = "application/json"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }
    headers = {"x-goog-api-key": settings.gemini_api_key, "Content-Type": "application/json"}

    if time.monotonic() < _breaker["until"] and _breaker["error"] is not None:
        raise _breaker["error"]

    attempts = settings.gemini_max_retries + 1
    last_error = ""
    for attempt in range(attempts):
        delay = min(30.0, 2 ** (attempt + 1)) + random.uniform(0, 1)
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as exc:
            last_error = f"network error: {type(exc).__name__}"
        else:
            if resp.status_code == 200:
                text, finish = _extract_text(resp.json())
                if not text.strip():
                    if finish == "MAX_TOKENS":
                        raise GeminiError(f"{model} used its whole output token limit before answering.")
                    raise GeminiError(f"{model} returned an empty response (finishReason={finish or 'unknown'}).")
                return GeminiResult(text=text, model=model, finish_reason=finish)
            message = _error_message(resp)
            if resp.status_code == 429:
                no_quota, daily, retry_after = _quota_details(resp)
                if no_quota:
                    raise _trip(GeminiQuotaExceeded(
                        f"{model} has no free-tier quota on this API key (Google reports a limit of 0). "
                        "Enable billing for the project in Google AI Studio, or set GEMINI_MODEL to a model "
                        "the key can use."
                    ), QUOTA_COOLDOWN)
                if daily:
                    raise _trip(GeminiQuotaExceeded(
                        f"The daily request quota for {model} on this API key is used up (Google free tier). "
                        "Enable billing for the Google AI project or wait for the quota to reset."
                    ), QUOTA_COOLDOWN)
                if retry_after:
                    delay = min(60.0, retry_after + 1)
            elif resp.status_code == 404:
                raise GeminiError(redact(f"{model} is not available to this API key (HTTP 404: {message})"))
            elif resp.status_code not in RETRYABLE_STATUS:
                raise GeminiError(redact(f"{model} request failed with HTTP {resp.status_code}: {message}"))
            last_error = f"HTTP {resp.status_code}: {message.splitlines()[0] if message else ''}"
        if attempt < attempts - 1:
            _sleep(delay)

    raise _trip(GeminiUnavailable(
        redact(f"{model} is unavailable after {attempts} attempt(s) ({last_error}). Try again shortly.")
    ), UNAVAILABLE_COOLDOWN)


def _error_message(resp: requests.Response) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", ""))[:300]
    except ValueError:
        return resp.text[:300]


def parse_json(text: str) -> Any:
    """Parse model output as JSON, tolerating markdown fences or leading prose."""
    cleaned = strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0), default=-1)
    if start >= 0:
        end = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise GeminiError("Gemini returned malformed JSON.")


def generate_json(system: str, prompt: str, **kwargs) -> tuple[Any, str]:
    result = generate(system, prompt, json_mode=True, **kwargs)
    try:
        return parse_json(result.text), result.model
    except GeminiError:
        if result.finish_reason == "MAX_TOKENS":
            raise GeminiError(f"{result.model}'s answer was cut off at the output token limit.") from None
        raise


def strip_fences(text: str) -> str:
    """Remove a single leading/trailing ``` fence (with optional language tag)."""
    text = text.strip()
    text = re.sub(r"^```[\w+-]*[ \t]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()
