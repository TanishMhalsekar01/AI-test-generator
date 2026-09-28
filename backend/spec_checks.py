"""
API & data-contract checks.

Inputs: OpenAPI 3.x / Swagger 2.0 (JSON or YAML), GraphQL SDL, or any JSON /
YAML document (optionally with a JSON Schema to validate against).

Deterministic stages (no AI):
  - syntax errors with line / column
  - OpenAPI lint: unresolved $ref, path-parameter mismatches, duplicate
    operationIds, missing responses, undefined security schemes, ...
  - GraphQL SDL validation with graphql-core
  - JSON Schema validation, duplicate keys
  - live contract checks against a base URL: status codes must be documented,
    JSON bodies must match the documented schema; GraphQL introspection diff

AI stage: Gemini writes a pytest suite per operation; it is executed only
when a live base URL is supplied.
"""

import ipaddress
import json
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional
from urllib.parse import quote, urlparse

import jsonschema
import requests
import yaml
from graphql import (
    GraphQLError,
    GraphQLSyntaxError,
    build_ast_schema,
    build_client_schema,
    get_introspection_query,
    parse as gql_parse,
    validate_schema,
)
from graphql.validation.validate import validate_sdl

import executors
import gemini_client
from config import get_settings
from spec_parser import MAX_OPERATIONS, parse_spec
from spec_llm import generate_spec_tests

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
MAX_LIVE_OPERATIONS = 25
MAX_GENERATED_OPERATIONS = 15
LIVE_TIMEOUT = 10
_MISSING = object()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _issue(severity: str, rule: str, message: str, pointer: str = "", line: Optional[int] = None,
           column: Optional[int] = None) -> dict:
    return {"severity": severity, "rule": rule, "message": message, "pointer": pointer or "",
            "line": line, "column": column}


def esc(token: Any) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def resolve_pointer(doc: Any, pointer: str) -> Any:
    node = doc
    for raw in pointer.lstrip("/").split("/") if pointer.strip("/") else []:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


def line_index(text: str) -> tuple[dict[str, int], list[dict]]:
    """Map JSON pointers to 1-based line numbers; also report duplicate keys."""
    index: dict[str, int] = {}
    duplicates: list[dict] = []
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return index, duplicates
    if root is None:
        return index, duplicates

    def walk(node, ptr: str, depth: int = 0):
        if depth > 60:
            return
        index.setdefault(ptr or "/", node.start_mark.line + 1)
        if isinstance(node, yaml.MappingNode):
            seen: dict[str, int] = {}
            for key_node, value_node in node.value:
                key = str(key_node.value)
                child = f"{ptr}/{esc(key)}"
                if key in seen:
                    duplicates.append(_issue("warning", "duplicate-key",
                                             f"Duplicate key '{key}' (first defined on line {seen[key]}); "
                                             "most parsers silently keep only the last value.",
                                             child, key_node.start_mark.line + 1, key_node.start_mark.column + 1))
                else:
                    seen[key] = key_node.start_mark.line + 1
                walk(value_node, child, depth + 1)
                index[child] = key_node.start_mark.line + 1
        elif isinstance(node, yaml.SequenceNode):
            for i, item in enumerate(node.value):
                walk(item, f"{ptr}/{i}", depth + 1)

    walk(root, "")
    return index, duplicates


def _locate(issues: list[dict], index: dict[str, int]) -> list[dict]:
    for issue in issues:
        if issue["line"] is None and issue["pointer"]:
            ptr = issue["pointer"]
            while ptr and ptr not in index:
                ptr = ptr.rsplit("/", 1)[0]
            issue["line"] = index.get(ptr or "/")
    return issues


