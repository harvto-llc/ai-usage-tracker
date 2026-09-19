import Foundation
import SwiftUI

@MainActor
class UsageViewModel: ObservableObject {
    @Published var stats: UsageStats?
    @Published var isOffline = true
    @Published var selectedPeriod: UsagePeriod = .day
    @Published var iconMode: MenuBarIconMode = .thermometer
    @Published var selectedTool: String = "claude"
    @Published var alertsEnabled = false
    @Published var visibleBars = VisibleBars(claudeSession: true, claudeWeekly: true, codexSession: true, codexWeekly: true)
    @Published var workStatus: WorkLedgerStatus?
    @Published var workItems: [WorkItem] = []
    @Published var workRepositories: [WorkRepository] = []
    @Published var workSessions: [WorkSession] = []
    @Published var workReviewSessions: [WorkSession] = []
    @Published var activeWork = ActiveWorkContext(
        state: "unassigned",
        intervalID: nil,
        startedAtUS: nil,
        workItemID: nil,
        kind: nil,
        name: nil,
        parentID: nil,
        parentName: nil,
        repositoryID: nil,
        repositoryName: nil,
        colorHex: nil
    )
    @Published var workTrackingEnabled = false
    @Published var isWorkMutationInFlight = false
    @Published var workError: String?
    @Published var workReport: WorkReport?
    @Published var workReportPeriod: WorkReportPeriod = .week
    @Published var isWorkReportLoading = false
    @Published var workReportError: String?
    @Published var sessionEvidence: [String: SessionEvidenceResponse] = [:]
    @Published var sessionEvidenceLoading: Set<String> = []
    @Published var sessionEvidenceErrors: [String: String] = [:]
    @Published var sessionEvidenceSearch: SessionEvidenceSearchResponse?
    @Published var isSessionEvidenceSearchLoading = false
    @Published var sessionEvidenceSearchError: String?
    @Published var sessionSearchIndexStatus: SessionSearchIndexStatus?
    @Published var isSessionSearchIndexStatusLoading = false
    @Published var sessionSearchIndexStatusError: String?
    @Published var usageExplanations: [String: UsageExplanation] = [:]
    @Published var usageExplanationLoading: Set<String> = []
    @Published var usageExplanationErrors: [String: String] = [:]

