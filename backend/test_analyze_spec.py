"""
Tests for:
  - spec_parser.detect_spec_type
  - spec_parser.parse_openapi
  - spec_parser.parse_graphql
  - spec_parser.parse_spec
  - spec_llm.build_spec_user_prompt
  - spec_llm.generate_spec_tests  (Gemini mocked)
  - POST /api/runs/spec  (signed-in TestClient, network mocked)

Run with:
    cd backend
    pytest test_analyze_spec.py -v
"""

import io
import json
import sys
import os
import pytest
from unittest.mock import patch, MagicMock

# Allow running from the repo root
sys.path.insert(0, os.path.dirname(__file__))

from spec_parser import (
    detect_spec_type,
    parse_openapi,
    parse_graphql,
    parse_spec,
    OperationInfo,
    ParamInfo,
    ResponseInfo,
    MAX_OPERATIONS,
    _SkippedOperationsError,
)
from spec_llm import build_spec_user_prompt, generate_spec_tests

import gemini_client
from conftest import gemini_response

# ---------------------------------------------------------------------------
# Fixtures / shared spec strings
# ---------------------------------------------------------------------------

MINIMAL_OPENAPI_JSON = json.dumps({
    "openapi": "3.0.0",
    "info": {"title": "Test API", "version": "1.0"},
    "paths": {
        "/users": {
            "get": {
                "summary": "List users",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer"},
                    }
                ],
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Bad Request"},
                },
            }
        },
        "/users/{id}": {
            "get": {
                "summary": "Get user by id",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {"description": "OK"},
                    "404": {"description": "Not Found"},
                },
            },
            "delete": {
                "summary": "Delete user",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "204": {"description": "No Content"},
                    "404": {"description": "Not Found"},
                },
            },
        },
        "/users/{id}/profile": {
            "post": {
                "summary": "Update profile",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "email": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Updated"},
                    "422": {"description": "Validation Error"},
                },
            }
        },
    },
})

MINIMAL_OPENAPI_YAML = """\
openapi: "3.0.0"
info:
  title: YAML API
  version: "1.0"
paths:
  /items:
    get:
      summary: List items
      responses:
        "200":
          description: OK
"""

MINIMAL_GRAPHQL_SDL = """\
type Query {
  getUser(id: ID!): User
  listUsers(limit: Int): [User]
}

type Mutation {
  createUser(name: String!, email: String!): User
  deleteUser(id: ID!): Boolean
}

type User {
  id: ID!
  name: String!
  email: String
}
"""

MALFORMED_JSON = '{"openapi": "3.0.0", "paths": {invalid}}'
MALFORMED_YAML = "openapi: 3.0.0\npaths:\n  - bad: [unclosed"
EMPTY_OPENAPI = json.dumps({"openapi": "3.0.0", "info": {}, "paths": {}})
NOT_A_SPEC = "Hello, this is just some random text."
NOT_API_JSON = json.dumps({"key": "value", "something": [1, 2, 3]})


def _make_big_openapi(n: int) -> str:
    """Generate an OpenAPI spec with n GET endpoints."""
    paths = {}
    for i in range(n):
        paths[f"/resource{i}"] = {
            "get": {
                "summary": f"Resource {i}",
                "responses": {"200": {"description": "OK"}},
            }
        }
    return json.dumps({"openapi": "3.0.0", "info": {}, "paths": paths})


# ---------------------------------------------------------------------------
# detect_spec_type
# ---------------------------------------------------------------------------

class TestDetectSpecType:
    def test_openapi_json(self):
        assert detect_spec_type(MINIMAL_OPENAPI_JSON) == "openapi"

    def test_openapi_yaml(self):
        assert detect_spec_type(MINIMAL_OPENAPI_YAML) == "openapi"

    def test_swagger_key(self):
        spec = json.dumps({"swagger": "2.0", "paths": {}})
        assert detect_spec_type(spec) == "openapi"

    def test_graphql_query_type(self):
        assert detect_spec_type(MINIMAL_GRAPHQL_SDL) == "graphql"

    def test_graphql_mutation_only(self):
        sdl = "type Mutation {\n  doThing(x: String!): Boolean\n}"
        assert detect_spec_type(sdl) == "graphql"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            detect_spec_type("")

    def test_random_text_raises(self):
        with pytest.raises(ValueError):
            detect_spec_type(NOT_A_SPEC)

    def test_json_without_openapi_key_raises(self):
        with pytest.raises(ValueError, match="openapi"):
            detect_spec_type(NOT_API_JSON)


