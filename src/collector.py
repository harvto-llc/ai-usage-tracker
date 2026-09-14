#!/usr/bin/env python3
"""Collector that scans local Claude Code JSONL files and Codex SQLite,
with optional subscription quota scraping. Posts everything to /cc/report.

Run every 60s via launchd. Subscription quota scrapes run every 5 minutes.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from src import fleet_push, focus_emit
from src.database import load_claude_code_stats, sync
from src.entitlements import resolve_plan
from src.plan_config import load_plans
from src.provider_metrics import (
    build_provider_snapshots,
    scan_codex_session_metrics,
    scan_codex_thread_stats,
    shape_claude_code_stats,
)
from src.pty_scraper import (
    claude_web_usage_configured,
    codex_web_analytics_configured,
    scrape_claude_usage_web,
    scrape_codex_analytics,
    scrape_codex_usage,
    scrape_cursor_usage,
)
from src.scanners import (
    scan_cc_messages_today,
    scan_cc_projects_today,
    scan_cc_tokens_today,
)

API_URL = "http://localhost:8000/cc/report"
ACCESS_SUBSCRIPTION = "subscription"
ACCESS_API = "api"
ACCESS_ALIASES = {
    "subscription": ACCESS_SUBSCRIPTION,
    "subscription-access": ACCESS_SUBSCRIPTION,
    "full": ACCESS_SUBSCRIPTION,  # legacy USAGE_TRACKER_MODE / --mode value
    "api": ACCESS_API,
    "api-based": ACCESS_API,
    "api-based-access": ACCESS_API,
    "api-key": ACCESS_API,
    "enterprise": ACCESS_API,
    "local": ACCESS_API,  # legacy USAGE_TRACKER_MODE / --mode value
}


def _normalize_access(value: str | None) -> str:
    raw = (value or ACCESS_SUBSCRIPTION).strip().lower().replace("_", "-")
    try:
        return ACCESS_ALIASES[raw]
    except KeyError as exc:
        allowed = "subscription, api"
        raise ValueError(f"Invalid access type {value!r}; expected {allowed}") from exc


def _default_access() -> str:
    if os.environ.get("USAGE_TRACKER_ACCESS"):
        return _normalize_access(os.environ["USAGE_TRACKER_ACCESS"])
    if os.environ.get("USAGE_TRACKER_MODE"):
        return _normalize_access(os.environ["USAGE_TRACKER_MODE"])
    return ACCESS_SUBSCRIPTION


def scan_codex_sessions() -> dict:
    """Scan Codex session JSONL files for today's usage — messages, tokens, active hours."""
    metrics = scan_codex_session_metrics()
    return {
        # Emit the canonical Codex session contract directly. The API still
        # accepts legacy aliases during the compatibility window.
        "active_hours_today": metrics["active_hours_today"],
        "messages_today": metrics["messages_today"],
        "sessions_today": metrics["sessions_today"],
        "total_sessions": metrics["total_sessions"],
        "input_tokens_today": metrics["input_tokens_today"],
        "output_tokens_today": metrics["output_tokens_today"],
        "reasoning_tokens_today": metrics["reasoning_tokens_today"],
        "cached_tokens_today": metrics["cached_tokens_today"],
        "user_messages_today": metrics["user_messages_today"],
        "events_scanned": metrics["events_scanned"],
    }


USAGE_STAMP = Path.home() / ".usage-tracker" / "collector-stamp"
USAGE_STAMP.parent.mkdir(parents=True, exist_ok=True)
USAGE_INTERVAL = 300  # 5 minutes


def _should_scrape_usage() -> bool:
    """Only scrape PTY usage every 5 minutes (it takes ~30s)."""
    if not USAGE_STAMP.exists():
        return True
    try:
        age = time.time() - USAGE_STAMP.stat().st_mtime
        return age >= USAGE_INTERVAL
    except OSError:
        return True


