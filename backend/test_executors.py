"""Tests for sandboxed test execution across languages (real runners, no mocks)."""

import os
import shutil

import pytest

import executors
import sandbox
from languages import detect_language


def needs(tool):
    return pytest.mark.skipif(shutil.which(tool) is None, reason=f"{tool} not installed")


def run(filename, source, tests):
    lang = detect_language(filename, source)
    return executors.run_tests(lang, executors.build_harness(lang, filename, source), source, tests)


def test_python_failing_test_reveals_bug():
    src = "def add(a, b):\n    return a - b\n"
    tests = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n\ndef test_zero():\n    assert add(0, 0) == 0\n"
    res = run("calc.py", src, tests)
    assert res["executed"] and res["status"] == "failed"
    assert (res["passed"], res["failed"]) == (1, 1)
    failing = next(t for t in res["tests"] if t["status"] == "failed")
    assert failing["name"] == "test_add" and "-1" in failing["message"]
    assert res["coverage_percent"] == 100.0


def test_python_module_name_avoids_stdlib_and_test_prefix():
    lang = detect_language("json.py")
    assert executors.build_harness(lang, "json.py", "").target_file == "module_under_test.py"
    assert executors.build_harness(lang, "test_utils.py", "").target_file == "module_under_test.py"


def test_sandbox_env_has_no_secrets(tmp_path):
    os.environ["GEMINI_API_KEY"] = "test-gemini-key-000000"
    res = sandbox.run([sandbox.PYTHON, "-c", "import os; print(sorted(os.environ))"], tmp_path)
    assert "GEMINI_API_KEY" not in res.stdout
    assert "SESSION_SECRET" not in res.stdout
    assert "DATABASE_URL" not in res.stdout


def test_sandbox_timeout_kills_process(tmp_path):
    res = sandbox.run([sandbox.PYTHON, "-c", "import time; time.sleep(30)"], tmp_path, timeout=1)
    assert res.timed_out


@needs("node")
def test_javascript_commonjs_without_exports():
    src = "function add(a, b) { return a - b; }\nconst mul = (a, b) => a * b;\n"
    h = executors.build_harness(detect_language("util.js"), "util.js", src)
    assert "require('./util.cjs')" in h.instructions
    tests = ("const test = require('node:test');\nconst assert = require('node:assert/strict');\n"
             "const { add, mul } = require('./util.cjs');\n"
             "test('add', () => { assert.equal(add(2, 3), 5); });\ntest('mul', () => { assert.equal(mul(2, 3), 6); });\n")
    res = run("util.js", src, tests)
    assert (res["passed"], res["failed"]) == (1, 1)


@needs("node")
def test_javascript_esm():
    src = "export function add(a, b) { return a + b; }\nfunction hidden() { return 1; }\n"
    tests = ("import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
             "import { add, hidden } from './util.mjs';\n"
             "test('add', () => assert.equal(add(1, 2), 3));\ntest('hidden', () => assert.equal(hidden(), 1));\n")
    res = run("util.mjs", src, tests)
    assert res["status"] == "passed" and res["passed"] == 2


@needs("go")
def test_go_subtests_counted_as_leaves():
    src = "package mathx\n\nfunc Add(a, b int) int { return a - b }\n"
    tests = ('package mathx\n\nimport "testing"\n\nfunc TestAdd(t *testing.T) {\n'
             '\tt.Run("pos", func(t *testing.T) { if Add(2, 3) != 5 { t.Fatalf("got %d", Add(2, 3)) } })\n'
             '\tt.Run("zero", func(t *testing.T) { if Add(0, 0) != 0 { t.Fatal("bad") } })\n}\n')
    res = run("mathx.go", src, tests)
    assert {t["name"] for t in res["tests"]} == {"TestAdd/pos", "TestAdd/zero"}
    assert res["failed"] == 1 and "got -1" in next(t for t in res["tests"] if t["status"] == "failed")["message"]


@needs("ruby")
def test_ruby_minitest():
    src = "def add(a, b)\n  a - b\nend\n"
    tests = ("require 'minitest/autorun'\nrequire_relative 'calc'\nclass TestCalc < Minitest::Test\n"
             "  def test_add\n    assert_equal 5, add(2, 3)\n  end\n  def test_zero\n    assert_equal 0, add(0, 0)\n  end\nend\n")
    res = run("calc.rb", src, tests)
    assert (res["passed"], res["failed"]) == (1, 1)


@needs("rustc")
def test_rust_appended_test_module():
    src = "pub fn add(a: i32, b: i32) -> i32 { a - b }\n"
    tests = ("#[cfg(test)]\nmod generated_tests {\n    use super::*;\n"
             "    #[test]\n    fn adds() { assert_eq!(add(2, 3), 5); }\n    #[test]\n    fn zero() { assert_eq!(add(0, 0), 0); }\n}\n")
    res = run("lib.rs", src, tests)
    assert (res["passed"], res["failed"]) == (1, 1)


def test_unsupported_language_is_not_executed():
    res = run("Main.java", "class Main {}", "class MainTest {}")
    assert res["executed"] is False and res["status"] == "not_executed"
    assert res["passed"] == 0  # never counted as passing


def test_parse_tap_skips_suites():
    tap = ("TAP version 13\n# Subtest: suite\n    ok 1 - inner\n      ---\n      duration_ms: 1\n      ...\n"
           "    1..1\nok 1 - suite\n  ---\n  duration_ms: 2\n  type: 'suite'\n  ...\n"
           "not ok 2 - broken\n  ---\n  error: |-\n    boom\n  expected: 1\n  actual: 2\n  ...\n")
    tests = executors.parse_tap(tap)
    assert [(t["name"], t["status"]) for t in tests] == [("inner", "passed"), ("broken", "failed")]
    assert "boom" in tests[1]["message"]
