import AppKit
import SwiftUI

struct WorkReportView: View {
    @ObservedObject var vm: UsageViewModel
    let loadsOnAppear: Bool
    let showsHeader: Bool

    init(vm: UsageViewModel, loadsOnAppear: Bool = true, showsHeader: Bool = true) {
        self.vm = vm
        self.loadsOnAppear = loadsOnAppear
        self.showsHeader = showsHeader
    }

    var body: some View {
        VStack(spacing: 0) {
            if showsHeader {
                header
                Divider()
            }
            Group {
                if vm.isWorkReportLoading && vm.workReport == nil {
                    ProgressView("Loading work report")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let report = vm.workReport {
                    reportBody(report)
                } else {
                    ContentUnavailableView(
                        "Report unavailable",
                        systemImage: "chart.bar.xaxis",
                        description: Text(vm.workReportError ?? "No work activity has been reported yet.")
                    )
                }
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .task(id: vm.workReportPeriod) {
            if loadsOnAppear {
                await vm.fetchWorkReport(period: vm.workReportPeriod)
            }
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Label("Work Report", systemImage: "briefcase")
                .font(.headline)
            Spacer()
            Picker("Period", selection: $vm.workReportPeriod) {
                ForEach(WorkReportPeriod.allCases) { period in
                    Text(period.label).tag(period)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 220)
            Button {
                Task { await vm.fetchWorkReport() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.plain)
            .disabled(vm.isWorkReportLoading)
            .help("Refresh report")
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 14)
    }

    private func reportBody(_ report: WorkReport) -> some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 18) {
                metricStrip(report.totals)

                if !report.totals.usageCategories.isEmpty {
                    reportSection("Usage drivers", icon: "chart.bar.fill") {
                        WorkUsageDriversView(categories: report.totals.usageCategories)
                    }
                }

                if let error = vm.workReportError {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }

                reportSection("Projects", icon: "folder") {
                    if report.projects.isEmpty {
                        emptyRow("No tagged project activity")
                    } else {
                        ForEach(report.projects, id: \.stableID) { project in
                            WorkReportBucketRow(
                                bucket: project,
                                emphasized: true,
                                onOpenRepository: openRepository
                            )
                            ForEach(
                                report.workItems.filter {
                                    $0.parentID == project.id && $0.metrics.hasActivity
                                },
                                id: \.stableID
                            ) { child in
                                WorkReportBucketRow(
                                    bucket: child,
                                    indented: true,
                                    onOpenRepository: openRepository
                                )
                            }
                        }
                    }
                    if report.unassigned.hasActivity {
                        Divider()
                        WorkReportBucketRow(
                            bucket: unassignedBucket(report.unassigned),
                            onOpenRepository: openRepository
                        )
                    }
                }

                reportSection("Repositories", icon: "externaldrive") {
                    if report.repositories.isEmpty {
                        emptyRow("No repository activity")
                    } else {
                        ForEach(report.repositories, id: \.stableID) { repository in
                            WorkReportBucketRow(
                                bucket: repository,
                                onOpenRepository: openRepository
                            )
                        }
                    }
                }

                reportSection("Providers", icon: "sparkles") {
                    ForEach(report.providers, id: \.stableID) { provider in
                        WorkReportBucketRow(bucket: provider)
                    }
                }

                pricingFooter(report.totals)
            }
            .padding(18)
        }
        .overlay(alignment: .topTrailing) {
            if vm.isWorkReportLoading {
                ProgressView()
                    .controlSize(.small)
                    .padding(10)
            }
        }
    }

    private func metricStrip(_ metrics: WorkReportMetrics) -> some View {
        VStack(spacing: 8) {
            HStack(spacing: 0) {
                reportMetric("Active time", formatDuration(metrics.reportedSeconds))
                reportMetric("Sessions", "\(metrics.sessionCount)")
                reportMetric("Tokens", formatTokens(metrics.totalTokens))
                reportMetric("Est. cost", formatCost(metrics.estimatedCostUSD))
            }
            Divider()
            HStack(spacing: 0) {
                reportMetric("Commits", "\(metrics.gitCommits)")
                reportMetric("Merged PRs", "\(metrics.pullRequestCount)")
                reportMetric("Files", "\(metrics.filesChanged)")
                reportMetric("Changed LOC", formatCount(metrics.changedLineCount))
            }
            Divider()
            HStack(spacing: 0) {
                reportMetric("Cost / PR", formatRatio(metrics.costPerPR))
                reportMetric("Cost / commit", formatRatio(metrics.costPerCommit))
                reportMetric("Cost / LOC", formatRatio(metrics.costPerChangedLine))
            }
        }
        .padding(.vertical, 12)
        .background(Color.primary.opacity(0.045))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    private func reportMetric(_ label: String, _ value: String) -> some View {
        VStack(spacing: 3) {
            Text(value)
                .font(.system(.title3, design: .rounded).weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }

    private func reportSection<Content: View>(
        _ title: String,
        icon: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Label(title, systemImage: icon)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
            content()
        }
    }

    private func emptyRow(_ text: String) -> some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.tertiary)
            .padding(.vertical, 4)
    }

    private func pricingFooter(_ metrics: WorkReportMetrics) -> some View {
        HStack(spacing: 6) {
            Image(systemName: metrics.unpricedModels.isEmpty ? "checkmark.seal" : "exclamationmark.triangle")
            if let coverage = metrics.pricingCoveragePct {
                Text("\(coverage, specifier: "%.1f")% of tokens priced")
            } else {
                Text("No priced token activity")
            }
            if !metrics.unpricedModels.isEmpty {
                Text("· \(metrics.unpricedModels.joined(separator: ", "))")
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
        .font(.caption)
        .foregroundStyle(metrics.unpricedModels.isEmpty ? Color.secondary : Color.orange)
    }

    private func unassignedBucket(_ metrics: WorkReportMetrics) -> WorkReportBucket {
        WorkReportBucket(
            id: nil,
            name: "Not attributed",
            kind: "unassigned",
            parentID: nil,
            parentName: nil,
            repositoryID: nil,
            repositoryName: nil,
            colorHex: nil,
            enabled: nil,
            inferred: false,
            attributionSource: "unassigned",
            attributionConfidence: 0,
            metrics: metrics
        )
    }

    private func openRepository(_ repositoryID: String) {
        guard let repository = vm.workRepositories.first(where: { $0.id == repositoryID }) else {
            return
        }
        var url = URL(fileURLWithPath: repository.commonDir, isDirectory: true)
        if url.lastPathComponent == ".git" {
            url.deleteLastPathComponent()
        }
        NSWorkspace.shared.open(url)
    }

    private func formatDuration(_ seconds: Double) -> String {
        let hours = seconds / 3_600
        return hours >= 10 ? String(format: "%.0fh", hours) : String(format: "%.1fh", hours)
    }

    private func formatCost(_ cost: Double?) -> String {
        guard let cost else { return "—" }
        if cost < 0.01 { return String(format: "$%.3f", cost) }
        return String(format: "$%.2f", cost)
    }

    /// Renders a cost-to-outcome ratio, or the same placeholder any absent figure gets.
    ///
    /// A nil ratio is the server saying the figure is not stateable: no denominator,
    /// an unpriced cost side, or no measured AI usage at all. It is shown as the
    /// standard placeholder rather than as $0.00, because a zero here would read as
    /// "this work was free" and would make unmeasured work look like the cheapest.
    /// Per-line costs are legitimately tiny, so very small values keep their exponent
    /// instead of being rounded to a misleading $0.00000.
    private func formatRatio(_ value: Double?) -> String {
        guard let value else { return "—" }
        if value >= 1 { return String(format: "$%.2f", value) }
        if value >= 0.001 { return String(format: "$%.4f", value) }
        if value > 0 { return String(format: "$%.2e", value) }
        return String(format: "$%.2f", value)
    }

    private func formatCount(_ value: Int) -> String {
        if value >= 1_000_000 { return String(format: "%.1fM", Double(value) / 1_000_000) }
        if value >= 1_000 { return String(format: "%.1fk", Double(value) / 1_000) }
        return "\(value)"
    }
}

private struct WorkReportBucketRow: View {
    let bucket: WorkReportBucket
    var emphasized = false
    var indented = false
    var onOpenRepository: ((String) -> Void)?

    var body: some View {
        HStack(spacing: 10) {
            if indented {
                Color.clear.frame(width: 14)
            }
            Circle()
                .fill(bucketColor)
                .frame(width: 7, height: 7)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 5) {
                    Text(bucket.name)
                        .font(emphasized ? .subheadline.weight(.semibold) : .subheadline)
                        .lineLimit(1)
                    if bucket.inferred == true {
                        Image(systemName: "sparkles")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .help(attributionHelp)
                            .accessibilityLabel(attributionHelp)
                    }
                }
                Text(detail)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            Spacer(minLength: 8)
            if let repositoryID = bucket.repositoryID,
               let onOpenRepository {
                Button {
                    onOpenRepository(repositoryID)
                } label: {
                    Image(systemName: "folder")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Open local repository")
                .accessibilityLabel("Open \(bucket.repositoryName ?? bucket.name) repository")
            }
            VStack(alignment: .trailing, spacing: 2) {
                Text(formatTokens(bucket.metrics.totalTokens))
                    .font(.system(.caption, design: .monospaced).weight(.medium))
                Text(formatCost(bucket.metrics.estimatedCostUSD))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            .frame(width: 68, alignment: .trailing)
        }
        .padding(.vertical, 4)
    }

    private var detail: String {
        var parts: [String] = []
        if bucket.metrics.reportedSeconds > 0 {
            parts.append("\(formatDuration(bucket.metrics.reportedSeconds)) active")
        }
        if bucket.metrics.sessionCount > 0 {
            parts.append("\(bucket.metrics.sessionCount) sessions")
        }
        if bucket.metrics.gitCommits > 0 {
            parts.append("\(bucket.metrics.gitCommits) commits")
        }
        if bucket.metrics.pullRequestCount > 0 {
            parts.append("\(bucket.metrics.pullRequestCount) merged PRs")
        }
        if bucket.metrics.filesChanged > 0 {
            parts.append("\(bucket.metrics.filesChanged) files")
        }
        if bucket.metrics.changedLineCount > 0 {
            parts.append("+\(bucket.metrics.additions) / -\(bucket.metrics.deletions)")
        }
        if bucket.metrics.gitCommits == 0,
           bucket.metrics.pullRequestCount == 0,
           bucket.metrics.activityEvents > 0 {
            parts.append("\(bucket.metrics.activityEvents) events")
        }
        if let credits = bucket.metrics.estimatedCredits {
            parts.append(String(format: "%.1f credits", credits))
        }
        if let driver = bucket.metrics.usageCategories.first,
           driver.sharePct >= 10 {
            parts.append("\(driver.label) \(Int(driver.sharePct.rounded()))%")
        }
        return parts.isEmpty ? "No activity" : parts.joined(separator: " · ")
    }

    private var attributionHelp: String {
        let confidence = Int(((bucket.attributionConfidence ?? 0) * 100).rounded())
        switch bucket.attributionSource {
        case "branch_match": return "Inferred from a matching Git branch (\(confidence)% confidence)"
        case "linked_repository": return "Inferred from the linked repository (\(confidence)% confidence)"
        case "branch": return "Inferred from the session Git branch (\(confidence)% confidence)"
        default: return "Inferred from the session repository (\(confidence)% confidence)"
        }
    }

    private var bucketColor: Color {
        guard let hex = bucket.colorHex,
              hex.count == 7,
              let value = Int(hex.dropFirst(), radix: 16) else {
            return bucket.id == nil ? .secondary : .accentColor
        }
        return Color(
            red: Double((value >> 16) & 0xFF) / 255,
            green: Double((value >> 8) & 0xFF) / 255,
            blue: Double(value & 0xFF) / 255
        )
    }

    private func formatDuration(_ seconds: Double) -> String {
        let hours = seconds / 3_600
        return hours >= 10 ? String(format: "%.0fh", hours) : String(format: "%.1fh", hours)
    }

    private func formatCost(_ cost: Double?) -> String {
        guard let cost else { return "—" }
        if cost < 0.01 { return String(format: "$%.3f", cost) }
        return String(format: "$%.2f", cost)
    }
}

private struct WorkUsageDriversView: View {
    let categories: [WorkReportUsageCategory]

    var body: some View {
        VStack(spacing: 6) {
            GeometryReader { geometry in
                HStack(spacing: 0) {
                    ForEach(categories.filter { $0.sharePct > 0 }) { category in
                        Rectangle()
                            .fill(categoryColor(category.color))
                            .frame(width: max(geometry.size.width * category.sharePct / 100, 1))
                            .help("\(category.label): \(category.sharePct, specifier: "%.1f")%")
                    }
                }
            }
            .frame(height: 7)
            .clipShape(RoundedRectangle(cornerRadius: 3))
            HStack(spacing: 12) {
                ForEach(Array(categories.prefix(4))) { category in
                    HStack(spacing: 4) {
                        Circle().fill(categoryColor(category.color)).frame(width: 6, height: 6)
                        Text("\(category.label) \(Int(category.sharePct.rounded()))%")
                            .lineLimit(1)
                    }
                }
                Spacer(minLength: 0)
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
        }
    }

    private func categoryColor(_ hex: String) -> Color {
        guard hex.count == 7, let value = Int(hex.dropFirst(), radix: 16) else { return .secondary }
        return Color(
            red: Double((value >> 16) & 0xFF) / 255,
            green: Double((value >> 8) & 0xFF) / 255,
            blue: Double(value & 0xFF) / 255
        )
    }
}

@MainActor
final class WorkReportWindowController: NSObject, NSWindowDelegate {
    static let shared = WorkReportWindowController()
    private var window: NSWindow?

    func open(vm: UsageViewModel) {
        if let window, window.isVisible {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let hostingView = NSHostingView(rootView: WorkWorkspaceView(vm: vm))
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 720, height: 680),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Work Activity"
        window.backgroundColor = .windowBackgroundColor
        window.contentView = hostingView
        window.contentMinSize = NSSize(width: 700, height: 520)
        window.center()
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        self.window = window
    }

    func windowWillClose(_ notification: Notification) {
        window = nil
    }
}
