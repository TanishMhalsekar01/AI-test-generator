"""
Stage 1 — Upload
FastAPI app that accepts a .py file upload and returns parsed function metadata.
"""

from fastapi import FastAPI, UploadFile, HTTPException, Form
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from typing import Optional
from pydantic import BaseModel

import requests as _http_requests

from parser import parse_source
from llm import generate_tests
from runner import run_generated_tests
from spec_parser import parse_spec
from spec_llm import generate_spec_tests
from github_fetcher import fetch_python_files

app = FastAPI(title="AI Unit-Test Generator — Stage 1/2 (Upload + Parse)")
app.mount("/ui", StaticFiles(directory="static", html=True), name="ui")


@app.post("/analyze")
async def analyze(file: UploadFile) -> JSONResponse:
    """
    Accept a single .py file, parse it for function metadata, and return the
    extracted information as JSON.

    Stage 3 (LLM test generation) will plug in here once it is ready.
    """
    if not file.filename or not file.filename.endswith(".py"):
        raise HTTPException(status_code=400, detail="Only .py files are accepted.")

    raw_bytes = await file.read()
    try:
        source = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="File could not be decoded as UTF-8."
        ) from exc

    functions = parse_source(source)

    payload = []
    for fn in functions:
        test_code = generate_tests(fn, module_import_name="target")
        report = run_generated_tests(
            target_source=fn.source,
            test_source=test_code,
            module_name="target",
        )
        payload.append(
            {
                "name": fn.name,
                "signature": fn.signature,
                "docstring": fn.docstring,
                "conditions": fn.conditions,
                "raises": fn.raises,
                "returns": fn.returns,
                "logic_summary": fn.logic_summary(),
                "flags": fn.flags,
                # NOTE: `source` is intentionally excluded from the API response;
                # it is kept in FunctionInfo for later test execution only.
                "tests_generated": report["tests_generated"],
                "tests_passed": report["tests_passed"],
                "tests_failed": report["tests_failed"],
                "coverage_percent": report["coverage_percent"],
            }
        )

    return JSONResponse(content=payload)


# ---------------------------------------------------------------------------
# POST /analyze-repo
# ---------------------------------------------------------------------------

class AnalyzeRepoRequest(BaseModel):
    github_url: str
    max_files: int = 50


# Sentinel comment left by _mock_generate() — used to detect mock mode below.
_MOCK_SENTINEL = "_placeholder"


@app.post("/analyze-repo")
async def analyze_repo(body: AnalyzeRepoRequest) -> JSONResponse:
    """
    Fetch every .py file from a public GitHub repository (up to max_files),
    run each one through the same Parse → LLM Generate → Run + Report pipeline
    used by POST /analyze, and return a structured report object.
    """
    # --- Validate max_files ---
    if body.max_files < 1 or body.max_files > 200:
        raise HTTPException(
            status_code=400,
            detail="max_files must be between 1 and 200.",
        )

    # --- Fetch files from GitHub (URL validation happens inside fetch_python_files) ---
    try:
        files, python_files_found = fetch_python_files(
            body.github_url, max_files=body.max_files, _return_total=True
        )
    except ValueError as exc:
        # Invalid URL format — client error
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # GitHub API errors, network failures, rate limits, etc. — upstream error
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch repository from GitHub: {exc}",
        ) from exc

    files_selected = len(files)
    files_skipped_due_to_limit = max(0, python_files_found - body.max_files)

    # --- Run pipeline per file ---
    results = []
    files_analyzed = 0
    for gh_file in files:
        # Parse — catch syntax errors so one bad file doesn't abort the whole request
        try:
            functions = parse_source(gh_file.source)
        except Exception as exc:
            print(f"[analyze-repo] parse failed for {gh_file.path!r}: {exc}")
            results.append({"file": gh_file.path, "functions": [], "parse_error": str(exc)})
            continue

        fn_results = []
        for fn in functions:
            test_code = generate_tests(fn, module_import_name="target")
            is_mock = _MOCK_SENTINEL in test_code

            report = run_generated_tests(
                target_source=fn.source,
                test_source=test_code,
                module_name="target",
            )

            if is_mock:
                # Mock/placeholder tests never exercise the real target code —
                # coverage measurement is meaningless here.
                fn_entry = {
                    "name": fn.name,
                    "signature": fn.signature,
                    "docstring": fn.docstring,
                    "conditions": fn.conditions,
                    "raises": fn.raises,
                    "returns": fn.returns,
                    "logic_summary": fn.logic_summary(),
                    "flags": fn.flags,
                    # NOTE: source excluded from response, same as /analyze
                    "tests_generated": report["tests_generated"],
                    "tests_passed": report["tests_passed"],
                    "tests_failed": report["tests_failed"],
                    "coverage_percent": None,
                    "generation_mode": "mock",
                    "coverage_status": "not_measured",
                    "warning": (
                        "No GROQ_API_KEY set or LLM call failed — placeholder test used. "
                        "coverage_percent is null because no target code was executed."
                    ),
                }
            else:
                fn_entry = {
                    "name": fn.name,
                    "signature": fn.signature,
                    "docstring": fn.docstring,
                    "conditions": fn.conditions,
                    "raises": fn.raises,
                    "returns": fn.returns,
                    "logic_summary": fn.logic_summary(),
                    "flags": fn.flags,
                    # NOTE: source excluded from response, same as /analyze
                    "tests_generated": report["tests_generated"],
                    "tests_passed": report["tests_passed"],
                    "tests_failed": report["tests_failed"],
                    "coverage_percent": report["coverage_percent"],
                    "generation_mode": "llm",
                    "coverage_status": "measured",
                    "warning": None,
                }

            fn_results.append(fn_entry)

        files_analyzed += 1
        results.append({"file": gh_file.path, "functions": fn_results})

    return JSONResponse(content={
        "max_files": body.max_files,
        "python_files_found": python_files_found,
        "files_selected": files_selected,
        "files_skipped_due_to_limit": files_skipped_due_to_limit,
        "files_analyzed": files_analyzed,
        "results": results,
    })


