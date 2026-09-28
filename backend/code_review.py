"""
Per-file analysis pipeline (any language):

  1. detect language
  2. static checks  — real compiler / linter diagnostics (static_checks.py)
  3. AI review      — Gemini finds defects and writes a test file for the
                      language's standard framework
  4. execute tests  — in the sandbox where a runner exists (executors.py)
  5. triage         — if tests fail, Gemini classifies each failure as a code
                      defect or a wrong test expectation

Every stage reports its own status; a failed AI call never turns into
fabricated results.
"""

from typing import Callable, Optional

import executors
import gemini_client
from languages import Language, detect_language
from parser import parse_source
from static_checks import run_static_checks

MAX_REVIEW_LINES = 2500
MAX_REVIEW_CHARS = 150_000
MAX_SOURCE_IN_REPORT = 300_000
SEVERITIES = ("critical", "high", "medium", "low")
CATEGORIES = {
    "bug", "exception-handling", "security", "performance", "concurrency",
    "resource-leak", "edge-case", "api-misuse", "type-error",
}

REVIEW_SYSTEM = """\
You are a principal software engineer performing a rigorous code review and writing unit tests.
You can read every programming language. Find the real defects a programmer made: logic errors,
wrong conditions and off-by-one errors, unhandled exceptions and error paths, null/undefined
dereferences, resource leaks, race conditions, injection and other security flaws, incorrect API
usage, and unhandled edge cases (empty input, zero, negative numbers, very large values, unicode).

Rules:
- Report only concrete problems that exist in the given code. No style preferences, no requests for
  comments or docs, no speculation. If the code is correct, return an empty findings list.
- Every finding must cite line numbers from the numbered listing.
- Severity: critical = security hole, data loss or crash on common input; high = wrong result or crash
  on plausible input; medium = edge-case bug or unhandled error path; low = minor robustness issue.
- Tests must assert the INTENDED behaviour implied by names, docstrings, comments and types, not the
  current behaviour, so a failing test demonstrates a bug. Cover normal cases, boundaries, invalid
  input and exception paths. Tests must be deterministic and must not use the network, wall-clock
  time, unseeded randomness or files outside a temporary directory.
- Respond with a single JSON object only."""

TRIAGE_SYSTEM = """\
You are debugging failing unit tests. For each failing test decide whether the failure demonstrates a
defect in the code under test ("code_defect"), a wrong assumption in the test itself ("test_error"),
or a sandbox/environment limitation ("environment"). Choose code_defect only when the code's behaviour
contradicts what its names, docstrings, comments or types promise. Respond with a single JSON object only."""


def numbered(source: str) -> str:
    return "\n".join(f"{i:>5} | {line}" for i, line in enumerate(source.splitlines(), start=1))


def _python_facts(source: str) -> str:
    try:
        functions = parse_source(source)
    except SyntaxError:
        return ""
    if not functions:
        return ""
    lines = ["Structured facts extracted with Python's ast module:"]
    for fn in functions[:40]:
        lines.append(f"- {fn.signature}: {fn.logic_summary()}")
    return "\n".join(lines)


def build_review_prompt(filename: str, source: str, language: Language, harness: executors.Harness,
                        diagnostics: list[dict], truncated: bool) -> str:
    diag_lines = [
        f"- line {d.get('line') or '?'}: {d['severity']}: {d['message']} [{d['tool']}]"
        for d in diagnostics[:40]
    ] or ["(none)"]
    lang_line = language.name if language.id != "other" else "unknown - identify it from the code"
    parts = [
        f"File: {filename}",
        f"Language: {lang_line}",
        f"Test framework and layout: {harness.instructions}",
    ]
    if harness.test_file:
        parts.append(f"Test file name: {harness.test_file}")
    parts += ["", "Compiler / linter diagnostics already reported to the user (do not repeat them verbatim):",
              *diag_lines]
    if language.id == "python":
        facts = _python_facts(source)
        if facts:
            parts += ["", facts]
    if truncated:
        parts += ["", f"NOTE: the file was truncated to the first {MAX_REVIEW_LINES} lines for review."]
    parts += [
        "",
        "Source (the line-number prefix is not part of the code):",
        numbered(source),
        "",
        "Return JSON with exactly this shape:",
        '{"language": "<language name>",',
        ' "summary": "<2-3 factual sentences: what the code does and its main risks>",',
        ' "findings": [{"line": <int>, "end_line": <int>, "severity": "critical|high|medium|low",',
        '   "category": "bug|exception-handling|security|performance|concurrency|resource-leak|edge-case|api-misuse|type-error",',
        '   "title": "<short title>", "explanation": "<what is wrong and which input triggers it>",',
        '   "suggestion": "<how to fix it>", "fixed_code": "<corrected snippet, or empty string>"}],',
        ' "tests": {"framework": "<framework>", "code": "<complete test file contents>"}}',
    ]
    return "\n".join(parts)


