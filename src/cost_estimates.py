"""Estimate the replacement value of observed subscription-quota usage.

Quota percentages are provider-defined and cannot be converted directly to
tokens or dollars. These estimates instead price the local token events that
fall inside each provider-reported quota window.
"""

import hashlib
import json
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.pricing_catalog import (
    codex_credit_usd_estimate,
    pricing_catalog_status,
    pricing_rate_info,
)

_CACHE_TTL = 300
_cache: dict[tuple, tuple[float, object]] = {}
_bundle_cache: dict[str, dict] = {}
_bundle_pending: set[str] = set()
_bundle_loaded = False
_bundle_lock = threading.Lock()
_bundle_jobs: queue.Queue[tuple] = queue.Queue()
_bundle_worker_started = False


def _number(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _reset_datetime(value) -> datetime | None:
    number = _number(value)
    if number is not None:
        if number > 1_000_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _duration_minutes(limit: dict) -> float | None:
    explicit = _number(limit.get("window_minutes"))
    if explicit and explicit > 0:
        return explicit
    return {
        "session": 5 * 60,
        "weekly": 7 * 24 * 60,
        "monthly": 30 * 24 * 60,
    }.get(str(limit.get("window_kind") or "").lower())


def _window_specs(limits: list[dict], now: datetime) -> dict[str, dict]:
    specs = {}
    for limit in limits:
        if not isinstance(limit, dict) or not limit.get("id"):
            continue
        scope = limit.get("scope_kind") or "aggregate"
        if scope not in {"aggregate", "model"}:
            continue
        duration = _duration_minutes(limit)
        if duration is None:
            continue
        reset = _reset_datetime(limit.get("reset_at"))
        end = reset if reset and reset > now - timedelta(minutes=duration) else now
        specs[str(limit["id"])] = {
            "start": end - timedelta(minutes=duration),
            "end": now,
            "reset": reset,
            "rolling_fallback": reset is None,
            "model": str(limit.get("model")) if scope == "model" and limit.get("model") else None,
        }
    return specs


def _empty_usage() -> dict:
    return {
        "tokens": 0,
        "priced_tokens": 0,
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_tokens": 0,
        "reasoning_tokens": 0,
        "messages": 0,
        "user_messages": 0,
        "active_timestamps": [],
        "sessions": set(),
        "models": {},
        "surfaces": {},
        "cost_usd": 0.0,
        "credits": 0.0,
        "unpriced_models": set(),
        "unpriced_reasons": {},
        "pricing_rates": {},
    }


def _surface_usage(bucket: dict, surface: str) -> dict:
    return bucket["surfaces"].setdefault(surface or "unknown", {
        "tokens": 0,
        "priced_tokens": 0,
        "requests": 0,
        "messages": 0,
        "user_messages": 0,
        "sessions": set(),
        "models": {},
        "cost_usd": 0.0,
        "credits": 0.0,
        "unpriced_models": set(),
    })


def price_usage_event(
    provider: str,
    model: str,
    at: datetime,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    reasoning_tokens: int = 0,
    codex_credit_usd: float | None = None,
) -> dict:
    """Price one normalized local token event with the dated provider catalog."""
    total_tokens = (
        input_tokens + output_tokens + cache_read_tokens
        + cache_write_5m_tokens + cache_write_1h_tokens
    )
    cost_usd = None
    credits = None
    if provider == "codex":
        rate_info = pricing_rate_info("codex", model, "credits_per_mtok", at=at)
        rates = rate_info["rates"] if rate_info else None
        if rates:
            credits = (
                input_tokens * rates["input"]
                + cache_read_tokens * rates["cache_read"]
                + output_tokens * rates["output"]
            ) / 1_000_000
            cost_usd = credits * (
                codex_credit_usd
                if codex_credit_usd is not None
                else codex_credit_usd_estimate()
            )
    else:
        rate_info = pricing_rate_info("claude", model, "api_usd_per_mtok", at=at)
        rates = rate_info["rates"] if rate_info else None
        if rates:
            cost_usd = (
                input_tokens * rates["input"]
                + output_tokens * rates["output"]
                + cache_read_tokens * rates["cache_read"]
                + cache_write_5m_tokens * rates["cache_write"]
                + cache_write_1h_tokens
                * rates.get("cache_write_1h", rates["cache_write"])
            ) / 1_000_000

    unpriced_reason = None
    if total_tokens > 0 and cost_usd is None:
        unpriced_reason = (
            rate_info.get("unpriced_reason")
            if rate_info
            else "No published rate for this model and event date."
        )
    return {
        "total_tokens": total_tokens,
        "reasoning_tokens": reasoning_tokens,
        "priced_tokens": total_tokens if cost_usd is not None else 0,
        "estimated_cost_usd": cost_usd,
        "estimated_credits": credits,
        "rate_info": rate_info,
        "unpriced_reason": unpriced_reason,
    }


def _add_event(
    totals: dict[str, dict],
    specs: dict[str, dict],
    *,
    provider: str,
    at: datetime,
    model: str,
    surface: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    reasoning_tokens: int = 0,
    codex_credit_usd: float | None = None,
) -> None:
    priced = price_usage_event(
        provider,
        model,
        at,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_5m_tokens=cache_write_5m_tokens,
        cache_write_1h_tokens=cache_write_1h_tokens,
        reasoning_tokens=reasoning_tokens,
        codex_credit_usd=codex_credit_usd,
    )
    total_tokens = priced["total_tokens"]
    if total_tokens <= 0:
        return
    cost_usd = priced["estimated_cost_usd"]
    credits = priced["estimated_credits"]
    rate_info = priced["rate_info"]

    for key, spec in specs.items():
        if not (spec["start"] <= at <= spec["end"]):
            continue
        if spec["model"] and not model.startswith(spec["model"]):
            continue
        bucket = totals[key]
        bucket["tokens"] += total_tokens
        bucket["requests"] += 1
        bucket["input_tokens"] += input_tokens + cache_read_tokens + cache_write_5m_tokens + cache_write_1h_tokens
        bucket["output_tokens"] += output_tokens
        bucket["cache_tokens"] += cache_read_tokens + cache_write_5m_tokens + cache_write_1h_tokens
        bucket["reasoning_tokens"] += reasoning_tokens
        model_usage = bucket["models"].setdefault(model, {"tokens": 0, "requests": 0})
        model_usage["tokens"] += total_tokens
        model_usage["requests"] += 1
        surface_usage = _surface_usage(bucket, surface)
        surface_usage["tokens"] += total_tokens
        surface_usage["requests"] += 1
        surface_model = surface_usage["models"].setdefault(model, {"tokens": 0, "requests": 0})
        surface_model["tokens"] += total_tokens
        surface_model["requests"] += 1
        if cost_usd is None:
            bucket["unpriced_models"].add(model)
            surface_usage["unpriced_models"].add(model)
            if rate_info and rate_info.get("unpriced_reason"):
                bucket["unpriced_reasons"][model] = rate_info["unpriced_reason"]
            elif priced["unpriced_reason"]:
                bucket["unpriced_reasons"][model] = priced["unpriced_reason"]
            continue
        bucket["priced_tokens"] += total_tokens
        bucket["cost_usd"] += cost_usd
        surface_usage["priced_tokens"] += total_tokens
        surface_usage["cost_usd"] += cost_usd
        if credits is not None:
            bucket["credits"] += credits
            surface_usage["credits"] += credits
        if rate_info:
            rate_key = "|".join(str(rate_info.get(key) or "") for key in (
                "basis", "model_prefix", "effective_from", "effective_until", "source_url"
            ))
            bucket["pricing_rates"][rate_key] = {
                key: rate_info.get(key)
                for key in (
                    "basis", "model_prefix", "effective_from", "effective_until",
                    "source_label", "source_url", "official", "catalog_source",
                )
            }


def _add_activity(
    totals: dict[str, dict],
    specs: dict[str, dict],
    *,
    at: datetime,
    session_id: str,
    surface: str,
    is_message: bool,
    is_user: bool,
) -> None:
    if not is_message:
        return
    for key, spec in specs.items():
        if not (spec["start"] <= at <= spec["end"]):
            continue
        bucket = totals[key]
        bucket["messages"] += 1
        bucket["sessions"].add(session_id)
        surface_usage = _surface_usage(bucket, surface)
        surface_usage["messages"] += 1
        surface_usage["sessions"].add(session_id)
        if is_user:
            bucket["user_messages"] += 1
            surface_usage["user_messages"] += 1
            bucket["active_timestamps"].append(at.astimezone())


def _scan_claude(specs: dict[str, dict], now: datetime) -> dict[str, dict]:
    return _scan_indexed_events("claude", specs, now)


def _scan_codex(specs: dict[str, dict], now: datetime) -> dict[str, dict]:
    return _scan_indexed_events("codex", specs, now)


def _scan_indexed_events(provider: str, specs: dict[str, dict], now: datetime) -> dict[str, dict]:
    from src.usage_ledger import iter_events_between, sync_provider

    totals = {key: _empty_usage() for key in specs}
    if not specs:
        return totals
    oldest = min(spec["start"] for spec in specs.values())
    sync_provider(provider)
    credit_usd = codex_credit_usd_estimate() if provider == "codex" else None
    for event in iter_events_between(provider, oldest, now):
        at = event["at"]
        _add_activity(
            totals,
            specs,
            at=at,
            session_id=event["session_id"],
            surface=event["surface"],
            is_message=bool(event["is_message"]),
            is_user=bool(event["is_user"]),
        )
        _add_event(
            totals,
            specs,
            provider=provider,
            at=at,
            model=event["model"],
            surface=event["surface"],
            input_tokens=event["input_tokens"],
            output_tokens=event["output_tokens"],
            cache_read_tokens=event["cache_read_tokens"],
            cache_write_5m_tokens=event["cache_write_5m_tokens"],
            cache_write_1h_tokens=event["cache_write_1h_tokens"],
            reasoning_tokens=event["reasoning_tokens"],
            codex_credit_usd=credit_usd,
        )
    return totals


def _surface_results(usage: dict, provider: str) -> dict:
    results = {}
    for surface, values in usage["surfaces"].items():
        tokens = values["tokens"]
        priced = values["priced_tokens"]
        has_price = priced > 0 or tokens == 0
        results[surface] = {
            "sessions": len(values["sessions"]),
            "messages": values["messages"],
            "user_messages": values["user_messages"],
            "total_tokens": tokens,
            "requests": values["requests"],
            "models": values["models"],
            "estimated_cost_usd": round(values["cost_usd"], 6) if has_price else None,
            "estimated_credits": (
                round(values["credits"], 3)
                if provider == "codex" and has_price
                else None
            ),
            "pricing_coverage_pct": round(priced / tokens * 100, 1) if tokens else None,
            "unpriced_models": sorted(values["unpriced_models"]),
        }
    return results


def _period_result(usage: dict, spec: dict, provider: str) -> dict:
    from src.scanners import _active_hours_from_timestamps

    tokens = usage["tokens"]
    priced = usage["priced_tokens"]
    has_price = priced > 0 or tokens == 0
    return {
        "active_hours": round(_active_hours_from_timestamps(sorted(usage["active_timestamps"])), 1),
        "messages": usage["messages"],
        "user_messages": usage["user_messages"],
        "sessions": len(usage["sessions"]),
        "conversations": len(usage["sessions"]),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_tokens": usage["cache_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "total_tokens": tokens,
        "requests": usage["requests"],
        "models": usage["models"],
        "surfaces": _surface_results(usage, provider),
        "estimated_cost_usd": round(usage["cost_usd"], 6) if has_price else None,
        "estimated_credits": round(usage["credits"], 3) if provider == "codex" and has_price else None,
        "pricing_coverage_pct": round(priced / tokens * 100, 1) if tokens else None,
        "unpriced_models": sorted(usage["unpriced_models"]),
        "unpriced_reasons": usage["unpriced_reasons"],
        "pricing_rates": list(usage["pricing_rates"].values()),
        "window_start_at": spec["start"].isoformat(),
        "window_end_at": spec["end"].isoformat(),
    }


def usage_bundle(
    provider: str,
    limits: list[dict],
    windows: dict[str, tuple[datetime, datetime]],
    now: datetime | None = None,
) -> dict:
    """Scan a provider once for quota estimates and named activity periods."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    quota_specs = _window_specs(limits, now)
    period_specs = {
        key: {
            "start": start.astimezone(timezone.utc),
            "end": min(end.astimezone(timezone.utc), now),
            "reset": None,
            "rolling_fallback": False,
            "model": None,
        }
        for key, (start, end) in windows.items()
    }
    merged_specs = {
        **{f"quota:{key}": value for key, value in quota_specs.items()},
        **{f"period:{key}": value for key, value in period_specs.items()},
    }
    totals = _scan_codex(merged_specs, now) if provider == "codex" else _scan_claude(merged_specs, now)

    estimates = []
    for key, spec in quota_specs.items():
        usage = totals[f"quota:{key}"]
        tokens = usage["tokens"]
        priced = usage["priced_tokens"]
        has_price = priced > 0 or tokens == 0
        estimates.append({
            "id": key,
            "estimate_basis": "codex_credit_rate_card" if provider == "codex" else "claude_api_rates",
            "estimated_cost_usd": round(usage["cost_usd"], 6) if has_price else None,
            "estimated_credits": round(usage["credits"], 3) if provider == "codex" and has_price else None,
            "observed_tokens": tokens,
            "observed_requests": usage["requests"],
            "pricing_coverage_pct": round(priced / tokens * 100, 1) if tokens else None,
            "unpriced_models": sorted(usage["unpriced_models"]),
            "unpriced_reasons": usage["unpriced_reasons"],
            "pricing_rates": list(usage["pricing_rates"].values()),
            "window_start_at": spec["start"].isoformat(),
            "window_reset_at": spec["reset"].isoformat() if spec["reset"] else None,
            "rolling_window_fallback": spec["rolling_fallback"],
        })
    periods = {
        key: _period_result(totals[f"period:{key}"], spec, provider)
        for key, spec in period_specs.items()
    }
    return {
        "quota_estimates": estimates,
        "periods": periods,
        "pricing_catalog": pricing_catalog_status(),
        "status": "current",
        "updated_at": int(time.time()),
    }


def _bundle_cache_path() -> Path:
    configured = os.environ.get("USAGE_TRACKER_COST_CACHE")
    return Path(configured).expanduser() if configured else Path.home() / ".usage-tracker" / "cost-cache.json"


def _load_bundle_cache() -> None:
    global _bundle_loaded
    if _bundle_loaded or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    _bundle_loaded = True
    try:
        payload = json.loads(_bundle_cache_path().read_text())
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(payload, dict):
        _bundle_cache.update({key: value for key, value in payload.items() if isinstance(value, dict)})


def _write_bundle_cache() -> None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    path = _bundle_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(_bundle_cache, separators=(",", ":")))
        temporary.replace(path)
    except OSError:
        return


def _bundle_signature(
    provider: str,
    limits: list[dict],
    windows: dict[str, tuple[datetime, datetime]],
    now: datetime,
) -> str:
    payload = {
        "provider": provider,
        "refresh_bucket": int(now.timestamp() // _CACHE_TTL),
        "limits": [
            {
                key: limit.get(key)
                for key in ("id", "window_kind", "window_minutes", "scope_kind", "model", "reset", "reset_at")
            }
            for limit in limits
            if isinstance(limit, dict)
        ],
        "windows": {
            key: int(start.timestamp() // _CACHE_TTL)
            for key, (start, _end) in sorted(windows.items())
        },
        "pricing_catalog": pricing_catalog_status(),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _refresh_usage_bundle(
    provider: str,
    signature: str,
    limits: list[dict],
    windows: dict[str, tuple[datetime, datetime]],
    now: datetime,
) -> None:
    try:
        result = usage_bundle(provider, limits, windows, now=now)
        with _bundle_lock:
            _bundle_cache[provider] = {
                "signature": signature,
                "updated_at": int(time.time()),
                **result,
            }
            _write_bundle_cache()
    finally:
        with _bundle_lock:
            _bundle_pending.discard(provider)


def _bundle_worker() -> None:
    while True:
        job = _bundle_jobs.get()
        try:
            try:
                _refresh_usage_bundle(*job)
            except Exception:
                pass
        finally:
            _bundle_jobs.task_done()


def _ensure_bundle_worker() -> None:
    global _bundle_worker_started
    if _bundle_worker_started:
        return
    threading.Thread(target=_bundle_worker, name="usage-cost", daemon=True).start()
    _bundle_worker_started = True


def cached_usage_bundle(
    provider: str,
    limits: list[dict],
    windows: dict[str, tuple[datetime, datetime]],
    now: datetime | None = None,
) -> dict:
    """Return cached usage immediately and refresh it once in the background."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return usage_bundle(provider, limits, windows, now=now)
    signature = _bundle_signature(provider, limits, windows, now)
    with _bundle_lock:
        _load_bundle_cache()
        cached = _bundle_cache.get(provider)
        if (not cached or cached.get("signature") != signature) and provider not in _bundle_pending:
            _bundle_pending.add(provider)
            _ensure_bundle_worker()
            _bundle_jobs.put((provider, signature, list(limits), dict(windows), now))
        if not cached:
            return {
                "quota_estimates": [],
                "periods": {},
                "pricing_catalog": pricing_catalog_status(),
                "status": "refreshing",
                "updated_at": None,
            }
        return {
            "quota_estimates": cached.get("quota_estimates") or [],
            "periods": cached.get("periods") or {},
            "pricing_catalog": cached.get("pricing_catalog") or pricing_catalog_status(),
            "status": "current" if cached.get("signature") == signature else "refreshing",
            "updated_at": cached.get("updated_at"),
        }


def observed_period_metrics(
    provider: str,
    windows: dict[str, tuple[datetime, datetime]],
    now: datetime | None = None,
) -> dict[str, dict]:
    """Return activity, model, token, credit, and value metrics for named windows."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    specs = {
        key: {
            "start": start.astimezone(timezone.utc),
            "end": min(end.astimezone(timezone.utc), now),
            "reset": None,
            "rolling_fallback": False,
            "model": None,
        }
        for key, (start, end) in windows.items()
    }
    cache_key = (
        "periods",
        provider,
        int(now.timestamp() // _CACHE_TTL),
        tuple(sorted((key, spec["start"].isoformat()) for key, spec in specs.items())),
    )
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]  # type: ignore[return-value]

    totals = _scan_codex(specs, now) if provider == "codex" else _scan_claude(specs, now)
    result = {
        key: _period_result(usage, specs[key], provider)
        for key, usage in totals.items()
    }
    _cache[cache_key] = (time.time(), result)
    return result


def provider_usage_trend(
    provider: str,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> dict:
    """Return zero-filled daily token/value points from persisted aggregates."""
    from src.usage_ledger import daily_models, sync_provider

    if provider not in {"claude", "codex"}:
        raise ValueError(f"unsupported provider: {provider}")
    days = max(1, min(int(days), 90))
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end_day = now.date()
    start_day = end_day - timedelta(days=days - 1)
    cache_key = ("trend", provider, int(now.timestamp() // _CACHE_TTL), days)
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]  # type: ignore[return-value]

    sync_provider(provider)
    rows = daily_models(provider, start_day.isoformat(), end_day.isoformat())
    by_day: dict[str, dict] = {}
    recent_models: dict[str, int] = {}
    recent_start = end_day - timedelta(days=6)
    credit_usd = codex_credit_usd_estimate() if provider == "codex" else None

    for row in rows:
        day = str(row["day"])
        model = str(row["model"])
        input_tokens = int(row["input_tokens"])
        output_tokens = int(row["output_tokens"])
        cache_read = int(row["cache_read_tokens"])
        cache_write_5m = int(row["cache_write_5m_tokens"])
        cache_write_1h = int(row["cache_write_1h_tokens"])
        tokens = input_tokens + output_tokens + cache_read + cache_write_5m + cache_write_1h
        bucket = by_day.setdefault(day, {
            "total_tokens": 0, "priced_tokens": 0, "requests": 0,
            "cost_usd": 0.0, "credits": 0.0,
        })
        bucket["total_tokens"] += tokens
        bucket["requests"] += int(row["requests"])
        at = datetime.fromisoformat(day).replace(hour=12, tzinfo=timezone.utc)
        basis = "credits_per_mtok" if provider == "codex" else "api_usd_per_mtok"
        info = pricing_rate_info(provider, model, basis, at=at)
        rates = info["rates"] if info else None
        if rates and tokens:
            if provider == "codex":
                credits = (
                    input_tokens * rates["input"]
                    + cache_read * rates["cache_read"]
                    + output_tokens * rates["output"]
                ) / 1_000_000
                bucket["credits"] += credits
                bucket["cost_usd"] += credits * (credit_usd or 0)
            else:
                bucket["cost_usd"] += (
                    input_tokens * rates["input"]
                    + output_tokens * rates["output"]
                    + cache_read * rates["cache_read"]
                    + cache_write_5m * rates["cache_write"]
                    + cache_write_1h * rates.get("cache_write_1h", rates["cache_write"])
                ) / 1_000_000
            bucket["priced_tokens"] += tokens
        if datetime.fromisoformat(day).date() >= recent_start:
            recent_models[model] = recent_models.get(model, 0) + tokens

    points = []
    for offset in range(days):
        day = (start_day + timedelta(days=offset)).isoformat()
        bucket = by_day.get(day) or {
            "total_tokens": 0, "priced_tokens": 0, "requests": 0,
            "cost_usd": 0.0, "credits": 0.0,
        }
        tokens = bucket["total_tokens"]
        points.append({
            "date": day,
            "total_tokens": tokens,
            "requests": bucket["requests"],
            "estimated_cost_usd": round(bucket["cost_usd"], 6) if bucket["priced_tokens"] or not tokens else None,
            "estimated_credits": round(bucket["credits"], 3) if provider == "codex" and (bucket["priced_tokens"] or not tokens) else None,
            "pricing_coverage_pct": round(bucket["priced_tokens"] / tokens * 100, 1) if tokens else None,
        })

    current_7d = sum(point["total_tokens"] for point in points[-7:])
    previous_7d = sum(point["total_tokens"] for point in points[-14:-7])
    change_pct = round((current_7d - previous_7d) / previous_7d * 100, 1) if previous_7d else None
    top_model = max(recent_models, key=recent_models.get) if recent_models else None
    top_tokens = recent_models.get(top_model, 0) if top_model else 0
    top_share = round(top_tokens / current_7d * 100, 1) if current_7d else None
    top_label = top_model
    if top_model:
        from src.plan_config import MODEL_TIERS
        tier_key = max(
            (prefix for prefix in MODEL_TIERS if top_model.startswith(prefix)),
            key=len,
            default=None,
        )
        if tier_key:
            top_label = str(MODEL_TIERS[tier_key]["label"])
    if top_model and top_share is not None:
        direction = ""
        if change_pct is not None and abs(change_pct) >= 5:
            direction = f"; seven-day volume is {abs(change_pct):.0f}% {'higher' if change_pct > 0 else 'lower'} than the prior week"
        advisor = f"{top_label} drove {top_share:.0f}% of seven-day tokens{direction}."
    else:
        advisor = "No token activity recorded in the last seven days."

    result = {
        "days": days,
        "points": points,
        "last_7d_tokens": current_7d,
        "previous_7d_tokens": previous_7d,
        "change_pct": change_pct,
        "top_model_7d": {
            "model": top_model,
            "label": top_label,
            "tokens": top_tokens,
            "share_pct": top_share,
        } if top_model else None,
        "advisor": advisor,
    }
    _cache[cache_key] = (time.time(), result)
    return result


def quota_cost_estimates(provider: str, limits: list[dict], now: datetime | None = None) -> list[dict]:
    """Return token-derived cost details keyed to dynamic quota bucket IDs."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    specs = _window_specs(limits, now)
    cache_key = (
        provider,
        int(now.timestamp() // _CACHE_TTL),
        tuple(sorted((key, spec["start"].isoformat(), spec["model"]) for key, spec in specs.items())),
    )
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]
    for old_key, (cached_at, _) in list(_cache.items()):
        if time.time() - cached_at >= _CACHE_TTL * 2:
            _cache.pop(old_key, None)

    totals = _scan_codex(specs, now) if provider == "codex" else _scan_claude(specs, now)
    result = []
    for key, usage in totals.items():
        tokens = usage["tokens"]
        priced = usage["priced_tokens"]
        coverage = round(priced / tokens * 100, 1) if tokens else None
        has_price = priced > 0 or tokens == 0
        estimate = {
            "id": key,
            "estimate_basis": "codex_credit_rate_card" if provider == "codex" else "claude_api_rates",
            "estimated_cost_usd": round(usage["cost_usd"], 6) if has_price else None,
            "estimated_credits": round(usage["credits"], 3) if provider == "codex" and has_price else None,
            "observed_tokens": tokens,
            "observed_requests": usage["requests"],
            "pricing_coverage_pct": coverage,
            "unpriced_models": sorted(usage["unpriced_models"]),
            "unpriced_reasons": usage["unpriced_reasons"],
            "pricing_rates": list(usage["pricing_rates"].values()),
            "window_start_at": specs[key]["start"].isoformat(),
            "window_reset_at": specs[key]["reset"].isoformat() if specs[key]["reset"] else None,
            "rolling_window_fallback": specs[key]["rolling_fallback"],
        }
        result.append(estimate)
    _cache[cache_key] = (time.time(), result)
    return result


def attach_quota_cost_estimates(provider: str, quota: dict, now: datetime | None = None) -> None:
    """Attach observed-token estimates alongside dynamic quota rows in place."""
    limits = quota.get("limits")
    if not isinstance(limits, list) or not limits:
        quota["cost_estimates"] = []
        return
    estimates = quota_cost_estimates(provider, limits, now=now)
    quota["cost_estimates"] = estimates


def invalidate_cache() -> None:
    _cache.clear()
    with _bundle_lock:
        _bundle_cache.clear()