def guard_url(url: str) -> str:
    """Reject non-HTTP URLs and (unless allowed) private-network targets (SSRF)."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("The URL must start with http:// or https:// and include a host.")
    if get_settings().allow_private_targets:
        return url
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve host {parsed.hostname!r}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"{parsed.hostname} resolves to a private or reserved address ({ip}); "
                             "requests to internal networks are blocked on this server.")
    return url


# ---------------------------------------------------------------------------
# Kind detection + syntax
# ---------------------------------------------------------------------------

def detect_kind(raw: str, filename: str = "") -> tuple[str, Any, Optional[dict]]:
    """Return (kind, parsed_document_or_None, syntax_issue_or_None)."""
    name = filename.lower()
    text = raw.strip()
    if not text:
        raise ValueError("The document is empty.")
    if name.endswith((".graphql", ".gql", ".graphqls", ".sdl")):
        return "graphql", None, None
    looks_json = name.endswith(".json") or text[:1] in "{["
    if looks_json:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            return "json", None, _issue("error", "json-syntax", exc.msg, "", exc.lineno, exc.colno)
        return ("openapi" if isinstance(doc, dict) and ("openapi" in doc or "swagger" in doc) else "json"), doc, None
    if name.endswith((".yaml", ".yml")):
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            return "yaml", None, _issue("error", "yaml-syntax", getattr(exc, "problem", None) or str(exc), "",
                                        mark.line + 1 if mark else None, mark.column + 1 if mark else None)
        return ("openapi" if isinstance(doc, dict) and ("openapi" in doc or "swagger" in doc) else "yaml"), doc, None
    if re.search(r"^\s*(type|schema|interface|input|enum|union|scalar|extend|directive)\b", text, re.M):
        return "graphql", None, None
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        doc = None
    if isinstance(doc, dict) and ("openapi" in doc or "swagger" in doc):
        return "openapi", doc, None
    if isinstance(doc, (dict, list)):
        return "yaml", doc, None
    raise ValueError("Unrecognised document. Upload OpenAPI/Swagger (JSON or YAML), GraphQL SDL, or JSON/YAML data.")


# ---------------------------------------------------------------------------
# OpenAPI lint
# ---------------------------------------------------------------------------

def _iter_refs(node: Any, ptr: str = ""):
    if isinstance(node, dict):
        for k, v in node.items():
            child = f"{ptr}/{esc(k)}"
            if k == "$ref" and isinstance(v, str):
                yield ptr, v
            else:
                yield from _iter_refs(v, child)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _iter_refs(v, f"{ptr}/{i}")


def _deref(doc: dict, node: Any) -> Any:
    seen = 0
    while isinstance(node, dict) and isinstance(node.get("$ref"), str) and node["$ref"].startswith("#") and seen < 20:
        target = resolve_pointer(doc, node["$ref"][1:])
        if target is _MISSING:
            return {}
        node = target
        seen += 1
    return node


def iter_operations(doc: dict):
    paths = doc.get("paths") if isinstance(doc.get("paths"), dict) else {}
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        item = _deref(doc, item)
        path_params = item.get("parameters") if isinstance(item.get("parameters"), list) else []
        for method in HTTP_METHODS:
            op = item.get(method)
            if isinstance(op, dict):
                merged: dict[tuple, dict] = {}
                for p in path_params + (op.get("parameters") if isinstance(op.get("parameters"), list) else []):
                    p = _deref(doc, p)
                    if isinstance(p, dict):
                        merged[(p.get("name"), p.get("in"))] = p
                yield path, method, op, list(merged.values())


def lint_openapi(doc: dict) -> list[dict]:
    issues: list[dict] = []
    add = lambda *a, **k: issues.append(_issue(*a, **k))  # noqa: E731
    v3 = "openapi" in doc
    version = str(doc.get("openapi") or doc.get("swagger") or "")
    if v3 and not version.startswith("3."):
        add("error", "openapi-version", f"Unsupported openapi version '{version}' (expected 3.x).", "/openapi")
    if not v3 and version != "2.0":
        add("error", "swagger-version", f"Unsupported swagger version '{version}' (expected 2.0).", "/swagger")

    info = doc.get("info")
    if not isinstance(info, dict):
        add("error", "info-missing", "The required 'info' object is missing.", "/info")
    else:
        for field in ("title", "version"):
            if not info.get(field):
                add("error", f"info-{field}-missing", f"'info.{field}' is required.", "/info")

    paths = doc.get("paths")
    if not isinstance(paths, dict) or not paths:
        add("error", "paths-empty", "The spec defines no paths.", "/paths")
        paths = {}

    for ptr, ref in _iter_refs(doc):
        if ref.startswith("#"):
            if resolve_pointer(doc, ref[1:]) is _MISSING:
                add("error", "unresolved-ref", f"$ref '{ref}' does not resolve to anything in this document.", ptr)
        else:
            add("info", "external-ref", f"External $ref '{ref}' was not followed.", ptr)

    comps = doc.get("components") if isinstance(doc.get("components"), dict) else {}
    schemes = (comps.get("securitySchemes") if v3 else doc.get("securityDefinitions")) or {}

    def check_security(reqs: Any, ptr: str):
        if not isinstance(reqs, list):
            return
        for i, req in enumerate(reqs):
            for name in (req or {}):
                if name not in schemes:
                    add("error", "undefined-security-scheme",
                        f"Security requirement '{name}' is not defined in "
                        f"{'components.securitySchemes' if v3 else 'securityDefinitions'}.", f"{ptr}/{i}")

    check_security(doc.get("security"), "/security")

    valid_in = {"path", "query", "header", "cookie"} if v3 else {"path", "query", "header", "formData", "body"}
    op_ids: dict[str, str] = {}
    for path, item in paths.items():
        pptr = f"/paths/{esc(path)}"
        if not str(path).startswith("/"):
            add("error", "path-leading-slash", f"Path '{path}' must start with '/'.", pptr)
        if not isinstance(item, dict):
            continue
        template = re.findall(r"\{([^}/]+)\}", str(path))
        if len(template) != len(set(template)):
            add("error", "duplicate-path-variable", f"Path '{path}' repeats a template variable.", pptr)

    for path, method, op, params in iter_operations(doc):
        optr = f"/paths/{esc(path)}/{method}"
        seen = set()
        for i, p in enumerate(params):
            name, loc = p.get("name"), p.get("in")
            if not name or not loc:
                add("error", "parameter-incomplete", "Every parameter needs 'name' and 'in'.", f"{optr}/parameters/{i}")
                continue
            if (name, loc) in seen:
                add("error", "duplicate-parameter", f"Parameter '{name}' in {loc} is declared twice.", f"{optr}/parameters")
            seen.add((name, loc))
            if loc not in valid_in:
                add("error", "parameter-location", f"Parameter '{name}' has invalid location '{loc}'.", f"{optr}/parameters")
            if loc == "path" and not p.get("required"):
                add("error", "path-parameter-not-required", f"Path parameter '{name}' must have required: true.",
                    f"{optr}/parameters")
            if v3 and "schema" not in p and "content" not in p:
                add("warning", "parameter-schema-missing", f"Parameter '{name}' has no schema.", f"{optr}/parameters")
            if not v3 and loc != "body" and "type" not in p:
                add("warning", "parameter-type-missing", f"Parameter '{name}' has no type.", f"{optr}/parameters")

        template = re.findall(r"\{([^}/]+)\}", str(path))
        declared = {p.get("name") for p in params if p.get("in") == "path"}
        for var in template:
            if var not in declared:
                add("error", "path-parameter-undeclared",
                    f"{method.upper()} {path}: template variable '{{{var}}}' has no matching 'in: path' parameter.", optr)
        for var in declared - set(template):
            add("error", "path-parameter-unused",
                f"{method.upper()} {path}: path parameter '{var}' does not appear in the URL template.", optr)

        op_id = op.get("operationId")
        if op_id:
            if op_id in op_ids:
                add("error", "duplicate-operation-id", f"operationId '{op_id}' is also used by {op_ids[op_id]}.",
                    f"{optr}/operationId")
            else:
                op_ids[op_id] = f"{method.upper()} {path}"
        else:
            add("info", "operation-id-missing", f"{method.upper()} {path} has no operationId.", optr)

        responses = op.get("responses")
        if not isinstance(responses, dict) or not responses:
            add("error", "responses-missing", f"{method.upper()} {path} documents no responses.", optr)
        else:
            codes = [str(c) for c in responses]
            if not any(c[:1] in {"2", "3"} or c == "default" for c in codes):
                add("warning", "no-success-response", f"{method.upper()} {path} documents no 2xx/3xx response.",
                    f"{optr}/responses")
            for code, resp in responses.items():
                rptr = f"{optr}/responses/{esc(code)}"
                if not re.fullmatch(r"[1-5](\d\d|XX)|default", str(code)):
                    add("error", "invalid-status-code", f"'{code}' is not a valid HTTP status code.", rptr)
                resp = _deref(doc, resp)
                if isinstance(resp, dict) and not resp.get("description"):
                    add("warning", "response-description-missing", f"Response {code} has no description (required).", rptr)

        if v3 and op.get("requestBody") is not None:
            body = _deref(doc, op["requestBody"])
            content = body.get("content") if isinstance(body, dict) else None
            if not content:
                add("error", "request-body-content-missing", "requestBody has no content.", f"{optr}/requestBody")
            else:
                for media, obj in content.items():
                    if not isinstance(obj, dict) or not obj.get("schema"):
                        add("warning", "request-body-schema-missing", f"requestBody '{media}' has no schema.",
                            f"{optr}/requestBody/content/{esc(media)}")
            if method in {"get", "head", "delete"}:
                add("warning", "body-on-safe-method", f"{method.upper()} {path} defines a request body; many "
                    "clients and proxies drop it.", f"{optr}/requestBody")
        check_security(op.get("security"), f"{optr}/security")

    schemas = (comps.get("schemas") if v3 else doc.get("definitions")) or {}
    base = "/components/schemas" if v3 else "/definitions"
    for name, schema in schemas.items() if isinstance(schemas, dict) else []:
        if isinstance(schema, dict) and isinstance(schema.get("required"), list):
            props = schema.get("properties") or {}
            for req in schema["required"]:
                if props and req not in props:
                    add("warning", "required-property-undefined",
                        f"Schema '{name}' lists '{req}' as required but does not define it.", f"{base}/{esc(name)}/required")
    if v3 and not doc.get("servers"):
        add("info", "servers-missing", "No 'servers' are declared; clients cannot discover the base URL.", "/servers")
    return issues


# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

def _gql_issue(err: GraphQLError, rule: str) -> dict:
    loc = err.locations[0] if err.locations else None
    return _issue("error", rule, err.message, "", loc.line if loc else None, loc.column if loc else None)


def lint_graphql(sdl: str):
    """Return (issues, schema_or_None)."""
    try:
        document = gql_parse(sdl)
    except GraphQLSyntaxError as exc:
        return [_gql_issue(exc, "graphql-syntax")], None
    errors = validate_sdl(document)
    if errors:
        return [_gql_issue(e, "graphql-sdl") for e in errors], None
    schema = build_ast_schema(document, assume_valid_sdl=True)
    issues = [_gql_issue(e, "graphql-schema") for e in validate_schema(schema)]
    return issues, schema


def graphql_live_diff(schema, endpoint: str) -> dict:
    started = time.monotonic()
    try:
        resp = requests.post(endpoint, json={"query": get_introspection_query()}, timeout=LIVE_TIMEOUT)
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return {"base_url": endpoint, "error": f"Introspection request failed: {type(exc).__name__}", "checks": []}
    if not isinstance(payload, dict) or not payload.get("data"):
        msg = (payload.get("errors") or [{}])[0].get("message", "no data") if isinstance(payload, dict) else "invalid"
        return {"base_url": endpoint, "error": f"Introspection is disabled or failed: {msg}", "checks": []}
    server = build_client_schema(payload["data"])
    checks = []
    for name, sdl_type in schema.type_map.items():
        if name.startswith("__"):
            continue
        srv_type = server.type_map.get(name)
        if srv_type is None:
            checks.append({"name": f"type {name}", "passed": False, "detail": "Declared in SDL but missing on the server."})
            continue
        sdl_fields = getattr(sdl_type, "fields", None)
        srv_fields = getattr(srv_type, "fields", None)
        if not sdl_fields or srv_fields is None:
            continue
        for fname, field in sdl_fields.items():
            srv_field = srv_fields.get(fname)
            if srv_field is None:
                checks.append({"name": f"{name}.{fname}", "passed": False, "detail": "Field missing on the server."})
            elif str(srv_field.type) != str(field.type):
                checks.append({"name": f"{name}.{fname}", "passed": False,
                               "detail": f"Type differs: SDL {field.type}, server {srv_field.type}."})
            else:
                checks.append({"name": f"{name}.{fname}", "passed": True, "detail": str(field.type)})
        for fname in set(srv_fields) - set(sdl_fields):
            checks.append({"name": f"{name}.{fname}", "passed": None, "detail": "Exposed by the server but not in the SDL."})
    return {"base_url": endpoint, "error": None, "duration_ms": int((time.monotonic() - started) * 1000),
            "checks": checks}


# ---------------------------------------------------------------------------
# Live OpenAPI contract checks
# ---------------------------------------------------------------------------

_FORMAT_EXAMPLES = {
    "uuid": "00000000-0000-4000-8000-000000000000", "date": "2024-01-01", "date-time": "2024-01-01T00:00:00Z",
    "email": "user@example.com", "uri": "https://example.com", "hostname": "example.com", "ipv4": "192.0.2.1",
}


def example_value(schema: Any, doc: dict, depth: int = 0) -> Any:
    schema = _deref(doc, schema) if isinstance(schema, dict) else {}
    if not isinstance(schema, dict) or depth > 4:
        return "test"
    for key in ("example", "default"):
        if key in schema:
            return schema[key]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    for combo in ("oneOf", "anyOf", "allOf"):
        if isinstance(schema.get(combo), list) and schema[combo]:
            if combo == "allOf":
                merged: dict = {}
                for part in schema[combo]:
                    val = example_value(part, doc, depth + 1)
                    if isinstance(val, dict):
                        merged.update(val)
                return merged
            return example_value(schema[combo][0], doc, depth + 1)
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t == "integer":
        return max(1, int(schema.get("minimum", 1) or 1))
    if t == "number":
        return float(schema.get("minimum", 1) or 1)
    if t == "boolean":
        return True
    if t == "array":
        return [example_value(schema.get("items", {}), doc, depth + 1)]
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        required = schema.get("required") or list(props)
        return {k: example_value(v, doc, depth + 1) for k, v in props.items() if k in required}
    return _FORMAT_EXAMPLES.get(schema.get("format", ""), "test")


def _param_value(p: dict, doc: dict) -> Any:
    if "example" in p:
        return p["example"]
    if isinstance(p.get("examples"), dict) and p["examples"]:
        first = next(iter(p["examples"].values()))
        if isinstance(first, dict) and "value" in first:
            return first["value"]
    return example_value(p.get("schema") or {"type": p.get("type", "string"), **({"enum": p["enum"]} if "enum" in p else {})}, doc)


def to_json_schema(schema: Any) -> Any:
    """Convert OpenAPI 3.0 schema quirks (nullable) into plain JSON Schema."""
    if isinstance(schema, list):
        return [to_json_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: to_json_schema(v) for k, v in schema.items() if k not in {"nullable", "discriminator", "xml", "example"}}
    if schema.get("nullable") is True:
        if isinstance(out.get("type"), str):
            out["type"] = [out["type"], "null"]
        elif "$ref" in out or "allOf" in out or "oneOf" in out or "anyOf" in out:
            out = {"anyOf": [out, {"type": "null"}]}
    return out


def _response_schema(doc: dict, op: dict, status: int, v3: bool) -> tuple[Optional[str], Any]:
    responses = op.get("responses") or {}
    key = next((k for k in (str(status), f"{str(status)[0]}XX", "default") if k in responses), None)
    if key is None:
        return None, None
    resp = _deref(doc, responses[key])
    if not isinstance(resp, dict):
        return key, None
    if v3:
        content = resp.get("content") or {}
        media = next((m for m in content if "json" in m), None)
        return key, (content.get(media) or {}).get("schema") if media else None
    return key, resp.get("schema")


def live_checks_openapi(doc: dict, base_url: str, allow_mutations: bool) -> dict:
    base = base_url.rstrip("/")
    v3 = "openapi" in doc
    root_schema = to_json_schema({k: v for k, v in doc.items() if k in {"components", "definitions"}})
    validator_cls = jsonschema.Draft202012Validator if str(doc.get("openapi", "")).startswith("3.1") else jsonschema.Draft4Validator
    results = []
    session = requests.Session()
    for path, method, op, params in list(iter_operations(doc))[:MAX_LIVE_OPERATIONS]:
        name = f"{method.upper()} {path}"
        if method not in {"get", "head"} and not allow_mutations:
            results.append({"operation": name, "status": "skipped", "checks": [],
                            "reason": "Write method; enable 'Send write requests' to include it."})
            continue
        url_path = str(path)
        query, headers = {}, {}
        for p in params:
            if p.get("in") == "path":
                url_path = url_path.replace("{" + p["name"] + "}", quote(str(_param_value(p, doc)), safe=""))
            elif p.get("in") == "query" and p.get("required"):
                query[p["name"]] = _param_value(p, doc)
            elif p.get("in") == "header" and p.get("required"):
                headers[p["name"]] = str(_param_value(p, doc))
        body = None
        if method not in {"get", "head"}:
            if v3 and isinstance(op.get("requestBody"), dict):
                content = (_deref(doc, op["requestBody"]).get("content") or {})
                media = next((m for m in content if "json" in m), None)
                if media:
                    mobj = content[media]
                    body = mobj.get("example") if "example" in mobj else example_value(mobj.get("schema") or {}, doc)
            elif not v3:
                bp = next((p for p in params if p.get("in") == "body"), None)
                if bp:
                    body = example_value(bp.get("schema") or {}, doc)

        url = base + url_path
        checks = []
        started = time.monotonic()
        try:
            resp = session.request(method.upper(), url, params=query, json=body, headers=headers,
                                   timeout=LIVE_TIMEOUT, allow_redirects=False)
        except requests.RequestException as exc:
            results.append({"operation": name, "url": url, "status": "failed", "status_code": None,
                            "duration_ms": int((time.monotonic() - started) * 1000),
                            "checks": [{"name": "reachable", "passed": False, "detail": type(exc).__name__}]})
            continue
        elapsed = int((time.monotonic() - started) * 1000)
        documented_key, schema = _response_schema(doc, op, resp.status_code, v3)
        checks.append({"name": "status code documented", "passed": documented_key is not None,
                       "detail": f"HTTP {resp.status_code}" + (f" matches '{documented_key}'" if documented_key
                                                               else " is not listed in the spec")})
        if resp.status_code in (401, 403) and (op.get("security") or doc.get("security")):
            checks.append({"name": "authentication", "passed": None,
                           "detail": "Endpoint requires credentials; response body not validated."})
        elif schema is not None and resp.content:
            try:
                payload = resp.json()
            except ValueError:
                checks.append({"name": "JSON body", "passed": False,
                               "detail": f"Documented JSON response but got {resp.headers.get('Content-Type', 'unknown')}."})
            else:
                target = {**root_schema, **to_json_schema(_deref(doc, schema) if "$ref" not in schema else schema)}
                errors = sorted(validator_cls(target).iter_errors(payload), key=lambda e: list(e.absolute_path))[:10]
                checks.append({
                    "name": "response matches schema", "passed": not errors,
                    "detail": "Valid" if not errors else "; ".join(
                        f"/{'/'.join(map(str, e.absolute_path))}: {e.message[:200]}" for e in errors),
                })
        if method == "get" and query:
            try:
                neg = session.get(url, timeout=LIVE_TIMEOUT, allow_redirects=False, headers=headers)
                checks.append({"name": "rejects missing required query params",
                               "passed": 400 <= neg.status_code < 500,
                               "detail": f"Without {', '.join(query)} the server returned HTTP {neg.status_code}."})
            except requests.RequestException:
                pass
        failed = any(c["passed"] is False for c in checks)
        results.append({"operation": name, "url": resp.request.url, "status": "failed" if failed else "passed",
                        "status_code": resp.status_code, "duration_ms": elapsed, "checks": checks})
    return {"base_url": base, "error": None, "operations": results}


# ---------------------------------------------------------------------------
# JSON / YAML data documents
# ---------------------------------------------------------------------------

def validate_with_schema(doc: Any, schema_raw: str) -> tuple[list[dict], dict]:
    try:
        schema = json.loads(schema_raw)
    except json.JSONDecodeError:
        try:
            schema = yaml.safe_load(schema_raw)
        except yaml.YAMLError as exc:
            return [_issue("error", "schema-syntax", f"JSON Schema could not be parsed: {exc}")], {"valid": False}
    if not isinstance(schema, dict):
        return [_issue("error", "schema-invalid", "JSON Schema must be an object.")], {"valid": False}
    cls = jsonschema.validators.validator_for(schema)
    try:
        cls.check_schema(schema)
    except jsonschema.SchemaError as exc:
        return [_issue("error", "schema-invalid", f"The JSON Schema itself is invalid: {exc.message}")], {"valid": False}
    errors = sorted(cls(schema).iter_errors(doc), key=lambda e: list(e.absolute_path))
    issues = [
        _issue("error", f"schema-{e.validator}", e.message[:500], "/" + "/".join(esc(p) for p in e.absolute_path))
        for e in errors[:200]
    ]
    return issues, {"valid": not errors, "title": schema.get("title"), "draft": cls.__name__.replace("Validator", ""),
                    "errors": len(errors)}


def lint_json_schema_document(doc: Any) -> list[dict]:
    if not (isinstance(doc, dict) and "json-schema.org" in str(doc.get("$schema", ""))):
        return []
    cls = jsonschema.validators.validator_for(doc)
    meta = cls(cls.META_SCHEMA)
    return [
        _issue("error", "meta-schema", e.message[:500], "/" + "/".join(esc(p) for p in e.absolute_path))
        for e in list(meta.iter_errors(doc))[:100]
    ]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _operation_view(op) -> dict:
    return {
        "name": op.name, "type": op.op_type, "method": op.method or None, "path": op.path or None,
        "gql_operation": op.gql_operation or None, "return_type": op.return_type or None,
        "summary": op.summary, "logic_summary": op.logic_summary(),
        "params": [{"name": p.name, "in": p.location, "required": p.required, "type": p.param_type} for p in op.params],
        "responses": [r.status_code for r in op.responses + op.error_responses],
    }


def _generate_and_run(ops: list, base_url: str, progress: Callable[[int, str], None]) -> tuple[list[dict], Optional[str], Optional[str]]:
    selected = ops[:MAX_GENERATED_OPERATIONS]
    results: list[dict] = [{} for _ in selected]
    abort: dict[str, Optional[str]] = {"error": None}

    def work(i: int, op) -> None:
        if abort["error"]:
            results[i] = {"operation": op.name, "code": "", "error": abort["error"],
                          "execution": executors.not_executed("Skipped because the AI service is unavailable.")}
            return
        try:
            code = generate_spec_tests(op, base_url=base_url)
        except (gemini_client.GeminiNotConfigured, gemini_client.GeminiUnavailable) as exc:
            abort["error"] = str(exc)
            results[i] = {"operation": op.name, "code": "", "error": str(exc),
                          "execution": executors.not_executed("No tests were generated.")}
            return
        except gemini_client.GeminiError as exc:
            results[i] = {"operation": op.name, "code": "", "error": str(exc),
                          "execution": executors.not_executed("No tests were generated.")}
            return
        if base_url:
            safe = re.sub(r"\W+", "_", op.name).strip("_").lower()[:60] or "op"
            execution = executors.run_api_tests(code, base_url, name=f"test_{safe}.py")
        else:
            execution = executors.not_executed("No base URL was given; run this suite with BASE_URL=<your server>.")
        results[i] = {"operation": op.name, "code": code, "error": None, "execution": execution}

    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(work, i, op) for i, op in enumerate(selected)]
        for fut in futures:
            fut.result()
            done += 1
            progress(40 + int(55 * done / max(len(selected), 1)), f"Generated tests for {done}/{len(selected)} operations")
    note = None
    if len(ops) > len(selected):
        note = f"Tests were generated for the first {len(selected)} of {len(ops)} operations."
    return results, abort["error"], note


def analyze_spec(raw: str, filename: str = "", *, schema_raw: Optional[str] = None, base_url: str = "",
                 allow_mutations: bool = False, generate_tests: bool = True,
                 progress: Optional[Callable[[int, str], None]] = None) -> tuple[dict, dict]:
    progress = progress or (lambda _p, _m: None)
    progress(5, "Detecting document type")
    kind, doc, syntax = detect_kind(raw, filename)
    index, duplicates = line_index(raw) if kind != "graphql" else ({}, [])
    report: dict = {
        "kind": kind, "filename": filename or None, "title": filename or kind, "spec_version": None,
        "issues": [], "operations": [], "operations_skipped": 0, "live": None, "tests": [], "tests_note": None,
        "schema_validation": None, "ai": {"model": get_settings().gemini_model, "error": None},
        "source": raw[:300_000],
    }
    issues: list[dict] = []
    if syntax:
        issues.append(syntax)
        report["issues"] = issues
        return report, _summary(report)

    ops = []
    if kind == "openapi":
        progress(15, "Linting OpenAPI document")
        report["spec_version"] = str(doc.get("openapi") or doc.get("swagger"))
        info = doc.get("info") if isinstance(doc.get("info"), dict) else {}
        report["title"] = info.get("title") or report["title"]
        issues += lint_openapi(doc) + duplicates
        try:
            ops, _, report["operations_skipped"] = parse_spec(raw)
        except ValueError:
            ops = []
        if base_url:
            progress(30, "Running live contract checks")
            report["live"] = live_checks_openapi(doc, guard_url(base_url), allow_mutations)
    elif kind == "graphql":
        progress(15, "Validating GraphQL SDL")
        gql_issues, schema = lint_graphql(raw)
        issues += gql_issues
        report["title"] = filename or "GraphQL schema"
        if schema is not None:
            try:
                ops, _, report["operations_skipped"] = parse_spec(raw)
            except ValueError as exc:
                issues.append(_issue("warning", "no-operations", str(exc)))
            if base_url:
                progress(30, "Comparing SDL with the live server")
                report["live"] = graphql_live_diff(schema, guard_url(base_url))
    else:
        progress(20, "Validating document")
        issues += duplicates + lint_json_schema_document(doc)
        if schema_raw:
            schema_issues, report["schema_validation"] = validate_with_schema(doc, schema_raw)
            issues += schema_issues

    report["operations"] = [_operation_view(op) for op in ops]
    if ops and generate_tests:
        progress(40, "Generating API tests")
        report["tests"], report["ai"]["error"], report["tests_note"] = _generate_and_run(ops, base_url, progress)
    report["issues"] = _locate(issues, index)
    order = {"error": 0, "warning": 1, "info": 2}
    report["issues"].sort(key=lambda i: (order.get(i["severity"], 3), i["line"] or 0))
    return report, _summary(report)


def _summary(report: dict) -> dict:
    issues = report["issues"]
    live_ops = (report.get("live") or {}).get("operations") or []
    live_checks = (report.get("live") or {}).get("checks") or []
    tests = [t.get("execution") or {} for t in report.get("tests") or []]
    return {
        "kind": report["kind"],
        "issues_error": sum(i["severity"] == "error" for i in issues),
        "issues_warning": sum(i["severity"] == "warning" for i in issues),
        "issues_info": sum(i["severity"] == "info" for i in issues),
        "operations": len(report.get("operations") or []),
        "live_passed": sum(o.get("status") == "passed" for o in live_ops) + sum(c.get("passed") is True for c in live_checks),
        "live_failed": sum(o.get("status") == "failed" for o in live_ops) + sum(c.get("passed") is False for c in live_checks),
        "tests_generated": sum(bool(t.get("code")) for t in report.get("tests") or []),
        "tests_passed": sum(t.get("passed", 0) for t in tests),
        "tests_failed": sum(t.get("failed", 0) + t.get("errors", 0) for t in tests),
        "ai_error": (report.get("ai") or {}).get("error"),
    }


__all__ = ["analyze_spec", "detect_kind", "lint_openapi", "lint_graphql", "guard_url", "MAX_OPERATIONS"]
