import XCTest
@testable import UsageMenuBar

final class WorkTrackingModelsTests: XCTestCase {
    func testSessionEvidenceDecodesTimelineAndSearchMetadata() throws {
        let timelineData = Data(
            """
            {
              "session_id": "session-1",
              "provider": "codex",
              "provider_session_id": "provider-1",
              "items": [{
                "id": "event-1", "occurred_at_us": 100,
                "kind": "test", "title": "Ran tests",
                "summary": "swift test", "tool_name": "exec_command",
                "command_category": "test", "file_paths": []
              }],
              "truncated": true, "source_missing": false,
              "bytes_scanned": 2048
            }
            """.utf8
        )
        let searchData = Data(
            """
            {
              "query": "swift test",
              "matches": [{
                "session_id": "session-1", "provider": "codex",
                "session_name": "Evidence search", "repository_name": "usage-tracker",
                "branch": "codex/evidence", "evidence_id": "event-1",
                "occurred_at_us": 100, "kind": "test", "title": "Ran tests",
                "snippet": "swift test"
              }],
              "sessions_scanned": 4, "sessions_available": 10,
              "bytes_scanned": 4096, "missing_sources": 1, "partial": true,
              "index": {
                "state": "ready", "backend": "sqlite_fts5",
                "privacy_mode": "contentless", "sessions_indexed": 9,
                "sessions_available": 10, "documents": 120,
                "stale_sessions": 1, "missing_sources": 0,
                "last_indexed_at_us": 200, "db_bytes": 4096,
                "last_error": null
              }
            }
            """.utf8
        )

        let timeline = try JSONDecoder().decode(SessionEvidenceResponse.self, from: timelineData)
        let search = try JSONDecoder().decode(SessionEvidenceSearchResponse.self, from: searchData)

        XCTAssertEqual(timeline.items.first?.kind, "test")
        XCTAssertEqual(timeline.items.first?.commandCategory, "test")
        XCTAssertTrue(timeline.truncated)
        XCTAssertEqual(search.matches.first?.id, "event-1")
        XCTAssertEqual(search.sessionsScanned, 4)
        XCTAssertEqual(search.index?.privacyMode, "contentless")
        XCTAssertEqual(search.index?.staleSessions, 1)
        XCTAssertTrue(search.partial)
    }

    func testUsageExplanationDecodesCategoryAndCoverage() throws {
        let data = Data(
            """
            {
              "provider": "codex",
              "period": "week",
              "window_start_at": "2026-07-18T12:00:00+00:00",
              "window_end_at": "2026-07-25T12:00:00+00:00",
              "estimate_basis": "local_transcript_attribution",
              "quota_relation": "independent_estimate",
              "coverage": {
                "status": "compacted", "confidence": "medium",
                "compactions": 1, "classified_pct": 96.5
              },
              "totals": {
                "input_tokens": 100, "output_tokens": 20,
                "cache_read_tokens": 300, "cache_write_5m_tokens": 0,
                "cache_write_1h_tokens": 0, "reasoning_tokens": 8,
                "total_tokens": 420, "effective_tokens": 230.0,
                "event_count": 3, "sessions": 2,
                "estimated_cost_usd": 0.42, "estimated_credits": 10.5,
                "pricing_coverage_pct": 100.0, "unpriced_models": []
              },
              "categories": [{
                "id": "shell", "label": "Shell", "color": "#D0A52B",
                "input_tokens": 100, "output_tokens": 20,
                "cache_read_tokens": 300, "cache_write_5m_tokens": 0,
                "cache_write_1h_tokens": 0, "reasoning_tokens": 8,
                "total_tokens": 420, "effective_tokens": 230.0,
                "share_pct": 100.0, "event_count": 3,
                "estimated_cost_usd": 0.42, "estimated_credits": 10.5,
                "pricing_coverage_pct": 100.0, "models": ["gpt-5.6-terra"],
                "unpriced_models": [], "confidence": "medium"
              }]
            }
            """.utf8
        )

        let explanation = try JSONDecoder().decode(UsageExplanation.self, from: data)

        XCTAssertEqual(explanation.provider, "codex")
        XCTAssertEqual(explanation.coverage.compactions, 1)
        XCTAssertEqual(explanation.totals.estimatedCredits, 10.5)
        XCTAssertEqual(explanation.categories.first?.id, "shell")
        XCTAssertEqual(explanation.categories.first?.cacheReadTokens, 300)
    }

