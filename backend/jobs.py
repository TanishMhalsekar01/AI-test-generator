"""
Background execution of analysis runs.

A run row is created by the request handler; the work happens on a thread
pool and progress is written to the database so the UI can poll
GET /api/runs/{id}. Set ``jobs.INLINE = True`` (tests) to run synchronously.
"""

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import db
from config import get_settings, redact

INLINE = False
_executor: Optional[ThreadPoolExecutor] = None
_lock = threading.Lock()

ProgressFn = Callable[[int, str], None]
JobFn = Callable[[ProgressFn], tuple[dict, dict, dict]]  # -> (report, summary, extra run columns)


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=get_settings().job_workers, thread_name_prefix="run")
        return _executor


def _execute(run_id: str, fn: JobFn) -> None:
    started = db.now()
    t0 = time.monotonic()
    db.update_run(run_id, status="running", started_at=started, progress=1, progress_note="Starting")
    last: dict = {"state": None}

    def progress(pct: int, note: str) -> None:
        state = (max(1, min(99, int(pct))), note[:500])
        if state == last["state"]:
            return  # nothing new to write
        last["state"] = state
        db.update_run(run_id, progress=state[0], progress_note=state[1])

    try:
        report, summary, extra = fn(progress)
        db.update_run(run_id, status="completed", report=report, summary=summary, progress=100,
                      progress_note="Done", finished_at=db.now(),
                      duration_ms=int((time.monotonic() - t0) * 1000), **extra)
    except Exception as exc:  # the run must always reach a terminal state
        traceback.print_exc()
        message = str(exc) if str(exc) else type(exc).__name__
        db.update_run(run_id, status="failed", error=redact(message)[:2000], finished_at=db.now(),
                      duration_ms=int((time.monotonic() - t0) * 1000))


def submit(run_id: str, fn: JobFn) -> None:
    if INLINE:
        _execute(run_id, fn)
    else:
        _pool().submit(_execute, run_id, fn)
