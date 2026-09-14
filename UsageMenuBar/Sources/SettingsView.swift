import AppKit
import SwiftUI
import ServiceManagement

// MARK: - Settings Keys

enum SettingsKey {
    static let iconMode = "iconMode"
    static let showClaudeSession = "showClaudeSession"
    static let showClaudeWeekly = "showClaudeWeekly"
    static let showCodexSession = "showCodexSession"
    static let showCodexWeekly = "showCodexWeekly"
    static let apiURL = "apiURL"
    static let apiToken = "apiToken"
    static let refreshInterval = "refreshInterval"
    static let launchAtLogin = "launchAtLogin"
    static let usageAlerts = "usageAlerts"
}

// MARK: - Settings View

struct SettingsView: View {
    @ObservedObject var vm: UsageViewModel

    @State private var iconMode = "Thermometer"
    @State private var showClaudeSession = true
    @State private var showClaudeWeekly = true
    @State private var showCodexSession = true
    @State private var showCodexWeekly = true
    @State private var refreshInterval = 30.0
    @AppStorage(SettingsKey.launchAtLogin) private var launchAtLogin = false
    @State private var usageAlerts = false
    @State private var newWorkKind = "project"
    @State private var newWorkName = ""
    @State private var newWorkParentID = ""
    @State private var newWorkRepositoryID = ""
    @State private var newWorkColor = "#2FAF88"

