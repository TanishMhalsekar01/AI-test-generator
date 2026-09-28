"""Tests for gemini_client: request shape, strict model, retries, key hygiene."""

import pytest

import gemini_client
from conftest import gemini_response


def test_request_uses_configured_model_and_header_key(mock_gemini):
    calls = mock_gemini('{"ok": true}')
    data, model = gemini_client.generate_json("sys", "prompt")
    assert data == {"ok": True}
    assert model == "gemini-3.1-pro-preview"
    call = calls[0]
    assert call["url"].endswith("/models/gemini-3.1-pro-preview:generateContent")
    assert call["headers"]["x-goog-api-key"] == "test-gemini-key-000000"
    assert "key=" not in call["url"]  # never in the URL
    assert call["json"]["generationConfig"]["responseMimeType"] == "application/json"
    assert call["json"]["systemInstruction"]["parts"][0]["text"] == "sys"
    # Thinking tokens count against maxOutputTokens, so the answer budget gets headroom on top.
    assert call["json"]["generationConfig"]["maxOutputTokens"] == 16384 + gemini_client.THINKING_TOKENS


def test_retries_on_503_then_succeeds(mock_gemini):
    calls = mock_gemini(gemini_response("", 503), gemini_response("", 503), '{"ok": 1}')
    data, _ = gemini_client.generate_json("s", "p")
    assert data == {"ok": 1}
    assert len(calls) == 3


def test_gives_up_after_retries_without_switching_model(mock_gemini, monkeypatch):
    monkeypatch.setenv("GEMINI_MAX_RETRIES", "2")
    calls = mock_gemini(gemini_response("", 503))
    with pytest.raises(gemini_client.GeminiUnavailable) as exc:
        gemini_client.generate("s", "p")
    assert len(calls) == 3
    assert all("gemini-3.1-pro-preview" in c["url"] for c in calls)
    assert "gemini-3.1-pro-preview is unavailable" in str(exc.value)


def test_non_retryable_error_raises_immediately(mock_gemini):
    bad = gemini_response("", 400)
    calls = mock_gemini(bad)
    with pytest.raises(gemini_client.GeminiError, match="HTTP 400"):
        gemini_client.generate("s", "p")
    assert len(calls) == 1


def test_missing_key_raises_not_configured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(gemini_client.GeminiNotConfigured):
        gemini_client.generate("s", "p")


def test_key_is_redacted_from_errors(mock_gemini):
    resp = gemini_response("", 400)
    resp.json.return_value = {"error": {"message": "bad key test-gemini-key-000000"}}
    mock_gemini(resp)
    with pytest.raises(gemini_client.GeminiError) as exc:
        gemini_client.generate("s", "p")
    assert "test-gemini-key-000000" not in str(exc.value)
    assert "***" in str(exc.value)


def test_thought_parts_are_ignored(monkeypatch):
    resp = gemini_response("")
    resp.json.return_value = {"candidates": [{"content": {"parts": [
        {"text": "thinking...", "thought": True}, {"text": "answer"}]}}]}
    monkeypatch.setattr(gemini_client.requests, "post", lambda *a, **k: resp)
    assert gemini_client.generate("s", "p").text == "answer"


@pytest.mark.parametrize("raw", ['```json\n{"a": 1}\n```', 'Here you go: {"a": 1} thanks', '{"a": 1}'])
def test_parse_json_tolerates_wrapping(raw):
    assert gemini_client.parse_json(raw) == {"a": 1}


def test_parse_json_rejects_garbage():
    with pytest.raises(gemini_client.GeminiError):
        gemini_client.parse_json("not json at all")


def _quota_429(daily: bool, retry: str = "7s"):
    resp = gemini_response("", 429)
    resp.json.return_value = {"error": {"code": 429, "message": "You exceeded your current quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if daily else "GenerateRequestsPerMinutePerProjectPerModel"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry},
    ]}}
    return resp


def test_daily_quota_fails_fast_and_trips_breaker(mock_gemini):
    calls = mock_gemini(_quota_429(daily=True))
    with pytest.raises(gemini_client.GeminiQuotaExceeded, match="daily request quota"):
        gemini_client.generate("s", "p")
    assert len(calls) == 1  # no pointless retries
    with pytest.raises(gemini_client.GeminiQuotaExceeded):
        gemini_client.generate("s", "p")
    assert len(calls) == 1  # breaker: second call never hits the API


def test_per_minute_limit_waits_retry_delay(mock_gemini, monkeypatch):
    slept = []
    monkeypatch.setattr(gemini_client, "_sleep", slept.append)
    mock_gemini(_quota_429(daily=False, retry="7s"), '{"ok": true}')
    data, _ = gemini_client.generate_json("s", "p")
    assert data == {"ok": True}
    assert slept == [8.0]


def test_unavailable_trips_short_breaker(mock_gemini, monkeypatch):
    monkeypatch.setenv("GEMINI_MAX_RETRIES", "0")
    calls = mock_gemini(gemini_response("", 503))
    with pytest.raises(gemini_client.GeminiUnavailable):
        gemini_client.generate("s", "p")
    with pytest.raises(gemini_client.GeminiUnavailable):
        gemini_client.generate("s", "p")
    assert len(calls) == 1


def test_model_without_free_tier_quota_explains_billing(mock_gemini):
    # A paid-only model on a free-tier key: Google reports "limit: 0" for the free-tier metrics.
    resp = _quota_429(daily=True)
    resp.json.return_value["error"]["message"] = (
        "You exceeded your current quota. Quota exceeded for metric: "
        "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 0, model: x")
    calls = mock_gemini(resp)
    with pytest.raises(gemini_client.GeminiQuotaExceeded, match="no free-tier quota.*Enable billing"):
        gemini_client.generate("s", "p")
    assert len(calls) == 1


def test_daily_limit_of_20_is_not_mistaken_for_no_quota(mock_gemini):
    resp = _quota_429(daily=True)
    resp.json.return_value["error"]["message"] = "Quota exceeded for metric: x, limit: 20, model: y"
    mock_gemini(resp)
    with pytest.raises(gemini_client.GeminiQuotaExceeded, match="daily request quota"):
        gemini_client.generate("s", "p")


def test_thinking_that_uses_the_whole_budget_is_reported(monkeypatch):
    resp = gemini_response("")
    resp.json.return_value = {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}
    monkeypatch.setattr(gemini_client.requests, "post", lambda *a, **k: resp)
    with pytest.raises(gemini_client.GeminiError, match="whole output token limit"):
        gemini_client.generate("s", "p")


def test_truncated_json_answer_is_reported(monkeypatch):
    resp = gemini_response("")
    resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": '{"findings": [{"line": 1,'}]},
                                              "finishReason": "MAX_TOKENS"}]}
    monkeypatch.setattr(gemini_client.requests, "post", lambda *a, **k: resp)
    with pytest.raises(gemini_client.GeminiError, match="cut off at the output token limit"):
        gemini_client.generate_json("s", "p")
