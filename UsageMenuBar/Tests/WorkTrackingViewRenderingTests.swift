import AppKit
import SwiftUI
import XCTest
@testable import UsageMenuBar

@MainActor
final class WorkTrackingViewRenderingTests: XCTestCase {
    func testStatusPopoverContentRendersAtStableDimensions() throws {
        let vm = UsageViewModel(startImmediately: false)
        let rendered = try renderInWindow(
            StatusPopoverContent(vm: vm),
            size: NSSize(width: 340, height: 442),
            name: "usage-tracker-status-menu"
        )

        XCTAssertEqual(CGFloat(rendered.image.width) / rendered.scale, 340)
        XCTAssertEqual(CGFloat(rendered.image.height) / rendered.scale, 442)
    }

    func testClaudePaidUsageRowRendersActualSpend() throws {
        let vm = UsageViewModel(startImmediately: false)
        let rendered = try renderInWindow(
            MenuContent(vm: vm).toolRow(
                name: "Claude",
                selected: true,
                sessionPct: 100,
                weeklyPct: nil,
                amountUSD: 80.84,
                amountIsEstimate: false,
                amountLabel: "Usage credits spent this month",
                gaugeLabel: "Session quota",
                status: "paid usage",
                reset: "Jul 25 7:30 PM",
                plan: "Max 5x",
                summary: "$80.84 spent this month · session resets 7:30p",
                onTap: {}
            )
            .frame(width: 340, height: 54)
            .background(Color(nsColor: .windowBackgroundColor)),
            size: NSSize(width: 340, height: 54),
            name: "usage-tracker-claude-paid-row"
        )

        XCTAssertEqual(CGFloat(rendered.image.width) / rendered.scale, 340)
        XCTAssertEqual(CGFloat(rendered.image.height) / rendered.scale, 54)
    }

    func testClaudeEmptyCreditBalanceRowRendersActualSpend() throws {
        let vm = UsageViewModel(startImmediately: false)
        let rendered = try renderInWindow(
            MenuContent(vm: vm).toolRow(
                name: "Claude",
                selected: true,
                sessionPct: 100,
                weeklyPct: nil,
                amountUSD: 84.65,
                amountIsEstimate: false,
                amountLabel: "Usage credits spent this month",
                gaugeLabel: "Session quota",
                status: "credit balance empty",
                reset: "Jul 26 12:30 AM",
                plan: "Max 5x",
                summary: "No prepaid funds left · $84.65 spent this month · session resets 12:30a",
                onTap: {}
            )
            .frame(width: 340, height: 54)
            .background(Color(nsColor: .windowBackgroundColor)),
            size: NSSize(width: 340, height: 54),
            name: "usage-tracker-claude-empty-credit-row"
        )

        XCTAssertEqual(CGFloat(rendered.image.width) / rendered.scale, 340)
        XCTAssertEqual(CGFloat(rendered.image.height) / rendered.scale, 54)
    }

    func testCodexPaidCreditRowRendersActualCreditsAndCheckoutDollars() throws {
        let vm = UsageViewModel(startImmediately: false)
        let rendered = try renderInWindow(
            MenuContent(vm: vm).toolRow(
                name: "Codex",
                selected: true,
                sessionPct: 100,
                weeklyPct: nil,
                amountUSD: 17.43,
                amountIsEstimate: false,
                amountLabel: "Purchased credit usage",
                gaugeLabel: "Weekly quota",
                status: "paid credits",
                reset: "Aug 1 12:18 PM",
                plan: "Pro",
                summary: "435.765 cr used · $17.43 · 30d",
                onTap: {}
            )
            .frame(width: 340, height: 54)
            .background(Color(nsColor: .windowBackgroundColor)),
            size: NSSize(width: 340, height: 54),
            name: "usage-tracker-codex-paid-row"
        )

        XCTAssertEqual(CGFloat(rendered.image.width) / rendered.scale, 340)
        XCTAssertEqual(CGFloat(rendered.image.height) / rendered.scale, 54)
    }

    func testAutomaticWorkAttributionControlRendersCompactly() throws {
        let vm = UsageViewModel(startImmediately: false)
        vm.workTrackingEnabled = true

        let rendered = try renderInWindow(
            WorkTrackingBar(vm: vm)
                .frame(width: 340, height: 52)
                .background(Color(nsColor: .windowBackgroundColor)),
            size: NSSize(width: 340, height: 52),
            name: "usage-tracker-auto-attribution"
        )

        XCTAssertEqual(CGFloat(rendered.image.width) / rendered.scale, 340)
        XCTAssertEqual(CGFloat(rendered.image.height) / rendered.scale, 52)
    }

