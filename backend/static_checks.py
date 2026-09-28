"""
Deterministic diagnostics from real compilers, interpreters and linters.

Each checker runs only if its toolchain is installed on the server. When a
tool is missing the result says so ("unavailable") instead of guessing.
"""

import ast
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

import yaml

import sandbox
from languages import Language

TOOL_TIMEOUT = 45


@dataclass
class Diagnostic:
    line: Optional[int]
    column: Optional[int]
    severity: str  # error | warning | info
    message: str
    tool: str
    code: Optional[str] = None


@dataclass
class ToolRun:
    tool: str
    status: str  # ran | unavailable | timeout | skipped
    detail: str = ""


# Messages that reflect the sandbox (missing third-party packages / headers),
# not a defect in the submitted code.
_ENV_PATTERNS = re.compile(
    r"No such file or directory|cannot find module|Cannot find module|can't find crate|"
    r"package .* does not exist|no required module provides package|cannot find symbol.*import|"
    r"import-error|E0401|E0432|E0463|TS2307|TS2580|TS2591|TS7016",
    re.IGNORECASE,
)

_GNU_RE = re.compile(
    r"^(?:vet: |ruby: )?(?P<file>[^:\n]+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?:(?P<sev>fatal error|error|warning|note)\s*:)?\s*(?P<msg>.+)$"
)


def _severity(word: Optional[str], default: str = "error") -> str:
    word = (word or "").lower()
    if "error" in word:
        return "error"
    if word == "warning":
        return "warning"
    if word == "note":
        return "info"
    return default


def _env_adjust(diag: Diagnostic) -> Diagnostic:
    if _ENV_PATTERNS.search(diag.message) or (diag.code and _ENV_PATTERNS.search(diag.code)):
        diag.severity = "info"
        diag.message += " (dependency not available in the analysis sandbox)"
    return diag


def _parse_gnu(output: str, filename: str, tool: str, default_sev: str = "error") -> list[Diagnostic]:
    diags = []
    for line in output.splitlines():
        m = _GNU_RE.match(line.strip())
        if not m or not m.group("file").endswith(filename):
            continue
        if m.group("sev") == "note":
            continue
        diags.append(_env_adjust(Diagnostic(
            line=int(m.group("line")),
            column=int(m.group("col")) if m.group("col") else None,
            severity=_severity(m.group("sev"), default_sev),
            message=m.group("msg").strip(),
            tool=tool,
        )))
    return diags


# ---------------------------------------------------------------------------
# Per-language checkers. Each returns (diagnostics, tool_runs).
# ---------------------------------------------------------------------------

Checker = Callable[[Path, str, str], tuple[list[Diagnostic], list[ToolRun]]]


def _check_python(workdir: Path, filename: str, source: str):
    diags: list[Diagnostic] = []
    runs = [ToolRun("python compile", "ran")]
    try:
        ast.parse(source, filename=filename)
    except SyntaxError as exc:
        diags.append(Diagnostic(exc.lineno, exc.offset, "error", f"SyntaxError: {exc.msg}", "python compile"))
        return diags, runs  # pylint would only repeat the syntax error

    if sandbox.which("pylint") is None:
        runs.append(ToolRun("pylint", "unavailable", "pylint is not installed"))
        return diags, runs
    res = sandbox.run(
        [sandbox.PYTHON, "-m", "pylint", "--output-format=json", "--score=n", "--persistent=n",
         "--disable=C,R,fixme,import-error,no-name-in-module", filename],
        workdir, timeout=TOOL_TIMEOUT,
    )
    if res.timed_out:
        runs.append(ToolRun("pylint", "timeout"))
        return diags, runs
    runs.append(ToolRun("pylint", "ran"))
    try:
        messages = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        messages = []
    for msg in messages:
        sev = {"error": "error", "fatal": "error", "warning": "warning"}.get(msg.get("type"), "info")
        diags.append(_env_adjust(Diagnostic(
            line=msg.get("line"), column=(msg.get("column") or 0) + 1, severity=sev,
            message=f"{msg.get('message')} ({msg.get('symbol')})", tool="pylint", code=msg.get("message-id"),
        )))
    return diags, runs


def _check_javascript(workdir: Path, filename: str, source: str):
    if sandbox.which("node") is None:
        return [], [ToolRun("node --check", "unavailable", "Node.js is not installed")]
    res = sandbox.run(["node", "--check", filename], workdir, timeout=TOOL_TIMEOUT)
    if res.timed_out:
        return [], [ToolRun("node --check", "timeout")]
    diags = []
    if res.returncode != 0:
        lines = res.stderr.splitlines()
        line_no = None
        for text in lines:
            m = re.match(rf".*{re.escape(filename)}:(\d+)$", text.strip())
            if m:
                line_no = int(m.group(1))
                break
        message = next((t.strip() for t in lines if re.match(r"^\w*Error:", t.strip())), res.stderr.strip()[:300])
        diags.append(Diagnostic(line_no, None, "error", message, "node --check"))
    return diags, [ToolRun("node --check", "ran")]


