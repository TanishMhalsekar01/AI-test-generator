"""
Dashboard aggregations. Numbers come only from stored runs (the database) and
live GitHub API responses — nothing is estimated or sampled.
"""

from datetime import timedelta
from typing import Optional

import db
from github_api import GitHubClient, GitHubError


def headline(kind: str, summary: Optional[dict]) -> dict:
    """Comparable numbers for any run kind."""
    s = summary or {}
    if kind == "spec":
        return {
            "findings": s.get("issues_error", 0) + s.get("issues_warning", 0) + s.get("live_failed", 0),
            "critical": s.get("issues_error", 0),
            "high": s.get("live_failed", 0),
            "tests_passed": s.get("tests_passed", 0),
            "tests_failed": s.get("tests_failed", 0),
            "ai_incomplete": False,
        }
    return {
        "findings": s.get("findings", 0) + s.get("diagnostic_errors", 0),
        "critical": s.get("critical", 0),
        "high": s.get("high", 0),
        "tests_passed": s.get("tests_passed", 0),
        "tests_failed": s.get("tests_failed", 0),
        # Findings depend on the AI review; a count of 0 is not a clean bill of health if it never ran.
        "ai_incomplete": s.get("ai_errors", 0) > 0,
    }


def _project_rollup(rows: list[dict]) -> list[dict]:
    """rows are newest-first. One entry per project with its latest completed run and a trend."""
    projects: dict[str, dict] = {}
    for r in rows:
        p = projects.setdefault(r["project"], {
            "project": r["project"], "kind": r["kind"], "owner": r.get("owner"),
            "runs": 0, "latest": None, "last_run_at": db.iso(r["created_at"]), "trend": [],
        })
        p["runs"] += 1
        if r["status"] == "completed":
            h = headline(r["kind"], r.get("summary"))
            if p["latest"] is None:
                p["latest"] = {**db.serialize_run(r), "headline": h}
            if len(p["trend"]) < 12:
                p["trend"].append({"run_id": r["id"], "at": db.iso(r["created_at"]), **h})
    for p in projects.values():
        p["trend"].reverse()  # chronological for charts
    return sorted(projects.values(), key=lambda p: p["last_run_at"] or "", reverse=True)


def overview(user_id: int) -> dict:
    rows = db.list_runs(user_id=user_id, limit=1000)
    since = db.now() - timedelta(days=30)
    projects = _project_rollup(rows)
    latest = [p["latest"]["headline"] for p in projects if p["latest"]]
    return {
        "totals": {
            "runs": len(rows),
            "runs_30d": db.count_runs(user_id=user_id, since=since),
            "projects": len(projects),
            "open_findings": sum(h["findings"] for h in latest),
            "critical": sum(h["critical"] for h in latest),
            "failing_tests": sum(h["tests_failed"] for h in latest),
            "ai_incomplete_projects": sum(bool(h.get("ai_incomplete")) for h in latest),
            "running": sum(r["status"] in {"queued", "running"} for r in rows),
        },
        "projects": projects,
        "recent": [db.serialize_run(r) for r in rows[:10]],
    }


def team(client: GitHubClient, org: str) -> dict:
    errors: list[str] = []
    try:
        info = client.org(org)
        org_view = {"login": info["login"], "name": info.get("name"), "avatar_url": info.get("avatar_url"),
                    "description": info.get("description"), "html_url": info.get("html_url")}
    except GitHubError as exc:
        org_view = {"login": org, "name": None, "avatar_url": None, "description": None,
                    "html_url": f"https://github.com/{org}"}
        errors.append(f"Organisation profile: {exc}")
    try:
        repos = client.org_repos(org)
    except GitHubError as exc:
        repos = []
        errors.append(f"Repositories: {exc}")
    try:
        members = client.org_members(org)
    except GitHubError as exc:
        members = []
        errors.append(f"Members: {exc}")

    rows = db.list_runs(owners=[org], limit=2000)
    since = db.now() - timedelta(days=30)
    rollup = {p["project"].lower(): p for p in _project_rollup(rows)}

    repo_view = []
    for repo in repos:
        p = rollup.get(repo["full_name"].lower())
        repo_view.append({**repo, "runs": p["runs"] if p else 0,
                          "latest": p["latest"] if p else None, "trend": p["trend"] if p else []})
    analysed = [r for r in repo_view if r["latest"]]
    not_analysed = sorted((r for r in repo_view if not r["latest"]), key=lambda r: r["pushed_at"] or "", reverse=True)
    analysed.sort(key=lambda r: r["latest"]["created_at"] or "", reverse=True)

    by_member: dict[str, dict] = {}
    for r in rows:
        m = by_member.setdefault(r["user_login"], {"login": r["user_login"], "runs": 0, "runs_30d": 0, "last_run_at": None})
        m["runs"] += 1
        created = r["created_at"] if r["created_at"].tzinfo else r["created_at"].replace(tzinfo=since.tzinfo)
        if created >= since:
            m["runs_30d"] += 1
        m["last_run_at"] = m["last_run_at"] or db.iso(r["created_at"])
    avatars = {m["login"]: m["avatar_url"] for m in members}
    member_view = sorted(
        [{**m, "avatar_url": avatars.get(m["login"])} for m in by_member.values()],
        key=lambda m: m["runs"], reverse=True,
    )

    latest = [r["latest"]["headline"] for r in analysed]
    return {
        "org": org_view,
        "errors": errors,
        "totals": {
            "repositories": len(repos),
            "members": len(members),
            "repositories_analysed": len(analysed),
            "runs_30d": sum(m["runs_30d"] for m in member_view),
            "open_findings": sum(h["findings"] for h in latest),
            "failing_tests": sum(h["tests_failed"] for h in latest),
        },
        "repositories": analysed + not_analysed,
        "contributors": member_view,
        "members": members,
        "activity": [db.serialize_run(r) for r in rows[:25]],
    }