    func testCollapsibleProviderSectionsRenderCompactly() throws {
        let rendered = try renderInWindow(
            VStack(alignment: .leading, spacing: 0) {
                CollapsibleDetailSection(
                    title: "Quota",
                    systemImage: "speedometer",
                    summary: "Weekly quota 61% used",
                    isExpanded: .constant(true)
                ) {
                    detailLine("Weekly quota", val: "61% used · resets Jul 30 5:31 PM")
                    detailLine("Fable", val: "22% used")
                }
                CollapsibleDetailSection(
                    title: "Usage Drivers",
                    systemImage: "chart.bar.xaxis",
                    summary: "65.6M effective · $324.02",
                    isExpanded: .constant(false)
                ) { EmptyView() }
                CollapsibleDetailSection(
                    title: "Activity & Models",
                    systemImage: "bolt",
                    summary: "Active 4.8h today",
                    isExpanded: .constant(false)
                ) { EmptyView() }
                CollapsibleDetailSection(
                    title: "Cost & Account",
                    systemImage: "dollarsign.circle",
                    summary: "Value $324.02",
                    isExpanded: .constant(false)
                ) { EmptyView() }
                CollapsibleDetailSection(
                    title: "Trend & Sources",
                    systemImage: "waveform.path.ecg",
                    summary: "Healthy",
                    isExpanded: .constant(false)
                ) { EmptyView() }
            }
            .font(.system(.caption, design: .rounded))
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .frame(width: 340, height: 220, alignment: .top)
            .background(Color(nsColor: .windowBackgroundColor)),
            size: NSSize(width: 340, height: 220),
            name: "usage-tracker-collapsible-sections"
        )

        XCTAssertEqual(CGFloat(rendered.image.width) / rendered.scale, 340)
        XCTAssertEqual(CGFloat(rendered.image.height) / rendered.scale, 220)
    }

    func testUsageExplanationRendersAtMenuWidth() throws {
        let explanation = UsageExplanation(
            provider: "claude",
            period: "day",
            windowStartAt: "2026-07-25T00:00:00+00:00",
            windowEndAt: "2026-07-25T12:00:00+00:00",
            estimateBasis: "local_transcript_attribution",
            quotaRelation: "independent_estimate",
            coverage: UsageExplanationCoverage(
                status: "complete",
                confidence: "medium",
                compactions: 0,
                classifiedPct: 98
            ),
            totals: UsageExplanationTotals(
                inputTokens: 100_000,
                outputTokens: 20_000,
                cacheReadTokens: 300_000,
                cacheWrite5mTokens: 10_000,
                cacheWrite1hTokens: 0,
                reasoningTokens: 0,
                totalTokens: 430_000,
                effectiveTokens: 250_000,
                eventCount: 12,
                sessions: 2,
                estimatedCostUSD: 2.75,
                estimatedCredits: nil,
                pricingCoveragePct: 100,
                unpricedModels: []
            ),
            categories: [
                explanationCategory("files", "Files", "#3DA58A", 55),
                explanationCategory("model_output", "Model output", "#34B27B", 30),
                explanationCategory("instructions", "Instructions", "#7D8590", 15),
            ]
        )

        let rendered = try renderInWindow(
            UsageExplanationView(explanation: explanation, isLoading: false, error: nil)
                .padding(14)
                .frame(width: 340, height: 180, alignment: .top)
                .background(Color(nsColor: .windowBackgroundColor)),
            size: NSSize(width: 340, height: 180),
            name: "usage-explanation"
        )

        XCTAssertEqual(CGFloat(rendered.image.width) / rendered.scale, 340)
        XCTAssertEqual(CGFloat(rendered.image.height) / rendered.scale, 180)
    }

