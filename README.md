# AI Test Generator

AI Test Generator reviews source code in any programming language, generates unit tests for it,
executes those tests in a sandbox, and reports the defects it finds with line numbers. It also checks
API contracts: OpenAPI/Swagger documents, GraphQL schemas and JSON/YAML data, including live checks
against a running server. Users sign in with GitHub. Every run is stored against their account and
repositories, so history and team dashboards show only real, recorded results.

- Production URL (after DNS setup): **https://aitestge.stream**
- Model: Google Gemini **`gemini-3.1-pro-preview`** (server-side only)
- Stack: FastAPI · SQLAlchemy (Supabase Postgres / SQLite) · vanilla HTML/CSS/JS · Docker on Render

---

## What it does

| Area | What happens |
|---|---|
| **Code review** | Upload up to 10 files or paste code in any language. Each file goes through the real compiler or linter for its language (where installed), then Gemini reviews it for defects: logic errors, unhandled exceptions, null dereferences, security flaws, resource leaks, edge cases. Findings cite exact line numbers and include a suggested fix. |
| **Test execution** | Gemini writes a test file in the language's standard framework. The tests assert the *intended* behaviour (from names, docstrings and types), so a failing test shows a bug. Tests run in a sandbox for **Python, JavaScript, Go, Ruby and Rust**. Each failure is then triaged as *code defect*, *wrong test expectation* or *sandbox limitation*. |
| **Repository analysis** | Analyze any repository the signed-in GitHub account can read, including private and organization repositories, pinned to the current commit of a branch. Source files in every language are selected; vendor, build output, generated and minified files are skipped. |
| **API & data contracts** | Lint OpenAPI 3.x / Swagger 2.0, validate GraphQL SDL, and validate JSON/YAML against a JSON Schema. With a base URL, check the live server: status codes must be documented, JSON bodies must match the schema, required parameters must be enforced, and GraphQL introspection is compared with the SDL. Gemini also writes a pytest suite per operation, which runs against the live server. |
| **History & dashboards** | The Overview shows each project's latest result and a findings trend. History lists every run, filterable by project, type and scope. Team dashboards combine live GitHub organization data (repositories, members) with runs made by organization members. |

Nothing is simulated. If the AI step fails, for example because of a quota limit, the report says so
and shows "—" instead of zero. Tests for languages without a sandbox runner are marked
"not executed" and never counted as passed.

---

## How a run works

```
Sign in with GitHub ─► submit code / repository / spec ─► run is queued (stored in the DB)
                                                             │
        ┌────────────────────────────────────────────────────┘
        ▼
  1. Detect language (extension, shebang; unknown extensions are identified by the model)
  2. Static checks ...... real compilers / linters, line + column diagnostics
  3. AI review .......... gemini-3.1-pro-preview: findings (severity, category, lines, fix) + a test file
  4. Execute tests ...... sandboxed runner, per-test pass/fail + failure output + coverage
  5. Triage failures .... gemini-3.1-pro-preview: code defect vs wrong expectation, with the source line
  6. Store report ....... summary + full report in the runs table ─► History / dashboards
```

The browser polls `GET /api/runs/{id}` until the run completes. Runs execute on a background
thread pool, so large repositories do not hold an HTTP request open.

---

## Language support

Every language can be reviewed by the model. Deterministic checks and test execution depend on the
toolchains installed on the server; the Docker image installs all of the tools below.

| Language | Static checks | Tests executed in sandbox |
|---|---|---|
| Python | `ast` compile + pylint (errors & warnings) + AST facts | pytest + coverage |
| JavaScript (CJS/ESM) | `node --check` | `node --test` + built-in coverage |
| TypeScript | `tsc --noEmit --strict` | generated (Vitest), run locally |
| Go | `gofmt -e`, `go vet` | `go test -json -cover` |
| Rust | `rustc` (JSON diagnostics) | `rustc --test` |
| Ruby | `ruby -wc` | Minitest |
| C / C++ | `gcc` / `g++ -fsyntax-only -Wall -Wextra` | generated, run locally |
| Java | `javac -Xlint:all` | generated (JUnit 5), run locally |
| PHP | `php -l` | generated (PHPUnit), run locally |
| Shell | `bash -n`, shellcheck | generated (bats), run locally |
| Kotlin, C#, Swift, Scala, Dart, SQL, R, Lua, Perl, Haskell, Elixir, Solidity, Terraform, … | AI review only | generated in the conventional framework, run locally |