    private var timer: Timer?
    private var apiBaseURL: String
    private var apiToken: String
    private(set) var refreshInterval: TimeInterval
    private let alertCoordinator = UsageAlertCoordinator()
    private let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.connectionProxyDictionary = [:]  // bypass any proxy
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 30
        return URLSession(configuration: config)
    }()

    init(startImmediately: Bool = true) {
        // Load persisted settings
        let defaults = UserDefaults.standard
        let storedURL = defaults.string(forKey: SettingsKey.apiURL)
        apiBaseURL = (storedURL?.isEmpty == false) ? storedURL! : "http://localhost:8000"

        let storedToken = defaults.string(forKey: SettingsKey.apiToken)
        apiToken = (storedToken?.isEmpty == false) ? storedToken! : Self.tokenFromConfigFile()

        refreshInterval = defaults.double(forKey: SettingsKey.refreshInterval)
        if refreshInterval < 10 { refreshInterval = 30 }

        if let stored = defaults.string(forKey: SettingsKey.iconMode),
           let mode = MenuBarIconMode(rawValue: stored) {
            iconMode = mode
        }
        alertsEnabled = defaults.bool(forKey: SettingsKey.usageAlerts)
        alertCoordinator.setEnabled(alertsEnabled)

        visibleBars = VisibleBars(
            claudeSession: defaults.object(forKey: SettingsKey.showClaudeSession) as? Bool ?? true,
            claudeWeekly: defaults.object(forKey: SettingsKey.showClaudeWeekly) as? Bool ?? true,
            codexSession: defaults.object(forKey: SettingsKey.showCodexSession) as? Bool ?? true,
            codexWeekly: defaults.object(forKey: SettingsKey.showCodexWeekly) as? Bool ?? true
        )

        if startImmediately {
            fetch()
            startTimer()
        }
    }

    func applySettings(
        iconMode: MenuBarIconMode,
        refreshInterval: TimeInterval,
        visibleBars: VisibleBars,
        alertsEnabled: Bool
    ) {
        self.iconMode = iconMode
        self.refreshInterval = refreshInterval
        self.visibleBars = visibleBars
        self.alertsEnabled = alertsEnabled
        let defaults = UserDefaults.standard
        defaults.set(iconMode.rawValue, forKey: SettingsKey.iconMode)
        defaults.set(refreshInterval, forKey: SettingsKey.refreshInterval)
        defaults.set(visibleBars.claudeSession, forKey: SettingsKey.showClaudeSession)
        defaults.set(visibleBars.claudeWeekly, forKey: SettingsKey.showClaudeWeekly)
        defaults.set(visibleBars.codexSession, forKey: SettingsKey.showCodexSession)
        defaults.set(visibleBars.codexWeekly, forKey: SettingsKey.showCodexWeekly)
        defaults.set(alertsEnabled, forKey: SettingsKey.usageAlerts)
        alertCoordinator.setEnabled(alertsEnabled)
        startTimer()
        fetch()
    }

    private func startTimer() {
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: refreshInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.fetch()
            }
        }
    }

    private static func tokenFromConfigFile() -> String {
        if let env = ProcessInfo.processInfo.environment["USAGE_TRACKER_SECRET"] {
            return env
        }
        let configPath = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".usage-tracker/config")
        if let contents = try? String(contentsOf: configPath, encoding: .utf8) {
            for line in contents.split(separator: "\n") {
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                if trimmed.hasPrefix("USAGE_TRACKER_SECRET=") {
                    return String(trimmed.dropFirst("USAGE_TRACKER_SECRET=".count))
                }
            }
        }
        return ""
    }

    func fetch() {
        Task {
            let logFile = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".usage-menubar.log")
            func log(_ msg: String) {
                let line = "\(Date()): \(msg)\n"
                if let fh = try? FileHandle(forWritingTo: logFile) {
                    fh.seekToEndOfFile()
                    fh.write(line.data(using: .utf8)!)
                    fh.closeFile()
                } else {
                    try? line.write(to: logFile, atomically: true, encoding: .utf8)
                }
            }
            guard let url = URL(string: "\(apiBaseURL)/stats") else {
                log("Invalid API URL: \(apiBaseURL)")
                self.isOffline = true
                return
            }
            do {
                var request = URLRequest(url: url)
                request.timeoutInterval = 30
                if apiToken.isEmpty {
                    // First launch: the bundled backend generates the secret after we start.
                    apiToken = Self.tokenFromConfigFile()
                }
                if !apiToken.isEmpty {
                    request.setValue("Bearer \(apiToken)", forHTTPHeaderField: "Authorization")
                }
                let (data, _) = try await session.data(for: request)
                let decoded = try JSONDecoder().decode(UsageStats.self, from: data)
                let previous = self.stats
                self.stats = decoded
                self.isOffline = false
                log("OK: session=\(self.stats?.claudeQuota?.sessionUsedPct ?? -1)")
                alertCoordinator.evaluate(current: decoded, previous: previous)

                await fetchWorkState()
            } catch {
                log("API error: \(error)")
                self.isOffline = true
            }
        }
    }

    func refreshProviderAccess() {
        Task {
            await Sentinel.shared.scanAndReport(force: true)
            fetch()
        }
    }

    func fetchWorkState() async {
        do {
            let statusData = try await workRequest(path: "/work-ledger/status")
            let decoder = JSONDecoder()
            let status = try decoder.decode(WorkLedgerStatus.self, from: statusData)
            let itemsData = try await workRequest(path: "/work-ledger/items")
            let repositoriesData = try await workRequest(path: "/work-ledger/repositories")
            let items = try decoder.decode(WorkItemsResponse.self, from: itemsData)
            let repositories = try decoder.decode(
                WorkRepositoriesResponse.self,
                from: repositoriesData
            )
            let sessions: WorkSessionsResponse
            if status.settings.collectionEnabled {
                let sessionsData = try await workRequest(
                    path: "/work-ledger/sessions?limit=500&review_hints=true"
                )
                sessions = try decoder.decode(WorkSessionsResponse.self, from: sessionsData)
            } else {
                sessions = WorkSessionsResponse(sessions: [])
            }
            workStatus = status
            workItems = items.items
            workRepositories = repositories.repositories
            workReviewSessions = sessions.sessions
            workSessions = Array(
                sessions.sessions
                    .filter { $0.runtimeState != "ended" }
                    .prefix(12)
            )
            activeWork = items.active
            workTrackingEnabled = status.settings.collectionEnabled
            workError = status.refresh.lastError
        } catch {
            workError = error.localizedDescription
        }
    }

    func setWorkTrackingEnabled(_ enabled: Bool) {
        let previous = workTrackingEnabled
        workTrackingEnabled = enabled
        Task {
            let succeeded = await performWorkMutation(
                path: "/work-ledger/settings",
                method: "PUT",
                body: ["enabled": enabled]
            )
            if !succeeded {
                workTrackingEnabled = previous
            }
        }
    }

    func setRepositoryEnabled(_ repositoryID: String, enabled: Bool) {
        Task {
            _ = await performWorkMutation(
                path: "/work-ledger/repositories/\(repositoryID)",
                method: "PATCH",
                body: ["enabled": enabled]
            )
        }
    }

    func selectWorkItem(_ workItemID: String?) {
        Task {
            _ = await performWorkMutation(
                path: "/work-ledger/active",
                method: "PUT",
                body: ["work_item_id": workItemID ?? NSNull()]
            )
        }
    }

    func setSessionNickname(_ sessionID: String, nickname: String?) {
        Task {
            _ = await performWorkMutation(
                path: "/work-ledger/sessions/\(sessionID)",
                method: "PATCH",
                body: ["nickname": nickname ?? NSNull()]
            )
        }
    }

    func setSessionAssignment(
        _ sessionID: String,
        mode: String,
        workItemID: String? = nil
    ) {
        Task {
            var body: [String: Any] = ["assignment_mode": mode]
            if let workItemID { body["work_item_id"] = workItemID }
            _ = await performWorkMutation(
                path: "/work-ledger/sessions/\(sessionID)",
                method: "PATCH",
                body: body
            )
        }
    }

    var repositoryProjectChoices: [WorkRepository] {
        let linkedRepositoryIDs = Set(
            workItems.compactMap { item in
                item.kind == "project" ? item.repositoryID : nil
            }
        )
        return workRepositories
            .filter { $0.enabled && !linkedRepositoryIDs.contains($0.id) }
            .sorted {
                $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedAscending
            }
    }

    func assignSession(_ sessionID: String, toRepository repository: WorkRepository) {
        Task {
            isWorkMutationInFlight = true
            defer { isWorkMutationInFlight = false }
            do {
                let projectID: String
                if let project = workItems.first(where: {
                    $0.kind == "project" && $0.repositoryID == repository.id
                }) {
                    projectID = project.id
                } else {
                    let data = try await workRequest(
                        path: "/work-ledger/items",
                        method: "POST",
                        body: [
                            "kind": "project",
                            "name": repository.displayName,
                            "repository_id": repository.id,
                        ]
                    )
                    projectID = try JSONDecoder().decode(WorkItemResponse.self, from: data).item.id
                }
                _ = try await workRequest(
                    path: "/work-ledger/sessions/\(sessionID)",
                    method: "PATCH",
                    body: [
                        "assignment_mode": "work_item",
                        "work_item_id": projectID,
                    ]
                )
                workError = nil
                await fetchWorkState()
            } catch {
                workError = error.localizedDescription
            }
        }
    }

    func createProjectFromFolder(
        _ path: String,
        assigning sessionID: String? = nil
    ) async -> Bool {
        var body: [String: Any] = ["path": path]
        if let sessionID { body["session_id"] = sessionID }
        return await performWorkMutation(
            path: "/work-ledger/projects/from-folder",
            method: "POST",
            body: body
        )
    }

    func createWorkItem(
        kind: String,
        name: String,
        parentID: String?,
        repositoryID: String?,
        colorHex: String?
    ) async -> Bool {
        var body: [String: Any] = ["kind": kind, "name": name]
        if let parentID { body["parent_id"] = parentID }
        if let repositoryID { body["repository_id"] = repositoryID }
        if let colorHex { body["color_hex"] = colorHex }
        return await performWorkMutation(
            path: "/work-ledger/items",
            method: "POST",
            body: body
        )
    }

    func archiveWorkItem(_ itemID: String) {
        Task {
            _ = await performWorkMutation(
                path: "/work-ledger/items/\(itemID)",
                method: "DELETE"
            )
        }
    }

    func refreshWorkLedger() {
        Task {
            _ = await performWorkMutation(
                path: "/work-ledger/refresh",
                method: "POST",
                refreshAfterMutation: true
            )
        }
    }

    func fetchWorkReport(period: WorkReportPeriod? = nil) async {
        let selected = period ?? workReportPeriod
        workReportPeriod = selected
        isWorkReportLoading = true
        defer { isWorkReportLoading = false }
        do {
            let data = try await workRequest(
                path: "/work-ledger/report?period=\(selected.rawValue)"
            )
            workReport = try JSONDecoder().decode(WorkReport.self, from: data)
            workReportError = nil
        } catch {
            workReportError = error.localizedDescription
        }
    }

    func fetchSessionEvidence(sessionID: String, force: Bool = false) async {
        if !force, sessionEvidence[sessionID] != nil { return }
        guard !sessionEvidenceLoading.contains(sessionID) else { return }
        sessionEvidenceLoading.insert(sessionID)
        defer { sessionEvidenceLoading.remove(sessionID) }
        do {
            let data = try await workRequest(
                path: "/work-ledger/sessions/\(sessionID)/evidence?limit=800"
            )
            sessionEvidence[sessionID] = try JSONDecoder().decode(
                SessionEvidenceResponse.self,
                from: data
            )
            sessionEvidenceErrors[sessionID] = nil
        } catch {
            sessionEvidenceErrors[sessionID] = error.localizedDescription
        }
    }

    func searchSessionEvidence(query: String, provider: String?) async {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count >= 2 else {
            clearSessionEvidenceSearch()
            return
        }
        let disallowed = CharacterSet(charactersIn: "+&=")
        let allowed = CharacterSet.urlQueryAllowed.subtracting(disallowed)
        guard let encoded = trimmed.addingPercentEncoding(withAllowedCharacters: allowed) else {
            sessionEvidenceSearchError = "Search query could not be encoded"
            return
        }
        isSessionEvidenceSearchLoading = true
        sessionEvidenceSearchError = nil
        defer { isSessionEvidenceSearchLoading = false }
        var path = "/work-ledger/session-search?q=\(encoded)&limit=50"
        if let provider { path += "&provider=\(provider)" }
        do {
            let data = try await workRequest(path: path)
            guard !Task.isCancelled else { return }
            let response = try JSONDecoder().decode(
                SessionEvidenceSearchResponse.self,
                from: data
            )
            sessionEvidenceSearch = response
            if let index = response.index {
                sessionSearchIndexStatus = index
                sessionSearchIndexStatusError = nil
            }
        } catch is CancellationError {
            return
        } catch {
            guard !Task.isCancelled else { return }
            sessionEvidenceSearchError = error.localizedDescription
        }
    }

    func clearSessionEvidenceSearch() {
        sessionEvidenceSearch = nil
        sessionEvidenceSearchError = nil
        isSessionEvidenceSearchLoading = false
    }

    func fetchSessionSearchIndexStatus(provider: String?) async {
        guard !isSessionSearchIndexStatusLoading else { return }
        isSessionSearchIndexStatusLoading = true
        defer { isSessionSearchIndexStatusLoading = false }
        var path = "/work-ledger/session-search/status"
        if let provider { path += "?provider=\(provider)" }
        do {
            let data = try await workRequest(path: path)
            guard !Task.isCancelled else { return }
            sessionSearchIndexStatus = try JSONDecoder().decode(
                SessionSearchIndexStatus.self,
                from: data
            )
            sessionSearchIndexStatusError = nil
        } catch is CancellationError {
            return
        } catch {
            guard !Task.isCancelled else { return }
            sessionSearchIndexStatusError = error.localizedDescription
        }
    }

    func reviewSessions(
        for period: WorkReportPeriod,
        needsReviewOnly: Bool = false,
        now: Date = Date()
    ) -> [WorkSession] {
        let calendar = Calendar.current
        let startOfToday = calendar.startOfDay(for: now)
        let start: Date
        switch period {
        case .day:
            start = startOfToday
        case .week:
            start = calendar.date(byAdding: .day, value: -6, to: startOfToday) ?? startOfToday
        case .month:
            start = calendar.date(byAdding: .day, value: -29, to: startOfToday) ?? startOfToday
        }
        let cutoffUS = Int(start.timeIntervalSince1970 * 1_000_000)
        return workReviewSessions.filter { session in
            guard (session.reviewTimestampUS ?? 0) >= cutoffUS else { return false }
            return !needsReviewOnly || session.attributionState == .needsReview
        }
    }

    func needsReviewCount(for period: WorkReportPeriod = .week) -> Int {
        reviewSessions(for: period, needsReviewOnly: true).count
    }

    func usageExplanation(provider: String, period: UsagePeriod) -> UsageExplanation? {
        usageExplanations["\(provider):\(period.rawValue)"]
    }

    func fetchUsageExplanation(
        provider: String,
        period: UsagePeriod,
        startAt: String? = nil
    ) async {
        guard provider == "claude" || provider == "codex" else { return }
        let key = "\(provider):\(period.rawValue)"
        guard !usageExplanationLoading.contains(key) else { return }
        usageExplanationLoading.insert(key)
        defer { usageExplanationLoading.remove(key) }
        var path = "/usage/explanation?provider=\(provider)&period=\(period.rawValue)"
        if let startAt, !startAt.isEmpty {
            let disallowed = CharacterSet(charactersIn: "+&=")
            let allowed = CharacterSet.urlQueryAllowed.subtracting(disallowed)
            if let encoded = startAt.addingPercentEncoding(withAllowedCharacters: allowed) {
                path += "&start_at=\(encoded)"
            }
        }
        do {
            let data = try await workRequest(path: path)
            usageExplanations[key] = try JSONDecoder().decode(UsageExplanation.self, from: data)
            usageExplanationErrors[key] = nil
        } catch {
            usageExplanationErrors[key] = error.localizedDescription
        }
    }

    private func performWorkMutation(
        path: String,
        method: String,
        body: [String: Any]? = nil,
        refreshAfterMutation: Bool = true
    ) async -> Bool {
        isWorkMutationInFlight = true
        defer { isWorkMutationInFlight = false }
        do {
            _ = try await workRequest(path: path, method: method, body: body)
            workError = nil
            if refreshAfterMutation {
                await fetchWorkState()
            }
            return true
        } catch {
            workError = error.localizedDescription
            return false
        }
    }

    private func workRequest(
        path: String,
        method: String = "GET",
        body: [String: Any]? = nil
    ) async throws -> Data {
        guard let url = URL(string: "\(apiBaseURL)\(path)") else {
            throw WorkAPIError.invalidURL
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 15
        if apiToken.isEmpty {
            apiToken = Self.tokenFromConfigFile()
        }
        if !apiToken.isEmpty {
            request.setValue("Bearer \(apiToken)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse,
              (200..<300).contains(http.statusCode) else {
            let detail = (try? JSONSerialization.jsonObject(with: data))
                .flatMap { $0 as? [String: Any] }?["detail"] as? String
            throw WorkAPIError.server(detail ?? "Work tracking request failed")
        }
        return data
    }

    // MARK: - Reset Time Helpers

    func formatResetDisplay(for resetStr: String?) -> String? {
        ResetTimeFormatter.formatResetDisplay(resetStr)
    }
}

private enum WorkAPIError: LocalizedError {
    case invalidURL
    case server(String)

    var errorDescription: String? {
        switch self {
        case .invalidURL:
            return "Invalid work tracking API URL"
        case .server(let message):
            return message
        }
    }
}
