"""
Runtime configuration.

Every value comes from the process environment. For local development a
``backend/.env`` file is loaded first (it is gitignored — never commit it).
Settings are re-read on each ``get_settings()`` call so tests can change the
environment with ``monkeypatch``.
"""

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent
FRONTEND_DIR = REPO_ROOT / "frontend"
DATA_DIR = BACKEND_DIR / "data"

DEFAULT_GEMINI_MODEL = "gemini-2.5-pro"
# Tried in order when the main model cannot answer (not available to the key, out of
# quota, or overloaded). GEMINI_FALLBACK_MODELS overrides; "none" disables fallback.
DEFAULT_GEMINI_FALLBACK_MODELS = ("gemini-3.6-flash", "gemini-3-flash-preview", "gemini-3.1-flash-lite")


def load_dotenv(path: Path = BACKEND_DIR / ".env") -> None:
    """Load KEY=VALUE lines into os.environ without overriding real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


_DEV_SECRET: dict = {}


def _dev_session_secret() -> str:
    """Stable per-checkout secret for local development (stored under data/)."""
    if "value" in _DEV_SECRET:
        return _DEV_SECRET["value"]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / ".session_secret"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
    else:
        value = secrets.token_urlsafe(48)
        path.write_text(value, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    _DEV_SECRET["value"] = value
    return value


@dataclass(frozen=True)
class Settings:
    app_env: str
    app_base_url: str
    allowed_hosts: tuple[str, ...]
    canonical_host: str
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_models: tuple[str, ...]
    gemini_max_retries: int
    github_client_id: str
    github_client_secret: str
    session_secret: str
    database_url: str
    allow_private_targets: bool
    max_repo_files: int
    job_workers: int

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def secure_cookies(self) -> bool:
        return self.app_base_url.startswith("https://")

    @property
    def oauth_configured(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)

    @property
    def ai_configured(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def gemini_models(self) -> tuple[str, ...]:
        """GEMINI_MODEL followed by the fallback models, without duplicates."""
        return tuple(dict.fromkeys((self.gemini_model, *self.gemini_fallback_models)))


def _model_list(raw: Optional[str]) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_GEMINI_FALLBACK_MODELS
    if raw.strip().lower() in ("", "none", "off"):
        return ()
    return tuple(m.strip() for m in raw.split(",") if m.strip())


def get_settings() -> Settings:
    env = os.environ.get("APP_ENV", "development").strip().lower() or "development"
    base_url = os.environ.get("APP_BASE_URL", "http://localhost:8000").strip().rstrip("/")

    hosts_raw = os.environ.get("ALLOWED_HOSTS", "").strip()
    allowed_hosts = tuple(h.strip() for h in hosts_raw.split(",") if h.strip())

    session_secret = os.environ.get("SESSION_SECRET", "").strip()
    if not session_secret:
        if env == "production":
            raise RuntimeError("SESSION_SECRET must be set when APP_ENV=production.")
        session_secret = _dev_session_secret()

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        database_url = f"sqlite:///{DATA_DIR / 'app.db'}"

    canonical = base_url.split("://", 1)[-1].split("/", 1)[0]

    return Settings(
        app_env=env,
        app_base_url=base_url,
        allowed_hosts=allowed_hosts,
        canonical_host=canonical,
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        gemini_model=os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL,
        gemini_fallback_models=_model_list(os.environ.get("GEMINI_FALLBACK_MODELS")),
        gemini_max_retries=max(0, int(os.environ.get("GEMINI_MAX_RETRIES", "4"))),
        github_client_id=os.environ.get("GITHUB_CLIENT_ID", "").strip(),
        github_client_secret=os.environ.get("GITHUB_CLIENT_SECRET", "").strip(),
        session_secret=session_secret,
        database_url=database_url,
        allow_private_targets=_bool("ALLOW_PRIVATE_TARGETS", env != "production"),
        max_repo_files=max(1, min(100, int(os.environ.get("MAX_REPO_FILES", "25")))),
        job_workers=max(1, int(os.environ.get("JOB_WORKERS", "3"))),
    )


def secret_values() -> list[str]:
    """Values that must never appear in logs, errors or sandboxed processes."""
    names = ("GEMINI_API_KEY", "GITHUB_CLIENT_SECRET", "SESSION_SECRET", "DATABASE_URL")
    return [os.environ[n] for n in names if os.environ.get(n)]


def redact(text: str) -> str:
    """Replace any configured secret value inside *text* with ***."""
    if not text:
        return text
    for value in secret_values():
        if len(value) >= 8:
            text = text.replace(value, "***")
    return text
