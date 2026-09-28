"""Tests for history scoping, team visibility and dashboard aggregation (real DB, GitHub mocked)."""

from unittest.mock import patch

import db
import github_api


def _completed_run(user_id, login, project, owner=None, kind="repo", findings=3, failed=1):
    db.upsert_user({"id": user_id, "login": login, "name": None, "avatar_url": None})
    run_id = db.create_run(user_id=user_id, user_login=login, kind=kind, project=project, owner=owner,
                           ref="main", commit_sha="b" * 40, model="gemini-3.7-flash")
    db.update_run(run_id, status="completed", report={"files": []}, finished_at=db.now(), duration_ms=1200,
                  summary={"findings": findings, "critical": 1, "high": 1, "tests_passed": 4, "tests_failed": failed})
    return run_id


def test_overview_empty_for_new_user(signed_in_client):
    data = signed_in_client.get("/api/dashboard/overview").json()
    assert data["totals"]["runs"] == 0
    assert data["projects"] == []


def test_overview_aggregates_latest_run_per_project(signed_in_client):
    u = signed_in_client.user
    _completed_run(u["id"], u["login"], "me/app", owner=u["login"], findings=10)
    _completed_run(u["id"], u["login"], "me/app", owner=u["login"], findings=4)
    _completed_run(u["id"], u["login"], "me/lib", owner=u["login"], findings=1, failed=0)
    data = signed_in_client.get("/api/dashboard/overview").json()
    assert data["totals"]["runs"] == 3
    assert data["totals"]["projects"] == 2
    app = next(p for p in data["projects"] if p["project"] == "me/app")
    assert app["runs"] == 2
    assert app["latest"]["headline"]["findings"] == 4  # latest, not the sum
    assert [t["findings"] for t in app["trend"]] == [10, 4]  # chronological
    assert data["totals"]["open_findings"] == 5


def test_history_is_private_by_default(signed_in_client):
    _completed_run(999001, "someone-else", "other/repo", owner="other")
    runs = signed_in_client.get("/api/runs").json()["runs"]
    assert all(r["user_login"] == signed_in_client.user["login"] for r in runs)


def test_team_scope_requires_membership(signed_in_client):
    with patch.object(github_api, "user_org_logins", return_value=[]), patch("main.user_org_logins", return_value=[]):
        resp = signed_in_client.get("/api/runs?scope=team:acme")
    assert resp.status_code == 403


def test_team_member_sees_teammates_runs_and_run_detail(signed_in_client):
    other = _completed_run(999002, "teammate", "acme/api", owner="acme")
    with patch("main.user_org_logins", return_value=["acme"]):
        runs = signed_in_client.get("/api/runs?scope=team:acme").json()["runs"]
        detail = signed_in_client.get(f"/api/runs/{other}")
    assert any(r["id"] == other for r in runs)
    assert detail.status_code == 200 and detail.json()["is_mine"] is False


def test_non_member_cannot_open_team_run(signed_in_client):
    other = _completed_run(999003, "stranger", "corp/secret", owner="corp")
    with patch("main.user_org_logins", return_value=[]):
        assert signed_in_client.get(f"/api/runs/{other}").status_code == 404
        assert signed_in_client.delete(f"/api/runs/{other}").status_code == 404


class FakeOrgClient:
    def __init__(self, *_a):
        pass

    def org(self, org):
        return {"login": org, "name": "Acme Corp", "avatar_url": None, "description": "d", "html_url": "https://github.com/acme"}

    def org_repos(self, org):
        return [
            {"full_name": "acme/api", "name": "api", "owner": "acme", "private": True, "language": "Go",
             "default_branch": "main", "pushed_at": "2026-09-01T00:00:00Z", "html_url": "https://github.com/acme/api"},
            {"full_name": "acme/web", "name": "web", "owner": "acme", "private": False, "language": "TypeScript",
             "default_branch": "main", "pushed_at": "2026-09-02T00:00:00Z", "html_url": "https://github.com/acme/web"},
        ]

    def org_members(self, org):
        return [{"login": "teammate2", "avatar_url": None}, {"login": "x", "avatar_url": None}]


def test_team_dashboard_combines_github_and_runs(signed_in_client):
    _completed_run(999004, "teammate2", "acme/api", owner="acme-dash", findings=7)
    with patch("main.user_org_logins", return_value=["acme-dash"]), patch("main.GitHubClient", FakeOrgClient):
        data = signed_in_client.get("/api/dashboard/team/acme-dash").json()
    assert data["totals"]["repositories"] == 2
    assert data["totals"]["members"] == 2
    assert data["contributors"][0]["login"] == "teammate2"
    assert data["activity"][0]["project"] == "acme/api"


def test_team_dashboard_forbidden_for_non_member(signed_in_client):
    with patch("main.user_org_logins", return_value=["other"]):
        assert signed_in_client.get("/api/dashboard/team/acme").status_code == 403


def test_delete_own_run(signed_in_client):
    u = signed_in_client.user
    run_id = _completed_run(u["id"], u["login"], "me/tmp")
    assert signed_in_client.delete(f"/api/runs/{run_id}").status_code == 204
    assert signed_in_client.get(f"/api/runs/{run_id}").status_code == 404


def test_interrupted_runs_marked_failed():
    db.upsert_user({"id": 999005, "login": "x", "name": None, "avatar_url": None})
    run_id = db.create_run(user_id=999005, user_login="x", kind="code", project="p")
    db.update_run(run_id, status="running")
    db.mark_interrupted_runs()
    assert db.get_run(run_id)["status"] == "failed"


def test_nul_characters_are_stripped_before_storing():
    # Postgres rejects NUL in text/JSONB; uploaded files or tool output may contain it.
    db.upsert_user({"id": 999006, "login": "nul", "name": None, "avatar_url": None})
    run_id = db.create_run(user_id=999006, user_login="nul", kind="code", project="a\x00b")
    db.update_run(run_id, status="completed", error="x\x00y", report={"files": [{"source": "print(1)\x00"}]},
                  summary={"note": "\x00"})
    row = db.get_run(run_id)
    assert row["project"] == "ab" and row["error"] == "xy"
    assert row["report"]["files"][0]["source"] == "print(1)" and row["summary"]["note"] == ""
