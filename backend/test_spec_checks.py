"""Tests for deterministic spec checks: OpenAPI lint, GraphQL, JSON Schema, live contract checks."""

import json

import pytest
import responses

import spec_checks

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Shop", "version": "1.0"},
    "servers": [{"url": "https://api.example.test"}],
    "paths": {
        "/items/{id}": {
            "get": {
                "operationId": "getItem",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {
                    "200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}}},
                    "404": {"description": "Missing"},
                },
            }
        },
        "/search": {
            "get": {
                "operationId": "search",
                "parameters": [{"name": "q", "in": "query", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "OK"}, "422": {"description": "Invalid"}},
            }
        },
        "/items": {"post": {"operationId": "createItem", "responses": {"201": {"description": "Created"}}}},
    },
    "components": {"schemas": {"Item": {
        "type": "object", "required": ["id", "name"],
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "note": {"type": "string", "nullable": True}},
    }}},
}


def rules(issues):
    return {i["rule"] for i in issues}


def test_clean_spec_has_no_errors():
    issues = spec_checks.lint_openapi(SPEC)
    assert not [i for i in issues if i["severity"] == "error"]


def test_lint_catches_common_contract_mistakes():
    bad = json.loads(json.dumps(SPEC))
    bad["paths"]["/items/{id}"]["get"]["parameters"] = []  # template var undeclared
    bad["paths"]["/search"]["get"]["operationId"] = "getItem"  # duplicate
    bad["paths"]["/search"]["get"]["responses"]["200"]["content"] = {"application/json": {"schema": {"$ref": "#/components/schemas/Nope"}}}
    bad["paths"]["/items"]["post"]["security"] = [{"oauth": []}]
    bad["paths"]["/items"]["post"]["responses"] = {}
    issues = spec_checks.lint_openapi(bad)
    assert {"path-parameter-undeclared", "duplicate-operation-id", "unresolved-ref",
            "undefined-security-scheme", "responses-missing"} <= rules(issues)


def test_issue_line_numbers_from_yaml():
    text = "openapi: 3.0.0\ninfo:\n  title: t\n  version: '1'\npaths:\n  /a:\n    get:\n      responses: {}\n"
    report, summary = spec_checks.analyze_spec(text, "api.yaml", generate_tests=False)
    issue = next(i for i in report["issues"] if i["rule"] == "responses-missing")
    assert issue["line"] == 7
    assert summary["issues_error"] >= 1


def test_graphql_unknown_type_located():
    issues, schema = spec_checks.lint_graphql("type Query {\n  posts: [Post]\n}\n")
    assert schema is None
    assert issues[0]["line"] == 2 and "Post" in issues[0]["message"]


def test_graphql_syntax_error():
    issues, _ = spec_checks.lint_graphql("type Query {\n  a: String\n")
    assert issues[0]["rule"] == "graphql-syntax"


def test_json_duplicate_keys_detected():
    report, _ = spec_checks.analyze_spec('{"a": 1,\n "a": 2}', "x.json")
    dup = next(i for i in report["issues"] if i["rule"] == "duplicate-key")
    assert dup["line"] == 2


def test_json_schema_document_meta_validated():
    doc = json.dumps({"$schema": "http://json-schema.org/draft-07/schema#", "type": "objekt"})
    report, _ = spec_checks.analyze_spec(doc, "schema.json")
    assert any(i["rule"] == "meta-schema" for i in report["issues"])


def test_example_values_follow_schema():
    doc = {"components": {"schemas": {"P": {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer", "minimum": 5}}}}}}
    assert spec_checks.example_value({"$ref": "#/components/schemas/P"}, doc) == {"n": 5}
    assert spec_checks.example_value({"type": "string", "format": "uuid"}, doc).count("-") == 4
    assert spec_checks.example_value({"enum": ["a", "b"]}, doc) == "a"


@responses.activate
def test_live_checks_find_undocumented_status_and_schema_violation():
    base = "https://api.example.test"
    responses.get(f"{base}/items/1", json={"id": "not-an-int", "name": "x", "note": None})
    responses.get(f"{base}/search", status=500)
    report = spec_checks.live_checks_openapi(SPEC, base, allow_mutations=False)
    ops = {o["operation"]: o for o in report["operations"]}
    item = ops["GET /items/{id}"]
    assert item["status"] == "failed"
    schema_check = next(c for c in item["checks"] if c["name"] == "response matches schema")
    assert schema_check["passed"] is False and "/id" in schema_check["detail"]
    search = ops["GET /search"]
    assert next(c for c in search["checks"] if c["name"] == "status code documented")["passed"] is False
    assert ops["POST /items"]["status"] == "skipped"  # write methods need explicit opt-in


@responses.activate
def test_live_checks_pass_for_conforming_server():
    base = "https://api.example.test"
    responses.get(f"{base}/items/1", json={"id": 1, "name": "x", "note": None})
    responses.get(f"{base}/search", json={}, match=[responses.matchers.query_param_matcher({"q": "test"})])
    responses.get(f"{base}/search", status=422)
    report = spec_checks.live_checks_openapi(SPEC, base, allow_mutations=False)
    ops = {o["operation"]: o for o in report["operations"]}
    assert ops["GET /items/{id}"]["status"] == "passed"
    assert ops["GET /search"]["status"] == "passed"
    neg = next(c for c in ops["GET /search"]["checks"] if c["name"].startswith("rejects missing"))
    assert neg["passed"] is True


def test_guard_url_blocks_private_targets(monkeypatch):
    monkeypatch.setenv("ALLOW_PRIVATE_TARGETS", "false")
    for url in ("http://127.0.0.1:8000", "http://169.254.169.254/latest", "http://10.0.0.5"):
        with pytest.raises(ValueError):
            spec_checks.guard_url(url)
    with pytest.raises(ValueError):
        spec_checks.guard_url("file:///etc/passwd")


def test_detect_kind():
    assert spec_checks.detect_kind('{"openapi": "3.0.0"}', "")[0] == "openapi"
    assert spec_checks.detect_kind("type Query { a: Int }", "")[0] == "graphql"
    assert spec_checks.detect_kind('[1, 2]', "data.json")[0] == "json"
    assert spec_checks.detect_kind("a: 1\n", "c.yaml")[0] == "yaml"
    with pytest.raises(ValueError):
        spec_checks.detect_kind("just words", "notes.txt")
