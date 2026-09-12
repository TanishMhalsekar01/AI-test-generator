# AI Test Generator

Built for [SkillUp Hackathon in collaboration with IBM] using **IBM Bob** as the primary development environment.
=======
AI Test Generator reads Python source code (or an API spec), extracts structured
facts about every function or endpoint using deterministic static analysis, sends
those facts to an LLM to produce a pytest test suite, then actually executes the
generated tests with `pytest` + `coverage.py` and returns real pass/fail counts
and coverage percentages. Nothing is mocked in the reporting — if a test fails,
it failed; if coverage is 80%, it was measured as 80%.

Built for IBM Hackathon using **IBM Bob** as the primary development environment.
>>>>>>> 349bef9 (Update AI test generator)

---

## The Problem

Writing comprehensive unit tests is time-consuming and error-prone. Developers
often test only the "happy path," forgetting edge cases like boundary values,
invalid inputs, and exception handling. This is especially true for students
and small teams without dedicated QA support, leading to bugs that surface
only after deployment.

---

## Architecture: the four-stage pipeline

```
Upload / Ingest  →  Parse (deterministic)  →  Generate (LLM)  →  Run + Report
```

| Stage | File | What it does |
|---|---|---|
| **1. Upload / Ingest** | `main.py` | Accepts a `.py` file upload, a public GitHub repo URL, or an API spec file. Routes the input to the correct pipeline. |
| **2. Parse** | `parser.py` / `spec_parser.py` | Extracts structured facts — function signatures, conditions, raised exceptions, return values, risk flags — using Python's built-in `ast` module or a deterministic spec parser. **No AI involved here.** The LLM never sees raw source code. |
| **3. Generate** | `llm.py` / `spec_llm.py` | Sends the structured facts (not raw code) to Llama 3.3 70B via Groq. Generates a complete pytest test suite covering normal paths, boundary conditions, invalid inputs, and declared error responses. |
| **4. Run + Report** | `runner.py` | Writes the function source and generated tests to an isolated temp directory, executes them with `pytest --cov`, and returns actual pass/fail counts and a real coverage percentage. |
| **GitHub fetcher** | `github_fetcher.py` | Walks a public repo's tree via the GitHub Contents API, downloads every `.py` file up to the configured cap, and feeds them individually through the pipeline. |

### Why separate Parse from Generate?

Sending raw source to an LLM produces tests that look plausible but often assert
the wrong behavior. By extracting structured facts first (conditions, raises,
returns, flags), the LLM prompt is grounded and significantly less likely to
hallucinate assertions. This is the core architectural choice of the project.

### Worked example

**Input function:**
```python
def calculate_discount(price, discount_percent):
    if discount_percent > 100:
        raise ValueError("Discount cannot exceed 100%")
    final_price = price - (price * discount_percent / 100)
    return final_price
```

**What the parser extracts (this is what goes to the LLM):**
```
Function signature : calculate_discount(price, discount_percent)
Logic summary      : Conditions: discount_percent > 100 | Raises: ValueError(...) | Returns: final_price
Flags              : no validation found for parameter 'price'
```

**Result after running the generated tests:**
```
Tests generated: 8   Tests passed: 8   Coverage: 100%
```

---

## Three ways to submit code

<<<<<<< HEAD
- **Backend:** Python, FastAPI
- **Parsing:** Python's built-in `ast` module + lightweight custom static analysis
- **AI layer:** LLM API (openai/gpt-oss-120b) -- structured-prompt based test generation
- **Test execution:** pytest + pytest-cov
- **Development environment:** IBM Bob (see below)
=======
### `POST /analyze` — upload a single Python file

Accepts a `.py` file upload. Runs the full pipeline for every top-level function
in the file and returns results as a JSON array.

```bash
curl -X POST http://localhost:8000/analyze \
     -F "file=@path/to/your_module.py"
```

**Response** — a JSON array, one object per top-level function:

```json
[
  {
    "name": "calculate_discount",
    "signature": "calculate_discount(price, discount_percent)",
    "docstring": null,
    "conditions": ["discount_percent > 100"],
    "raises": ["ValueError('Discount cannot exceed 100%')"],
    "returns": ["final_price"],
    "logic_summary": "Conditions: discount_percent > 100 | Raises: ... | Returns: final_price",
    "flags": ["no validation found for parameter 'price'"],
    "tests_generated": 8,
    "tests_passed": 8,
    "tests_failed": 0,
    "coverage_percent": 100.0
  }
]
```

---

### `POST /analyze-repo` — analyze a public GitHub repository

Accepts a JSON body with a GitHub repo URL and an optional file cap. Fetches
every `.py` file up to `max_files`, runs each through the full pipeline, and
returns a structured report object.

```bash
curl -X POST http://localhost:8000/analyze-repo \
     -H "Content-Type: application/json" \
     -d '{"github_url": "https://github.com/psf/requests", "max_files": 10}'
```

**Request body:**

