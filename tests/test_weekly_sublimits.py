import os

os.environ.setdefault("USAGE_TRACKER_SECRET", "test-secret")

from unittest.mock import patch

from fastapi.testclient import TestClient

import src.api as api_module
import src.database as db
from src.api import API_SECRET, _remote_cc, app
from src.pty_scraper import _parse_claude_web_usage_payload

AUTH = {"Authorization": f"Bearer {API_SECRET}"}


def _reset_remote_state() -> None:
    for key in list(_remote_cc.keys()):
        _remote_cc[key] = 0 if key == "ts" else None
    api_module._stats_cache = None
    api_module._stats_cache_ts = 0


def test_sonnet_only_payload_omits_design_sublimit():
    payload = {
        "weekly": {
            "sonnetOnly": {
                "usedPercent": 9,
                "resetAt": "2026-04-17T06:00:00Z",
            },
        },
    }

    result = _parse_claude_web_usage_payload(payload)

    assert result["weekly_sonnet_pct"] == 9
    assert "weekly_design_pct" not in result
    assert "weekly_pct" not in result
    assert "weekly_reset" not in result


def test_post_usage_with_only_sonnet_defaults_design_to_none(tmp_path):
    test_db = tmp_path / "test.db"
    with patch.object(db, "DB", test_db):
        db.init()
        _reset_remote_state()
        try:
            with TestClient(app) as client:
                payload = {
                    "messages": {"total_messages": 5},
                    "tokens": {"total_tokens": 100},
                    "projects": [],
                    "usage": {
                        "session_pct": 40,
                        "weekly_pct": 20,
                        "extra_pct": 0,
                        "weekly_sonnet_pct": 9,
                    },
                }

                response = client.post("/cc/report", json=payload, headers=AUTH)
                assert response.status_code == 200

                sample = db.latest_sample()
                assert sample is not None
                assert sample["weekly_sonnet_pct"] == 9
                assert sample["weekly_design_pct"] is None

                stats = client.get("/stats", headers=AUTH)
                assert stats.status_code == 200
                claude_quota = stats.json()["claude_quota"]
                assert claude_quota["weekly_sonnet_used_pct"] == 9
                assert claude_quota["weekly_design_used_pct"] is None
        finally:
            _reset_remote_state()
