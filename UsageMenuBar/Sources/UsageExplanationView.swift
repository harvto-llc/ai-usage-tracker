import SwiftUI

struct UsageExplanationView: View {
    let explanation: UsageExplanation?
    let isLoading: Bool
    let error: String?
    var showsHeader = true

    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            if showsHeader {
                HStack(spacing: 6) {
                    Text("WHERE USAGE WENT")
                        .font(.system(size: 10, weight: .semibold, design: .rounded))
                        .foregroundStyle(.secondary)
                    Spacer()
                    if isLoading {
                        ProgressView().controlSize(.mini)
                    } else if let totals = explanation?.totals, totals.totalTokens > 0 {
                        Text(totalLabel(totals))
                            .font(.system(size: 10, design: .rounded))
                            .foregroundStyle(.secondary)
                    }
                }
            }

            if let explanation, !explanation.categories.isEmpty {
                UsageBreakdownBar(categories: explanation.categories)

                ForEach(displayedCategories(explanation.categories)) { category in
                    categoryRow(category, provider: explanation.provider)
                }

                if explanation.categories.count > 4 {
                    Button {
                        expanded.toggle()
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: expanded ? "chevron.up" : "chevron.down")
                            Text(expanded ? "Show top drivers" : "Show all categories")
                        }
                        .font(.system(size: 10, design: .rounded))
                        .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .help(expanded ? "Collapse usage categories" : "Expand usage categories")
                }

                coverageLine(explanation)
            } else if let error, !error.isEmpty {
                Label("Breakdown unavailable", systemImage: "exclamationmark.triangle")
                    .font(.system(size: 10, design: .rounded))
                    .foregroundStyle(.orange)
                    .help(error)
            } else if !isLoading {
                Text("No local token activity in this period")
                    .font(.system(size: 10, design: .rounded))
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 2)
    }

    private func displayedCategories(_ categories: [UsageExplanationCategory]) -> [UsageExplanationCategory] {
        expanded ? categories : Array(categories.prefix(4))
    }

    private func categoryRow(_ category: UsageExplanationCategory, provider: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Circle()
                    .fill(color(category.color))
                    .frame(width: 7, height: 7)
                Text(category.label)
                    .lineLimit(1)
                Spacer()
                Text(String(format: "%.0f%%", category.sharePct))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                Text(categoryValue(category, provider: provider))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                    .frame(minWidth: 48, alignment: .trailing)
            }
            HStack(spacing: 5) {
                Text("\(formatTokens(category.totalTokens)) raw")
                if category.cacheReadTokens > 0 {
                    Text("\(formatTokens(category.cacheReadTokens)) cached")
                }
                if category.eventCount > 0 {
                    Text("\(category.eventCount) events")
                }
                Text(category.confidence)
            }
            .font(.system(size: 9, design: .rounded))
            .foregroundStyle(.tertiary)
            .padding(.leading, 13)
        }
        .font(.system(size: 10, design: .rounded))
    }

    private func coverageLine(_ explanation: UsageExplanation) -> some View {
        HStack(spacing: 4) {
            Image(systemName: explanation.coverage.status == "compacted" ? "arrow.triangle.2.circlepath" : "checkmark.circle")
            Text("Local estimate")
            if let classified = explanation.coverage.classifiedPct {
                Text("· \(classified, specifier: "%.0f")% classified")
            }
            if explanation.coverage.compactions > 0 {
                Text("· \(explanation.coverage.compactions) compacted")
            }
        }
        .font(.system(size: 9, design: .rounded))
        .foregroundStyle(.tertiary)
        .help("Transcript attribution is separate from provider quota usage")
    }

    private func totalLabel(_ totals: UsageExplanationTotals) -> String {
        var parts = ["\(formatTokens(Int(totals.effectiveTokens.rounded()))) effective"]
        if let cost = totals.estimatedCostUSD {
            parts.append(formatCost(cost))
        }
        return parts.joined(separator: " · ")
    }

    private func categoryValue(_ category: UsageExplanationCategory, provider: String) -> String {
        if provider == "codex", let credits = category.estimatedCredits {
            return credits < 0.1 ? String(format: "%.2f cr", credits) : String(format: "%.1f cr", credits)
        }
        if let cost = category.estimatedCostUSD {
            return formatCost(cost)
        }
        return formatTokens(Int(category.effectiveTokens.rounded()))
    }

    private func formatTokens(_ value: Int) -> String {
        if value >= 1_000_000_000 { return String(format: "%.1fB", Double(value) / 1_000_000_000) }
        if value >= 1_000_000 { return String(format: "%.1fM", Double(value) / 1_000_000) }
        if value >= 1_000 { return String(format: "%.1fk", Double(value) / 1_000) }
        return "\(value)"
    }

    private func formatCost(_ value: Double) -> String {
        value < 0.01 ? String(format: "$%.3f", value) : String(format: "$%.2f", value)
    }

    private func color(_ hex: String) -> Color {
        guard hex.count == 7, let value = Int(hex.dropFirst(), radix: 16) else { return .secondary }
        return Color(
            red: Double((value >> 16) & 0xFF) / 255,
            green: Double((value >> 8) & 0xFF) / 255,
            blue: Double(value & 0xFF) / 255
        )
    }
}

private struct UsageBreakdownBar: View {
    let categories: [UsageExplanationCategory]

    var body: some View {
        GeometryReader { geometry in
            HStack(spacing: 0) {
                ForEach(categories.filter { $0.sharePct > 0 }) { category in
                    Rectangle()
                        .fill(color(category.color))
                        .frame(width: max(geometry.size.width * category.sharePct / 100, 1))
                        .help("\(category.label): \(category.sharePct, specifier: "%.1f")%")
                }
            }
        }
        .frame(height: 7)
        .clipShape(RoundedRectangle(cornerRadius: 3))
        .background(Color.primary.opacity(0.08), in: RoundedRectangle(cornerRadius: 3))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Usage category breakdown")
        .accessibilityValue(
            categories.prefix(4).map { "\($0.label) \(Int($0.sharePct.rounded())) percent" }.joined(separator: ", ")
        )
    }

    private func color(_ hex: String) -> Color {
        guard hex.count == 7, let value = Int(hex.dropFirst(), radix: 16) else { return .secondary }
        return Color(
            red: Double((value >> 16) & 0xFF) / 255,
            green: Double((value >> 8) & 0xFF) / 255,
            blue: Double(value & 0xFF) / 255
        )
    }
}
