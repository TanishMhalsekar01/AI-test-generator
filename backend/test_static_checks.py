"""Tests for language detection and real compiler / linter diagnostics."""

import shutil

import pytest

from languages import detect_language, is_source_file
from static_checks import json_syntax_error, run_static_checks, yaml_syntax_error


def needs(tool):
    return pytest.mark.skipif(shutil.which(tool) is None, reason=f"{tool} not installed")


@pytest.mark.parametrize("name,expected", [
    ("a.py", "python"), ("a.js", "javascript"), ("a.mjs", "javascript"), ("a.ts", "typescript"),
    ("a.go", "go"), ("a.rs", "rust"), ("A.java", "java"), ("a.cpp", "cpp"), ("a.c", "c"), ("a.cs", "csharp"),
    ("a.rb", "ruby"), ("a.php", "php"), ("a.kt", "kotlin"), ("a.swift", "swift"), ("a.sql", "sql"),
    ("Dockerfile", "dockerfile"), ("a.unknownext", "other"),
])
def test_detect_language(name, expected):
    assert detect_language(name).id == expected


def test_detect_language_from_shebang():
    assert detect_language("script", "#!/usr/bin/env python3\nprint(1)\n").id == "python"


def test_data_files_are_not_source():
    assert not is_source_file("package.json")
    assert not is_source_file("README.md")
    assert is_source_file("src/app.ts")


def test_python_syntax_error_has_line():
    lang = detect_language("x.py")
    result = run_static_checks("x.py", "def f(:\n    pass\n", lang)
    assert result["diagnostics"][0]["line"] == 1
    assert result["diagnostics"][0]["severity"] == "error"


@needs("pylint")
def test_pylint_reports_undefined_name():
    lang = detect_language("x.py")
    result = run_static_checks("x.py", "def f():\n    return undefined_thing\n", lang)
    messages = [d["message"] for d in result["diagnostics"]]
    assert any("undefined_thing" in m for m in messages)


@needs("node")
def test_javascript_syntax_error():
    result = run_static_checks("x.js", "function f(a {\n  return a\n}\n", detect_language("x.js"))
    assert result["diagnostics"][0]["line"] == 1
    assert "SyntaxError" in result["diagnostics"][0]["message"]


@needs("gcc")
def test_c_missing_semicolon():
    src = "#include <stdio.h>\nint main(void){ int x = 1 return x; }\n"
    result = run_static_checks("x.c", src, detect_language("x.c"))
    assert any(d["line"] == 2 and d["severity"] == "error" for d in result["diagnostics"])


@needs("go")
def test_go_vet_finds_printf_bug():
    src = 'package demo\n\nimport "fmt"\n\nfunc F() { fmt.Printf("%d\\n") }\n'
    result = run_static_checks("demo.go", src, detect_language("demo.go"))
    assert any("Printf" in d["message"] for d in result["diagnostics"])


@needs("ruby")
def test_ruby_syntax_error():
    result = run_static_checks("x.rb", "def f(\n  1\nend\n", detect_language("x.rb"))
    assert result["diagnostics"] and result["diagnostics"][0]["severity"] == "error"


def test_unknown_language_reports_unavailable():
    result = run_static_checks("x.kt", "fun main() {}", detect_language("x.kt"))
    if shutil.which("kotlinc") is None:
        assert result["tools"][0]["status"] == "unavailable"
        assert result["diagnostics"] == []


def test_json_and_yaml_syntax_errors():
    assert json_syntax_error('{"a": 1,}')["line"] == 1
    assert yaml_syntax_error("a: [1, 2\nb: 3\n")["line"] is not None
    assert json_syntax_error('{"a": 1}') is None
