"""
Tests for:
  - spec_parser.detect_spec_type
  - spec_parser.parse_openapi
  - spec_parser.parse_graphql
  - spec_parser.parse_spec
  - spec_llm.build_spec_user_prompt
  - spec_llm.generate_spec_tests  (LLM API mocked)
  - POST /analyze-spec  (FastAPI TestClient + all network mocked)

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

from fastapi.testclient import TestClient
from main import app  # noqa: E402

client = TestClient(app)

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

    def test_rest_prompt_mock_mode_by_default(self):
        prompt = build_spec_user_prompt(self._rest_op())
        assert "MOCK" in prompt
        assert "responses" in prompt.lower()

    def test_rest_prompt_live_mode_when_base_url_given(self):
        prompt = build_spec_user_prompt(self._rest_op(), base_url="http://localhost:8080")
        assert "LIVE" in prompt
        assert "http://localhost:8080" in prompt

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
# generate_spec_tests  (LLM path mocked)
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

    def test_mock_fallback_when_no_api_key(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        code = generate_spec_tests(self._simple_op())
        assert "def test_" in code

    def test_mock_fallback_includes_placeholder(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        code = generate_spec_tests(self._simple_op())
        assert "placeholder" in code

    def test_live_mode_prompt_reflected_in_mock(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        code = generate_spec_tests(self._simple_op(), base_url="http://localhost:9000")
        # The mock always returns placeholder regardless of mode — just check it runs
        assert "def test_" in code

    def test_llm_success_path(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        fake_response = MagicMock()
        fake_response.raise_for_status = MagicMock()
        fake_response.json.return_value = {
            "choices": [{"message": {"content": "def test_health():\n    pass\n"}}]
        }
        with patch("spec_llm._requests.post", return_value=fake_response):
            code = generate_spec_tests(self._simple_op())
        assert "def test_health" in code

    def test_llm_failure_falls_back_to_mock(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        with patch("spec_llm._requests.post", side_effect=ConnectionError("offline")):
            code = generate_spec_tests(self._simple_op())
        assert "def test_" in code

    def test_llm_strips_markdown_fences(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        fake_response = MagicMock()
        fake_response.raise_for_status = MagicMock()
        fenced = "```python\ndef test_foo():\n    pass\n```"
        fake_response.json.return_value = {
            "choices": [{"message": {"content": fenced}}]
        }
        with patch("spec_llm._requests.post", return_value=fake_response):
            code = generate_spec_tests(self._simple_op())
        assert "```" not in code
        assert "def test_foo" in code


# ---------------------------------------------------------------------------
# POST /analyze-spec  (FastAPI endpoint)
# ---------------------------------------------------------------------------

def _make_llm_response(content: str) -> MagicMock:
    """Build a minimal mock that looks like a successful LLM response."""
    m = MagicMock()
    m.raise_for_status = MagicMock()
    m.json.return_value = {"choices": [{"message": {"content": content}}]}
    return m


PLACEHOLDER_TEST = "import pytest\ndef test_placeholder():\n    pass\n"


class TestAnalyzeSpecEndpoint:

    # ---- happy path: OpenAPI file upload ----

    def test_openapi_file_upload_returns_200(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        resp = client.post(
            "/analyze-spec",
            files={"file": ("spec.json", io.BytesIO(MINIMAL_OPENAPI_JSON.encode()), "application/json")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["spec_type"] == "openapi"
        assert data["operations_processed"] > 0

    def test_openapi_response_shape(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        resp = client.post(
            "/analyze-spec",
            files={"file": ("spec.json", io.BytesIO(MINIMAL_OPENAPI_JSON.encode()), "application/json")},
        )
        data = resp.json()
        assert "results" in data
        assert "failed_operations" in data
        assert "operations_skipped" in data
        first = data["results"][0]
        assert "operation" in first
        assert "tests_generated" in first
        assert "method" in first

    # ---- happy path: GraphQL SDL file upload ----

    def test_graphql_file_upload_returns_200(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        resp = client.post(
            "/analyze-spec",
            files={"file": ("schema.graphql", io.BytesIO(MINIMAL_GRAPHQL_SDL.encode()), "text/plain")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["spec_type"] == "graphql"
        assert data["operations_processed"] > 0

    def test_graphql_response_has_gql_operation_field(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        resp = client.post(
            "/analyze-spec",
            files={"file": ("schema.graphql", io.BytesIO(MINIMAL_GRAPHQL_SDL.encode()), "text/plain")},
        )
        data = resp.json()
        first = data["results"][0]
        assert "gql_operation" in first

    # ---- spec_url path ----

    def test_spec_url_fetched_and_parsed(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = MINIMAL_OPENAPI_JSON
        with patch("main._http_requests.get", return_value=mock_resp):
            resp = client.post(
                "/analyze-spec",
                data={"spec_url": "https://example.com/openapi.json"},
            )
        assert resp.status_code == 200
        assert resp.json()["spec_type"] == "openapi"

    def test_invalid_spec_url_returns_400(self):
        with patch("main._http_requests.get", side_effect=ConnectionError("unreachable")):
            resp = client.post(
                "/analyze-spec",
                data={"spec_url": "https://example.com/bad.json"},
            )
        assert resp.status_code == 400
        assert "Could not fetch" in resp.json()["detail"]

    # ---- no input ----

    def test_no_file_no_url_returns_400(self):
        resp = client.post("/analyze-spec")
        assert resp.status_code == 400
        assert "file upload" in resp.json()["detail"].lower() or "spec_url" in resp.json()["detail"]

    # ---- malformed spec ----

    def test_malformed_json_returns_400(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        bad = b'{"openapi": "3.0.0", "paths": {invalid}}'
        resp = client.post(
            "/analyze-spec",
            files={"file": ("spec.json", io.BytesIO(bad), "application/json")},
        )
        assert resp.status_code == 400

    def test_unrecognised_spec_returns_400(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        resp = client.post(
            "/analyze-spec",
            files={"file": ("file.txt", io.BytesIO(b"Hello world"), "text/plain")},
        )
        assert resp.status_code == 400

    # ---- empty spec ----

    def test_empty_spec_returns_400(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        resp = client.post(
            "/analyze-spec",
            files={"file": ("spec.json", io.BytesIO(EMPTY_OPENAPI.encode()), "application/json")},
        )
        assert resp.status_code == 400
        assert "no endpoint" in resp.json()["detail"].lower()

    # ---- oversized spec (cap triggered) ----

    def test_oversized_spec_processes_cap_and_reports_skipped(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        big_spec = _make_big_openapi(MAX_OPERATIONS + 3).encode()
        resp = client.post(
            "/analyze-spec",
            files={"file": ("big.json", io.BytesIO(big_spec), "application/json")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["operations_skipped"] == 3
        assert data["operations_processed"] == MAX_OPERATIONS
        assert "warning" in data

    # ---- LLM failure for one operation: skip and report ----

    def test_llm_failure_skipped_and_reported(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        # Make the LLM always raise an exception
        with patch("main.generate_spec_tests", side_effect=RuntimeError("LLM down")):
            resp = client.post(
                "/analyze-spec",
                files={"file": ("spec.json", io.BytesIO(MINIMAL_OPENAPI_JSON.encode()), "application/json")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["failed_operations"]) > 0
        assert data["operations_processed"] == 0

    # ---- mock mode: no base_url ----

    def test_mock_mode_no_base_url(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        resp = client.post(
            "/analyze-spec",
            files={"file": ("spec.json", io.BytesIO(MINIMAL_OPENAPI_JSON.encode()), "application/json")},
        )
        assert resp.status_code == 200
        # In mock mode all operations should be processed without a real server
        data = resp.json()
        assert data["operations_processed"] > 0

    # ---- live base_url mode ----

    def test_live_base_url_mode(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        fake_llm = MagicMock()
        fake_llm.raise_for_status = MagicMock()
        fake_llm.json.return_value = {
            "choices": [{"message": {"content": PLACEHOLDER_TEST}}]
        }
        with patch("spec_llm._requests.post", return_value=fake_llm):
            resp = client.post(
                "/analyze-spec",
                files={"file": ("spec.json", io.BytesIO(MINIMAL_OPENAPI_JSON.encode()), "application/json")},
                data={"base_url": "http://localhost:8080"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["operations_processed"] > 0

    # ---- existing /analyze endpoint must still work ----

    def test_existing_analyze_endpoint_unaffected(self):
        simple_py = b"def hello():\n    return 'hello'\n"
        resp = client.post(
            "/analyze",
            files={"file": ("sample.py", io.BytesIO(simple_py), "text/plain")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data[0]["name"] == "hello"
