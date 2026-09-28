"""
Tests for:
  - github_fetcher.parse_github_url
  - github_fetcher.fetch_python_files  (network calls mocked via unittest.mock)
  - POST /analyze-repo  (FastAPI TestClient + mocked fetch_python_files)

Run with:
    cd backend
    pytest test_analyze_repo.py -v
"""

import base64
import sys
import os
import pytest
from unittest.mock import patch, MagicMock

# Allow running from the repo root
sys.path.insert(0, os.path.dirname(__file__))

from github_fetcher import parse_github_url, fetch_python_files, GitHubFile


# ---------------------------------------------------------------------------
# parse_github_url
# ---------------------------------------------------------------------------

class TestParseGitHubUrl:
    def test_plain_url(self):
        owner, repo = parse_github_url("https://github.com/psf/requests")
        assert owner == "psf"
        assert repo == "requests"

    def test_git_suffix(self):
        owner, repo = parse_github_url("https://github.com/psf/requests.git")
        assert owner == "psf"
        assert repo == "requests"

    def test_trailing_whitespace(self):
        owner, repo = parse_github_url("  https://github.com/octocat/Hello-World  ")
        assert owner == "octocat"
        assert repo == "Hello-World"

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Not a recognised"):
            parse_github_url("https://gitlab.com/owner/repo")

    def test_missing_repo_raises(self):
        with pytest.raises(ValueError):
            parse_github_url("https://github.com/owner")

    def test_plain_http(self):
        owner, repo = parse_github_url("http://github.com/owner/repo")
        assert owner == "owner"
        assert repo == "repo"


# ---------------------------------------------------------------------------
# fetch_python_files  (mocked network)
# ---------------------------------------------------------------------------

def _make_response(json_data, status=200):
    """Create a minimal mock requests.Response."""
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = json_data
    mock.raise_for_status = MagicMock()
    return mock


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


SIMPLE_PY = "def hello():\n    return 'hello'\n"


class TestFetchPythonFiles:
    def test_returns_py_files_only(self):
        """Files with non-.py extensions should be filtered out."""
        repo_resp = _make_response({"default_branch": "main"})
        tree_resp = _make_response({
            "tree": [
                {"type": "blob", "path": "foo.py", "url": "u1"},
                {"type": "blob", "path": "README.md", "url": "u2"},
                {"type": "blob", "path": "bar.py", "url": "u3"},
                {"type": "tree", "path": "src", "url": "u4"},
            ]
        })
        content_resp = _make_response({
            "content": _b64(SIMPLE_PY),
            "encoding": "base64",
        })

        with patch("github_fetcher.requests.get") as mock_get:
            mock_get.side_effect = [repo_resp, tree_resp, content_resp, content_resp]
            files = fetch_python_files("https://github.com/owner/repo")

        assert len(files) == 2
        assert all(f.path.endswith(".py") for f in files)
        assert all(f.source == SIMPLE_PY for f in files)

    def test_max_files_cap(self):
        """fetch_python_files should honour the max_files limit."""
        repo_resp = _make_response({"default_branch": "main"})
        tree_entries = [
            {"type": "blob", "path": f"f{i}.py", "url": f"u{i}"}
            for i in range(10)
        ]
        tree_resp = _make_response({"tree": tree_entries})
        content_resp = _make_response({
            "content": _b64(SIMPLE_PY),
            "encoding": "base64",
        })

        with patch("github_fetcher.requests.get") as mock_get:
            # repo + tree + 3 content calls
            mock_get.side_effect = [repo_resp, tree_resp] + [content_resp] * 3
            files = fetch_python_files("https://github.com/owner/repo", max_files=3)

        assert len(files) == 3

    def test_binary_file_skipped(self):
        """Files that cannot be decoded as UTF-8 should be silently skipped."""
        repo_resp = _make_response({"default_branch": "main"})
        tree_resp = _make_response({
            "tree": [{"type": "blob", "path": "data.py", "url": "u1"}]
        })
        binary_resp = _make_response({
            "content": base64.b64encode(b"\xff\xfe").decode(),
            "encoding": "base64",
        })

        with patch("github_fetcher.requests.get") as mock_get:
            mock_get.side_effect = [repo_resp, tree_resp, binary_resp]
            files = fetch_python_files("https://github.com/owner/repo")

        assert files == []

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError):
            fetch_python_files("https://not-github.com/owner/repo")

    def test_return_total_flag(self):
        """_return_total=True should return (files, total_found) tuple."""
        repo_resp = _make_response({"default_branch": "main"})
        tree_entries = [
            {"type": "blob", "path": f"f{i}.py", "url": f"u{i}"}
            for i in range(5)
        ]
        tree_resp = _make_response({"tree": tree_entries})
        content_resp = _make_response({
            "content": _b64(SIMPLE_PY),
            "encoding": "base64",
        })

        with patch("github_fetcher.requests.get") as mock_get:
            mock_get.side_effect = [repo_resp, tree_resp] + [content_resp] * 3
            result = fetch_python_files(
                "https://github.com/owner/repo", max_files=3, _return_total=True
            )

        assert isinstance(result, tuple)
        files, total = result
        assert len(files) == 3
        assert total == 5

    def test_return_total_without_flag_is_list(self):
        """Default (_return_total=False) must still return a plain list."""
        repo_resp = _make_response({"default_branch": "main"})
        tree_resp = _make_response({"tree": [
            {"type": "blob", "path": "a.py", "url": "u1"},
        ]})
        content_resp = _make_response({
            "content": _b64(SIMPLE_PY),
            "encoding": "base64",
        })
        with patch("github_fetcher.requests.get") as mock_get:
            mock_get.side_effect = [repo_resp, tree_resp, content_resp]
            result = fetch_python_files("https://github.com/owner/repo")
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# POST /analyze-repo  (FastAPI endpoint via TestClient)
# ---------------------------------------------------------------------------

