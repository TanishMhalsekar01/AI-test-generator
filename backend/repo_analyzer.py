"""
Repository analysis: pick source files from a GitHub repository (any
language), pin the commit being analysed, and run every file through the
code_review pipeline with bounded concurrency.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import PurePosixPath
from typing import Callable, Optional

import code_review
from github_api import GitHubClient
from languages import is_source_file

MAX_FILE_BYTES = 200_000
FILE_WORKERS = 3

SKIP_DIRS = {
    ".git", ".github", "node_modules", "vendor", "third_party", "third-party", "dist", "build", "out",
    "target", "bin", "obj", "coverage", "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache",
    ".next", ".nuxt", ".svelte-kit", ".gradle", ".idea", ".vscode", "Pods", "site-packages", "migrations",
    "bower_components", "jspm_packages", "public/assets", "static/vendor",
}
SKIP_SUFFIXES = (".min.js", ".min.css", ".bundle.js", ".d.ts", ".pb.go", "_pb2.py", ".generated.cs", ".g.dart")
TEST_MARKERS = ("test", "tests", "spec", "specs", "__tests__", "testing")


def _is_test_path(path: str) -> bool:
    p = PurePosixPath(path.lower())
    name = p.name
    if any(part in TEST_MARKERS for part in p.parts[:-1]):
        return True
    return (name.startswith("test_") or name.endswith(("_test.py", "_test.go", "_spec.rb"))
            or ".test." in name or ".spec." in name)


def select_files(entries: list[dict], max_files: int) -> tuple[list[dict], dict]:
    candidates = []
    skipped = {"vendor_or_build": 0, "too_large": 0, "not_source": 0, "generated": 0}
    for e in entries:
        path = e["path"]
        parts = PurePosixPath(path).parts
        if any(part in SKIP_DIRS for part in parts[:-1]):
            skipped["vendor_or_build"] += 1
            continue
        if path.endswith(SKIP_SUFFIXES):
            skipped["generated"] += 1
            continue
        if not is_source_file(path):
            skipped["not_source"] += 1
            continue
        if (e.get("size") or 0) > MAX_FILE_BYTES:
            skipped["too_large"] += 1
            continue
        candidates.append(e)
    # Application code first, tests last; shallower paths first, then larger files.
    candidates.sort(key=lambda e: (_is_test_path(e["path"]), e["path"].count("/"), -(e.get("size") or 0)))
    return candidates[:max_files], {"candidates": len(candidates), **skipped}


def analyze_repository(client: GitHubClient, owner: str, repo: str, commit_sha: str, max_files: int,
                       progress: Optional[Callable[[int, str], None]] = None) -> tuple[dict, dict]:
    report_progress = progress or (lambda _pct, _msg: None)
    report_progress(2, "Reading repository tree")
    entries, truncated_tree = client.tree(owner, repo, commit_sha)
    selected, stats = select_files(entries, max_files)

    file_reports: list[dict] = []
    unreadable: list[str] = []
    done = 0

    def work(entry: dict) -> Optional[dict]:
        text = client.blob_text(owner, repo, entry["sha"])
        if text is None:
            return None
        return code_review.review_file(entry["path"], text)

    with ThreadPoolExecutor(max_workers=FILE_WORKERS) as pool:
        futures = {pool.submit(work, e): e for e in selected}
        for fut in as_completed(futures):
            entry = futures[fut]
            done += 1
            try:
                rep = fut.result()
            except Exception as exc:  # one broken file must not sink the whole run
                rep = None
                unreadable.append(f"{entry['path']} ({type(exc).__name__}: {exc})")
            if rep is None:
                if not any(u.startswith(entry["path"]) for u in unreadable):
                    unreadable.append(f"{entry['path']} (binary or not UTF-8)")
            else:
                file_reports.append(rep)
            report_progress(5 + int(90 * done / max(len(selected), 1)), f"Analysed {done}/{len(selected)}: {entry['path']}")

    file_reports.sort(key=lambda r: r["file"])
    summary = code_review.summarize(file_reports)
    summary.update({
        "files_found": len(entries),
        "source_files": stats["candidates"],
        "files_selected": len(selected),
        "files_skipped_due_to_limit": max(0, stats["candidates"] - len(selected)),
    })
    report = {
        "repository": f"{owner}/{repo}",
        "commit_sha": commit_sha,
        "tree_truncated": truncated_tree,
        "selection": {**stats, "max_files": max_files, "selected": [e["path"] for e in selected]},
        "unreadable": unreadable,
        "files": file_reports,
    }
    return report, summary


def languages_of(file_reports: list[dict]) -> list[str]:
    seen: list[str] = []
    for rep in file_reports:
        if rep["language"] not in seen:
            seen.append(rep["language"])
    return seen