    func testWorkTrackingSurfacesRenderAtStableDimensions() throws {
        let vm = UsageViewModel(startImmediately: false)
        let project = WorkItem(
            id: "project-1",
            kind: "project",
            parentID: nil,
            repositoryID: "repo-1",
            name: "Usage Tracker",
            colorHex: "#2FAF88",
            parentName: nil,
            repositoryName: "usage-tracker"
        )
        let task = WorkItem(
            id: "task-1",
            kind: "task",
            parentID: project.id,
            repositoryID: "repo-1",
            name: "Project attribution controls",
            colorHex: nil,
            parentName: project.name,
            repositoryName: "usage-tracker"
        )
        vm.workTrackingEnabled = true
        vm.workItems = [project, task]
        vm.workRepositories = [WorkRepository(
            id: "repo-1",
            displayName: "usage-tracker",
            commonDir: "/repo/.git",
            enabled: true
        )]
        vm.activeWork = ActiveWorkContext(
            state: "active",
            intervalID: "interval-1",
            startedAtUS: 100,
            workItemID: task.id,
            kind: task.kind,
            name: task.name,
            parentID: project.id,
            parentName: project.name,
            repositoryID: "repo-1",
            repositoryName: "usage-tracker",
            colorHex: project.colorHex
        )
        vm.workStatus = WorkLedgerStatus(
            schemaVersion: 5,
            settings: WorkLedgerSettings(
                collectionEnabled: true,
                pausedAt: nil,
                explicitRoots: ["/repo"],
                retentionDays: 90,
                updatedAt: 10
            ),
            activeWork: vm.activeWork,
            refresh: WorkRefreshState(
                status: "ok",
                startedAt: 9,
                finishedAt: 10,
                lastError: nil,
                updatedAt: 10
            )
        )
        let nowUS = Int(Date().timeIntervalSince1970 * 1_000_000)
        let automaticSession = WorkSession(
            id: "session-1",
            provider: "codex",
            providerSessionID: "provider-session-1",
            displayName: "Session work attribution",
            nativeTitle: "Session work attribution",
            nickname: nil,
            nicknameSource: nil,
            runtimeState: "active",
            lastActivityAtUS: nowUS,
            cwd: "/repo",
            repositoryID: "repo-1",
            repositoryName: "usage-tracker",
            worktreePath: "/repo",
            branch: "codex/session-work-tagging",
            models: ["gpt-5.6-terra"],
            assignmentMode: "automatic",
            assignedWorkItemID: nil,
            assignedColorHex: nil,
            effectiveWorkLabel: "Usage Tracker / Project attribution controls",
            attributionSource: "default"
        )
        let reviewSession = WorkSession(
            id: "session-2",
            provider: "claude",
            providerSessionID: "provider-session-2",
            displayName: "Investigate usage limits",
            nativeTitle: "Investigate usage limits",
            nickname: nil,
            nicknameSource: nil,
            runtimeState: "ended",
            lastActivityAtUS: nowUS - 3_600_000_000,
            cwd: "/private/tmp/unmatched-usage-review",
            repositoryID: nil,
            repositoryName: nil,
            worktreePath: nil,
            branch: nil,
            models: ["claude-fable-5"],
            assignmentMode: "automatic",
            assignedWorkItemID: nil,
            assignedColorHex: nil,
            effectiveWorkLabel: "Automatic",
            attributionSource: "automatic",
            startedAtUS: nowUS - 5_400_000_000,
            endedAtUS: nowUS - 3_600_000_000,
            inputTokens: 120_000,
            outputTokens: 8_000,
            cacheTokens: 40_000,
            estimatedCostUSD: 1.42,
            firstPrompt: "Compare the Codex weekly total with the local session usage and explain the mismatch"
        )
        let groupedReviewSession = WorkSession(
            id: "session-3",
            provider: "codex",
            providerSessionID: "019f9bd2-775a-7662-af18-ac43513ae0f9",
            displayName: reviewSession.displayName,
            nativeTitle: reviewSession.nativeTitle,
            nickname: nil,
            nicknameSource: nil,
            runtimeState: "ended",
            lastActivityAtUS: nowUS - 3_660_000_000,
            cwd: reviewSession.cwd,
            repositoryID: reviewSession.repositoryID,
            repositoryName: reviewSession.repositoryName,
            worktreePath: reviewSession.worktreePath,
            branch: reviewSession.branch,
            models: ["gpt-5.6-sol"],
            assignmentMode: reviewSession.assignmentMode,
            assignedWorkItemID: nil,
            assignedColorHex: nil,
            effectiveWorkLabel: reviewSession.effectiveWorkLabel,
            attributionSource: reviewSession.attributionSource,
            startedAtUS: nowUS - 5_200_000_000,
            endedAtUS: nowUS - 3_660_000_000,
            inputTokens: 37_000,
            outputTokens: 400,
            firstPrompt: "Check whether the local Codex usage window was reset"
        )
        vm.workSessions = [automaticSession]
        vm.workReviewSessions = [automaticSession, reviewSession, groupedReviewSession]
        vm.sessionEvidence[automaticSession.id] = SessionEvidenceResponse(
            sessionID: automaticSession.id,
            provider: automaticSession.provider,
            providerSessionID: automaticSession.providerSessionID,
            items: [
                SessionEvidenceItem(
                    id: "evidence-1",
                    occurredAtUS: nowUS - 120_000_000,
                    kind: "prompt",
                    title: "Prompt",
                    summary: "Add a read-only session evidence timeline",
                    toolName: nil,
                    commandCategory: nil,
                    filePaths: []
                ),
                SessionEvidenceItem(
                    id: "evidence-2",
                    occurredAtUS: nowUS - 60_000_000,
                    kind: "test",
                    title: "Ran tests",
                    summary: "swift test",
                    toolName: "exec_command",
                    commandCategory: "test",
                    filePaths: []
                ),
            ],
            truncated: false,
            sourceMissing: false,
            bytesScanned: 2048
        )
        vm.sessionSearchIndexStatus = SessionSearchIndexStatus(
            state: "ready",
            backend: "sqlite_fts5",
            privacyMode: "contentless",
            sessionsIndexed: 791,
            sessionsAvailable: 791,
            documents: 146_083,
            staleSessions: 2,
            missingSources: 2,
            lastIndexedAtUS: nowUS,
            dbBytes: 78_999_552,
            lastError: nil
        )

        let bar = try renderInWindow(
            WorkTrackingBar(vm: vm)
                .frame(width: 340, height: 52)
                .background(Color(nsColor: .windowBackgroundColor)),
            size: NSSize(width: 340, height: 52),
            name: "usage-tracker-work-bar"
        )
        let settings = try renderInWindow(
            SettingsView(vm: vm)
                .frame(width: 480, height: 650),
            size: NSSize(width: 480, height: 650),
            name: "usage-tracker-work-settings"
        )
        vm.workReport = sampleReport(project: project, task: task)
        let workspace = try renderInWindow(
            WorkWorkspaceView(vm: vm, loadsOnAppear: false)
                .frame(width: 720, height: 680),
            size: NSSize(width: 720, height: 680),
            name: "usage-tracker-work-review"
        )
        let report = try renderInWindow(
            WorkReportView(vm: vm, loadsOnAppear: false)
                .frame(width: 720, height: 680),
            size: NSSize(width: 720, height: 680),
            name: "usage-tracker-work-report"
        )
        let timeline = try renderInWindow(
            SessionTimelineWorkspaceView(
                vm: vm,
                selectedSessionID: .constant(automaticSession.id),
                highlightedEvidenceID: .constant("evidence-2")
            )
            .frame(width: 720, height: 620),
            size: NSSize(width: 720, height: 620),
            name: "usage-tracker-session-evidence"
        )

        XCTAssertEqual(CGFloat(bar.image.width) / bar.scale, 340)
        XCTAssertEqual(CGFloat(bar.image.height) / bar.scale, 52)
        XCTAssertEqual(CGFloat(settings.image.width) / settings.scale, 480)
        XCTAssertEqual(CGFloat(settings.image.height) / settings.scale, 650)
        XCTAssertEqual(CGFloat(workspace.image.width) / workspace.scale, 720)
        XCTAssertEqual(CGFloat(workspace.image.height) / workspace.scale, 680)
        XCTAssertEqual(CGFloat(report.image.width) / report.scale, 720)
        XCTAssertEqual(CGFloat(report.image.height) / report.scale, 680)
        XCTAssertEqual(CGFloat(timeline.image.width) / timeline.scale, 720)
        XCTAssertEqual(CGFloat(timeline.image.height) / timeline.scale, 620)
    }

