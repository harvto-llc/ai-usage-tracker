"""Minimal usage-tracker API for the UsageMenuBar Swift app.

Endpoints:
    GET  /stats          — menubar payload (15s cache)
    GET  /budget/weekly  — weekly forecast per provider
    POST /cc/report      — collector ingest
    POST /sentinel/report — cookie refresh from the menubar sentinel
    GET  /health         — liveness probe
"""

import hmac
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

API_SECRET = os.environ.get("USAGE_TRACKER_SECRET")
if not API_SECRET:
    raise RuntimeError(
        "USAGE_TRACKER_SECRET must be set. "
        "Generate one with: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\""
    )


def verify_auth(authorization: str = Header(None)) -> None:
    """Dependency that enforces bearer-token auth on protected endpoints."""
    if not hmac.compare_digest(authorization or "", f"Bearer {API_SECRET}"):
        raise HTTPException(status_code=401, detail="Unauthorized")


from src.database import (
    cc_messages_today,
    cc_token_usage_today_cached,
    codex_last_completed_cycles,
    codex_local_stats,
    codex_reset_credit_balance_changes,
    init,
    insert,
    insert_codex,
    insert_provider_metric_samples,
    latest_codex,
    latest_provider_metric_samples,
    latest_sample,
    plan_history,
    load_claude_code_stats,
    prune_provider_metric_samples,
    sync,
)
from src.entitlements import resolve_plan
from src.cost_estimates import cached_usage_bundle, provider_usage_trend
from src.credential_store import (
    keychain_enabled,
    load_provider_credential,
    store_provider_credential,
)
from src.metrics import (
    cache_health,
    codex_burn_rate,
    current_streak,
    output_density,
    predict_lock,
    session_burn_rate,
    weekly_forecast,
    weekly_utilization_pace,
    workload_label,
)
from src.plan_config import load_plans
from src.pricing_catalog import (
    codex_credit_usd_estimate,
    codex_credit_usd_is_estimate,
    pricing_catalog_status,
)
from src.provider_metrics import (
    CODEX_LEGACY_LOCAL_ALIAS_MAP,
    CODEX_LEGACY_SESSION_ALIAS_MAP,
    build_provider_snapshots,
    empty_claude_code_stats,
    empty_codex_session_stats,
    empty_codex_thread_stats,
    provider_registry,
    scan_codex_session_metrics,
    shape_claude_code_stats,
)
from src.scanners import scan_cc_daily_model_tokens
from src import (
    focus,
    invoice_ingest,
    repository_discovery,
    session_evidence,
    session_runtime,
    session_search_index,
    usage_explanation,
    work_ledger,
    work_refresh,
    work_reporting,
)

# ── Helpers ──────────────────────────────────────────────


def _first_present(raw: dict, *keys: str):
    for key in keys:
        if key in raw:
            return raw.get(key)
    return None


CLAUDE_SESSION_RESET_MAX_HOURS = 6


def _relative_hours_left(reset_str: str | None) -> float | None:
    if not reset_str:
        return None
    h = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b", reset_str, re.IGNORECASE)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)\b", reset_str, re.IGNORECASE)
    if h or m:
        hours = float(h.group(1)) if h else 0.0
        minutes = float(m.group(1)) if m else 0.0
        return round(hours + minutes / 60, 1)
    return None


def parse_hours_left(reset_str: str | None) -> float | None:
    """Parse a reset string into hours remaining for generic quota windows."""
    relative = _relative_hours_left(reset_str)
    if relative is not None:
        return relative
    if not reset_str:
        return None

    cleaned = reset_str.strip()
    now = datetime.now()
    for fmt in ["%I%p", "%I:%M%p", "%I:%M %p", "%I %p"]:
        try:
            t = datetime.strptime(cleaned.lower(), fmt)
        except ValueError:
            continue
        t = t.replace(year=now.year, month=now.month, day=now.day)
        if t <= now:
            t += timedelta(days=1)
        return round((t - now).total_seconds() / 3600, 1)

    for fmt in [
        "%b %d %I:%M %p",
        "%b %d %I:%M%p",
        "%b %d at %I:%M%p",
        "%b %d at %I%p",
        "%b %d at %I:%M %p",
        "%b %d, %Y %I:%M %p",
    ]:
        for candidate in (cleaned, cleaned.lower()):
            try:
                t = datetime.strptime(candidate, fmt)
            except ValueError:
                continue
            if t.year == 1900:
                t = t.replace(year=now.year)
            delta = (t - now).total_seconds() / 3600
            if delta > 0:
                return round(delta, 1)
    return None


def _claude_session_reset_datetime(reset_str: str, now: datetime) -> datetime | None:
    cleaned = reset_str.strip()
    time_only_fmts = ["%I%p", "%I:%M%p", "%I:%M %p", "%I %p"]
    absolute_fmts = [
        "%b %d %I:%M %p",
        "%b %d %I:%M%p",
        "%b %d at %I:%M%p",
        "%b %d at %I%p",
        "%b %d at %I:%M %p",
        "%b %d, %Y %I:%M %p",
    ]

    for fmt in absolute_fmts:
        for candidate in (cleaned, cleaned.lower()):
            try:
                t = datetime.strptime(candidate, fmt)
            except ValueError:
                continue
            if t.year == 1900:
                t = t.replace(year=now.year)
            delta = (t - now).total_seconds() / 3600
            if 0 < delta <= CLAUDE_SESSION_RESET_MAX_HOURS:
                return t
            return None

    for fmt in time_only_fmts:
        try:
            t = datetime.strptime(cleaned.lower(), fmt)
        except ValueError:
            continue
        t = t.replace(year=now.year, month=now.month, day=now.day)
        if t <= now:
            t += timedelta(days=1)
        delta = (t - now).total_seconds() / 3600
        if 0 < delta <= CLAUDE_SESSION_RESET_MAX_HOURS:
            return t
        return None
    return None


def parse_claude_session_hours_left(reset_str: str | None, now: datetime | None = None) -> float | None:
    """Parse Claude's rolling five-hour session reset into hours remaining."""
    relative = _relative_hours_left(reset_str)
    if relative is not None:
        return relative
    if not reset_str:
        return None

    now = now or datetime.now()
    reset_at = _claude_session_reset_datetime(reset_str, now)
    if reset_at is None:
        return None
    return round((reset_at - now).total_seconds() / 3600, 1)


def normalize_claude_session_reset(reset_str: str | None, now: datetime | None = None) -> str | None:
    """Normalize Claude's rolling five-hour reset without inventing tomorrow."""
    if not reset_str:
        return reset_str
    cleaned = reset_str.strip()
    if _relative_hours_left(cleaned) is not None:
        return cleaned

    now = now or datetime.now()
    reset_at = _claude_session_reset_datetime(cleaned, now)
    if reset_at is None:
        return None
    return reset_at.strftime("%b %-d %-I:%M %p")


def normalize_reset(s: str | None) -> str | None:
    """Normalize reset strings to consistent 'Apr 6 1:00 AM' format."""
    if not s:
        return s
    s = s.strip()
    now = datetime.now()
    for fmt in ["%b %d %I:%M %p", "%b %d at %I:%M%p", "%b %d at %I%p", "%b %d at %I:%M %p"]:
        try:
            t = datetime.strptime(s, fmt).replace(year=now.year)
            return t.strftime("%b %-d %-I:%M %p")
        except ValueError:
            continue
    for fmt in ["%I%p", "%I:%M%p", "%I:%M %p", "%I %p"]:
        try:
            t = datetime.strptime(s.lower(), fmt)
            t = t.replace(year=now.year, month=now.month, day=now.day)
            if t < now:
                t += timedelta(days=1)
            return t.strftime("%b %-d %-I:%M %p")
        except ValueError:
            continue
    return s