When a toolchain is missing, the report says "unavailable" for that tool rather than silently
skipping it. Diagnostics caused by missing third-party dependencies are downgraded to *info* with
the note "dependency not available in the analysis sandbox".

## API & spec checks

**OpenAPI / Swagger lint:** unsupported version; missing `info.title` / `info.version`; empty
`paths`; unresolved local `$ref`; path template variables without a matching `in: path` parameter
(and the reverse); path parameters not marked `required`; duplicate parameters and
`operationId`s; missing `responses`; no 2xx/3xx response; invalid status codes; responses without a
description; `requestBody` without content or schema; bodies on GET/HEAD/DELETE; undefined security
schemes; `required` properties that are not defined; missing `servers`. Every issue carries a JSON
pointer and, where possible, a line number.

**GraphQL:** syntax errors and SDL/schema validation from `graphql-core` (unknown types, invalid
root types and similar), with line and column.

**JSON / YAML:** syntax errors with line and column, duplicate keys (which most parsers silently
drop), JSON Schema validation (Draft 4 to 2020-12), and meta-validation when the document is itself a
JSON Schema.

**Live contract checks** (optional base URL): each operation is called with values taken from
examples, defaults, enums or the schema. The checker verifies the returned status code is documented,
validates the JSON response against the documented schema (with OpenAPI 3.0 `nullable` handled), and
confirms that omitting required query parameters returns a 4xx. Write methods
(POST/PUT/PATCH/DELETE) are sent only when you explicitly opt in. For GraphQL, the server's
introspection result is compared with the SDL type by type and field by field.

---

## Project layout

```
backend/
  main.py            FastAPI app: pages, /auth, /api routes, security headers
  config.py          settings from environment / backend/.env
  auth.py            GitHub OAuth, server-side sessions, encrypted GitHub tokens
  db.py              SQLAlchemy Core schema + queries (Supabase Postgres or SQLite)
  jobs.py            background run execution + progress
  gemini_client.py   Gemini REST client: strict model, retries, quota circuit breaker
  languages.py       language detection + test framework per language
  static_checks.py   compiler / linter diagnostics
  sandbox.py         isolated subprocess execution (scrubbed env, rlimits, privilege drop)
  executors.py       run generated tests and parse results (pytest, node, go, ruby, rust)
  code_review.py     per-file pipeline: static → AI review → tests → triage
  repo_analyzer.py   repository file selection + parallel analysis
  github_api.py      GitHub REST client (repos, orgs, members, trees, blobs)
  spec_parser.py     OpenAPI / GraphQL operation extraction
  spec_checks.py     lint, validation, live contract checks, test generation for specs
  spec_llm.py        prompts for API test generation
  parser.py          Python AST fact extraction
  dashboards.py      overview + team aggregations
  test_*.py          pytest suite (network mocked; language runners are real)
frontend/
  index.html         sign-in page
  app.html           application shell
  assets/            CSS, ES-module views, icons (no build step)
supabase/migrations/ database schema with Row Level Security
examples/            sample specs and deliberately buggy files for demos
Dockerfile, render.yaml
```

---

## Configuration

All settings are environment variables. For local development, copy `backend/.env.example` to
`backend/.env`. That file is gitignored; never commit real keys.

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | yes (for AI) | Google AI Studio key. Used only by the server, sent in the `x-goog-api-key` header. |
| `GEMINI_MODEL` | no | Defaults to `gemini-3.1-pro-preview`. No other model is used as a fallback. |
| `GEMINI_MAX_RETRIES` | no | Retries on 429/5xx with exponential backoff (default 4). |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | yes | GitHub OAuth App credentials. |
| `SESSION_SECRET` | production | Random string; encrypts stored GitHub tokens. Auto-generated for local dev. |
| `DATABASE_URL` | production | Supabase Postgres connection string. Empty = SQLite in `backend/data/`. |
| `APP_BASE_URL` | yes | Public URL, e.g. `https://aitestge.stream`. Builds the OAuth callback URL. |
| `APP_ENV` | no | `production` enables HTTPS / `www` redirects, HSTS and secure cookies. |
| `ALLOWED_HOSTS` | no | Comma-separated Host allowlist (TrustedHostMiddleware). |
| `ALLOW_PRIVATE_TARGETS` | no | Allow live checks and spec URLs on localhost/private IPs. Default `true` in dev, `false` in production. |
| `MAX_REPO_FILES` | no | Upper bound for files per repository run (default 25, max 100). |
| `JOB_WORKERS` | no | Concurrent runs (default 3). |

