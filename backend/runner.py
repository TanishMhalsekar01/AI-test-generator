"""
Stage 4 — Test Runner
Writes generated test code to a temporary directory and executes it with
pytest + pytest-cov, returning a structured results dict.
"""

import json
import sys 
import subprocess
import tempfile
from pathlib import Path


def run_generated_tests(
    target_source: str,
    test_source: str,
    module_name: str = "target",
) -> dict:
    """
    Write *target_source* and *test_source* to a temp directory, run pytest
    with coverage, and return a results dict with the keys:
        tests_generated, tests_passed, tests_failed, coverage_percent, raw_output
    """
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # Write source files
            (tmp_path / f"{module_name}.py").write_text(target_source, encoding="utf-8")
            (tmp_path / "test_generated.py").write_text(test_source, encoding="utf-8")

            # Run pytest with coverage
            result = subprocess.run(
                [
                    sys.executable, "-m", "pytest", "test_generated.py",
                    f"--cov={module_name}",
                    "--cov-report=json",
                    "-v",
                    "--tb=short",
                ],
                cwd=tmp_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )

            raw_output = result.stdout + result.stderr

            tests_passed = raw_output.count("PASSED")
            tests_failed = raw_output.count("FAILED")

            # Extract coverage from coverage.json if present
            coverage_percent = 0.0
            coverage_json_path = tmp_path / "coverage.json"
            if coverage_json_path.exists():
                try:
                    coverage_data = json.loads(coverage_json_path.read_text(encoding="utf-8"))
                    coverage_percent = float(coverage_data["totals"]["percent_covered"])
                except (KeyError, TypeError, ValueError):
                    coverage_percent = 0.0

            return {
                "tests_generated": tests_passed + tests_failed,
                "tests_passed": tests_passed,
                "tests_failed": tests_failed,
                "coverage_percent": coverage_percent,
                "raw_output": raw_output,
            }

    except Exception as exc:
        return {
            "tests_generated": 0,
            "tests_passed": 0,
            "tests_failed": 0,
            "coverage_percent": 0.0,
            "raw_output": str(exc),
        }
