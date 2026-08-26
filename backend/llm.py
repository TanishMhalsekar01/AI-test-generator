"""
Stage 3 — LLM Test Generation
Calls the Groq API (llama-3.3-70b-versatile) to generate pytest unit tests for
a given FunctionInfo.  Falls back to _mock_generate() when GROQ_API_KEY is not
set or the API call fails.
"""

import os
import re
import requests

from parser import FunctionInfo

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an expert Python test engineer.
Given metadata about a Python function, write a complete pytest test module.
Rules:
- Use only pytest (no unittest).
- Name every test function test_<something>.
- Do NOT import the function under test; assume it is already in scope.
- Output ONLY the Python code — no prose, no markdown fences.\
"""


def build_user_prompt(info: FunctionInfo) -> str:
    """Build the user-turn prompt from a FunctionInfo."""
    lines = [
        f"Function signature : {info.signature}",
        f"Docstring          : {info.docstring or '(none)'}",
        f"Logic summary      : {info.logic_summary()}",
        f"Flags              : {', '.join(info.flags) or '(none)'}",
        "",
        "Write pytest tests for this function.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_markdown_fences(text: str) -> str:
    """Remove leading/trailing ```python … ``` fences if present."""
    text = text.strip()
    text = re.sub(r"^```(?:python)?\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _mock_generate(info: FunctionInfo, module_import_name) -> str:
    """Return a minimal placeholder test when no LLM is available."""
    return (
        f"# Mock tests for {info.name}\n"
        f"def test_{info.name}_placeholder():\n"
        f"    pass\n"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_tests(info: FunctionInfo, module_import_name: str = "target") -> str:
    """
    Generate pytest test code for *info*.

    Uses the Groq chat-completions API when GROQ_API_KEY is set; otherwise
    falls back to _mock_generate().
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return _mock_generate(info, module_import_name)

    user_prompt = build_user_prompt(info)
    
    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openai/gpt-oss-120b",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 800,
            },
            timeout=30,
        )
        response.raise_for_status()
        response_json = response.json()
        raw_text = response_json["choices"][0]["message"]["content"]
    except Exception as exc:
     print(f"[llm] Groq API call failed ({exc}); falling back to mock output.")
     return _mock_generate(info, module_import_name)
    code = _strip_markdown_fences(raw_text)

    # Drop any stray import lines the model may have emitted
    code = "\n".join(
        line for line in code.splitlines()
        if not line.startswith("import ") and not line.startswith("from ")
    )
    if "import pytest" not in code:
     code = "import pytest\n" + code
     code: str = f"from {module_import_name} import {info.name}\n\n" + code
    return code