    func testStatusDecodesPausedAndActiveWorkState() throws {
        let data = Data(
            """
            {
              "schema_version": 5,
              "settings": {
                "collection_enabled": true,
                "paused_at": null,
                "explicit_roots": ["/repo"],
                "retention_days": 90,
                "updated_at": 10
              },
              "active_work": {
                "state": "active",
                "interval_id": "interval-1",
                "started_at_us": 100,
                "work_item_id": "task-1",
                "kind": "task",
                "name": "Billing",
                "parent_id": "project-1",
                "parent_name": "Client Portal",
                "repository_id": "repo-1",
                "repository_name": "usage-tracker",
                "color_hex": "#2FAF88"
              },
              "refresh": {
                "status": "partial",
                "started_at": 10,
                "finished_at": 11,
                "last_error": "codex: unavailable",
                "result": {},
                "updated_at": 11
              },
              "counts": {},
              "freshness": {}
            }
            """.utf8
        )

        let status = try JSONDecoder().decode(WorkLedgerStatus.self, from: data)

        XCTAssertTrue(status.settings.collectionEnabled)
        XCTAssertEqual(status.settings.explicitRoots, ["/repo"])
        XCTAssertEqual(status.activeWork.workItemID, "task-1")
        XCTAssertEqual(status.activeWork.parentName, "Client Portal")
        XCTAssertEqual(status.refresh.status, "partial")
        XCTAssertEqual(status.refresh.lastError, "codex: unavailable")
    }

    func testWorkItemDisplayNameIncludesProjectForChildren() throws {
        let data = Data(
            """
            {
              "id": "task-1",
              "kind": "task",
              "parent_id": "project-1",
              "repository_id": null,
              "name": "Billing",
              "color_hex": null,
              "parent_name": "Client Portal",
              "repository_name": null
            }
            """.utf8
        )

        let item = try JSONDecoder().decode(WorkItem.self, from: data)

        XCTAssertEqual(item.displayName, "Client Portal / Billing")
    }

    @MainActor
    func testRepositoryProjectChoicesIncludeEnabledRepositoriesWithoutProjects() {
        let vm = UsageViewModel(startImmediately: false)
        vm.workItems = [
            WorkItem(
                id: "usage-project",
                kind: "project",
                parentID: nil,
                repositoryID: "usage-repo",
                name: "Usage Tracker",
                colorHex: nil,
                parentName: nil,
                repositoryName: "usage-tracker"
            ),
        ]
        vm.workRepositories = [
            WorkRepository(
                id: "usage-repo",
                displayName: "usage-tracker",
                commonDir: "/usage/.git",
                enabled: true
            ),
            WorkRepository(
                id: "harvto-repo",
                displayName: "harvto",
                commonDir: "/harvto/.git",
                enabled: true
            ),
            WorkRepository(
                id: "disabled-repo",
                displayName: "archived-project",
                commonDir: "/archived/.git",
                enabled: false
            ),
        ]

        XCTAssertEqual(vm.repositoryProjectChoices.map(\.displayName), ["harvto"])
    }

    func testActiveSessionDecodesNamingRuntimeAndAssignment() throws {
        let data = Data(
            """
            {
              "sessions": [{
                "id": "session-1",
                "provider": "codex",
                "provider_session_id": "provider-session-1",
                "display_name": "Usage tagging",
                "native_title": "Session tagging",
                "nickname": "Usage tagging",
                "nickname_source": "usage_tracker",
                "runtime_state": "active",
                "last_activity_at_us": 1000000,
                "started_at_us": 100000000,
                "ended_at_us": 700000000,
                "cwd": "/repo",
                "repository_id": "repo-1",
                "repository_name": "usage-tracker",
                "worktree_path": "/repo",
                "branch": "codex/session-tags",
                "models": ["gpt-5.6-terra"],
                "assignment_mode": "work_item",
                "assigned_work_item_id": "task-1",
                "assigned_color_hex": "#2FAF88",
                "effective_work_label": "Usage Tracker / Session tags",
                "attribution_source": "session",
                "first_prompt": "Fix the session tagging report"
              }]
            }
            """.utf8
        )

        let session = try JSONDecoder().decode(WorkSessionsResponse.self, from: data).sessions[0]

        XCTAssertEqual(session.displayName, "Usage tagging")
        XCTAssertEqual(session.runtimeState, "active")
        XCTAssertEqual(session.assignmentMode, "work_item")
        XCTAssertEqual(session.assignedWorkItemID, "task-1")
        XCTAssertEqual(session.effectiveWorkLabel, "Usage Tracker / Session tags")
        XCTAssertEqual(session.reviewDurationUS, 600000000)
        XCTAssertEqual(session.firstPrompt, "Fix the session tagging report")
    }

