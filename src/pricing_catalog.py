"""Validated, effective-dated pricing metadata with an offline fallback."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BUNDLED_CATALOG_PATH = Path(__file__).with_name("pricing_catalog.json")
_BASES = {"api_usd_per_mtok", "credits_per_mtok"}
_PROVIDERS = {"claude", "codex"}
_REQUIRED_RATES = {
    "api_usd_per_mtok": {"input", "output", "cache_read", "cache_write"},
    "credits_per_mtok": {"input", "output", "cache_read"},
}
_lock = threading.Lock()
_cached_key: tuple[str, int | None] | None = None
_cached_catalog: dict[str, Any] | None = None
_next_cache_check = 0.0


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError("catalog timestamps must be strings")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_credit_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("codex_credit_usd_estimate must be an object")
    amount = value.get("value")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount < 0:
        raise ValueError("codex_credit_usd_estimate.value must be non-negative")
    return {
        "value": float(amount),
        "official": bool(value.get("official", False)),
        "checkout_verified": bool(value.get("checkout_verified", False)),
        "source_label": str(value.get("source_label") or "Configured estimated credit value"),
        "source_url": str(value.get("source_url") or "") or None,
        "note": str(value.get("note") or "") or None,
    }


def _validate_rate(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("rate entries must be objects")
    provider = entry.get("provider")
    basis = entry.get("basis")
    prefix = entry.get("model_prefix")
    if provider not in _PROVIDERS or basis not in _BASES:
        raise ValueError("rate provider or basis is invalid")
    if not isinstance(prefix, str) or not prefix.strip():
        raise ValueError("model_prefix must be non-empty")
    start = entry.get("effective_from")
    end = entry.get("effective_until")
    start_at = _timestamp(start)
    end_at = _timestamp(end)
    if start_at and end_at and start_at > end_at:
        raise ValueError("effective_from must precede effective_until")

    raw_rates = entry.get("rates")
    reason = entry.get("unpriced_reason")
    if raw_rates is None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("unpriced entries require unpriced_reason")
        rates = None
    else:
        if not isinstance(raw_rates, dict) or not _REQUIRED_RATES[basis].issubset(raw_rates):
            raise ValueError("rate entry is missing required token categories")
        rates = {}
        for key, value in raw_rates.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise ValueError("token rates must be non-negative numbers")
            rates[str(key)] = float(value)

    return {
        "provider": provider,
        "model_prefix": prefix.strip(),
        "basis": basis,
        "effective_from": start_at,
        "effective_until": end_at,
        "rates": rates,
        "unpriced_reason": str(reason).strip() if reason else None,
        "source_label": str(entry.get("source_label") or "Pricing catalog"),
        "source_url": str(entry.get("source_url") or "") or None,
        "official": bool(entry.get("official", False)),
    }


def _validate_catalog(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("pricing catalog version must be 1")
    raw_rates = payload.get("rates")
    if not isinstance(raw_rates, list):
        raise ValueError("pricing catalog rates must be a list")
    updated_at = _timestamp(payload.get("updated_at"))
    return {
        "version": 1,
        "updated_at": updated_at,
        "credit_value": _validate_credit_value(payload.get("codex_credit_usd_estimate")),
        "rates": [_validate_rate(entry) for entry in raw_rates],
    }


def _read_catalog(path: Path) -> dict[str, Any]:
    return _validate_catalog(json.loads(path.read_text()))


def _cache_path() -> Path:
    configured = os.environ.get("USAGE_TRACKER_PRICING_CACHE")
    return Path(configured).expanduser() if configured else Path.home() / ".usage-tracker" / "pricing-catalog.json"


def _entry_key(entry: dict[str, Any]) -> tuple[Any, ...]:
    return (
        entry["provider"],
        entry["basis"],
        entry["model_prefix"],
        entry["effective_from"],
        entry["effective_until"],
    )


def _load_catalog() -> dict[str, Any]:
    global _cached_catalog, _cached_key, _next_cache_check
    cache_path = _cache_path()
    with _lock:
        now = time.monotonic()
        if (
            _cached_catalog is not None
            and _cached_key is not None
            and _cached_key[0] == str(cache_path)
            and now < _next_cache_check
        ):
            return _cached_catalog
        try:
            cache_mtime = cache_path.stat().st_mtime_ns
        except OSError:
            cache_mtime = None
        key = (str(cache_path), cache_mtime)
        if _cached_catalog is not None and _cached_key == key:
            _next_cache_check = now + 30
            return _cached_catalog

        bundled = _read_catalog(BUNDLED_CATALOG_PATH)
        merged = dict(bundled)
        merged["rates"] = list(bundled["rates"])
        merged.update({"source": "bundled", "cache_path": str(cache_path), "cache_error": None})
        if cache_mtime is not None:
            try:
                override = _read_catalog(cache_path)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                merged["cache_error"] = str(exc)
            else:
                entries = {_entry_key(entry): entry for entry in bundled["rates"]}
                entries.update({_entry_key(entry): entry for entry in override["rates"]})
                merged.update({
                    "updated_at": override["updated_at"] or bundled["updated_at"],
                    "credit_value": override["credit_value"],
                    "rates": list(entries.values()),
                    "source": "local_cache",
                })
        _cached_key = key
        _cached_catalog = merged
        _next_cache_check = now + 30
        return merged


def pricing_rate_info(
    provider: str,
    model: str,
    basis: str,
    *,
    at: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the longest-prefix rate active at the event timestamp."""
    when = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    catalog = _load_catalog()
    candidates = [
        entry for entry in catalog["rates"]
        if entry["provider"] == provider
        and entry["basis"] == basis
        and model.startswith(entry["model_prefix"])
    ]
    if not candidates:
        return None
    longest = max(len(entry["model_prefix"]) for entry in candidates)
    prefix_entries = [entry for entry in candidates if len(entry["model_prefix"]) == longest]
    active = [
        entry for entry in prefix_entries
        if (entry["effective_from"] is None or when >= entry["effective_from"])
        and (entry["effective_until"] is None or when <= entry["effective_until"])
    ]
    if not active:
        return None
    selected = max(active, key=lambda entry: entry["effective_from"] or datetime.min.replace(tzinfo=timezone.utc))
    return {
        **selected,
        "effective_from": selected["effective_from"].isoformat() if selected["effective_from"] else None,
        "effective_until": selected["effective_until"].isoformat() if selected["effective_until"] else None,
        "catalog_source": catalog["source"],
    }


