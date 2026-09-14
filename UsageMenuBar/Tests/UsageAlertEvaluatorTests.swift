import XCTest
@testable import UsageMenuBar

final class UsageAlertEvaluatorTests: XCTestCase {
    private func quota(_ used: Double, kind: String = "weekly") -> AlertQuotaSnapshot {
        AlertQuotaSnapshot(
            provider: "codex",
            id: kind,
            label: kind == "session" ? "5 hour quota" : "Weekly quota",
            windowKind: kind,
            scopeKind: "aggregate",
            usedPct: used,
            remainingPct: 100 - used,
            reset: "Jul 30 5:31 PM"
        )
    }

    private func snapshot(
        _ quotas: [AlertQuotaSnapshot],
        burn: Double = 0,
        stale: Set<String> = []
    ) -> UsageAlertSnapshot {
        UsageAlertSnapshot(quotas: quotas, burnRates: ["codex": burn], staleProviders: stale)
    }

    func testThresholdsFireOnlyWhenCrossed() {
        let warning = UsageAlertEvaluator.evaluate(
            current: snapshot([quota(75)]),
            previous: snapshot([quota(65)])
        )
        XCTAssertEqual(warning.map(\.kind), [.threshold70])

        let critical = UsageAlertEvaluator.evaluate(
            current: snapshot([quota(92)]),
            previous: snapshot([quota(75)])
        )
        XCTAssertEqual(critical.map(\.kind), [.threshold90])

        XCTAssertTrue(UsageAlertEvaluator.evaluate(
            current: snapshot([quota(95)]),
            previous: snapshot([quota(92)])
        ).isEmpty)
    }

    func testRestoreFiresAfterHighUsageDrops() {
        let events = UsageAlertEvaluator.evaluate(
            current: snapshot([quota(4)]),
            previous: snapshot([quota(95)])
        )
        XCTAssertEqual(events.map(\.kind), [.quotaRestored])
    }

    func testProjectedExhaustionFiresWhenCrossingTwoHours() {
        let current = quota(80, kind: "session")
        let previous = quota(60, kind: "session")
        let events = UsageAlertEvaluator.evaluate(
            current: snapshot([current], burn: 12),
            previous: snapshot([previous], burn: 10)
        )
        XCTAssertEqual(events.map(\.kind), [.projectedExhaustion])
    }

    func testStaleCredentialsFireOnTransition() {
        let events = UsageAlertEvaluator.evaluate(
            current: snapshot([], stale: ["codex"]),
            previous: snapshot([])
        )
        XCTAssertEqual(events.map(\.kind), [.credentialsStale])
        XCTAssertTrue(UsageAlertEvaluator.evaluate(
            current: snapshot([], stale: ["codex"]),
            previous: snapshot([], stale: ["codex"])
        ).isEmpty)
    }

    func testCooldownSuppressesRecentDeliveryAndAllowsExpiredEvent() {
        let event = UsageAlertEvent(
            id: "codex.weekly.threshold.70",
            kind: .threshold70,
            title: "Codex quota at 70%",
            body: "Weekly quota usage is elevated."
        )
        let now: TimeInterval = 100_000

        XCTAssertTrue(UsageAlertCooldown.due(
            [event], lastSent: [event.id: now - 60], now: now
        ).isEmpty)
        XCTAssertEqual(UsageAlertCooldown.due(
            [event], lastSent: [event.id: now - event.cooldown - 1], now: now
        ), [event])
    }

    func testStandaloneExecutableUsesAppleScriptNotifications() {
        let executableDirectory = URL(fileURLWithPath: "/tmp/UsageMenuBar.build/release")
        XCTAssertEqual(
            UsageNotificationDeliveryMode.current(bundleURL: executableDirectory),
            .appleScript
        )
    }

    func testAppBundleUsesUserNotifications() {
        let appBundle = URL(fileURLWithPath: "/Applications/Usage Tracker.app")
        XCTAssertEqual(
            UsageNotificationDeliveryMode.current(bundleURL: appBundle),
            .userNotifications
        )
    }

}

final class QuotaCostBucketVisibilityTests: XCTestCase {
    private func bucket(windowKind: String, scopeKind: String? = "aggregate") -> QuotaBucket {
        QuotaBucket(
            id: "test-\(windowKind)-\(scopeKind ?? "default")",
            label: nil,
            windowKind: windowKind,
            windowMinutes: nil,
            scopeKind: scopeKind,
            model: scopeKind == "model" ? "claude-opus-5" : nil,
            feature: nil,
            usedPct: 35,
            remainingPct: 65,
            reset: nil
        )
    }

    func testQuotaWeekHidesMatchingAggregateWeeklyValue() {
        XCTAssertFalse(shouldShowQuotaCostBucket(
            bucket(windowKind: "weekly"),
            selectedWindowKinds: ["weekly", "monthly", "account"],
            period: .week
        ))
    }

