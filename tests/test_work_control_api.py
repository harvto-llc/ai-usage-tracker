import os
import subprocess
from unittest.mock import patch

os.environ.setdefault("USAGE_TRACKER_SECRET", "test-secret")

import pytest
from fastapi.testclient import TestClient

from src import usage_ledger, work_ledger, work_refresh
from src.api import API_SECRET, app


AUTH = {"Authorization": f"Bearer {API_SECRET}"}


@pytest.fixture(autouse=True)
def activity_db(tmp_path, monkeypatch):
    db_path = tmp_path / "activity.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(db_path))
    usage_ledger._initialized_paths.discard(str(db_path))
    work_ledger._initialized_paths.discard(str(db_path))
    work_refresh._refresh_thread = None


@pytest.fixture
def client():
    return TestClient(app)


def test_collection_settings_are_authenticated_and_start_async_refresh(client):
    assert client.put("/work-ledger/settings", json={"enabled": True}).status_code == 401

    with patch.object(
        work_refresh,
        "start_background_refresh",
        return_value={"accepted": True, "reason": "started"},
    ) as refresh:
        response = client.put(
            "/work-ledger/settings",
            headers=AUTH,
            json={
                "enabled": True,
                "explicit_roots": ["/repo"],
                "retention_days": 90,
            },
        )

    assert response.status_code == 200
    assert response.json()["settings"]["collection_enabled"] is True
    assert response.json()["settings"]["explicit_roots"] == ["/repo"]
    refresh.assert_called_once_with()


def test_work_item_and_active_tag_lifecycle(client):
    project_response = client.post(
        "/work-ledger/items",
        headers=AUTH,
        json={"kind": "project", "name": "Client Portal", "color_hex": "#00AA88"},
    )
    assert project_response.status_code == 200
    project = project_response.json()["item"]
    task_response = client.post(
        "/work-ledger/items",
        headers=AUTH,
        json={"kind": "task", "name": "Billing", "parent_id": project["id"]},
    )
    task = task_response.json()["item"]

    selected = client.put(
        "/work-ledger/active",
        headers=AUTH,
        json={"work_item_id": task["id"]},
    )
    listed = client.get("/work-ledger/items", headers=AUTH)
    stopped = client.put(
        "/work-ledger/active",
        headers=AUTH,
        json={"work_item_id": None},
    )
    archived = client.delete(f"/work-ledger/items/{project['id']}", headers=AUTH)

    assert selected.status_code == 200
    assert selected.json()["work_item_id"] == task["id"]
    assert len(listed.json()["items"]) == 2
    assert listed.json()["active"]["work_item_id"] == task["id"]
    assert stopped.json() == {"state": "unassigned"}
    assert archived.json()["status"] == "archived"
    assert client.get("/work-ledger/items", headers=AUTH).json()["items"] == []


def test_invalid_work_item_and_tag_return_clear_client_errors(client):
    orphan = client.post(
        "/work-ledger/items",
        headers=AUTH,
        json={"kind": "task", "name": "Orphan"},
    )
    missing = client.put(
        "/work-ledger/active",
        headers=AUTH,
        json={"work_item_id": "missing"},
    )

    assert orphan.status_code == 400
    assert "project parent" in orphan.json()["detail"]
    assert missing.status_code == 400
    assert "active work item" in missing.json()["detail"]


