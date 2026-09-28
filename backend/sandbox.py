"""
Subprocess sandbox for compilers, linters and generated tests.

Untrusted code (the user's source and the generated tests) runs:
  - in a throwaway temp directory,
  - with a scrubbed environment (no API keys, OAuth secrets or DB URLs),
  - with a wall-clock timeout that kills the whole process group,
  - with CPU / file-size / core-dump rlimits (Linux),
  - as the unprivileged SANDBOX_USER when the server itself runs as root
    (the Docker image does this), so the child cannot read the server's
    /proc/<pid>/environ or the .env file.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

try:
    import pwd
    import resource
except ImportError:  # pragma: no cover - non-POSIX
    pwd = None
    resource = None

MAX_OUTPUT_CHARS = 20000

_PASS_THROUGH = ("PATH", "LANG", "LC_ALL", "JAVA_HOME", "GOROOT", "RUSTUP_HOME", "CARGO_HOME", "NODE_PATH",
                 "GEM_HOME", "GEM_PATH")
_SHARED_CACHE = Path(tempfile.gettempdir()) / "aitestgen-cache"
# Toolchain managers that locate their installs under $HOME. The sandbox replaces
# HOME with the throwaway workdir, so point them at the real locations instead
# (e.g. rustup on GitHub runners keeps toolchains in ~/.rustup without RUSTUP_HOME set).
_HOME_TOOLCHAINS = {"RUSTUP_HOME": ".rustup", "CARGO_HOME": ".cargo"}


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int

    @property
    def output(self) -> str:
        return (self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr).strip()


def which(tool: str) -> Optional[str]:
    return shutil.which(tool)


def _sandbox_ids() -> Optional[tuple[int, int]]:
    if pwd is None or os.geteuid() != 0:
        return None
    name = os.environ.get("SANDBOX_USER", "sandbox")
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return None
    return entry.pw_uid, entry.pw_gid


def scrubbed_env(workdir: Path, extra: Optional[dict] = None) -> dict:
    env = {k: os.environ[k] for k in _PASS_THROUGH if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    if _sandbox_ids() is None:  # a dropped-privilege sandbox user cannot read our home anyway
        real_home = Path.home()
        for var, sub in _HOME_TOOLCHAINS.items():
            if var not in env and (real_home / sub).is_dir():
                env[var] = str(real_home / sub)
    cache = _SHARED_CACHE
    # TMPDIR must differ from the working directory: Go ignores a go.mod that
    # sits in the temp root.
    env.update({
        "HOME": str(workdir),
        "TMPDIR": str(workdir / ".tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "NO_COLOR": "1",
        "CI": "1",
        "GOCACHE": str(cache / "go-build"),
        "GOPATH": str(cache / "gopath"),
        "GOFLAGS": "-mod=mod",
        "GOTOOLCHAIN": "local",
        "GOPROXY": "off",
        "GOTELEMETRY": "off",
    })
    if extra:
        env.update(extra)
    return env


def make_workdir(prefix: str = "aitg-") -> Path:
    path = Path(tempfile.mkdtemp(prefix=prefix))
    ids = _sandbox_ids()
    if ids:
        os.chown(path, *ids)
    return path


def prepare_for_sandbox(path: Path) -> None:
    """Hand ownership of files written by the server to the sandbox user."""
    ids = _sandbox_ids()
    if not ids:
        return
    for p in [path, *path.rglob("*")]:
        try:
            os.chown(p, *ids)
        except OSError:
            pass


def remove_workdir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _preexec(cpu_seconds: int, ids: Optional[tuple[int, int]]):
    def apply():
        if resource is not None:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if ids:
            os.setgid(ids[1])
            os.setgroups([])
            os.setuid(ids[0])
    return apply


def run(cmd: Sequence[str], cwd: Path, *, timeout: int = 30, env_extra: Optional[dict] = None,
        stdin_text: Optional[str] = None) -> ProcResult:
    ids = _sandbox_ids()
    _SHARED_CACHE.mkdir(parents=True, exist_ok=True)
    tmp = Path(cwd) / ".tmp"
    tmp.mkdir(exist_ok=True)
    if ids:
        for path in (_SHARED_CACHE, tmp):
            try:
                os.chown(path, *ids)
            except OSError:
                pass
    start = time.monotonic()
    kwargs = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
        kwargs["preexec_fn"] = _preexec(timeout + 5, ids)
    try:
        proc = subprocess.Popen(
            list(cmd),
            cwd=str(cwd),
            env=scrubbed_env(cwd, env_extra),
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            **kwargs,
        )
    except FileNotFoundError as exc:
        return ProcResult(127, "", f"{cmd[0]}: not found ({exc})", False, 0)

    timed_out = False
    try:
        stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
        stdout, stderr = proc.communicate()
    duration = int((time.monotonic() - start) * 1000)
    return ProcResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=(stdout or "")[:MAX_OUTPUT_CHARS],
        stderr=(stderr or "")[:MAX_OUTPUT_CHARS],
        timed_out=timed_out,
        duration_ms=duration,
    )


PYTHON = sys.executable