    func testQuotaWeekKeepsModelSpecificAndOtherWindowValues() {
        XCTAssertTrue(shouldShowQuotaCostBucket(
            bucket(windowKind: "weekly", scopeKind: "model"),
            selectedWindowKinds: ["weekly", "monthly", "account"],
            period: .week
        ))
        XCTAssertTrue(shouldShowQuotaCostBucket(
            bucket(windowKind: "monthly"),
            selectedWindowKinds: ["weekly", "monthly", "account"],
            period: .week
        ))
    }

    func testTodayKeepsSessionQuotaValue() {
        XCTAssertTrue(shouldShowQuotaCostBucket(
            bucket(windowKind: "session"),
            selectedWindowKinds: ["session"],
            period: .day
        ))
    }
}

final class MenuMetricPresentationTests: XCTestCase {
    private func usageCredits(
        enabled: Bool = true,
        spendControlReached: Bool = false,
        outOfCredits: Bool? = false,
        balance: Double? = 218.76
    ) -> CreditPool {
        CreditPool(
            id: "claude-usage-credits",
            label: "Usage credits",
            kind: "prepaid",
            unit: "usd",
            enabled: enabled,
            unlimited: false,
            spendControlReached: spendControlReached,
            outOfCredits: outOfCredits,
            balance: balance,
            spentMonth: 80.84,
            monthlySpendCap: 150,
            monthlySpendHeadroom: 69.16,
            usedPct: 53.89,
            reset: "Aug 1 12:00 AM",
            nextExpiryAt: nil
        )
    }

    private func weeklyLimit(usedPct: Double) -> QuotaBucket {
        QuotaBucket(
            id: "claude-weekly",
            label: "Weekly",
            windowKind: "weekly",
            windowMinutes: nil,
            scopeKind: "aggregate",
            model: nil,
            feature: nil,
            usedPct: usedPct,
            remainingPct: 100 - usedPct,
            reset: "Jul 27 6:00 PM"
        )
    }

    private func codexPurchasedCredits(
        balance: Double = 158,
        enabled: Bool = true,
        spendControlReached: Bool = false
    ) -> CreditPool {
        CreditPool(
            id: "codex-shared-credits",
            label: "Shared credits",
            kind: "purchased",
            unit: "credits",
            enabled: enabled,
            unlimited: false,
            spendControlReached: spendControlReached,
            outOfCredits: nil,
            balance: balance,
            spentMonth: nil,
            monthlySpendCap: nil,
            monthlySpendHeadroom: nil,
            usedPct: nil,
            reset: nil,
            nextExpiryAt: nil
        )
    }

    private func bankedReset() -> CreditPool {
        CreditPool(
            id: "codex-banked-resets",
            label: "Banked resets",
            kind: "reset",
            unit: "resets",
            enabled: nil,
            unlimited: nil,
            spendControlReached: nil,
            outOfCredits: nil,
            balance: 1,
            spentMonth: nil,
            monthlySpendCap: nil,
            monthlySpendHeadroom: nil,
            usedPct: nil,
            reset: nil,
            nextExpiryAt: nil
        )
    }

    func testClaudePaidUsageActivatesAtAnExhaustedIncludedLimit() {
        let sessionPresentation = claudePaidUsagePresentation(
            sessionUsedPct: 100,
            limits: [],
            creditPools: [usageCredits()]
        )
        let weeklyPresentation = claudePaidUsagePresentation(
            sessionUsedPct: 20,
            limits: [weeklyLimit(usedPct: 100)],
            creditPools: [usageCredits()]
        )

        XCTAssertEqual(sessionPresentation?.state, .active)
        XCTAssertEqual(sessionPresentation?.spentMonth, 80.84)
        XCTAssertEqual(sessionPresentation?.monthlySpendHeadroom, 69.16)
        XCTAssertEqual(weeklyPresentation?.state, .active)
    }

    func testClaudePaidUsageRequiresEnabledCreditsAndAnExhaustedLimit() {
        XCTAssertNil(claudePaidUsagePresentation(
            sessionUsedPct: 99,
            limits: [weeklyLimit(usedPct: 53)],
            creditPools: [usageCredits()]
        ))
        XCTAssertNil(claudePaidUsagePresentation(
            sessionUsedPct: 100,
            limits: [],
            creditPools: [usageCredits(enabled: false)]
        ))
        XCTAssertNil(claudePaidUsagePresentation(
            sessionUsedPct: 100,
            limits: [],
            creditPools: [usageCredits(outOfCredits: nil, balance: nil)]
        ))
    }

    func testClaudePaidUsageReportsEmptyPrepaidBalance() {
        let presentation = claudePaidUsagePresentation(
            sessionUsedPct: 100,
            limits: [],
            creditPools: [usageCredits(outOfCredits: true, balance: 0)]
        )

        XCTAssertEqual(presentation?.state, .balanceEmpty)
        XCTAssertEqual(presentation?.spentMonth, 80.84)
    }