# ---------------------------------------------------------------------------
# parse_openapi
# ---------------------------------------------------------------------------

class TestParseOpenapi:
    def test_returns_operations_for_all_methods(self):
        ops = parse_openapi(MINIMAL_OPENAPI_JSON)
        names = [op.name for op in ops]
        assert "GET /users" in names
        assert "GET /users/{id}" in names
        assert "DELETE /users/{id}" in names
        assert "POST /users/{id}/profile" in names

    def test_path_param_is_required(self):
        ops = parse_openapi(MINIMAL_OPENAPI_JSON)
        get_id = next(op for op in ops if op.name == "GET /users/{id}")
        id_param = next(p for p in get_id.params if p.name == "id")
        assert id_param.required is True
        assert id_param.location == "path"

    def test_query_param_optional(self):
        ops = parse_openapi(MINIMAL_OPENAPI_JSON)
        get_users = next(op for op in ops if op.name == "GET /users")
        limit_param = next(p for p in get_users.params if p.name == "limit")
        assert limit_param.required is False
        assert limit_param.location == "query"

    def test_error_responses_separated(self):
        ops = parse_openapi(MINIMAL_OPENAPI_JSON)
        get_users = next(op for op in ops if op.name == "GET /users")
        error_codes = [r.status_code for r in get_users.error_responses]
        assert "400" in error_codes

    def test_success_response_in_responses(self):
        ops = parse_openapi(MINIMAL_OPENAPI_JSON)
        get_users = next(op for op in ops if op.name == "GET /users")
        success_codes = [r.status_code for r in get_users.responses]
        assert "200" in success_codes

    def test_request_body_fields_become_params(self):
        ops = parse_openapi(MINIMAL_OPENAPI_JSON)
        post_op = next(op for op in ops if op.name == "POST /users/{id}/profile")
        param_names = [p.name for p in post_op.params]
        assert "name" in param_names
        assert "email" in param_names

    def test_required_body_field(self):
        ops = parse_openapi(MINIMAL_OPENAPI_JSON)
        post_op = next(op for op in ops if op.name == "POST /users/{id}/profile")
        name_param = next(p for p in post_op.params if p.name == "name")
        assert name_param.required is True

    def test_yaml_input(self):
        ops = parse_openapi(MINIMAL_OPENAPI_YAML)
        assert len(ops) == 1
        assert ops[0].name == "GET /items"

    def test_empty_paths_raises(self):
        with pytest.raises(ValueError, match="no endpoint"):
            parse_openapi(EMPTY_OPENAPI)

    def test_malformed_json_raises(self):
        with pytest.raises(ValueError):
            parse_openapi(MALFORMED_JSON)

    def test_cap_triggers_skipped_error(self):
        big_spec = _make_big_openapi(MAX_OPERATIONS + 5)
        with pytest.raises(_SkippedOperationsError) as exc_info:
            parse_openapi(big_spec)
        err = exc_info.value
        assert err.skipped == 5
        assert len(err.operations) == MAX_OPERATIONS

    def test_op_type_is_rest(self):
        ops = parse_openapi(MINIMAL_OPENAPI_JSON)
        assert all(op.op_type == "rest" for op in ops)

    def test_logic_summary_contains_param_info(self):
        ops = parse_openapi(MINIMAL_OPENAPI_JSON)
        get_id = next(op for op in ops if op.name == "GET /users/{id}")
        summary = get_id.logic_summary()
        assert "id" in summary


# ---------------------------------------------------------------------------
# parse_graphql
# ---------------------------------------------------------------------------

