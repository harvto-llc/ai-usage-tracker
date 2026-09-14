from src.provider_metrics import build_provider_snapshots


def test_cursor_limit_state_shapes_shared_and_unique_fields():
    snapshots = build_provider_snapshots(
        timestamp=1775779200,
        collector_access="subscription",
        messages=None,
        tokens=None,
        usage=None,
        codex_local=None,
        codex_sessions=None,
        codex_usage=None,
        cursor_usage={
            "plan": "free",
            "total_requests": 50,
            "total_tokens": 0,
            "max_requests": 50,
            "remaining_requests": 0,
            "at_limit": True,
            "limit_hit": True,
            "limit_kind": "free_requests",
            "limit_message": "You've hit your free requests limit.",
            "reset_at": "2026-05-10",
        },
    )

    cursor = snapshots["cursor"]

    assert cursor["status"] == "ok"
    assert cursor["shared"]["primary_used_pct"] == 100.0
    assert cursor["shared"]["primary_remaining_pct"] == 0.0
    assert cursor["shared"]["primary_reset"] == "2026-05-10"
    assert cursor["shared"]["messages_total_day"] == 50.0
    assert cursor["unique"]["max_requests"] == 50
    assert cursor["unique"]["remaining_requests"] == 0
    assert cursor["unique"]["at_limit"] is True
    assert cursor["unique"]["limit_kind"] == "free_requests"
