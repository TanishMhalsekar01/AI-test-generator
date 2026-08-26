"""
Stage 1 — Upload
FastAPI app that accepts a .py file upload and returns parsed function metadata.
"""

from fastapi import FastAPI, UploadFile, HTTPException
from fastapi.responses import JSONResponse

from parser import parse_source
from llm import generate_tests
from runner import run_generated_tests

app = FastAPI(title="AI Unit-Test Generator — Stage 1/2 (Upload + Parse)")


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
                "raw_output": report["raw_output"],
                "coverage_percent": report["coverage_percent"],
            }
        )

    return JSONResponse(content=payload)