class TestParseGraphQL:
    def test_extracts_query_fields(self):
        ops = parse_graphql(MINIMAL_GRAPHQL_SDL)
        names = [op.name for op in ops]
        assert "getUser" in names
        assert "listUsers" in names

    def test_extracts_mutation_fields(self):
        ops = parse_graphql(MINIMAL_GRAPHQL_SDL)
        names = [op.name for op in ops]
        assert "createUser" in names
        assert "deleteUser" in names

    def test_gql_operation_field(self):
        ops = parse_graphql(MINIMAL_GRAPHQL_SDL)
        get_user = next(op for op in ops if op.name == "getUser")
        assert get_user.gql_operation == "query"
        create_user = next(op for op in ops if op.name == "createUser")
        assert create_user.gql_operation == "mutation"

    def test_non_null_arg_is_required(self):
        ops = parse_graphql(MINIMAL_GRAPHQL_SDL)
        get_user = next(op for op in ops if op.name == "getUser")
        id_arg = next(p for p in get_user.params if p.name == "id")
        assert id_arg.required is True
        assert id_arg.nullable is False

    def test_nullable_arg_is_optional(self):
        ops = parse_graphql(MINIMAL_GRAPHQL_SDL)
        list_users = next(op for op in ops if op.name == "listUsers")
        limit_arg = next(p for p in list_users.params if p.name == "limit")
        assert limit_arg.required is False
        assert limit_arg.nullable is True

    def test_return_type_extracted(self):
        ops = parse_graphql(MINIMAL_GRAPHQL_SDL)
        get_user = next(op for op in ops if op.name == "getUser")
        assert get_user.return_type == "User"

    def test_list_return_type(self):
        ops = parse_graphql(MINIMAL_GRAPHQL_SDL)
        list_users = next(op for op in ops if op.name == "listUsers")
        assert "[User]" in list_users.return_type

    def test_op_type_is_graphql(self):
        ops = parse_graphql(MINIMAL_GRAPHQL_SDL)
        assert all(op.op_type == "graphql" for op in ops)

    def test_empty_sdl_raises(self):
        with pytest.raises(ValueError, match="empty"):
            parse_graphql("")

    def test_sdl_with_no_query_or_mutation_raises(self):
        with pytest.raises(ValueError, match="No Query or Mutation"):
            parse_graphql("type User {\n  id: ID!\n}")

    def test_cap_triggers_skipped_error(self):
        # Build an SDL with too many fields
        fields = "\n".join(f"  op{i}(x: Int): Boolean" for i in range(MAX_OPERATIONS + 3))
        big_sdl = f"type Query {{\n{fields}\n}}"
        with pytest.raises(_SkippedOperationsError) as exc_info:
            parse_graphql(big_sdl)
        err = exc_info.value
        assert err.skipped == 3
        assert len(err.operations) == MAX_OPERATIONS

    def test_logic_summary_contains_arg_info(self):
        ops = parse_graphql(MINIMAL_GRAPHQL_SDL)
        create_user = next(op for op in ops if op.name == "createUser")
        summary = create_user.logic_summary()
        assert "name" in summary


# ---------------------------------------------------------------------------
# parse_spec (top-level router)
# ---------------------------------------------------------------------------

class TestParseSpec:
    def test_openapi_json_route(self):
        ops, spec_type, skipped = parse_spec(MINIMAL_OPENAPI_JSON)
        assert spec_type == "openapi"
        assert skipped == 0
        assert len(ops) > 0

    def test_graphql_route(self):
        ops, spec_type, skipped = parse_spec(MINIMAL_GRAPHQL_SDL)
        assert spec_type == "graphql"
        assert skipped == 0
        assert len(ops) > 0

    def test_skipped_count_propagated(self):
        big_spec = _make_big_openapi(MAX_OPERATIONS + 7)
        ops, spec_type, skipped = parse_spec(big_spec)
        assert spec_type == "openapi"
        assert skipped == 7
        assert len(ops) == MAX_OPERATIONS

    def test_invalid_spec_raises(self):
        with pytest.raises(ValueError):
            parse_spec(NOT_A_SPEC)


# ---------------------------------------------------------------------------
# build_spec_user_prompt
# ---------------------------------------------------------------------------

