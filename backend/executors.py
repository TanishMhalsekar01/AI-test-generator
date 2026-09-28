"""
Execute generated test suites in the sandbox and parse per-test results.

Supported runners (when the toolchain is installed on the server):
  Python      pytest + coverage (JUnit XML)
  JavaScript  node --test (TAP) + built-in coverage
  Go          go test -json -cover
  Ruby        Minitest (verbose output)
  Rust        rustc --test (libtest output)

Other languages get generated tests that are returned to the user but are
reported as "not executed" — they are never counted as passed.
"""

import json
import keyword
import re
import sys
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import yaml

import sandbox
from languages import Language

TEST_TIMEOUT = 90
MAX_OUTPUT = 8000


@dataclass
class Harness:
    """How the code under test is laid out on disk and how tests must import it."""
    target_file: str
    test_file: str
    instructions: str
    executable: bool
    reason: str = ""
    append_to_source: str = ""   # JS export shim


# ---------------------------------------------------------------------------
# Harness construction (also feeds the prompt so tests match the layout)
# ---------------------------------------------------------------------------

def _python_module_name(filename: str) -> str:
    stem = Path(filename).stem
    stdlib = getattr(sys, "stdlib_module_names", set())
    if (not stem.isidentifier() or keyword.iskeyword(stem) or stem in stdlib
            or stem.startswith("test") or stem in {"conftest", "pytest", "target"}):
        return "module_under_test"
    return stem


_JS_DECL_RE = re.compile(
    r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function\s*\*?\s*(?P<fn>[A-Za-z_$][\w$]*)|"
    r"class\s+(?P<cls>[A-Za-z_$][\w$]*)|(?:const|let|var)\s+(?P<var>[A-Za-z_$][\w$]*)\s*=)",
    re.M,
)
_ESM_RE = re.compile(r"^\s*(?:import\s+[\w{*]|import\s+['\"]|export\s+)", re.M)


def _js_names(source: str) -> tuple[list[str], list[str]]:
    """Return (all top-level declared names, names already exported via `export`)."""
    names, exported = [], []
    for m in _JS_DECL_RE.finditer(source):
        name = m.group("fn") or m.group("cls") or m.group("var")
        if name and name not in names:
            names.append(name)
            if m.group(0).startswith("export"):
                exported.append(name)
    return names, exported