### Gemini quota

`gemini-3.1-pro-preview` is a paid model: **the Gemini API free tier gives it no quota (limit 0)**,
so billing must be enabled for the Google AI Studio project that owns `GEMINI_API_KEY`. Without
billing every AI call fails with a quota error, and the report says so. One file uses one request, or
two when its tests fail and need triage. Pro models "think" before answering; those thinking tokens
are billed as output tokens.

When the key has no quota for the model, or its daily quota is exhausted, the client stops
immediately (no pointless retries) and pauses AI calls for 15 minutes. Compiler/linter results and live contract checks still complete, and
reports clearly mark the AI step as not run.

---

## Local development

Prerequisites: Python 3.11+. Node.js, Go, Ruby, rustc, a JDK, gcc and PHP are optional; each one
you install enables its checks and runners.

```bash
git clone https://github.com/TanishMhalsekar01/AI-test-generator.git
cd AI-test-generator/backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # then fill in the values
python -m uvicorn main:app --reload
```

Open http://localhost:8000 and sign in with GitHub. For local sign-in, create a separate OAuth App
whose callback URL is `http://localhost:8000/auth/github/callback`.

### Tests

```bash
cd backend
python -m pytest -q
```

183 tests cover auth and sessions, the Gemini client (retries, quota handling, key redaction),
compiler and linter parsing, the real sandbox runners for every supported language, the review
pipeline, repository selection, spec linting, live contract checks (HTTP mocked with `responses`),
and history/team access control. Gemini and GitHub are always mocked, and the test suite never uses
the key in `backend/.env`. CI runs the suite on every pull request
([workflow](.github/workflows/pytest.yml)).

---

## GitHub OAuth App

1. GitHub → **Settings → Developer settings → OAuth Apps → New OAuth App**.
2. Homepage URL: `https://aitestge.stream`
3. Authorization callback URL: `https://aitestge.stream/auth/github/callback`
4. Copy the **Client ID**, generate a **Client secret**, and set both as environment variables.

The app requests `read:user`, `read:org` and `repo`. Those scopes are needed to list organizations
and to analyze private repositories. If an organization restricts third-party OAuth apps, an
organization owner must approve the app before its repositories and members appear on the team
dashboard.

## Supabase (database)

1. Create a Supabase project.
2. Apply the schema: open the **SQL editor** and run
   [`supabase/migrations/20260928000000_init.sql`](supabase/migrations/20260928000000_init.sql),
   or run `supabase db push` with the Supabase CLI.
3. **Project Settings → Database → Connection string → Session pooler**. Copy the URI, fill in the
   database password, and set it as `DATABASE_URL`. The session pooler works over IPv4, which Render
   requires.

Row Level Security is enabled on all tables with no policies. The public Supabase API
(anon/authenticated keys) therefore cannot read sessions or reports; only the backend's direct
database connection can.

## Deployment on Render with the custom domain

1. Push this repository to GitHub. In Render: **New → Blueprint** → select the repository.
   [`render.yaml`](render.yaml) creates the Docker web service `ai-test-generator` with a health
   check at `/healthz` and the domains `aitestge.stream` and `www.aitestge.stream`.
2. Enter the secret environment variables in the Render dashboard: `GEMINI_API_KEY`,
   `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `DATABASE_URL`. `SESSION_SECRET` is generated
   automatically.
3. **Register the domain** `aitestge.stream` with any registrar if you have not already.
4. **DNS**: in Render, open the service → **Settings → Custom Domains** and create the records it
   shows at your DNS provider. At the time of writing these are:

   | Host | Type | Value |
   |---|---|---|
   | `aitestge.stream` (apex) | `A` (or `ALIAS`/`ANAME` → `ai-test-generator.onrender.com`) | `216.24.57.1` |
   | `www` | `CNAME` | `ai-test-generator.onrender.com` |

   Remove any conflicting `AAAA` records for the apex. Render issues TLS certificates automatically
   once DNS resolves. The app forces HTTPS in production (HSTS).
5. Update the GitHub OAuth App's callback URL to `https://aitestge.stream/auth/github/callback`.

Until DNS is live, set `APP_BASE_URL` to the `https://ai-test-generator.onrender.com` URL and use it
in the OAuth App. In production, `www.aitestge.stream` redirects to `aitestge.stream` and HTTP redirects
to HTTPS.