def compute_risk_outlook(burn: float, session_pct: float, pace: dict, codex: dict) -> str:
    """One sentence summarizing trajectory across all tools."""
    risks = []
    if burn > 0 and session_pct < 100:
        remaining = 100 - session_pct
        hours_left = remaining / burn
        if hours_left < 2:
            risks.append(f"Claude session: ~{hours_left:.0f}h at current pace")
    if pace.get("pace_status") == "front_loaded":
        risks.append(f"Claude front-loaded ({pace['projected_pct']:.0f}% projected)")
    cdx_burn = codex_burn_rate()
    if cdx_burn > 0 and codex:
        cdx_sess = codex.get("session_remaining_pct")
        if cdx_sess is not None and cdx_sess < 100:
            cdx_hours = cdx_sess / cdx_burn
            if cdx_hours < 2:
                risks.append(f"Codex session: ~{cdx_hours:.0f}h at current pace")
    if risks:
        return "⚠ " + " · ".join(risks)
    days_left = pace.get("days_remaining")
    pace_pct = pace.get("projected_pct", 0)
    if days_left:
        return f"Claude week: {pace_pct:.0f}% projected · {days_left:.1f}d left"
    return "All clear"


def _empty_provider_snapshot(provider_id: str) -> dict:
    return {
        "provider": provider_id,
        "timestamp": None,
        "status": "stale",
        "shared": {
            "primary_used_pct": None,
            "primary_remaining_pct": None,
            "primary_reset": None,
            "secondary_used_pct": None,
            "secondary_remaining_pct": None,
            "secondary_reset": None,
            "tokens_total_day": None,
            "messages_total_day": None,
            "active_hours_day": None,
        },
        "unique": {},
        "source": {},
        "error_text": None,
    }


def _providers_latest_payload() -> dict[str, dict]:
    latest = latest_provider_metric_samples()
    payload: dict[str, dict] = {}
    for item in provider_registry():
        provider_id = str(item["id"])
        payload[provider_id] = latest.get(provider_id) or _empty_provider_snapshot(provider_id)
    return payload


def _normalize_claude_code_stats_payload(raw: dict | None) -> dict:
    if not raw:
        return empty_claude_code_stats()
    if "dailyActivity" in raw or "totalSessions" in raw:
        return shape_claude_code_stats(raw)
    shaped = empty_claude_code_stats()
    shaped.update(raw)
    return shaped


def _model_display_name(model: str) -> str:
    return model.replace("claude-", "").replace("-", " ").title()


def _with_recent_claude_daily_tokens(stats: dict) -> dict:
    recent = scan_cc_daily_model_tokens(days=30)
    if not recent:
        return stats

    merged: dict[str, dict] = {}
    for entry in stats.get("daily_tokens") or []:
        if not isinstance(entry, dict) or not entry.get("date"):
            continue
        merged[entry["date"]] = {
            "date": entry["date"],
            "tokens_by_model": dict(entry.get("tokens_by_model") or {}),
        }

    for entry in recent:
        merged[entry["date"]] = entry

    updated = dict(stats)
    updated["daily_tokens"] = [merged[day] for day in sorted(merged)]

    model_totals: dict[str, int] = {}
    for entry in updated["daily_tokens"]:
        for model, tokens in (entry.get("tokens_by_model") or {}).items():
            model_totals[model] = model_totals.get(model, 0) + int(tokens or 0)
    if model_totals:
        updated["total_tokens_by_model"] = model_totals
        favorite = max(model_totals, key=model_totals.get)
        updated["favorite_model"] = _model_display_name(favorite)
        updated["models_used"] = sorted(model_totals)

    return updated


def _normalize_codex_local_payload(raw: dict | None) -> dict:
    normalized = empty_codex_thread_stats()
    if not raw:
        return normalized

    # Compatibility-only aliases are accepted only at this boundary.
    normalized["total_tokens"] = raw.get("total_tokens")
    for canonical_field, aliases in CODEX_LEGACY_LOCAL_ALIAS_MAP.items():
        normalized[canonical_field] = _first_present(raw, canonical_field, *aliases)
    normalized["today_tokens"] = raw.get("today_tokens")
    normalized["recent_threads"] = raw.get("recent_threads", []) or []

    by_model: dict[str, dict] = {}
    for model, info in (raw.get("by_model") or {}).items():
        if not isinstance(info, dict):
            continue
        by_model[model] = {
            "tokens": info.get("tokens"),
            "threads": info.get("threads", info.get("sessions")),
        }
    normalized["by_model"] = by_model

    today_by_model: dict[str, dict] = {}
    for model, info in (raw.get("today_by_model") or {}).items():
        if not isinstance(info, dict):
            continue
        today_by_model[model] = {
            "tokens": info.get("tokens"),
            "threads": info.get("threads", info.get("sessions")),
        }
    normalized["today_by_model"] = today_by_model
    if raw.get("by_source"):
        normalized["by_source"] = raw["by_source"]
    return normalized


def _normalize_codex_session_payload(raw: dict | None) -> dict:
    normalized = empty_codex_session_stats()
    if not raw:
        return normalized

    # Compatibility-only aliases are accepted only at this boundary.
    for canonical_field, aliases in CODEX_LEGACY_SESSION_ALIAS_MAP.items():
        normalized[canonical_field] = _first_present(raw, canonical_field, *aliases)
    normalized["total_sessions"] = raw.get("total_sessions")
    normalized["events_scanned"] = raw.get("events_scanned")
    return normalized


def _normalize_codex_quota_payload(raw: dict | None) -> dict:
    if not raw:
        return {
            "timestamp": None,
            "session_used_pct": None,
            "weekly_used_pct": None,
            "code_review_used_pct": None,
            "weekly_gpt54_used_pct": None,
            "weekly_spark_used_pct": None,
            "session_remaining_pct": None,
            "weekly_remaining_pct": None,
            "code_review_remaining_pct": None,
            "session_reset": None,
            "weekly_reset": None,
            "account_used_pct": None,
            "account_remaining_pct": None,
            "account_reset": None,
            "account_window_minutes": None,
            "plan_id": None,
            "plan_label": None,
            "plan_source": None,
            "raw_plan": None,
            "plan_entitlements": {},
            "rate_limit_reset_credits": None,
            "rate_limit_reset_credits_used_day": 0,
            "rate_limit_reset_credits_used_week": 0,
            "rate_limit_reset_credits_granted_day": 0,
            "rate_limit_reset_credits_granted_week": 0,
            "credits_remaining": None,
            "limits": [],
            "credit_pools": [],
        }

    session_remaining_pct = raw.get("session_remaining_pct")
    weekly_remaining_pct = raw.get("weekly_remaining_pct")
    review_remaining_pct = raw.get("code_review_remaining_pct")
    gpt54_remaining = raw.get("weekly_gpt54_remaining_pct")
    spark_remaining = raw.get("weekly_spark_remaining_pct")
    limits = raw.get("limits") if isinstance(raw.get("limits"), list) else []
    account_limit = next((
        item for item in limits
        if isinstance(item, dict)
        and item.get("window_kind") in {"monthly", "account"}
        and item.get("scope_kind") == "aggregate"
    ), {})
    account_remaining = raw.get("account_remaining_pct")
    if account_remaining is None:
        account_remaining = account_limit.get("remaining_pct")
    plan = resolve_plan("codex", raw.get("raw_plan") or raw.get("plan_id"), plans=load_plans())
    return {
        "timestamp": raw.get("timestamp"),
        "session_used_pct": 100 - session_remaining_pct if session_remaining_pct is not None else None,
        "weekly_used_pct": 100 - weekly_remaining_pct if weekly_remaining_pct is not None else None,
        "code_review_used_pct": 100 - review_remaining_pct if review_remaining_pct is not None else None,
        "weekly_gpt54_used_pct": 100 - gpt54_remaining if gpt54_remaining is not None else None,
        "weekly_spark_used_pct": 100 - spark_remaining if spark_remaining is not None else None,
        "session_remaining_pct": session_remaining_pct,
        "weekly_remaining_pct": weekly_remaining_pct,
        "code_review_remaining_pct": review_remaining_pct,
        "session_reset": raw.get("session_reset"),
        "weekly_reset": raw.get("weekly_reset") or raw.get("reset_at"),
        "account_used_pct": 100 - account_remaining if account_remaining is not None else None,
        "account_remaining_pct": account_remaining,
        "account_reset": raw.get("account_reset") or account_limit.get("reset"),
        "account_window_minutes": raw.get("account_window_minutes") or account_limit.get("window_minutes"),
        "plan_id": plan.get("plan_id") or raw.get("plan_id"),
        "plan_label": plan.get("plan_label"),
        "plan_source": raw.get("plan_source") or plan.get("plan_source"),
        "raw_plan": raw.get("raw_plan"),
        "plan_entitlements": plan.get("entitlements") or {},
        "rate_limit_reset_credits": raw.get("rate_limit_reset_credits"),
        "credits_remaining": raw.get("credits_remaining"),
        "limits": limits,
        "credit_pools": raw.get("credit_pools") if isinstance(raw.get("credit_pools"), list) else [],
    }


