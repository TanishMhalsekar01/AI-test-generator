"""
Spec Parser — deterministic, zero-LLM extraction of API facts from:
  • OpenAPI / Swagger specs (JSON or YAML, detected by "openapi" / "swagger" key)
  • GraphQL SDL files (detected by "type Query", "type Mutation", or "schema" keyword)

Output is a list of OperationInfo dataclasses shaped analogously to FunctionInfo
in parser.py, so the downstream LLM prompt-builder in spec_llm.py can consume
them with the same pipeline pattern.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# --------------------------------------------------------------------------
# Data models
# --------------------------------------------------------------------------

MAX_OPERATIONS = 50  # cap — same spirit as the repo file-count cap in main.py


@dataclass
class ParamInfo:
    name: str
    location: str          # "path", "query", "body", "argument" (GraphQL)
    required: bool
    param_type: str        # JSON Schema type string or GraphQL type
    nullable: bool = True


@dataclass
class ResponseInfo:
    status_code: str       # e.g. "200", "404", "default"
    description: str
    schema_summary: str    # human-readable summary of the response schema


@dataclass
class OperationInfo:
    """
    Structured facts about a single API endpoint or GraphQL operation.
    Mirrors FunctionInfo in shape so spec_llm.py can build prompts similarly.
    """
    # Shared
    name: str              # "GET /users/{id}" or GraphQL "getUser"
    op_type: str           # "rest" or "graphql"
    summary: str           # one-line description

    # REST-specific (empty for GraphQL)
    path: str = ""
    method: str = ""       # "GET", "POST", etc.

    # GraphQL-specific (empty for REST)
    gql_operation: str = ""   # "query" or "mutation"
    return_type: str = ""
    return_nullable: bool = True

    # Shared structured facts
    params: list[ParamInfo] = field(default_factory=list)
    responses: list[ResponseInfo] = field(default_factory=list)
    error_responses: list[ResponseInfo] = field(default_factory=list)

    def logic_summary(self) -> str:
        """One-line summary suitable for use in an LLM prompt."""
        parts = []
        required = [p for p in self.params if p.required]
        optional = [p for p in self.params if not p.required]
        if required:
            parts.append("Required params: " + ", ".join(
                f"{p.name}({p.param_type} in {p.location})" for p in required
            ))
        if optional:
            parts.append("Optional params: " + ", ".join(
                f"{p.name}({p.param_type} in {p.location})" for p in optional
            ))
        success = [r for r in self.responses if r.status_code not in self.error_responses]
        if success:
            parts.append("Success codes: " + ", ".join(r.status_code for r in success))
        if self.error_responses:
            parts.append("Error codes: " + ", ".join(r.status_code for r in self.error_responses))
        if self.return_type:
            null_str = "nullable" if self.return_nullable else "non-null"
            parts.append(f"Returns: {self.return_type} ({null_str})")
        return " | ".join(parts) if parts else "(no details extracted)"


# --------------------------------------------------------------------------
# Spec-type detection
# --------------------------------------------------------------------------

def detect_spec_type(raw: str) -> str:
    """
    Return "openapi", or "graphql".
    Raises ValueError with a user-facing message if the content is not
    recognisable as either.
    """
    stripped = raw.strip()
    if not stripped:
        raise ValueError("Spec content is empty.")

    # Try JSON / YAML structural detection for OpenAPI first
    data = _try_parse_json_or_yaml(stripped)
    if data is not None and isinstance(data, dict):
        if "openapi" in data or "swagger" in data:
            return "openapi"

    # GraphQL SDL detection — look for schema keywords
    if _looks_like_graphql(stripped):
        return "graphql"

    # If JSON/YAML parsed but had neither openapi/swagger key
    if data is not None and isinstance(data, dict):
        raise ValueError(
            "The JSON/YAML file does not contain an 'openapi' or 'swagger' key. "
            "Only OpenAPI/Swagger specs are supported."
        )

    raise ValueError(
        "Cannot determine spec type. Provide a valid OpenAPI/Swagger (JSON or YAML) "
        "or GraphQL SDL file."
    )


def _try_parse_json_or_yaml(text: str) -> Any:
    """Try JSON first, then YAML. Returns parsed object or None."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except Exception:
        pass
    return None