    private func sampleReport(project: WorkItem, task: WorkItem) -> WorkReport {
        let totals = WorkReportMetrics(
            trackedSeconds: 10_800,
            activeSeconds: 12_600,
            sessionCount: 3,
            messages: 43,
            userMessages: 17,
            requests: 9,
            inputTokens: 900_000,
            outputTokens: 200_000,
            cacheTokens: 200_000,
            reasoningTokens: 2_600,
            totalTokens: 1_300_000,
            estimatedCostUSD: 4.82,
            estimatedCredits: 11.2,
            pricingCoveragePct: 92.4,
            unpricedModels: ["gpt-5.6-sol"],
            activityEvents: 14,
            gitCommits: 2,
            gitPullRequests: 1,
            mergeCommits: 1,
            filesChanged: 8,
            additions: 210,
            deletions: 45,
            changedLines: 255,
            // Ratios come from the server; these match this fixture's own figures
            // (4.82 over 1 PR, 2 commits and 255 changed lines).
            costPerPR: 4.82,
            costPerCommit: 2.41,
            costPerChangedLine: 0.018902,
            models: [WorkReportModelUsage(model: "gpt-5.6-terra", tokens: 1_300_000, requests: 9)],
            eventKinds: ["edit": 8, "git_commit": 2, "test": 4],
            usageCategories: [WorkReportUsageCategory(
                id: "files",
                label: "Files",
                color: "#3DA58A",
                totalTokens: 700_000,
                effectiveTokens: 800_000,
                sharePct: 61.5,
                eventCount: 8,
                estimatedCostUSD: 2.6,
                estimatedCredits: nil
            )]
        )
        let projectBucket = WorkReportBucket(
            id: project.id,
            name: project.name,
            kind: project.kind,
            parentID: nil,
            parentName: nil,
            repositoryID: project.repositoryID,
            repositoryName: project.repositoryName,
            colorHex: project.colorHex,
            enabled: nil,
            inferred: false,
            attributionSource: "manual",
            attributionConfidence: 1,
            metrics: totals
        )
        let taskBucket = WorkReportBucket(
            id: task.id,
            name: task.name,
            kind: task.kind,
            parentID: project.id,
            parentName: project.name,
            repositoryID: task.repositoryID,
            repositoryName: task.repositoryName,
            colorHex: project.colorHex,
            enabled: nil,
            inferred: true,
            attributionSource: "branch_match",
            attributionConfidence: 0.9,
            metrics: totals
        )
        let repository = WorkReportBucket(
            id: "repo-1",
            name: "usage-tracker",
            kind: nil,
            parentID: nil,
            parentName: nil,
            repositoryID: nil,
            repositoryName: nil,
            colorHex: nil,
            enabled: true,
            inferred: false,
            attributionSource: "repository",
            attributionConfidence: 1,
            metrics: totals
        )
        let provider = WorkReportBucket(
            id: "codex",
            name: "Codex",
            kind: nil,
            parentID: nil,
            parentName: nil,
            repositoryID: nil,
            repositoryName: nil,
            colorHex: nil,
            enabled: nil,
            inferred: false,
            attributionSource: nil,
            attributionConfidence: nil,
            metrics: totals
        )
        return WorkReport(
            period: "week",
            generatedAt: 100,
            window: WorkReportWindow(startAt: 1, endAt: 100, timezone: "UTC"),
            totals: totals,
            unassigned: totals,
            projects: [projectBucket],
            workItems: [projectBucket, taskBucket],
            repositories: [repository],
            providers: [provider]
        )
    }