def _claude_today_contract(cc_today: dict, cc_tokens: dict) -> dict:
    # Per-model breakdown for today: tokens = input + output + cache
    # (consistent with input_tokens_today, which folds cache in).
    models_today: dict[str, dict] = {}
    for model, info in (cc_tokens.get("by_model") or {}).items():
        if not isinstance(info, dict):
            continue
        models_today[model] = {
            "tokens": (info.get("input") or 0)
            + (info.get("output") or 0)
            + (info.get("cache_read") or 0)
            + (info.get("cache_create") or 0),
            "requests": info.get("requests"),
        }
    return {
        "active_hours_today": cc_today.get("active_hours"),
        "messages_today": cc_today.get("total_messages"),
        "output_tokens_today": cc_tokens.get("output_tokens"),
        "input_tokens_today": (cc_tokens.get("input_tokens") or 0) + (cc_tokens.get("cache_read_tokens") or 0) + (cc_tokens.get("cache_create_tokens") or 0),
        "threads_today": None,
        "sessions_today": None,
        "conversations_today": cc_today.get("conversations"),
        "models_today": models_today,
    }


def _claude_totals_contract(cc_stats_payload: dict) -> dict:
    return {
        "total_threads": None,
        "total_sessions": cc_stats_payload.get("total_sessions"),
        "total_messages": cc_stats_payload.get("total_messages"),
        "favorite_model": cc_stats_payload.get("favorite_model"),
    }


def _codex_today_contract(codex_local_payload: dict, codex_session_payload: dict) -> dict:
    models_today: dict[str, dict] = {}
    for model, info in (codex_local_payload.get("today_by_model") or {}).items():
        if not isinstance(info, dict):
            continue
        models_today[model] = {
            "tokens": info.get("tokens"),
            "requests": info.get("threads"),
        }
    return {
        "active_hours_today": codex_session_payload.get("active_hours_today"),
        "messages_today": codex_session_payload.get("messages_today"),
        "output_tokens_today": codex_session_payload.get("output_tokens_today"),
        "input_tokens_today": codex_session_payload.get("input_tokens_today"),
        "threads_today": codex_local_payload.get("today_threads"),
        "sessions_today": codex_session_payload.get("sessions_today"),
        "user_messages_today": codex_session_payload.get("user_messages_today"),
        "reasoning_tokens_today": codex_session_payload.get("reasoning_tokens_today"),
        "models_today": models_today,
    }