def _looks_like_graphql(text: str) -> bool:
    """Heuristic: SDL must contain at least one of the canonical GraphQL keywords."""
    gql_keywords = [
        r"\btype\s+Query\b",
        r"\btype\s+Mutation\b",
        r"\bschema\s*\{",
        r"\btype\s+\w+\s*\{",
    ]
    for pattern in gql_keywords:
        if re.search(pattern, text):
            return True
    return False


# --------------------------------------------------------------------------
# OpenAPI / Swagger parser
# --------------------------------------------------------------------------

def parse_openapi(raw: str) -> list[OperationInfo]:
    """
    Parse an OpenAPI/Swagger spec (JSON or YAML) and return one OperationInfo
    per endpoint × HTTP method combination.

    Raises ValueError on malformed input or empty spec.
    Raises OverflowError (with message) when the cap is exceeded.
    """
    data = _try_parse_json_or_yaml(raw)
    if data is None or not isinstance(data, dict):
        raise ValueError("Could not parse spec as JSON or YAML.")

    # Normalise both OpenAPI 3.x and Swagger 2.x
    paths = data.get("paths") or {}
    if not isinstance(paths, dict):
        raise ValueError("Spec has no valid 'paths' object.")

    operations: list[OperationInfo] = []
    skipped = 0

    # Collect all (path, method, operation) triples first so we can cap cleanly
    all_ops: list[tuple[str, str, dict]] = []
    http_methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        # Path-level parameters (inherited by all methods)
        path_level_params = path_item.get("parameters", [])
        for method, op in path_item.items():
            if method.lower() not in http_methods:
                continue
            if not isinstance(op, dict):
                continue
            # Merge path-level params into op-level
            merged_op = dict(op)
            if path_level_params:
                existing_names = {
                    p.get("name") for p in op.get("parameters", []) if isinstance(p, dict)
                }
                extra = [
                    p for p in path_level_params
                    if isinstance(p, dict) and p.get("name") not in existing_names
                ]
                merged_op["parameters"] = op.get("parameters", []) + extra
            all_ops.append((path, method.upper(), merged_op))

    if not all_ops:
        raise ValueError("Spec contains no endpoint operations.")

    if len(all_ops) > MAX_OPERATIONS:
        skipped = len(all_ops) - MAX_OPERATIONS
        all_ops = all_ops[:MAX_OPERATIONS]
        # We still return the capped list but callers can check skipped via the raised info
        # Signal via OverflowError so the endpoint can format the message properly
        operations = [_parse_openapi_op(path, method, op, data) for path, method, op in all_ops]
        raise _SkippedOperationsError(
            f"Spec has too many operations. Processed {MAX_OPERATIONS}, "
            f"skipped {skipped}. Reduce the spec or raise the cap.",
            operations=operations,
            skipped=skipped,
        )

    for path, method, op in all_ops:
        operations.append(_parse_openapi_op(path, method, op, data))

    return operations


class _SkippedOperationsError(Exception):
    """Raised when the operation cap is hit; carries the partial results."""
    def __init__(self, message: str, operations: list, skipped: int):
        super().__init__(message)
        self.operations = operations
        self.skipped = skipped


def _resolve_ref(ref_str: str, root: dict) -> dict:
    """Resolve a simple $ref like '#/components/schemas/Foo' within the same doc."""
    if not ref_str.startswith("#/"):
        return {}
    parts = ref_str.lstrip("#/").split("/")
    node: Any = root
    for part in parts:
        if isinstance(node, dict):
            node = node.get(part, {})
        else:
            return {}
    return node if isinstance(node, dict) else {}


