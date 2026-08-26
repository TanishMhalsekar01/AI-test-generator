"""
Stage 2 — Parse
Extracts structured information from a Python function using the built-in `ast` module.
No LLM calls here; this is pure static analysis.
"""

import ast
import subprocess
import tempfile
import os
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FunctionInfo:
    name: str
    signature: str
    docstring: Optional[str]
    conditions: list[str]
    raises: list[str]
    returns: list[str]
    catches: list[str] = field(default_factory=list)
    source: str = ""     # kept for later test execution; NOT sent to any LLM
    flags: list[str] = field(default_factory=list)

    def logic_summary(self) -> str:
        """One-line summary of the function's key logic branches."""
        parts = []
        if self.conditions:
            parts.append("Conditions: " + " | ".join(self.conditions))
        if self.raises:
            parts.append("Raises: " + " | ".join(self.raises))
        if self.returns:
            parts.append("Returns: " + " | ".join(self.returns))
        if self.catches:
            parts.append("Catches: " + "; ".join(self.catches))
        return " | ".join(parts) if parts else "(no logic extracted)"


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def _extract_conditions(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return the source text of every `if` condition inside the function."""
    conditions = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.If):
            conditions.append(ast.unparse(node.test))
    return conditions


def _extract_raises(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return the source text of every raised expression inside the function."""
    raises = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Raise) and node.exc is not None:
            raises.append(ast.unparse(node.exc))
    return raises


def _extract_returns(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return the source text of every return expression inside the function."""
    returns = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Return) and node.value is not None:
            returns.append(ast.unparse(node.value))
    return returns


def _extract_catches(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return the exception type (or 'bare except') for every except handler inside the function."""
    catches = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.ExceptHandler):
            if node.type is not None:
                catches.append(ast.unparse(node.type))
            else:
                catches.append("bare except")
    return catches


def _build_signature(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Build a human-readable signature, e.g. 'calculate_discount(price, discount_percent)'."""
    args = func_node.args
    param_names = [arg.arg for arg in args.posonlyargs + args.args]
    if args.vararg:
        param_names.append("*" + args.vararg.arg)
    for kwonly in args.kwonlyargs:
        param_names.append(kwonly.arg)
    if args.kwarg:
        param_names.append("**" + args.kwarg.arg)
    return f"{func_node.name}({', '.join(param_names)})"


# ---------------------------------------------------------------------------
# Static-analysis flags
# ---------------------------------------------------------------------------

def _static_flags(catches: list[str]) -> list[str]:
    """Return flags derived from catch analysis."""
    flags = []
    if "bare except" in catches:
        flags.append("bare except clause found -- may silently swallow errors")
    return flags


def _flag_unvalidated_params(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """
    For each parameter, check whether it ever appears as an operand in a
    Compare node anywhere in the function body.  If not, flag it.
    """
    args = func_node.args
    all_params = {arg.arg for arg in args.posonlyargs + args.args + args.kwonlyargs}
    if args.vararg:
        all_params.add(args.vararg.arg)
    if args.kwarg:
        all_params.add(args.kwarg.arg)

    validated: set[str] = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Compare):
            # Check both the left operand and every comparator
            operands = [node.left] + list(node.comparators)
            for operand in operands:
                if isinstance(operand, ast.Name) and operand.id in all_params:
                    validated.add(operand.id)

    flags = []
    for param in sorted(all_params - validated):
        flags.append(f"no validation found for parameter '{param}'")
    return flags


# ---------------------------------------------------------------------------
# Optional pylint pass
# ---------------------------------------------------------------------------

_PYLINT_CHECKS = {
    "C0116",  # missing-function-docstring
    "W0102",  # dangerous-default-value
    "W0702",  # bare-except
    "W0703",  # broad-except
    "W0612",  # unused-variable
}


def _run_pylint(source: str) -> list[str]:
    """
    Run pylint on *source* in a temp file.
    Returns a filtered list of relevant messages, or an empty list if pylint
    is not installed or exits with an unexpected error.
    """
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(source)
            tmp_path = tmp.name

        result = subprocess.run(
            [
                "pylint",
                "--output-format=text",
                "--score=no",
                tmp_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        messages = []
        for line in result.stdout.splitlines():
            # pylint lines look like:  path.py:5:0: C0116: …
            for code in _PYLINT_CHECKS:
                if code in line:
                    messages.append(line.strip())
                    break
        return messages
    except FileNotFoundError:
        # pylint not installed
        return []
    except Exception:
        return []
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_source(source: str, run_pylint: bool = False) -> list[FunctionInfo]:
    """
    Parse *source* (raw Python source code) and return one :class:`FunctionInfo`
    for every top-level function definition found.
    """
    tree = ast.parse(source)
    pylint_messages = _run_pylint(source) if run_pylint else []

    results = []
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        catches = _extract_catches(node)
        info = FunctionInfo(
            name=node.name,
            signature=_build_signature(node),
            docstring=ast.get_docstring(node),
            conditions=_extract_conditions(node),
            raises=_extract_raises(node),
            returns=_extract_returns(node),
            catches=catches,
            source=ast.unparse(node),
            flags=_flag_unvalidated_params(node) + _static_flags(catches),
        )
        results.append(info)

    # Attach any matching pylint messages to the relevant function
    for info in results:
        for msg in pylint_messages:
            if info.name in msg or True:   # attach all messages when scope is ambiguous
                info.flags.append(f"[pylint] {msg}")

    return results

# --- Backward-compatible aliases (main.py, test_parser.py import these names) ---
def extract_functions(source_code: str) -> list[FunctionInfo]:
    return parse_source(source_code, run_pylint=False)


def run_pylint_flags(source_code: str) -> list[str]:
    return _run_pylint(source_code)