    func testClaudePaidUsageReportsSpendControlReached() {
        let presentation = claudePaidUsagePresentation(
            sessionUsedPct: 100,
            limits: [],
            creditPools: [usageCredits(spendControlReached: true)]
        )

        XCTAssertEqual(presentation?.state, .spendControlReached)
        XCTAssertEqual(presentation?.monthlySpendCap, 150)
    }

    func testCodexPaidCreditsActivateAtAnExhaustedLimit() {
        let presentation = codexPaidCreditPresentation(
            limits: [weeklyLimit(usedPct: 100)],
            creditPools: [codexPurchasedCredits(), bankedReset()],
            creditsUsed: 435.765,
            usdValue: 17.43,
            usdIsEstimate: false,
            windowDays: 30
        )

        XCTAssertEqual(presentation?.state, .active)
        XCTAssertEqual(presentation?.creditsUsed, 435.765)
        XCTAssertEqual(presentation?.usdValue, 17.43)
        XCTAssertEqual(presentation?.usdIsEstimate, false)
        XCTAssertEqual(presentation?.balance, 158)
        XCTAssertEqual(formatCreditAmount(435.765), "435.765")
    }

    func testCodexPaidCreditsIgnoreBankedResetsAndAvailablePlanQuota() {
        XCTAssertNil(codexPaidCreditPresentation(
            limits: [weeklyLimit(usedPct: 76)],
            creditPools: [codexPurchasedCredits(), bankedReset()],
            creditsUsed: 4,
            usdValue: 0.16,
            usdIsEstimate: false,
            windowDays: 7
        ))
        XCTAssertNil(codexPaidCreditPresentation(
            limits: [weeklyLimit(usedPct: 100)],
            creditPools: [bankedReset()],
            creditsUsed: nil,
            usdValue: nil,
            usdIsEstimate: true,
            windowDays: nil
        ))
    }

    func testCodexPaidCreditsReportEmptyBalanceAndSpendControl() {
        let empty = codexPaidCreditPresentation(
            limits: [weeklyLimit(usedPct: 100)],
            creditPools: [codexPurchasedCredits(balance: 0, enabled: false)],
            creditsUsed: nil,
            usdValue: nil,
            usdIsEstimate: true,
            windowDays: nil
        )
        let controlled = codexPaidCreditPresentation(
            limits: [weeklyLimit(usedPct: 100)],
            creditPools: [codexPurchasedCredits(spendControlReached: true)],
            creditsUsed: nil,
            usdValue: nil,
            usdIsEstimate: true,
            windowDays: nil
        )

        XCTAssertEqual(empty?.state, .balanceEmpty)
        XCTAssertEqual(controlled?.state, .spendControlReached)
    }

    func testTodayWithoutQuotaLeadsWithObservedActivity() {
        XCTAssertEqual(
            activityFirstSummary(
                period: .day,
                activeHours: 0.2,
                messages: 43,
                tokens: 1_300_000
            ),
            "43 msgs · 1.3M tok"
        )
        XCTAssertEqual(
            activityFirstSummary(
                period: .day,
                activeHours: nil,
                messages: nil,
                tokens: nil
            ),
            "No activity today"
        )
    }

    func testWeekWithoutQuotaSaysDataIsUnavailable() {
        XCTAssertEqual(
            activityFirstSummary(
                period: .week,
                activeHours: 4,
                messages: 20,
                tokens: 500_000
            ),
            "Quota unavailable"
        )
    }

    func testMatchingSessionAndConversationCountsCollapse() {
        XCTAssertNil(distinctConversationCount(sessions: 50, conversations: 50))
        XCTAssertEqual(distinctConversationCount(sessions: 50, conversations: 42), 42)
        XCTAssertNil(distinctConversationCount(sessions: 50, conversations: 0))
    }

    func testPairedMetricLabelDescribesOnlyVisibleValues() {
        XCTAssertEqual(
            pairedMetricLabel(
                firstLabel: "Cache",
                firstValue: 1_000,
                secondLabel: "Reasoning",
                secondValue: 20
            ),
            "Cache / Reasoning"
        )
        XCTAssertEqual(
            pairedMetricLabel(
                firstLabel: "Cache",
                firstValue: 1_000,
                secondLabel: "Reasoning",
                secondValue: 0
            ),
            "Cache"
        )
        XCTAssertEqual(
            pairedMetricLabel(
                firstLabel: "Cache",
                firstValue: 0,
                secondLabel: "Reasoning",
                secondValue: 20
            ),
            "Reasoning"
        )
    }

    func testEmptyCursorPayloadHasNoDisplayData() throws {
        let empty = try JSONDecoder().decode(
            CursorStats.self,
            from: Data(#"{"plan":"unknown"}"#.utf8)
        )
        XCTAssertFalse(empty.hasDisplayData)

        let configured = try JSONDecoder().decode(
            CursorStats.self,
            from: Data(#"{"plan":"pro","total_requests":0,"max_requests":500}"#.utf8)
        )
        XCTAssertTrue(configured.hasDisplayData)
    }
}