    func testSessionAttributionStatesDistinguishReviewAutomaticConfirmedAndExcluded() {
        let base = reviewSession(id: "review", timestamp: 1, repositoryID: nil)

        XCTAssertEqual(base.attributionState, .needsReview)
        XCTAssertEqual(base.attributionReason, "No repository detected")

        let automatic = reviewSession(id: "automatic", timestamp: 1, repositoryID: "repo-1")
        XCTAssertEqual(automatic.attributionState, .automatic)

        let confirmed = reviewSession(
            id: "confirmed",
            timestamp: 1,
            repositoryID: "repo-1",
            assignmentMode: "work_item",
            attributionSource: "session"
        )
        XCTAssertEqual(confirmed.attributionState, .confirmed)

        let excluded = reviewSession(
            id: "excluded",
            timestamp: 1,
            repositoryID: nil,
            assignmentMode: "unassigned",
            attributionSource: "session"
        )
        XCTAssertEqual(excluded.attributionState, .excluded)
    }

    @MainActor
    func testReviewSessionsRespectPeriodAndReviewFilter() {
        let vm = UsageViewModel(startImmediately: false)
        let now = Date(timeIntervalSince1970: 2_000_000_000)
        var recent = reviewSession(id: "recent", timestamp: now.timeIntervalSince1970, repositoryID: nil)
        recent.inputTokens = 100
        recent.outputTokens = 20
        let old = reviewSession(
            id: "old",
            timestamp: now.timeIntervalSince1970 - (10 * 86_400),
            repositoryID: nil
        )
        let automatic = reviewSession(
            id: "automatic",
            timestamp: now.timeIntervalSince1970,
            repositoryID: "repo-1"
        )
        vm.workReviewSessions = [recent, old, automatic]

        XCTAssertEqual(vm.reviewSessions(for: .day, now: now).count, 2)
        XCTAssertEqual(vm.reviewSessions(for: .week, needsReviewOnly: true, now: now).map(\.id), ["recent"])
        XCTAssertEqual(recent.totalTokens, 120)
    }

