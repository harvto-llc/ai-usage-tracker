"""Self-imposed quotas for API-based access.

Subscription access exposes session/weekly quota percentages that the
collector can scrape. API-based access (Bedrock, Vertex, OpenAI enterprise,
or API-key setups) has no such pages, so the quota gauges would stay empty.
This module computes used-percentage against caps the user configures in
~/.usage-tracker/plans.toml:

    [claude.self_quota]
    window_hours = 5              # rolling "session" window
    weekly_days = 7               # rolling "weekly" window
    session_cap_tokens = 44_000_000
    weekly_cap_tokens = 300_000_000
    # or cost-based (uses plan_config.MODEL_COSTS, overridable via
    # [claude.self_quota.pricing."<model-prefix>"]):
    # session_cap_usd = 35.0
    # weekly_cap_usd = 250.0

    # Per-model caps (prefix match against observed model names):
    [claude.self_quota.models."claude-fable"]
    session_cap_tokens = 20_000_000

Usage is measured from the same local session files the scanners read
(Claude JSONL, Codex session JSONL), so it works regardless of which
backend serves the model. Results are cached 5 min.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.plan_config import model_cost_rates, self_quota_config

_CACHE_TTL = 300  # seconds
_cache: dict[str, dict] = {}


def _cost_usd(model: str, inp: int, out: int, cache_read: int, cache_write: int,
              pricing: dict | None, at: datetime | None = None,
              cache_write_1h: int = 0) -> float:
    rates = model_cost_rates(model, pricing, at=at)
    return (
        inp * rates["input"]
        + out * rates["output"]
        + cache_read * rates["cache_read"]
        + cache_write * rates["cache_write"]
        + cache_write_1h * rates.get("cache_write_1h", rates["cache_write"])
    ) / 1_000_000


def _empty_windows() -> dict:
    return {
        "session": {"tokens": 0, "cost_usd": 0.0, "requests": 0, "models": {}},
        "weekly": {"tokens": 0, "cost_usd": 0.0, "requests": 0, "models": {}},
    }


def _add_usage(window: dict, model: str, tokens: int, cost: float, requests: int = 1) -> None:
    window["tokens"] += tokens
    window["cost_usd"] += cost
    window["requests"] += requests
    bucket = window["models"].setdefault(
        model, {"tokens": 0, "cost_usd": 0.0, "requests": 0}
    )
    bucket["tokens"] += tokens
    bucket["cost_usd"] += cost
    bucket["requests"] += requests


def _claude_usage_windows(session_cutoff: datetime, weekly_cutoff: datetime,
                          pricing: dict | None) -> dict:
    """Sum Claude tokens/cost since each cutoff in one pass over the JSONL."""
    from src.scanners import claude_jsonl_files

    oldest = min(session_cutoff, weekly_cutoff)
    totals = _empty_windows()
    seen_usage: set[tuple[str, str]] = set()
    for f in claude_jsonl_files():
        try:
            # Cheap skip: file untouched since the oldest cutoff can't
            # contain entries inside either window.
            if datetime.fromtimestamp(Path(f).stat().st_mtime, tz=timezone.utc) < oldest:
                continue
            with open(f) as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                        ts = e.get("timestamp", "")
                        if not isinstance(ts, str) or len(ts) < 10:
                            continue
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if dt < oldest:
                            continue
                        msg = e.get("message", {})
                        if not isinstance(msg, dict):
                            continue
                        usage = msg.get("usage")
                        if not isinstance(usage, dict):
                            continue
                        usage_id = msg.get("id") or e.get("requestId")
                        if usage_id:
                            usage_key = (str(f), str(usage_id))
                            if usage_key in seen_usage:
                                continue
                            seen_usage.add(usage_key)
                        inp = int(usage.get("input_tokens") or 0)
                        out = int(usage.get("output_tokens") or 0)
                        cr = int(usage.get("cache_read_input_tokens") or 0)
                        cache_creation = usage.get("cache_creation") or {}
                        cw_5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
                        cw_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
                        cw_total = int(usage.get("cache_creation_input_tokens") or 0)
                        if not cw_5m and not cw_1h:
                            cw_5m = cw_total
                        elif cw_total > cw_5m + cw_1h:
                            cw_5m += cw_total - cw_5m - cw_1h
                        cw = cw_5m + cw_1h
                        tokens = inp + out + cr + cw
                        model = msg.get("model", "unknown")
                        cost = _cost_usd(
                            model, inp, out, cr, cw_5m, pricing,
                            at=dt, cache_write_1h=cw_1h,
                        )
                        if dt >= session_cutoff:
                            _add_usage(totals["session"], model, tokens, cost)
                        if dt >= weekly_cutoff:
                            _add_usage(totals["weekly"], model, tokens, cost)
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            continue
    return totals


def _codex_usage_windows(session_cutoff: datetime, weekly_cutoff: datetime,
                         pricing: dict | None,
                         sessions_root: Path | None = None) -> dict:
    """Sum Codex tokens/cost since each cutoff from ~/.codex/sessions JSONL."""
    from src.provider_metrics import CODEX_BOOKKEEPING_TYPES, _parse_event_timestamp

    root = sessions_root or (Path.home() / ".codex" / "sessions")
    oldest = min(session_cutoff, weekly_cutoff)
    totals = _empty_windows()
    if not root.exists():
        return totals

    for session_file in root.glob("*/*/*/*.jsonl"):
        try:
            if datetime.fromtimestamp(session_file.stat().st_mtime, tz=timezone.utc) < oldest:
                continue
            current_model = "unknown"
            with open(session_file) as fh:
                for line in fh:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = entry.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if isinstance(payload.get("model"), str) and payload["model"]:
                        current_model = payload["model"]
                    if payload.get("type") not in CODEX_BOOKKEEPING_TYPES:
                        continue
                    dt = _parse_event_timestamp(entry.get("timestamp"))
                    if dt is None or dt < oldest:
                        continue
                    info = payload.get("info") or {}
                    last_usage = info.get("last_token_usage") or {}
                    if not isinstance(last_usage, dict):
                        continue
                    inp = int(last_usage.get("input_tokens") or 0)
                    out = int(last_usage.get("output_tokens") or 0)
                    cached = int(last_usage.get("cached_input_tokens") or 0)
                    # Codex reports cached_input_tokens as a subset of input_tokens.
                    # Charge cached input once at its discounted rate.
                    uncached = max(inp - cached, 0)
                    tokens = inp + out
                    model = str(info.get("model") or current_model)
                    cost = _cost_usd(model, uncached, out, cached, 0, pricing, at=dt)
                    if dt >= session_cutoff:
                        _add_usage(totals["session"], model, tokens, cost)
                    if dt >= weekly_cutoff:
                        _add_usage(totals["weekly"], model, tokens, cost)
        except OSError:
            continue
    return totals


def _window_pct(usage: dict, caps: dict, window_key: str) -> tuple[float | None, dict]:
    """Percentage used for one window. Precedence: tokens > cost."""
    cap_tokens = caps.get(f"{window_key}_cap_tokens")
    cap_usd = caps.get(f"{window_key}_cap_usd")
    if cap_tokens:
        return round(usage["tokens"] / cap_tokens * 100, 1), {
            "basis": "tokens", "used": usage["tokens"], "cap": cap_tokens,
        }
    if cap_usd:
        return round(usage["cost_usd"] / cap_usd * 100, 1), {
            "basis": "cost", "used_usd": round(usage["cost_usd"], 2), "cap_usd": cap_usd,
        }
    return None, {}


def _prefix_usage(window: dict, prefix: str) -> dict:
    """Aggregate a window's per-model buckets for models matching a prefix."""
    agg = {"tokens": 0, "cost_usd": 0.0, "requests": 0}
    for model, bucket in window["models"].items():
        if model.startswith(prefix):
            agg["tokens"] += bucket["tokens"]
            agg["cost_usd"] += bucket["cost_usd"]
            agg["requests"] += bucket["requests"]
    return agg