def build_harness(language: Language, filename: str, source: str) -> Harness:
    base = Path(filename).name
    stem = Path(base).stem or "module"

    if language.id == "python":
        module = _python_module_name(base)
        return Harness(
            target_file=f"{module}.py",
            test_file=f"test_{module}_generated.py",
            instructions=(
                f"Use pytest. The code under test is saved as `{module}.py` in the same directory; "
                f"import it with `from {module} import ...` or `import {module}`. "
                "Use pytest.raises for expected exceptions and pytest.mark.parametrize for input tables."
            ),
            executable=True,
        )

    if language.id == "javascript":
        esm = bool(_ESM_RE.search(source)) or base.endswith(".mjs")
        names, exported = _js_names(source)
        ext = "mjs" if esm else "cjs"
        safe_stem = re.sub(r"[^\w.-]", "_", stem)
        target = f"{safe_stem}.{ext}"
        if esm:
            extra = [n for n in names if n not in exported]
            shim = f"\nexport {{ {', '.join(extra)} }};\n" if extra else ""
            imp = f"import {{ {', '.join(names)} }} from './{target}';" if names else f"import * as mod from './{target}';"
        else:
            shim = ("\n;if (typeof module !== 'undefined') { Object.assign(module.exports, { "
                    + ", ".join(names) + " }); }\n") if names else ""
            imp = f"const {{ {', '.join(names)} }} = require('./{target}');" if names else f"const mod = require('./{target}');"
        runner = "node:test" if sandbox.which("node") else ""
        return Harness(
            target_file=target,
            test_file=f"{safe_stem}.generated.test.{ext}",
            instructions=(
                "Use Node's built-in test runner only (no Jest/Mocha): "
                + ("`import test from 'node:test'; import assert from 'node:assert/strict';` "
                   if esm else "`const test = require('node:test'); const assert = require('node:assert/strict');` ")
                + f"Import the code under test exactly like this: `{imp}`"
                + (f" Available top-level names: {', '.join(names)}." if names else "")
            ),
            executable=bool(runner),
            reason="" if runner else "Node.js is not installed on this server.",
            append_to_source=shim,
        )

    if language.id == "go":
        pkg = re.search(r"^\s*package\s+(\w+)", source, re.M)
        if not pkg:
            return Harness(base, "", "Use the standard testing package.", False, "No `package` clause found.")
        if base.endswith("_test.go"):
            return Harness(base, "", "", False, "This file is already a Go test file.")
        return Harness(
            target_file=base if base.endswith(".go") else f"{stem}.go",
            test_file=f"{stem}_generated_test.go",
            instructions=(
                f"Write a Go test file in `package {pkg.group(1)}` using only the standard `testing` package "
                "(no third-party imports). Tests live in the same package and may call unexported identifiers. "
                "Prefer table-driven tests with t.Run."
            ),
            executable=language.can_execute_tests,
            reason="" if language.can_execute_tests else "Go is not installed on this server.",
        )

    if language.id == "ruby":
        safe = re.sub(r"\W", "_", stem)
        return Harness(
            target_file=f"{safe}.rb",
            test_file=f"test_{safe}_generated.rb",
            instructions=(
                "Use Minitest: start with `require 'minitest/autorun'` and "
                f"`require_relative '{safe}'`. Define `class Test... < Minitest::Test` with `test_` methods."
            ),
            executable=language.can_execute_tests,
            reason="" if language.can_execute_tests else "Ruby is not installed on this server.",
        )

    if language.id == "rust":
        return Harness(
            target_file="lib_under_test.rs",
            test_file="(appended to the source file)",
            instructions=(
                "Write ONLY a test module that will be appended to the end of the same source file: "
                "`#[cfg(test)] mod generated_tests { use super::*; #[test] fn ...() { ... } }`. "
                "Standard library only; use #[should_panic] for expected panics."
            ),
            executable=language.can_execute_tests,
            reason="" if language.can_execute_tests else "rustc is not installed on this server.",
        )

    return Harness(
        target_file=base,
        test_file="",
        instructions=(
            f"Use {language.test_framework}, the conventional framework for {language.name}. "
            "Include the imports/package declarations needed for the test file to compile."
        ),
        executable=False,
        reason=f"This server has no sandboxed test runner for {language.name}; run the generated tests locally.",
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _result(runner: str, **kw) -> dict:
    base = {
        "executed": True, "runner": runner, "reason": None, "status": "passed", "tests": [],
        "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "coverage_percent": None,
        "duration_ms": 0, "output": "",
    }
    base.update(kw)
    return base


def _finalise(res: dict) -> dict:
    tests = res["tests"]
    res["passed"] = sum(t["status"] == "passed" for t in tests)
    res["failed"] = sum(t["status"] == "failed" for t in tests)
    res["errors"] = sum(t["status"] == "error" for t in tests)
    res["skipped"] = sum(t["status"] == "skipped" for t in tests)
    if res["status"] != "error":
        if not tests:
            res["status"] = "error"
            res["reason"] = res.get("reason") or "No tests were collected; see the runner output."
        elif res["failed"] or res["errors"]:
            res["status"] = "failed"
        else:
            res["status"] = "passed"
    if len(res["output"]) > MAX_OUTPUT:
        res["output"] = "…" + res["output"][-MAX_OUTPUT:]
    return res


def not_executed(reason: str, runner: str = "") -> dict:
    return _result(runner, executed=False, status="not_executed", reason=reason)


def run_tests(language: Language, harness: Harness, source: str, test_code: str) -> dict:
    if not harness.executable:
        return not_executed(harness.reason or "Not executable on this server.")
    if not test_code.strip():
        return not_executed("The model did not return any test code.")
    workdir = sandbox.make_workdir()
    try:
        runner = {
            "python": _run_python, "javascript": _run_node, "go": _run_go,
            "ruby": _run_ruby, "rust": _run_rust,
        }[language.id]
        return _finalise(runner(workdir, harness, source, test_code))
    finally:
        sandbox.remove_workdir(workdir)


def _write(workdir: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        (workdir / name).write_text(content, encoding="utf-8")
    sandbox.prepare_for_sandbox(workdir)


# -- Python -----------------------------------------------------------------

def parse_junit(path: Path) -> list[dict]:
    tests = []
    root = ET.parse(path).getroot()
    for tc in root.iter("testcase"):
        name = tc.get("name", "?")
        status, message = "passed", ""
        for child in tc:
            if child.tag in {"failure", "error"}:
                status = "failed" if child.tag == "failure" else "error"
                message = ((child.get("message") or "") + "\n" + (child.text or "")).strip()
            elif child.tag == "skipped":
                status, message = "skipped", child.get("message") or ""
        tests.append({"name": name, "status": status, "message": message[:4000]})
    return tests


def _run_python(workdir: Path, h: Harness, source: str, test_code: str) -> dict:
    module = Path(h.target_file).stem
    _write(workdir, {h.target_file: source, h.test_file: test_code})
    proc = sandbox.run(
        [sandbox.PYTHON, "-m", "pytest", h.test_file, "-q", "-p", "no:cacheprovider", "--no-header",
         "--junitxml=junit.xml", f"--cov={module}", "--cov-report=json:coverage.json"],
        workdir, timeout=TEST_TIMEOUT,
    )
    res = _result("pytest", duration_ms=proc.duration_ms, output=proc.output)
    if proc.timed_out:
        return _result("pytest", status="error", reason=f"Tests timed out after {TEST_TIMEOUT}s.", output=proc.output)
    junit = workdir / "junit.xml"
    if junit.exists():
        try:
            res["tests"] = parse_junit(junit)
        except ET.ParseError:
            pass
    cov = workdir / "coverage.json"
    if cov.exists() and res["tests"]:
        try:
            res["coverage_percent"] = round(float(json.loads(cov.read_text())["totals"]["percent_covered"]), 1)
        except (KeyError, ValueError, TypeError):
            pass
    return res


def run_api_tests(test_code: str, base_url: str, name: str = "test_api_generated.py") -> dict:
    """Run a generated API test module against a live server (BASE_URL)."""
    workdir = sandbox.make_workdir()
    try:
        _write(workdir, {name: test_code})
        proc = sandbox.run(
            [sandbox.PYTHON, "-m", "pytest", name, "-q", "-p", "no:cacheprovider", "--no-header",
             "--junitxml=junit.xml"],
            workdir, timeout=TEST_TIMEOUT, env_extra={"BASE_URL": base_url},
        )
        if proc.timed_out:
            return _finalise(_result("pytest", status="error", reason=f"Tests timed out after {TEST_TIMEOUT}s.",
                                     output=proc.output))
        res = _result("pytest", duration_ms=proc.duration_ms, output=proc.output)
        junit = workdir / "junit.xml"
        if junit.exists():
            try:
                res["tests"] = parse_junit(junit)
            except ET.ParseError:
                pass
        return _finalise(res)
    finally:
        sandbox.remove_workdir(workdir)


# -- JavaScript -------------------------------------------------------------

_TAP_RE = re.compile(r"^(?P<indent>\s*)(?P<ok>ok|not ok) \d+ - (?P<name>.+?)(?:\s+#\s*(?P<dir>SKIP|TODO)\b.*)?$")


def parse_tap(output: str) -> list[dict]:
    tests = []
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        m = _TAP_RE.match(lines[i])
        if not m:
            i += 1
            continue
        block: list[str] = []
        j = i + 1
        if j < len(lines) and lines[j].strip() == "---":
            j += 1
            while j < len(lines) and lines[j].strip() != "...":
                block.append(lines[j])
                j += 1
        raw_block = textwrap.dedent("\n".join(block))
        i = j + 1 if block else i + 1
        if "type: 'suite'" in raw_block:
            continue
        status = "passed" if m.group("ok") == "ok" else "failed"
        if m.group("dir") == "SKIP":
            status = "skipped"
        message = ""
        if status == "failed":
            try:
                data = yaml.safe_load(raw_block) or {}
                parts = [str(data.get("error", "")).strip()]
                if "expected" in data or "actual" in data:
                    parts.append(f"expected: {data.get('expected')!r}\nactual:   {data.get('actual')!r}")
                message = "\n".join(p for p in parts if p)
            except yaml.YAMLError:
                message = raw_block
        tests.append({"name": m.group("name").strip(), "status": status, "message": message[:4000]})
    return tests


def _run_node(workdir: Path, h: Harness, source: str, test_code: str) -> dict:
    _write(workdir, {h.target_file: source + h.append_to_source, h.test_file: test_code})
    proc = sandbox.run(["node", "--test", "--test-reporter=tap", "--experimental-test-coverage", h.test_file],
                       workdir, timeout=TEST_TIMEOUT)
    if proc.timed_out:
        return _result("node:test", status="error", reason=f"Tests timed out after {TEST_TIMEOUT}s.", output=proc.output)
    res = _result("node:test", duration_ms=proc.duration_ms, output=proc.output, tests=parse_tap(proc.stdout))
    for line in proc.stdout.splitlines():
        m = re.match(r"^#\s*(\S+)\s*\|\s*([\d.]+)\s*\|", line)
        if m and m.group(1) == h.target_file:
            res["coverage_percent"] = float(m.group(2))
    return res


# -- Go ---------------------------------------------------------------------

def _run_go(workdir: Path, h: Harness, source: str, test_code: str) -> dict:
    # Go ignores a go.mod placed directly in the system temp root, so use a subdirectory.
    moddir = workdir / "gomod"
    moddir.mkdir()
    _write(moddir, {"go.mod": "module sandbox\n\ngo 1.21\n", h.target_file: source, h.test_file: test_code})
    sandbox.prepare_for_sandbox(workdir)
    proc = sandbox.run(["go", "test", "-json", "-count=1", "-cover", "-vet=off", "./..."], moddir, timeout=180)
    if proc.timed_out:
        return _result("go test", status="error", reason="go test timed out.", output=proc.output)
    tests: dict[str, dict] = {}
    outputs: dict[str, list[str]] = {}
    plain: list[str] = []
    coverage = None
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            plain.append(line)
            continue
        name, action = ev.get("Test"), ev.get("Action")
        text = ev.get("Output", "")
        if action in {"output", "build-output"}:
            if name:
                outputs.setdefault(name, []).append(text)
            else:
                plain.append(text.rstrip("\n"))
            m = re.search(r"coverage: ([\d.]+)% of statements", text)
            if m:
                coverage = float(m.group(1))
        elif name and action in {"pass", "fail", "skip"}:
            status = {"pass": "passed", "fail": "failed", "skip": "skipped"}[action]
            tests[name] = {"name": name, "status": status, "message": ""}
    for name, t in tests.items():
        if t["status"] == "failed":
            t["message"] = "".join(outputs.get(name, []))[-4000:].strip()
    # Parent tests fail when a subtest fails; keep only leaves to avoid double counting.
    leaves = [t for n, t in tests.items() if not any(o.startswith(n + "/") for o in tests)]
    output = "\n".join(plain + [proc.stderr]).strip()
    if not leaves:
        return _result("go test", status="error", output=output,
                       reason="The package did not build; see compiler output.")
    return _result("go test", tests=leaves, coverage_percent=coverage, duration_ms=proc.duration_ms, output=output)


# -- Ruby -------------------------------------------------------------------

def _run_ruby(workdir: Path, h: Harness, source: str, test_code: str) -> dict:
    _write(workdir, {h.target_file: source, h.test_file: test_code})
    proc = sandbox.run(["ruby", h.test_file, "-v"], workdir, timeout=TEST_TIMEOUT)
    if proc.timed_out:
        return _result("minitest", status="error", reason="Tests timed out.", output=proc.output)
    out = proc.output
    tests = []
    for m in re.finditer(r"^(?P<name>[\w:]+#\S+) = [\d.]+ s = (?P<r>[.FES])$", out, re.M):
        status = {".": "passed", "F": "failed", "E": "error", "S": "skipped"}[m.group("r")]
        tests.append({"name": m.group("name"), "status": status, "message": ""})
    details = re.split(r"\n\s*\d+\) (?:Failure|Error|Skipped):\n", out)
    for chunk in details[1:]:
        head, _, body = chunk.partition("\n")
        name = re.split(r" \[|:$", head.strip())[0]
        for t in tests:
            if t["name"] == name:
                t["message"] = (head + "\n" + body).split("\n\n")[0].strip()[:4000]
    return _result("minitest", tests=tests, duration_ms=proc.duration_ms, output=out)


# -- Rust -------------------------------------------------------------------

def _run_rust(workdir: Path, h: Harness, source: str, test_code: str) -> dict:
    _write(workdir, {h.target_file: source + "\n\n" + test_code + "\n"})
    build = sandbox.run(["rustc", "--edition", "2021", "--test", "-o", "testbin", h.target_file],
                        workdir, timeout=180)
    if build.returncode != 0:
        return _result("rustc --test", status="error", reason="The test binary did not compile.", output=build.output)
    proc = sandbox.run(["./testbin", "--test-threads=1"], workdir, timeout=TEST_TIMEOUT)
    if proc.timed_out:
        return _result("rustc --test", status="error", reason="Tests timed out.", output=proc.output)
    tests = []
    for m in re.finditer(r"^test (?P<name>\S+) \.\.\. (?P<r>ok|FAILED|ignored)", proc.stdout, re.M):
        status = {"ok": "passed", "FAILED": "failed", "ignored": "skipped"}[m.group("r")]
        tests.append({"name": m.group("name"), "status": status, "message": ""})
    for m in re.finditer(r"^---- (?P<name>\S+) stdout ----\n(?P<body>.*?)(?=^---- |^failures:|\Z)",
                         proc.stdout, re.M | re.S):
        for t in tests:
            if t["name"] == m.group("name"):
                t["message"] = m.group("body").strip()[:4000]
    return _result("rustc --test", tests=tests, duration_ms=build.duration_ms + proc.duration_ms, output=proc.output)


def available_runners() -> dict[str, bool]:
    return {
        "python": True,
        "javascript": sandbox.which("node") is not None,
        "go": sandbox.which("go") is not None,
        "ruby": sandbox.which("ruby") is not None,
        "rust": sandbox.which("rustc") is not None,
    }