def _schema_type_summary(schema: Any, root: dict, depth: int = 0) -> str:
    """Produce a short human-readable type string from a JSON Schema fragment."""
    if depth > 3 or not isinstance(schema, dict):
        return "any"
    if "$ref" in schema:
        schema = _resolve_ref(schema["$ref"], root)
    t = schema.get("type", "")
    fmt = schema.get("format", "")
    if t == "array":
        items = schema.get("items", {})
        return f"array[{_schema_type_summary(items, root, depth + 1)}]"
    if t == "object":
        props = schema.get("properties", {})
        if props:
            fields = ", ".join(list(props.keys())[:5])
            return f"object{{{fields}}}"
        return "object"
    if t:
        return f"{t}({fmt})" if fmt else t
    # oneOf / anyOf / allOf
    for combiner in ("oneOf", "anyOf", "allOf"):
        if combiner in schema:
            sub = schema[combiner]
            if isinstance(sub, list) and sub:
                return f"{combiner}[{_schema_type_summary(sub[0], root, depth + 1)}]"
    return "any"


def _parse_openapi_op(path: str, method: str, op: dict, root: dict) -> OperationInfo:
    """Convert a single OpenAPI operation dict into an OperationInfo."""
    name = f"{method} {path}"
    summary = op.get("summary") or op.get("description") or ""

    # Parameters
    params: list[ParamInfo] = []
    for p in op.get("parameters", []):
        if not isinstance(p, dict):
            continue
        if "$ref" in p:
            p = _resolve_ref(p["$ref"], root)
        schema = p.get("schema", {})
        params.append(ParamInfo(
            name=p.get("name", "unknown"),
            location=p.get("in", "query"),
            required=bool(p.get("required", False)),
            param_type=_schema_type_summary(schema, root),
            nullable=bool(schema.get("nullable", True)),
        ))

    # Request body (OpenAPI 3.x)
    req_body = op.get("requestBody", {})
    if req_body:
        content = req_body.get("content", {})
        for media_type, media_obj in content.items():
            if not isinstance(media_obj, dict):
                continue
            body_schema = media_obj.get("schema", {})
            props = body_schema.get("properties", {})
            required_body_fields = set(body_schema.get("required", []))
            if props:
                for field_name, field_schema in props.items():
                    if isinstance(field_schema, dict) and "$ref" in field_schema:
                        field_schema = _resolve_ref(field_schema["$ref"], root)
                    params.append(ParamInfo(
                        name=field_name,
                        location="body",
                        required=field_name in required_body_fields,
                        param_type=_schema_type_summary(field_schema, root),
                        nullable=bool(field_schema.get("nullable", True))
                        if isinstance(field_schema, dict) else True,
                    ))
            else:
                # No individual fields — add as a single "body" param
                params.append(ParamInfo(
                    name="body",
                    location="body",
                    required=bool(req_body.get("required", False)),
                    param_type=_schema_type_summary(body_schema, root),
                    nullable=True,
                ))
            break  # only parse the first media type

    # Responses
    responses: list[ResponseInfo] = []
    error_responses: list[ResponseInfo] = []
    for code, resp_obj in (op.get("responses") or {}).items():
        if not isinstance(resp_obj, dict):
            continue
        if "$ref" in resp_obj:
            resp_obj = _resolve_ref(resp_obj["$ref"], root)
        description = resp_obj.get("description") or ""
        # Try to find a schema for the response
        schema_summary = ""
        content = resp_obj.get("content", {})
        if content and isinstance(content, dict):
            for _, media_obj in content.items():
                if isinstance(media_obj, dict) and "schema" in media_obj:
                    schema_summary = _schema_type_summary(media_obj["schema"], root)
                    break
        ri = ResponseInfo(
            status_code=str(code),
            description=description,
            schema_summary=schema_summary,
        )
        try:
            numeric_code = int(code)
            if numeric_code >= 400:
                error_responses.append(ri)
            else:
                responses.append(ri)
        except ValueError:
            # "default" or non-numeric
            responses.append(ri)

    return OperationInfo(
        name=name,
        op_type="rest",
        summary=summary,
        path=path,
        method=method,
        params=params,
        responses=responses,
        error_responses=error_responses,
    )


# --------------------------------------------------------------------------
# GraphQL SDL parser
# --------------------------------------------------------------------------

