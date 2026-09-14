"""Usage metrics split into Pacing and Productivity.

Pacing: operational metrics about rate-limit management.
Productivity: outcome metrics about value delivered.
"""

import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

from src.database import last_codex_samples, last_samples, latest_codex, latest_sample, samples_in_range

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_WINDOW = 5
WEEKLY_RESET_THRESHOLD = 20
SESSION_CONTINUITY_SEC = 3600
TAIL_ACTIVE_SEC = 900

# Streak: consecutive days with >= this many active hours
STREAK_ACTIVE_HOURS_THRESHOLD = 1.0


# ═══════════════════════════════════════════════════════════════════════════
# PACING — "Am I burning through limits too fast?"
# ═══════════════════════════════════════════════════════════════════════════


def session_burn_rate(window: int = SESSION_WINDOW) -> float:
    """Session %/hr over the last N samples. Operational, short-horizon."""
    samples = last_samples(window)
    if len(samples) < 2:
        return 0.0

    newest = samples[0]
    oldest = samples[-1]
    pct_delta = newest[1] - oldest[1]
    time_delta_hours = (newest[0] - oldest[0]) / 3600

    if time_delta_hours <= 0:
        return 0.0

    return max(0.0, pct_delta / time_delta_hours)


def codex_burn_rate(window: int = 5) -> float:
    """Codex session %/hr consumed over the last N samples.

    Codex stores session_remaining_pct (100=full), so burn = decrease rate.
    """
    samples = last_codex_samples(window)
    if len(samples) < 2:
        return 0.0

    newest = samples[0]   # (timestamp, remaining_pct)
    oldest = samples[-1]
    # remaining drops as usage increases, so delta is negative when burning
    pct_consumed = oldest[1] - newest[1]  # positive when burning
    time_delta_hours = (newest[0] - oldest[0]) / 3600

    if time_delta_hours <= 0:
        return 0.0

    return max(0.0, pct_consumed / time_delta_hours)


def predict_lock(session: float) -> float | None:
    """ETA (unix timestamp) when session will hit 100% at current burn rate."""
    burn = session_burn_rate()
    if burn <= 0:
        return None

    remaining = max(0.0, 100.0 - session)
    hours = remaining / burn
    return time.time() + (hours * 3600)


def workload_label(burn: float) -> str:
    """Human label for current burn intensity."""
    if burn > 35:
        return "Ultra Context"
    if burn > 15:
        return "Heavy Code"
    if burn > 5:
        return "Coding"
    return "Chat"


def weekly_utilization_pace() -> dict:
    """Project weekly usage to end of reset window.

    Derives window length from the last observed weekly reset in history
    rather than assuming a fixed 7-day window.
    """
    sample = latest_sample()
    if not sample:
        return {"current_weekly_pct": 0, "days_elapsed": 0, "projected_pct": 0, "on_track": False}

    current_weekly = sample["weekly"]
    weekly_reset_str = sample.get("weekly_reset") or ""

    days_until_reset = _estimate_days_until_reset(weekly_reset_str)

    # Find actual window start from history (last weekly reset event)
    now_ts = sample["timestamp"]
    window_start_ts = _find_last_weekly_reset_ts()

    if window_start_ts is not None and days_until_reset is not None:
        days_elapsed = max((now_ts - window_start_ts) / 86400, 0)
        window_length = days_elapsed + days_until_reset
    elif days_until_reset is not None:
        # No reset found in history — fall back to 7-day assumption
        window_length = 7.0
        days_elapsed = max(window_length - days_until_reset, 0.5)
    else:
        window_length = 7.0
        days_elapsed = 3.5  # fallback: assume mid-window

    rate = current_weekly / days_elapsed if days_elapsed > 0 else 0
    projected = round(min(rate * window_length, 100), 1)

    # Pace status: under-using / on-track / front-loaded
    daily_budget = current_weekly / days_elapsed if days_elapsed > 0 else 0
    ideal_daily = 100.0 / window_length if window_length > 0 else 14.3
    pace_ratio = daily_budget / ideal_daily if ideal_daily > 0 else 0

    if pace_ratio < 0.8:
        pace_status = "under"
    elif pace_ratio <= 1.2:
        pace_status = "on_track"
    else:
        pace_status = "front_loaded"

    return {
        "current_weekly_pct": round(current_weekly, 1),
        "days_elapsed": round(days_elapsed, 2),
        "days_remaining": round(days_until_reset, 1) if days_until_reset else None,
        "window_length": round(window_length, 1),
        "projected_pct": projected,
        "pace_status": pace_status,
        "on_track": pace_status == "on_track",
    }