    var body: some View {
        Form {
            Section {
                Picker("Icon Style", selection: $iconMode) {
                    ForEach(MenuBarIconMode.allCases) { mode in
                        Text(mode.rawValue).tag(mode.rawValue)
                    }
                }

                VStack(alignment: .leading, spacing: 6) {
                    Text("Visible Bars")
                        .foregroundStyle(.secondary)
                        .font(.system(.subheadline))
                    HStack(spacing: 16) {
                        VStack(alignment: .leading, spacing: 4) {
                            Toggle("Claude Session", isOn: $showClaudeSession)
                                .accessibilityLabel("Show Claude session in the menu bar icon")
                            Toggle("Claude Weekly", isOn: $showClaudeWeekly)
                                .accessibilityLabel("Show Claude weekly quota in the menu bar icon")
                        }
                        VStack(alignment: .leading, spacing: 4) {
                            if hasCodexSessionBar {
                                Toggle("Codex Session", isOn: $showCodexSession)
                                    .accessibilityLabel("Show Codex session in the menu bar icon")
                            }
                            Toggle("Codex Weekly", isOn: $showCodexWeekly)
                                .accessibilityLabel("Show Codex weekly quota in the menu bar icon")
                        }
                    }
                    .toggleStyle(.checkbox)
                }
            } header: {
                Label("Appearance", systemImage: "paintbrush")
            }

            Section {
                HStack {
                    Text("Refresh")
                    Slider(value: $refreshInterval, in: 10...120, step: 5)
                    Text("\(Int(refreshInterval))s")
                        .font(.system(.body, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .frame(width: 36, alignment: .trailing)
                }

                Toggle("Launch at Login", isOn: $launchAtLogin)
                    .onChange(of: launchAtLogin) { _, newValue in
                        setLaunchAtLogin(newValue)
                    }

                Toggle("Usage Alerts", isOn: $usageAlerts)

            } header: {
                Label("General", systemImage: "gearshape")
            }

            Section {
                Toggle(
                    "Track local work",
                    isOn: Binding(
                        get: { vm.workTrackingEnabled },
                        set: { vm.setWorkTrackingEnabled($0) }
                    )
                )
                .disabled(vm.isWorkMutationInFlight)

                if vm.workTrackingEnabled {
                    HStack {
                        Label(refreshLabel, systemImage: refreshIcon)
                            .font(.caption)
                            .foregroundStyle(refreshColor)
                        Spacer()
                        Button {
                            vm.refreshWorkLedger()
                        } label: {
                            Image(systemName: "arrow.triangle.2.circlepath")
                        }
                        .help("Refresh work activity")
                    }

                    if vm.workRepositories.isEmpty {
                        Text("No repositories selected")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(vm.workRepositories) { repository in
                            Toggle(
                                repository.displayName,
                                isOn: Binding(
                                    get: { repository.enabled },
                                    set: {
                                        vm.setRepositoryEnabled(repository.id, enabled: $0)
                                    }
                                )
                            )
                            .toggleStyle(.checkbox)
                            .help(repository.commonDir)
                        }
                    }

                    Divider()

                    Picker("Type", selection: $newWorkKind) {
                        Text("Project").tag("project")
                        Text("Task").tag("task")
                        Text("Story").tag("story")
                    }
                    .pickerStyle(.segmented)
                    .onChange(of: newWorkKind) { _, kind in
                        if kind != "project", newWorkParentID.isEmpty {
                            newWorkParentID = projects.first?.id ?? ""
                        }
                    }

                    TextField("Name", text: $newWorkName)

                    if newWorkKind != "project" {
                        Picker("Project", selection: $newWorkParentID) {
                            Text("Select project").tag("")
                            ForEach(projects) { project in
                                Text(project.name).tag(project.id)
                            }
                        }
                    }

                    Picker("Repository", selection: $newWorkRepositoryID) {
                        Text("No repository").tag("")
                        ForEach(vm.workRepositories) { repository in
                            Text(repository.displayName).tag(repository.id)
                        }
                    }

                    if newWorkKind == "project" {
                        HStack(spacing: 10) {
                            Text("Color")
                            ForEach(workColors, id: \.hex) { swatch in
                                Button {
                                    newWorkColor = swatch.hex
                                } label: {
                                    Circle()
                                        .fill(swatch.color)
                                        .frame(width: 16, height: 16)
                                        .overlay {
                                            if newWorkColor == swatch.hex {
                                                Image(systemName: "checkmark")
                                                    .font(.system(size: 8, weight: .bold))
                                                    .foregroundStyle(.white)
                                            }
                                        }
                                }
                                .buttonStyle(.plain)
                                .help(swatch.hex)
                            }
                        }
                    }

                    HStack {
                        if newWorkKind == "project" {
                            Button {
                                chooseProjectFolder()
                            } label: {
                                Label("From Folder...", systemImage: "folder.badge.plus")
                            }
                            .disabled(vm.isWorkMutationInFlight)
                            .help("Create a project from a Git folder")
                        }
                        Spacer()
                        Button {
                            createWorkItem()
                        } label: {
                            Label("Add", systemImage: "plus")
                        }
                        .disabled(!canCreateWorkItem || vm.isWorkMutationInFlight)
                    }

                    if !vm.workItems.isEmpty {
                        Divider()
                        ForEach(vm.workItems) { item in
                            HStack {
                                Image(systemName: workItemIcon(item.kind))
                                    .foregroundStyle(.secondary)
                                Text(item.displayName)
                                    .lineLimit(1)
                                Spacer()
                                Button(role: .destructive) {
                                    vm.archiveWorkItem(item.id)
                                } label: {
                                    Image(systemName: "trash")
                                }
                                .buttonStyle(.plain)
                                .help("Archive \(item.name)")
                            }
                        }
                    }

                    if let error = vm.workError {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .font(.caption)
                            .foregroundStyle(.orange)
                    }
                }
            } header: {
                Label("Work Tracking", systemImage: "briefcase")
            }

            Section {
                providerAccessRow(
                    provider: "claude",
                    name: "Claude",
                    signInURL: "https://claude.ai/settings/usage"
                )
                providerAccessRow(
                    provider: "codex",
                    name: "Codex",
                    signInURL: "https://chatgpt.com/codex/cloud/settings/analytics"
                )

                Button(action: vm.refreshProviderAccess) {
                    Label("Refresh Access", systemImage: "arrow.clockwise")
                }
            } header: {
                Label("Provider Access", systemImage: "key")
            }
        }
        .formStyle(.grouped)
        .scrollContentBackground(.hidden)
        .background(Color(nsColor: .windowBackgroundColor))
        .frame(width: 480, height: 650)
        .onAppear {
            loadFromVM()
            Task { await vm.fetchWorkState() }
        }
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("Done") { applyAndClose() }
            }
        }
    }

    // MARK: - Actions

    private func loadFromVM() {
        iconMode = vm.iconMode.rawValue
        showClaudeSession = vm.visibleBars.claudeSession
        showClaudeWeekly = vm.visibleBars.claudeWeekly
        showCodexSession = vm.visibleBars.codexSession
        showCodexWeekly = vm.visibleBars.codexWeekly
        refreshInterval = vm.refreshInterval
        usageAlerts = vm.alertsEnabled
    }

    private func applyAndClose() {
        vm.applySettings(
            iconMode: MenuBarIconMode(rawValue: iconMode) ?? .thermometer,
            refreshInterval: refreshInterval,
            visibleBars: VisibleBars(
                claudeSession: showClaudeSession,
                claudeWeekly: showClaudeWeekly,
                codexSession: showCodexSession,
                codexWeekly: showCodexWeekly
            ),
            alertsEnabled: usageAlerts
        )
        SettingsWindowController.shared.close()
    }

