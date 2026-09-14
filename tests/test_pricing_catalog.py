import json
from datetime import datetime, timezone

import pytest

from src import pricing_catalog
from src.plan_config import model_credit_rates, published_model_cost_rates


@pytest.fixture(autouse=True)
def _fresh_catalog():
    pricing_catalog.invalidate_cache()
    yield
    pricing_catalog.invalidate_cache()


def _at(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, tzinfo=timezone.utc)


def test_codex_token_rates_start_at_plus_pro_migration():
    assert model_credit_rates("gpt-5.6-terra", at=_at(2026, 4, 1)) is None
    assert model_credit_rates("gpt-5.6-terra", at=_at(2026, 4, 2)) == {
        "input": 62.5,
        "cache_read": 6.25,
        "output": 375.0,
    }


def test_current_codex_catalog_includes_52_53_and_cyber_but_not_spark():
    assert model_credit_rates("gpt-5.2-codex", at=_at(2026, 7, 24))["output"] == 350.0
    assert model_credit_rates("gpt-5.3-codex", at=_at(2026, 7, 24))["output"] == 350.0
    assert model_credit_rates("gpt-5.5-cyber", at=_at(2026, 7, 24))["output"] == 3000.0
    assert model_credit_rates("gpt-5.3-codex-spark", at=_at(2026, 7, 24)) is None

    info = pricing_catalog.pricing_rate_info(
        "codex", "gpt-5.3-codex-spark", "credits_per_mtok", at=_at(2026, 7, 24)
    )
    assert info["model_prefix"] == "gpt-5.3-codex-spark"
    assert "not final" in info["unpriced_reason"]


def test_sonnet_5_rates_follow_launch_and_promotion_dates():
    assert published_model_cost_rates("claude-sonnet-5", at=_at(2026, 6, 29)) is None
    assert published_model_cost_rates("claude-sonnet-5", at=_at(2026, 6, 30))["output"] == 10.0
    assert published_model_cost_rates("claude-sonnet-5", at=_at(2026, 9, 1))["output"] == 15.0


def test_local_catalog_adds_rates_and_overrides_credit_value(tmp_path, monkeypatch):
    cache = tmp_path / "pricing.json"
    cache.write_text(json.dumps({
        "version": 1,
        "updated_at": "2026-07-25T00:00:00Z",
        "codex_credit_usd_estimate": {
            "value": 0.05,
            "official": False,
            "source_label": "Local account estimate"
        },
        "rates": [{
            "provider": "codex",
            "model_prefix": "gpt-local",
            "basis": "credits_per_mtok",
            "effective_from": "2026-07-01T00:00:00Z",
            "rates": {"input": 1, "cache_read": 0.1, "output": 5},
            "source_label": "Local test rate",
            "official": False
        }]
    }))
    monkeypatch.setenv("USAGE_TRACKER_PRICING_CACHE", str(cache))

    info = pricing_catalog.pricing_rate_info(
        "codex", "gpt-local-v2", "credits_per_mtok", at=_at(2026, 7, 24)
    )

    assert info["rates"]["output"] == 5.0
    assert info["catalog_source"] == "local_cache"
    assert pricing_catalog.codex_credit_usd_estimate() == 0.05
    assert pricing_catalog.codex_credit_usd_is_estimate() is True
    assert pricing_catalog.pricing_catalog_status()["cache_error"] is None


def test_invalid_local_catalog_falls_back_to_bundled(tmp_path, monkeypatch):
    cache = tmp_path / "pricing.json"
    cache.write_text('{"version": 1, "rates": "bad"}')
    monkeypatch.setenv("USAGE_TRACKER_PRICING_CACHE", str(cache))

    status = pricing_catalog.pricing_catalog_status()

    assert status["source"] == "bundled"
    assert status["cache_error"]
    assert model_credit_rates("gpt-5.6-terra", at=_at(2026, 7, 24))["output"] == 375.0


def test_bundled_credit_value_uses_verified_checkout_rate():
    status = pricing_catalog.pricing_catalog_status()

    assert pricing_catalog.codex_credit_usd_estimate() == 0.04
    assert pricing_catalog.codex_credit_usd_is_estimate() is False
    assert status["codex_credit_usd_estimate"]["checkout_verified"] is True


def test_credit_dollar_value_can_be_overridden_by_environment(monkeypatch):
    monkeypatch.setenv("USAGE_TRACKER_CODEX_CREDIT_USD", "0.06")

    status = pricing_catalog.pricing_catalog_status()

    assert pricing_catalog.codex_credit_usd_estimate() == 0.06
    assert status["codex_credit_usd_estimate"]["configured_by_env"] is True
    assert status["codex_credit_usd_estimate"]["official"] is False
    assert status["codex_credit_usd_estimate"]["checkout_verified"] is False

    monkeypatch.setenv("USAGE_TRACKER_CODEX_CREDIT_USD_CHECKOUT_VERIFIED", "true")
    assert pricing_catalog.codex_credit_usd_is_estimate() is False
