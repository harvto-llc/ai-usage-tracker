"""Scrapers for AI tool usage data:
- Claude Code: claude.ai web usage endpoint
- Codex: app-server JSON-RPC
- Cursor: api2.cursor.sh REST API
"""

import json
import os
import plistlib
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.credential_store import load_provider_credential

def _find_bin(name: str) -> str:
    """Find binary via PATH, with Homebrew fallback."""
    found = shutil.which(name)
    if found:
        return found
    # Homebrew defaults
    for prefix in ["/opt/homebrew/bin", "/usr/local/bin"]:
        path = f"{prefix}/{name}"
        if os.path.exists(path):
            return path
    return name  # fall through to PATH


def _strip_ansi(text):
    return re.sub(r'\x1b[\[\]()>=#][0-9;?]*[a-zA-Z\x07]?|\x1b.', '', text)


def _clean(text):
    return _strip_ansi(text).replace('\r\n', '\n').replace('\r', '')


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_json_key(key: str) -> str:
    key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    key = re.sub(r"[^a-zA-Z0-9]+", "_", key)
    return key.strip("_").lower()


def _iter_nodes(node, path=()):
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_nodes(value, path + (_normalize_json_key(key),))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            yield from _iter_nodes(value, path + (f"item_{idx}",))


def _iter_leaves(node, path=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_leaves(value, path + (_normalize_json_key(key),))
        return
    if isinstance(node, list):
        for idx, value in enumerate(node):
            yield from _iter_leaves(value, path + (f"item_{idx}",))
        return
    yield path, node


def _path_tokens(path: tuple[str, ...]) -> list[str]:
    tokens = []
    for part in path:
        tokens.extend(token for token in part.split("_") if token)
    return tokens


def _read_env_or_file(
    env_name: str,
    file_env_name: str,
    fallback_file: Path | None = None,
) -> str | None:
    value = os.environ.get(env_name, "").strip()
    if value:
        return value

    file_value = os.environ.get(file_env_name, "").strip()
    paths = [Path(file_value).expanduser()] if file_value else []
    if fallback_file is not None:
        paths.append(fallback_file.expanduser())
    for path in paths:
        try:
            content = path.read_text().strip()
        except OSError:
            continue
        if content:
            return content
    return None


def _read_provider_cookie(
    provider: str,
    env_name: str,
    file_env_name: str,
    fallback_file: Path,
) -> str | None:
    keychain_value = load_provider_credential(provider)
    if keychain_value:
        try:
            fallback_file.expanduser().unlink(missing_ok=True)
        except OSError:
            pass
        return keychain_value
    return _read_env_or_file(
        env_name,
        file_env_name,
        fallback_file,
    )


def _normalize_cookie_header(cookie_text: str) -> str:
    cookie_text = cookie_text.strip()
    if cookie_text.lower().startswith("cookie:"):
        cookie_text = cookie_text.split(":", 1)[1].strip()
    return cookie_text


def _parse_cookie_header(cookie_text: str) -> dict[str, str]:
    cookies = {}
    if not cookie_text:
        return cookies
    for part in _normalize_cookie_header(cookie_text).split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        cookies[name] = value.strip()
    return cookies


def _cookie_name_present(cookies: dict[str, str], name: str) -> bool:
    return name in cookies or any(key.startswith(f"{name}.") for key in cookies)


def _valid_claude_cookie_header(cookie_text: str | None) -> bool:
    if not cookie_text:
        return False
    cookies = _parse_cookie_header(cookie_text)
    return _cookie_name_present(cookies, "sessionKey") and bool(cookies.get("lastActiveOrg"))


def _installed_chrome_user_agent() -> str:
    configured = os.environ.get("CLAUDE_WEB_USER_AGENT", "").strip()
    if configured:
        return configured
    info_path = Path("/Applications/Google Chrome.app/Contents/Info.plist")
    try:
        with info_path.open("rb") as handle:
            version = str(plistlib.load(handle).get("CFBundleShortVersionString") or "").strip()
    except (OSError, plistlib.InvalidFileException):
        version = ""
    version = version or "146.0.0.0"
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )


def _load_web_header_overrides(
    raw_env_name: str,
    file_env_name: str,
    label: str,
) -> dict[str, str]:
    raw = os.environ.get(raw_env_name, "").strip()
    if not raw:
        path = os.environ.get(file_env_name, "").strip()
        if path:
            try:
                raw = Path(path).expanduser().read_text().strip()
            except OSError as exc:
                raise RuntimeError(f"could not read {file_env_name}: {exc}") from exc
    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid {label} web headers JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} web headers must be a JSON object")

    headers = {}
    for key, value in data.items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            headers[str(key)] = text
    return headers


def _load_claude_web_header_overrides() -> dict[str, str]:
    return _load_web_header_overrides(
        "CLAUDE_WEB_HEADERS_JSON",
        "CLAUDE_WEB_HEADERS_FILE",
        "Claude",
    )


def _load_codex_web_header_overrides() -> dict[str, str]:
    return _load_web_header_overrides(
        "CODEX_WEB_HEADERS_JSON",
        "CODEX_WEB_HEADERS_FILE",
        "Codex",
    )


def _format_reset_time(value) -> str | None:
    if value is None:
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = float(value)
        if ts > 1_000_000_000_000:
            ts /= 1000.0
        dt = datetime.fromtimestamp(ts).astimezone()
        return dt.strftime("%b %-d %-I:%M %p")

    if not isinstance(value, str):
        return None

    text = _normalize_whitespace(value)
    if not text:
        return None

    iso_text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_text).astimezone()
        return dt.strftime("%b %-d %-I:%M %p")
    except ValueError:
        return text


