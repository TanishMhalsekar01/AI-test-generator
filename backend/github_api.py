"""
GitHub REST API client used with the signed-in user's OAuth token.

Everything shown on the dashboards that comes from GitHub (repositories,
organisations, members, commits) is fetched live through this module.
"""

import base64
import hashlib
import re
import threading
import time
from typing import Any, Optional

import requests

API = "https://api.github.com"
TIMEOUT = 20

_REPO_URL_RE = re.compile(
    r"^(?:https?://github\.com/)?(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)"
    r"(?:\.git)?(?:/(?:tree|blob)/(?P<branch>[^\s?#]+))?/?$"
)


class GitHubError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class GitHubAuthError(GitHubError):
    def __init__(self, message: str = "Your GitHub session has expired. Sign in again."):
        super().__init__(message, status=401)


def parse_repo_ref(value: str) -> tuple[str, str, Optional[str]]:
    """Accept 'owner/repo', a github.com URL, or a /tree/<branch> URL."""
    m = _REPO_URL_RE.match((value or "").strip())
    if not m:
        raise ValueError("Enter a repository as owner/name or https://github.com/owner/name.")
    return m.group("owner"), m.group("repo"), m.group("branch")


class GitHubClient:
    def __init__(self, token: str):
        self.token = token
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-test-generator",
        })

    # -- transport ---------------------------------------------------------

    def _request(self, path: str, params: Optional[dict] = None) -> requests.Response:
        url = path if path.startswith("http") else f"{API}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise GitHubError(f"Could not reach GitHub ({type(exc).__name__}).") from exc
        if resp.status_code == 401:
            raise GitHubAuthError()
        if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = resp.headers.get("X-RateLimit-Reset")
            wait = max(0, int(reset) - int(time.time())) // 60 if reset else None
            raise GitHubError(
                "GitHub API rate limit reached" + (f"; resets in about {wait} min." if wait is not None else "."),
                status=429,
            )
        if resp.status_code == 404:
            raise GitHubError("Not found on GitHub, or your account has no access to it.", status=404)
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("message", "")
            except ValueError:
                msg = resp.text[:200]
            # 403 = GitHub refused (e.g. an organization restricts OAuth apps); pass it through.
            raise GitHubError(f"GitHub API error {resp.status_code}: {msg}",
                              status=403 if resp.status_code == 403 else 502)
        return resp

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        return self._request(path, params).json()

    def get_paginated(self, path: str, params: Optional[dict] = None, max_pages: int = 5) -> list:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        items: list = []
        url: Optional[str] = path
        for _ in range(max_pages):
            if not url:
                break
            resp = self._request(url, params)
            items.extend(resp.json())
            url = resp.links.get("next", {}).get("url")
            params = None  # the next link already carries the query string
        return items

    # -- users / orgs ------------------------------------------------------

    def user(self) -> dict:
        return self.get("/user")

    def repos(self) -> list[dict]:
        data = self.get_paginated("/user/repos", {"sort": "pushed", "affiliation": "owner,collaborator,organization_member"})
        return [repo_summary(r) for r in data]

    def orgs(self) -> list[dict]:
        return [
            {"login": o["login"], "avatar_url": o.get("avatar_url"), "description": o.get("description")}
            for o in self.get_paginated("/user/orgs")
        ]

    def org(self, org: str) -> dict:
        return self.get(f"/orgs/{org}")

    def org_repos(self, org: str) -> list[dict]:
        return [repo_summary(r) for r in self.get_paginated(f"/orgs/{org}/repos", {"sort": "pushed"})]

    def org_members(self, org: str) -> list[dict]:
        return [
            {"login": m["login"], "avatar_url": m.get("avatar_url")}
            for m in self.get_paginated(f"/orgs/{org}/members", max_pages=3)
        ]

    # -- repositories ------------------------------------------------------

    def repo(self, owner: str, name: str) -> dict:
        return self.get(f"/repos/{owner}/{name}")

    def commit_sha(self, owner: str, name: str, ref: str) -> str:
        return self.get(f"/repos/{owner}/{name}/commits/{ref}")["sha"]

    def tree(self, owner: str, name: str, sha: str) -> tuple[list[dict], bool]:
        data = self.get(f"/repos/{owner}/{name}/git/trees/{sha}", {"recursive": "1"})
        entries = [e for e in data.get("tree", []) if e.get("type") == "blob"]
        return entries, bool(data.get("truncated"))

    def blob_text(self, owner: str, name: str, sha: str) -> Optional[str]:
        data = self.get(f"/repos/{owner}/{name}/git/blobs/{sha}")
        if data.get("encoding") != "base64":
            return None
        try:
            raw = base64.b64decode(data.get("content", ""))
        except ValueError:
            return None
        if b"\x00" in raw[:8000]:
            return None  # binary
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None


def repo_summary(r: dict) -> dict:
    return {
        "full_name": r["full_name"],
        "name": r["name"],
        "owner": r["owner"]["login"],
        "owner_type": r["owner"].get("type"),
        "private": r.get("private", False),
        "language": r.get("language"),
        "description": r.get("description"),
        "default_branch": r.get("default_branch"),
        "pushed_at": r.get("pushed_at"),
        "open_issues": r.get("open_issues_count", 0),
        "stars": r.get("stargazers_count", 0),
        "html_url": r.get("html_url"),
        "archived": r.get("archived", False),
    }


# ---------------------------------------------------------------------------
# Org membership cache (membership decides who may see team data)
# ---------------------------------------------------------------------------

_ORG_CACHE: dict[str, tuple[float, list[str]]] = {}
_ORG_CACHE_TTL = 300
_org_lock = threading.Lock()


def user_org_logins(token: str) -> list[str]:
    key = hashlib.sha256(token.encode()).hexdigest()
    with _org_lock:
        hit = _ORG_CACHE.get(key)
        if hit and time.time() - hit[0] < _ORG_CACHE_TTL:
            return hit[1]
    logins = [o["login"] for o in GitHubClient(token).orgs()]
    with _org_lock:
        _ORG_CACHE[key] = (time.time(), logins)
    return logins


def clear_org_cache() -> None:
    with _org_lock:
        _ORG_CACHE.clear()