def _check_typescript(workdir: Path, filename: str, source: str):
    if sandbox.which("tsc") is None:
        return [], [ToolRun("tsc", "unavailable", "TypeScript compiler is not installed")]
    cmd = ["tsc", "--noEmit", "--skipLibCheck", "--target", "es2022", "--lib", "es2022,dom",
           "--moduleResolution", "node", "--strict", "--pretty", "false"]
    if filename.endswith(".tsx"):
        cmd += ["--jsx", "preserve"]
    res = sandbox.run(cmd + [filename], workdir, timeout=90)
    if res.timed_out:
        return [], [ToolRun("tsc", "timeout")]
    diags = []
    for line in res.stdout.splitlines():
        m = re.match(r"^(.+?)\((\d+),(\d+)\): (error|warning) (TS\d+): (.+)$", line.strip())
        if m and m.group(1).endswith(filename):
            diags.append(_env_adjust(Diagnostic(int(m.group(2)), int(m.group(3)), _severity(m.group(4)),
                                                m.group(6), "tsc", m.group(5))))
    return diags, [ToolRun("tsc", "ran")]


def _check_go(workdir: Path, filename: str, source: str):
    if sandbox.which("gofmt") is None:
        return [], [ToolRun("gofmt", "unavailable", "Go toolchain is not installed")]
    runs = []
    res = sandbox.run(["gofmt", "-e", "-l", filename], workdir, timeout=TOOL_TIMEOUT)
    diags = _parse_gnu(res.stderr, filename, "gofmt")
    runs.append(ToolRun("gofmt", "ran"))
    if diags or sandbox.which("go") is None:
        return diags, runs
    # Go ignores a go.mod placed directly in the system temp root, so vet from a subdirectory.
    moddir = workdir / "gomod"
    moddir.mkdir(exist_ok=True)
    (moddir / "go.mod").write_text("module sandbox\n\ngo 1.21\n", encoding="utf-8")
    (moddir / filename).write_text(source, encoding="utf-8")
    sandbox.prepare_for_sandbox(workdir)
    res = sandbox.run(["go", "vet", "./..."], moddir, timeout=90)
    if res.timed_out:
        runs.append(ToolRun("go vet", "timeout"))
        return diags, runs
    runs.append(ToolRun("go vet", "ran"))
    diags += _parse_gnu(res.stderr, filename, "go vet", default_sev="warning")
    return diags, runs


def _gcc_like(compiler: str, std: str):
    def check(workdir: Path, filename: str, source: str):
        if sandbox.which(compiler) is None:
            return [], [ToolRun(compiler, "unavailable", f"{compiler} is not installed")]
        res = sandbox.run([compiler, "-fsyntax-only", "-Wall", "-Wextra", f"-std={std}",
                           "-fdiagnostics-color=never", filename], workdir, timeout=TOOL_TIMEOUT)
        if res.timed_out:
            return [], [ToolRun(compiler, "timeout")]
        return _parse_gnu(res.stderr, filename, compiler), [ToolRun(compiler, "ran")]
    return check


def _check_java(workdir: Path, filename: str, source: str):
    if sandbox.which("javac") is None:
        return [], [ToolRun("javac", "unavailable", "JDK is not installed")]
    res = sandbox.run(["javac", "-Xlint:all", "-d", "out", filename], workdir, timeout=90)
    if res.timed_out:
        return [], [ToolRun("javac", "timeout")]
    diags = []
    lines = res.stderr.splitlines()
    for i, line in enumerate(lines):
        m = re.match(rf"^{re.escape(filename)}:(\d+): (error|warning): (.+)$", line)
        if not m:
            continue
        col = None
        for follow in lines[i + 1:i + 3]:
            if follow.strip() == "^":
                col = follow.index("^") + 1
                break
        diags.append(_env_adjust(Diagnostic(int(m.group(1)), col, _severity(m.group(2)), m.group(3), "javac")))
    return diags, [ToolRun("javac", "ran")]


def _check_ruby(workdir: Path, filename: str, source: str):
    if sandbox.which("ruby") is None:
        return [], [ToolRun("ruby -wc", "unavailable", "Ruby is not installed")]
    res = sandbox.run(["ruby", "-wc", filename], workdir, timeout=TOOL_TIMEOUT)
    diags = []
    for line in res.stderr.splitlines():
        m = re.match(rf"^(?:ruby: )?{re.escape(filename)}:(\d+):\s*(warning: )?(.+)$", line.strip())
        if m:
            diags.append(Diagnostic(int(m.group(1)), None, "warning" if m.group(2) else "error",
                                    m.group(3).strip(), "ruby -wc"))
    return diags, [ToolRun("ruby -wc", "ran")]