def parse_graphql(raw: str) -> list[OperationInfo]:
    """
    Parse a GraphQL SDL file and return one OperationInfo per query/mutation field.

    Raises ValueError on malformed input or empty spec.
    Raises _SkippedOperationsError when the cap is hit.
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("GraphQL SDL content is empty.")

    operations = _extract_gql_operations(raw)

    if not operations:
        raise ValueError("No Query or Mutation fields found in the GraphQL SDL.")

    if len(operations) > MAX_OPERATIONS:
        skipped = len(operations) - MAX_OPERATIONS
        capped = operations[:MAX_OPERATIONS]
        raise _SkippedOperationsError(
            f"GraphQL SDL has too many operations. Processed {MAX_OPERATIONS}, "
            f"skipped {skipped}.",
            operations=capped,
            skipped=skipped,
        )

    return operations


def _gql_type_str(type_node_str: str) -> tuple[str, bool]:
    """
    From a raw GraphQL type string like 'String!', '[User]', '[User!]!',
    return (base_type_name, is_nullable).
    """
    s = type_node_str.strip()
    nullable = not s.endswith("!")
    s = s.rstrip("!")
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].rstrip("!")
        return f"[{inner}]", nullable
    return s, nullable


def _extract_gql_operations(sdl: str) -> list[OperationInfo]:
    """
    Minimal SDL parser that extracts fields from type Query and type Mutation
    blocks using regex — no external SDL parser required.
    """
    operations: list[OperationInfo] = []

    # Match top-level type blocks: type Query { ... } / type Mutation { ... }
    # We handle nested braces by a simple depth counter approach via regex groups.
    # Strategy: find each `type Query { ... }` block
    type_block_re = re.compile(
        r'\btype\s+(Query|Mutation)\s*\{([^}]*)\}',
        re.DOTALL | re.IGNORECASE,
    )

    for block_match in type_block_re.finditer(sdl):
        gql_op = block_match.group(1).lower()   # "query" or "mutation"
        block_body = block_match.group(2)

        # Extract individual fields from the block body
        # A field looks like:  fieldName(arg: Type, ...): ReturnType  [# optional comment]
        # or just:             fieldName: ReturnType
        field_re = re.compile(
            r'(?m)^\s*(\w+)\s*'            # field name
            r'(\([^)]*\))?\s*'             # optional args in parens
            r':\s*'                        # colon
            r'([\w\[\]!]+)'                # return type
        )

        for field_match in field_re.finditer(block_body):
            field_name = field_match.group(1)
            args_str = field_match.group(2) or ""
            return_type_raw = field_match.group(3)

            return_type, return_nullable = _gql_type_str(return_type_raw)

            # Parse arguments
            params: list[ParamInfo] = []
            if args_str:
                # Strip the surrounding parentheses
                args_inner = args_str.strip("()")
                # Split by comma, but be careful of nested types
                arg_re = re.compile(r'(\w+)\s*:\s*([\w\[\]!]+)')
                for arg_match in arg_re.finditer(args_inner):
                    arg_name = arg_match.group(1)
                    arg_type_raw = arg_match.group(2)
                    arg_type, arg_nullable = _gql_type_str(arg_type_raw)
                    params.append(ParamInfo(
                        name=arg_name,
                        location="argument",
                        required=not arg_nullable,
                        param_type=arg_type,
                        nullable=arg_nullable,
                    ))

            operations.append(OperationInfo(
                name=field_name,
                op_type="graphql",
                summary=f"GraphQL {gql_op}: {field_name}",
                gql_operation=gql_op,
                return_type=return_type,
                return_nullable=return_nullable,
                params=params,
                responses=[],
                error_responses=[],
            ))

    return operations


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def parse_spec(raw: str) -> tuple[list[OperationInfo], str, int]:
    """
    Detect and parse a spec string.  Returns (operations, spec_type, skipped_count).

    spec_type is "openapi" or "graphql".

    Raises ValueError for invalid/empty/unrecognised specs.
    The operations list is always the capped result when skipped_count > 0.
    """
    spec_type = detect_spec_type(raw)
    skipped = 0
    try:
        if spec_type == "openapi":
            ops = parse_openapi(raw)
        else:
            ops = parse_graphql(raw)
    except _SkippedOperationsError as exc:
        return exc.operations, spec_type, exc.skipped
    return ops, spec_type, skipped
