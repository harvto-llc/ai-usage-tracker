"""Read-only command line client for the local Usage Tracker API."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


PROVIDERS = ("claude", "codex")


class CLIError(RuntimeError):
    pass


def load_api_secret(config_path: Path | None = None) -> str:
    secret = os.environ.get("USAGE_TRACKER_SECRET", "").strip()
    if secret:
        return secret
    path = config_path or Path.home() / ".usage-tracker" / "config"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip() == "USAGE_TRACKER_SECRET" and value.strip():
            return value.strip()
    raise CLIError("Missing local API token. Configure ~/.usage-tracker/config first.")


def fetch_api_json(
    api_url: str,
    secret: str,
    path: str,
    opener=urllib.request.urlopen,
) -> dict:
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"},
    )
    try:
        with opener(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise CLIError("Local API rejected the configured token.") from exc
        raise CLIError(f"Local API returned HTTP {exc.code}.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CLIError(f"Could not read the local Usage Tracker API: {exc}") from exc


def fetch_stats(api_url: str, secret: str, opener=urllib.request.urlopen) -> dict:
    return fetch_api_json(api_url, secret, "/stats", opener=opener)


def fetch_explanations(
    api_url: str,
    secret: str,
    provider: str,
    period: str,
    opener=urllib.request.urlopen,
) -> dict:
    return {
        name: fetch_api_json(
            api_url,
            secret,
            f"/usage/explanation?provider={name}&period={period}",
            opener=opener,
        )
        for name in _selected_providers(provider)
    }


def _selected_providers(provider: str) -> tuple[str, ...]:
    return PROVIDERS if provider == "all" else (provider,)


def _number(value, suffix: str = "") -> str:
    if value is None:
        return "-"
    number = float(value)
    rendered = f"{number:.1f}".rstrip("0").rstrip(".")
    return f"{rendered}{suffix}"


def _tokens(value) -> str:
    if value is None:
        return "-"
    number = float(value)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if abs(number) >= divisor:
            return f"{number / divisor:.1f}{suffix}"
    return str(int(number))


def _money(value) -> str:
    return "-" if value is None else f"${float(value):,.2f}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "No matching data."
    widths = [len(header) for header in headers]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    lines = ["  ".join(header.ljust(width) for header, width in zip(headers, widths))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend("  ".join(cell.ljust(width) for cell, width in zip(row, widths)) for row in rows)
    return "\n".join(lines)


def usage_data(stats: dict, provider: str) -> dict:
    return {
        name: {
            "quota": stats.get(f"{name}_quota"),
            "health": (stats.get("provider_health") or {}).get(name),
        }
        for name in _selected_providers(provider)
    }


def render_usage(stats: dict, provider: str) -> str:
    rows: list[list[str]] = []
    credit_rows: list[list[str]] = []
    for name, data in usage_data(stats, provider).items():
        quota = data["quota"] or {}
        health = ((data["health"] or {}).get("quota") or {}).get("status", "unknown")
        for limit in quota.get("limits") or []:
            used = limit.get("used_pct")
            remaining = limit.get("remaining_pct")
            if used is None and remaining is not None:
                used = 100 - float(remaining)
            if remaining is None and used is not None:
                remaining = 100 - float(used)
            rows.append([
                name.title(),
                str(quota.get("plan_label") or "Unknown"),
                str(limit.get("label") or limit.get("id") or "Quota"),
                _number(used, "%"),
                _number(remaining, "%"),
                str(limit.get("reset") or "-"),
                str(health),
            ])
        for pool in quota.get("credit_pools") or []:
            credit_rows.append([
                name.title(),
                str(pool.get("label") or pool.get("id") or "Credits"),
                _number(pool.get("balance")),
                str(pool.get("unit") or "credits"),
                str(pool.get("kind") or "reported"),
            ])
    output = _table(["Provider", "Plan", "Limit", "Used", "Left", "Reset", "Status"], rows)
    if credit_rows:
        output += "\n\nReported balances\n"
        output += _table(["Provider", "Pool", "Balance", "Unit", "Kind"], credit_rows)
    return output


def cost_data(stats: dict, provider: str, period: str) -> dict:
    periods = stats.get("provider_periods") or {}
    return {
        name: (periods.get(name) or {}).get(period)
        for name in _selected_providers(provider)
    }


def render_cost(stats: dict, provider: str, period: str) -> str:
    rows = []
    for name, metrics in cost_data(stats, provider, period).items():
        metrics = metrics or {}
        rows.append([
            name.title(),
            "Today" if period == "day" else "Quota week",
            _tokens(metrics.get("total_tokens")),
            str(metrics.get("requests") if metrics.get("requests") is not None else "-"),
            _money(metrics.get("estimated_cost_usd")),
            _number(metrics.get("estimated_credits")),
            _number(metrics.get("pricing_coverage_pct"), "%"),
        ])
    return _table(
        ["Provider", "Period", "Tokens", "Requests", "Est. value", "Est. credits", "Coverage"],
        rows,
    )


def history_data(stats: dict, provider: str, days: int) -> dict:
    trends = stats.get("provider_trends") or {}
    return {
        name: ((trends.get(name) or {}).get("points") or [])[-days:]
        for name in _selected_providers(provider)
    }


def render_history(stats: dict, provider: str, days: int) -> str:
    rows = []
    for name, points in history_data(stats, provider, days).items():
        for point in points:
            rows.append([
                str(point.get("date") or "-"),
                name.title(),
                _tokens(point.get("total_tokens")),
                str(point.get("requests") if point.get("requests") is not None else "-"),
                _money(point.get("estimated_cost_usd")),
                _number(point.get("estimated_credits")),
                _number(point.get("pricing_coverage_pct"), "%"),
            ])
    return _table(
        ["Date", "Provider", "Tokens", "Requests", "Est. value", "Est. credits", "Coverage"],
        rows,
    )


def render_explanations(explanations: dict) -> str:
    rows = []
    notes = []
    for provider, report in explanations.items():
        totals = report.get("totals") or {}
        for category in (report.get("categories") or [])[:8]:
            rows.append([
                provider.title(),
                str(category.get("label") or category.get("id") or "Other"),
                _number(category.get("share_pct"), "%"),
                _tokens(category.get("total_tokens")),
                _tokens(category.get("effective_tokens")),
                _money(category.get("estimated_cost_usd")),
                _number(category.get("estimated_credits")),
            ])
        coverage = report.get("coverage") or {}
        notes.append(
            f"{provider.title()}: {_tokens(totals.get('total_tokens'))} raw tokens, "
            f"{_tokens(totals.get('effective_tokens'))} effective, "
            f"{_number(coverage.get('classified_pct'), '%')} classified; local estimate, not quota debit."
        )
    table = _table(
        ["Provider", "Category", "Share", "Raw tokens", "Effective", "Est. value", "Est. credits"],
        rows,
    )
    return table + ("\n\n" + "\n".join(notes) if notes else "")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="usage-tracker", description="Read local Claude and Codex usage.")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("USAGE_TRACKER_API_URL", "http://127.0.0.1:8000"),
        help="local Usage Tracker API URL",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    usage = subparsers.add_parser("usage", help="show provider quota windows")
    usage.add_argument("--provider", choices=(*PROVIDERS, "all"), default="all")

    cost = subparsers.add_parser("cost", help="show estimated activity value")
    cost.add_argument("--provider", choices=(*PROVIDERS, "all"), default="all")
    cost.add_argument("--period", choices=("day", "week"), default="day")

    history = subparsers.add_parser("history", help="show daily usage history")
    history.add_argument("--provider", choices=(*PROVIDERS, "all"), default="all")
    history.add_argument("--days", type=int, choices=(7, 30), default=7)

    explain = subparsers.add_parser("explain", help="explain where local token usage went")
    explain.add_argument("--provider", choices=(*PROVIDERS, "all"), default="all")
    explain.add_argument("--period", choices=("day", "week"), default="day")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        secret = load_api_secret()
        if args.command == "explain":
            data = fetch_explanations(
                args.api_url,
                secret,
                args.provider,
                args.period,
            )
            output = json.dumps(data, indent=2, sort_keys=True) if args.json else render_explanations(data)
            print(output)
            return 0
        stats = fetch_stats(args.api_url, secret)
        if args.command == "usage":
            data = usage_data(stats, args.provider)
            output = json.dumps(data, indent=2, sort_keys=True) if args.json else render_usage(stats, args.provider)
        elif args.command == "cost":
            data = cost_data(stats, args.provider, args.period)
            output = json.dumps(data, indent=2, sort_keys=True) if args.json else render_cost(stats, args.provider, args.period)
        else:
            data = history_data(stats, args.provider, args.days)
            output = json.dumps(data, indent=2, sort_keys=True) if args.json else render_history(stats, args.provider, args.days)
        print(output)
        return 0
    except CLIError as exc:
        print(f"usage-tracker: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
