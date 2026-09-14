"""Auditable project and repository reports over the private work ledger."""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta, timezone
import re

from src import usage_ledger, work_ledger
from src.cost_estimates import price_usage_event
from src.usage_explanation import EFFECTIVE_WEIGHTS, category_color, category_label


REPORT_PERIODS = frozenset({"day", "week", "month"})
ACTIVE_GAP_US = 30 * 60 * 1_000_000


def report_window(period: str, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    if period not in REPORT_PERIODS:
        raise ValueError("period must be day, week, or month")
    end = now or datetime.now().astimezone()
    if end.tzinfo is None:
        end = end.astimezone()
    if period == "day":
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = end - timedelta(days=7 if period == "week" else 30)
    return start, end


class _IntervalIndex:
    def __init__(self, intervals: list[dict]):
        self.intervals = intervals
        self.starts = [int(row["started_at_us"]) for row in intervals]

    def work_item_id_at(self, timestamp_us: int) -> str | None:
        index = bisect_right(self.starts, timestamp_us) - 1
        if index < 0:
            return None
        interval = self.intervals[index]
        ended_at = interval.get("ended_at_us")
        if ended_at is not None and timestamp_us >= int(ended_at):
            return None
        return str(interval["work_item_id"])


_DEFAULT_BRANCHES = frozenset({
    "main", "master", "trunk", "develop", "development", "dev", "head",
})


def _name_tokens(value: str | None) -> tuple[str, ...]:
    return tuple(
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if token not in {"feature", "feat", "fix", "bugfix", "chore", "codex"}
    )


def _item_identity(
    item: dict,
    *,
    inferred: bool,
    source: str,
    confidence: float,
) -> dict:
    return {
        "id": item["id"],
        "name": item["name"],
        "kind": item["kind"],
        "parent_id": item.get("parent_id"),
        "parent_name": item.get("parent_name"),
        "repository_id": item.get("effective_repository_id"),
        "repository_name": item.get("repository_name"),
        "color_hex": item.get("effective_color_hex"),
        "inferred": inferred,
        "attribution_source": source,
        "attribution_confidence": confidence,
    }


class _AttributionResolver:
    def __init__(
        self,
        intervals: list[dict],
        items: dict[str, dict],
        repositories: dict[str, dict],
        session_assignments: list[dict] | None = None,
    ):
        self.intervals = _IntervalIndex(intervals)
        self.items = items
        self.repositories = repositories
        self.session_assignments = {
            str(row["ai_session_id"]): row
            for row in (session_assignments or [])
        }
        self.projects_by_repository: dict[str, list[dict]] = {}
        self.children_by_project: dict[str, list[dict]] = {}
        for item in items.values():
            if item.get("archived_at") is not None:
                continue
            if item["kind"] == "project" and item.get("effective_repository_id"):
                self.projects_by_repository.setdefault(
                    str(item["effective_repository_id"]), []
                ).append(item)
            elif item.get("parent_id"):
                self.children_by_project.setdefault(
                    str(item["parent_id"]), []
                ).append(item)

    def _project_for_item(self, item: dict) -> dict | None:
        project_id = item["id"] if item["kind"] == "project" else item.get("parent_id")
        return self.items.get(str(project_id)) if project_id else None

    def _manual_targets(
        self,
        timestamp_us: int,
    ) -> tuple[list[dict], list[dict]] | None:
        item_id = self.intervals.work_item_id_at(timestamp_us)
        item = self.items.get(item_id) if item_id else None
        if item is None:
            return None
        direct = _item_identity(
            item,
            inferred=False,
            source="manual",
            confidence=1.0,
        )
        project = self._project_for_item(item)
        projects = (
            []
            if project is None
            else [_item_identity(
                project,
                inferred=False,
                source="manual",
                confidence=1.0,
            )]
        )
        return [direct], projects

    def _session_targets(
        self,
        ai_session_id: str | None,
    ) -> tuple[list[dict], list[dict]] | None:
        assignment = self.session_assignments.get(str(ai_session_id)) if ai_session_id else None
        if assignment is None:
            return None
        if assignment.get("assignment_mode") == "unassigned":
            return [], []
        item = self.items.get(str(assignment.get("work_item_id") or ""))
        if item is None or item.get("archived_at") is not None:
            return None
        direct = _item_identity(
            item,
            inferred=False,
            source="session",
            confidence=1.0,
        )
        project = self._project_for_item(item)
        projects = (
            []
            if project is None
            else [_item_identity(
                project,
                inferred=False,
                source="session",
                confidence=1.0,
            )]
        )
        return [direct], projects

    def _branch_match(self, branch: str | None, projects: list[dict]) -> dict | None:
        branch_tokens = set(_name_tokens(branch))
        if not branch_tokens:
            return None
        matches: list[tuple[int, dict]] = []
        for project in projects:
            for item in self.children_by_project.get(str(project["id"]), []):
                item_tokens = set(_name_tokens(item["name"]))
                if not item_tokens:
                    continue
                if item_tokens.issubset(branch_tokens):
                    matches.append((len(item_tokens), item))
        if not matches:
            return None
        matches.sort(key=lambda match: (-match[0], str(match[1]["id"])))
        if len(matches) > 1 and matches[0][0] == matches[1][0]:
            return None
        return matches[0][1]

    def _inferred_project(self, repository_id: str) -> dict:
        repository = self.repositories[repository_id]
        return {
            "id": work_ledger.stable_id("inferred_project", repository_id),
            "name": repository["display_name"],
            "kind": "project",
            "parent_id": None,
            "parent_name": None,
            "repository_id": repository_id,
            "repository_name": repository["display_name"],
            "color_hex": None,
            "inferred": True,
            "attribution_source": "repository",
            "attribution_confidence": 0.75,
        }

    def targets(
        self,
        timestamp_us: int,
        *,
        ai_session_id: str | None = None,
        repository_id: str | None,
        branch: str | None,
    ) -> tuple[list[dict], list[dict]]:
        session = self._session_targets(ai_session_id)
        if session is not None:
            return session
        manual = self._manual_targets(timestamp_us)
        if manual is not None:
            return manual
        if not repository_id or repository_id not in self.repositories:
            return [], []

        linked_projects = self.projects_by_repository.get(repository_id, [])
        branch_match = self._branch_match(branch, linked_projects)
        if branch_match is not None:
            project = self._project_for_item(branch_match)
            direct = _item_identity(
                branch_match,
                inferred=True,
                source="branch_match",
                confidence=0.9,
            )
            projects = (
                []
                if project is None
                else [_item_identity(
                    project,
                    inferred=True,
                    source="branch_match",
                    confidence=0.9,
                )]
            )
            return [direct], projects

        if len(linked_projects) == 1:
            project = _item_identity(
                linked_projects[0],
                inferred=True,
                source="linked_repository",
                confidence=0.85,
            )
        else:
            project = self._inferred_project(repository_id)

        normalized_branch = str(branch or "").strip()
        if normalized_branch and normalized_branch.lower() not in _DEFAULT_BRANCHES:
            branch_identity = {
                "id": work_ledger.stable_id(
                    "inferred_branch", repository_id, normalized_branch
                ),
                "name": normalized_branch,
                "kind": "branch",
                "parent_id": project["id"],
                "parent_name": project["name"],
                "repository_id": repository_id,
                "repository_name": self.repositories[repository_id]["display_name"],
                "color_hex": project.get("color_hex"),
                "inferred": True,
                "attribution_source": "branch",
                "attribution_confidence": 0.8,
            }
            return [branch_identity], [project]
        return [project], [project]


def _empty_metrics() -> dict:
    return {
        "tracked_seconds": 0.0,
        "active_timestamps": set(),
        "messages": 0,
        "user_messages": 0,
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "priced_tokens": 0,
        "estimated_cost_usd": 0.0,
        "estimated_credits": 0.0,
        "credit_priced_tokens": 0,
        "activity_events": 0,
        "git_commits": 0,
        "git_pull_requests": 0,
        "merge_commits": 0,
        "files_changed": 0,
        "additions": 0,
        "deletions": 0,
        "sessions": set(),
        "models": {},
        "event_kinds": {},
        "usage_categories": {},
        "unpriced_models": set(),
        "unpriced_reasons": {},
    }


def _bucket(store: dict, key: str | None, identity: dict) -> dict:
    bucket_key = key or "__unassigned__"
    if bucket_key not in store:
        store[bucket_key] = {**identity, "metrics": _empty_metrics()}
    elif float(identity.get("attribution_confidence") or 0) > float(
        store[bucket_key].get("attribution_confidence") or 0
    ):
        metrics = store[bucket_key]["metrics"]
        store[bucket_key].update(identity)
        store[bucket_key]["metrics"] = metrics
    return store[bucket_key]["metrics"]


def _add_usage(
    metrics: dict,
    event: dict,
    provider: str,
    priced: dict,
    category_prices: dict,
) -> None:
    cache_tokens = (
        int(event["cache_read_tokens"] or 0)
        + int(event["cache_write_5m_tokens"] or 0)
        + int(event["cache_write_1h_tokens"] or 0)
    )
    total_tokens = int(priced["total_tokens"])
    metrics["messages"] += int(bool(event["is_message"]))
    metrics["user_messages"] += int(bool(event["is_user"]))
    metrics["requests"] += int(total_tokens > 0)
    metrics["input_tokens"] += int(event["input_tokens"] or 0)
    metrics["output_tokens"] += int(event["output_tokens"] or 0)
    metrics["cache_tokens"] += cache_tokens
    metrics["reasoning_tokens"] += int(event["reasoning_tokens"] or 0)
    metrics["total_tokens"] += total_tokens
    metrics["priced_tokens"] += int(priced["priced_tokens"])
    metrics["sessions"].add(f"{provider}:{event['session_id']}")
    timestamp_us = event.get("timestamp_us")
    if isinstance(timestamp_us, (int, float)) and not isinstance(timestamp_us, bool):
        metrics["active_timestamps"].add(int(timestamp_us))
    if total_tokens <= 0:
        return
    model = str(event["model"])
    model_usage = metrics["models"].setdefault(
        model,
        {"model": model, "tokens": 0, "requests": 0},
    )
    model_usage["tokens"] += total_tokens
    model_usage["requests"] += 1
    if priced["estimated_cost_usd"] is not None:
        metrics["estimated_cost_usd"] += float(priced["estimated_cost_usd"])
    else:
        metrics["unpriced_models"].add(model)
        if priced["unpriced_reason"]:
            metrics["unpriced_reasons"][model] = priced["unpriced_reason"]
    if priced["estimated_credits"] is not None:
        metrics["estimated_credits"] += float(priced["estimated_credits"])
        metrics["credit_priced_tokens"] += total_tokens
    for category, values in (event.get("usage_breakdown") or {}).items():
        if not isinstance(values, dict):
            continue
        category_metrics = metrics["usage_categories"].setdefault(
            category,
            {
                "id": category,
                "label": category_label(category),
                "color": category_color(category),
                "total_tokens": 0,
                "effective_tokens": 0.0,
                "event_count": 0,
                "priced_tokens": 0,
                "estimated_cost_usd": 0.0,
                "estimated_credits": 0.0,
            },
        )
        category_tokens = sum(
            int(values.get(field) or 0)
            for field in (
                "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_5m_tokens", "cache_write_1h_tokens",
            )
        )
        category_metrics["total_tokens"] += category_tokens
        category_metrics["effective_tokens"] += sum(
            int(values.get(field) or 0) * weight
            for field, weight in EFFECTIVE_WEIGHTS.items()
        )
        category_metrics["event_count"] += int(values.get("event_count") or 0)
        category_price = category_prices.get(category) or {}
        category_metrics["priced_tokens"] += int(category_price.get("priced_tokens") or 0)
        category_metrics["estimated_cost_usd"] += float(category_price.get("estimated_cost_usd") or 0)
        category_metrics["estimated_credits"] += float(category_price.get("estimated_credits") or 0)


def _add_activity(metrics: dict, event: dict, session_identity: str | None) -> None:
    kind = str(event["kind"])
    metadata = event.get("metadata") or {}
    metrics["activity_events"] += 1
    metrics["event_kinds"][kind] = metrics["event_kinds"].get(kind, 0) + 1
    metrics["git_commits"] += int(kind == "git_commit")
    metrics["git_pull_requests"] += int(kind == "git_pull_request")
    metrics["merge_commits"] += int(
        kind == "git_commit" and bool(metadata.get("merge_commit"))
    )
    for field in ("files_changed", "additions", "deletions"):
        metrics[field] += int(metadata.get(field) or 0)
    if session_identity:
        metrics["sessions"].add(session_identity)
        timestamp_us = event.get("occurred_at_us")
        if isinstance(timestamp_us, (int, float)) and not isinstance(timestamp_us, bool):
            metrics["active_timestamps"].add(int(timestamp_us))


def _bounded_active_seconds(timestamps: set[int]) -> float:
    ordered = sorted(timestamps)
    return sum(
        (current - previous) / 1_000_000
        for previous, current in zip(ordered, ordered[1:])
        if 0 < current - previous <= ACTIVE_GAP_US
    )


def _cost_ratio(cost: float | None, denominator: float | None) -> float | None:
    """Cost per unit of outcome, or None when the ratio cannot be stated honestly.

    Suppression is a first-class state rather than a zero. Emitting 0.0 for a period
    with no pull requests would read as "these pull requests were free", and an
    unpriced cost side divided by anything is not a cost at all. This mirrors
    estimated_cost_usd and pricing_coverage_pct, which already return None rather
    than 0 when they cannot be computed.

    The caller must pass a MEASURED cost: None unless priced_tokens > 0. A bucket
    with no AI usage at all carries estimated_cost_usd 0.0 rather than None (see
    has_cost), and dividing that would publish 0.0 for work whose cost was never
    measured, ranking it as the cheapest work in the report. That reading is the
    precise failure this rule exists to prevent, and pricing_coverage_pct cannot
    qualify it, because coverage is itself None when there are no tokens.

    A positive ratio never degrades to 0.0. Per-changed-line costs are legitimately
    smaller than 1e-6, and rounding those to zero would be indistinguishable from
    "never measured", so a value that would round away keeps significant digits.
    """
    if cost is None or not denominator or denominator < 0:
        return None
    ratio = float(cost) / float(denominator)
    rounded = round(ratio, 6)
    if rounded == 0.0 and ratio != 0.0:
        return float(f"{ratio:.6g}")
    return rounded


def _finalize_metrics(metrics: dict) -> dict:
    total_tokens = int(metrics["total_tokens"])
    priced_tokens = int(metrics["priced_tokens"])
    has_cost = priced_tokens > 0 or total_tokens == 0
    has_credits = int(metrics["credit_priced_tokens"]) > 0
    effective_total = sum(
        float(category["effective_tokens"])
        for category in metrics["usage_categories"].values()
    )
    usage_categories = []
    for category in metrics["usage_categories"].values():
        category_tokens = int(category["total_tokens"])
        category_priced = int(category["priced_tokens"])
        usage_categories.append({
            "id": category["id"],
            "label": category["label"],
            "color": category["color"],
            "total_tokens": category_tokens,
            "effective_tokens": round(float(category["effective_tokens"]), 1),
            "share_pct": (
                round(float(category["effective_tokens"]) / effective_total * 100, 1)
                if effective_total else 0.0
            ),
            "event_count": int(category["event_count"]),
            "estimated_cost_usd": (
                round(float(category["estimated_cost_usd"]), 6)
                if category_priced else None
            ),
            "estimated_credits": (
                round(float(category["estimated_credits"]), 3)
                if category_priced and category["estimated_credits"] else None
            ),
        })
    usage_categories.sort(key=lambda row: (-row["effective_tokens"], row["label"]))
    tracked_seconds = float(metrics["tracked_seconds"])
    active_seconds = max(
        tracked_seconds,
        _bounded_active_seconds(metrics["active_timestamps"]),
    )
    estimated_cost_usd = (
        round(float(metrics["estimated_cost_usd"]), 6) if has_cost else None
    )
    # Ratios divide the MEASURED cost only. has_cost is also true when a bucket has
    # no tokens whatsoever, which yields a 0.0 cost that means "never measured"
    # rather than "free"; publishing ratios from it would make unmeasured work look
    # like the cheapest work in the report.
    measured_cost_usd = estimated_cost_usd if priced_tokens > 0 else None
    git_commits = int(metrics["git_commits"])
    git_pull_requests = int(metrics["git_pull_requests"])
    # NOTE: changed_lines counts lines TOUCHED, not lines landed. _add_activity
    # accumulates additions/deletions for every event kind, and git_dirty events
    # carry the whole working-tree diff at each poll, so an uncommitted change is
    # counted once per poll. cost_per_changed_line is therefore a cost per line
    # touched and reads LOWER than a cost per committed line would.
    changed_lines = int(metrics["additions"] + metrics["deletions"])
    return {
        "tracked_seconds": round(tracked_seconds, 1),
        "active_seconds": round(active_seconds, 1),
        "session_count": len(metrics["sessions"]),
        "messages": int(metrics["messages"]),
        "user_messages": int(metrics["user_messages"]),
        "requests": int(metrics["requests"]),
        "input_tokens": int(metrics["input_tokens"]),
        "output_tokens": int(metrics["output_tokens"]),
        "cache_tokens": int(metrics["cache_tokens"]),
        "reasoning_tokens": int(metrics["reasoning_tokens"]),
        "total_tokens": total_tokens,
        "estimated_cost_usd": estimated_cost_usd,
        "estimated_credits": (
            round(float(metrics["estimated_credits"]), 3) if has_credits else None
        ),
        "pricing_coverage_pct": (
            round(priced_tokens / total_tokens * 100, 1) if total_tokens else None
        ),
        "unpriced_models": sorted(metrics["unpriced_models"]),
        "unpriced_reasons": dict(sorted(metrics["unpriced_reasons"].items())),
        "activity_events": int(metrics["activity_events"]),
        "git_commits": git_commits,
        "git_pull_requests": git_pull_requests,
        "merge_commits": int(metrics["merge_commits"]),
        "files_changed": int(metrics["files_changed"]),
        "additions": int(metrics["additions"]),
        "deletions": int(metrics["deletions"]),
        "changed_lines": changed_lines,
        "cost_per_pr": _cost_ratio(measured_cost_usd, git_pull_requests),
        "cost_per_commit": _cost_ratio(measured_cost_usd, git_commits),
        "cost_per_changed_line": _cost_ratio(measured_cost_usd, changed_lines),
        "models": sorted(
            metrics["models"].values(),
            key=lambda row: (-row["tokens"], row["model"]),
        ),
        "event_kinds": dict(sorted(metrics["event_kinds"].items())),
        "usage_categories": usage_categories,
    }


def _finalize_buckets(store: dict, *, sort_key=None) -> list[dict]:
    rows = [
        {**{key: value for key, value in bucket.items() if key != "metrics"},
         "metrics": _finalize_metrics(bucket["metrics"])}
        for bucket in store.values()
    ]
    return sorted(rows, key=sort_key or (lambda row: str(row.get("name") or "").lower()))


def build_report(
    period: str,
    *,
    now: datetime | None = None,
    sync_usage: bool = False,
) -> dict:
    start, end = report_window(period, now=now)
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    start_us = round(start_utc.timestamp() * 1_000_000)
    end_us = round(end_utc.timestamp() * 1_000_000)
    snapshot = work_ledger.reporting_snapshot(start_us, end_us)
    items = {row["id"]: row for row in snapshot["items"]}
    repositories = {row["id"]: row for row in snapshot["repositories"]}
    resolver = _AttributionResolver(
        snapshot["intervals"],
        items,
        repositories,
        snapshot.get("session_assignments"),
    )
    sessions_by_provider_id = {
        (row["provider"], row["provider_session_id"]): row
        for row in snapshot["sessions"]
    }
    sessions_by_id = {row["id"]: row for row in snapshot["sessions"]}

    overall = _empty_metrics()
    work_buckets: dict = {}
    project_buckets: dict = {}
    repository_buckets: dict = {}
    provider_buckets: dict = {}
    unassigned_identity = {
        "id": None,
        "name": "Unassigned",
        "kind": "unassigned",
        "parent_id": None,
        "parent_name": None,
        "repository_id": None,
        "repository_name": None,
        "color_hex": None,
        "inferred": False,
        "attribution_source": "unassigned",
        "attribution_confidence": 0.0,
    }

    def work_targets(
        timestamp_us: int,
        *,
        ai_session_id: str | None = None,
        repository_id: str | None = None,
        branch: str | None = None,
    ) -> tuple[list[dict], list[dict]]:
        direct, projects = resolver.targets(
            timestamp_us,
            ai_session_id=ai_session_id,
            repository_id=repository_id,
            branch=branch,
        )
        return (direct, projects) if direct else ([unassigned_identity], [])

    def repository_target(repository_id: str | None) -> tuple[str | None, dict]:
        repository = repositories.get(repository_id) if repository_id else None
        return repository_id if repository else None, {
            "id": repository_id if repository else None,
            "name": repository["display_name"] if repository else "Unassigned",
            "enabled": repository["enabled"] if repository else False,
            "repository_id": repository_id if repository else None,
            "inferred": False,
            "attribution_source": "repository",
            "attribution_confidence": 1.0 if repository else 0.0,
        }

    for interval in snapshot["intervals"]:
        overlap_start = max(start_us, int(interval["started_at_us"]))
        overlap_end = min(end_us, int(interval.get("ended_at_us") or end_us))
        if overlap_end <= overlap_start:
            continue
        seconds = (overlap_end - overlap_start) / 1_000_000
        direct, projects = work_targets(overlap_start)
        overall["tracked_seconds"] += seconds
        for identity in direct:
            _bucket(work_buckets, identity["id"], identity)["tracked_seconds"] += seconds
        for identity in projects:
            _bucket(project_buckets, identity["id"], identity)["tracked_seconds"] += seconds
        item = items.get(interval["work_item_id"])
        repository_id = item.get("effective_repository_id") if item else None
        repo_key, repo_identity = repository_target(repository_id)
        _bucket(repository_buckets, repo_key, repo_identity)["tracked_seconds"] += seconds

    for provider in ("claude", "codex"):
        if sync_usage:
            usage_ledger.sync_provider(provider)
        provider_metrics = _bucket(
            provider_buckets,
            provider,
            {"id": provider, "name": "Claude" if provider == "claude" else "Codex"},
        )
        for event in usage_ledger.iter_events_between(provider, start_utc, end_utc):
            priced = price_usage_event(
                provider,
                str(event["model"]),
                event["at"],
                input_tokens=int(event["input_tokens"] or 0),
                output_tokens=int(event["output_tokens"] or 0),
                cache_read_tokens=int(event["cache_read_tokens"] or 0),
                cache_write_5m_tokens=int(event["cache_write_5m_tokens"] or 0),
                cache_write_1h_tokens=int(event["cache_write_1h_tokens"] or 0),
                reasoning_tokens=int(event["reasoning_tokens"] or 0),
            )
            category_prices = {
                category: price_usage_event(
                    provider,
                    str(event["model"]),
                    event["at"],
                    input_tokens=int(values.get("input_tokens") or 0),
                    output_tokens=int(values.get("output_tokens") or 0),
                    cache_read_tokens=int(values.get("cache_read_tokens") or 0),
                    cache_write_5m_tokens=int(values.get("cache_write_5m_tokens") or 0),
                    cache_write_1h_tokens=int(values.get("cache_write_1h_tokens") or 0),
                    reasoning_tokens=int(values.get("reasoning_tokens") or 0),
                )
                for category, values in (event.get("usage_breakdown") or {}).items()
                if isinstance(values, dict)
            }
            session = sessions_by_provider_id.get((provider, event["session_id"]))
            repository_id = session.get("repository_id") if session else None
            direct, projects = work_targets(
                int(event["timestamp_us"]),
                ai_session_id=session.get("id") if session else None,
                repository_id=repository_id,
                branch=session.get("branch") if session else None,
            )
            repo_key, repo_identity = repository_target(repository_id)
            for metrics in (
                overall,
                provider_metrics,
                _bucket(repository_buckets, repo_key, repo_identity),
            ):
                _add_usage(metrics, event, provider, priced, category_prices)
            for identity in direct:
                _add_usage(
                    _bucket(work_buckets, identity["id"], identity),
                    event,
                    provider,
                    priced,
                    category_prices,
                )
            for identity in projects:
                _add_usage(
                    _bucket(project_buckets, identity["id"], identity),
                    event,
                    provider,
                    priced,
                    category_prices,
                )

    for event in snapshot["activity_events"]:
        session = sessions_by_id.get(event.get("ai_session_id"))
        repository_id = event.get("repository_id") or (
            session.get("repository_id") if session else None
        )
        metadata = event.get("metadata") or {}
        branch = session.get("branch") if session else None
        if not branch and event.get("kind") in {"git_head", "git_dirty"}:
            branch = metadata.get("branch")
        if not branch and event.get("kind") == "git_pull_request":
            branch = metadata.get("pull_request_branch")
        direct, projects = work_targets(
            int(event["occurred_at_us"]),
            ai_session_id=session.get("id") if session else None,
            repository_id=repository_id,
            branch=branch,
        )
        repo_key, repo_identity = repository_target(repository_id)
        provider = event.get("provider") or (session.get("provider") if session else None)
        session_identity = (
            f"{session['provider']}:{session['provider_session_id']}" if session else None
        )
        targets = [overall, _bucket(repository_buckets, repo_key, repo_identity)]
        if provider in {"claude", "codex"}:
            targets.append(_bucket(
                provider_buckets,
                provider,
                {"id": provider, "name": "Claude" if provider == "claude" else "Codex"},
            ))
        for metrics in targets:
            _add_activity(metrics, event, session_identity)
        for identity in direct:
            _add_activity(
                _bucket(work_buckets, identity["id"], identity),
                event,
                session_identity,
            )
        for identity in projects:
            _add_activity(
                _bucket(project_buckets, identity["id"], identity),
                event,
                session_identity,
            )

    unassigned = work_buckets.get("__unassigned__", {"metrics": _empty_metrics()})
    return {
        "period": period,
        "generated_at": int(end.timestamp()),
        "window": {
            "start_at": int(start.timestamp()),
            "end_at": int(end.timestamp()),
            "timezone": str(end.tzinfo),
        },
        "totals": _finalize_metrics(overall),
        "unassigned": _finalize_metrics(unassigned["metrics"]),
        "projects": _finalize_buckets(
            project_buckets,
            sort_key=lambda row: (-row["metrics"]["active_seconds"], row["name"].lower()),
        ),
        "work_items": _finalize_buckets(
            work_buckets,
            sort_key=lambda row: (
                row.get("parent_name") or row["name"],
                0 if row["kind"] == "project" else 1,
                row["name"],
            ),
        ),
        "repositories": _finalize_buckets(
            repository_buckets,
            sort_key=lambda row: (-row["metrics"]["active_seconds"], row["name"].lower()),
        ),
        "providers": _finalize_buckets(
            provider_buckets,
            sort_key=lambda row: row["id"],
        ),
    }
