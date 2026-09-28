"""
Spec LLM — pytest generation for API operations (OpenAPI/Swagger or GraphQL).

Prompts are built from OperationInfo facts extracted deterministically by
spec_parser.py. The generated suite always targets a real server whose
address is read from the BASE_URL environment variable; nothing is stubbed,
so a passing test means the live API behaved as documented.
"""

import re

import gemini_client
from spec_parser import OperationInfo

SPEC_SYSTEM_PROMPT = """\
You are an expert API test engineer. Given facts about one API endpoint or GraphQL operation,
write a complete pytest module that tests it against a running server.
Rules:
- Use pytest and the `requests` library only. Name every test function test_<something>.
- Read the server address once: `BASE_URL = os.environ["BASE_URL"].rstrip("/")`. Never hard-code hosts.
- Pass timeout=10 to every request.
- Cover: the happy path with valid inputs, a missing required parameter (expect 400 or 422),
  an invalid parameter type, and each documented error response. Assert status codes and the
  documented response shape (required fields and their types).
- For GraphQL, POST {"query": ..., "variables": ...} to BASE_URL and check `data` / `errors`.
- Output ONLY Python code — no prose, no markdown fences."""


def build_spec_user_prompt(op: OperationInfo, base_url: str = "") -> str:
    """Build the user-turn prompt from an OperationInfo."""
    lines = []
    if op.op_type == "rest":
        lines.append(f"Endpoint          : {op.method} {op.path}")
        lines.append(f"Summary           : {op.summary or '(none)'}")
    else:
        lines.append(f"GraphQL operation : {op.gql_operation} {op.name}")
        lines.append(f"Return type       : {op.return_type} ({'nullable' if op.return_nullable else 'non-null'})")

    lines.append(f"Logic summary     : {op.logic_summary()}")

    if op.params:
        lines.append("")
        lines.append("Parameters:")
        for p in op.params:
            req_str = "required" if p.required else "optional"
            lines.append(f"  - {p.name} ({p.param_type}, {p.location}, {req_str})")

    if op.responses:
        lines.append("")
        lines.append("Success responses:")
        for r in op.responses:
            schema_part = f" — schema: {r.schema_summary}" if r.schema_summary else ""
            lines.append(f"  - {r.status_code}: {r.description}{schema_part}")

    if op.error_responses:
        lines.append("")
        lines.append("Error responses:")
        for r in op.error_responses:
            schema_part = f" — schema: {r.schema_summary}" if r.schema_summary else ""
            lines.append(f"  - {r.status_code}: {r.description}{schema_part}")

    lines.append("")
    if base_url:
        lines.append(f"The suite will run now against {base_url}, exposed to the tests as BASE_URL.")
    else:
        lines.append("No server is available yet; the user will run the suite later with BASE_URL set.")
    lines.append("")
    lines.append("Write pytest tests covering: happy path, missing required param, "
                 "invalid type, and each error response code.")
    return "\n".join(lines)


_REQUIRED_IMPORTS = ("import os", "import pytest", "import requests")


def generate_spec_tests(op: OperationInfo, base_url: str = "") -> str:
    """
    Generate a pytest module for *op* with Gemini.

    Raises gemini_client.GeminiError when the model is not configured or
    unavailable — callers report that instead of substituting placeholder tests.
    """
    result = gemini_client.generate(SPEC_SYSTEM_PROMPT, build_spec_user_prompt(op, base_url=base_url),
                                    max_output_tokens=8192)
    code = gemini_client.strip_fences(result.text)
    missing = [imp for imp in _REQUIRED_IMPORTS if not re.search(rf"^{imp}\b", code, re.M)]
    if missing:
        code = "\n".join(missing) + "\n" + code
    return code