| Field | Type | Default | Description |
|---|---|---|---|
| `github_url` | string | required | Full HTTPS URL of a public GitHub repo. Trailing `.git`, whitespace, and trailing slashes are normalised automatically. |
| `max_files` | int | 50 | Maximum `.py` files to fetch and analyse (1–200). Files beyond the cap are counted and reported but not processed. |

**Response** — a JSON object with top-level metadata and a `results` array:

```json
{
  "max_files": 10,
  "python_files_found": 47,
  "files_selected": 10,
  "files_skipped_due_to_limit": 37,
  "files_analyzed": 10,
  "results": [
    {
      "file": "src/adapters/utils.py",
      "functions": [
        {
          "name": "to_key_val_list",
          "signature": "to_key_val_list(value)",
          "tests_generated": 5,
          "tests_passed": 5,
          "tests_failed": 0,
          "coverage_percent": 94.3,
          "generation_mode": "llm",
          "coverage_status": "measured",
          "warning": null
        }
      ]
    }
  ]
}
```

**Error responses:**

| Status | Condition |
|---|---|
| `400` | Invalid or non-GitHub URL; `max_files` outside 1–200 |
| `502` | GitHub API failure (rate limit, repo not found, network timeout) |

---

### `POST /analyze-spec` — analyze an OpenAPI/Swagger or GraphQL SDL spec

Accepts a spec file upload (or a `spec_url` form field pointing to a public URL).
Detects the spec type automatically, extracts every endpoint or GraphQL operation,
generates pytest tests using the `requests` library for each one, runs them, and
returns a per-operation report.

```bash
# File upload (OpenAPI JSON or YAML, or GraphQL SDL)
curl -X POST http://localhost:8000/analyze-spec \
     -F "file=@openapi.json"

# With a live server base URL (live mode)
curl -X POST http://localhost:8000/analyze-spec \
     -F "file=@openapi.json" \
     -F "base_url=http://localhost:8080"

# From a public URL instead of a file upload
curl -X POST http://localhost:8000/analyze-spec \
     -F "spec_url=https://petstore3.swagger.io/api/v3/openapi.json"
```

**Form fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `file` | file | — | Uploaded spec file. Mutually exclusive with `spec_url`. |
| `spec_url` | string | — | URL of a publicly reachable spec. Mutually exclusive with `file`. |
| `base_url` | string | `""` | Base URL for live mode. If omitted, tests stub HTTP calls with `responses` and require no running server. |

**Response:**

```json
{
  "spec_type": "openapi",
  "operations_processed": 12,
  "operations_skipped": 0,
  "failed_operations": [],
  "results": [
    {
      "operation": "GET /pets",
      "spec_type": "openapi",
      "method": "GET",
      "path": "/pets",
      "summary": "List all pets",
      "logic_summary": "Optional params: limit(integer in query) | Success codes: 200 | Error codes: default",
      "tests_generated": 1,
      "tests_passed": 1,
      "tests_failed": 0,
      "coverage_percent": 0.0
    }
  ]
}
```

**Supported spec formats:**

| Format | Detection |
|---|---|
| OpenAPI 3.x | JSON or YAML containing an `"openapi"` key |
| Swagger 2.x | JSON or YAML containing a `"swagger"` key |
| GraphQL SDL | Text containing `type Query`, `type Mutation`, or `schema { }` |

Operations beyond the 50-operation cap are counted and skipped; the response
includes a `warning` field explaining how many were skipped.

---

## `generation_mode` transparency

Every function result in `/analyze-repo` (and fallback cases elsewhere) includes
three transparency fields:

| Field | `"llm"` mode | `"mock"` mode |
|---|---|---|
| `generation_mode` | `"llm"` | `"mock"` |
| `coverage_status` | `"measured"` | `"not_measured"` |
| `coverage_percent` | A real measured float (e.g. `94.3`) | `null` — explicitly not `0` |
| `warning` | `null` | Explanation string |

**Why `null` instead of `0` for mock coverage?**

In mock mode, the placeholder test is `def test_fn_placeholder(): pass` — it
runs and passes, but it never imports or calls the target function. Reporting
`0%` coverage would be accurate but misleading: it implies coverage was
measured and came up zero, when in fact it was never attempted. `null` is the
honest value.

**When does mock mode trigger?**

- `GROQ_API_KEY` is not set in the environment, or
- The LLM API call fails for any reason (network, rate limit, API error)

The application does not crash in either case. It falls back to the placeholder
and clearly labels the result. This is intentional: the pipeline should always
complete and return useful structural information even without a working LLM
connection.

---

## Web UI

The project ships a zero-build, zero-npm frontend served directly by FastAPI at
`http://localhost:8000/ui/`.

It is three tabs of plain HTML, CSS, and vanilla JavaScript — no React, no
bundler, no build step required. The static files live in `backend/static/`
and are mounted by FastAPI's `StaticFiles`.

**Tab 1 — Python File:** Drag-and-drop (or click-to-browse) a `.py` file.
Calls `POST /analyze`. Results render as expandable function cards with stats
rows, a coverage progress bar, and a flags list.