# ---------------------------------------------------------------------------
# POST /analyze-spec
# ---------------------------------------------------------------------------

@app.post("/analyze-spec")
async def analyze_spec(
    file: Optional[UploadFile] = None,
    spec_url: Optional[str] = Form(default=None),
    base_url: Optional[str] = Form(default=None),
) -> JSONResponse:
    """
    Accept an OpenAPI/Swagger (JSON or YAML) or GraphQL SDL spec — either as
    an uploaded file or a publicly reachable URL — and return generated pytest
    tests plus run results for every endpoint / operation found.
    """
    # --- 1. Obtain raw spec text ---
    raw: str = ""

    if file is not None:
        raw_bytes = await file.read()
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="Uploaded file could not be decoded as UTF-8.",
            ) from exc
    elif spec_url:
        try:
            resp = _http_requests.get(spec_url.strip(), timeout=15)
            resp.raise_for_status()
            raw = resp.text
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Could not fetch spec from URL: {exc}",
            ) from exc
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide either a file upload or a 'spec_url' form field.",
        )

    # --- 2. Parse the spec ---
    skipped_count = 0
    try:
        operations, spec_type, skipped_count = parse_spec(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    live_base_url: str = (base_url or "").strip()

    # --- 3. Generate + run tests per operation ---
    payload = []
    failed_operations = []

    for op in operations:
        try:
            test_code = generate_spec_tests(op, base_url=live_base_url)
        except Exception as exc:
            print(f"[analyze-spec] LLM generation failed for {op.name!r}: {exc}")
            failed_operations.append({"operation": op.name, "reason": str(exc)})
            continue

        # Run in an isolated temp dir — reuse the existing runner
        report = run_generated_tests(
            target_source="# spec target placeholder\n",
            test_source=test_code,
            module_name="target",
        )

        entry: dict = {
            "operation": op.name,
            "spec_type": spec_type,
            "summary": op.summary,
            "logic_summary": op.logic_summary(),
            "tests_generated": report["tests_generated"],
            "tests_passed": report["tests_passed"],
            "tests_failed": report["tests_failed"],
            "coverage_percent": report["coverage_percent"],
        }
        if op.op_type == "rest":
            entry["method"] = op.method
            entry["path"] = op.path
        else:
            entry["gql_operation"] = op.gql_operation
            entry["return_type"] = op.return_type

        payload.append(entry)

    response_body: dict = {
        "spec_type": spec_type,
        "operations_processed": len(payload),
        "operations_skipped": skipped_count,
        "failed_operations": failed_operations,
        "results": payload,
    }
    if skipped_count:
        response_body["warning"] = (
            f"{skipped_count} operation(s) were skipped because the spec exceeds "
            f"the {50}-operation cap. Trim the spec to process all operations."
        )

    return JSONResponse(content=response_body)
