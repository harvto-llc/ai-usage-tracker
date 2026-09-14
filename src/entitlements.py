"""Normalize provider plans without treating plan names as quota truth."""

from __future__ import annotations

import re
from typing import Any


PLAN_CATALOG: dict[str, dict[str, dict[str, Any]]] = {
    "claude": {
        "free": {
            "label": "Free",
            "monthly_usd": 0,
            "billing_mode": "subscription",
            "usage_credits": False,
        },
        "pro": {
            "label": "Pro",
            "monthly_usd": 20,
            "billing_mode": "subscription",
            "session_capacity_vs_free": 5,
            "usage_credits": True,
        },
        "max-5x": {
            "label": "Max 5x",
            "monthly_usd": 100,
            "billing_mode": "subscription",
            "session_capacity_vs_pro": 5,
            "usage_credits": True,
            "expected_weekly_sonnet_limit": True,
        },
        "max-20x": {
            "label": "Max 20x",
            "monthly_usd": 200,
            "billing_mode": "subscription",
            "session_capacity_vs_pro": 20,
            "usage_credits": True,
            "expected_weekly_sonnet_limit": True,
        },
        "team": {
            "label": "Team",
            "billing_mode": "workspace",
            "usage_credits": True,
            "admin_model_controls": True,
        },
        "enterprise": {
            "label": "Enterprise",
            "billing_mode": "workspace",
            "usage_credits": True,
            "admin_model_controls": True,
        },
        "api": {
            "label": "API",
            "billing_mode": "payg",
            "usage_credits": False,
        },
    },
    "codex": {
        "free": {
            "label": "Free",
            "monthly_usd": 0,
            "billing_mode": "subscription",
            "credit_topup": False,
        },
        "go": {
            "label": "Go",
            "monthly_usd": 8,
            "billing_mode": "subscription",
            "credit_topup": False,
        },
        "plus": {
            "label": "Plus",
            "monthly_usd": 20,
            "billing_mode": "subscription",
            "credit_topup": True,
            "expected_models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
        },
        "pro": {
            "label": "Pro",
            "billing_mode": "subscription",
            "credit_topup": True,
            "expected_models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
            "expected_spark_access": True,
        },
        "pro-5x": {
            "label": "Pro 5x",
            "monthly_usd": 100,
            "billing_mode": "subscription",
            "capacity_vs_plus": 5,
            "credit_topup": True,
            "expected_models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
            "expected_spark_access": True,
        },
        "pro-20x": {
            "label": "Pro 20x",
            "monthly_usd": 200,
            "billing_mode": "subscription",
            "capacity_vs_plus": 20,
            "credit_topup": True,
            "expected_models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
            "expected_spark_access": True,
        },
        "business": {
            "label": "Business",
            "billing_mode": "workspace",
            "credit_topup": True,
            "workspace_credit_pool": True,
        },
        "enterprise": {
            "label": "Enterprise",
            "billing_mode": "workspace",
            "workspace_credit_pool": True,
        },
        "edu": {
            "label": "Edu",
            "billing_mode": "workspace",
            "workspace_credit_pool": True,
        },
        "api": {
            "label": "API",
            "billing_mode": "payg",
            "credit_topup": False,
        },
    },
}


PLAN_ALIASES: dict[str, dict[str, str]] = {
    "claude": {
        "max": "max-5x",
        "max5x": "max-5x",
        "max-5": "max-5x",
        "max20x": "max-20x",
        "max-20": "max-20x",
        "premium": "team",
        "api-key": "api",
        "pay-as-you-go": "api",
    },
    "codex": {
        # The app-server reports ChatGPT Pro accounts as "prolite".
        "prolite": "pro",
        "pro-light": "pro",
        "pro5x": "pro-5x",
        "pro20x": "pro-20x",
        "team": "business",
        "chatgpt-team": "business",
        "chatgpt-business": "business",
        "api-key": "api",
        "pay-as-you-go": "api",
    },
}


def _slug(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    text = text.replace("×", "x")
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or None


def normalize_plan_id(provider: str, raw_plan: object) -> str | None:
    provider_id = provider.strip().lower()
    slug = _slug(raw_plan)
    if not slug:
        return None
    return PLAN_ALIASES.get(provider_id, {}).get(slug, slug)


def configured_plan_value(provider: str, plans: dict | None) -> object:
    entry = (plans or {}).get(provider)
    return entry.get("plan") if isinstance(entry, dict) else None


def resolve_plan(
    provider: str,
    detected_plan: object = None,
    *,
    plans: dict | None = None,
) -> dict[str, Any]:
    """Resolve live plan metadata, falling back to configured plan text."""
    provider_id = provider.strip().lower()
    configured = configured_plan_value(provider_id, plans)
    raw = detected_plan if _slug(detected_plan) else configured
    source = "detected" if _slug(detected_plan) else "configured" if _slug(configured) else "unknown"
    plan_id = normalize_plan_id(provider_id, raw)
    catalog = PLAN_CATALOG.get(provider_id, {}).get(plan_id or "", {})
    label = catalog.get("label")
    if not label and plan_id:
        label = plan_id.replace("-", " ").title()
    return {
        "provider": provider_id,
        "plan_id": plan_id,
        "plan_label": label,
        "plan_source": source,
        "raw_plan": str(raw).strip() if raw is not None else None,
        "known_plan": bool(catalog),
        "entitlements": dict(catalog),
    }