def _today_period_contract(today: dict) -> dict:
    input_tokens = int(today.get("input_tokens_today") or 0)
    output_tokens = int(today.get("output_tokens_today") or 0)
    return {
        "active_hours": today.get("active_hours_today"),
        "messages": today.get("messages_today"),
        "user_messages": today.get("user_messages_today"),
        "sessions": today.get("sessions_today"),
        "conversations": today.get("conversations_today"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_tokens": None,
        "reasoning_tokens": today.get("reasoning_tokens_today"),
        "total_tokens": input_tokens + output_tokens,
        "requests": None,
        "models": today.get("models_today") or {},
        "estimated_cost_usd": None,
        "estimated_credits": None,
        "pricing_coverage_pct": None,
        "unpriced_models": [],
    }


def _provider_period_windows(quota: dict, now: datetime) -> dict[str, tuple[datetime, datetime]]:
    local_now = now.astimezone()
    day_start = datetime(local_now.year, local_now.month, local_now.day).astimezone(timezone.utc)
    week_start = now - timedelta(days=7)

    aggregate_weekly_ids = {
        str(limit.get("id"))
        for limit in quota.get("limits") or []
        if isinstance(limit, dict)
        and (limit.get("scope_kind") or "aggregate") == "aggregate"
        and limit.get("window_kind") == "weekly"
        and limit.get("id")
    }
    for estimate in quota.get("cost_estimates") or []:
        if not isinstance(estimate, dict) or str(estimate.get("id")) not in aggregate_weekly_ids:
            continue
        raw_start = estimate.get("window_start_at")
        if not isinstance(raw_start, str):
            continue
        try:
            candidate = datetime.fromisoformat(raw_start.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            continue
        if candidate < now:
            week_start = candidate
            break

    return {
        "day": (day_start, now),
        "week": (week_start, now),
    }


def _codex_totals_contract(codex_local_payload: dict, codex_session_payload: dict) -> dict:
    return {
        "total_threads": codex_local_payload.get("total_threads"),
        "total_sessions": codex_session_payload.get("total_sessions"),
        "total_tokens": codex_local_payload.get("total_tokens"),
    }


# ── App ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    _migrate_managed_provider_credentials()
    init()
    scheduler_enabled = os.environ.get(
        "USAGE_TRACKER_WORK_REFRESH_SCHEDULER",
        "1",
    ).lower() not in {"0", "false", "no"}
    if scheduler_enabled:
        work_refresh.start_refresh_scheduler()
    try:
        yield
    finally:
        if scheduler_enabled:
            work_refresh.stop_refresh_scheduler()

app = FastAPI(
    title="Usage Tracker (minimal)",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


# ── Sentinel cookie refresh ──────────────────────────────

class SentinelReport(BaseModel):
    cookies: dict[str, str]


_SENTINEL_ALLOWED_PROVIDERS = {"claude", "codex", "cursor"}

_SENTINEL_ENV_VAR_MAP: dict[str, str] = {
    "claude": "CLAUDE_WEB_COOKIE_FILE",
    "codex": "CODEX_WEB_COOKIE_FILE",
    "cursor": "CURSOR_WEB_COOKIE_FILE",
}

_SENTINEL_REQUIRED_COOKIES: dict[str, tuple[str, ...]] = {
    "claude": ("sessionKey", "lastActiveOrg"),
    "codex": ("__Secure-next-auth.session-token",),
    "cursor": ("WorkosCursorSessionToken",),
}


def _sentinel_cookie_names(cookie_header: str) -> set[str]:
    names = set()
    for part in cookie_header.split(";"):
        name, separator, _ = part.strip().partition("=")
        if separator and name:
            names.add(name)
    return names


def _sentinel_cookie_valid(provider: str, cookie_header: str) -> bool:
    required = _SENTINEL_REQUIRED_COOKIES.get(provider)
    if not required:
        return False
    names = _sentinel_cookie_names(cookie_header)
    return all(name in names or any(item.startswith(f"{name}.") for item in names) for name in required)


def _sentinel_env_file() -> Path:
    override = os.environ.get("USAGE_TRACKER_ENV_FILE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / ".env"


def _replace_env_path(env_content: str, env_var: str, cookie_file: Path | None) -> str:
    prefix = f"export {env_var}="
    lines = [line for line in env_content.splitlines() if not line.strip().startswith(prefix)]
    if cookie_file is not None:
        lines.append(f'export {env_var}="{cookie_file}"')
    return "\n".join(lines).strip() + "\n"


def _migrate_managed_provider_credentials() -> dict[str, str]:
    if not keychain_enabled():
        return {}
    base_dir = Path.home() / ".usage-tracker"
    env_file = _sentinel_env_file()
    env_content = env_file.read_text() if env_file.exists() else ""
    changed_env = False
    migrated: dict[str, str] = {}

    for provider in ("claude", "codex"):
        cookie_file = base_dir / f"{provider}-cookie.txt"
        stored = load_provider_credential(provider)
        if stored is None and cookie_file.exists():
            try:
                legacy_value = cookie_file.read_text().strip()
            except OSError:
                legacy_value = ""
            if legacy_value and store_provider_credential(provider, legacy_value):
                stored = legacy_value
        if stored is None:
            continue

        cookie_file.unlink(missing_ok=True)
        env_var = _SENTINEL_ENV_VAR_MAP[provider]
        updated = _replace_env_path(env_content, env_var, None)
        changed_env = changed_env or updated != env_content
        env_content = updated
        migrated[provider] = "keychain"

    if changed_env and env_file.exists():
        env_file.write_text(env_content)
    return migrated


def _update_env_with_cookies(reports: dict[str, str]) -> dict[str, str]:
    base_dir = Path.home() / ".usage-tracker"
    base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    env_file = _sentinel_env_file()

    env_content = ""
    if env_file.exists():
        env_content = env_file.read_text()

    storage: dict[str, str] = {}
    for provider, cookie_val in reports.items():
        if provider not in _SENTINEL_ALLOWED_PROVIDERS or not _sentinel_cookie_valid(provider, cookie_val):
            continue
        env_var = _SENTINEL_ENV_VAR_MAP[provider]
        cookie_file = base_dir / f"{provider}-cookie.txt"

        if provider in {"claude", "codex"} and store_provider_credential(provider, cookie_val):
            cookie_file.unlink(missing_ok=True)
            env_content = _replace_env_path(env_content, env_var, None)
            storage[provider] = "keychain"
            continue

        cookie_file.write_text(cookie_val)
        cookie_file.chmod(0o600)
        env_content = _replace_env_path(env_content, env_var, cookie_file)
        storage[provider] = "file"

    env_file.write_text(env_content)
    return storage


@app.post("/sentinel/report")
async def sentinel_report(report: SentinelReport, _ = Depends(verify_auth)):
    storage = _update_env_with_cookies(report.cookies)
    sync()
    rejected = sorted(
        provider
        for provider, cookie in report.cookies.items()
        if provider in _SENTINEL_ALLOWED_PROVIDERS and not _sentinel_cookie_valid(provider, cookie)
    )
    return {"status": "ok", "received": list(storage), "rejected": rejected, "storage": storage}


# ── CC stats cache (pushed by collector.py) ──────────────

_remote_cc: dict = {
    "messages": None,
    "tokens": None,
    "projects": None,
    "codex_local": None,
    "codex_sessions": None,
    "cc_stats": None,
    "usage": None,
    "codex_usage": None,
    "codex_analytics": None,
    "cursor_usage": None,
    "provider_snapshots": None,
    "ts": 0,
}
_REMOTE_CC_TTL = 120  # consider stale after 2 min


class CCReport(BaseModel):
    messages: dict
    tokens: dict
    projects: list
    codex_local: dict | None = None
    cc_stats: dict | None = None
    usage: dict | None = None
    codex_usage: dict | None = None
    codex_analytics: dict | None = None
    codex_sessions: dict | None = None
    cursor_usage: dict | None = None
    provider_snapshots: dict[str, dict] | None = None


def _remote_cc_fresh() -> bool:
    return _remote_cc["ts"] > 0 and (time.time() - _remote_cc["ts"]) < _REMOTE_CC_TTL


def _feed_health(
    *,
    available: bool,
    source: str,
    timestamp: int | float | None,
    now: float,
    stale_after_seconds: int | None = None,
    unavailable_detail: str | None = None,
    stale_recovery: str | None = None,
) -> dict:
    observed_at = int(timestamp) if timestamp else None
    age_seconds = max(0, int(now - observed_at)) if observed_at else None
    if not available:
        status = "unavailable"
        detail = unavailable_detail
        recovery = None
    elif stale_after_seconds is not None and age_seconds is not None and age_seconds > stale_after_seconds:
        status = "stale"
        detail = f"Last successful refresh was {age_seconds // 60}m ago"
        recovery = stale_recovery
    else:
        status = "current"
        detail = None
        recovery = None
    return {
        "status": status,
        "source": source,
        "timestamp": observed_at,
        "age_seconds": age_seconds,
        "detail": detail,
        "recovery": recovery,
    }


def _provider_feed_health(
    *,
    now: float,
    remote_fresh: bool,
    claude_sample: dict,
    claude_quota: dict,
    codex_quota: dict,
    provider_periods: dict,
    codex_analytics: dict | None,
) -> dict[str, dict[str, dict]]:
    activity_timestamp = _remote_cc["ts"] if remote_fresh else now
    activity_source = "collector report" if remote_fresh else "local session files"

    def has_activity_feed(provider_id: str) -> bool:
        return isinstance(provider_periods.get(provider_id, {}).get("day"), dict)

    def has_quota(quota: dict) -> bool:
        return any(
            quota.get(key) is not None
            for key in ("session_used_pct", "weekly_used_pct", "account_used_pct")
        ) or bool(quota.get("limits"))

    def quota_source(quota: dict) -> str:
        if quota.get("self_quota"):
            return "local self quota"
        return "subscription usage"

    claude_credits = bool(claude_quota.get("credit_pools"))
    codex_credits = bool(codex_quota.get("credit_pools")) or (
        isinstance(codex_analytics, dict) and codex_analytics.get("credits_used") is not None
    )
    claude_timestamp = claude_sample.get("timestamp")
    codex_timestamp = codex_quota.get("timestamp")

    return {
        "claude": {
            "quota": _feed_health(
                available=has_quota(claude_quota),
                source=quota_source(claude_quota),
                timestamp=claude_timestamp,
                now=now,
                stale_after_seconds=660,
                unavailable_detail="No subscription quota reported",
                stale_recovery="Refresh Claude sign-in",
            ),
            "activity": _feed_health(
                available=has_activity_feed("claude"),
                source=activity_source,
                timestamp=activity_timestamp,
                now=now,
                stale_after_seconds=_REMOTE_CC_TTL if remote_fresh else None,
                unavailable_detail="No local Claude activity in this period",
            ),
            "credits": _feed_health(
                available=claude_credits,
                source="subscription billing",
                timestamp=claude_timestamp,
                now=now,
                stale_after_seconds=660,
                unavailable_detail="Not reported for this plan",
                stale_recovery="Refresh Claude sign-in",
            ),
        },
        "codex": {
            "quota": _feed_health(
                available=has_quota(codex_quota),
                source=quota_source(codex_quota),
                timestamp=codex_timestamp,
                now=now,
                stale_after_seconds=660,
                unavailable_detail="No subscription quota reported",
                stale_recovery="Refresh Codex sign-in",
            ),
            "activity": _feed_health(
                available=has_activity_feed("codex"),
                source=activity_source,
                timestamp=activity_timestamp,
                now=now,
                stale_after_seconds=_REMOTE_CC_TTL if remote_fresh else None,
                unavailable_detail="No local Codex activity in this period",
            ),
            "credits": _feed_health(
                available=codex_credits,
                source="Codex analytics",
                timestamp=codex_timestamp or (_remote_cc["ts"] if remote_fresh else None),
                now=now,
                stale_after_seconds=660,
                unavailable_detail="No credit balance or usage reported",
                stale_recovery="Refresh Codex sign-in",
            ),
        },
    }


def _codex_local_payload() -> dict:
    if _remote_cc_fresh() and _remote_cc["codex_local"]:
        return _normalize_codex_local_payload(_remote_cc["codex_local"])
    return codex_local_stats()


def _codex_analytics_summary(period_metrics: dict | None = None) -> dict | None:
    """Surface breakdown plus web credit consumption for the active window."""
    result: dict = {}
    period_surfaces = (period_metrics or {}).get("surfaces") or {}
    if period_surfaces:
        total_tokens = sum(int(item.get("total_tokens") or 0) for item in period_surfaces.values())
        total_sessions = sum(int(item.get("sessions") or 0) for item in period_surfaces.values())
        measure = "total_tokens" if total_tokens else "sessions"
        total = total_tokens or total_sessions
        dominant = max(
            period_surfaces.items(),
            key=lambda item: int(item[1].get(measure) or 0),
        )
        result.update({
            "dominant_surface": dominant[0],
            "dominant_share_pct": round(int(dominant[1].get(measure) or 0) / total * 100) if total else None,
            "surfaces": {key: int(value.get("sessions") or 0) for key, value in period_surfaces.items()},
            "total_threads": total_sessions,
        })

    cl = _codex_local_payload()
    by_source = cl.get("by_source") if not period_surfaces else None
    if by_source:
        total = sum(s["sessions"] for s in by_source.values())
        if total:
            dominant = max(by_source.items(), key=lambda x: x[1]["sessions"])
            result.update({
                "dominant_surface": dominant[0],
                "dominant_share_pct": round(dominant[1]["sessions"] / total * 100),
                "surfaces": {k: v["sessions"] for k, v in by_source.items()},
                "total_threads": total,
            })

    analytics = _remote_cc.get("codex_analytics")
    if isinstance(analytics, dict):
        summary = analytics.get("summary") or {}
        workspace_summary = summary.get("workspace") or {}
        sessions_summary = summary.get("sessions_messages") or {}
        review_summary = summary.get("code_review") or {}
        credit_rows = analytics.get("credit_usage_events", {}).get("data", []) or []
        credits_used = sum(
            float(row.get("credit_amount") or 0)
            for row in credit_rows
            if isinstance(row, dict)
        )
        result.update({
            "avg_daily_turns": workspace_summary.get("avg_daily_turns"),
            "avg_daily_credits": sessions_summary.get("avg_daily_credits"),
            "credits_used": round(credits_used, 3),
            "credits_used_usd": round(credits_used * codex_credit_usd_estimate(), 2),
            "credits_used_usd_is_estimate": codex_credit_usd_is_estimate(),
            "credit_events_count": len(credit_rows),
            "credits_window_days": analytics.get("window_days"),
            "avg_daily_reviews": review_summary.get("avg_daily_reviews"),
            "avg_daily_comments": review_summary.get("avg_daily_comments"),
            "reviews_available": bool(analytics.get("daily_code_review_metrics", {}).get("data")),
        })
    return result or None


def _overlay_self_quota(quota: dict, provider_id: str, stale: bool = False) -> None:
    """Fill quota gauges from self-imposed caps when scraped data is unusable.

    Mutates `quota` in place. Only applies when the scraped session pct is
    missing or flagged stale, so subscription scraping always wins when live.
    """
    if quota.get("session_used_pct") is not None and not stale:
        return
    try:
        from src.self_quota import self_quota_snapshot
        snapshot = self_quota_snapshot(provider_id)
    except Exception:
        return
    if not snapshot:
        return
    for key in (
        "session_used_pct", "weekly_used_pct",
        "session_remaining_pct", "weekly_remaining_pct",
    ):
        if snapshot.get(key) is not None:
            quota[key] = snapshot[key]
    quota["source"] = snapshot["source"]
    quota["self_quota"] = {
        "window_hours": snapshot["window_hours"],
        "weekly_days": snapshot["weekly_days"],
        "session": snapshot["session_detail"],
        "weekly": snapshot["weekly_detail"],
        "models": snapshot.get("models") or {},
    }


# ── Endpoints ────────────────────────────────────────────

_stats_cache: dict | None = None
_stats_cache_ts: float = 0
_stats_cache_lock = threading.Lock()


@app.get("/stats")
def stats(_auth: None = Depends(verify_auth)) -> dict:
    global _stats_cache, _stats_cache_ts
    now = time.time()
    if _stats_cache is not None and (now - _stats_cache_ts) < 15:
        return _stats_cache
    with _stats_cache_lock:
        now = time.time()
        if _stats_cache is not None and (now - _stats_cache_ts) < 15:
            return _stats_cache
        return _build_stats()


def _build_stats() -> dict:
    global _stats_cache, _stats_cache_ts
    now = time.time()

    sample = latest_sample()
    if not sample:
        # Local mode / fresh install: no scraped quota samples exist.
        # API-based access / fresh install: no scraped quota samples exist.
        # Self-imposed quotas (plans.toml [<provider>.self_quota]) can still
        # populate the gauges, so continue with an empty sample.
        sample = {
            "timestamp": None,
            "session": None,
            "weekly": None,
            "extra": 0.0,
            "session_reset": None,
            "weekly_reset": None,
            "extra_reset": None,
            "extra_spent_usd": None,
            "extra_limit_usd": None,
            "extra_balance_usd": None,
        }

    burn = session_burn_rate()
    eta = predict_lock(sample["session"]) if sample["session"] is not None else None

    codex_quota = _normalize_codex_quota_payload(latest_codex())
    codex_cycles = codex_last_completed_cycles()
    codex_quota["session_last_cycle_used_pct"] = codex_cycles["session_used_pct"]
    codex_quota["weekly_last_cycle_used_pct"] = codex_cycles["weekly_used_pct"]
    fresh = _remote_cc_fresh()

    cc_today = (_remote_cc["messages"] if fresh else cc_messages_today()) or {}
    cc_tokens = (_remote_cc["tokens"] if fresh else cc_token_usage_today_cached()) or {}
    cc_stats_payload = (
        _normalize_claude_code_stats_payload(_remote_cc["cc_stats"])
        if fresh
        else shape_claude_code_stats(load_claude_code_stats())
    )
    cc_stats_payload = _with_recent_claude_daily_tokens(cc_stats_payload)
    codex_local_payload = (
        _normalize_codex_local_payload(_remote_cc["codex_local"])
        if fresh
        else codex_local_stats()
    )
    codex_session_payload = (
        _normalize_codex_session_payload(_remote_cc["codex_sessions"])
        if fresh
        else scan_codex_session_metrics()
    )
    claude_today = _claude_today_contract(cc_today, cc_tokens)
    codex_today = _codex_today_contract(codex_local_payload, codex_session_payload)
    claude_totals = _claude_totals_contract(cc_stats_payload)
    codex_totals = _codex_totals_contract(codex_local_payload, codex_session_payload)
    pace = weekly_utilization_pace()
    streak = current_streak()

    # Productivity metrics from today's token data
    today_active_hours = claude_today["active_hours_today"] or 0
    today_output = claude_today["output_tokens_today"] or 0
    today_density = output_density(today_output, today_active_hours)
    today_cache = cache_health(
        cc_tokens.get("cache_read_tokens", 0),
        cc_tokens.get("cache_create_tokens", 0),
    )

    cc_session_left = parse_claude_session_hours_left(sample["session_reset"])
    if codex_quota.get("session_reset"):
        codex_quota["session_reset"] = normalize_reset(codex_quota["session_reset"])
    if codex_quota.get("weekly_reset"):
        codex_quota["weekly_reset"] = normalize_reset(codex_quota["weekly_reset"])
    # Freshness: insert() only fires when the collector successfully scrapes
    # Claude subscription quota. If the web path fails (e.g. expired or
    # missing cookie), no new row is written and
    # latest_sample() returns the last good values — stale, but
    # indistinguishable unless we flag them. Threshold is 2× the collector
    # cycle (5 min) plus
    # margin, giving one missed cycle of grace.
    CLAUDE_QUOTA_STALE_AFTER = 660  # seconds (11 min)
    quota_age = max(0, int(now - (sample.get("timestamp") or now)))
    quota_stale = quota_age > CLAUDE_QUOTA_STALE_AFTER
    claude_quota = {
        "session_used_pct": sample["session"],
        "weekly_used_pct": sample["weekly"],
        "weekly_sonnet_used_pct": sample.get("weekly_sonnet_pct"),
        "weekly_design_used_pct": sample.get("weekly_design_pct"),
        "session_remaining_pct": 100 - sample["session"] if sample["session"] is not None else None,
        "weekly_remaining_pct": 100 - sample["weekly"] if sample["weekly"] is not None else None,
        "session_reset": normalize_claude_session_reset(sample["session_reset"]),
        "weekly_reset": normalize_reset(sample["weekly_reset"]),
        "age_seconds": quota_age,
        "is_stale": quota_stale,
        "stale_threshold_seconds": CLAUDE_QUOTA_STALE_AFTER,
        "limits": sample.get("limits") or [],
        "credit_pools": sample.get("credit_pools") or [],
    }
    claude_plan = resolve_plan("claude", sample.get("raw_plan") or sample.get("plan_id"), plans=load_plans())
    claude_quota.update({
        "plan_id": claude_plan.get("plan_id") or sample.get("plan_id"),
        "plan_label": claude_plan.get("plan_label"),
        "plan_source": sample.get("plan_source") or claude_plan.get("plan_source"),
        "raw_plan": sample.get("raw_plan"),
        "plan_entitlements": claude_plan.get("entitlements") or {},
    })

    # Self-imposed quotas: when the scraped quota is absent or stale
    # (Bedrock / Vertex / enterprise / API-based access), fill the gauges from
    # locally measured usage against plans.toml [<provider>.self_quota] caps.
    _overlay_self_quota(claude_quota, "claude", stale=quota_stale)
    _overlay_self_quota(codex_quota, "codex")

    provider_periods = {
        "claude": {"day": _today_period_contract(claude_today), "week": {}},
        "codex": {"day": _today_period_contract(codex_today), "week": {}},
    }
    period_now = datetime.fromtimestamp(now, tz=timezone.utc)
    for provider_id, quota in (("claude", claude_quota), ("codex", codex_quota)):
        if not quota.get("limits"):
            quota["cost_estimates"] = []
            quota["cost_estimate_status"] = "not_applicable"
            continue
        try:
            bundle = cached_usage_bundle(
                provider_id,
                quota["limits"],
                _provider_period_windows(quota, period_now),
                now=period_now,
            )
        except Exception:
            quota["cost_estimates"] = []
            quota["cost_estimate_status"] = "unavailable"
            continue
        quota["cost_estimates"] = bundle["quota_estimates"]
        quota["cost_estimate_status"] = bundle.get("status", "current")
        quota["cost_estimate_updated_at"] = bundle.get("updated_at")
        quota["pricing_catalog"] = bundle.get("pricing_catalog") or pricing_catalog_status()
        provider_periods[provider_id].update(bundle["periods"])

    codex_windows = _provider_period_windows(codex_quota, period_now)
    for period_name, (window_start, window_end) in codex_windows.items():
        reset_changes = codex_reset_credit_balance_changes(
            int(window_start.timestamp()),
            int(window_end.timestamp()),
        )
        codex_quota[f"rate_limit_reset_credits_used_{period_name}"] = reset_changes["balance_decreases"]
        codex_quota[f"rate_limit_reset_credits_granted_{period_name}"] = reset_changes["balance_increases"]

    codex_analytics_summary = _codex_analytics_summary(
        provider_periods.get("codex", {}).get("week")
    )
    provider_health = _provider_feed_health(
        now=now,
        remote_fresh=fresh,
        claude_sample=sample,
        claude_quota=claude_quota,
        codex_quota=codex_quota,
        provider_periods=provider_periods,
        codex_analytics=codex_analytics_summary,
    )

    try:
        from src.gap_rollups import gap_rollups_for_stats
        gap_rollups = gap_rollups_for_stats()
    except Exception:
        gap_rollups = None

    provider_trends = {}
    for provider_id in ("claude", "codex"):
        try:
            provider_trends[provider_id] = provider_usage_trend(provider_id, days=30, now=period_now)
        except Exception:
            provider_trends[provider_id] = None

    result = {
        "timestamp": sample["timestamp"],
        "pricing_catalog": pricing_catalog_status(),
        "provider_registry": provider_registry(),
        "providers_latest": _providers_latest_payload(),
        "provider_periods": provider_periods,
        "provider_health": provider_health,
        "provider_trends": provider_trends,
        "risk_outlook": compute_risk_outlook(burn, claude_quota["session_used_pct"] or 0, pace, codex_quota),
        "extra": sample["extra"],
        "extra_reset": sample["extra_reset"],
        "extra_spent_usd": sample["extra_spent_usd"],
        "extra_limit_usd": sample["extra_limit_usd"],
        "extra_balance_usd": sample["extra_balance_usd"],
        # Pacing — Claude
        "burn": burn,
        "workload": workload_label(burn),
        "lock_eta": eta,
        # Pacing — Codex
        "codex_burn": codex_burn_rate(),
        # Pacing — shared
        "weekly_pace": pace,
        "streak": streak,
        # Productivity
        "output_density": today_density,
        "cache_health": today_cache,
        # 4-state gap rollups (focus / attention-idle / off-hours / agent-runtime).
        # Rolled up over today, yesterday, and last 7d.
        # Legacy human_time_sec is retained (= focus + attention); downtime_sec is
        # zeroed because the legacy "downtime" bucket folded into attention_idle.
        "gap_rollups": gap_rollups,
        # Normalized provider contract
        "claude_today": claude_today,
        "codex_today": codex_today,
        "claude_totals": claude_totals,
        "codex_totals": codex_totals,
        "claude_quota": claude_quota,
        "codex_quota": codex_quota,
        "plan_history": {
            "claude": plan_history("claude"),
            "codex": plan_history("codex"),
        },
        "codex_analytics_summary": codex_analytics_summary,
        "cursor": _remote_cc.get("cursor_usage") if fresh else None,
        "cc_session_hours_left": cc_session_left,
    }
    _stats_cache = result
    _stats_cache_ts = time.time()
    return result


@app.post("/cc/report")
def cc_report(report: CCReport, _auth: None = Depends(verify_auth)) -> dict:
    _remote_cc["messages"] = report.messages
    _remote_cc["tokens"] = report.tokens
    _remote_cc["projects"] = report.projects
    _remote_cc["codex_local"] = report.codex_local
    _remote_cc["codex_sessions"] = report.codex_sessions
    _remote_cc["cc_stats"] = report.cc_stats
    if report.usage is not None:
        _remote_cc["usage"] = report.usage
    if report.codex_usage is not None:
        _remote_cc["codex_usage"] = report.codex_usage
    if report.codex_analytics is not None:
        _remote_cc["codex_analytics"] = report.codex_analytics
    if report.cursor_usage is not None:
        _remote_cc["cursor_usage"] = report.cursor_usage
    if report.provider_snapshots is not None:
        _remote_cc["provider_snapshots"] = report.provider_snapshots
    _remote_cc["ts"] = time.time()

    # If codex usage data included, insert into DB for history
    if report.codex_usage:
        cu = report.codex_usage
        plan = resolve_plan("codex", cu.get("raw_plan"), plans=load_plans())
        insert_codex(
            cu.get("weekly_remaining_pct"),
            cu.get("code_review_remaining_pct"),
            cu.get("weekly_reset") or cu.get("reset_at"),
            session_remaining_pct=cu.get("session_remaining_pct"),
            session_reset=cu.get("session_reset"),
            weekly_gpt54_remaining_pct=cu.get("weekly_gpt54_remaining_pct"),
            weekly_spark_remaining_pct=cu.get("weekly_spark_remaining_pct"),
            plan_id=cu.get("plan_id") or plan.get("plan_id"),
            raw_plan=cu.get("raw_plan"),
            plan_source=cu.get("plan_source") or plan.get("plan_source"),
            limits=cu.get("limits"),
            credit_pools=cu.get("credit_pools"),
            rate_limit_reset_credits=cu.get("rate_limit_reset_credits"),
            credits_remaining=cu.get("credits_remaining"),
        )

    # If usage data included, also insert into the DB so history works
    if report.usage:
        u = report.usage
        plan = resolve_plan("claude", u.get("raw_plan"), plans=load_plans())
        insert(
            u.get("session_pct", 0),
            u.get("weekly_pct", 0),
            u.get("extra_pct", 0),
            session_reset=u.get("session_reset"),
            weekly_reset=u.get("weekly_reset"),
            extra_reset=u.get("extra_reset"),
            extra_spent_usd=u.get("extra_spent_usd"),
            extra_limit_usd=u.get("extra_limit_usd"),
            extra_balance_usd=u.get("extra_balance_usd"),
            weekly_sonnet_pct=u.get("weekly_sonnet_pct"),
            weekly_design_pct=u.get("weekly_design_pct"),
            plan_id=u.get("plan_id") or plan.get("plan_id"),
            raw_plan=u.get("raw_plan"),
            plan_source=u.get("plan_source") or plan.get("plan_source"),
            limits=u.get("limits"),
            credit_pools=u.get("credit_pools"),
        )

    snapshots_map = report.provider_snapshots
    if snapshots_map is None:
        snapshots_map = build_provider_snapshots(
            timestamp=int(_remote_cc["ts"] or time.time()),
            collector_access="subscription",
            messages=report.messages,
            tokens=report.tokens,
            usage=report.usage,
            codex_local=report.codex_local,
            codex_sessions=report.codex_sessions,
            codex_usage=report.codex_usage,
            cursor_usage=report.cursor_usage,
            errors=None,
        )
    if isinstance(snapshots_map, dict):
        snapshots = [snapshot for snapshot in snapshots_map.values() if isinstance(snapshot, dict)]
        if snapshots:
            insert_provider_metric_samples(snapshots)
    prune_provider_metric_samples(retention_days=180)

    return {"status": "ok"}


@app.get("/budget/weekly")
def budget_weekly_api(_auth: None = Depends(verify_auth)) -> dict:
    """Weekly forecast with day-by-day quota projections through reset."""
    plans = load_plans()
    if not plans:
        return {"forecasts": {}}
    providers = _providers_latest_payload()
    return weekly_forecast(plans, providers)


@app.get("/work-ledger/status")
def work_ledger_status(_auth: None = Depends(verify_auth)) -> dict:
    return work_ledger.diagnostics()


class WorkLedgerSettingsUpdate(BaseModel):
    enabled: bool
    explicit_roots: list[str] | None = None
    retention_days: int | None = None


class RepositoryEnableUpdate(BaseModel):
    enabled: bool


class WorkItemCreate(BaseModel):
    kind: str
    name: str
    parent_id: str | None = None
    repository_id: str | None = None
    color_hex: str | None = None


class ProjectFromFolderCreate(BaseModel):
    path: str
    session_id: str | None = None
    color_hex: str | None = "#2FAF88"


class ActiveWorkUpdate(BaseModel):
    work_item_id: str | None = None


class SessionWorkUpdate(BaseModel):
    nickname: str | None = None
    assignment_mode: str | None = None
    work_item_id: str | None = None


@app.put("/work-ledger/settings")
def work_ledger_update_settings(
    update: WorkLedgerSettingsUpdate,
    _auth: None = Depends(verify_auth),
) -> dict:
    settings = work_ledger.configure_collection(
        enabled=update.enabled,
        explicit_roots=update.explicit_roots,
        retention_days=update.retention_days,
    )
    if not update.enabled:
        work_ledger.switch_active_work_item(None)
        refresh = {"accepted": False, "reason": "collection_paused"}
    else:
        refresh = work_refresh.start_background_refresh()
    return {"settings": settings, "refresh": refresh}


@app.get("/work-ledger/repositories")
def work_ledger_repositories(_auth: None = Depends(verify_auth)) -> dict:
    return {"repositories": work_ledger.list_repositories()}


@app.post("/work-ledger/projects/from-folder")
def work_ledger_project_from_folder(
    update: ProjectFromFolderCreate,
    _auth: None = Depends(verify_auth),
) -> dict:
    if update.session_id and work_ledger.get_ai_session_by_id(update.session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    repository_result = repository_discovery.discover_repository(
        update.path,
        enabled=True,
    )
    if repository_result is None:
        raise HTTPException(
            status_code=400,
            detail="Choose a folder inside a Git repository",
        )
    repository_id = repository_result["repository_id"]
    work_ledger.set_repository_enabled(repository_id, True)
    repository = next(
        row for row in work_ledger.list_repositories() if row["id"] == repository_id
    )
    settings = work_ledger.settings()
    roots = list(settings["explicit_roots"])
    repository_root = repository_result["path"]
    if repository_root not in roots:
        roots.append(repository_root)
        settings = work_ledger.configure_collection(
            enabled=settings["collection_enabled"],
            explicit_roots=roots,
        )
    try:
        project = work_ledger.create_work_item(
            kind="project",
            name=repository["display_name"],
            repository_id=repository_id,
            color_hex=update.color_hex,
        )
        if update.session_id:
            work_ledger.set_session_work_assignment(
                update.session_id,
                mode="work_item",
                work_item_id=project["id"],
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    refresh = (
        work_refresh.start_background_refresh()
        if settings["collection_enabled"]
        else {"accepted": False, "reason": "collection_paused"}
    )
    return {
        "repository": repository,
        "item": project,
        "assigned_session_id": update.session_id,
        "refresh": refresh,
    }


@app.patch("/work-ledger/repositories/{repository_id}")
def work_ledger_update_repository(
    repository_id: str,
    update: RepositoryEnableUpdate,
    _auth: None = Depends(verify_auth),
) -> dict:
    if not work_ledger.set_repository_enabled(repository_id, update.enabled):
        raise HTTPException(status_code=404, detail="Repository not found")
    repository = next(
        row for row in work_ledger.list_repositories() if row["id"] == repository_id
    )
    return {"repository": repository}


@app.get("/work-ledger/items")
def work_ledger_items(_auth: None = Depends(verify_auth)) -> dict:
    return {
        "items": work_ledger.list_work_items(),
        "active": work_ledger.active_work_context(),
    }


@app.post("/work-ledger/items")
def work_ledger_create_item(
    item: WorkItemCreate,
    _auth: None = Depends(verify_auth),
) -> dict:
    try:
        created = work_ledger.create_work_item(
            kind=item.kind,
            name=item.name,
            parent_id=item.parent_id,
            repository_id=item.repository_id,
            color_hex=item.color_hex,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"item": created}


@app.delete("/work-ledger/items/{item_id}")
def work_ledger_archive_item(
    item_id: str,
    _auth: None = Depends(verify_auth),
) -> dict:
    if not work_ledger.archive_work_item(item_id):
        raise HTTPException(status_code=404, detail="Work item not found")
    return {"status": "archived", "active": work_ledger.active_work_context()}


@app.get("/work-ledger/active")
def work_ledger_active(_auth: None = Depends(verify_auth)) -> dict:
    return work_ledger.active_work_context()


@app.put("/work-ledger/active")
def work_ledger_update_active(
    update: ActiveWorkUpdate,
    _auth: None = Depends(verify_auth),
) -> dict:
    try:
        return work_ledger.switch_active_work_item(update.work_item_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/work-ledger/intervals")
def work_ledger_intervals(
    limit: int = 500,
    _auth: None = Depends(verify_auth),
) -> dict:
    return {"intervals": work_ledger.list_attribution_intervals(limit=limit)}


@app.get("/work-ledger/report")
def work_ledger_report(
    period: str = "week",
    _auth: None = Depends(verify_auth),
) -> dict:
    """Work report: activity, cost, and cost-to-outcome ratios per bucket.

    Every metrics object carries cost_per_pr, cost_per_commit and
    cost_per_changed_line alongside pricing_coverage_pct, which states how much of
    that bucket's cost side was actually priced.

    A ratio is null, never 0, whenever it cannot be stated honestly: the denominator
    is zero, the cost side is unpriced, or the bucket has no AI usage at all (a
    0.00 cost there means "never measured", not "free"). Consumers must treat null
    as "not stateable" and must not coerce it to 0, which would rank unmeasured work
    as the cheapest in the report.

    cost_per_changed_line is a cost per line TOUCHED rather than landed, because
    changed_lines accumulates working-tree diffs observed while editing.
    """
    try:
        return work_reporting.build_report(period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/usage/explanation")
def usage_explanation_report(
    provider: str,
    period: str = "day",
    start_at: str | None = None,
    _auth: None = Depends(verify_auth),
) -> dict:
    parsed_start = None
    if start_at:
        try:
            parsed_start = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="start_at must be an ISO timestamp") from exc
        if parsed_start.tzinfo is None:
            parsed_start = parsed_start.replace(tzinfo=timezone.utc)
    try:
        return usage_explanation.build_usage_explanation(
            provider,
            period,
            start_at=parsed_start,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/work-ledger/refresh")
def work_ledger_refresh(_auth: None = Depends(verify_auth)) -> dict:
    return work_refresh.start_background_refresh()


@app.get("/work-ledger/sessions")
def work_ledger_sessions(
    provider: str | None = None,
    repository_id: str | None = None,
    active_only: bool = False,
    review_hints: bool = False,
    limit: int = 500,
    _auth: None = Depends(verify_auth),
) -> dict:
    sessions = session_runtime.list_session_views(
        provider=provider,
        repository_id=repository_id,
        active_only=active_only,
        limit=limit,
    )
    if review_hints:
        for session in sessions:
            if (
                session.get("attribution_source") == "automatic"
                and session.get("repository_id") is None
            ):
                session["first_prompt"] = session_evidence.first_prompt_preview(session)
    return {"sessions": sessions}


@app.get("/work-ledger/sessions/{session_id}/evidence")
def work_ledger_session_evidence(
    session_id: str,
    limit: int = 800,
    _auth: None = Depends(verify_auth),
) -> dict:
    result = session_evidence.session_timeline(session_id, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return result


@app.get("/work-ledger/session-search")
def work_ledger_session_search(
    q: str,
    provider: str | None = None,
    limit: int = 50,
    _auth: None = Depends(verify_auth),
) -> dict:
    try:
        return session_evidence.search(q, provider=provider, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/work-ledger/session-search/status")
def work_ledger_session_search_status(
    provider: str | None = None,
    _auth: None = Depends(verify_auth),
) -> dict:
    try:
        return session_search_index.status(provider=provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/work-ledger/sessions/{session_id}")
def work_ledger_update_session(
    session_id: str,
    update: SessionWorkUpdate,
    _auth: None = Depends(verify_auth),
) -> dict:
    changed = False
    fields_set = update.model_fields_set
    try:
        if "nickname" in fields_set:
            if not work_ledger.set_session_nickname(session_id, update.nickname):
                raise HTTPException(status_code=404, detail="Session not found")
            changed = True
        if update.assignment_mode is not None:
            if not work_ledger.set_session_work_assignment(
                session_id,
                mode=update.assignment_mode,
                work_item_id=update.work_item_id,
            ):
                raise HTTPException(status_code=404, detail="Session not found")
            changed = True
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not changed:
        raise HTTPException(status_code=400, detail="No session changes supplied")
    return {"status": "ok"}


@app.get("/work-ledger/events")
def work_ledger_events(
    repository_id: str | None = None,
    ai_session_id: str | None = None,
    provider: str | None = None,
    kind: str | None = None,
    include_superseded: bool = False,
    limit: int = 1_000,
    _auth: None = Depends(verify_auth),
) -> dict:
    return {
        "events": work_ledger.list_activity_events(
            repository_id=repository_id,
            ai_session_id=ai_session_id,
            provider=provider,
            kind=kind,
            include_superseded=include_superseded,
            limit=limit,
        )
    }


@app.get("/work-ledger/reconciliation")
def work_ledger_reconciliation(_auth: None = Depends(verify_auth)) -> dict:
    return work_ledger.reconciliation()


@app.get("/focus/facts")
def focus_facts(
    period: str,
    _auth: None = Depends(verify_auth),
) -> dict:
    """FOCUS fact rows for a YYYY-MM period.

    A cost that was never measured serialises as null. It is not coerced to 0, which a
    consumer would add into a total as if the usage had been free.
    """
    try:
        billing_period_start = focus.period_start(period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "focus_version": focus.FOCUS_VERSION,
        "period": period,
        "billing_period_start": billing_period_start,
        "facts": focus.facts_for_period(billing_period_start),
    }


@app.get("/focus/export")
def focus_export(
    period: str,
    format: str = "csv",
    _auth: None = Depends(verify_auth),
) -> Response:
    """Download a period as FOCUS 1.3 CSV. An absent cost is an empty field, not 0."""
    if format != "csv":
        raise HTTPException(status_code=400, detail=f"unsupported format {format!r}; expected 'csv'")
    try:
        body = focus.export_focus_csv(period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="focus-{period}.csv"'},
    )


@app.get("/focus/summary")
def focus_summary(
    period: str,
    _auth: None = Depends(verify_auth),
) -> dict:
    """Per-provider cost totals for a period, with unpriced rows counted, not zeroed."""
    try:
        return focus.summary_for_period(period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/focus/reconcile")
def focus_reconcile(
    period: str,
    provider: str | None = None,
    _auth: None = Depends(verify_auth),
) -> dict:
    """What a vendor billed against what this machine derived, per provider-month.

    A difference of 0.0 means the two agreed. A difference of null means the comparison
    could not be made, and `difference_absent_reason` says which side was incomplete.
    The two are opposite findings and are never both serialised as 0.

    Each provider also carries `coverage`, and `degraded_providers` lists the ones whose
    supply shrank, so a reader sees a closed provider surface without walking the payload.
    """
    try:
        return invoice_ingest.reconcile_period(period, provider=provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    # Hard-locked to loopback. Remote access must go through SSH/tailscale, not direct exposure.
    import uvicorn

    uvicorn.run("src.api:app", host="127.0.0.1", port=8000)
