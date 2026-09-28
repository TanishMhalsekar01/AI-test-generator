"""
Shared pytest fixtures.

- Isolated SQLite database per test session.
- A fake GEMINI_API_KEY is set *before* config loads backend/.env, so a
  developer's real key is never used by the test suite.
- Any Gemini HTTP call that a test has not explicitly mocked fails loudly.
- Jobs run inline so run results are available immediately.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="aitg-tests-"))
os.environ["GEMINI_API_KEY"] = "test-gemini-key-000000"
os.environ["GEMINI_MODEL"] = "gemini-3.7-flash"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["SESSION_SECRET"] = "test-session-secret-for-pytest-only"
os.environ["APP_ENV"] = "development"
os.environ["APP_BASE_URL"] = "http://testserver"
os.environ["ALLOW_PRIVATE_TARGETS"] = "true"
os.environ["GITHUB_CLIENT_ID"] = "test-client-id"
os.environ["GITHUB_CLIENT_SECRET"] = "test-client-secret"
os.environ.pop("ALLOWED_HOSTS", None)

sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import auth  # noqa: E402
import gemini_client  # noqa: E402
import github_api  # noqa: E402
import jobs  # noqa: E402

jobs.INLINE = True


@pytest.fixture(autouse=True)
def _guard_network(monkeypatch):
    """Block real Gemini calls and backoff sleeps unless a test mocks them."""
    def unmocked(*_a, **_k):
        raise AssertionError("Unmocked Gemini API call in tests")
    monkeypatch.setattr(gemini_client.requests, "post", unmocked)
    monkeypatch.setattr(gemini_client, "_sleep", lambda _s: None)
    gemini_client.reset_breaker()
    # Unmocked GitHub calls hit a closed local port instead of the real API.
    monkeypatch.setattr(github_api, "API", "http://127.0.0.1:9")
    github_api.clear_org_cache()
    yield


def gemini_response(text: str, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    if status == 200:
        resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}
    else:
        resp.json.return_value = {"error": {"message": "This model is currently experiencing high demand."}}
    return resp


@pytest.fixture
def mock_gemini(monkeypatch):
    """Queue Gemini responses: mock_gemini(text1, text2, ...) — returns the call list."""
    calls: list[dict] = []

    def install(*texts: str):
        queue = list(texts)

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append({"url": url, "headers": headers, "json": json})
            text = queue.pop(0) if len(queue) > 1 else queue[0]
            return text if isinstance(text, MagicMock) else gemini_response(text)
        monkeypatch.setattr(gemini_client.requests, "post", fake_post)
        return calls
    return install


_user_counter = [1000]


@pytest.fixture
def signed_in_client():
    """TestClient with a real session row for a fresh GitHub user."""
    from fastapi.testclient import TestClient
    import main

    _user_counter[0] += 1
    uid = _user_counter[0]
    login = f"user{uid}"
    raw = auth.start_session({"id": uid, "login": login, "name": "Test User", "avatar_url": None},
                             f"gho_fake_token_{uid}", "read:user,read:org,repo")
    client = TestClient(main.app)
    client.cookies.set(auth.SESSION_COOKIE, raw)
    client.user = {"id": uid, "login": login, "token": f"gho_fake_token_{uid}"}
    return client


@pytest.fixture
def anon_client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)