def _reset_epoch(value) -> int | None:
    """Preserve a machine-readable reset instant alongside display text."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = float(value)
        if ts > 1_000_000_000_000:
            ts /= 1000.0
        return int(ts)
    if not isinstance(value, str) or not value.strip():
        return None
    text = _normalize_whitespace(value).replace(",", "").replace(" at ", " ")
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        pass

    now = datetime.now()
    try:
        return int(datetime.strptime(text, "%b %d %Y %I:%M %p").timestamp())
    except ValueError:
        pass

    try:
        parsed = datetime.strptime(f"{now.year} {text}", "%Y %b %d %I:%M %p")
        if parsed < now - timedelta(days=1):
            parsed = parsed.replace(year=now.year + 1)
        return int(parsed.timestamp())
    except ValueError:
        pass

    try:
        parsed = datetime.strptime(
            f"{now.year}-{now.month}-{now.day} {text}",
            "%Y-%m-%d %I:%M %p",
        )
        if parsed <= now:
            parsed += timedelta(days=1)
        return int(parsed.timestamp())
    except ValueError:
        return None


def _pick_section(
    payload,
    include_tokens: tuple[str, ...],
    exclude_tokens: tuple[str, ...] = (),
    minimum_hits: int = 1,
) -> dict | None:
    candidates = []
    for path, node in _iter_nodes(payload):
        if not isinstance(node, dict) or not node:
            continue
        tokens = _path_tokens(path)
        if any(token in tokens for token in exclude_tokens):
            continue
        hits = sum(1 for token in include_tokens if token in tokens)
        if hits < minimum_hits:
            continue
        candidates.append((hits, len(path), node))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def _find_value_in_section(
    section,
    wanted_tokens: tuple[str, ...],
    *,
    value_predicate,
    forbidden_tokens: tuple[str, ...] = (),
):
    candidates = []
    for path, value in _iter_leaves(section):
        tokens = _path_tokens(path)
        if any(token in tokens for token in forbidden_tokens):
            continue
        if not value_predicate(value):
            continue
        hits = sum(1 for token in wanted_tokens if token in tokens)
        if hits < 1:
            continue
        candidates.append((hits, len(path), value))

    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]
    return None


def _merge_usage_pct_reset(
    result: dict,
    section,
    *,
    pct_key: str,
    pct_field: str,
    reset_key: str | None = None,
    reset_field: str | None = None,
) -> None:
    if not isinstance(section, dict):
        return

    pct_value = section.get(pct_field)
    if isinstance(pct_value, (int, float)) and not isinstance(pct_value, bool):
        result[pct_key] = int(round(float(pct_value)))

    if reset_key and reset_field:
        reset_value = _format_reset_time(section.get(reset_field))
        if reset_value:
            result[reset_key] = reset_value


def _numeric_percent(section: dict | None) -> float | None:
    if not isinstance(section, dict):
        return None
    for key in ("utilization", "usedPercent", "used_pct", "percentUsed"):
        value = section.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100:
            return float(value)
    return None


def _claude_money_value(section: dict, value) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    decimal_places = section.get("decimal_places")
    if isinstance(decimal_places, int) and not isinstance(decimal_places, bool) and decimal_places >= 0:
        return float(value) / (10 ** decimal_places)
    return float(value)


def _quota_bucket(
    bucket_id: str,
    label: str,
    section: dict | None,
    *,
    window_kind: str,
    scope_kind: str = "aggregate",
    model: str | None = None,
    feature: str | None = None,
) -> dict | None:
    used = _numeric_percent(section)
    if used is None:
        return None
    raw_reset = (
        (section or {}).get("resets_at")
        or (section or {}).get("resetAt")
        or (section or {}).get("resetsAt")
    )
    reset = _format_reset_time(raw_reset)
    return {
        "id": bucket_id,
        "label": label,
        "window_kind": window_kind,
        "scope_kind": scope_kind,
        "model": model,
        "feature": feature,
        "used_pct": used,
        "remaining_pct": 100 - used,
        "reset": reset,
        "reset_at": _reset_epoch(raw_reset),
    }


def _claude_plan_value(payload: dict) -> object:
    for key in ("plan_type", "planType", "subscription_plan", "subscriptionPlan", "plan"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key in ("subscription", "account", "organization"):
        section = payload.get(key)
        if not isinstance(section, dict):
            continue
        for nested_key in ("plan_type", "planType", "plan", "tier"):
            value = section.get(nested_key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _claude_structured_limits(payload: dict) -> list[dict]:
    rows = payload.get("limits")
    if not isinstance(rows, list):
        return []
    buckets = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        used = row.get("percent")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            continue
        group = _normalize_json_key(row.get("group") or row.get("kind") or "unknown")
        scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
        model_scope = scope.get("model") if isinstance(scope.get("model"), dict) else {}
        surface_scope = scope.get("surface") if isinstance(scope.get("surface"), dict) else {}
        model_name = str(model_scope.get("display_name") or "").strip()
        surface_name = str(surface_scope.get("display_name") or "").strip()
        if model_name:
            scope_kind = "model"
            scope_name = model_name
        elif surface_name:
            scope_kind = "feature"
            scope_name = surface_name
        else:
            scope_kind = "aggregate"
            scope_name = ""
        if group == "session":
            window_kind = "session"
        elif group == "weekly" or "weekly" in group or "seven_day" in group:
            window_kind = "weekly"
        elif "month" in group:
            window_kind = "monthly"
        else:
            window_kind = "unknown"
        suffix = _slug(scope_name) if scope_name else window_kind
        label = f"{scope_name} weekly" if scope_name and window_kind == "weekly" else (
            scope_name or window_kind.title()
        )
        raw_reset = row.get("resets_at") or row.get("resetAt")
        buckets.append({
            "id": (
                f"claude-{window_kind}"
                if scope_kind == "aggregate"
                else f"claude-{window_kind}-{suffix}"
            ),
            "label": label,
            "window_kind": window_kind,
            "scope_kind": scope_kind,
            "model": _slug(model_name) if model_name else None,
            "feature": _slug(surface_name) if surface_name else None,
            "used_pct": float(used),
            "remaining_pct": 100 - float(used),
            "reset": _format_reset_time(raw_reset),
            "reset_at": _reset_epoch(raw_reset),
        })
    return buckets


def _next_month_reset(now: datetime | None = None) -> str:
    current = (now or datetime.now().astimezone()).astimezone()
    if current.month == 12:
        reset = current.replace(year=current.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        reset = current.replace(month=current.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return reset.isoformat()


def _merge_claude_browser_bundle(bundle: dict, now: datetime | None = None) -> dict:
    usage = bundle.get("usage")
    if not isinstance(usage, dict):
        return {}
    payload = dict(usage)
    existing_extra = payload.get("extra_usage")
    extra = dict(existing_extra) if isinstance(existing_extra, dict) else {}

    overage_wrapper = bundle.get("overage")
    overage = overage_wrapper.get("data") if isinstance(overage_wrapper, dict) else None
    if isinstance(overage, dict):
        extra.update({
            "is_enabled": overage.get("is_enabled"),
            "monthly_limit": overage.get("monthly_credit_limit"),
            "used_credits": overage.get("used_credits"),
            "currency": overage.get("currency") or extra.get("currency"),
            "out_of_credits": overage.get("out_of_credits"),
            "decimal_places": 2,
        })

    prepaid_wrapper = bundle.get("prepaid")
    prepaid = prepaid_wrapper.get("data") if isinstance(prepaid_wrapper, dict) else None
    if isinstance(prepaid, dict):
        if isinstance(prepaid.get("amount"), (int, float)):
            extra["balanceUsd"] = prepaid["amount"]
            extra.setdefault("decimal_places", 2)
        extra["next_expiry_at"] = prepaid.get("next_expires_at")
        extra["promo_tranches"] = prepaid.get("promo_tranches") or []

    if extra:
        extra.setdefault("resets_at", _next_month_reset(now))
        payload["extra_usage"] = extra
    return payload


def _finalize_claude_usage_payload(payload: dict | list, result: dict) -> dict:
    if not isinstance(payload, dict):
        return result

    limits: list[dict] = []

    def add(bucket: dict | None) -> None:
        if bucket and bucket["id"] not in {item["id"] for item in limits}:
            limits.append(bucket)

    session = payload.get("five_hour") or payload.get("currentSession")
    add(_quota_bucket("claude-session", "Session", session, window_kind="session"))
    weekly = payload.get("weekly") if isinstance(payload.get("weekly"), dict) else {}
    weekly_all = payload.get("seven_day") or weekly.get("allModels")
    weekly_sonnet = payload.get("seven_day_sonnet") or weekly.get("sonnetOnly")
    weekly_design = payload.get("seven_day_design") or weekly.get("designOnly")
    add(_quota_bucket("claude-weekly", "Weekly", weekly_all, window_kind="weekly"))
    add(_quota_bucket(
        "claude-weekly-sonnet", "Sonnet weekly", weekly_sonnet,
        window_kind="weekly", scope_kind="model", model="claude-sonnet",
    ))
    add(_quota_bucket(
        "claude-weekly-design", "Design weekly", weekly_design,
        window_kind="weekly", scope_kind="feature", feature="design",
    ))
    for bucket in _claude_structured_limits(payload):
        add(bucket)

    known = {
        "five_hour", "currentSession", "seven_day", "seven_day_sonnet",
        "seven_day_design", "weekly", "extra_usage", "extraUsage", "limits", "spend",
    }
    for key, section in payload.items():
        if key in known or not isinstance(section, dict) or _numeric_percent(section) is None:
            continue
        normalized = _normalize_json_key(key)
        if "month" in normalized or "thirty_day" in normalized or "30_day" in normalized:
            kind = "monthly"
        elif "week" in normalized or "seven_day" in normalized:
            kind = "weekly"
        elif "hour" in normalized or "session" in normalized:
            kind = "session"
        else:
            kind = "unknown"
        scope = "model" if any(token in normalized for token in ("opus", "sonnet", "haiku", "fable")) else "feature"
        add(_quota_bucket(
            f"claude-{normalized.replace('_', '-')}",
            normalized.replace("_", " ").title(),
            section,
            window_kind=kind,
            scope_kind=scope,
            model=normalized.replace("_", "-") if scope == "model" else None,
            feature=normalized.replace("_", "-") if scope == "feature" else None,
        ))

    if limits:
        result["limits"] = limits

    extra = payload.get("extra_usage")
    if not isinstance(extra, dict):
        extra = payload.get("extraUsage")
    if isinstance(extra, dict):
        balance = extra.get("balanceUsd")
        spent = extra.get("used_credits") if extra.get("used_credits") is not None else extra.get("spentUsd")
        cap = extra.get("monthly_limit") if extra.get("monthly_limit") is not None else extra.get("limitUsd")
        normalized_balance = _claude_money_value(extra, balance)
        normalized_spent = _claude_money_value(extra, spent)
        normalized_cap = _claude_money_value(extra, cap)
        out_of_credits = extra.get("out_of_credits")
        spend_control_reached = extra.get("spend_limit_reached")
        if spend_control_reached is None and isinstance(out_of_credits, bool):
            spend_control_reached = bool(
                out_of_credits
                and normalized_cap is not None
                and normalized_spent is not None
                and normalized_spent >= normalized_cap - 0.005
            )
        pool = {
            "id": "claude-usage-credits",
            "label": "Usage credits",
            "kind": "prepaid",
            "unit": str(extra.get("currency") or "usd").lower(),
            "enabled": extra.get("is_enabled") if "is_enabled" in extra else extra.get("isEnabled"),
            "balance": normalized_balance,
            "spent_month": normalized_spent,
            "monthly_spend_cap": normalized_cap,
            "monthly_spend_headroom": (
                max(normalized_cap - normalized_spent, 0.0)
                if normalized_cap is not None and normalized_spent is not None
                else None
            ),
            "spend_control_reached": spend_control_reached,
            "out_of_credits": out_of_credits,
            "used_pct": _numeric_percent(extra),
            "reset": _format_reset_time(extra.get("resets_at") or extra.get("resetAt")),
            "next_expiry_at": _reset_epoch(extra.get("next_expiry_at")),
            "unlimited": bool(
                (extra.get("is_enabled") if "is_enabled" in extra else extra.get("isEnabled"))
                and cap is None
            ),
        }
        pools = [pool]
        promo_tranches = extra.get("promo_tranches")
        if isinstance(promo_tranches, list):
            for index, tranche in enumerate(promo_tranches):
                if not isinstance(tranche, dict):
                    continue
                amount = tranche.get("remaining_amount_minor_units")
                if not isinstance(amount, (int, float)) or isinstance(amount, bool):
                    continue
                exponent = tranche.get("exponent")
                exponent = exponent if isinstance(exponent, int) and exponent >= 0 else 2
                pools.append({
                    "id": f"claude-promotional-credit-{index + 1}",
                    "label": "Promotional credit",
                    "kind": "promotional",
                    "unit": str(tranche.get("currency") or "usd").lower(),
                    "balance": float(amount) / (10 ** exponent),
                    "next_expiry_at": _reset_epoch(tranche.get("expires_at")),
                })
        result["credit_pools"] = pools

    detected_plan = _claude_plan_value(payload)
    if detected_plan:
        result["raw_plan"] = str(detected_plan)
    return result


def _finalize_claude_text_usage(result: dict) -> dict:
    limits = []
    rows = (
        ("claude-session", "Session", "session_pct", "session_reset", "session", "aggregate", None, None),
        ("claude-weekly", "Weekly", "weekly_pct", "weekly_reset", "weekly", "aggregate", None, None),
        ("claude-weekly-sonnet", "Sonnet weekly", "weekly_sonnet_pct", "weekly_reset", "weekly", "model", "claude-sonnet", None),
        ("claude-weekly-design", "Design weekly", "weekly_design_pct", "weekly_reset", "weekly", "feature", None, "design"),
    )
    for bucket_id, label, pct_key, reset_key, window_kind, scope_kind, model, feature in rows:
        used = result.get(pct_key)
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            continue
        limits.append({
            "id": bucket_id,
            "label": label,
            "window_kind": window_kind,
            "scope_kind": scope_kind,
            "model": model,
            "feature": feature,
            "used_pct": float(used),
            "remaining_pct": 100 - float(used),
            "reset": result.get(reset_key),
        })
    if limits:
        result["limits"] = limits

    spent = result.get("extra_spent_usd")
    cap = result.get("extra_limit_usd")
    balance = result.get("extra_balance_usd")
    if any(value is not None for value in (spent, cap, balance)) or result.get("extra_pct"):
        result["credit_pools"] = [{
            "id": "claude-usage-credits",
            "label": "Usage credits",
            "kind": "prepaid",
            "unit": "usd",
            "balance": balance,
            "spent_month": spent,
            "monthly_spend_cap": cap,
            "monthly_spend_headroom": (
                max(float(cap) - float(spent), 0.0)
                if isinstance(cap, (int, float)) and isinstance(spent, (int, float))
                else None
            ),
            "used_pct": result.get("extra_pct"),
            "reset": result.get("extra_reset"),
        }]
    return result


def _parse_claude_web_usage_payload(payload: dict | list) -> dict:
    if not isinstance(payload, (dict, list)):
        return {}

    result = {}

    if isinstance(payload, dict):
        _merge_usage_pct_reset(
            result,
            payload.get("five_hour"),
            pct_key="session_pct",
            pct_field="utilization",
            reset_key="session_reset",
            reset_field="resets_at",
        )
        if "session_pct" not in result or "session_reset" not in result:
            _merge_usage_pct_reset(
                result,
                payload.get("currentSession"),
                pct_key="session_pct",
                pct_field="usedPercent",
                reset_key="session_reset",
                reset_field="resetAt",
            )
        _merge_usage_pct_reset(
            result,
            payload.get("seven_day"),
            pct_key="weekly_pct",
            pct_field="utilization",
            reset_key="weekly_reset",
            reset_field="resets_at",
        )
        _merge_usage_pct_reset(
            result,
            payload.get("seven_day_sonnet"),
            pct_key="weekly_sonnet_pct",
            pct_field="utilization",
        )
        _merge_usage_pct_reset(
            result,
            payload.get("seven_day_design"),
            pct_key="weekly_design_pct",
            pct_field="utilization",
        )

        weekly = payload.get("weekly")
        if isinstance(weekly, dict):
            _merge_usage_pct_reset(
                result,
                weekly.get("allModels"),
                pct_key="weekly_pct",
                pct_field="usedPercent",
                reset_key="weekly_reset",
                reset_field="resetAt",
            )
            _merge_usage_pct_reset(
                result,
                weekly.get("sonnetOnly"),
                pct_key="weekly_sonnet_pct",
                pct_field="usedPercent",
            )
            _merge_usage_pct_reset(
                result,
                weekly.get("designOnly"),
                pct_key="weekly_design_pct",
                pct_field="usedPercent",
            )

        for bucket in _claude_structured_limits(payload):
            if bucket["id"] == "claude-session":
                result.setdefault("session_pct", int(round(bucket["used_pct"])))
                if bucket.get("reset"):
                    result.setdefault("session_reset", bucket["reset"])
            elif bucket["id"] == "claude-weekly":
                result.setdefault("weekly_pct", int(round(bucket["used_pct"])))
                if bucket.get("reset"):
                    result.setdefault("weekly_reset", bucket["reset"])
            elif bucket.get("model") == "fable" and bucket.get("window_kind") == "weekly":
                result["weekly_fable_pct"] = int(round(bucket["used_pct"]))

        extra_usage = payload.get("extra_usage")
        if not isinstance(extra_usage, dict):
            extra_usage = payload.get("extraUsage")
        if isinstance(extra_usage, dict):
            utilization = extra_usage.get("utilization")
            if not isinstance(utilization, (int, float)) or isinstance(utilization, bool):
                utilization = extra_usage.get("usedPercent")
            if isinstance(utilization, (int, float)) and not isinstance(utilization, bool):
                result["extra_pct"] = int(round(float(utilization)))
            reset_at = _format_reset_time(extra_usage.get("resets_at") or extra_usage.get("resetAt"))
            if reset_at:
                result["extra_reset"] = reset_at
            monthly_limit = extra_usage.get("monthly_limit")
            if monthly_limit is None:
                monthly_limit = extra_usage.get("limitUsd")
            used_credits = extra_usage.get("used_credits")
            if used_credits is None:
                used_credits = extra_usage.get("spentUsd")
            normalized_limit = _claude_money_value(extra_usage, monthly_limit)
            normalized_spent = _claude_money_value(extra_usage, used_credits)
            if normalized_limit is not None:
                result["extra_limit_usd"] = normalized_limit
            if normalized_spent is not None:
                result["extra_spent_usd"] = normalized_spent
            balance_usd = extra_usage.get("balanceUsd")
            normalized_balance = _claude_money_value(extra_usage, balance_usd)
            if normalized_balance is not None:
                result["extra_balance_usd"] = normalized_balance

        if _claude_usage_complete(result):
            return _finalize_claude_usage_payload(payload, result)

    session_section = _pick_section(
        payload,
        include_tokens=("session",),
        exclude_tokens=("week", "weekly", "sonnet", "extra"),
    )
    if session_section:
        session_pct = _find_value_in_section(
            session_section,
            ("used", "percent", "pct", "usage"),
            value_predicate=lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100,
            forbidden_tokens=("remaining", "reset", "time", "spent", "limit", "balance"),
        )
        session_reset = _find_value_in_section(
            session_section,
            ("reset", "resets", "time", "at"),
            value_predicate=lambda value: isinstance(value, (str, int, float)) and _format_reset_time(value) is not None,
        )
        if session_pct is not None:
            result["session_pct"] = int(round(float(session_pct)))
        if session_reset is not None:
            result["session_reset"] = _format_reset_time(session_reset)

    weekly_section = _pick_section(
        payload,
        include_tokens=("week", "weekly", "all", "models"),
        exclude_tokens=("session", "sonnet", "design", "extra"),
        minimum_hits=2,
    )
    if not weekly_section:
        weekly_section = _pick_section(
            payload,
            include_tokens=("all", "models"),
            exclude_tokens=("session", "sonnet", "design", "extra"),
            minimum_hits=2,
        )
    if weekly_section:
        weekly_pct = _find_value_in_section(
            weekly_section,
            ("used", "percent", "pct", "usage"),
            value_predicate=lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100,
            forbidden_tokens=("remaining", "reset", "time", "spent", "limit", "balance"),
        )
        weekly_reset = _find_value_in_section(
            weekly_section,
            ("reset", "resets", "time", "at"),
            value_predicate=lambda value: isinstance(value, (str, int, float)) and _format_reset_time(value) is not None,
        )
        if weekly_pct is not None:
            result["weekly_pct"] = int(round(float(weekly_pct)))
        if weekly_reset is not None:
            result["weekly_reset"] = _format_reset_time(weekly_reset)

    weekly_sonnet_section = _pick_section(
        payload,
        include_tokens=("week", "weekly", "sonnet"),
        exclude_tokens=("session", "design", "extra"),
        minimum_hits=2,
    )
    if not weekly_sonnet_section:
        weekly_sonnet_section = _pick_section(
            payload,
            include_tokens=("sonnet",),
            exclude_tokens=("session", "design", "extra"),
        )
    if weekly_sonnet_section:
        weekly_sonnet_pct = _find_value_in_section(
            weekly_sonnet_section,
            ("used", "percent", "pct", "usage"),
            value_predicate=lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100,
            forbidden_tokens=("remaining", "reset", "time", "spent", "limit", "balance"),
        )
        if weekly_sonnet_pct is not None:
            result["weekly_sonnet_pct"] = int(round(float(weekly_sonnet_pct)))

    weekly_design_section = _pick_section(
        payload,
        include_tokens=("week", "weekly", "design"),
        exclude_tokens=("session", "sonnet", "extra"),
        minimum_hits=2,
    )
    if not weekly_design_section:
        weekly_design_section = _pick_section(
            payload,
            include_tokens=("design",),
            exclude_tokens=("session", "sonnet", "extra"),
        )
    if weekly_design_section:
        weekly_design_pct = _find_value_in_section(
            weekly_design_section,
            ("used", "percent", "pct", "usage"),
            value_predicate=lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100,
            forbidden_tokens=("remaining", "reset", "time", "spent", "limit", "balance"),
        )
        if weekly_design_pct is not None:
            result["weekly_design_pct"] = int(round(float(weekly_design_pct)))

    extra_section = _pick_section(
        payload,
        include_tokens=("extra",),
        exclude_tokens=("session", "week", "weekly", "sonnet"),
    )
    if extra_section:
        extra_pct = _find_value_in_section(
            extra_section,
            ("used", "percent", "pct", "usage"),
            value_predicate=lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100,
            forbidden_tokens=("remaining", "reset", "time", "spent", "limit", "balance"),
        )
        extra_reset = _find_value_in_section(
            extra_section,
            ("reset", "resets", "time", "at"),
            value_predicate=lambda value: isinstance(value, (str, int, float)) and _format_reset_time(value) is not None,
        )
        extra_spent = _find_value_in_section(
            extra_section,
            ("spent", "cost"),
            value_predicate=lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
            forbidden_tokens=("percent", "pct", "used", "limit", "balance"),
        )
        extra_limit = _find_value_in_section(
            extra_section,
            ("limit", "max", "cap"),
            value_predicate=lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
            forbidden_tokens=("percent", "pct", "used", "spent", "balance"),
        )
        extra_balance = _find_value_in_section(
            extra_section,
            ("balance", "remaining"),
            value_predicate=lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
            forbidden_tokens=("percent", "pct", "used"),
        )
        if extra_pct is not None:
            result.setdefault("extra_pct", int(round(float(extra_pct))))
        if extra_reset is not None:
            result.setdefault("extra_reset", _format_reset_time(extra_reset))
        if extra_spent is not None:
            result.setdefault("extra_spent_usd", _claude_money_value(extra_section, extra_spent))
        if extra_limit is not None:
            result.setdefault("extra_limit_usd", _claude_money_value(extra_section, extra_limit))
        if extra_balance is not None:
            result.setdefault("extra_balance_usd", _claude_money_value(extra_section, extra_balance))

    return _finalize_claude_usage_payload(payload, result)


def _claude_web_request_config() -> tuple[str, dict[str, str]] | None:
    fallback_file = Path.home() / ".usage-tracker" / "claude-cookie.txt"
    keychain_cookie = load_provider_credential("claude")
    if _valid_claude_cookie_header(keychain_cookie):
        cookie = keychain_cookie
        try:
            fallback_file.unlink(missing_ok=True)
        except OSError:
            pass
    else:
        cookie = _read_env_or_file(
            "CLAUDE_WEB_COOKIE",
            "CLAUDE_WEB_COOKIE_FILE",
            fallback_file,
        )
    cookie = _normalize_cookie_header(cookie) if cookie else None
    if cookie and "=" not in cookie:
        cookie = f"sessionKey={cookie}"

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://claude.ai/settings/usage",
        "User-Agent": _installed_chrome_user_agent(),
        "anthropic-client-platform": os.environ.get("CLAUDE_WEB_CLIENT_PLATFORM", "web_claude_ai"),
    }
    if cookie:
        headers["Cookie"] = cookie

    overrides = _load_claude_web_header_overrides()
    headers.update(overrides)
    cookie_key = next((key for key in headers if key.lower() == "cookie"), None)
    cookie_text = _normalize_cookie_header(headers[cookie_key]) if cookie_key else ""
    headers = {key: value for key, value in headers.items() if key.lower() != "cookie"}
    if cookie_text:
        headers["Cookie"] = cookie_text
    if not _valid_claude_cookie_header(cookie_text):
        return None

    cookies = _parse_cookie_header(cookie_text)
    derived_headers = {
        "anthropic-device-id": cookies.get("anthropic-device-id"),
        "anthropic-anonymous-id": cookies.get("ajs_anonymous_id"),
        "x-activity-session-id": cookies.get("activitySessionId"),
    }
    existing_header_names = {key.lower() for key in headers}
    for key, value in derived_headers.items():
        if value and key not in existing_header_names:
            headers[key] = value

    org_id = os.environ.get("CLAUDE_WEB_ORG_ID", "").strip()
    if not org_id and cookie_text:
        org_id = _parse_cookie_header(cookie_text).get("lastActiveOrg", "")

    if not org_id or not cookie_text:
        return None

    url = f"https://claude.ai/api/organizations/{org_id}/usage"
    return url, headers


def claude_web_usage_configured() -> bool:
    return _claude_web_request_config() is not None


def _codex_web_origin() -> str:
    source_url = (
        os.environ.get("CODEX_WEB_ANALYTICS_URL", "").strip()
        or os.environ.get("CODEX_WEB_USAGE_URL", "").strip()
        or "https://chatgpt.com/codex/cloud/settings/analytics"
    )
    parsed = urllib.parse.urlparse(source_url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "https://chatgpt.com"


def _codex_web_request_config() -> tuple[str, dict[str, str]] | None:
    cookie = _read_provider_cookie(
        "codex",
        "CODEX_WEB_COOKIE",
        "CODEX_WEB_COOKIE_FILE",
        Path.home() / ".usage-tracker" / "codex-cookie.txt",
    )
    cookie = _normalize_cookie_header(cookie) if cookie else None
    if cookie and "=" not in cookie:
        cookie = f"__Secure-next-auth.session-token={cookie}"

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": os.environ.get(
            "CODEX_WEB_ANALYTICS_URL",
            "https://chatgpt.com/codex/cloud/settings/analytics",
        ),
        "User-Agent": os.environ.get(
            "CODEX_WEB_USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        ),
    }
    if cookie:
        headers["Cookie"] = cookie

    overrides = _load_codex_web_header_overrides()
    headers.update(overrides)
    if "Cookie" in headers:
        headers["Cookie"] = _normalize_cookie_header(headers["Cookie"])

    if not headers.get("Cookie"):
        return None

    return _codex_web_origin(), headers


def codex_web_analytics_configured() -> bool:
    return _codex_web_request_config() is not None


def _parse_bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _codex_analytics_window_days() -> int:
    raw = os.environ.get("CODEX_ANALYTICS_WINDOW_DAYS", "").strip()
    if not raw:
        return 30
    try:
        window_days = int(raw)
    except ValueError:
        return 30
    return max(1, min(window_days, 90))


def _codex_analytics_group_by() -> str:
    value = os.environ.get("CODEX_ANALYTICS_GROUP_BY", "day").strip().lower()
    return value if value in {"day", "week"} else "day"


def _codex_analytics_date_range(window_days: int) -> tuple[str, str]:
    end_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    start_date = end_date - timedelta(days=max(window_days - 1, 0))
    return start_date.isoformat(), end_date.isoformat()


def _codex_analytics_helper_script() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts" / "fetch_codex_web_analytics.mjs"


def _run_codex_analytics_browser_helper(payload: dict) -> dict:
    helper = _codex_analytics_helper_script()
    if not helper.exists():
        raise RuntimeError(f"Codex analytics helper missing: {helper}")

    try:
        proc = subprocess.run(
            [_find_bin("node"), str(helper)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Codex analytics helper timed out") from exc
    except OSError as exc:
        raise RuntimeError(f"Codex analytics helper failed to start: {exc}") from exc

    if proc.returncode != 0:
        detail = _normalize_whitespace(proc.stderr) or f"exit {proc.returncode}"
        raise RuntimeError(f"Codex analytics helper failed: {detail}")

    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError("Codex analytics helper returned no data")

    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Codex analytics helper returned invalid JSON: {exc}") from exc

    if not isinstance(result, dict):
        raise RuntimeError("Codex analytics helper returned a non-object payload")
    return result


def _safe_number(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _round_metric(value: float) -> float:
    return round(value, 1) if abs(value - round(value)) > 1e-9 else float(int(round(value)))


def _average(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _summarize_codex_analytics(bundle: dict) -> dict:
    workspace_rows = bundle.get("daily_workspace_usage_counts", {}).get("data", []) or []
    workspace_users = []
    workspace_threads = []
    workspace_turns = []
    workspace_credits = []
    for row in workspace_rows:
        totals = row.get("totals", {}) if isinstance(row, dict) else {}
        workspace_users.append(_safe_number(totals.get("users")))
        workspace_threads.append(_safe_number(totals.get("threads")))
        workspace_turns.append(_safe_number(totals.get("turns")))
        workspace_credits.append(_safe_number(totals.get("credits")))

    session_rows = bundle.get("daily_sessions_messages_counts", {}).get("data", []) or []
    sessions_by_day: dict[str, dict[str, float]] = {}
    for row in session_rows:
        if not isinstance(row, dict):
            continue
        date_key = str(row.get("date", "")).strip()
        if not date_key:
            continue
        day = sessions_by_day.setdefault(
            date_key,
            {
                "sessions": 0.0,
                "messages": 0.0,
                "credits": 0.0,
                "users": 0.0,
                "tasks_web": 0.0,
                "code_reviews_web": 0.0,
            },
        )
        day["sessions"] += _safe_number(row.get("n_new_sessions_total"))
        day["messages"] += _safe_number(row.get("n_user_messages_total"))
        day["credits"] += _safe_number(row.get("credit_total"))
        day["users"] += _safe_number(row.get("n_users_used_codex"))
        day["tasks_web"] += _safe_number(row.get("n_tasks_web"))
        day["code_reviews_web"] += _safe_number(row.get("n_code_reviews_web"))

    session_days = list(sessions_by_day.values())
    review_rows = bundle.get("daily_code_review_metrics", {}).get("data", []) or []
    review_counts = []
    review_comments = []
    review_p0 = []
    review_p1 = []
    review_p2 = []
    for row in review_rows:
        if not isinstance(row, dict):
            continue
        review_counts.append(_safe_number(row.get("n_reviews")))
        review_comments.append(_safe_number(row.get("n_comments")))
        review_p0.append(_safe_number(row.get("n_comments_p0")))
        review_p1.append(_safe_number(row.get("n_comments_p1")))
        review_p2.append(_safe_number(row.get("n_comments_p2")))

    return {
        "workspace": {
            "days": len(workspace_rows),
            "avg_daily_users": _round_metric(_average(workspace_users)),
            "avg_daily_threads": _round_metric(_average(workspace_threads)),
            "avg_daily_turns": _round_metric(_average(workspace_turns)),
            "avg_daily_credits": _round_metric(_average(workspace_credits)),
            "peak_daily_users": _round_metric(max(workspace_users, default=0.0)),
        },
        "sessions_messages": {
            "days": len(session_days),
            "avg_daily_sessions": _round_metric(_average([row["sessions"] for row in session_days])),
            "avg_daily_user_messages": _round_metric(_average([row["messages"] for row in session_days])),
            "avg_daily_credits": _round_metric(_average([row["credits"] for row in session_days])),
            "avg_daily_users": _round_metric(_average([row["users"] for row in session_days])),
            "avg_daily_web_tasks": _round_metric(_average([row["tasks_web"] for row in session_days])),
            "avg_daily_web_reviews": _round_metric(_average([row["code_reviews_web"] for row in session_days])),
        },
        "code_review": {
            "days": len(review_rows),
            "avg_daily_reviews": _round_metric(_average(review_counts)),
            "avg_daily_comments": _round_metric(_average(review_comments)),
            "avg_daily_p0_comments": _round_metric(_average(review_p0)),
            "avg_daily_p1_comments": _round_metric(_average(review_p1)),
            "avg_daily_p2_comments": _round_metric(_average(review_p2)),
        },
    }


def scrape_codex_analytics() -> dict | None:
    """Fetch Codex analytics datasets from ChatGPT private web APIs."""
    config = _codex_web_request_config()
    if not config:
        return None

    origin, headers = config
    window_days = _codex_analytics_window_days()
    group_by = _codex_analytics_group_by()
    include_emails = _parse_bool_env("CODEX_ANALYTICS_INCLUDE_EMAILS", default=False)
    start_date, end_date = _codex_analytics_date_range(window_days)
    bundle = _run_codex_analytics_browser_helper(
        {
            "origin": origin,
            "analytics_page_url": os.environ.get(
                "CODEX_WEB_ANALYTICS_URL",
                f"{origin}/codex/cloud/settings/analytics",
            ),
            "cookie": headers.get("Cookie", ""),
            "window_days": window_days,
            "group_by": group_by,
            "include_emails": include_emails,
            "date_range": {
                "start_date": start_date,
                "end_date": end_date,
            },
        }
    )

    bundle.setdefault("window_days", window_days)
    bundle.setdefault("group_by", group_by)
    bundle.setdefault(
        "date_range",
        {
            "start_date": start_date,
            "end_date": end_date,
        },
    )
    bundle.setdefault("include_emails", include_emails)
    bundle.setdefault("daily_workspace_usage_counts", {"data": []})
    bundle.setdefault("daily_sessions_messages_counts", {"data": []})
    bundle.setdefault("daily_code_review_metrics", {"data": []})
    bundle.setdefault("usage", {})
    bundle.setdefault("credit_usage_events", {"data": []})
    bundle["summary"] = _summarize_codex_analytics(bundle)
    return bundle


def _claude_usage_complete(result: dict) -> bool:
    required = ("session_pct", "session_reset", "weekly_pct", "weekly_reset")
    return all(key in result for key in required)


def _fetch_claude_usage_http(url: str, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            content_type = resp.headers.get("content-type", "").lower()
            cf_mitigated = resp.headers.get("cf-mitigated", "").lower()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        cf_mitigated = exc.headers.get("cf-mitigated", "").lower()
        if exc.code == 403 and cf_mitigated == "challenge":
            raise RuntimeError("Claude web API was blocked by a Cloudflare challenge") from exc
        raise RuntimeError(f"Claude web API returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Claude web API request failed: {exc.reason}") from exc

    if cf_mitigated == "challenge":
        raise RuntimeError("Claude web API returned a Cloudflare challenge")
    if "json" not in content_type:
        text = body.decode("utf-8", errors="replace")
        if "Just a moment" in text:
            raise RuntimeError("Claude web API returned a Cloudflare challenge page")
        raise RuntimeError(f"Claude web API returned non-JSON content ({content_type or 'unknown'})")

    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Claude web API returned invalid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Claude web API returned a non-object payload")
    return result


def _claude_usage_browser_helper_script() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts" / "fetch_claude_web_usage.mjs"


def _run_claude_usage_browser_helper(payload: dict) -> dict:
    helper = _claude_usage_browser_helper_script()
    if not helper.exists():
        raise RuntimeError(f"Claude browser usage helper missing: {helper}")
    try:
        proc = subprocess.run(
            [_find_bin("node"), str(helper)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Claude browser usage helper timed out") from exc
    except OSError as exc:
        raise RuntimeError("Claude browser usage helper failed to start") from exc
    if proc.returncode != 0:
        detail = _normalize_whitespace(proc.stderr) or f"exit {proc.returncode}"
        raise RuntimeError(f"Claude browser usage helper failed: {detail}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Claude browser usage helper returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Claude browser usage helper returned a non-object payload")
    return result


def scrape_claude_usage_web() -> dict | None:
    """Fetch Claude plan usage from its browser-bound private web endpoints."""
    config = _claude_web_request_config()
    if not config:
        return None

    url, headers = config
    mode = os.environ.get("CLAUDE_WEB_FETCH_MODE", "browser").strip().lower()
    if mode not in {"auto", "browser", "http"}:
        mode = "browser"

    payload = None
    browser_error = None
    if mode in {"auto", "browser"}:
        try:
            parsed_url = urllib.parse.urlparse(url)
            organization_id = parsed_url.path.split("/organizations/", 1)[1].split("/", 1)[0]
            user_agent = next(
                (value for key, value in headers.items() if key.lower() == "user-agent"),
                _installed_chrome_user_agent(),
            )
            bundle = _run_claude_usage_browser_helper({
                "cookie": headers.get("Cookie", ""),
                "organization_id": organization_id,
                "user_agent": user_agent,
            })
            payload = _merge_claude_browser_bundle(bundle)
        except Exception as exc:
            browser_error = exc
            if mode == "browser":
                raise
    if payload is None and mode in {"auto", "http"}:
        try:
            payload = _fetch_claude_usage_http(url, headers)
        except Exception:
            if browser_error is not None:
                raise browser_error
            raise

    result = _parse_claude_web_usage_payload(payload)
    return result if _claude_usage_complete(result) else None


def _parse_codex_rate_limits_payload(payload: dict) -> dict | None:
    """Normalize app-server rate limits without assuming a fixed plan shape."""
    rl = payload.get("rateLimits") if isinstance(payload.get("rateLimits"), dict) else {}
    limits_by_id = payload.get("rateLimitsByLimitId")
    result: dict = {}
    parsed_limits: list[dict] = []
    credit_pools: list[dict] = []

    def numeric(value) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return None
        return None

    def used_pct(bucket: dict | None) -> float | None:
        return numeric(bucket.get("usedPercent")) if isinstance(bucket, dict) else None

    def reset_text(value) -> str | None:
        if not value:
            return None
        try:
            return datetime.fromtimestamp(float(value)).strftime("%b %-d %-I:%M %p")
        except (TypeError, ValueError, OSError, OverflowError):
            return _format_reset_time(value)

    def label_text(limit_id: str, group: dict, bucket: dict) -> str:
        return " ".join(str(item or "") for item in (
            limit_id, group.get("limitName"), bucket.get("name"), bucket.get("label"),
            bucket.get("model"), bucket.get("kind"), bucket.get("scope"), bucket.get("type"),
        )).lower()

    def window_kind(bucket: dict, bucket_name: str, label: str) -> str:
        minutes = numeric(bucket.get("windowDurationMins"))
        if minutes is not None:
            if minutes <= 360:
                return "session"
            if 9000 <= minutes <= 11000:
                return "weekly"
            if 40000 <= minutes <= 50000:
                return "monthly"
            if minutes > 11000:
                return "account"
        if "month" in label:
            return "monthly"
        if "week" in label:
            return "weekly"
        if "session" in label or "5h" in label or "5 hour" in label:
            return "session"
        if bucket_name == "secondary":
            return "weekly"
        if bucket_name == "primary":
            return "session"
        return "unknown"

    def model_id(label: str) -> str | None:
        aliases = (
            (("gpt-5.6 sol", "gpt-5.6-sol", "gpt56-sol"), "gpt-5.6-sol"),
            (("gpt-5.6 terra", "gpt-5.6-terra", "gpt56-terra"), "gpt-5.6-terra"),
            (("gpt-5.6 luna", "gpt-5.6-luna", "gpt56-luna"), "gpt-5.6-luna"),
            (("gpt-5.5", "gpt55"), "gpt-5.5"),
            (("gpt-5.4 mini", "gpt-5.4-mini", "gpt54-mini"), "gpt-5.4-mini"),
            (("gpt-5.4", "gpt54"), "gpt-5.4"),
            (("spark", "gpt-5.3-codex-spark"), "gpt-5.3-codex-spark"),
        )
        for keywords, canonical in aliases:
            if any(keyword in label for keyword in keywords):
                return canonical
        return None

    def is_aggregate(limit_id: str, group: dict) -> bool:
        group_id = str(group.get("limitId") or limit_id).lower()
        return group_id == "codex" and not str(group.get("limitName") or "").strip()

    groups: list[tuple[str, dict]] = []
    if isinstance(limits_by_id, dict) and limits_by_id:
        groups.extend((str(key), value) for key, value in limits_by_id.items() if isinstance(value, dict))
    elif rl:
        groups.append((str(rl.get("limitId") or "codex"), rl))

    def record(limit_id: str, group: dict, bucket_name: str, bucket: dict) -> None:
        used = used_pct(bucket)
        if used is None:
            return
        label = label_text(limit_id, group, bucket)
        model = model_id(label)
        feature = "code-review" if "review" in label else None
        aggregate = is_aggregate(limit_id, group)
        kind = window_kind(bucket, bucket_name, label)
        raw_reset = bucket.get("resetsAt") or bucket.get("resetAt")
        reset = reset_text(raw_reset)
        row = {
            "id": f"codex-{limit_id}-{bucket_name}",
            "label": str(group.get("limitName") or bucket.get("label") or bucket.get("name") or model or feature or kind).replace("_", " ").title(),
            "limit_id": group.get("limitId") or limit_id,
            "raw_plan": group.get("planType"),
            "bucket": bucket_name,
            "window_kind": kind,
            "window_minutes": numeric(bucket.get("windowDurationMins")),
            "scope_kind": "aggregate" if aggregate else "model" if model else "feature",
            "model": model,
            "feature": feature,
            "used_pct": used,
            "remaining_pct": 100 - used,
            "reset": reset,
            "reset_at": _reset_epoch(raw_reset),
            "rate_limit_reached_type": group.get("rateLimitReachedType"),
        }
        parsed_limits.append(row)

        remaining = row["remaining_pct"]
        if aggregate and kind == "session" and result.get("session_remaining_pct") is None:
            result["session_remaining_pct"] = remaining
            result["session_reset"] = reset
        elif aggregate and kind == "weekly":
            result["weekly_remaining_pct"] = remaining
            result["reset_at"] = reset
        elif aggregate and kind in {"monthly", "account"}:
            result["account_remaining_pct"] = remaining
            result["account_reset"] = reset
            result["account_window_minutes"] = row["window_minutes"]
        if feature == "code-review":
            result["code_review_remaining_pct"] = remaining
        if model == "gpt-5.4":
            result["weekly_gpt54_remaining_pct"] = remaining
        if model == "gpt-5.3-codex-spark":
            result["weekly_spark_remaining_pct"] = remaining

    for limit_id, group in groups:
        for bucket_name in ("primary", "secondary"):
            bucket = group.get(bucket_name)
            if isinstance(bucket, dict):
                record(limit_id, group, bucket_name, bucket)

    if not isinstance(limits_by_id, dict):
        for key, bucket in rl.items():
            if key in {"primary", "secondary"} or not isinstance(bucket, dict):
                continue
            synthetic = {"limitId": key, "limitName": bucket.get("label") or bucket.get("name")}
            record(str(key), synthetic, str(key), bucket)

    for limit_id, group in groups:
        raw_plan = group.get("planType")
        if raw_plan and "raw_plan" not in result:
            result["raw_plan"] = str(raw_plan)
        credits = group.get("credits")
        balance = numeric(credits.get("balance")) if isinstance(credits, dict) else None
        if balance is not None and (is_aggregate(limit_id, group) or "credits_remaining" not in result):
            result["credits_remaining"] = balance
            result["credits_state"] = {
                "enabled": credits.get("hasCredits", credits.get("has_credits")),
                "unlimited": credits.get("unlimited"),
                "spend_control_reached": credits.get(
                    "spendControlReached", credits.get("spend_control_reached")
                ),
            }

    if "credits_remaining" in result:
        credit_state = result.get("credits_state") or {}
        credit_pools.append({
            "id": "codex-shared-credits",
            "label": "Shared credits",
            "kind": "purchased",
            "unit": "credits",
            "balance": result["credits_remaining"],
            "enabled": credit_state.get("enabled"),
            "unlimited": credit_state.get("unlimited"),
            "spend_control_reached": credit_state.get("spend_control_reached"),
            "shared_with": ["codex", "work"],
        })

    reset_credits = payload.get("rateLimitResetCredits") or {}
    count = reset_credits.get("availableCount") if isinstance(reset_credits, dict) else None
    if isinstance(count, int) and not isinstance(count, bool):
        result["rate_limit_reset_credits"] = count
        reset_pool = {
            "id": "codex-banked-resets",
            "label": "Banked resets",
            "kind": "reset",
            "unit": "resets",
            "balance": count,
        }
        credits = reset_credits.get("credits")
        if isinstance(credits, list):
            expiries = [
                credit.get("expiresAt")
                for credit in credits
                if isinstance(credit, dict)
                and isinstance(credit.get("expiresAt"), int)
                and not isinstance(credit.get("expiresAt"), bool)
            ]
            if expiries:
                reset_pool["next_expiry_at"] = min(expiries)
        credit_pools.append(reset_pool)
    if parsed_limits:
        result["limits"] = parsed_limits
    if credit_pools:
        result["credit_pools"] = credit_pools
    return result or None


# Seconds scrape_codex_usage waits for each app-server response.
CODEX_RECV_TIMEOUT = 10


def scrape_codex_usage() -> dict | None:
    """Get Codex usage via the app-server JSON-RPC API.

    Starts `codex app-server`, sends initialize + account/rateLimits/read,
    returns structured data with exact reset timestamps.
    """
    codex_bin = _find_bin('codex')
    if not shutil.which('codex') and not os.path.exists(codex_bin):
        return None

    proc = subprocess.Popen(
        [codex_bin, 'app-server'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )

    # A reader thread instead of select(): on Windows select() accepts only sockets, so a
    # select() on this pipe raised there and Codex quota was never read.
    lines: queue.Queue = queue.Queue()

    def pump():
        for line in iter(proc.stdout.readline, ''):
            lines.put(line)

    threading.Thread(target=pump, name="codex-app-server-reader", daemon=True).start()

    def send(msg):
        proc.stdin.write(json.dumps(msg) + '\n')
        proc.stdin.flush()

    def recv(timeout=None):
        # Codex 0.129+ pushes unsolicited JSON-RPC notifications
        # (e.g. `remoteControl/status/changed`) between request/response pairs.
        # Filter them out — only return objects that carry an `id` (responses).
        deadline = time.time() + (CODEX_RECV_TIMEOUT if timeout is None else timeout)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                line = lines.get(timeout=min(1.0, remaining))
            except queue.Empty:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict) or "id" not in msg:
                continue
            return msg

    try:
        # Initialize
        send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
              'params': {'clientInfo': {'name': 'usage-tracker', 'version': '1.0'}}})
        recv()

        # Initialized notification
        send({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})

        # Read rate limits
        send({'jsonrpc': '2.0', 'id': 2, 'method': 'account/rateLimits/read', 'params': {}})
        resp = recv()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if not resp or 'result' not in resp:
        return None

    return _parse_codex_rate_limits_payload(resp["result"])


# ── Cursor ───────────────────────────────────────────────

_CURSOR_STATE_DB = Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
_CURSOR_LIMIT_STATE_FILE = Path.home() / ".usage-tracker" / "cursor-agent-status.txt"
_CURSOR_FREE_REQUEST_LIMIT = 50


def _cursor_auth_token() -> str | None:
    """Extract Cursor access token from local state database."""
    if not _CURSOR_STATE_DB.exists():
        return None
    try:
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(_CURSOR_STATE_DB)) as conn:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken'"
            ).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _parse_cursor_agent_limit_text(text: str) -> dict | None:
    """Parse Cursor Agent quota-limit text copied from the CLI/chat surface."""
    cleaned = _clean(text or "")
    if not cleaned.strip():
        return None

    lower = cleaned.lower()
    free_limit_hit = "free requests limit" in lower
    generic_limit_hit = "usage limit" in lower or "requests limit" in lower
    spend_limit_match = re.search(r"spendLimitHit:[ \t]*(true|false)", cleaned, re.IGNORECASE)
    spend_limit_hit = None
    if spend_limit_match:
        spend_limit_hit = spend_limit_match.group(1).lower() == "true"

    spend_limits: list[int] = []
    spend_limits_match = re.search(r"spendLimits:[ \t]*\[([^\]]*)\]", cleaned, re.IGNORECASE)
    if spend_limits_match:
        spend_limits = [
            int(value)
            for value in re.findall(r"\d+", spend_limits_match.group(1))
        ]

    reset_at = None
    reset_match = re.search(
        r"monthly cycle ends on\s+(\d{1,2}/\d{1,2}/\d{4})",
        cleaned,
        re.IGNORECASE,
    )
    if reset_match:
        try:
            reset_at = datetime.strptime(reset_match.group(1), "%m/%d/%Y").date().isoformat()
        except ValueError:
            reset_at = reset_match.group(1)

    fallback_model = None
    fallback_match = re.search(r"fallbackModel:[ \t]*([^\n\r]*)", cleaned)
    if fallback_match:
        fallback_model = fallback_match.group(1).strip() or None

    if not (generic_limit_hit or spend_limit_hit or spend_limits or reset_at or fallback_model):
        return None

    result: dict[str, object] = {}
    if generic_limit_hit:
        result.update(
            {
                "limit_hit": True,
                "at_limit": True,
                "limit_kind": "free_requests" if free_limit_hit else "usage",
                "limit_message": (
                    "You've hit your free requests limit."
                    if free_limit_hit
                    else "You've hit your Cursor usage limit."
                ),
                "remaining_requests": 0,
            }
        )
        if free_limit_hit:
            result["plan"] = "free"
            result["max_requests"] = _CURSOR_FREE_REQUEST_LIMIT
            result["total_requests"] = _CURSOR_FREE_REQUEST_LIMIT
            result["models"] = {
                "agent": {
                    "requests": _CURSOR_FREE_REQUEST_LIMIT,
                    "tokens": 0,
                    "max_requests": _CURSOR_FREE_REQUEST_LIMIT,
                }
            }

    if reset_at:
        result["reset_at"] = reset_at
    if spend_limit_hit is not None:
        result["spend_limit_hit"] = spend_limit_hit
    if spend_limits:
        result["spend_limits"] = spend_limits
    if fallback_model:
        result["fallback_model"] = fallback_model

    return result


def _cursor_agent_limit_state() -> dict | None:
    text = _read_env_or_file("CURSOR_AGENT_LIMIT_TEXT", "CURSOR_AGENT_LIMIT_FILE")
    state = _parse_cursor_agent_limit_text(text or "")
    if state and _cursor_limit_state_active(state):
        return state

    try:
        state = _parse_cursor_agent_limit_text(_CURSOR_LIMIT_STATE_FILE.read_text())
    except OSError:
        return None
    return state if state and _cursor_limit_state_active(state) else None


def _cursor_limit_state_active(state: dict) -> bool:
    reset_at = state.get("reset_at")
    if not isinstance(reset_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", reset_at):
        return True
    try:
        return datetime.strptime(reset_at, "%Y-%m-%d").date() >= datetime.now().date()
    except ValueError:
        return True


def _merge_cursor_limit_state(result: dict, limit_state: dict | None) -> dict:
    if not limit_state:
        return result

    plan = str(result.get("plan") or "").strip().lower()
    if (
        limit_state.get("limit_kind") == "free_requests"
        and plan
        and plan not in {"free", "free_trial", "hobby", "unknown"}
    ):
        return result

    merged = dict(result)
    for key, value in limit_state.items():
        if value is not None:
            merged[key] = value

    if merged.get("at_limit") or merged.get("limit_hit"):
        max_requests = merged.get("max_requests")
        total_requests = merged.get("total_requests")
        if isinstance(max_requests, (int, float)) and max_requests > 0:
            if not isinstance(total_requests, (int, float)) or total_requests < max_requests:
                merged["total_requests"] = int(max_requests)
            merged["remaining_requests"] = 0
    return merged


def scrape_cursor_usage() -> dict | None:
    """Fetch Cursor usage and profile via api2.cursor.sh."""
    limit_state = _cursor_agent_limit_state()
    token = _cursor_auth_token()
    if not token:
        return limit_state

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    result = {}

    # Usage data
    try:
        req = urllib.request.Request(
            "https://api2.cursor.sh/auth/usage",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            usage = json.loads(resp.read())
        result["usage"] = usage

        # Aggregate totals across all models
        total_requests = 0
        total_tokens = 0
        models = {}
        for model_key, model_data in usage.items():
            if model_key == "startOfMonth":
                result["start_of_month"] = model_data
                continue
            if not isinstance(model_data, dict):
                continue
            reqs = model_data.get("numRequestsTotal", 0) or 0
            toks = model_data.get("numTokens", 0) or 0
            max_reqs = model_data.get("maxRequestUsage")
            total_requests += reqs
            total_tokens += toks
            if reqs > 0 or toks > 0:
                models[model_key] = {
                    "requests": reqs,
                    "tokens": toks,
                    "max_requests": max_reqs,
                }
        result["total_requests"] = total_requests
        result["total_tokens"] = total_tokens
        result["models"] = models
    except Exception:
        pass

    # Profile/plan data
    try:
        req = urllib.request.Request(
            "https://api2.cursor.sh/auth/full_stripe_profile",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            profile = json.loads(resp.read())
        result["plan"] = profile.get("membershipType", "free")
        result["trial_eligible"] = profile.get("trialEligible", False)
    except Exception:
        if "plan" not in result:
            result["plan"] = "unknown"

    # Apply known free plan limits when API returns null
    KNOWN_FREE_LIMITS = {
        "free": _CURSOR_FREE_REQUEST_LIMIT,
        "free_trial": _CURSOR_FREE_REQUEST_LIMIT,
        "hobby": _CURSOR_FREE_REQUEST_LIMIT,
    }
    plan = (result.get("plan") or "free").lower()
    if plan in KNOWN_FREE_LIMITS:
        default_max = KNOWN_FREE_LIMITS[plan]
        result["max_requests"] = default_max
        # Backfill models that had null maxRequestUsage
        for model_data in result.get("models", {}).values():
            if model_data.get("max_requests") is None:
                model_data["max_requests"] = default_max
        # If no models were tracked but we have a limit, add a synthetic entry
        if not result.get("models"):
            result["models"] = {
                "premium": {
                    "requests": result.get("total_requests", 0),
                    "tokens": result.get("total_tokens", 0),
                    "max_requests": default_max,
                }
            }

    result = _merge_cursor_limit_state(result, limit_state)
    return result if result else None