def current_streak(lookback_days: int = 90) -> int:
    """Consecutive recent days with >= STREAK_ACTIVE_HOURS_THRESHOLD active hours.

    Based on actual usage activity, not session-peak averages.
    Synthetic zero-filled days with no samples are skipped (don't break streak).
    """
    daily = daily_activity(days=lookback_days)
    if not daily:
        return 0

    streak = 0
    for day in reversed(daily):
        if not day.get("has_data", True):
            continue
        if day["active_hours"] >= STREAK_ACTIVE_HOURS_THRESHOLD:
            streak += 1
        else:
            break

    return streak


# ═══════════════════════════════════════════════════════════════════════════
# PRODUCTIVITY — "Am I getting value from the tool?"
# ═══════════════════════════════════════════════════════════════════════════


def output_density(output_tokens: int, active_hours: float) -> float | None:
    """Output tokens per active hour. Higher = more productive sessions."""
    if active_hours <= 0:
        return None
    return round(output_tokens / active_hours)


def cache_health(cache_read: int, cache_create: int) -> float:
    """Cache reuse ratio (0-100%). High = good context management."""
    total = cache_read + cache_create
    if total <= 0:
        return 0.0
    return round(cache_read / total * 100, 1)


# ═══════════════════════════════════════════════════════════════════════════
# BUDGET — cross-provider utilization against configured plan caps
# ═══════════════════════════════════════════════════════════════════════════


def budget_utilization(plans: dict, providers_latest: dict) -> dict:
    """Compute per-provider utilization against configured plan budgets.

    Args:
        plans: Parsed plans.toml dict (provider_id -> config).
        providers_latest: Dict of provider_id -> snapshot dict with
            'shared' and 'unique' sub-dicts (from _providers_latest_payload).
    """
    from src.plan_config import total_monthly_cost

    result: dict[str, dict] = {}

    for provider_id, plan in plans.items():
        if not isinstance(plan, dict) or "plan" not in plan:
            continue

        snapshot = providers_latest.get(provider_id, {})
        shared = snapshot.get("shared", {}) if isinstance(snapshot, dict) else {}

        entry: dict = {
            "plan": plan.get("plan"),
            "cost_usd_month": plan.get("cost_usd_month", 0),
        }

        # --- Claude / Codex: weekly output token cap ---
        weekly_cap = plan.get("weekly_cap_output_tokens")
        if weekly_cap:
            used_pct = shared.get("secondary_used_pct")
            if used_pct is None:
                # Fall back to direct DB query
                used_pct = _live_weekly_pct(provider_id)
            entry.update(
                unit="output_tokens",
                window="weekly",
                cap=weekly_cap,
                used_pct=round(used_pct, 1) if used_pct is not None else None,
                used_absolute=round(weekly_cap * used_pct / 100) if used_pct is not None else None,
                remaining_absolute=round(weekly_cap * (100 - used_pct) / 100) if used_pct is not None else None,
            )

        # --- Session cap (supplemental info) ---
        session_cap = plan.get("session_cap_output_tokens")
        if session_cap:
            session_pct = shared.get("primary_used_pct")
            if session_pct is None:
                session_pct = _live_session_pct(provider_id)
            entry["session_cap"] = session_cap
            entry["session_used_pct"] = round(session_pct, 1) if session_pct is not None else None

        # --- Tracking-only providers (no cap configured) ---
        if "unit" not in entry:
            entry.update(unit="none", window="none", cap=None, used_pct=None)

        result[provider_id] = entry

    # Overall utilization: cost-weighted average of used_pct across providers with caps
    weighted_sum = 0.0
    weight_total = 0.0
    for pid, e in result.items():
        pct = e.get("used_pct")
        if pct is None or e.get("cap") is None:
            continue
        weight = max(e.get("cost_usd_month", 0), 1)  # free providers get weight 1
        weighted_sum += pct * weight
        weight_total += weight

    return {
        "configured": True,
        "providers": result,
        "total_monthly_cost_usd": total_monthly_cost(plans),
        "overall_utilization_pct": round(weighted_sum / weight_total, 1) if weight_total > 0 else None,
    }


