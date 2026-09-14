import Foundation
import UserNotifications

struct AlertQuotaSnapshot: Equatable {
    let provider: String
    let id: String
    let label: String
    let windowKind: String
    let scopeKind: String
    let usedPct: Double
    let remainingPct: Double
    let reset: String?
}

struct UsageAlertSnapshot: Equatable {
    let quotas: [AlertQuotaSnapshot]
    let burnRates: [String: Double]
    let staleProviders: Set<String>
}

struct UsageAlertEvent: Equatable {
    enum Kind: String {
        case threshold70
        case threshold90
        case projectedExhaustion
        case quotaRestored
        case credentialsStale
    }

    let id: String
    let kind: Kind
    let title: String
    let body: String

    var cooldown: TimeInterval {
        switch kind {
        case .threshold70, .threshold90: return 4 * 3600
        case .projectedExhaustion: return 2 * 3600
        case .quotaRestored: return 2 * 3600
        case .credentialsStale: return 12 * 3600
        }
    }
}

enum UsageAlertCooldown {
    static func due(
        _ events: [UsageAlertEvent],
        lastSent: [String: TimeInterval],
        now: TimeInterval
    ) -> [UsageAlertEvent] {
        events.filter { now - (lastSent[$0.id] ?? 0) >= $0.cooldown }
    }
}

enum UsageNotificationDeliveryMode: Equatable {
    case userNotifications
    case appleScript

    static func current(bundleURL: URL = Bundle.main.bundleURL) -> Self {
        bundleURL.pathExtension.lowercased() == "app" ? .userNotifications : .appleScript
    }
}

enum UsageAlertEvaluator {
    static func evaluate(
        current: UsageAlertSnapshot,
        previous: UsageAlertSnapshot?
    ) -> [UsageAlertEvent] {
        let previousByID = Dictionary(uniqueKeysWithValues: (previous?.quotas ?? []).map { (key($0), $0) })
        var events: [UsageAlertEvent] = []

        for quota in current.quotas {
            let quotaKey = key(quota)
            let prior = previousByID[quotaKey]
            let provider = providerLabel(quota.provider)

            let crossed90 = quota.usedPct >= 90 && (prior?.usedPct ?? 0) < 90
            let crossed70 = quota.usedPct >= 70 && (prior?.usedPct ?? 0) < 70
            var projectedHours: Double?
            var crossedProjection = false
            if quota.windowKind == "session", quota.scopeKind == "aggregate",
               quota.remainingPct > 0,
               let burn = current.burnRates[quota.provider], burn > 0 {
                let hours = quota.remainingPct / burn
                let priorHours: Double? = prior.flatMap { old in
                    guard let oldBurn = previous?.burnRates[quota.provider], oldBurn > 0 else { return nil }
                    return old.remainingPct / oldBurn
                }
                projectedHours = hours
                crossedProjection = hours <= 2 && (priorHours == nil || priorHours! > 2)
            }

            if crossed90 {
                events.append(UsageAlertEvent(
                    id: "\(quotaKey).threshold.90",
                    kind: .threshold90,
                    title: "\(provider) quota at \(Int(quota.usedPct.rounded()))%",
                    body: resetBody(quota, prefix: "\(quota.label) is nearly exhausted.")
                ))
            } else if crossedProjection, let hours = projectedHours {
                events.append(UsageAlertEvent(
                    id: "\(quotaKey).projected",
                    kind: .projectedExhaustion,
                    title: "\(provider) session may run out soon",
                    body: resetBody(quota, prefix: String(format: "About %.1f hours left at the current pace.", hours))
                ))
            } else if crossed70 {
                events.append(UsageAlertEvent(
                    id: "\(quotaKey).threshold.70",
                    kind: .threshold70,
                    title: "\(provider) quota at \(Int(quota.usedPct.rounded()))%",
                    body: resetBody(quota, prefix: "\(quota.label) usage is elevated.")
                ))
            }

            if let prior, prior.usedPct >= 70, quota.usedPct <= 10 {
                events.append(UsageAlertEvent(
                    id: "\(quotaKey).restored",
                    kind: .quotaRestored,
                    title: "\(provider) quota restored",
                    body: "\(quota.label) is back to \(Int(quota.remainingPct.rounded()))% remaining."
                ))
            }

        }

        for provider in current.staleProviders where !(previous?.staleProviders.contains(provider) ?? false) {
            let label = providerLabel(provider)
            events.append(UsageAlertEvent(
                id: "\(provider).credentials.stale",
                kind: .credentialsStale,
                title: "\(label) usage needs sign-in",
                body: "Usage data is stale. Refresh the \(label) sign-in to resume updates."
            ))
        }
        return events
    }

    private static func key(_ quota: AlertQuotaSnapshot) -> String {
        "\(quota.provider).\(quota.id)"
    }

    private static func providerLabel(_ provider: String) -> String {
        provider == "codex" ? "Codex" : "Claude"
    }

    private static func resetBody(_ quota: AlertQuotaSnapshot, prefix: String) -> String {
        guard let reset = quota.reset, !reset.isEmpty else { return prefix }
        return "\(prefix) Resets \(reset)."
    }
}

@MainActor
final class UsageAlertCoordinator {
    private let defaults = UserDefaults.standard
    private let sentKey = "usageAlertLastSent"
    private let deliveryMode: UsageNotificationDeliveryMode
    private var enabled = false

    init(deliveryMode: UsageNotificationDeliveryMode = .current()) {
        self.deliveryMode = deliveryMode
    }

    func setEnabled(_ enabled: Bool) {
        self.enabled = enabled
        guard enabled, deliveryMode == .userNotifications else { return }
        Task {
            let center = UNUserNotificationCenter.current()
            _ = try? await center.requestAuthorization(options: [.alert, .sound])
        }
    }