class TestBuildSpecUserPrompt:
    def _rest_op(self) -> OperationInfo:
        return OperationInfo(
            name="GET /pets",
            op_type="rest",
            summary="List pets",
            path="/pets",
            method="GET",
            params=[
                ParamInfo("limit", "query", False, "integer"),
                ParamInfo("status", "query", True, "string"),
            ],
            responses=[ResponseInfo("200", "OK", "array[object]")],
            error_responses=[ResponseInfo("400", "Bad Request", "")],
        )

    def _graphql_op(self) -> OperationInfo:
        return OperationInfo(
            name="getUser",
            op_type="graphql",
            summary="GraphQL query: getUser",
            gql_operation="query",
            return_type="User",
            return_nullable=True,
            params=[ParamInfo("id", "argument", True, "ID", nullable=False)],
        )

    def test_rest_prompt_contains_method_and_path(self):
        prompt = build_spec_user_prompt(self._rest_op())
        assert "GET" in prompt
        assert "/pets" in prompt

    def test_rest_prompt_without_server_says_suite_runs_later(self):
        prompt = build_spec_user_prompt(self._rest_op())
        assert "BASE_URL" in prompt
        assert "No server is available" in prompt

    def test_rest_prompt_with_base_url_targets_live_server(self):
        prompt = build_spec_user_prompt(self._rest_op(), base_url="http://localhost:8080")
        assert "http://localhost:8080" in prompt
        assert "BASE_URL" in prompt

    def test_graphql_prompt_contains_op_name(self):
        prompt = build_spec_user_prompt(self._graphql_op())
        assert "getUser" in prompt
        assert "query" in prompt

    def test_prompt_lists_required_param(self):
        prompt = build_spec_user_prompt(self._rest_op())
        assert "status" in prompt
        assert "required" in prompt

    def test_prompt_lists_error_response(self):
        prompt = build_spec_user_prompt(self._rest_op())
        assert "400" in prompt



# ---------------------------------------------------------------------------
# generate_spec_tests  (Gemini mocked)
# ---------------------------------------------------------------------------