def codex_credit_usd_estimate() -> float:
    configured = os.environ.get("USAGE_TRACKER_CODEX_CREDIT_USD")
    if configured is not None:
        try:
            value = float(configured)
            if value >= 0:
                return value
        except ValueError:
            pass
    return float(_load_catalog()["credit_value"]["value"])


def codex_credit_usd_is_estimate() -> bool:
    configured = os.environ.get("USAGE_TRACKER_CODEX_CREDIT_USD")
    if configured is not None:
        verified = os.environ.get("USAGE_TRACKER_CODEX_CREDIT_USD_CHECKOUT_VERIFIED", "")
        return verified.strip().lower() not in {"1", "true", "yes", "on"}
    return not bool(_load_catalog()["credit_value"].get("checkout_verified"))


def pricing_catalog_status() -> dict[str, Any]:
    catalog = _load_catalog()
    credit = dict(catalog["credit_value"])
    credit["value"] = codex_credit_usd_estimate()
    credit["configured_by_env"] = os.environ.get("USAGE_TRACKER_CODEX_CREDIT_USD") is not None
    credit["checkout_verified"] = not codex_credit_usd_is_estimate()
    return {
        "version": catalog["version"],
        "updated_at": catalog["updated_at"].isoformat() if catalog["updated_at"] else None,
        "source": catalog["source"],
        "cache_path": catalog["cache_path"],
        "cache_error": catalog["cache_error"],
        "codex_credit_usd_estimate": credit,
    }


def invalidate_cache() -> None:
    global _cached_catalog, _cached_key, _next_cache_check
    with _lock:
        _cached_catalog = None
        _cached_key = None
        _next_cache_check = 0.0