def _live_weekly_pct(provider_id: str) -> float | None:
    """Get live weekly used % from the dedicated usage tables."""
    if provider_id == "claude":
        sample = latest_sample()
        return sample["weekly"] if sample else None
    if provider_id == "codex":
        codex = latest_codex()
        if codex and codex.get("weekly_remaining_pct") is not None:
            return 100.0 - codex["weekly_remaining_pct"]
    return _latest_snapshot_pct(provider_id, "primary_used_pct")


def _live_session_pct(provider_id: str) -> float | None:
    """Get live session used % from the dedicated usage tables."""
    if provider_id == "claude":
        sample = latest_sample()
        return sample["session"] if sample else None
    if provider_id == "codex":
        codex = latest_codex()
        if codex and codex.get("session_remaining_pct") is not None:
            return 100.0 - codex["session_remaining_pct"]
    return None


def _latest_snapshot_pct(provider_id: str, field: str) -> float | None:
    """Find the most recent non-null value for a shared_json field in provider snapshots."""
    from src.database import _connect, _safe_json_load
    from contextlib import closing
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"""
            SELECT shared_json FROM provider_metric_samples
            WHERE provider = ?
            AND json_extract(shared_json, '$.{field}') IS NOT NULL
            ORDER BY timestamp DESC LIMIT 1
            """,
            (provider_id,),
        ).fetchall()
    if rows:
        shared = _safe_json_load(rows[0][0], {})
        return shared.get(field)
    return None


# ═══════════════════════════════════════════════════════════════════════════
# WEEKLY FORECAST — day-by-day projection with reset boundary
# ═══════════════════════════════════════════════════════════════════════════


