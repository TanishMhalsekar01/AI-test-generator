"""
Spec LLM — test generation for API specs.
Reuses the existing Groq API call infrastructure from llm.py but builds prompts
from OperationInfo (parsed spec facts) instead of FunctionInfo (AST facts).

For each operation it generates:
  - A happy-path test (valid params, expect declared success code)
  - A missing-required-param test (expect 400/422)
  - An invalid-type test (where schema allows)
  - A test per declared error response code

Two modes:
  - Mock mode (default, no base_url): uses `responses` library to stub HTTP
  - Live mode (base_url provided): hits the real server via `requests`
"""

import os
import re

import requests as _requests

from spec_parser import OperationInfo

# --------------------------------------------------------------------------
# Prompt templates
# --------------------------------------------------------------------------

SPEC_SYSTEM_PROMPT = """\
You are an expert Python test engineer specialising in API testing.
Given metadata about an API endpoint or GraphQL operation, write a complete pytest test module.
Rules:
- Use only pytest (no unittest classes).
- Name every test function test_<something>.
- Output ONLY the Python code — no prose, no markdown fences.
- Use the `requests` library for HTTP calls.
- In MOCK MODE: use the `responses` library (pip install responses) to stub all HTTP calls.
  Import it at the top. Never make real network calls.
- In LIVE MODE: hit the provided base_url for real using `requests`.
- Always include: a happy-path test, a missing-required-param test (expect 422 or 400),
  an invalid-type test, and one test per declared error response code.\
"""


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
        lines.append(f"MODE: LIVE — base URL is {base_url!r}. Use requests to hit the real server.")
    else:
        lines.append(
            "MODE: MOCK — use the `responses` library to stub all HTTP calls. "
            "Do NOT make real network calls."
        )

    lines.append("")
    lines.append("Write pytest tests covering: happy path, missing required param, "
                 "invalid type, and each error response code.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Helpers (mirror llm.py helpers — no import to avoid coupling)
# --------------------------------------------------------------------------

def _strip_markdown_fences(text: str) -> str:
    """Remove leading/trailing ```python … ``` fences if present."""
    text = text.strip()
    text = re.sub(r"^```(?:python)?\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _mock_generate_spec(op: OperationInfo, base_url: str = "") -> str:
    """Return a minimal placeholder test when no LLM is available."""
    safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", op.name)
    mode_comment = f"# base_url={base_url!r}" if base_url else "# mock mode"
    return (
        f"# Auto-generated placeholder tests for {op.name}\n"
        f"# {mode_comment}\n"
        f"import pytest\n\n"
        f"def test_{safe_name}_placeholder():\n"
        f"    pass\n"
    )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def generate_spec_tests(op: OperationInfo, base_url: str = "") -> str:
    """
    Generate pytest test code for *op* (an OperationInfo).

    Uses the Groq chat-completions API when GROQ_API_KEY is set; otherwise
    falls back to _mock_generate_spec().

    Parameters
    ----------
    op:
        The parsed API operation to generate tests for.
    base_url:
        Optional live server base URL.  Empty string → mock mode.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return _mock_generate_spec(op, base_url)

    user_prompt = build_spec_user_prompt(op, base_url=base_url)

    try:
        response = _requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openai/gpt-oss-120b",
                "messages": [
                    {"role": "system", "content": SPEC_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 1000,
            },
            timeout=30,
        )
        response.raise_for_status()
        response_json = response.json()
        raw_text = response_json["choices"][0]["message"]["content"]
    except Exception as exc:
        print(f"[spec_llm] Groq API call failed for {op.name!r} ({exc}); falling back to mock.")
        return _mock_generate_spec(op, base_url)

    code = _strip_markdown_fences(raw_text)
    return code