def _check_php(workdir: Path, filename: str, source: str):
    if sandbox.which("php") is None:
        return [], [ToolRun("php -l", "unavailable", "PHP is not installed")]
    res = sandbox.run(["php", "-l", filename], workdir, timeout=TOOL_TIMEOUT)
    diags = []
    for line in res.output.splitlines():
        m = re.match(r"^(?:PHP )?(Parse error|Fatal error|Warning|Deprecated):\s*(.+?) in .+ on line (\d+)$", line.strip())
        if m:
            sev = "warning" if m.group(1) in {"Warning", "Deprecated"} else "error"
            diags.append(Diagnostic(int(m.group(3)), None, sev, f"{m.group(1)}: {m.group(2)}", "php -l"))
    return diags, [ToolRun("php -l", "ran")]


def _check_rust(workdir: Path, filename: str, source: str):
    if sandbox.which("rustc") is None:
        return [], [ToolRun("rustc", "unavailable", "Rust toolchain is not installed")]
    res = sandbox.run(["rustc", "--edition", "2021", "--crate-type", "lib", "--error-format=json",
                       "--emit=metadata", "-o", "out.rmeta", filename], workdir, timeout=90)
    if res.timed_out:
        return [], [ToolRun("rustc", "timeout")]
    diags = []
    for line in res.stderr.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        level = msg.get("level")
        if level not in {"error", "warning"} or not msg.get("spans"):
            continue
        span = next((s for s in msg["spans"] if s.get("is_primary")), msg["spans"][0])
        code = (msg.get("code") or {}).get("code")
        diags.append(_env_adjust(Diagnostic(span.get("line_start"), span.get("column_start"), level,
                                            msg.get("message", ""), "rustc", code)))
    return diags, [ToolRun("rustc", "ran")]


def _check_shell(workdir: Path, filename: str, source: str):
    diags: list[Diagnostic] = []
    runs = []
    if sandbox.which("bash"):
        res = sandbox.run(["bash", "-n", filename], workdir, timeout=TOOL_TIMEOUT)
        runs.append(ToolRun("bash -n", "ran"))
        for line in res.stderr.splitlines():
            m = re.match(rf"^{re.escape(filename)}: line (\d+): (.+)$", line.strip())
            if m:
                diags.append(Diagnostic(int(m.group(1)), None, "error", m.group(2), "bash -n"))
    if sandbox.which("shellcheck"):
        res = sandbox.run(["shellcheck", "-f", "json", filename], workdir, timeout=TOOL_TIMEOUT)
        runs.append(ToolRun("shellcheck", "ran"))
        try:
            for item in json.loads(res.stdout or "[]"):
                sev = {"error": "error", "warning": "warning"}.get(item.get("level"), "info")
                diags.append(Diagnostic(item.get("line"), item.get("column"), sev, item.get("message", ""),
                                        "shellcheck", f"SC{item.get('code')}"))
        except json.JSONDecodeError:
            pass
    else:
        runs.append(ToolRun("shellcheck", "unavailable", "shellcheck is not installed"))
    return diags, runs


CHECKERS: dict[str, list[tuple[str, Checker]]] = {
    "python": [("python", _check_python)],
    "javascript": [("node", _check_javascript)],
    "typescript": [("tsc", _check_typescript)],
    "go": [("go", _check_go)],
    "c": [("gcc", _gcc_like("gcc", "c17"))],
    "cpp": [("g++", _gcc_like("g++", "c++20"))],
    "java": [("javac", _check_java)],
    "ruby": [("ruby", _check_ruby)],
    "php": [("php", _check_php)],
    "rust": [("rustc", _check_rust)],
    "shell": [("bash", _check_shell)],
}


def run_static_checks(filename: str, source: str, language: Language) -> dict:
    """Run every available checker for *language* on *source*."""
    checkers = CHECKERS.get(language.id)
    if not checkers:
        return {
            "tools": [asdict(ToolRun("compiler/linter", "unavailable",
                                     f"No compiler or linter for {language.name} is installed on this server."))],
            "diagnostics": [],
        }
    safe_name = Path(filename).name or "source"
    if language.id == "c" and safe_name.endswith(".h"):
        safe_name = safe_name[:-2] + ".c"
    workdir = sandbox.make_workdir()
    try:
        (workdir / safe_name).write_text(source, encoding="utf-8")
        sandbox.prepare_for_sandbox(workdir)
        diags: list[Diagnostic] = []
        tools: list[ToolRun] = []
        for _, checker in checkers:
            d, t = checker(workdir, safe_name, source)
            diags += d
            tools += t
    finally:
        sandbox.remove_workdir(workdir)
    diags.sort(key=lambda d: (d.line or 0, d.column or 0))
    return {"tools": [asdict(t) for t in tools], "diagnostics": [asdict(d) for d in diags[:200]]}


# ---------------------------------------------------------------------------
# Structured-data syntax checks (also used by the API & specs pipeline)
# ---------------------------------------------------------------------------

def json_syntax_error(text: str) -> Optional[dict]:
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return {"line": exc.lineno, "column": exc.colno, "message": exc.msg}
    return None


def yaml_syntax_error(text: str) -> Optional[dict]:
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or str(exc)
        return {"line": mark.line + 1 if mark else None, "column": mark.column + 1 if mark else None,
                "message": problem}
    return None