---

## Security model

- **Secrets stay on the server.** The Gemini key is read from the environment, sent only in a request
  header, redacted from any error text, and never included in API responses. `backend/.env` is
  gitignored.
- **Sessions.** The browser holds only a random session id in an HttpOnly, SameSite=Lax cookie
  (Secure in production). The database stores a SHA-256 of that id. The GitHub access token is
  encrypted with Fernet using a key derived from `SESSION_SECRET`.
- **Access control.** Every `/api/*` route requires a session. Users see their own runs, plus runs on
  repositories owned by GitHub organizations they belong to (membership is checked live against
  GitHub).
- **Sandbox.** Submitted code and generated tests run in a temporary directory with a scrubbed
  environment (no API keys, OAuth secrets or database URL), CPU, file-size and core-dump limits, and
  a timeout that kills the whole process group. In the Docker image, each process also drops to the
  unprivileged `sandbox` user, so it cannot read the server's environment or files. Network access
  from the sandbox is not blocked, because live API tests need it. For hostile multi-tenant use, run
  the service in its own isolated container or VM.
- **SSRF guard.** In production, spec URLs and live-check base URLs that resolve to private,
  loopback, link-local or reserved addresses are rejected.
- **Browser hardening.** Strict Content-Security-Policy (no inline scripts or styles, no third-party
  origins except GitHub avatars), `X-Frame-Options: DENY`, `nosniff`, HSTS in production. All
  user-controlled and model-generated text is rendered with `textContent`, never as HTML.

---

## HTTP API

All endpoints except sign-in and `/healthz` require the session cookie. Interactive docs are at
`/api/docs`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/auth/github/login` | Start GitHub sign-in |
| GET | `/auth/github/callback` | OAuth callback |
| POST | `/auth/logout` | End the session |
| GET | `/api/me` | Signed-in user, model name, available runners |
| POST | `/api/runs/code` | Multipart: `files[]` or `code` + `filename`; optional `project`, `run_tests` |
| POST | `/api/runs/repo` | JSON: `{ "repo": "owner/name" \| URL, "branch"?: str, "max_files"?: int }` |
| POST | `/api/runs/spec` | Multipart: `file` \| `spec_text` \| `spec_url`; optional `schema_file`, `base_url`, `allow_mutations`, `generate_tests` |
| GET | `/api/runs` | History: `scope=mine\|all\|team:<org>`, `kind`, `project`, `limit`, `offset` |
| GET / DELETE | `/api/runs/{id}` | Run with full report / delete your own run |
| GET | `/api/github/repos`, `/api/github/orgs` | Live GitHub data for the signed-in user |
| GET | `/api/dashboard/overview` | Per-project latest results and trends |
| GET | `/api/dashboard/team/{org}` | Organization dashboard (members only) |

The `POST /api/runs/*` endpoints return `202 {"id": ...}`; poll `GET /api/runs/{id}` until
`status` is `completed` or `failed`.

---

## Claude Code tooling

This repository ships project-level Claude Code configuration:

- **Plugins** (declared in [`.claude/settings.json`](.claude/settings.json), from
  `anthropics/claude-plugins-official`): **Supabase** (database/auth management via MCP),
  **Playwright** (browser automation MCP), **Context7** (up-to-date library documentation), and
  **frontend-design** (UI implementation skill).
- **Playwright CLI** skill in `.claude/skills/playwright-cli/` (from `@playwright/cli`), for driving a
  browser from the terminal.
- **Strix** security-testing skill in `.claude/skills/strix-scan/`, with instructions for running
  [Strix](https://github.com/usestrix/strix) against a local instance. Strix needs Docker and a
  billed LLM key.
- [`scripts/setup-dev-tools.sh`](scripts/setup-dev-tools.sh) installs all of the above on a
  developer machine.

---

## Limitations

- AI findings are model output: they are specific and line-referenced, but review them before
  acting. Failing tests plus triage are the strongest evidence a defect is real.
- Tests execute only for Python, JavaScript, Go, Ruby and Rust. Other languages get generated tests
  to run locally.
- Generated tests run against the single file under test. Code that depends on other project files
  or third-party packages may need those dependencies, and such failures are triaged as
  *sandbox limitation*.
- Repository runs analyze up to `MAX_REPO_FILES` files per run (application code before tests).
- `gemini-3.1-pro-preview` needs a Google AI project with billing enabled; it has no free-tier quota.
