"""
Persistence layer (SQLAlchemy Core).

Production uses the Supabase Postgres database (DATABASE_URL = the Supabase
connection string). Local development falls back to SQLite under backend/data/.
The schema mirrors supabase/migrations/*_init.sql.
"""

import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    delete,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

from config import get_settings

metadata = MetaData()
JSONType = JSON().with_variant(JSONB(), "postgresql")

users = Table(
    "users",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=False),  # GitHub user id
    Column("login", String(100), nullable=False),
    Column("name", String(255)),
    Column("avatar_url", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_login_at", DateTime(timezone=True), nullable=False),
)

sessions = Table(
    "sessions",
    metadata,
    Column("id_hash", String(64), primary_key=True),  # sha256 of the cookie token
    Column("user_id", BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("github_token", Text, nullable=False),  # Fernet-encrypted
    Column("scopes", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

runs = Table(
    "runs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("user_login", String(100), nullable=False),
    Column("kind", String(16), nullable=False),  # code | repo | spec
    Column("status", String(16), nullable=False),  # queued | running | completed | failed
    Column("project", String(300), nullable=False),
    Column("owner", String(100)),  # GitHub owner (user or org) for repository runs
    Column("ref", String(255)),
    Column("commit_sha", String(64)),
    Column("languages", Text),
    Column("model", String(100)),
    Column("progress", Integer, nullable=False, default=0),
    Column("progress_note", Text),
    Column("summary", JSONType),
    Column("report", JSONType),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("duration_ms", Integer),
)

Index("ix_sessions_user_id", sessions.c.user_id)
Index("ix_runs_user_created", runs.c.user_id, runs.c.created_at)
Index("ix_runs_owner_created", runs.c.owner, runs.c.created_at)
Index("ix_runs_project", runs.c.project)

_engine: Optional[Engine] = None
_engine_url: Optional[str] = None
_lock = threading.Lock()


def _normalise_url(url: str) -> str:
    # Supabase hands out postgres:// or postgresql:// URLs; use the psycopg 3 driver.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def get_engine() -> Engine:
    global _engine, _engine_url
    url = _normalise_url(get_settings().database_url)
    with _lock:
        if _engine is None or _engine_url != url:
            kwargs: dict[str, Any] = {"pool_pre_ping": True}
            if url.startswith("sqlite"):
                kwargs["connect_args"] = {"check_same_thread": False}
            _engine = create_engine(url, **kwargs)
            _engine_url = url
            metadata.create_all(_engine)
        return _engine


def init_db() -> None:
    get_engine()


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite drops tzinfo; values are always stored as UTC
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Users and sessions
# ---------------------------------------------------------------------------

def upsert_user(gh_user: dict) -> dict:
    ts = now()
    values = {
        "login": gh_user["login"],
        "name": gh_user.get("name"),
        "avatar_url": gh_user.get("avatar_url"),
        "last_login_at": ts,
    }
    with get_engine().begin() as conn:
        exists = conn.execute(select(users.c.id).where(users.c.id == gh_user["id"])).first()
        if exists:
            conn.execute(update(users).where(users.c.id == gh_user["id"]).values(**values))
        else:
            conn.execute(insert(users).values(id=gh_user["id"], created_at=ts, **values))
        row = conn.execute(select(users).where(users.c.id == gh_user["id"])).mappings().one()
    return dict(row)


def create_session(id_hash: str, user_id: int, encrypted_token: str, scopes: str, ttl_days: int = 14) -> None:
    ts = now()
    with get_engine().begin() as conn:
        conn.execute(insert(sessions).values(
            id_hash=id_hash, user_id=user_id, github_token=encrypted_token, scopes=scopes,
            created_at=ts, expires_at=ts + timedelta(days=ttl_days),
        ))


def get_session_user(id_hash: str) -> Optional[dict]:
    with get_engine().connect() as conn:
        row = conn.execute(
            select(
                sessions.c.github_token, sessions.c.expires_at, sessions.c.scopes,
                users.c.id, users.c.login, users.c.name, users.c.avatar_url,
            ).join(users, users.c.id == sessions.c.user_id).where(sessions.c.id_hash == id_hash)
        ).mappings().first()
    if row is None:
        return None
    expires = row["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now():
        delete_session(id_hash)
        return None
    return dict(row)


def delete_session(id_hash: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(delete(sessions).where(sessions.c.id_hash == id_hash))


def purge_expired_sessions() -> None:
    with get_engine().begin() as conn:
        conn.execute(delete(sessions).where(sessions.c.expires_at < now()))


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def create_run(*, user_id: int, user_login: str, kind: str, project: str, owner: Optional[str] = None,
               ref: Optional[str] = None, commit_sha: Optional[str] = None, model: Optional[str] = None) -> str:
    run_id = str(uuid.uuid4())
    with get_engine().begin() as conn:
        conn.execute(insert(runs).values(
            id=run_id, user_id=user_id, user_login=user_login, kind=kind, status="queued",
            project=project[:300], owner=owner, ref=ref, commit_sha=commit_sha, model=model,
            progress=0, created_at=now(),
        ))
    return run_id


def update_run(run_id: str, **values: Any) -> None:
    with get_engine().begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id).values(**values))


def get_run(run_id: str) -> Optional[dict]:
    with get_engine().connect() as conn:
        row = conn.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
    return dict(row) if row else None


def delete_run(run_id: str, user_id: int) -> bool:
    with get_engine().begin() as conn:
        result = conn.execute(delete(runs).where(and_(runs.c.id == run_id, runs.c.user_id == user_id)))
    return result.rowcount > 0


_LIST_COLUMNS = [c for c in runs.c if c.name != "report"]


def list_runs(*, user_id: Optional[int] = None, owners: Optional[list[str]] = None, kind: Optional[str] = None,
              project: Optional[str] = None, limit: int = 50, offset: int = 0) -> list[dict]:
    """List runs without the (large) report column.

    user_id and owners are OR-ed: a user's own runs plus runs on repositories
    owned by any of *owners* (used for team visibility).
    """
    stmt = select(*_LIST_COLUMNS)
    scope = []
    if user_id is not None:
        scope.append(runs.c.user_id == user_id)
    if owners:
        scope.append(runs.c.owner.in_(owners))
    if scope:
        stmt = stmt.where(or_(*scope))
    if kind:
        stmt = stmt.where(runs.c.kind == kind)
    if project:
        stmt = stmt.where(runs.c.project == project)
    stmt = stmt.order_by(runs.c.created_at.desc()).limit(limit).offset(offset)
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]


def count_runs(*, user_id: Optional[int] = None, owners: Optional[list[str]] = None,
               since: Optional[datetime] = None) -> int:
    stmt = select(func.count()).select_from(runs)
    scope = []
    if user_id is not None:
        scope.append(runs.c.user_id == user_id)
    if owners:
        scope.append(runs.c.owner.in_(owners))
    if scope:
        stmt = stmt.where(or_(*scope))
    if since is not None:
        stmt = stmt.where(runs.c.created_at >= since)
    with get_engine().connect() as conn:
        return int(conn.execute(stmt).scalar_one())


def mark_interrupted_runs() -> int:
    """Runs left queued/running by a previous process can never finish; fail them."""
    with get_engine().begin() as conn:
        result = conn.execute(
            update(runs)
            .where(runs.c.status.in_(["queued", "running"]))
            .values(status="failed", error="The server restarted before this run finished.", finished_at=now())
        )
    return result.rowcount


def serialize_run(row: dict, include_report: bool = False) -> dict:
    out = {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "project": row["project"],
        "owner": row.get("owner"),
        "ref": row.get("ref"),
        "commit_sha": row.get("commit_sha"),
        "languages": [x for x in (row.get("languages") or "").split(",") if x],
        "model": row.get("model"),
        "user_login": row["user_login"],
        "progress": row.get("progress") or 0,
        "progress_note": row.get("progress_note"),
        "summary": row.get("summary") or {},
        "error": row.get("error"),
        "created_at": iso(row.get("created_at")),
        "started_at": iso(row.get("started_at")),
        "finished_at": iso(row.get("finished_at")),
        "duration_ms": row.get("duration_ms"),
    }
    if include_report:
        out["report"] = row.get("report")
    return out
