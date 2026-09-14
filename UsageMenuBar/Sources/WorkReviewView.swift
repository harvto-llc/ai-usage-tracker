import AppKit
import SwiftUI

private enum WorkWorkspaceTab: String, CaseIterable, Identifiable {
    case review
    case timeline
    case report

    var id: String { rawValue }
    var label: String { rawValue.capitalized }
}

private enum WorkSessionFilter: String, CaseIterable, Identifiable {
    case needsReview
    case all

    var id: String { rawValue }
    var label: String {
        switch self {
        case .needsReview: return "Needs Review"
        case .all: return "All Sessions"
        }
    }
}

struct WorkWorkspaceView: View {
    @ObservedObject var vm: UsageViewModel
    let loadsOnAppear: Bool
    @State private var selectedTab: WorkWorkspaceTab = .review
    @State private var sessionFilter: WorkSessionFilter = .needsReview
    @State private var selectedSessionID: String?
    @State private var highlightedEvidenceID: String?

    init(vm: UsageViewModel, loadsOnAppear: Bool = true) {
        self.vm = vm
        self.loadsOnAppear = loadsOnAppear
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            switch selectedTab {
            case .review:
                WorkSessionReviewView(
                    vm: vm,
                    filter: $sessionFilter,
                    openEvidence: { sessionID in
                        selectedSessionID = sessionID
                        highlightedEvidenceID = nil
                        selectedTab = .timeline
                    }
                )
            case .timeline:
                SessionTimelineWorkspaceView(
                    vm: vm,
                    selectedSessionID: $selectedSessionID,
                    highlightedEvidenceID: $highlightedEvidenceID
                )
            case .report:
                WorkReportView(vm: vm, loadsOnAppear: false, showsHeader: false)
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .frame(minWidth: 700, idealWidth: 720, minHeight: 520, idealHeight: 680)
        .task(id: "\(selectedTab.rawValue):\(vm.workReportPeriod.rawValue)") {
            guard loadsOnAppear else { return }
            await vm.fetchWorkState()
            if selectedTab == .report {
                await vm.fetchWorkReport(period: vm.workReportPeriod)
            }
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Label("Work Activity", systemImage: "briefcase.fill")
                .font(.headline)
            Picker("View", selection: $selectedTab) {
                ForEach(WorkWorkspaceTab.allCases) { tab in
                    Text(tab.label).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 250)
            Spacer(minLength: 8)
            Picker("Period", selection: $vm.workReportPeriod) {
                ForEach(WorkReportPeriod.allCases) { period in
                    Text(period.label).tag(period)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 210)
            workOverrideMenu
            Button {
                vm.refreshWorkLedger()
                Task {
                    try? await Task.sleep(for: .milliseconds(500))
                    await vm.fetchWorkState()
                    if selectedTab == .report {
                        await vm.fetchWorkReport(period: vm.workReportPeriod)
                    }
                }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.plain)
            .disabled(vm.isWorkMutationInFlight)
            .help("Refresh work activity")
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 14)
    }

    private var workOverrideMenu: some View {
        Menu {
            Button {
                vm.selectWorkItem(nil)
            } label: {
                Label(
                    "Automatic by repository",
                    systemImage: vm.activeWork.state == "active" ? "sparkles" : "checkmark"
                )
            }
            if !vm.workItems.isEmpty {
                Section("Pin new activity to") {
                    ForEach(vm.workItems) { item in
                        Button {
                            vm.selectWorkItem(item.id)
                        } label: {
                            Label(
                                item.displayName,
                                systemImage: vm.activeWork.workItemID == item.id
                                    ? "checkmark"
                                    : workItemIcon(item.kind)
                            )
                        }
                    }
                }
            }
            Divider()
            Button {
                SettingsWindowController.shared.open(vm: vm)
            } label: {
                Label("Manage projects", systemImage: "gearshape")
            }
        } label: {
            Image(systemName: vm.activeWork.state == "active" ? "pin.fill" : "pin")
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .help(vm.activeWork.state == "active" ? "Change pinned project" : "Pin new activity to a project")
    }
}

private struct WorkSessionReviewView: View {
    @ObservedObject var vm: UsageViewModel
    @Binding var filter: WorkSessionFilter
    let openEvidence: (String) -> Void
    @State private var editingSessionID: String?
    @State private var nicknameText = ""
    @State private var expandedGroupIDs: Set<String> = []
    @State private var projectFolderError: String?

    var body: some View {
        VStack(spacing: 0) {
            reviewSummary
            Divider()
            if displayedSessions.isEmpty {
                emptyState
            } else {
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(Array(displayedGroups.enumerated()), id: \.element.id) { index, group in
                            if group.isCollection {
                                sessionGroupRow(group)
                                if expandedGroupIDs.contains(group.id) {
                                    Divider().padding(.leading, 48)
                                    ForEach(Array(group.sessions.enumerated()), id: \.element.id) {
                                        childIndex, session in
                                        sessionRow(session, grouped: true)
                                        if childIndex < group.sessions.count - 1 {
                                            Divider().padding(.leading, 72)
                                        }
                                    }
                                }
                            } else if let session = group.sessions.first {
                                sessionRow(session)
                            }
                            if index < displayedGroups.count - 1 {
                                Divider().padding(.leading, 48)
                            }
                        }
                    }
                    .padding(.horizontal, 18)
                }
            }
        }
        .alert(
            "Could Not Add Project",
            isPresented: Binding(
                get: { projectFolderError != nil },
                set: { if !$0 { projectFolderError = nil } }
            )
        ) {
            Button("OK") { projectFolderError = nil }
        } message: {
            Text(projectFolderError ?? "The selected folder could not be added.")
        }
    }

    private var reviewSummary: some View {
        HStack(spacing: 14) {
            Picker("Sessions", selection: $filter) {
                ForEach(WorkSessionFilter.allCases) { value in
                    Text(value.label).tag(value)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 220)
            Spacer()
            summaryLabel(
                "\(needsReviewSessions.count)",
                text: "review",
                icon: "exclamationmark.circle.fill",
                color: needsReviewSessions.isEmpty ? .secondary : .orange
            )
            summaryLabel("\(automaticCount)", text: "automatic", icon: "sparkles", color: .secondary)
            summaryLabel("\(confirmedCount)", text: "confirmed", icon: "checkmark.circle", color: .secondary)
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 10)
    }

    private func summaryLabel(_ value: String, text: String, icon: String, color: Color) -> some View {
        Label("\(value) \(text)", systemImage: icon)
            .font(.caption)
            .foregroundStyle(color)
            .lineLimit(1)
    }

    private var emptyState: some View {
        ContentUnavailableView(
            filter == .needsReview ? "No Sessions Need Review" : "No Sessions Found",
            systemImage: filter == .needsReview ? "checkmark.circle" : "clock",
            description: Text(
                filter == .needsReview
                    ? "Recent sessions have project attribution."
                    : "There is no AI session activity in this period."
            )
        )
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func sessionGroupRow(_ group: WorkSessionReviewGroup) -> some View {
        Button {
            if expandedGroupIDs.contains(group.id) {
                expandedGroupIDs.remove(group.id)
            } else {
                expandedGroupIDs.insert(group.id)
            }
        } label: {
            HStack(spacing: 12) {
                ZStack(alignment: .bottomTrailing) {
                    Image(systemName: "rectangle.stack.fill")
                        .font(.system(size: 16))
                        .frame(width: 24, height: 24)
                    Circle()
                        .fill(runtimeColor(group.runtimeState))
                        .frame(width: 7, height: 7)
                        .overlay(Circle().stroke(Color(nsColor: .windowBackgroundColor), lineWidth: 1))
                }
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 7) {
                        Text(group.displayName)
                            .font(.subheadline.weight(.medium))
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Text("\(group.sessions.count) sessions")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(.secondary)
                    }
                    Text(locationDetail(group.representative))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(groupActivityDetail(group))
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                    if group.representative.attributionState == .needsReview,
                       let firstPrompt = group.representative.firstPrompt,
                       !firstPrompt.isEmpty {
                        Label(firstPrompt, systemImage: "text.bubble")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                            .help(firstPrompt)
                    }
                }
                Spacer(minLength: 10)
                VStack(alignment: .trailing, spacing: 3) {
                    Text(group.attributionLabel)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text(group.attributionSummary)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
                .frame(width: 150, alignment: .trailing)
                Image(systemName: expandedGroupIDs.contains(group.id) ? "chevron.up" : "chevron.down")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .frame(width: 18)
            }
            .contentShape(Rectangle())
            .padding(.vertical, 11)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(
            "\(group.displayName), \(group.sessions.count) sessions, \(group.attributionSummary)"
        )
    }

    @ViewBuilder
    private func sessionRow(_ session: WorkSession, grouped: Bool = false) -> some View {
        if editingSessionID == session.id {
            HStack(spacing: 8) {
                if grouped {
                    Color.clear.frame(width: 24)
                }
                TextField("Session name", text: $nicknameText)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { commitNickname(session) }
                Button { commitNickname(session) } label: {
                    Image(systemName: "checkmark")
                }
                .help("Save session name")
                Button { editingSessionID = nil } label: {
                    Image(systemName: "xmark")
                }
                .help("Cancel rename")
            }
            .buttonStyle(.plain)
            .padding(.vertical, 14)
        } else {
            HStack(spacing: 12) {
                if grouped {
                    Color.clear.frame(width: 24)
                }
                ZStack(alignment: .bottomTrailing) {
                    Image(systemName: providerIcon(session.provider))
                        .font(.system(size: 16))
                        .frame(width: 24, height: 24)
                    Circle()
                        .fill(runtimeColor(session.runtimeState))
                        .frame(width: 7, height: 7)
                        .overlay(Circle().stroke(Color(nsColor: .windowBackgroundColor), lineWidth: 1))
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text(grouped ? session.reviewChildIdentifier : session.displayName)
                        .font(.subheadline.weight(.medium))
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .help(grouped ? session.providerSessionID : session.displayName)
                    Text(locationDetail(session))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(activityDetail(session, includeProvider: !grouped))
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                    if session.attributionState == .needsReview,
                       let firstPrompt = session.firstPrompt,
                       !firstPrompt.isEmpty {
                        Label(firstPrompt, systemImage: "text.bubble")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                            .help(firstPrompt)
                    }
                }
                Spacer(minLength: 10)
                VStack(alignment: .trailing, spacing: 3) {
                    Label(attributionLabel(session), systemImage: attributionIcon(session))
                        .font(.caption.weight(session.attributionState == .needsReview ? .semibold : .regular))
                        .foregroundStyle(attributionColor(session))
                    Text(session.attributionReason)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                .frame(width: 150, alignment: .trailing)
                assignmentMenu(session)
                Button {
                    openEvidence(session.id)
                } label: {
                    Image(systemName: "doc.text.magnifyingglass")
                }
                .buttonStyle(.plain)
                .help("View session evidence")
                Button {
                    nicknameText = session.nickname ?? session.displayName
                    editingSessionID = session.id
                } label: {
                    Image(systemName: "pencil")
                }
                .buttonStyle(.plain)
                .help("Rename session locally")
            }
            .padding(.vertical, 11)
        }
    }

    private func assignmentMenu(_ session: WorkSession) -> some View {
        Menu {
            Section("This session") {
                Button {
                    vm.setSessionAssignment(session.id, mode: "automatic")
                } label: {
                    assignmentLabel(
                        "Automatic by repository",
                        icon: "sparkles",
                        selected: session.assignmentMode == "automatic"
                    )
                }
                Button {
                    vm.setSessionAssignment(session.id, mode: "unassigned")
                } label: {
                    assignmentLabel(
                        "Exclude from reports",
                        icon: "minus.circle",
                        selected: session.assignmentMode == "unassigned"
                    )
                }
            }
            if !vm.workItems.isEmpty {
                Section("Work items") {
                    ForEach(vm.workItems) { item in
                        Button {
                            vm.setSessionAssignment(
                                session.id,
                                mode: "work_item",
                                workItemID: item.id
                            )
                        } label: {
                            assignmentLabel(
                                item.displayName,
                                icon: workItemIcon(item.kind),
                                selected: session.assignedWorkItemID == item.id
                            )
                        }
                    }
                }
            }
            Section("Projects") {
                ForEach(vm.repositoryProjectChoices) { repository in
                    Button {
                        vm.assignSession(session.id, toRepository: repository)
                    } label: {
                        Label(repository.displayName, systemImage: "folder")
                    }
                }
                Button {
                    chooseProjectFolder(assigning: session.id)
                } label: {
                    Label("Add Project from Folder...", systemImage: "folder.badge.plus")
                }
            }
        } label: {
            Image(systemName: session.assignmentMode == "automatic" ? "tag" : "tag.fill")
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .disabled(vm.isWorkMutationInFlight)
        .help("Change session attribution")
    }

    private func assignmentLabel(_ text: String, icon: String, selected: Bool) -> some View {
        Label(text, systemImage: selected ? "checkmark" : icon)
    }

    private func chooseProjectFolder(assigning sessionID: String) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Add Project"
        panel.message = "Choose a folder inside a Git repository."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task {
            if !(await vm.createProjectFromFolder(url.path, assigning: sessionID)) {
                projectFolderError = vm.workError ?? "The selected folder could not be added."
            }
        }
    }

    private func commitNickname(_ session: WorkSession) {
        let value = nicknameText.trimmingCharacters(in: .whitespacesAndNewlines)
        vm.setSessionNickname(session.id, nickname: value.isEmpty ? nil : value)
        editingSessionID = nil
    }

    private var periodSessions: [WorkSession] {
        vm.reviewSessions(for: vm.workReportPeriod)
    }

    private var needsReviewSessions: [WorkSession] {
        periodSessions.filter { $0.attributionState == .needsReview }
    }

    private var displayedSessions: [WorkSession] {
        filter == .needsReview ? needsReviewSessions : periodSessions
    }

    private var displayedGroups: [WorkSessionReviewGroup] {
        displayedSessions.groupedForReview()
    }

    private var automaticCount: Int {
        periodSessions.filter { $0.attributionState == .automatic }.count
    }

    private var confirmedCount: Int {
        periodSessions.filter { $0.attributionState == .confirmed }.count
    }

    private func locationDetail(_ session: WorkSession) -> String {
        let location = session.repositoryName
            ?? session.worktreePath
            ?? session.cwd.map { NSString(string: $0).abbreviatingWithTildeInPath }
            ?? "Folder not captured"
        guard let branch = session.branch,
              !branch.isEmpty,
              !["HEAD", "main", "master"].contains(branch) else {
            return location
        }
        return "\(location) / \(branch)"
    }

    private func activityDetail(_ session: WorkSession, includeProvider: Bool = true) -> String {
        var parts: [String] = []
        if let startedAtUS = session.startedAtUS {
            let started = Date(timeIntervalSince1970: Double(startedAtUS) / 1_000_000)
            parts.append("started \(sessionDateFormatter.string(from: started))")
        } else if let timestamp = session.reviewTimestampUS {
            let date = Date(timeIntervalSince1970: Double(timestamp) / 1_000_000)
            parts.append(sessionDateFormatter.string(from: date))
        }
        if let durationUS = session.reviewDurationUS {
            parts.append(formatSessionDuration(durationUS))
        }
        if includeProvider { parts.append(session.provider.capitalized) }
        if let model = session.models.first, !model.isEmpty { parts.append(model) }
        if session.totalTokens > 0 { parts.append("\(formatTokens(session.totalTokens)) tokens") }
        if let cost = session.estimatedCostUSD { parts.append(formatCost(cost)) }
        return parts.joined(separator: " · ")
    }

    private func formatSessionDuration(_ durationUS: Int) -> String {
        let totalMinutes = max(1, Int((Double(durationUS) / 60_000_000).rounded()))
        let hours = totalMinutes / 60
        let minutes = totalMinutes % 60
        if hours == 0 { return "\(minutes)m" }
        if minutes == 0 { return "\(hours)h" }
        return "\(hours)h \(minutes)m"
    }

    private func groupActivityDetail(_ group: WorkSessionReviewGroup) -> String {
        var parts: [String] = []
        if let startedAtUS = group.representative.startedAtUS {
            let date = Date(timeIntervalSince1970: Double(startedAtUS) / 1_000_000)
            parts.append("latest started \(sessionDateFormatter.string(from: date))")
        } else if let timestamp = group.latestTimestampUS {
            let date = Date(timeIntervalSince1970: Double(timestamp) / 1_000_000)
            parts.append("latest \(sessionDateFormatter.string(from: date))")
        }
        if let durationUS = group.representative.reviewDurationUS {
            parts.append(formatSessionDuration(durationUS))
        }
        parts.append(group.providerSummary)
        if group.totalTokens > 0 { parts.append("\(formatTokens(group.totalTokens)) tokens") }
        return parts.joined(separator: " · ")
    }

    private func attributionLabel(_ session: WorkSession) -> String {
        switch session.attributionState {
        case .needsReview: return "Needs review"
        case .automatic: return "Automatic"
        case .confirmed: return "Confirmed"
        case .excluded: return "Excluded"
        }
    }

    private func attributionIcon(_ session: WorkSession) -> String {
        switch session.attributionState {
        case .needsReview: return "exclamationmark.circle.fill"
        case .automatic: return "sparkles"
        case .confirmed: return "checkmark.circle"
        case .excluded: return "minus.circle"
        }
    }

    private func attributionColor(_ session: WorkSession) -> Color {
        switch session.attributionState {
        case .needsReview: return .orange
        case .automatic: return .accentColor
        case .confirmed: return .green
        case .excluded: return .secondary
        }
    }

    private func providerIcon(_ provider: String) -> String {
        provider == "claude" ? "sparkles" : "chevron.left.forwardslash.chevron.right"
    }

    private func runtimeColor(_ state: String) -> Color {
        switch state {
        case "running": return .green
        case "active": return .cyan
        case "idle": return .orange
        default: return .secondary
        }
    }

    private func formatCost(_ cost: Double) -> String {
        cost < 0.01 ? String(format: "$%.3f", cost) : String(format: "$%.2f", cost)
    }
}

private enum SessionProviderFilter: String, CaseIterable, Identifiable {
    case all
    case claude
    case codex

    var id: String { rawValue }
    var label: String { rawValue.capitalized }
    var provider: String? { self == .all ? nil : rawValue }
}

struct SessionTimelineWorkspaceView: View {
    @ObservedObject var vm: UsageViewModel
    @Binding var selectedSessionID: String?
    @Binding var highlightedEvidenceID: String?
    @State private var searchText = ""
    @State private var providerFilter: SessionProviderFilter = .all

    var body: some View {
        VStack(spacing: 0) {
            searchBar
            Divider()
            HSplitView {
                sessionList
                    .frame(minWidth: 245, idealWidth: 285, maxWidth: 340)
                evidenceDetail
                    .frame(minWidth: 390, maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .task {
            if selectedSessionID == nil {
                selectedSessionID = recentSessions.first?.id
            }
        }
        .task(id: providerFilter.rawValue) {
            await vm.fetchSessionSearchIndexStatus(provider: providerFilter.provider)
        }
        .task(id: searchTaskID) {
            let query = trimmedSearch
            guard query.count >= 2 else {
                vm.clearSessionEvidenceSearch()
                return
            }
            do {
                try await Task.sleep(for: .milliseconds(280))
            } catch {
                return
            }
            guard !Task.isCancelled else { return }
            await vm.searchSessionEvidence(query: query, provider: providerFilter.provider)
        }
    }

    private var searchBar: some View {
        HStack(spacing: 10) {
            HStack(spacing: 7) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                TextField("Search sessions, prompts, files, commands", text: $searchText)
                    .textFieldStyle(.plain)
                if !searchText.isEmpty {
                    Button {
                        searchText = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .help("Clear search")
                }
            }
            .padding(.horizontal, 9)
            .frame(maxWidth: .infinity)
            .frame(height: 28)
            .background(Color(nsColor: .controlBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 6))

            Picker("Provider", selection: $providerFilter) {
                ForEach(SessionProviderFilter.allCases) { filter in
                    Text(filter.label).tag(filter)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 190)

            if vm.isSessionEvidenceSearchLoading {
                ProgressView()
                    .controlSize(.small)
                    .frame(width: 18, height: 18)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private var sessionList: some View {
        VStack(spacing: 0) {
            HStack {
                Text(isSearching ? "SEARCH RESULTS" : "RECENT SESSIONS")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                Spacer()
                Text("\(isSearching ? searchMatches.count : recentSessions.count)")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)

            Divider()

            if let error = vm.sessionEvidenceSearchError, isSearching {
                ContentUnavailableView(
                    "Search Unavailable",
                    systemImage: "exclamationmark.triangle",
                    description: Text(error)
                )
            } else if isSearching, searchMatches.isEmpty, !vm.isSessionEvidenceSearchLoading {
                ContentUnavailableView("No Matches", systemImage: "magnifyingglass")
            } else if !isSearching, recentSessions.isEmpty {
                ContentUnavailableView("No Recent Sessions", systemImage: "clock")
            } else {
                ScrollView {
                    LazyVStack(spacing: 2) {
                        if isSearching {
                            ForEach(searchMatches) { match in
                                searchResultRow(match)
                            }
                        } else {
                            ForEach(recentSessions) { session in
                                recentSessionRow(session)
                            }
                        }
                    }
                    .padding(6)
                }
            }

            if showsSessionListFooter {
                Divider()
                HStack(spacing: 10) {
                    if isSearching, vm.sessionEvidenceSearch?.partial == true {
                        Label("Partial results", systemImage: "exclamationmark.circle")
                            .foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 4)
                    if let status = vm.sessionSearchIndexStatus {
                        Label(indexStatusLabel(status), systemImage: indexStatusIcon(status))
                            .foregroundStyle(indexStatusColor(status))
                            .help(indexStatusHelp(status))
                    } else if vm.isSessionSearchIndexStatusLoading {
                        ProgressView()
                            .controlSize(.mini)
                    } else if vm.sessionSearchIndexStatusError != nil {
                        Label("Index unavailable", systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.orange)
                    }
                }
                .font(.caption2)
                .padding(.horizontal, 12)
                .padding(.vertical, 7)
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private func recentSessionRow(_ session: WorkSession) -> some View {
        Button {
            selectedSessionID = session.id
            highlightedEvidenceID = nil
        } label: {
            HStack(alignment: .top, spacing: 9) {
                Image(systemName: providerIcon(session.provider))
                    .frame(width: 18, height: 18)
                    .foregroundStyle(session.provider == "claude" ? .cyan : .green)
                VStack(alignment: .leading, spacing: 3) {
                    Text(session.displayName)
                        .font(.subheadline.weight(.medium))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(sessionLocation(session))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer(minLength: 4)
                if let timestamp = session.reviewTimestampUS {
                    Text(shortSessionTime(timestamp))
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.tertiary)
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(selectionBackground(session.id))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func searchResultRow(_ match: SessionEvidenceMatch) -> some View {
        Button {
            selectedSessionID = match.sessionID
            highlightedEvidenceID = match.evidenceID
        } label: {
            HStack(alignment: .top, spacing: 9) {
                Image(systemName: evidenceIcon(match.kind))
                    .frame(width: 18, height: 18)
                    .foregroundStyle(evidenceColor(match.kind))
                VStack(alignment: .leading, spacing: 3) {
                    Text(match.sessionName)
                        .font(.subheadline.weight(.medium))
                        .lineLimit(1)
                    Text(match.snippet)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                    if let repository = match.repositoryName {
                        Text(repository)
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                    }
                }
                Spacer(minLength: 4)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(selectionBackground(match.sessionID))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private var evidenceDetail: some View {
        if let sessionID = selectedSessionID,
           let session = vm.workReviewSessions.first(where: { $0.id == sessionID }) {
            SessionEvidenceDetailView(
                vm: vm,
                session: session,
                highlightedEvidenceID: $highlightedEvidenceID
            )
        } else {
            ContentUnavailableView("No Session Selected", systemImage: "doc.text")
        }
    }

    private var recentSessions: [WorkSession] {
        vm.reviewSessions(for: vm.workReportPeriod).filter { session in
            providerFilter.provider == nil || session.provider == providerFilter.provider
        }
    }

    private var searchMatches: [SessionEvidenceMatch] {
        vm.sessionEvidenceSearch?.matches ?? []
    }

    private var trimmedSearch: String {
        searchText.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var isSearching: Bool { trimmedSearch.count >= 2 }
    private var searchTaskID: String { "\(providerFilter.rawValue):\(trimmedSearch)" }
    private var showsSessionListFooter: Bool {
        (isSearching && vm.sessionEvidenceSearch?.partial == true)
            || vm.sessionSearchIndexStatus != nil
            || vm.isSessionSearchIndexStatusLoading
            || vm.sessionSearchIndexStatusError != nil
    }

    private func indexStatusLabel(_ status: SessionSearchIndexStatus) -> String {
        if status.state == "error" { return "Index unavailable" }
        if status.state == "empty" { return "Building index" }
        if status.state == "updating" || status.sessionsIndexed < status.sessionsAvailable {
            return "Index updating"
        }
        return "History indexed"
    }

    private func indexStatusIcon(_ status: SessionSearchIndexStatus) -> String {
        if status.state == "error" { return "exclamationmark.triangle.fill" }
        if status.state == "empty" { return "clock.arrow.circlepath" }
        if status.state == "updating" || status.sessionsIndexed < status.sessionsAvailable {
            return "arrow.triangle.2.circlepath"
        }
        return "checkmark.circle.fill"
    }

    private func indexStatusColor(_ status: SessionSearchIndexStatus) -> Color {
        if status.state == "error" { return .orange }
        if status.state == "empty" || status.state == "updating"
            || status.sessionsIndexed < status.sessionsAvailable {
            return .secondary
        }
        return .green
    }

    private func indexStatusHelp(_ status: SessionSearchIndexStatus) -> String {
        if status.staleSessions > 0 && status.sessionsIndexed >= status.sessionsAvailable {
            return "\(status.sessionsIndexed) sessions indexed; \(status.staleSessions) searched live"
        }
        return "\(status.sessionsIndexed) of \(status.sessionsAvailable) sessions indexed"
    }

    private func selectionBackground(_ sessionID: String) -> some View {
        RoundedRectangle(cornerRadius: 5)
            .fill(selectedSessionID == sessionID ? Color.accentColor.opacity(0.14) : Color.clear)
    }

    private func sessionLocation(_ session: WorkSession) -> String {
        let repository = session.repositoryName ?? session.provider.capitalized
        guard let branch = session.branch, !branch.isEmpty else { return repository }
        return "\(repository) / \(branch)"
    }

    private func shortSessionTime(_ timestampUS: Int) -> String {
        sessionTimelineTimeFormatter.string(from: Date(
            timeIntervalSince1970: Double(timestampUS) / 1_000_000
        ))
    }
}

private struct SessionEvidenceDetailView: View {
    @ObservedObject var vm: UsageViewModel
    let session: WorkSession
    @Binding var highlightedEvidenceID: String?

    var body: some View {
        VStack(spacing: 0) {
            detailHeader
            Divider()
            detailBody
        }
        .task(id: session.id) {
            await vm.fetchSessionEvidence(sessionID: session.id)
        }
    }

    private var detailHeader: some View {
        HStack(spacing: 10) {
            Image(systemName: providerIcon(session.provider))
                .font(.system(size: 16))
                .foregroundStyle(session.provider == "claude" ? .cyan : .green)
                .frame(width: 24, height: 24)
            VStack(alignment: .leading, spacing: 2) {
                Text(session.displayName)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(detailSubtitle)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer()
            if let response = response {
                Text("\(response.items.count) events")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            Button {
                Task { await vm.fetchSessionEvidence(sessionID: session.id, force: true) }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.plain)
            .disabled(isLoading)
            .help("Reload session evidence")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    @ViewBuilder
    private var detailBody: some View {
        if isLoading, response == nil {
            ProgressView()
                .controlSize(.small)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let error = vm.sessionEvidenceErrors[session.id] {
            ContentUnavailableView(
                "Evidence Unavailable",
                systemImage: "exclamationmark.triangle",
                description: Text(error)
            )
        } else if response?.sourceMissing == true {
            ContentUnavailableView("Session Source Missing", systemImage: "doc.badge.ellipsis")
        } else if let response, response.items.isEmpty {
            ContentUnavailableView("No Evidence Found", systemImage: "doc.text.magnifyingglass")
        } else if let response {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 0) {
                        if response.truncated {
                            Label(
                                "Showing the most recent evidence",
                                systemImage: "clock.arrow.circlepath"
                            )
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 10)
                            Divider()
                        }
                        ForEach(Array(response.items.enumerated()), id: \.element.id) { index, item in
                            SessionEvidenceRow(
                                item: item,
                                isHighlighted: highlightedEvidenceID == item.id
                            )
                            .id(item.id)
                            if index < response.items.count - 1 {
                                Divider().padding(.leading, 52)
                            }
                        }
                    }
                }
                .onChange(of: highlightedEvidenceID) { _, value in
                    guard let value else { return }
                    withAnimation(.easeInOut(duration: 0.2)) {
                        proxy.scrollTo(value, anchor: .center)
                    }
                }
                .task(id: highlightedEvidenceID) {
                    guard let value = highlightedEvidenceID else { return }
                    proxy.scrollTo(value, anchor: .center)
                }
            }
        }
    }

    private var response: SessionEvidenceResponse? { vm.sessionEvidence[session.id] }
    private var isLoading: Bool { vm.sessionEvidenceLoading.contains(session.id) }

    private var detailSubtitle: String {
        var values = [session.provider.capitalized]
        if let repository = session.repositoryName { values.append(repository) }
        if let branch = session.branch, !branch.isEmpty { values.append(branch) }
        return values.joined(separator: " / ")
    }
}

private struct SessionEvidenceRow: View {
    let item: SessionEvidenceItem
    let isHighlighted: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            ZStack {
                Circle()
                    .fill(evidenceColor(item.kind).opacity(0.14))
                Image(systemName: evidenceIcon(item.kind))
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(evidenceColor(item.kind))
            }
            .frame(width: 26, height: 26)

            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline) {
                    Text(item.title)
                        .font(.subheadline.weight(.medium))
                    Spacer(minLength: 8)
                    Text(evidenceTime(item.occurredAtUS))
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.tertiary)
                }
                Text(item.summary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .lineLimit(8)
                if item.filePaths.count > 1 {
                    Text("\(item.filePaths.count) files")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(isHighlighted ? Color.accentColor.opacity(0.11) : Color.clear)
    }

    private func evidenceTime(_ timestampUS: Int) -> String {
        sessionEvidenceTimeFormatter.string(from: Date(
            timeIntervalSince1970: Double(timestampUS) / 1_000_000
        ))
    }
}

private func providerIcon(_ provider: String) -> String {
    provider == "claude" ? "sparkles" : "chevron.left.forwardslash.chevron.right"
}

private func evidenceIcon(_ kind: String) -> String {
    switch kind {
    case "prompt": return "person.fill"
    case "response": return "text.bubble.fill"
    case "edit": return "pencil"
    case "read": return "doc.text"
    case "test": return "checkmark.circle.fill"
    case "build": return "hammer.fill"
    case "git": return "arrow.triangle.branch"
    case "web": return "globe"
    case "search": return "magnifyingglass"
    case "command": return "terminal.fill"
    case "session": return "clock"
    default: return "wrench.and.screwdriver.fill"
    }
}

private func evidenceColor(_ kind: String) -> Color {
    switch kind {
    case "prompt": return .blue
    case "response": return .green
    case "edit": return .orange
    case "test": return .mint
    case "build": return .purple
    case "git": return .indigo
    case "web": return .cyan
    case "command": return .yellow
    default: return .secondary
    }
}

private func workItemIcon(_ kind: String) -> String {
    switch kind {
    case "project": return "folder"
    case "story": return "book.closed"
    default: return "checkmark.circle"
    }
}

private let sessionDateFormatter: DateFormatter = {
    let formatter = DateFormatter()
    formatter.dateFormat = "EEE h:mm a"
    return formatter
}()

private let sessionTimelineTimeFormatter: DateFormatter = {
    let formatter = DateFormatter()
    formatter.dateFormat = "MMM d"
    return formatter
}()

private let sessionEvidenceTimeFormatter: DateFormatter = {
    let formatter = DateFormatter()
    formatter.dateFormat = "h:mm:ss a"
    return formatter
}()
