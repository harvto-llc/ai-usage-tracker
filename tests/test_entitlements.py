from src.entitlements import normalize_plan_id, resolve_plan


def test_normalizes_internal_and_public_plan_names():
    assert normalize_plan_id("codex", "prolite") == "pro"
    assert normalize_plan_id("codex", "Pro 20x") == "pro-20x"
    assert normalize_plan_id("claude", "Max (5x)") == "max-5x"
    assert normalize_plan_id("claude", "Max 20x") == "max-20x"


def test_detected_plan_wins_over_stale_configuration():
    plan = resolve_plan(
        "codex",
        "pro-20x",
        plans={"codex": {"plan": "Plus"}},
    )

    assert plan["plan_id"] == "pro-20x"
    assert plan["plan_label"] == "Pro 20x"
    assert plan["plan_source"] == "detected"


def test_codex_app_server_prolite_is_public_pro():
    plan = resolve_plan("codex", "prolite", plans={"codex": {"plan": "Plus"}})

    assert plan["plan_id"] == "pro"
    assert plan["plan_label"] == "Pro"
    assert plan["entitlements"]["expected_spark_access"] is True


def test_configuration_is_only_a_fallback():
    plan = resolve_plan("claude", plans={"claude": {"plan": "Max 5x"}})

    assert plan["plan_id"] == "max-5x"
    assert plan["plan_source"] == "configured"


def test_unknown_plan_is_preserved_without_claiming_known_entitlements():
    plan = resolve_plan("codex", "future-ultra")

    assert plan["plan_id"] == "future-ultra"
    assert plan["plan_label"] == "Future Ultra"
    assert plan["known_plan"] is False
