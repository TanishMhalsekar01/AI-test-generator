"""Tests for the per-file review pipeline and the /api/runs/code endpoint (Gemini mocked)."""

import io
import json

import code_review

BUGGY = '''def average(values):
    """Return the arithmetic mean of a list of numbers."""
    return sum(values) / len(values) + 1
'''

REVIEW = json.dumps({
    "language": "Python",
    "summary": "Computes a mean but adds 1 to the result.",
    "findings": [
        {"line": 3, "end_line": 3, "severity": "high", "category": "bug", "title": "Mean is off by one",
         "explanation": "The result has 1 added.", "suggestion": "Remove + 1.", "fixed_code": "return sum(values) / len(values)"},
        {"line": 3, "severity": "medium", "category": "exception-handling", "title": "Empty list divides by zero",
         "explanation": "len([]) is 0.", "suggestion": "Raise ValueError for empty input.", "fixed_code": ""},
        {"line": 999, "severity": "nonsense", "category": "weird", "title": "Out of range line"},
    ],
    "tests": {"framework": "pytest", "code": (
        "from stats import average\nimport pytest\n\n"
        "def test_average_of_three():\n    assert average([1, 2, 3]) == 2\n\n"
        "def test_single():\n    assert average([4]) == 4\n")},
})

TRIAGE = json.dumps({"failures": [
    {"test": "test_average_of_three", "verdict": "code_defect", "line": 3, "explanation": "Adds 1 to the mean."},
    {"test": "test_single", "verdict": "code_defect", "line": 3, "explanation": "Adds 1 to the mean."},
]})


def test_review_file_runs_tests_and_triages(mock_gemini):
    calls = mock_gemini(REVIEW, TRIAGE)
    rep = code_review.review_file("stats.py", BUGGY)
    assert rep["language"] == "Python"
    assert rep["review"]["status"] == "ok"
    assert rep["review"]["model"] == "gemini-2.5-pro"
    findings = rep["review"]["findings"]
    assert findings[0]["severity"] == "high"
    assert findings[-1]["line"] == 3  # clamped to file length
    assert findings[-1]["severity"] == "medium" and findings[-1]["category"] == "bug"  # normalised
    exe = rep["tests"]["execution"]
    assert exe["executed"] and exe["failed"] == 2 and exe["passed"] == 0
    assert all(t["triage"]["verdict"] == "code_defect" for t in exe["tests"])
    assert findings[0]["confirmed_by_test"] in {"test_average_of_three", "test_single"}
    assert rep["counts"]["tests_failed"] == 2 and rep["counts"]["high"] == 1
    # The review prompt carries numbered source lines so findings can cite them.
    prompt = calls[0]["json"]["contents"][0]["parts"][0]["text"]
    assert "    3 |     return sum(values) / len(values) + 1" in prompt


def test_review_file_without_gemini_still_reports_static(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    rep = code_review.review_file("broken.py", "def f(:\n  pass\n")
    assert rep["review"]["status"] == "error"
    assert "GEMINI_API_KEY" in rep["review"]["error"]
    assert rep["static"]["diagnostics"][0]["severity"] == "error"
    exe = rep["tests"]["execution"]
    assert exe["executed"] is False and exe["passed"] == 0  # nothing fabricated


def test_code_run_endpoint_stores_report(signed_in_client, mock_gemini):
    mock_gemini(REVIEW, TRIAGE)
    resp = signed_in_client.post("/api/runs/code", files=[("files", ("stats.py", io.BytesIO(BUGGY.encode()), "text/x-python"))],
                                 data={"project": "stats-lib"})
    assert resp.status_code == 202
    run = signed_in_client.get(f"/api/runs/{resp.json()['id']}").json()
    assert run["status"] == "completed"
    assert run["project"] == "stats-lib"
    assert run["languages"] == ["Python"]
    assert run["summary"]["tests_failed"] == 2
    assert run["report"]["files"][0]["file"] == "stats.py"


def test_code_run_accepts_pasted_code_in_any_language(signed_in_client, mock_gemini):
    review = json.dumps({"language": "Kotlin", "summary": "ok", "findings": [], "tests": {"framework": "JUnit 5", "code": "class T"}})
    mock_gemini(review)
    resp = signed_in_client.post("/api/runs/code", data={"code": "fun main() { println(1) }", "filename": "Main.kt"})
    run = signed_in_client.get(f"/api/runs/{resp.json()['id']}").json()
    f = run["report"]["files"][0]
    assert f["language"] == "Kotlin"
    assert f["tests"]["execution"]["status"] == "not_executed"


def test_code_run_validation(signed_in_client):
    assert signed_in_client.post("/api/runs/code", data={}).status_code == 400
    big = "x" * 300_001
    assert signed_in_client.post("/api/runs/code", data={"code": big, "filename": "a.py"}).status_code == 413