    func testReviewGroupsCollapseSharedTitleRepositoryAndBranch() {
        let now = Date(timeIntervalSince1970: 2_000_000_000)
        var newest = reviewSession(
            id: "newest",
            timestamp: now.timeIntervalSince1970,
            repositoryID: "repo-1"
        )
        newest = WorkSession(
            id: newest.id,
            provider: "codex",
            providerSessionID: "019f9bdb-55f6-72e2-be97-d95f4eab9652",
            displayName: "loop35/base",
            nativeTitle: "loop35/base",
            nickname: nil,
            nicknameSource: nil,
            runtimeState: "active",
            lastActivityAtUS: newest.lastActivityAtUS,
            cwd: "/repo",
            repositoryID: "repo-1",
            repositoryName: "harvto",
            worktreePath: "/repo",
            branch: "loop35/base",
            models: ["gpt-5.6-sol"],
            assignmentMode: "automatic",
            assignedWorkItemID: nil,
            assignedColorHex: nil,
            effectiveWorkLabel: "harvto",
            attributionSource: "automatic",
            inputTokens: 100
        )
        var older = newest
        older = WorkSession(
            id: "older",
            provider: "claude",
            providerSessionID: "61076315-fd6b-4d33-ae34-c8733d32d234",
            displayName: newest.displayName,
            nativeTitle: newest.nativeTitle,
            nickname: nil,
            nicknameSource: nil,
            runtimeState: "ended",
            lastActivityAtUS: Int((now.timeIntervalSince1970 - 60) * 1_000_000),
            cwd: newest.cwd,
            repositoryID: newest.repositoryID,
            repositoryName: newest.repositoryName,
            worktreePath: newest.worktreePath,
            branch: newest.branch,
            models: ["claude-opus-5"],
            assignmentMode: "automatic",
            assignedWorkItemID: nil,
            assignedColorHex: nil,
            effectiveWorkLabel: "harvto",
            attributionSource: "automatic",
            outputTokens: 20
        )
        var otherBranch = newest
        otherBranch = WorkSession(
            id: "other-branch",
            provider: newest.provider,
            providerSessionID: "other-branch-session",
            displayName: newest.displayName,
            nativeTitle: newest.nativeTitle,
            nickname: nil,
            nicknameSource: nil,
            runtimeState: "ended",
            lastActivityAtUS: Int((now.timeIntervalSince1970 - 120) * 1_000_000),
            cwd: newest.cwd,
            repositoryID: newest.repositoryID,
            repositoryName: newest.repositoryName,
            worktreePath: newest.worktreePath,
            branch: "loop36/base",
            models: newest.models,
            assignmentMode: "automatic",
            assignedWorkItemID: nil,
            assignedColorHex: nil,
            effectiveWorkLabel: "harvto",
            attributionSource: "automatic"
        )

        let groups = [newest, older, otherBranch].groupedForReview()

        XCTAssertEqual(groups.count, 2)
        XCTAssertEqual(groups[0].sessions.map(\.id), ["newest", "older"])
        XCTAssertEqual(groups[0].providerSummary, "Claude + Codex")
        XCTAssertEqual(groups[0].totalTokens, 120)
        XCTAssertEqual(groups[0].attributionSummary, "2 automatic")
        XCTAssertEqual(groups[0].sessions[0].reviewChildIdentifier, "Codex · 4eab9652")
        XCTAssertFalse(groups[1].isCollection)
    }

    private func reviewSession(
        id: String,
        timestamp: TimeInterval,
        repositoryID: String?,
        assignmentMode: String = "automatic",
        attributionSource: String = "automatic"
    ) -> WorkSession {
        WorkSession(
            id: id,
            provider: "claude",
            providerSessionID: "provider-\(id)",
            displayName: id,
            nativeTitle: nil,
            nickname: nil,
            nicknameSource: nil,
            runtimeState: "ended",
            lastActivityAtUS: Int(timestamp * 1_000_000),
            cwd: nil,
            repositoryID: repositoryID,
            repositoryName: repositoryID == nil ? nil : "usage-tracker",
            worktreePath: nil,
            branch: nil,
            models: [],
            assignmentMode: assignmentMode,
            assignedWorkItemID: nil,
            assignedColorHex: nil,
            effectiveWorkLabel: repositoryID == nil ? "Automatic" : "usage-tracker",
            attributionSource: attributionSource
        )
    }