# Import here so sys.path is already set up
from fastapi.testclient import TestClient
from main import app  # noqa: E402

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers shared across endpoint tests
# ---------------------------------------------------------------------------

def _fake_fetch(files, total=None):
    """
    Return a mock for main.fetch_python_files that gives back (files, total).
    If total is omitted it defaults to len(files).
    """
    if total is None:
        total = len(files)
    return (files, total)


# ---------------------------------------------------------------------------
# Validation / error path tests  (shape unchanged from old tests)
# ---------------------------------------------------------------------------

class TestAnalyzeRepoEndpointValidation:
    def test_invalid_url_returns_400(self):
        resp = client.post(
            "/analyze-repo",
            json={"github_url": "https://gitlab.com/owner/repo"},
        )
        assert resp.status_code == 400
        assert "Not a recognised" in resp.json()["detail"]

    def test_max_files_too_large_returns_400(self):
        resp = client.post(
            "/analyze-repo",
            json={"github_url": "https://github.com/owner/repo", "max_files": 201},
        )
        assert resp.status_code == 400

    def test_max_files_zero_returns_400(self):
        resp = client.post(
            "/analyze-repo",
            json={"github_url": "https://github.com/owner/repo", "max_files": 0},
        )
        assert resp.status_code == 400

    def test_github_api_error_returns_502(self):
        with patch("main.fetch_python_files", side_effect=RuntimeError("network down")):
            resp = client.post(
                "/analyze-repo",
                json={"github_url": "https://github.com/owner/repo"},
            )
        assert resp.status_code == 502
        assert "Failed to fetch" in resp.json()["detail"]

    def test_default_max_files_is_50(self):
        """Verify that omitting max_files from the body defaults to 50."""
        with patch("main.fetch_python_files", return_value=([], 0)) as mock_fetch:
            client.post(
                "/analyze-repo",
                json={"github_url": "https://github.com/owner/repo"},
            )
            mock_fetch.assert_called_once_with(
                "https://github.com/owner/repo", max_files=50, _return_total=True
            )


# ---------------------------------------------------------------------------
# New response shape tests
# ---------------------------------------------------------------------------

