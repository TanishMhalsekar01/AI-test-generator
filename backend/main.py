"""
AI Test Generator — FastAPI application.

Pages
  GET  /                 sign-in page (redirects to /app when signed in)
  GET  /app              application (redirects to / when signed out)
Auth
  GET  /auth/github/login, /auth/github/callback;  POST /auth/logout
API (all require a GitHub session)
  GET  /api/me
  POST /api/runs/code    review uploaded / pasted source files (any language)
  POST /api/runs/repo    review a GitHub repository at a pinned commit
  POST /api/runs/spec    check OpenAPI / GraphQL / JSON documents
  GET  /api/runs, GET/DELETE /api/runs/{id}
  GET  /api/github/repos, /api/github/orgs
  GET  /api/dashboard/overview, /api/dashboard/team/{org}
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import requests as _http_requests
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

import auth
import code_review
import dashboards
import db
import executors
import jobs
import repo_analyzer
import spec_checks
from auth import CurrentUser, current_user, optional_user
from config import FRONTEND_DIR, get_settings
from github_api import GitHubAuthError, GitHubClient, GitHubError, parse_repo_ref, user_org_logins

MAX_UPLOAD_FILES = 10
MAX_FILE_BYTES = 300_000
MAX_SPEC_BYTES = 5_000_000



@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    db.mark_interrupted_runs()
    db.purge_expired_sessions()
    yield


settings = get_settings()
app = FastAPI(title="AI Test Generator", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json",
              lifespan=lifespan)
if settings.allowed_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

_CSP = (
    "default-src 'self'; img-src 'self' data: https://avatars.githubusercontent.com; "
    "style-src 'self'; script-src 'self'; connect-src 'self'; font-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


@app.middleware("http")
async def edge_policies(request: Request, call_next):
    s = get_settings()
    path = request.url.path
    if s.is_production and path != "/healthz":
        host = request.headers.get("host", "")
        query = f"?{request.url.query}" if request.url.query else ""
        # www.<domain> -> <domain>. Other hosts (e.g. *.onrender.com) keep working,
        # so the service stays reachable before the custom domain's DNS is live.
        if s.canonical_host and host.split(":")[0] == f"www.{s.canonical_host}":
            return RedirectResponse(f"https://{s.canonical_host}{path}{query}", status_code=301)
        if request.url.scheme != "https":
            return RedirectResponse(f"https://{host or s.canonical_host}{path}{query}", status_code=301)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if not path.startswith("/api/docs"):
        response.headers.setdefault("Content-Security-Policy", _CSP)
    if s.is_production:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


app.include_router(auth.router)
app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")


@app.exception_handler(GitHubError)
async def _github_error(request: Request, exc: GitHubError):
    response = JSONResponse(status_code=exc.status, content={"detail": str(exc)})
    if isinstance(exc, GitHubAuthError):
        # The stored GitHub token was revoked or expired: end the session so the
        # browser returns to sign-in instead of bouncing between / and /app.
        raw = request.cookies.get(auth.SESSION_COOKIE)
        if raw:
            db.delete_session(auth.hash_session_id(raw))
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index(request: Request):
    if optional_user(request):
        return RedirectResponse("/app", status_code=303)
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/app", include_in_schema=False)
def application(request: Request):
    if not optional_user(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(FRONTEND_DIR / "app.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(FRONTEND_DIR / "assets" / "favicon.svg", media_type="image/svg+xml")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Session info
# ---------------------------------------------------------------------------

@app.get("/api/me")
def me(user: CurrentUser = Depends(current_user)):
    s = get_settings()
    return {
        "login": user.login,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "model": s.gemini_model,
        "fallback_models": list(s.gemini_fallback_models),
        "ai_configured": s.ai_configured,
        "runners": executors.available_runners(),
        "limits": {"upload_files": MAX_UPLOAD_FILES, "file_bytes": MAX_FILE_BYTES, "repo_files": s.max_repo_files},
    }


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def _decode(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{name} is not UTF-8 text.") from exc


@app.post("/api/runs/code", status_code=202)
async def create_code_run(
    user: CurrentUser = Depends(current_user),
    files: list[UploadFile] = File(default=[]),
    code: str = Form(default=""),
    filename: str = Form(default=""),
    project: str = Form(default=""),
    run_tests: bool = Form(default=True),
):
    sources: list[tuple[str, str]] = []
    for upload in files:
        if not upload.filename:
            continue
        data = await upload.read()
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail=f"{upload.filename} is larger than {MAX_FILE_BYTES // 1000} KB.")
        sources.append((Path(upload.filename).name, _decode(data, upload.filename)))
    if code.strip():
        if len(code.encode()) > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail=f"Pasted code is larger than {MAX_FILE_BYTES // 1000} KB.")
        sources.append((Path(filename.strip() or "snippet.txt").name, code))
    if not sources:
        raise HTTPException(status_code=400, detail="Upload at least one source file or paste code.")
    if len(sources) > MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail=f"At most {MAX_UPLOAD_FILES} files per run.")

    s = get_settings()
    run_id = db.create_run(user_id=user.id, user_login=user.login, kind="code",
                           project=project.strip() or sources[0][0], model=s.gemini_model)

    def job(progress):
        reports = []
        for i, (name, text) in enumerate(sources):
            base = 5 + int(90 * i / len(sources))
            reports.append(code_review.review_file(name, text, run_tests=run_tests,
                                                   progress=lambda msg, b=base: progress(b, msg)))
        summary = code_review.summarize(reports)
        return {"files": reports}, summary, {"languages": ",".join(repo_analyzer.languages_of(reports)),
                                             **_answered_by(summary)}

    jobs.submit(run_id, job)
    return {"id": run_id}


def _answered_by(summary: dict) -> dict:
    """Record the model(s) that actually answered (a fallback may have been used)."""
    models = summary.get("models") or []
    return {"model": ", ".join(models)[:100]} if models else {}


class RepoRunRequest(BaseModel):
    repo: str
    branch: Optional[str] = None
    max_files: Optional[int] = None


@app.post("/api/runs/repo", status_code=202)
def create_repo_run(body: RepoRunRequest, user: CurrentUser = Depends(current_user)):
    s = get_settings()
    try:
        owner, name, url_branch = parse_repo_ref(body.repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    max_files = min(20, s.max_repo_files) if body.max_files is None else body.max_files
    if not 1 <= max_files <= s.max_repo_files:
        raise HTTPException(status_code=400, detail=f"max_files must be between 1 and {s.max_repo_files}.")

    client = GitHubClient(user.github_token)
    info = client.repo(owner, name)  # 404 if the user cannot see it
    branch = (body.branch or url_branch or info["default_branch"]).strip()
    sha = client.commit_sha(info["owner"]["login"], info["name"], branch)
    full_name = info["full_name"]
    run_id = db.create_run(user_id=user.id, user_login=user.login, kind="repo", project=full_name,
                           owner=info["owner"]["login"], ref=branch, commit_sha=sha, model=s.gemini_model)

    def job(progress):
        report, summary = repo_analyzer.analyze_repository(
            client, info["owner"]["login"], info["name"], sha, max_files, progress)
        report["branch"] = branch
        report["html_url"] = info.get("html_url")
        return report, summary, {"languages": ",".join(repo_analyzer.languages_of(report["files"])),
                                 **_answered_by(summary)}

    jobs.submit(run_id, job)
    return {"id": run_id}


@app.post("/api/runs/spec", status_code=202)
async def create_spec_run(
    user: CurrentUser = Depends(current_user),
    file: Optional[UploadFile] = File(default=None),
    spec_text: str = Form(default=""),
    spec_url: str = Form(default=""),
    schema_file: Optional[UploadFile] = File(default=None),
    base_url: str = Form(default=""),
    allow_mutations: bool = Form(default=False),
    generate_tests: bool = Form(default=True),
    project: str = Form(default=""),
):
    filename = ""
    if file is not None and file.filename:
        data = await file.read()
        if len(data) > MAX_SPEC_BYTES:
            raise HTTPException(status_code=413, detail="Spec file is larger than 5 MB.")
        raw, filename = _decode(data, file.filename), Path(file.filename).name
    elif spec_text.strip():
        raw, filename = spec_text, "pasted"
    elif spec_url.strip():
        try:
            url = await run_in_threadpool(spec_checks.guard_url, spec_url)
            resp = await run_in_threadpool(_http_requests.get, url, timeout=15, allow_redirects=False)
            resp.raise_for_status()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except _http_requests.RequestException as exc:
            raise HTTPException(status_code=400, detail=f"Could not fetch the spec URL ({type(exc).__name__}).") from exc
        raw, filename = resp.text, Path(url.split("?")[0]).name or "spec"
    else:
        raise HTTPException(status_code=400, detail="Provide a spec file, pasted text, or a spec URL.")

    schema_raw = None
    if schema_file is not None and schema_file.filename:
        schema_raw = _decode(await schema_file.read(), schema_file.filename)

    base = base_url.strip()
    if base:
        try:
            base = await run_in_threadpool(spec_checks.guard_url, base)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Fail fast on unrecognised documents instead of creating a run that can only fail.
    try:
        kind, _, _ = spec_checks.detect_kind(raw, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    s = get_settings()
    run_id = db.create_run(user_id=user.id, user_login=user.login, kind="spec",
                           project=project.strip() or filename or kind, model=s.gemini_model)

    def job(progress):
        report, summary = spec_checks.analyze_spec(
            raw, filename, schema_raw=schema_raw, base_url=base, allow_mutations=allow_mutations,
            generate_tests=generate_tests, progress=progress)
        return report, summary, {"languages": report["kind"], **_answered_by(summary)}

    jobs.submit(run_id, job)
    return {"id": run_id}


def _visible_owners(user: CurrentUser) -> list[str]:
    try:
        return user_org_logins(user.github_token)
    except GitHubError:
        return []


@app.get("/api/runs")
def list_runs(
    user: CurrentUser = Depends(current_user),
    scope: str = Query(default="mine"),
    kind: Optional[str] = None,
    project: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    if scope == "mine":
        rows = db.list_runs(user_id=user.id, kind=kind, project=project, limit=limit, offset=offset)
    elif scope.startswith("team:"):
        org = scope.split(":", 1)[1]
        if org not in _visible_owners(user):
            raise HTTPException(status_code=403, detail=f"You are not a member of {org}.")
        rows = db.list_runs(owners=[org], kind=kind, project=project, limit=limit, offset=offset)
    elif scope == "all":
        rows = db.list_runs(user_id=user.id, owners=_visible_owners(user), kind=kind, project=project,
                            limit=limit, offset=offset)
    else:
        raise HTTPException(status_code=400, detail="scope must be mine, all, or team:<org>.")
    return {"runs": [{**db.serialize_run(r), "headline": dashboards.headline(r["kind"], r.get("summary"))}
                     for r in rows]}


def _load_visible_run(run_id: str, user: CurrentUser) -> dict:
    row = db.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if row["user_id"] != user.id and not (row.get("owner") and row["owner"] in _visible_owners(user)):
        raise HTTPException(status_code=404, detail="Run not found.")
    return row


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, user: CurrentUser = Depends(current_user)):
    row = _load_visible_run(run_id, user)
    include = row["status"] in {"completed", "failed"}
    return {**db.serialize_run(row, include_report=include), "is_mine": row["user_id"] == user.id}


@app.delete("/api/runs/{run_id}", status_code=204)
def delete_run(run_id: str, user: CurrentUser = Depends(current_user)):
    if not db.delete_run(run_id, user.id):
        raise HTTPException(status_code=404, detail="Run not found.")


# ---------------------------------------------------------------------------
# GitHub data + dashboards
# ---------------------------------------------------------------------------

@app.get("/api/github/repos")
def github_repos(user: CurrentUser = Depends(current_user)):
    return {"repos": GitHubClient(user.github_token).repos()}


@app.get("/api/github/orgs")
def github_orgs(user: CurrentUser = Depends(current_user)):
    return {"orgs": GitHubClient(user.github_token).orgs()}


@app.get("/api/dashboard/overview")
def dashboard_overview(user: CurrentUser = Depends(current_user)):
    return dashboards.overview(user.id)


@app.get("/api/dashboard/team/{org}")
def dashboard_team(org: str, user: CurrentUser = Depends(current_user)):
    if org not in _visible_owners(user):
        raise HTTPException(status_code=403, detail=f"You are not a member of {org}, or the app has not been "
                                                    "granted access to it on GitHub.")
    return dashboards.team(GitHubClient(user.github_token), org)