**Tab 2 — GitHub Repo:** Text input for the repo URL and a `max_files` number
input. Calls `POST /analyze-repo`. Results show the response metadata
(`python_files_found`, `files_selected`, `files_skipped_due_to_limit`,
`files_analyzed`, `max_files`) as prominent summary chips above the per-file
expandable cards. Mock-mode functions show a yellow `MOCK` badge and "Coverage:
Not measured" — never "0%". LLM-mode functions show a blue `LLM` badge and
a real coverage bar.

**Tab 3 — API Spec:** Drag-and-drop an OpenAPI/Swagger or GraphQL SDL file,
with an optional `base_url` field for live-server mode. Calls
`POST /analyze-spec`. Results render as operation cards with color-coded
HTTP method badges.

All three tabs share a "Download full JSON report" button and display backend
error messages (400 / 502 / network failure) as readable text, not raw JSON.

---

## Setup

```bash
# 1. Clone and enter the backend directory
git clone https://github.com/<owner>/<repo>.git
cd <repo>/backend

# 2. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Optional) Set environment variables
export GROQ_API_KEY="gsk_..."   # Enables LLM test generation
                                # Without this the app still runs in mock mode —
                                # it will not crash or refuse requests
export GITHUB_TOKEN="ghp_..."   # Raises GitHub API rate limit from 60 to 5000 req/h
                                # Without this public repos work fine below the limit

# 5. Start the server
python -m uvicorn main:app --reload
```

Then open:
- **`http://localhost:8000/ui/`** — web interface
- **`http://localhost:8000/docs`** — Swagger UI for the raw API

---

## Testing

```bash
cd backend
python -m pytest -q
```

The suite currently has **104 tests** across three test files:

| File | What it covers |
|---|---|
| `test_analyze_repo.py` | `parse_github_url`, `fetch_python_files` (mocked network), `POST /analyze-repo` — including new response shape, mock-mode fields, LLM-mode fields, cap behavior |
| `test_analyze_spec.py` | `detect_spec_type`, `parse_openapi`, `parse_graphql`, `parse_spec`, `build_spec_user_prompt`, `generate_spec_tests` (mocked LLM), `POST /analyze-spec` |
| `test_parser.py` | The core `parser.py` AST extraction logic |

All network calls (GitHub API, Groq API) are mocked — no real API hits during
`pytest`. CI runs automatically on every push and pull request to `main` via
[GitHub Actions](.github/workflows/pytest.yml).

---

## Known Limitations

- **GitHub API rate limit:** Unauthenticated requests are capped at 60/hour.
  Set `GITHUB_TOKEN` to raise this to 5,000/hour. Large repos with many files
  hit this quickly.
- **`max_files` cap:** `/analyze-repo` processes at most 200 files per request
  (default 50). Files beyond the cap are skipped and counted in the response
  metadata but not analysed.
- **Python only:** The code parser and test runner support Python source files.
  Other languages are out of scope for this build.
- **No auth on endpoints:** The API has no authentication layer. It is intended
  for local or trusted-network use during a hackathon demo.
>>>>>>> 349bef9 (Update AI test generator)

---

## How IBM Bob Was Used

IBM Bob was our primary development environment throughout this build. We used
it as an active collaborator rather than a black box — scoping tasks precisely,
reviewing every generated change, and manually verifying behavior at each stage.

- **Scaffolding:** Bob generated the initial structure for our parser, FastAPI
  backend, and test-runner from detailed, scoped prompts.
- **Iterative debugging:** Bob's integrated terminal and agent chat were used
  to diagnose real issues (environment setup, import errors, API integration
  bugs) as they came up.
- **Feature extension:** When we found our parser couldn't detect `try`/`except`
  blocks, we used Bob to add that capability, then validated it ourselves with
  targeted test cases before accepting the change.
- **A real lesson learned:** In several cases, Bob's automated edits altered
  code outside the requested scope (e.g. renaming a core function during an
  unrelated refactor). We caught this by manually re-verifying behavior after
  every change rather than assuming generated code was correct — a discipline
  that caught multiple real bugs during development.
- **Scope discipline at scale:** As the project grew (three endpoints, a spec
  parser, a GitHub fetcher, a frontend, 104 tests), keeping each Bob task
  tightly scoped with explicit "do not touch X" rules became increasingly
  important. Tasks with loose scope produced changes that looked correct but
  silently broke adjacent behavior.

<<<<<<< HEAD
Our workflow throughout: **scope a task precisely -> review the diff -> manually
test actual behavior -> only then move forward.**

---

## Known Limitations / Future Work

Scoped out deliberately for this build, listed here as an honest roadmap:

- Multi-language support (currently Python only)
- API/Swagger/GraphQL test generation module
- GitHub repo ingestion / CI-CD integration
- Team dashboards and multi-project history

## Setup

```bash
cd backend
pip install -r requirements.txt
export GROQ_API_KEY="your_key_here"   # or set via $env: on Windows
uvicorn main:app --reload
```

Then POST a `.py` file to `http://localhost:8000/analyze`.
=======
Our workflow throughout: **scope a task precisely → review the diff → manually
test actual behavior → only then move forward.**
>>>>>>> 349bef9 (Update AI test generator)
