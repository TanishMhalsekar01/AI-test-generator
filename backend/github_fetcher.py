"""
GitHub Repo Fetcher
Fetches Python source files from a public GitHub repository using the GitHub
Contents API.  No authentication required for public repos; optionally uses
GITHUB_TOKEN to raise the rate limit from 60 to 5 000 req/h.
"""

import os
import re
import base64
from typing import NamedTuple

import requests


# Regex that accepts both github.com URL styles:
#   https://github.com/owner/repo
#   https://github.com/owner/repo.git
_GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/\s]+?)(?:\.git)?$"
)

_API_BASE = "https://api.github.com"
_TIMEOUT = 15  # seconds per request


class GitHubFile(NamedTuple):
    path: str      # relative path inside the repo, e.g. "src/utils.py"
    source: str    # decoded UTF-8 source text


def _auth_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def parse_github_url(url: str) -> tuple[str, str]:
    """
    Parse a GitHub repo URL and return (owner, repo).
    Raises ValueError for unrecognised URLs.
    """
    m = _GITHUB_URL_RE.match(url.strip())
    if not m:
        raise ValueError(
            f"Not a recognised public GitHub URL: {url!r}. "
            "Expected https://github.com/owner/repo"
        )
    return m.group("owner"), m.group("repo")


def _get_default_branch(owner: str, repo: str) -> str:
    """Return the default branch name for the given repo."""
    resp = requests.get(
        f"{_API_BASE}/repos/{owner}/{repo}",
        headers=_auth_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["default_branch"]


def _list_python_files(owner: str, repo: str, branch: str) -> list[dict]:
    """
    Use the Git Trees API (recursive) to list every .py file in the repo.
    Returns a list of tree-entry dicts (each has 'path' and 'url').
    """
    resp = requests.get(
        f"{_API_BASE}/repos/{owner}/{repo}/git/trees/{branch}",
        headers=_auth_headers(),
        params={"recursive": "1"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        entry for entry in data.get("tree", [])
        if entry.get("type") == "blob" and entry["path"].endswith(".py")
    ]


def _fetch_file_content(owner: str, repo: str, path: str) -> str | None:
    """
    Fetch a single file via the Contents API and return decoded UTF-8 text.
    Returns None if the file cannot be decoded or is binary.
    """
    resp = requests.get(
        f"{_API_BASE}/repos/{owner}/{repo}/contents/{path}",
        headers=_auth_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("content", "")
    encoding = data.get("encoding", "base64")
    if encoding != "base64":
        return None
    try:
        return base64.b64decode(raw).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def fetch_python_files(
    github_url: str,
    max_files: int = 50,
    _return_total: bool = False,
):
    """
    Fetch up to *max_files* Python source files from a public GitHub repo.

    Parameters
    ----------
    github_url:
        Full HTTPS URL of the repo, e.g. ``https://github.com/owner/repo``.
    max_files:
        Safety cap so a single request cannot hammer the GitHub API
        indefinitely.  Defaults to 50.
    _return_total:
        Internal flag.  When True, returns a ``(files, total_found)`` tuple
        where ``total_found`` is the number of eligible .py files discovered
        before the cap was applied.  Defaults to False (returns list only),
        preserving backward compatibility for all existing callers.

    Returns
    -------
    List of :class:`GitHubFile` named-tuples with ``path`` and ``source``,
    or ``(list, int)`` when *_return_total* is True.

    Raises
    ------
    ValueError
        If the URL cannot be parsed as a GitHub repo URL.
    requests.HTTPError
        On non-2xx responses from the GitHub API.
    """
    owner, repo = parse_github_url(github_url)
    branch = _get_default_branch(owner, repo)
    entries = _list_python_files(owner, repo, branch)

    total_found = len(entries)

    files: list[GitHubFile] = []
    for entry in entries[:max_files]:
        source = _fetch_file_content(owner, repo, entry["path"])
        if source is not None:
            files.append(GitHubFile(path=entry["path"], source=source))

    if _return_total:
        return files, total_found
    return files