class TestGenerateSpecTests:
    def _simple_op(self) -> OperationInfo:
        return OperationInfo(
            name="GET /health",
            op_type="rest",
            summary="Health check",
            path="/health",
            method="GET",
            params=[],
            responses=[ResponseInfo("200", "OK", "")],
            error_responses=[],
        )

    def test_no_api_key_raises_instead_of_placeholder(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(gemini_client.GeminiNotConfigured):
            generate_spec_tests(self._simple_op())

    def test_llm_success_path_adds_required_imports(self, mock_gemini):
        mock_gemini("def test_health():\n    assert requests.get(BASE_URL + '/health', timeout=10).status_code == 200\n")
        code = generate_spec_tests(self._simple_op())
        assert "def test_health" in code
        assert code.startswith("import os\nimport pytest\nimport requests")

    def test_llm_unavailable_raises(self, mock_gemini):
        mock_gemini(gemini_response("", 503))
        with pytest.raises(gemini_client.GeminiUnavailable):
            generate_spec_tests(self._simple_op())

    def test_llm_strips_markdown_fences(self, mock_gemini):
        mock_gemini("```python\nimport os\nimport pytest\nimport requests\ndef test_foo():\n    pass\n```")
        code = generate_spec_tests(self._simple_op())
        assert "```" not in code
        assert "def test_foo" in code


# ---------------------------------------------------------------------------
# POST /api/runs/spec
# ---------------------------------------------------------------------------

GENERATED = "import os\nimport pytest\nimport requests\n\ndef test_placeholder_shape():\n    assert True\n"


def _run(client, resp):
    assert resp.status_code == 202, resp.text
    return client.get(f"/api/runs/{resp.json()['id']}").json()


class TestSpecRunEndpoint:

    def test_openapi_upload_without_server_generates_but_does_not_execute(self, signed_in_client, mock_gemini):
        mock_gemini(GENERATED)
        run = _run(signed_in_client, signed_in_client.post(
            "/api/runs/spec", files={"file": ("spec.json", io.BytesIO(MINIMAL_OPENAPI_JSON.encode()), "application/json")}))
        assert run["status"] == "completed"
        report = run["report"]
        assert report["kind"] == "openapi"
        assert len(report["operations"]) > 0
        assert report["tests"][0]["code"]
        # No server given: the suite is not run, so nothing can be reported as passing.
        assert report["tests"][0]["execution"]["status"] == "not_executed"
        assert run["summary"]["tests_passed"] == 0

    def test_graphql_upload(self, signed_in_client, mock_gemini):
        mock_gemini(GENERATED)
        run = _run(signed_in_client, signed_in_client.post(
            "/api/runs/spec", files={"file": ("schema.graphql", io.BytesIO(MINIMAL_GRAPHQL_SDL.encode()), "text/plain")}))
        report = run["report"]
        assert report["kind"] == "graphql"
        assert report["operations"][0]["gql_operation"] in {"query", "mutation"}

    def test_spec_url_fetched(self, signed_in_client, mock_gemini):
        mock_gemini(GENERATED)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = MINIMAL_OPENAPI_JSON
        with patch("main._http_requests.get", return_value=mock_resp):
            run = _run(signed_in_client, signed_in_client.post("/api/runs/spec", data={"spec_url": "https://example.com/openapi.json"}))
        assert run["report"]["kind"] == "openapi"

    def test_unreachable_spec_url_returns_400(self, signed_in_client):
        import requests as real_requests
        with patch("main._http_requests.get", side_effect=real_requests.ConnectionError("unreachable")):
            resp = signed_in_client.post("/api/runs/spec", data={"spec_url": "https://example.com/bad.json"})
        assert resp.status_code == 400
        assert "Could not fetch" in resp.json()["detail"]

    def test_no_input_returns_400(self, signed_in_client):
        resp = signed_in_client.post("/api/runs/spec")
        assert resp.status_code == 400

    def test_malformed_json_reports_syntax_error_with_line(self, signed_in_client):
        run = _run(signed_in_client, signed_in_client.post(
            "/api/runs/spec", files={"file": ("spec.json", io.BytesIO(MALFORMED_JSON.encode()), "application/json")}))
        issue = run["report"]["issues"][0]
        assert issue["rule"] == "json-syntax" and issue["line"] == 1

    def test_unrecognised_text_returns_400(self, signed_in_client):
        resp = signed_in_client.post("/api/runs/spec", files={"file": ("file.txt", io.BytesIO(NOT_A_SPEC.encode()), "text/plain")})
        assert resp.status_code == 400

    def test_empty_openapi_is_linted(self, signed_in_client):
        run = _run(signed_in_client, signed_in_client.post(
            "/api/runs/spec", files={"file": ("spec.json", io.BytesIO(EMPTY_OPENAPI.encode()), "application/json")},
            data={"generate_tests": "false"}))
        rules = {i["rule"] for i in run["report"]["issues"]}
        assert {"paths-empty", "info-title-missing", "info-version-missing"} <= rules

    def test_oversized_spec_reports_skipped(self, signed_in_client):
        big_spec = _make_big_openapi(MAX_OPERATIONS + 3).encode()
        run = _run(signed_in_client, signed_in_client.post(
            "/api/runs/spec", files={"file": ("big.json", io.BytesIO(big_spec), "application/json")},
            data={"generate_tests": "false"}))
        assert run["report"]["operations_skipped"] == 3
        assert len(run["report"]["operations"]) == MAX_OPERATIONS

    def test_ai_unavailable_is_reported_not_faked(self, signed_in_client, mock_gemini):
        mock_gemini(gemini_response("", 503))
        run = _run(signed_in_client, signed_in_client.post(
            "/api/runs/spec", files={"file": ("spec.json", io.BytesIO(MINIMAL_OPENAPI_JSON.encode()), "application/json")}))
        assert run["status"] == "completed"
        assert "unavailable" in run["report"]["ai"]["error"]
        assert run["summary"]["tests_generated"] == 0

    def test_json_data_validated_against_schema(self, signed_in_client):
        schema = json.dumps({"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}})
        run = _run(signed_in_client, signed_in_client.post(
            "/api/runs/spec",
            files={"file": ("data.json", io.BytesIO(NOT_API_JSON.encode()), "application/json"),
                   "schema_file": ("schema.json", io.BytesIO(schema.encode()), "application/json")}))
        assert run["report"]["kind"] == "json"
        assert run["report"]["schema_validation"]["valid"] is False
        assert any(i["rule"] == "schema-required" for i in run["report"]["issues"])

    def test_private_base_url_blocked_when_not_allowed(self, signed_in_client, monkeypatch):
        monkeypatch.setenv("ALLOW_PRIVATE_TARGETS", "false")
        resp = signed_in_client.post(
            "/api/runs/spec", files={"file": ("spec.json", io.BytesIO(MINIMAL_OPENAPI_JSON.encode()), "application/json")},
            data={"base_url": "http://127.0.0.1:8080"})
        assert resp.status_code == 400
        assert "private" in resp.json()["detail"]