    func evaluate(current: UsageStats, previous: UsageStats?) {
        guard enabled else { return }
        let events = UsageAlertEvaluator.evaluate(
            current: Self.snapshot(current),
            previous: previous.map(Self.snapshot)
        )
        guard !events.isEmpty else { return }
        Task { await deliver(events) }
    }

    private func deliver(_ events: [UsageAlertEvent]) async {
        var sent = lastSent()
        let now = Date().timeIntervalSince1970
        for event in UsageAlertCooldown.due(events, lastSent: sent, now: now) {
            let delivered: Bool
            switch deliveryMode {
            case .userNotifications:
                delivered = await deliverUserNotification(event)
            case .appleScript:
                delivered = await deliverAppleScriptNotification(event)
            }
            if delivered {
                sent[event.id] = now
            }
        }
        saveLastSent(sent)
    }

    private func deliverUserNotification(_ event: UsageAlertEvent) async -> Bool {
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
            return false
        }
        let content = UNMutableNotificationContent()
        content.title = event.title
        content.body = event.body
        content.sound = .default
        let request = UNNotificationRequest(identifier: event.id, content: content, trigger: nil)
        return (try? await center.add(request)) != nil
    }

    private func deliverAppleScriptNotification(_ event: UsageAlertEvent) async -> Bool {
        let title = event.title
        let body = event.body
        return await Task.detached {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            process.arguments = [
                "-e",
                "on run argv\n  display notification (item 2 of argv) with title (item 1 of argv)\nend run",
                title,
                body,
            ]
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            do {
                try process.run()
                process.waitUntilExit()
                return process.terminationStatus == 0
            } catch {
                return false
            }
        }.value
    }

    private func lastSent() -> [String: TimeInterval] {
        guard let data = defaults.data(forKey: sentKey),
              let value = try? JSONDecoder().decode([String: TimeInterval].self, from: data) else { return [:] }
        return value
    }

    private func saveLastSent(_ value: [String: TimeInterval]) {
        guard let data = try? JSONEncoder().encode(value) else { return }
        defaults.set(data, forKey: sentKey)
    }

    static func snapshot(_ stats: UsageStats) -> UsageAlertSnapshot {
        var quotas: [AlertQuotaSnapshot] = []
        quotas += quotaSnapshots(
            provider: "claude",
            limits: stats.claudeQuota?.limits,
            sessionUsed: stats.claudeQuota?.sessionUsedPct,
            weeklyUsed: stats.claudeQuota?.weeklyUsedPct,
            sessionReset: stats.claudeQuota?.sessionReset,
            weeklyReset: stats.claudeQuota?.weeklyReset
        )
        quotas += quotaSnapshots(
            provider: "codex",
            limits: stats.codexQuota?.limits,
            sessionUsed: stats.codexQuota?.sessionUsedPct,
            weeklyUsed: stats.codexQuota?.weeklyUsedPct,
            sessionReset: stats.codexQuota?.sessionReset,
            weeklyReset: stats.codexQuota?.weeklyReset
        )
        let stale: Set<String> = Set((stats.providerHealth ?? [:]).compactMap { provider, health in
            guard let quota = health.quota else { return nil }
            let signInUnavailable = quota.status == "unavailable" && quota.recovery?.localizedCaseInsensitiveContains("sign-in") == true
            return quota.status == "stale" || signInUnavailable ? provider : nil
        })
        return UsageAlertSnapshot(
            quotas: quotas,
            burnRates: ["claude": stats.burn ?? 0, "codex": stats.codexBurn ?? 0],
            staleProviders: stale
        )
    }

    private static func quotaSnapshots(
        provider: String,
        limits: [QuotaBucket]?,
        sessionUsed: Double?,
        weeklyUsed: Double?,
        sessionReset: String?,
        weeklyReset: String?
    ) -> [AlertQuotaSnapshot] {
        var snapshots: [AlertQuotaSnapshot] = (limits ?? []).compactMap { limit in
                guard let used = limit.usedPct ?? limit.remainingPct.map({ 100 - $0 }) else { return nil }
                let normalizedUsed = min(max(used, 0), 100)
                return AlertQuotaSnapshot(
                    provider: provider,
                    id: limit.id,
                    label: limit.label ?? (limit.windowKind == "weekly" ? "Weekly quota" : "Session quota"),
                    windowKind: limit.windowKind ?? "other",
                    scopeKind: limit.scopeKind ?? "aggregate",
                    usedPct: normalizedUsed,
                    remainingPct: min(max(limit.remainingPct ?? 100 - normalizedUsed, 0), 100),
                    reset: limit.reset
                )
        }
        if !snapshots.contains(where: { $0.windowKind == "session" }),
           let fallback = fallbackQuota(
               provider: provider, id: "session", label: "Session quota",
               kind: "session", used: sessionUsed, reset: sessionReset
           ) {
            snapshots.append(fallback)
        }
        if !snapshots.contains(where: { $0.windowKind == "weekly" }),
           let fallback = fallbackQuota(
               provider: provider, id: "weekly", label: "Weekly quota",
               kind: "weekly", used: weeklyUsed, reset: weeklyReset
           ) {
            snapshots.append(fallback)
        }
        return snapshots
    }

    private static func fallbackQuota(
        provider: String,
        id: String,
        label: String,
        kind: String,
        used: Double?,
        reset: String?
    ) -> AlertQuotaSnapshot? {
        guard let used else { return nil }
        return AlertQuotaSnapshot(
            provider: provider,
            id: id,
            label: label,
            windowKind: kind,
            scopeKind: "aggregate",
            usedPct: used,
            remainingPct: max(0, 100 - used),
            reset: reset
        )
    }
}