def test_session_list_and_per_session_tag_mutations(client, tmp_path):
    project = work_ledger.create_work_item(kind="project", name="Usage Tracker", now=1)
    session_id = work_ledger.upsert_ai_session({
        "provider": "codex",
        "provider_session_id": "codex-session",
        "source_modified_at_us": int(tmp_path.stat().st_mtime_ns // 1_000),
        "native_title": "Session tagging",
    })

    assert client.get("/work-ledger/sessions?active_only=true").status_code == 401
    renamed = client.patch(
        f"/work-ledger/sessions/{session_id}",
        headers=AUTH,
        json={"nickname": "Menu session"},
    )
    assigned = client.patch(
        f"/work-ledger/sessions/{session_id}",
        headers=AUTH,
        json={"assignment_mode": "work_item", "work_item_id": project["id"]},
    )
    listed = client.get("/work-ledger/sessions?active_only=true", headers=AUTH)

    assert renamed.status_code == 200
    assert assigned.status_code == 200
    assert listed.status_code == 200
    session = listed.json()["sessions"][0]
    assert session["display_name"] == "Session tagging"
    assert session["nickname"] == "Menu session"
    assert session["assignment_mode"] == "work_item"
    assert session["effective_work_label"] == "Usage Tracker"

    invalid = client.patch(
        f"/work-ledger/sessions/{session_id}",
        headers=AUTH,
        json={"assignment_mode": "work_item", "work_item_id": "missing"},
    )
    assert invalid.status_code == 400


def test_repository_enablement_and_manual_refresh(client):
    work_ledger.upsert_repository({
        "id": "repo-1",
        "display_name": "Repo",
        "identity_kind": "root_commit",
        "common_dir": "/repo/.git",
        "enabled": False,
    })
    enabled = client.patch(
        "/work-ledger/repositories/repo-1",
        headers=AUTH,
        json={"enabled": True},
    )
    missing = client.patch(
        "/work-ledger/repositories/missing",
        headers=AUTH,
        json={"enabled": True},
    )
    with patch.object(
        work_refresh,
        "start_background_refresh",
        return_value={"accepted": True, "reason": "started"},
    ):
        refreshed = client.post("/work-ledger/refresh", headers=AUTH)

    assert enabled.status_code == 200
    assert enabled.json()["repository"]["enabled"] is True
    assert missing.status_code == 404
    assert refreshed.json()["accepted"] is True


def test_project_from_folder_creates_reuses_and_assigns(client, tmp_path):
    repository = tmp_path / "Client Portal"
    nested = repository / "src"
    nested.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", str(repository), "init", "-b", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    session_id = work_ledger.upsert_ai_session({
        "provider": "codex",
        "provider_session_id": "folder-project-session",
    })

    assert client.post(
        "/work-ledger/projects/from-folder",
        json={"path": str(nested), "session_id": session_id},
    ).status_code == 401
    with patch.object(
        work_refresh,
        "start_background_refresh",
        return_value={"accepted": True, "reason": "started"},
    ):
        created = client.post(
            "/work-ledger/projects/from-folder",
            headers=AUTH,
            json={"path": str(nested), "session_id": session_id},
        )
        repeated = client.post(
            "/work-ledger/projects/from-folder",
            headers=AUTH,
            json={"path": str(repository)},
        )

    assert created.status_code == 200
    payload = created.json()
    assert payload["item"]["name"] == "Client Portal"
    assert payload["repository"]["enabled"] is True
    assert payload["assigned_session_id"] == session_id
    assert repeated.json()["item"]["id"] == payload["item"]["id"]
    assert work_ledger.settings()["explicit_roots"] == [str(repository)]
    session = work_ledger.list_ai_sessions()[0]
    assert session["assignment_mode"] == "work_item"
    assert session["assigned_work_item_id"] == payload["item"]["id"]


def test_project_from_folder_rejects_non_git_folder_and_missing_session(client, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    invalid_folder = client.post(
        "/work-ledger/projects/from-folder",
        headers=AUTH,
        json={"path": str(plain)},
    )
    missing_session = client.post(
        "/work-ledger/projects/from-folder",
        headers=AUTH,
        json={"path": str(plain), "session_id": "missing"},
    )

    assert invalid_folder.status_code == 400
    assert invalid_folder.json()["detail"] == "Choose a folder inside a Git repository"
    assert missing_session.status_code == 404


def test_report_requires_auth_and_valid_period(client):
    assert client.get("/work-ledger/report").status_code == 401
    invalid = client.get(
        "/work-ledger/report?period=quarter",
        headers=AUTH,
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "period must be day, week, or month"
    report = client.get("/work-ledger/report?period=day", headers=AUTH)
    assert report.status_code == 200
    assert report.json()["period"] == "day"


RATIO_FIELDS = ("cost_per_pr", "cost_per_commit", "cost_per_changed_line")


def test_report_contract_exposes_cost_ratios_with_their_coverage(client):
    """The provider contract carries the ratios next to the coverage that qualifies them."""
    report = client.get("/work-ledger/report?period=day", headers=AUTH)
    assert report.status_code == 200
    totals = report.json()["totals"]

    for field in RATIO_FIELDS:
        assert field in totals, f"{field} missing from the report contract"
    assert "pricing_coverage_pct" in totals


def test_report_contract_suppresses_ratios_as_null_never_zero(client):
    """A zero denominator must reach the wire as JSON null, not 0.

    An empty period has no pull requests, commits or changed lines, and no measured
    cost. Serialising that as 0 would tell a consumer the work was free; null says
    the ratio is not stateable. The distinction has to survive JSON, not just live
    inside Python, so this asserts on the parsed response body.
    """
    response = client.get("/work-ledger/report?period=day", headers=AUTH)
    assert response.status_code == 200
    totals = response.json()["totals"]

    assert totals["git_pull_requests"] == 0
    assert totals["git_commits"] == 0
    assert totals["changed_lines"] == 0
    for field in RATIO_FIELDS:
        assert totals[field] is None, f"{field} must be null when its denominator is zero"

    # Guard the exact confusion this rule exists to prevent: a suppressed ratio must
    # not be serialised as a number a consumer could chart or sort as "cheapest".
    raw = response.text
    for field in RATIO_FIELDS:
        assert f'"{field}": 0' not in raw and f'"{field}":0' not in raw


def test_report_contract_ratios_apply_to_every_bucket_list(client):
    """Ratios are defined once, so every bucket list in the contract carries them."""
    report = client.get("/work-ledger/report?period=day", headers=AUTH).json()

    for bucket_list in ("projects", "work_items", "repositories", "providers"):
        assert bucket_list in report
        for row in report[bucket_list]:
            for field in RATIO_FIELDS:
                assert field in row["metrics"], (
                    f"{field} missing from a {bucket_list} bucket"
                )