def _cached_usage_for_snapshots() -> tuple[dict | None, dict | None]:
    try:
        cached = json.loads(USAGE_STAMP.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(cached, dict):
        return None, None
    usage = cached.get("usage") if isinstance(cached.get("usage"), dict) else None
    codex_usage = cached.get("codex_usage") if isinstance(cached.get("codex_usage"), dict) else None
    return usage, codex_usage


def _claude_usage_source() -> str:
    source = os.environ.get("CLAUDE_USAGE_SOURCE", "auto").strip().lower()
    if source == "tmux":
        print(
            "CLAUDE_USAGE_SOURCE=tmux is no longer supported; the PTY poller "
            "was removed. Using web.",
            file=sys.stderr,
        )
        return "auto"
    if source in {"auto", "web"}:
        return source
    return "auto"


def _scrape_claude_usage() -> tuple[dict | None, str]:
    # Claude quota comes only from noninteractive sources. There is no PTY
    # fallback: launching the Claude CLI to scrape /usage writes into the
    # same session history this tracker analyzes.
    source = _claude_usage_source()

    web_ready = False
    try:
        web_ready = claude_web_usage_configured()
    except RuntimeError as exc:
        print(f"Claude web config invalid: {exc}", file=sys.stderr)

    if not web_ready and source != "web":
        return None, "web"

    try:
        usage = scrape_claude_usage_web()
        if usage:
            return usage, "web"
    except Exception as exc:
        print(f"Claude web scrape failed: {exc}", file=sys.stderr)
    return None, "web"


def main(access: str | None = None, mode: str | None = None):
    """Run one collection cycle.

    access:
      "subscription" — local scans plus subscription quota scrapes.
      "api"          — local scans only. For Bedrock, Vertex, OpenAI
                       enterprise, or any setup without subscription quota
                       pages/cookies. Token/model/activity stats keep working;
                       quota gauges come from self_quota when configured.

    The legacy mode values "full" and "local" are accepted as aliases.
    """
    if access is not None:
        selected = access
    elif mode is not None:
        selected = mode
    else:
        selected = _default_access()
    access_type = _normalize_access(selected)
    messages = scan_cc_messages_today()
    tokens = scan_cc_tokens_today()
    projects = scan_cc_projects_today()
    codex_local = scan_codex_thread_stats()
    codex_sessions = scan_codex_sessions()
    cc_stats = shape_claude_code_stats(load_claude_code_stats())

    # Slow cycle: subscription quota scrapes (every 5 min). Skipped for API-based access.
    usage = None
    codex_usage = None
    codex_analytics = None
    cursor_usage = None
    scrape_errors: dict[str, str] = {}
    if access_type == ACCESS_SUBSCRIPTION and _should_scrape_usage():
        sync()  # Pull/Push from Turso
        usage, usage_source = _scrape_claude_usage()
        if usage:
            usage.update(resolve_plan("claude", usage.get("raw_plan"), plans=load_plans()))
            print(f"Claude {usage_source}: session={usage.get('session_pct')}% weekly={usage.get('weekly_pct')}%")
        else:
            print(f"Claude {usage_source}: no data parsed", file=sys.stderr)

        try:
            codex_usage = scrape_codex_usage()
            if codex_usage:
                codex_usage.update(resolve_plan("codex", codex_usage.get("raw_plan"), plans=load_plans()))
                print(f"PTY Codex: 5h={codex_usage.get('session_remaining_pct')}% weekly={codex_usage.get('weekly_remaining_pct')}%")
        except Exception as e:
            scrape_errors["codex"] = str(e)
            print(f"PTY Codex scrape failed: {e}", file=sys.stderr)

        if codex_web_analytics_configured():
            try:
                codex_analytics = scrape_codex_analytics()
                if codex_analytics:
                    summary = codex_analytics.get("summary", {})
                    workspace = summary.get("workspace", {})
                    review = summary.get("code_review", {})
                    print(
                        "Codex analytics:"
                        f" users/day={workspace.get('avg_daily_users', 0)}"
                        f" turns/day={workspace.get('avg_daily_turns', 0)}"
                        f" reviews/day={review.get('avg_daily_reviews', 0)}"
                    )
            except Exception as e:
                scrape_errors["codex_analytics"] = str(e)
                print(f"Codex analytics fetch failed: {e}", file=sys.stderr)

        # Cursor usage
        try:
            cursor_usage = scrape_cursor_usage()
            if cursor_usage:
                print(f"Cursor: {cursor_usage.get('plan', '?')} plan, {cursor_usage.get('total_requests', 0)} reqs")
        except Exception as e:
            scrape_errors["cursor"] = str(e)
            print(f"Cursor fetch failed: {e}", file=sys.stderr)

        # Cache results
        cache = {"usage": usage, "codex_usage": codex_usage, "codex_analytics": codex_analytics}
        USAGE_STAMP.write_text(json.dumps(cache))
    # Fast cycle — don't resend usage/codex_usage/codex_analytics to avoid
    # duplicate DB rows and redundant large analytics payloads. The API keeps
    # the last-posted values in memory (_remote_cc) so they stay available
    # for /stats responses without re-inserting into the DB.

    snapshot_usage = usage
    snapshot_codex_usage = codex_usage
    if snapshot_usage is None or snapshot_codex_usage is None:
        cached_usage, cached_codex_usage = _cached_usage_for_snapshots()
        snapshot_usage = snapshot_usage or cached_usage
        snapshot_codex_usage = snapshot_codex_usage or cached_codex_usage

    provider_snapshots = build_provider_snapshots(
        timestamp=int(time.time()),
        collector_access=access_type,
        messages=messages,
        tokens=tokens,
        usage=snapshot_usage,
        codex_local=codex_local,
        codex_sessions=codex_sessions,
        codex_usage=snapshot_codex_usage,
        cursor_usage=cursor_usage,
        errors=scrape_errors,
    )

    payload = json.dumps({
        "messages": messages,
        "tokens": tokens,
        "projects": projects,
        "codex_local": codex_local,
        "codex_sessions": codex_sessions,
        "cc_stats": cc_stats,
        "usage": usage,
        "codex_usage": codex_usage,
        "codex_analytics": codex_analytics,
        "cursor_usage": cursor_usage,
        "provider_snapshots": provider_snapshots,
    }).encode()

    # Fleet push runs BEFORE the local POST, and the ordering is the point. The local
    # POST below exits the process with status 1 when the API is down, so a fleet step
    # placed after it would be skipped exactly when buffering matters most. It buffers
    # first and pushes second, it never raises, and it returns a state naming what it
    # decided — including "unconfigured", which is the normal condition today because no
    # receiver exists yet. See src/fleet_push.py.
    fleet = fleet_push.record_cycle([
        ("usage_tracker.claude.messages.today", messages.get("total_messages")),
        ("usage_tracker.claude.tokens.today", tokens.get("total_tokens")),
        ("usage_tracker.claude.projects.today", len(projects)),
        ("usage_tracker.codex.messages.today", codex_sessions.get("messages_today")),
        ("usage_tracker.codex.tokens.today", codex_sessions.get("input_tokens_today"),
         {"type": "input"}),
        ("usage_tracker.codex.tokens.today", codex_sessions.get("output_tokens_today"),
         {"type": "output"}),
    ])
    print(f"Fleet push: {fleet['state']} ({fleet['reason']})")

    # FOCUS emission runs here for the same reason fleet push does: BEFORE the local POST,
    # which exits the process with status 1 when the API is down. It reads today's
    # provider-days out of the ledger the scan above has just updated, so it publishes the
    # day that was collected rather than the day before it. Like fleet push it never
    # raises and returns a state naming what it decided. See src/focus_emit.py.
    focus_cycle = focus_emit.record_cycle()
    print(f"FOCUS emit: {focus_cycle['state']} ({focus_cycle['reason']})")

    headers = {"Content-Type": "application/json"}
    api_secret = os.environ.get("USAGE_TRACKER_SECRET", "")
    if api_secret:
        headers["Authorization"] = f"Bearer {api_secret}"

    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"OK: {messages['total_messages']} msgs, {tokens['total_tokens']} tokens, {len(projects)} projects")
            sync()  # Push local changes to Turso
    except Exception as e:
        print(f"Error posting to API: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Usage tracker collector (one cycle).")
    parser.add_argument(
        "--access",
        choices=[ACCESS_SUBSCRIPTION, ACCESS_API],
        default=None,
        help=(
            "subscription: local scans + web/PTY quota scrapes (default). "
            "api: scans only; use for Bedrock, Vertex, OpenAI enterprise, "
            "or API-key setups. Default can also be set with "
            "USAGE_TRACKER_ACCESS."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["full", "local"],
        default=None,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    try:
        selected_access = args.access or args.mode or _default_access()
        main(access=selected_access)
    except ValueError as exc:
        parser.error(str(exc))