def _model_quotas(usage: dict, model_caps: dict) -> dict:
    """Per-model-prefix quota percentages for both windows."""
    result = {}
    for prefix, caps in model_caps.items():
        session_pct, session_detail = _window_pct(
            _prefix_usage(usage["session"], prefix), caps, "session"
        )
        weekly_pct, weekly_detail = _window_pct(
            _prefix_usage(usage["weekly"], prefix), caps, "weekly"
        )
        if session_pct is None and weekly_pct is None:
            continue
        result[prefix] = {
            "session_used_pct": session_pct,
            "weekly_used_pct": weekly_pct,
            "session_detail": session_detail,
            "weekly_detail": weekly_detail,
        }
    return result


def _usage_windows(provider_id: str, cfg: dict, now_dt: datetime) -> dict | None:
    session_cutoff = now_dt - timedelta(hours=cfg["window_hours"])
    weekly_cutoff = now_dt - timedelta(days=cfg["weekly_days"])
    if provider_id == "claude":
        return _claude_usage_windows(session_cutoff, weekly_cutoff, cfg.get("pricing"))
    if provider_id == "codex":
        return _codex_usage_windows(session_cutoff, weekly_cutoff, cfg.get("pricing"))
    return None


def self_quota_snapshot(provider_id: str, now: datetime | None = None) -> dict | None:
    """Quota-shaped snapshot from self-imposed caps, or None if unconfigured.

    Returns session/weekly used+remaining percentages, per-model quota
    percentages (when [.models] caps are set), and measurement detail.
    Percentages may exceed 100 when the cap is blown.
    """
    cfg = self_quota_config(provider_id)
    if not cfg:
        return None

    cached = _cache.get(provider_id)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
        return cached["snapshot"]

    now_dt = now or datetime.now(timezone.utc)
    usage = _usage_windows(provider_id, cfg, now_dt)
    if usage is None:
        return None

    session_pct, session_detail = _window_pct(usage["session"], cfg, "session")
    weekly_pct, weekly_detail = _window_pct(usage["weekly"], cfg, "weekly")

    snapshot = {
        "source": "self_quota",
        "session_used_pct": session_pct,
        "weekly_used_pct": weekly_pct,
        "session_remaining_pct": round(max(0.0, 100 - session_pct), 1) if session_pct is not None else None,
        "weekly_remaining_pct": round(max(0.0, 100 - weekly_pct), 1) if weekly_pct is not None else None,
        # Rolling windows have no fixed reset moment.
        "session_reset": None,
        "weekly_reset": None,
        "window_hours": cfg["window_hours"],
        "weekly_days": cfg["weekly_days"],
        "session_detail": session_detail,
        "weekly_detail": weekly_detail,
        "models": _model_quotas(usage, cfg.get("models") or {}),
    }
    _cache[provider_id] = {"ts": time.time(), "snapshot": snapshot}
    return snapshot


def invalidate_cache() -> None:
    """Force recomputation on next call. Useful for tests."""
    _cache.clear()