def weekly_forecast(plans: dict, providers_latest: dict) -> dict:
    """Build a day-by-day forecast for the current week through the reset boundary.

    Shows remaining quota per day (even-paced), the reset event, and
    post-reset fresh capacity for the days after.
    """
    from src.plan_config import MODEL_TIERS, TIER_LABELS

    now = datetime.now()
    budget = budget_utilization(plans, providers_latest)
    forecasts: dict[str, dict] = {}

    for pid in ("claude", "codex"):
        prov = budget.get("providers", {}).get(pid, {})
        plan_cfg = plans.get(pid, {})
        if not isinstance(plan_cfg, dict):
            continue

        weekly_cap = plan_cfg.get("weekly_cap_output_tokens")
        used_pct = prov.get("used_pct")
        if weekly_cap is None or used_pct is None:
            continue

        remaining = prov.get("remaining_absolute", 0) or 0

        # Get reset info
        if pid == "claude":
            sample = latest_sample()
            reset_str = (sample or {}).get("weekly_reset", "")
        else:
            codex = latest_codex()
            reset_str = (codex or {}).get("reset_at", "")

        days_until_reset = _estimate_days_until_reset(reset_str)
        if days_until_reset is None:
            days_until_reset = 5.0  # fallback

        # Parse reset date
        reset_date = now + timedelta(days=days_until_reset)

        # Build day-by-day forecast
        days: list[dict] = []
        budget_left = remaining
        today = now.date()

        # Tier weights for this provider
        tier_weights = {}
        for info in MODEL_TIERS.values():
            if info["provider"] == pid:
                t = info["tier"]
                if t not in tier_weights:
                    tier_weights[t] = {"weight": info["quota_weight"], "label": TIER_LABELS.get(t, f"Tier {t}")}

        # Pre-reset days
        pre_reset_days = max(1, int(days_until_reset + 0.5))
        daily_budget = budget_left / max(pre_reset_days, 1)

        for i in range(pre_reset_days):
            day_date = today + timedelta(days=i)
            is_today = (i == 0)
            alloc = min(daily_budget, budget_left)

            day_entry = {
                "date": day_date.isoformat(),
                "label": "Today" if is_today else day_date.strftime("%a %b %d"),
                "is_today": is_today,
                "is_reset_day": False,
                "budget_tokens": round(alloc),
                "cumulative_remaining": round(budget_left),
                "pct_of_cap": round(alloc / weekly_cap * 100, 1) if weekly_cap > 0 else 0,
            }

            # Per-tier breakdown
            if tier_weights:
                tiers = {}
                for t, tw in sorted(tier_weights.items()):
                    tiers[str(t)] = {
                        "label": tw["label"],
                        "tokens": round(alloc / tw["weight"]) if tw["weight"] > 0 else round(alloc),
                        "weight": tw["weight"],
                    }
                day_entry["tiers"] = tiers

            days.append(day_entry)
            budget_left -= alloc

        # Reset day marker
        reset_day_date = today + timedelta(days=pre_reset_days)
        days.append({
            "date": reset_day_date.isoformat(),
            "label": reset_day_date.strftime("%a %b %d"),
            "is_today": False,
            "is_reset_day": True,
            "budget_tokens": round(weekly_cap / 7),  # fresh daily budget after reset
            "cumulative_remaining": weekly_cap,
            "pct_of_cap": round(100 / 7, 1),
            "reset_event": True,
        })

        # Post-reset days (show 3 days after reset for context)
        post_reset_daily = weekly_cap / 7
        post_budget_left = weekly_cap
        for i in range(1, 4):
            day_date = reset_day_date + timedelta(days=i)
            alloc = min(post_reset_daily, post_budget_left)
            days.append({
                "date": day_date.isoformat(),
                "label": day_date.strftime("%a %b %d"),
                "is_today": False,
                "is_reset_day": False,
                "budget_tokens": round(alloc),
                "cumulative_remaining": round(post_budget_left),
                "pct_of_cap": round(alloc / weekly_cap * 100, 1),
                "post_reset": True,
            })
            post_budget_left -= alloc

        forecasts[pid] = {
            "plan": prov.get("plan"),
            "weekly_cap": weekly_cap,
            "used_pct": used_pct,
            "remaining": remaining,
            "reset_date": reset_date.strftime("%a %b %d, %I:%M %p"),
            "reset_in_days": round(days_until_reset, 1),
            "daily_budget_pre_reset": round(daily_budget),
            "daily_budget_post_reset": round(weekly_cap / 7),
            "days": days,
        }

    return {"forecasts": forecasts, "generated_at": int(time.time())}


# ═══════════════════════════════════════════════════════════════════════════
# HISTORICAL — time series for streak computation
# ═══════════════════════════════════════════════════════════════════════════


def daily_activity(days: int = 30) -> list[dict]:
    """Per-day activity: active hours and working hours from samples."""
    now = int(time.time())
    today = datetime.fromtimestamp(now).date()
    start_date = today - timedelta(days=days - 1)
    start_ts = int(datetime(start_date.year, start_date.month, start_date.day).timestamp())

    samples = samples_in_range(start_ts, now)
    if not samples:
        return []

    active_mask = _compute_active_mask(samples)

    sample_by_date: dict[str, list[int]] = defaultdict(list)
    for idx, s in enumerate(samples):
        d = datetime.fromtimestamp(s[0]).strftime("%Y-%m-%d")
        sample_by_date[d].append(idx)

    result = []
    for day_offset in range(days):
        d = start_date + timedelta(days=day_offset)
        d_str = d.strftime("%Y-%m-%d")

        day_sample_idxs = sample_by_date.get(d_str, [])
        if not day_sample_idxs:
            result.append({
                "date": d_str,
                "active_hours": 0.0,
                "has_data": False,
            })
            continue

        active_seconds = 0.0
        for k in range(1, len(day_sample_idxs)):
            idx = day_sample_idxs[k]
            prev_idx = day_sample_idxs[k - 1]
            if active_mask[idx]:
                interval = samples[idx][0] - samples[prev_idx][0]
                if interval < 600:
                    active_seconds += interval

        active_hours = round(active_seconds / 3600, 1)

        result.append({
            "date": d_str,
            "active_hours": active_hours,
            "has_data": True,
        })

    return result