    func testWorkReportDecodesPricingAndHierarchy() throws {
        let metrics =
            """
            {
              "tracked_seconds": 3600.0,
              "active_seconds": 4500.0,
              "session_count": 2,
              "messages": 4,
              "user_messages": 2,
              "requests": 3,
              "input_tokens": 100,
              "output_tokens": 50,
              "cache_tokens": 25,
              "reasoning_tokens": 10,
              "total_tokens": 175,
              "estimated_cost_usd": 0.25,
              "estimated_credits": 1.5,
              "pricing_coverage_pct": 80.0,
              "unpriced_models": ["future-model"],
              "unpriced_reasons": {"future-model": "No published rate"},
              "activity_events": 3,
              "git_commits": 1,
              "git_pull_requests": 1,
              "merge_commits": 1,
              "files_changed": 2,
              "additions": 10,
              "deletions": 4,
              "changed_lines": 14,
              "models": [{"model": "gpt-5.6-terra", "tokens": 175, "requests": 3}],
              "event_kinds": {"git_commit": 1, "edit": 2},
              "usage_categories": []
            }
            """
        let data = Data(
            """
            {
              "period": "week",
              "generated_at": 100,
              "window": {"start_at": 1, "end_at": 100, "timezone": "UTC"},
              "totals": \(metrics),
              "unassigned": \(metrics),
              "projects": [{
                "id": "project-1", "name": "Usage Tracker", "kind": "project",
                "repository_id": "repo-1", "repository_name": "usage-tracker",
                "color_hex": "#2FAF88", "inferred": true,
                "attribution_source": "linked_repository",
                "attribution_confidence": 0.85, "metrics": \(metrics)
              }],
              "work_items": [{
                "id": "task-1", "name": "Reporting", "kind": "task",
                "parent_id": "project-1", "parent_name": "Usage Tracker",
                "repository_id": "repo-1", "repository_name": "usage-tracker",
                "color_hex": null, "metrics": \(metrics)
              }],
              "repositories": [{
                "id": "repo-1", "name": "usage-tracker", "enabled": true,
                "metrics": \(metrics)
              }],
              "providers": [{"id": "codex", "name": "Codex", "metrics": \(metrics)}]
            }
            """.utf8
        )

        let report = try JSONDecoder().decode(WorkReport.self, from: data)

        XCTAssertEqual(report.projects.first?.name, "Usage Tracker")
        XCTAssertEqual(report.workItems.first?.parentID, "project-1")
        XCTAssertEqual(report.totals.pricingCoveragePct, 80)
        XCTAssertEqual(report.totals.unpricedModels, ["future-model"])
        XCTAssertEqual(report.totals.pullRequestCount, 1)
        XCTAssertEqual(report.totals.changedLineCount, 14)
        XCTAssertEqual(report.totals.activeSeconds, 4500)
        XCTAssertEqual(report.totals.reportedSeconds, 4500)
        XCTAssertTrue(report.projects.first?.inferred == true)
        XCTAssertEqual(report.projects.first?.attributionSource, "linked_repository")
        XCTAssertTrue(report.repositories.first?.enabled == true)
    }

    private func metricsJSON(ratios: String) -> String {
        """
        {
          "tracked_seconds": 60, "active_seconds": 60, "session_count": 1,
          "messages": 1, "user_messages": 1, "requests": 1,
          "input_tokens": 10, "output_tokens": 10, "cache_tokens": 0,
          "reasoning_tokens": 0, "total_tokens": 20,
          "estimated_cost_usd": 12.0, "estimated_credits": null,
          "pricing_coverage_pct": 100.0, "unpriced_models": [],
          "activity_events": 1, "git_commits": 8, "git_pull_requests": 4,
          "merge_commits": 0, "files_changed": 2, "additions": 200,
          "deletions": 100, "changed_lines": 300,
          \(ratios)
          "models": [], "event_kinds": {}, "usage_categories": []
        }
        """
    }

    func testCostRatiosDecodeFromTheServerModel() throws {
        let json = metricsJSON(ratios: """
            "cost_per_pr": 3.0, "cost_per_commit": 1.5,
            "cost_per_changed_line": 0.04,
            """)

        let metrics = try JSONDecoder().decode(
            WorkReportMetrics.self, from: Data(json.utf8)
        )

        XCTAssertEqual(metrics.costPerPR, 3.0)
        XCTAssertEqual(metrics.costPerCommit, 1.5)
        XCTAssertEqual(metrics.costPerChangedLine, 0.04)
    }

    func testSuppressedCostRatiosDecodeAsNilNotZero() throws {
        // The server sends null when a ratio is not stateable. That must arrive as
        // nil so the view renders a placeholder; decoding it as 0 would claim the
        // work was free.
        let json = metricsJSON(ratios: """
            "cost_per_pr": null, "cost_per_commit": null,
            "cost_per_changed_line": null,
            """)

        let metrics = try JSONDecoder().decode(
            WorkReportMetrics.self, from: Data(json.utf8)
        )

        XCTAssertNil(metrics.costPerPR)
        XCTAssertNil(metrics.costPerCommit)
        XCTAssertNil(metrics.costPerChangedLine)
    }

    func testMissingCostRatioKeysStayNilForOlderServers() throws {
        // A server predating the ratios omits the keys entirely; the app must still
        // decode rather than fail, and must not invent a value.
        let json = metricsJSON(ratios: "")

        let metrics = try JSONDecoder().decode(
            WorkReportMetrics.self, from: Data(json.utf8)
        )

        XCTAssertNil(metrics.costPerPR)
        XCTAssertNil(metrics.costPerCommit)
        XCTAssertNil(metrics.costPerChangedLine)
        XCTAssertEqual(metrics.gitCommits, 8)
    }
}