def _normalise_findings(raw: object, n_lines: int) -> list[dict]:
    findings = []
    if not isinstance(raw, list):
        return findings
    for item in raw[:60]:
        if not isinstance(item, dict):
            continue
        try:
            line = int(item.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        try:
            end = int(item.get("end_line") or line)
        except (TypeError, ValueError):
            end = line
        line = min(max(line, 1), max(n_lines, 1)) if line else None
        end = min(max(end, line or 1), max(n_lines, 1)) if line else None
        severity = str(item.get("severity", "medium")).lower()
        category = str(item.get("category", "bug")).lower()
        findings.append({
            "line": line,
            "end_line": end,
            "severity": severity if severity in SEVERITIES else "medium",
            "category": category if category in CATEGORIES else "bug",
            "title": str(item.get("title", "")).strip()[:200] or "Issue",
            "explanation": str(item.get("explanation", "")).strip()[:3000],
            "suggestion": str(item.get("suggestion", "")).strip()[:3000],
            "fixed_code": str(item.get("fixed_code", "") or "").strip()[:4000],
            "source": "ai-review",
            "confirmed_by_test": None,
        })
    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: (order[f["severity"]], f["line"] or 0))
    return findings


def _triage(filename: str, source: str, test_code: str, execution: dict) -> tuple[list[dict], Optional[str]]:
    failing = [t for t in execution["tests"] if t["status"] in {"failed", "error"}][:25]
    if not failing:
        return [], None
    listing = "\n\n".join(f"TEST {t['name']}\n{t['message'][:1500]}" for t in failing)
    prompt = "\n".join([
        f"File under test: {filename}", "", "Source:", numbered(source[:MAX_REVIEW_CHARS]), "",
        "Test file:", test_code[:40_000], "", "Failing tests and their output:", listing, "",
        'Return JSON: {"failures": [{"test": "<test name exactly as given>", '
        '"verdict": "code_defect|test_error|environment", "line": <line in the source or null>, '
        '"explanation": "<one or two sentences>"}]}',
    ])
    try:
        data, _model = gemini_client.generate_json(TRIAGE_SYSTEM, prompt, max_output_tokens=8192)
    except gemini_client.GeminiError as exc:
        return [], str(exc)
    out = []
    for item in (data.get("failures") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        verdict = item.get("verdict")
        out.append({
            "test": str(item.get("test", "")),
            "verdict": verdict if verdict in {"code_defect", "test_error", "environment"} else "test_error",
            "line": item.get("line") if isinstance(item.get("line"), int) else None,
            "explanation": str(item.get("explanation", ""))[:1500],
        })
    return out, None


def _apply_triage(findings: list[dict], execution: dict, triage: list[dict], n_lines: int) -> None:
    by_name = {t["test"]: t for t in triage}
    for test in execution["tests"]:
        verdict = by_name.get(test["name"])
        if not verdict:
            # Names can come back shortened (e.g. without class prefix); match on suffix.
            verdict = next((v for n, v in by_name.items() if n and (test["name"].endswith(n) or n.endswith(test["name"]))), None)
        if not verdict:
            continue
        test["triage"] = verdict
        if verdict["verdict"] != "code_defect":
            continue
        line = verdict["line"] if verdict["line"] and 1 <= verdict["line"] <= n_lines else None
        match = next((f for f in findings if line and f["line"] and f["line"] - 2 <= line <= (f["end_line"] or f["line"]) + 2), None)
        if match:
            match["confirmed_by_test"] = test["name"]
        else:
            findings.append({
                "line": line, "end_line": line, "severity": "high", "category": "bug",
                "title": f"Failing test: {test['name']}", "explanation": verdict["explanation"],
                "suggestion": "", "fixed_code": "", "source": "failing-test", "confirmed_by_test": test["name"],
            })


def count_file(report: dict) -> dict:
    counts = {s: 0 for s in SEVERITIES}
    for f in report["review"].get("findings", []):
        counts[f["severity"]] += 1
    diags = report["static"]["diagnostics"]
    execution = report["tests"]["execution"]
    counts.update({
        "diagnostic_errors": sum(d["severity"] == "error" for d in diags),
        "diagnostic_warnings": sum(d["severity"] == "warning" for d in diags),
        "tests_passed": execution["passed"],
        "tests_failed": execution["failed"] + execution["errors"],
        "tests_executed": bool(execution["executed"]),
        "tests_generated": bool(report["tests"]["code"]),
    })
    return counts


def review_file(filename: str, source: str, *, run_tests: bool = True,
                progress: Optional[Callable[[str], None]] = None) -> dict:
    note = progress or (lambda _msg: None)
    language = detect_language(filename, source)
    lines = source.splitlines()
    truncated = len(lines) > MAX_REVIEW_LINES or len(source) > MAX_REVIEW_CHARS
    review_source = "\n".join(lines[:MAX_REVIEW_LINES])[:MAX_REVIEW_CHARS] if truncated else source

    note(f"{filename}: compiler and linter checks")
    static = run_static_checks(filename, source, language)

    harness = executors.build_harness(language, filename, source)
    review: dict = {"status": "ok", "error": None, "summary": "", "findings": [], "model": None,
                    "detected_language": None}
    test_code, framework = "", language.test_framework

    note(f"{filename}: AI review")
    try:
        data, model = gemini_client.generate_json(
            REVIEW_SYSTEM,
            build_review_prompt(filename, review_source, language, harness, static["diagnostics"], truncated),
        )
        if not isinstance(data, dict):
            raise gemini_client.GeminiError("Gemini returned JSON that is not an object.")
        review.update({
            "model": model,
            "summary": str(data.get("summary", "")).strip()[:2000],
            "findings": _normalise_findings(data.get("findings"), len(lines)),
            "detected_language": str(data.get("language", "")).strip()[:60] or None,
        })
        tests = data.get("tests") if isinstance(data.get("tests"), dict) else {}
        test_code = gemini_client.strip_fences(str(tests.get("code", "") or ""))
        framework = str(tests.get("framework", "") or framework)[:80]
    except gemini_client.GeminiError as exc:
        review.update({"status": "error", "error": str(exc)})

    if not test_code:
        execution = executors.not_executed(
            "No tests were generated because the AI review did not complete." if review["status"] == "error"
            else "The model did not return any tests."
        )
    elif not run_tests:
        execution = executors.not_executed("Test execution was turned off for this run.")
    else:
        note(f"{filename}: running generated tests")
        execution = executors.run_tests(language, harness, source, test_code)

    triage_error = None
    if execution["executed"] and (execution["failed"] or execution["errors"]):
        note(f"{filename}: triaging failing tests")
        triage, triage_error = _triage(filename, review_source, test_code, execution)
        _apply_triage(review["findings"], execution, triage, len(lines))

    report = {
        "file": filename,
        "language": language.name if language.id != "other" else (review["detected_language"] or "Unknown"),
        "language_id": language.id,
        "lines": len(lines),
        "truncated": truncated,
        "source": source[:MAX_SOURCE_IN_REPORT],
        "static": static,
        "review": review,
        "tests": {
            "framework": framework,
            "file": harness.test_file,
            "code": test_code,
            "execution": execution,
            "triage_error": triage_error,
        },
    }
    report["counts"] = count_file(report)
    return report


def summarize(file_reports: list[dict]) -> dict:
    """Aggregate counts for a set of file reports (stored as the run summary)."""
    total = {s: 0 for s in SEVERITIES}
    total.update({"diagnostic_errors": 0, "diagnostic_warnings": 0, "tests_passed": 0, "tests_failed": 0,
                  "files": len(file_reports), "files_with_tests_executed": 0, "ai_errors": 0})
    languages: dict[str, int] = {}
    for rep in file_reports:
        c = rep["counts"]
        for key in (*SEVERITIES, "diagnostic_errors", "diagnostic_warnings", "tests_passed", "tests_failed"):
            total[key] += c[key]
        total["files_with_tests_executed"] += int(c["tests_executed"])
        total["ai_errors"] += int(rep["review"]["status"] == "error")
        languages[rep["language"]] = languages.get(rep["language"], 0) + 1
    total["findings"] = sum(total[s] for s in SEVERITIES)
    total["languages"] = languages
    total["models"] = sorted({rep["review"]["model"] for rep in file_reports if rep["review"].get("model")})
    return total
