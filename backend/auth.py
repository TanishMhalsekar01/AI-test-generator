"""
GitHub OAuth sign-in and server-side sessions.

Flow: /auth/github/login -> github.com authorize -> /auth/github/callback
The browser only ever holds a random session id (HttpOnly cookie). The GitHub
access token stays on the server, encrypted with a key derived from
SESSION_SECRET.
"""

import base64
import hashlib
import hmac
import secrets
import socket
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

import db
from config import get_settings

router = APIRouter()

SESSION_COOKIE = "aitg_session"
STATE_COOKIE = "aitg_oauth_state"
OAUTH_SCOPES = "read:user read:org repo"
SESSION_DAYS = 14


@dataclass
class CurrentUser:
    id: int
    login: str
    name: Optional[str]
    avatar_url: Optional[str]
    github_token: str


def _fernet() -> Fernet:
    digest = hashlib.sha256(get_settings().session_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(value: str) -> Optional[str]:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return None


def hash_session_id(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def start_session(gh_user: dict, access_token: str, scopes: str) -> str:
    """Persist user + session; returns the raw session id for the cookie."""
    db.upsert_user(gh_user)
    raw = secrets.token_urlsafe(32)
    db.create_session(hash_session_id(raw), gh_user["id"], encrypt_token(access_token), scopes, SESSION_DAYS)
    return raw


def _callback_url() -> str:
    return f"{get_settings().app_base_url}/auth/github/callback"


def _set_cookie(resp, name: str, value: str, max_age: int) -> None:
    resp.set_cookie(
        name, value, max_age=max_age, httponly=True, samesite="lax",
        secure=get_settings().secure_cookies, path="/",
    )


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def optional_user(request: Request) -> Optional[CurrentUser]:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    row = db.get_session_user(hash_session_id(raw))
    if not row:
        return None
    token = decrypt_token(row["github_token"])
    if not token:
        return None
    return CurrentUser(id=row["id"], login=row["login"], name=row["name"],
                       avatar_url=row["avatar_url"], github_token=token)


def current_user(request: Request) -> CurrentUser:
    user = optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in with GitHub to continue.")
    return user


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

_DNS_CACHE: dict[str, tuple[float, bool]] = {}


def host_resolves(host: str) -> bool:
    """Whether *host* has a DNS record (cached for a minute)."""
    name = host.split(":")[0]
    hit = _DNS_CACHE.get(name)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    try:
        socket.getaddrinfo(name, None)
        ok = True
    except OSError:
        ok = False
    _DNS_CACHE[name] = (time.monotonic(), ok)
    return ok


def _request_host(request: Request) -> str:
    return request.headers.get("host", "").lower()


@router.get("/auth/status", include_in_schema=False)
def auth_status(request: Request) -> dict:
    """Public sign-in configuration, so the sign-in page can explain setup problems."""
    settings = get_settings()
    canonical = settings.canonical_host.lower()
    return {
        "oauth_configured": settings.oauth_configured,
        "app_base_url": settings.app_base_url,
        "on_base_host": _request_host(request) == canonical,
        "base_host_resolves": host_resolves(canonical) if canonical else False,
    }


@router.get("/auth/github/login", include_in_schema=False)
def github_login(request: Request) -> RedirectResponse:
    settings = get_settings()
    if not settings.oauth_configured:
        return RedirectResponse("/?error=oauth_not_configured", status_code=303)
    # GitHub returns to APP_BASE_URL, and the state cookie must be set on that same
    # host or the callback cannot verify it. Start sign-in there.
    canonical = settings.canonical_host.lower()
    if canonical and _request_host(request) != canonical:
        if not host_resolves(canonical):
            return RedirectResponse("/?error=base_url_unreachable", status_code=303)
        return RedirectResponse(f"{settings.app_base_url}/auth/github/login", status_code=303)
    state = secrets.token_urlsafe(32)
    query = urlencode({
        "client_id": settings.github_client_id,
        "redirect_uri": _callback_url(),
        "scope": OAUTH_SCOPES,
        "state": state,
        "allow_signup": "true",
    })
    resp = RedirectResponse(f"https://github.com/login/oauth/authorize?{query}", status_code=303)
    _set_cookie(resp, STATE_COOKIE, state, 600)
    return resp


@router.get("/auth/github/callback", include_in_schema=False)
def github_callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    settings = get_settings()
    if error:
        return RedirectResponse("/?error=access_denied", status_code=303)
    expected = request.cookies.get(STATE_COOKIE, "")
    if not code or not state or not expected or not hmac.compare_digest(state, expected):
        return RedirectResponse("/?error=state_mismatch", status_code=303)
    if not settings.oauth_configured:
        return RedirectResponse("/?error=oauth_not_configured", status_code=303)

    try:
        token_resp = requests.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "redirect_uri": _callback_url(),
            },
            headers={"Accept": "application/json"},
            timeout=20,
        )
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return RedirectResponse("/?error=token_exchange_failed", status_code=303)
        user_resp = requests.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            timeout=20,
        )
        user_resp.raise_for_status()
        gh_user = user_resp.json()
    except (requests.RequestException, ValueError):
        return RedirectResponse("/?error=github_unreachable", status_code=303)

    raw = start_session(gh_user, access_token, token_data.get("scope", ""))
    resp = RedirectResponse("/app", status_code=303)
    _set_cookie(resp, SESSION_COOKIE, raw, SESSION_DAYS * 86400)
    resp.delete_cookie(STATE_COOKIE, path="/")
    return resp


@router.post("/auth/logout", include_in_schema=False)
def logout(request: Request) -> RedirectResponse:
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        db.delete_session(hash_session_id(raw))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