    private func setLaunchAtLogin(_ enabled: Bool) {
        if #available(macOS 13.0, *) {
            do {
                if enabled {
                    try SMAppService.mainApp.register()
                } else {
                    try SMAppService.mainApp.unregister()
                }
            } catch {}
        }
    }

    private var projects: [WorkItem] {
        vm.workItems.filter { $0.kind == "project" }
    }

    private var hasCodexSessionBar: Bool {
        vm.stats?.codexQuota?.sessionUsedPct != nil
            || vm.stats?.codexQuota?.limits?.contains(where: { $0.windowKind == "session" }) == true
    }

    private var canCreateWorkItem: Bool {
        !newWorkName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && (newWorkKind == "project" || !newWorkParentID.isEmpty)
    }

    private var refreshLabel: String {
        switch vm.workStatus?.refresh.status {
        case "running": return "Refreshing"
        case "partial": return "Refresh incomplete"
        case "ok": return "Activity current"
        default: return "Ready"
        }
    }

    private var refreshIcon: String {
        switch vm.workStatus?.refresh.status {
        case "running": return "arrow.triangle.2.circlepath"
        case "partial": return "exclamationmark.triangle.fill"
        case "ok": return "checkmark.circle.fill"
        default: return "pause.circle"
        }
    }

    private var refreshColor: Color {
        switch vm.workStatus?.refresh.status {
        case "partial": return .orange
        case "ok": return .green
        default: return .secondary
        }
    }

    private var workColors: [(hex: String, color: Color)] {
        [
            ("#2FAF88", Color(red: 0.18, green: 0.69, blue: 0.53)),
            ("#3D8DDE", Color(red: 0.24, green: 0.55, blue: 0.87)),
            ("#D99B2B", Color(red: 0.85, green: 0.61, blue: 0.17)),
            ("#D85D67", Color(red: 0.85, green: 0.36, blue: 0.40)),
            ("#8B72C9", Color(red: 0.55, green: 0.45, blue: 0.79)),
        ]
    }

    private func chooseProjectFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Add Project"
        panel.message = "Choose a folder inside a Git repository."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task {
            _ = await vm.createProjectFromFolder(url.path)
        }
    }

    private func createWorkItem() {
        let name = newWorkName.trimmingCharacters(in: .whitespacesAndNewlines)
        let parentID = newWorkKind == "project" ? nil : newWorkParentID
        let repositoryID = newWorkRepositoryID.isEmpty ? nil : newWorkRepositoryID
        let colorHex = newWorkKind == "project" ? newWorkColor : nil
        Task {
            if await vm.createWorkItem(
                kind: newWorkKind,
                name: name,
                parentID: parentID,
                repositoryID: repositoryID,
                colorHex: colorHex
            ) {
                newWorkName = ""
                if newWorkKind != "project" {
                    newWorkParentID = projects.first?.id ?? ""
                }
            }
        }
    }

    private func workItemIcon(_ kind: String) -> String {
        switch kind {
        case "project": return "folder"
        case "story": return "book.closed"
        default: return "checkmark.circle"
        }
    }

    @ViewBuilder
    private func providerAccessRow(provider: String, name: String, signInURL: String) -> some View {
        let state = providerAccessState(provider)
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(name)
                Label(state.label, systemImage: state.icon)
                    .font(.caption)
                    .foregroundStyle(state.color)
            }
            Spacer()
            Button {
                guard let url = URL(string: signInURL) else { return }
                NSWorkspace.shared.open(url)
            } label: {
                Label(state.action, systemImage: "arrow.up.right.square")
            }
        }
    }

    private func providerAccessState(_ provider: String) -> (label: String, icon: String, color: Color, action: String) {
        switch vm.stats?.providerHealth?[provider]?.quota?.status {
        case "current":
            return ("Connected", "checkmark.circle.fill", .green, "Open")
        case "stale":
            return ("Refresh needed", "exclamationmark.triangle.fill", .orange, "Sign In")
        case "unavailable":
            return ("Sign in needed", "exclamationmark.circle.fill", .orange, "Sign In")
        default:
            return ("Not configured", "minus.circle", .secondary, "Sign In")
        }
    }
}

// MARK: - Visible Bars Config

struct VisibleBars {
    var claudeSession: Bool
    var claudeWeekly: Bool
    var codexSession: Bool
    var codexWeekly: Bool
}

// MARK: - Settings Window Controller

class SettingsWindowController {
    static let shared = SettingsWindowController()
    private var window: NSWindow?

    func open(vm: UsageViewModel) {
        if let window = window, window.isVisible {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }

        let view = SettingsView(vm: vm)
        let hostingView = NSHostingView(rootView: view)

        let w = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 650),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        w.title = "Settings"
        w.backgroundColor = .windowBackgroundColor
        w.contentView = hostingView
        w.center()
        w.isReleasedWhenClosed = false
        w.level = .floating
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        self.window = w
    }

    func close() {
        window?.close()
    }
}
