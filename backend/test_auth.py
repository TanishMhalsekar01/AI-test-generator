"""Tests for GitHub OAuth sign-in, sessions and route protection."""

from unittest.mock import MagicMock, patch

import pytest

import auth
import db


PROTECTED = [
    ("get", "/api/me"), ("get", "/api/runs"), ("get", "/api/dashboard/overview"),
    ("get", "/api/github/repos"), ("post", "/api/runs/code"), ("post", "/api/runs/spec"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_api_requires_session(anon_client, method, path):
    resp = getattr(anon_client, method)(path)
    assert resp.status_code == 401


def test_app_page_redirects_to_signin_when_signed_out(anon_client):
    resp = anon_client.get("/app", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_signin_page_redirects_to_app_when_signed_in(signed_in_client):
    resp = signed_in_client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"


def test_login_redirects_to_github_with_state_cookie(anon_client):
    resp = anon_client.get("/auth/github/login", follow_redirects=False)
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=test-client-id" in loc
    assert "scope=read%3Auser+read%3Aorg+repo" in loc
    assert auth.STATE_COOKIE in resp.cookies


def test_login_without_oauth_config(anon_client, monkeypatch):
    monkeypatch.delenv("GITHUB_CLIENT_ID")
    resp = anon_client.get("/auth/github/login", follow_redirects=False)
    assert resp.headers["location"] == "/?error=oauth_not_configured"


def test_callback_rejects_state_mismatch(anon_client):
    anon_client.cookies.set(auth.STATE_COOKIE, "expected")
    resp = anon_client.get("/auth/github/callback?code=abc&state=other", follow_redirects=False)
    assert resp.headers["location"] == "/?error=state_mismatch"


def test_callback_creates_session_and_encrypts_token(anon_client):
    anon_client.cookies.set(auth.STATE_COOKIE, "s1")
    token_resp = MagicMock()
    token_resp.json.return_value = {"access_token": "gho_real_looking_token", "scope": "repo,read:org"}
    user_resp = MagicMock()
    user_resp.json.return_value = {"id": 424242, "login": "octo", "name": "Octo Cat", "avatar_url": None}
    user_resp.raise_for_status = MagicMock()
    with patch("auth.requests.post", return_value=token_resp), patch("auth.requests.get", return_value=user_resp):
        resp = anon_client.get("/auth/github/callback?code=abc&state=s1", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"
    raw = resp.cookies[auth.SESSION_COOKIE]
    row = db.get_session_user(auth.hash_session_id(raw))
    assert row["login"] == "octo"
    assert row["github_token"] != "gho_real_looking_token"  # stored encrypted
    assert auth.decrypt_token(row["github_token"]) == "gho_real_looking_token"

    anon_client.cookies.set(auth.SESSION_COOKIE, raw)
    me = anon_client.get("/api/me").json()
    assert me["login"] == "octo"
    assert me["model"] == "gemini-3.7-flash"
    assert "test-gemini-key" not in str(me)  # the key never reaches the browser


def test_logout_deletes_session(signed_in_client):
    raw = signed_in_client.cookies.get(auth.SESSION_COOKIE)
    resp = signed_in_client.post("/auth/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert db.get_session_user(auth.hash_session_id(raw)) is None


def test_security_headers_present(anon_client):
    resp = anon_client.get("/")
    assert "default-src 'self'" in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"


def test_production_redirects_to_canonical_https_host(anon_client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_BASE_URL", "https://aitestgen.dev")
    resp = anon_client.get("/app?x=1", headers={"host": "www.aitestgen.dev"}, follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "https://aitestgen.dev/app?x=1"
    assert anon_client.get("/healthz").status_code == 200


def test_revoked_github_token_ends_session(signed_in_client):
    from unittest.mock import patch
    from github_api import GitHubAuthError
    raw = signed_in_client.cookies.get(auth.SESSION_COOKIE)
    with patch("main.GitHubClient") as client_cls:
        client_cls.return_value.repos.side_effect = GitHubAuthError()
        resp = signed_in_client.get("/api/github/repos")
    assert resp.status_code == 401
    assert db.get_session_user(auth.hash_session_id(raw)) is None


def test_login_from_other_host_moves_to_base_url_host(anon_client, monkeypatch):
    # The state cookie must live on the APP_BASE_URL host, where GitHub sends the user back.
    monkeypatch.setattr(auth, "host_resolves", lambda host: True)
    resp = anon_client.get("/auth/github/login", headers={"host": "ai-test-generator-6bv7.onrender.com"},
                           follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "http://testserver/auth/github/login"
    assert auth.STATE_COOKIE not in resp.cookies


def test_login_explains_unreachable_base_url(anon_client, monkeypatch):
    monkeypatch.setattr(auth, "host_resolves", lambda host: False)
    resp = anon_client.get("/auth/github/login", headers={"host": "ai-test-generator-6bv7.onrender.com"},
                           follow_redirects=False)
    assert resp.headers["location"] == "/?error=base_url_unreachable"


def test_auth_status_reports_configuration(anon_client, monkeypatch):
    monkeypatch.setattr(auth, "host_resolves", lambda host: False)
    status = anon_client.get("/auth/status", headers={"host": "elsewhere.example"}).json()
    assert status == {"oauth_configured": True, "app_base_url": "http://testserver",
                      "on_base_host": False, "base_host_resolves": False}
    assert anon_client.get("/auth/status").json()["on_base_host"] is True


def test_production_http_redirects_to_https_on_same_host(anon_client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_BASE_URL", "https://aitestge.stream")
    resp = anon_client.get("/app", headers={"host": "ai-test-generator-6bv7.onrender.com"}, follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "https://ai-test-generator-6bv7.onrender.com/app"