    private func explanationCategory(
        _ id: String,
        _ label: String,
        _ color: String,
        _ share: Double
    ) -> UsageExplanationCategory {
        UsageExplanationCategory(
            id: id,
            label: label,
            color: color,
            inputTokens: 50_000,
            outputTokens: id == "model_output" ? 20_000 : 0,
            cacheReadTokens: 100_000,
            cacheWrite5mTokens: 0,
            cacheWrite1hTokens: 0,
            reasoningTokens: 0,
            totalTokens: 150_000,
            effectiveTokens: 100_000,
            sharePct: share,
            eventCount: 4,
            estimatedCostUSD: 0.75,
            estimatedCredits: nil,
            pricingCoveragePct: 100,
            models: ["claude-fable-5"],
            unpricedModels: [],
            confidence: id == "instructions" ? "low" : "medium"
        )
    }

    private func renderInWindow<Content: View>(
        _ content: Content,
        size: NSSize,
        name: String
    ) throws -> RenderedView {
        let hostingView = NSHostingView(rootView: content)
        hostingView.frame = NSRect(origin: .zero, size: size)
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: size),
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        window.appearance = NSAppearance(named: .aqua)
        window.backgroundColor = .windowBackgroundColor
        window.contentView = hostingView
        window.orderFrontRegardless()
        hostingView.layoutSubtreeIfNeeded()

        guard let bitmap = hostingView.bitmapImageRepForCachingDisplay(in: hostingView.bounds) else {
            XCTFail("AppKit returned no bitmap for the rendered view")
            throw RenderingError.noImage
        }
        hostingView.cacheDisplay(in: hostingView.bounds, to: bitmap)
        guard let image = bitmap.cgImage else {
            XCTFail("AppKit returned no image for the rendered view")
            throw RenderingError.noImage
        }
        if ProcessInfo.processInfo.environment["USAGE_TRACKER_RENDER_ARTIFACTS"] == "1" {
            let data = bitmap.representation(using: .png, properties: [:])
            try data?.write(to: URL(fileURLWithPath: "/tmp/\(name).png"))
        }
        window.orderOut(nil)
        let scale = CGFloat(bitmap.pixelsWide) / size.width
        return RenderedView(image: image, scale: scale)
    }
}

private struct RenderedView {
    let image: CGImage
    let scale: CGFloat
}

private enum RenderingError: Error {
    case noImage
}
