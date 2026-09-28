"""
Language detection and per-language testing conventions.

Detection is by file extension (plus a shebang check for extension-less
scripts). Unknown extensions are still reviewed — the model is asked to
identify the language itself.
"""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Optional

import sandbox


@dataclass(frozen=True)
class Language:
    id: str
    name: str
    extensions: tuple[str, ...]
    test_framework: str
    runner_tool: Optional[str] = None  # executable that must exist for tests to run here

    @property
    def can_execute_tests(self) -> bool:
        return bool(self.runner_tool) and sandbox.which(self.runner_tool) is not None


LANGUAGES: tuple[Language, ...] = (
    Language("python", "Python", (".py", ".pyw"), "pytest", "python3"),
    Language("javascript", "JavaScript", (".js", ".mjs", ".cjs"), "node:test", "node"),
    Language("jsx", "JavaScript (JSX)", (".jsx",), "Jest + React Testing Library"),
    Language("typescript", "TypeScript", (".ts", ".mts", ".cts", ".tsx"), "Vitest"),
    Language("go", "Go", (".go",), "testing (go test)", "go"),
    Language("rust", "Rust", (".rs",), "built-in #[test]", "rustc"),
    Language("ruby", "Ruby", (".rb",), "Minitest", "ruby"),
    Language("java", "Java", (".java",), "JUnit 5"),
    Language("kotlin", "Kotlin", (".kt", ".kts"), "JUnit 5 / kotlin.test"),
    Language("csharp", "C#", (".cs",), "xUnit"),
    Language("c", "C", (".c", ".h"), "Unity / assert.h"),
    Language("cpp", "C++", (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"), "GoogleTest"),
    Language("php", "PHP", (".php",), "PHPUnit"),
    Language("swift", "Swift", (".swift",), "XCTest"),
    Language("scala", "Scala", (".scala",), "ScalaTest"),
    Language("dart", "Dart", (".dart",), "package:test"),
    Language("shell", "Shell", (".sh", ".bash", ".zsh"), "bats"),
    Language("powershell", "PowerShell", (".ps1", ".psm1"), "Pester"),
    Language("sql", "SQL", (".sql",), "pgTAP / tSQLt"),
    Language("r", "R", (".r", ".R"), "testthat"),
    Language("lua", "Lua", (".lua",), "busted"),
    Language("perl", "Perl", (".pl", ".pm"), "Test::More"),
    Language("haskell", "Haskell", (".hs",), "Hspec"),
    Language("elixir", "Elixir", (".ex", ".exs"), "ExUnit"),
    Language("erlang", "Erlang", (".erl",), "EUnit"),
    Language("clojure", "Clojure", (".clj", ".cljs", ".cljc"), "clojure.test"),
    Language("fsharp", "F#", (".fs", ".fsx"), "xUnit"),
    Language("objc", "Objective-C", (".m", ".mm"), "XCTest"),
    Language("julia", "Julia", (".jl",), "Test"),
    Language("zig", "Zig", (".zig",), "zig test"),
    Language("solidity", "Solidity", (".sol",), "Foundry (forge test)"),
    Language("vue", "Vue", (".vue",), "Vitest + Vue Test Utils"),
    Language("svelte", "Svelte", (".svelte",), "Vitest + Testing Library"),
    Language("html", "HTML", (".html", ".htm"), "Playwright"),
    Language("css", "CSS", (".css", ".scss", ".sass", ".less"), "Stylelint"),
    Language("groovy", "Groovy", (".groovy", ".gradle"), "Spock"),
    Language("vb", "Visual Basic", (".vb",), "MSTest"),
    Language("fortran", "Fortran", (".f90", ".f95", ".f"), "pFUnit"),
    Language("cobol", "COBOL", (".cob", ".cbl"), "COBOL Check"),
    Language("assembly", "Assembly", (".asm", ".s"), "(manual)"),
    Language("terraform", "Terraform", (".tf",), "terraform test"),
    Language("dockerfile", "Dockerfile", (), "hadolint / container-structure-test"),
)

OTHER = Language("other", "Other", (), "the standard test framework for the detected language")

_BY_EXT = {ext.lower(): lang for lang in LANGUAGES for ext in lang.extensions}
_BY_ID = {lang.id: lang for lang in LANGUAGES}

_SHEBANGS = (
    (re.compile(r"python"), "python"),
    (re.compile(r"\bnode\b"), "javascript"),
    (re.compile(r"\b(ba|z)?sh\b"), "shell"),
    (re.compile(r"ruby"), "ruby"),
    (re.compile(r"perl"), "perl"),
    (re.compile(r"php"), "php"),
)

# Files that are data/config rather than program source; they belong in the
# API & specs checks, not in code review.
DATA_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".graphql", ".gql", ".xml", ".csv", ".md", ".txt", ".lock"}


def by_id(lang_id: str) -> Language:
    return _BY_ID.get(lang_id, OTHER)


def detect_language(filename: str, source: str = "") -> Language:
    name = PurePosixPath(filename).name
    if name.lower() in {"dockerfile", "containerfile"} or name.lower().startswith("dockerfile."):
        return _BY_ID["dockerfile"]
    suffix = PurePosixPath(name).suffix
    if suffix in _BY_EXT:
        return _BY_EXT[suffix]
    if suffix.lower() in _BY_EXT:
        return _BY_EXT[suffix.lower()]
    first = source.splitlines()[0] if source else ""
    if first.startswith("#!"):
        for pattern, lang_id in _SHEBANGS:
            if pattern.search(first):
                return _BY_ID[lang_id]
    return OTHER


def is_source_file(path: str) -> bool:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in DATA_EXTENSIONS:
        return False
    return detect_language(path).id != "other"
