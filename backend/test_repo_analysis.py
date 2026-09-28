"""Tests for repository selection, analysis and POST /api/runs/repo (GitHub + Gemini mocked)."""

import json
from unittest.mock import MagicMock, patch

import pytest

import github_api
import repo_analyzer
from github_api import GitHubAuthError, GitHubClient, GitHubError, parse_repo_ref


class TestParseRepoRef:
    @pytest.mark.parametrize("value,expected", [
        ("https://github.com/psf/requests", ("psf", "requests", None)),
        ("https://github.com/psf/requests.git", ("psf", "requests", None)),
        ("  https://github.com/octocat/Hello-World  ", ("octocat", "Hello-World", None)),
        ("http://github.com/owner/repo/", ("owner", "repo", None)),
        ("owner/repo", ("owner", "repo", None)),
        ("https://github.com/o/r/tree/feature/x", ("o", "r", "feature/x")),
    ])
    def test_valid(self, value, expected):
        assert parse_repo_ref(value) == expected

    @pytest.mark.parametrize("value", ["https://gitlab.com/owner/repo", "https://github.com/owner", "", "just-a-name"])
    def test_invalid(self, value):
        with pytest.raises(ValueError):
            parse_repo_ref(value)


def _resp(status=200, data=None, headers=None, links=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = data
    r.headers = headers or {}
    r.links = links or {}
    r.text = ""
    return r


class TestGitHubClient:
    def test_401_raises_auth_error(self):
        client = GitHubClient("t")
        with patch.object(client._session, "get", return_value=_resp(401, {})):
            with pytest.raises(GitHubAuthError):
                client.user()

    def test_rate_limit_message(self):
        client = GitHubClient("t")
        with patch.object(client._session, "get", return_value=_resp(403, {}, {"X-RateLimit-Remaining": "0"})):
            with pytest.raises(GitHubError) as exc:
                client.user()
        assert exc.value.status == 429

    def test_pagination_follows_next_links(self):
        client = GitHubClient("t")
        pages = [_resp(200, [{"login": "a"}], links={"next": {"url": "https://api.github.com/user/orgs?page=2"}}),
                 _resp(200, [{"login": "b"}])]
        with patch.object(client._session, "get", side_effect=pages):
            assert [o["login"] for o in client.orgs()] == ["a", "b"]

    def test_blob_text_skips_binary(self):
        import base64
        client = GitHubClient("t")
        data = {"encoding": "base64", "content": base64.b64encode(b"\x00\x01binary").decode()}
        with patch.object(client._session, "get", return_value=_resp(200, data)):
            assert client.blob_text("o", "r", "sha") is None


def test_select_files_prefers_source_and_skips_vendor():
    entries = [
        {"path": "src/app.py", "sha": "1", "size": 100},
        {"path": "src/server.go", "sha": "2", "size": 100},
        {"path": "tests/test_app.py", "sha": "3", "size": 100},
        {"path": "node_modules/x/index.js", "sha": "4", "size": 100},
        {"path": "web/app.min.js", "sha": "5", "size": 100},
        {"path": "README.md", "sha": "6", "size": 100},
        {"path": "big/data.py", "sha": "7", "size": 900_000},
        {"path": "lib/Main.java", "sha": "8", "size": 100},
    ]
    selected, stats = repo_analyzer.select_files(entries, max_files=3)
    paths = [e["path"] for e in selected]
    assert "tests/test_app.py" not in paths  # tests come last
    assert set(paths) == {"src/app.py", "src/server.go", "lib/Main.java"}
    assert stats["vendor_or_build"] == 1 and stats["generated"] == 1 and stats["too_large"] == 1
    assert stats["candidates"] == 4


class FakeGitHub:
    def __init__(self, files):
        self.files = files

    def repo(self, owner, name):
        return {"full_name": f"{owner}/{name}", "name": name, "owner": {"login": owner},
                "default_branch": "main", "html_url": f"https://github.com/{owner}/{name}"}

    def commit_sha(self, owner, name, ref):
        return "a" * 40

    def tree(self, owner, name, sha):
        return [{"path": p, "sha": p, "size": len(t)} for p, t in self.files.items()], False

    def blob_text(self, owner, name, sha):
        return self.files[sha]


REVIEW = json.dumps({"language": "x", "summary": "s", "findings": [
    {"line": 1, "severity": "low", "category": "bug", "title": "t", "explanation": "e", "suggestion": "s"}],
    "tests": {"framework": "pytest", "code": "def test_ok():\n    assert True\n"}})


def test_repo_run_endpoint(signed_in_client, mock_gemini):
    mock_gemini(REVIEW)
    fake = FakeGitHub({"pkg/util.py": "def one():\n    return 1\n", "cmd/main.go": "package main\n\nfunc main() {}\n"})
    with patch("main.GitHubClient", return_value=fake):
        resp = signed_in_client.post("/api/runs/repo", json={"repo": "acme/widgets", "max_files": 5})
    assert resp.status_code == 202, resp.text
    run = signed_in_client.get(f"/api/runs/{resp.json()['id']}").json()
    assert run["status"] == "completed"
    assert run["project"] == "acme/widgets" and run["owner"] == "acme"
    assert run["commit_sha"] == "a" * 40 and run["ref"] == "main"
    assert sorted(run["languages"]) == ["Go", "Python"]
    assert run["summary"]["files"] == 2
    assert run["summary"]["low"] == 2


def test_repo_run_rejects_bad_input(signed_in_client):
    assert signed_in_client.post("/api/runs/repo", json={"repo": "not a repo"}).status_code == 400
    assert signed_in_client.post("/api/runs/repo", json={"repo": "a/b", "max_files": 0}).status_code in (400, 422)


def test_repo_run_github_not_found(signed_in_client):
    client = MagicMock()
    client.repo.side_effect = GitHubError("Not found on GitHub, or your account has no access to it.", status=404)
    with patch("main.GitHubClient", return_value=client):
        resp = signed_in_client.post("/api/runs/repo", json={"repo": "a/private"})
    assert resp.status_code == 404
    assert "no access" in resp.json()["detail"]


def test_org_membership_cache(monkeypatch):
    calls = []

    class C:
        def __init__(self, token):
            pass

        def orgs(self):
            calls.append(1)
            return [{"login": "acme"}]
    monkeypatch.setattr(github_api, "GitHubClient", C)
    assert github_api.user_org_logins("tok") == ["acme"]
    assert github_api.user_org_logins("tok") == ["acme"]
    assert len(calls) == 1