# ═══════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════


def _find_last_weekly_reset_ts() -> int | None:
    """Find the timestamp of the most recent weekly reset from sample history.

    Scans recent samples for a drop in weekly_pct > WEEKLY_RESET_THRESHOLD.
    Returns the timestamp of the sample just after the drop.
    """
    samples = last_samples(200)
    if len(samples) < 2:
        return None
    # last_samples is newest-first: samples[0] = newest, samples[-1] = oldest,
    # so samples[i+1] is older than samples[i].
    for i in range(len(samples) - 1):
        older_weekly = samples[i + 1][2]
        newer_weekly = samples[i][2]
        if older_weekly - newer_weekly > WEEKLY_RESET_THRESHOLD:
            return samples[i][0]  # timestamp of the sample right after the drop
    return None


def _estimate_days_until_reset(reset_str: str) -> float | None:
    """Parse weekly reset string to days until that time.

    Handles formats like 'Tue 12:00 AM', 'Apr 7 at 12am', 'Mon 11:59 PM'.
    """
    if not reset_str:
        return None

    # Try day-of-week format: "Tue 12:00 AM"
    day_names = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    match = re.match(r"(\w{3})", reset_str, re.IGNORECASE)
    if match:
        target_day = day_names.get(match.group(1).lower())
        if target_day is not None:
            now = datetime.now()
            current_day = now.weekday()
            delta = (target_day - current_day) % 7
            if delta == 0:
                delta = 7
            return float(delta)

    # Try bare time format: "6pm", "11:00 PM", "6:00PM"
    bare_time = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", reset_str.strip(), re.IGNORECASE)
    if bare_time:
        hour = int(bare_time.group(1))
        minute = int(bare_time.group(2) or 0)
        ampm = bare_time.group(3).upper()
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        delta = (target - now).total_seconds() / 86400
        return max(delta, 0.01)

    # Try date+time format: "Apr 27 6:00 PM" or "Apr 7 at 12am"
    date_match = re.search(r"(\w{3})\s+(\d{1,2})", reset_str)
    if date_match:
        month_names = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                       "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
        month = month_names.get(date_match.group(1).lower())
        day = int(date_match.group(2))
        if month:
            now = datetime.now()
            # Parse optional time: "6:00 PM", "12:00 AM", etc.
            hour, minute = 0, 0
            time_match = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", reset_str, re.IGNORECASE)
            if time_match:
                hour = int(time_match.group(1))
                minute = int(time_match.group(2))
                ampm = time_match.group(3).upper()
                if ampm == "PM" and hour != 12:
                    hour += 12
                elif ampm == "AM" and hour == 12:
                    hour = 0
            try:
                target = datetime(now.year, month, day, hour, minute)
                if target < now:
                    target = datetime(now.year + 1, month, day, hour, minute)
                delta = (target - now).total_seconds() / 86400
                return max(delta, 0.1)
            except ValueError:
                pass

    return None


def _compute_active_mask(samples: list[tuple]) -> list[bool]:
    """Boolean mask: True if sample is during an active usage span."""
    n = len(samples)
    if n == 0:
        return []

    changed = [False] * n
    for i in range(1, n):
        if samples[i][1] != samples[i - 1][1]:
            changed[i] = True

    active = [False] * n

    change_indices = [i for i in range(n) if changed[i]]
    if not change_indices:
        return active

    span_start = change_indices[0]
    for k in range(1, len(change_indices)):
        gap = samples[change_indices[k]][0] - samples[change_indices[k - 1]][0]
        if gap > SESSION_CONTINUITY_SEC:
            last_change_ts = samples[change_indices[k - 1]][0]
            for j in range(span_start, n):
                if samples[j][0] <= last_change_ts + TAIL_ACTIVE_SEC:
                    active[j] = True
                else:
                    break
            span_start = change_indices[k]

    last_change_ts = samples[change_indices[-1]][0]
    for j in range(span_start, n):
        if samples[j][0] <= last_change_ts + TAIL_ACTIVE_SEC:
            active[j] = True
        else:
            break

    return active