class TestAnalyzeRepoResponseShape:
    def test_response_is_object_not_list(self):
        """Root response must be a dict, not a bare list."""
        with patch("main.fetch_python_files", return_value=([], 0)):
            resp = client.post(
                "/analyze-repo",
                json={"github_url": "https://github.com/owner/repo"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict), "Response must be a JSON object, not a list"

    def test_response_metadata_fields_present(self):
        with patch("main.fetch_python_files", return_value=([], 0)):
            resp = client.post(
                "/analyze-repo",
                json={"github_url": "https://github.com/owner/repo"},
            )
        data = resp.json()
        for field in (
            "max_files", "python_files_found", "files_selected",
            "files_skipped_due_to_limit", "files_analyzed", "results",
        ):
            assert field in data, f"Missing top-level field: {field!r}"

    def test_empty_repo_returns_empty_results(self):
        """An empty repo must return an object with results=[]."""
        with patch("main.fetch_python_files", return_value=([], 0)):
            resp = client.post(
                "/analyze-repo",
                json={"github_url": "https://github.com/owner/repo"},
            )
        data = resp.json()
        assert data["results"] == []
        assert data["python_files_found"] == 0
        assert data["files_selected"] == 0
        assert data["files_analyzed"] == 0

    def test_max_files_echoed_in_response(self):
        with patch("main.fetch_python_files", return_value=([], 0)):
            resp = client.post(
                "/analyze-repo",
                json={"github_url": "https://github.com/owner/repo", "max_files": 7},
            )
        assert resp.json()["max_files"] == 7

    def test_files_skipped_due_to_limit_computed_correctly(self):
        """When repo has more .py files than max_files, skipped count is correct."""
        fake_files = [GitHubFile(path=f"f{i}.py", source=SIMPLE_PY) for i in range(3)]
        # total_found=10, but only 3 were selected (max_files=3)
        with patch("main.fetch_python_files", return_value=(fake_files, 10)):
            resp = client.post(
                "/analyze-repo",
                json={"github_url": "https://github.com/owner/repo", "max_files": 3},
            )
        data = resp.json()
        assert data["python_files_found"] == 10
        assert data["files_selected"] == 3
        assert data["files_skipped_due_to_limit"] == 7
        assert data["files_analyzed"] == 3

    def test_no_files_skipped_when_under_limit(self):
        fake_files = [GitHubFile(path="a.py", source=SIMPLE_PY)]
        with patch("main.fetch_python_files", return_value=(fake_files, 1)):
            resp = client.post(
                "/analyze-repo",
                json={"github_url": "https://github.com/owner/repo"},
            )
        data = resp.json()
        assert data["files_skipped_due_to_limit"] == 0

    def test_results_list_has_file_and_functions_keys(self):
        fake_files = [GitHubFile(path="utils.py", source=SIMPLE_PY)]
        with patch("main.fetch_python_files", return_value=(fake_files, 1)):
            resp = client.post(
                "/analyze-repo",
                json={"github_url": "https://github.com/owner/repo"},
            )
        data = resp.json()
        assert len(data["results"]) == 1
        entry = data["results"][0]
        assert entry["file"] == "utils.py"
        assert isinstance(entry["functions"], list)

    def test_function_name_present_in_results(self):
        fake_files = [GitHubFile(path="utils.py", source=SIMPLE_PY)]
        with patch("main.fetch_python_files", return_value=(fake_files, 1)):
            resp = client.post(
                "/analyze-repo",
                json={"github_url": "https://github.com/owner/repo"},
            )
        fn_names = [f["name"] for f in resp.json()["results"][0]["functions"]]
        assert "hello" in fn_names

    def test_syntax_error_file_returns_empty_functions(self):
        """A .py file with invalid syntax should not crash the endpoint."""
        fake_files = [GitHubFile(path="broken.py", source="def (: pass")]
        with patch("main.fetch_python_files", return_value=(fake_files, 1)):
            resp = client.post(
                "/analyze-repo",
                json={"github_url": "https://github.com/owner/repo"},
            )
        assert resp.status_code == 200
        data = resp.json()
        entry = data["results"][0]
        assert entry["file"] == "broken.py"
        assert entry["functions"] == []


# ---------------------------------------------------------------------------
# Per-function transparency fields: mock mode
# ---------------------------------------------------------------------------

class TestMockModeFields:
    """
    Tests run without GROQ_API_KEY, so generate_tests always falls back to
    the placeholder mock — _mock_generate() embeds '_placeholder' in the code.
    """

    def _get_fn(self, monkeypatch):
        """Helper: call /analyze-repo with SIMPLE_PY and return first function dict."""
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        fake_files = [GitHubFile(path="utils.py", source=SIMPLE_PY)]
        with patch("main.fetch_python_files", return_value=(fake_files, 1)):
            resp = client.post(
                "/analyze-repo",
                json={"github_url": "https://github.com/owner/repo"},
            )
        assert resp.status_code == 200
        return resp.json()["results"][0]["functions"][0]

    def test_generation_mode_is_mock(self, monkeypatch):
        fn = self._get_fn(monkeypatch)
        assert fn["generation_mode"] == "mock"

    def test_coverage_status_is_not_measured(self, monkeypatch):
        fn = self._get_fn(monkeypatch)
        assert fn["coverage_status"] == "not_measured"

    def test_coverage_percent_is_null(self, monkeypatch):
        fn = self._get_fn(monkeypatch)
        assert fn["coverage_percent"] is None

    def test_warning_is_present_and_non_empty(self, monkeypatch):
        fn = self._get_fn(monkeypatch)
        assert fn["warning"]
        assert isinstance(fn["warning"], str)
        assert len(fn["warning"]) > 0

    def test_tests_passed_still_reported(self, monkeypatch):
        fn = self._get_fn(monkeypatch)
        # Placeholder test has one `pass` — it runs and passes
        assert "tests_passed" in fn
        assert isinstance(fn["tests_passed"], int)

    def test_tests_failed_still_reported(self, monkeypatch):
        fn = self._get_fn(monkeypatch)
        assert "tests_failed" in fn
        assert isinstance(fn["tests_failed"], int)

    def test_all_transparency_fields_present(self, monkeypatch):
        fn = self._get_fn(monkeypatch)
        for field in ("generation_mode", "coverage_status", "coverage_percent", "warning"):
            assert field in fn, f"Missing field: {field!r}"


# ---------------------------------------------------------------------------
# Per-function transparency fields: LLM mode  (LLM call mocked)
# ---------------------------------------------------------------------------

class TestLLMModeFields:
    """
    Simulate a successful LLM response by patching generate_tests to return
    real test code that actually imports and calls the target function.
    """

    # Test code that imports 'hello' from target and calls it — produces real coverage.
    _LLM_TEST_CODE = (
        "import pytest\n"
        "from target import hello\n\n"
        "def test_hello_returns_string():\n"
        "    assert hello() == 'hello'\n"
    )

    def _get_fn(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        fake_files = [GitHubFile(path="utils.py", source=SIMPLE_PY)]
        with patch("main.fetch_python_files", return_value=(fake_files, 1)):
            with patch("main.generate_tests", return_value=self._LLM_TEST_CODE):
                resp = client.post(
                    "/analyze-repo",
                    json={"github_url": "https://github.com/owner/repo"},
                )
        assert resp.status_code == 200
        return resp.json()["results"][0]["functions"][0]

    def test_generation_mode_is_llm(self, monkeypatch):
        fn = self._get_fn(monkeypatch)
        assert fn["generation_mode"] == "llm"

    def test_coverage_status_is_measured(self, monkeypatch):
        fn = self._get_fn(monkeypatch)
        assert fn["coverage_status"] == "measured"

    def test_coverage_percent_is_numeric(self, monkeypatch):
        fn = self._get_fn(monkeypatch)
        assert fn["coverage_percent"] is not None
        assert isinstance(fn["coverage_percent"], (int, float))

    def test_warning_is_null(self, monkeypatch):
        fn = self._get_fn(monkeypatch)
        assert fn["warning"] is None

    def test_all_transparency_fields_present(self, monkeypatch):
        fn = self._get_fn(monkeypatch)
        for field in ("generation_mode", "coverage_status", "coverage_percent", "warning"):
            assert field in fn, f"Missing field: {field!r}"

    def test_coverage_percent_is_positive_with_real_test(self, monkeypatch):
        """
        When a real test imports and calls hello(), coverage must be > 0.
        This is the transparency regression check: if this fails it means
        runner.py is not measuring coverage correctly for LLM-generated tests.
        """
        fn = self._get_fn(monkeypatch)
        assert fn["coverage_percent"] > 0, (
            f"Expected coverage > 0 for LLM-generated test that calls hello(), "
            f"got {fn['coverage_percent']!r}. "
            "Probable cause: runner.py writes fn.source (the AST-unparsed body) as "
            "target.py, but the test imports 'hello' from target — the function must "
            "be at module level in target.py for coverage to be recorded."
        )


# ---------------------------------------------------------------------------
# Regression: existing /analyze endpoint must still work
# ---------------------------------------------------------------------------

class TestExistingEndpointsUnchanged:
    def test_analyze_endpoint_still_works(self):
        """/analyze must still accept a .py upload and return a list."""
        import io
        source = SIMPLE_PY.encode()
        resp = client.post(
            "/analyze",
            files={"file": ("sample.py", io.BytesIO(source), "text/plain")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data[0]["name"] == "hello"
