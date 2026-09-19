import SwiftUI

@main
struct UsageMenuBarApp: App {
    @NSApplicationDelegateAdaptor(UsageMenuBarApplicationDelegate.self)
    private var appDelegate

    init() {
        // AICUR_SMOKE=1 is the headless CI smoke test: no browser-cookie reads (they can
        // raise Keychain prompts nobody can answer on a CI runner).
        if ProcessInfo.processInfo.environment["AICUR_SMOKE"] != "1" {
            Sentinel.shared.start()
        }
    }

    var body: some Scene {
        Settings {
            EmptyView()
        }
    }
}

// Menu bar icon mode — toggle between designs
enum MenuBarIconMode: String, CaseIterable, Identifiable {
    case reactor = "Reactor Irises"
    case eclipse = "Eclipse Orbitals"
    case thermometer = "Thermometer"
    case grid = "Breathing Grid"
    var id: String { rawValue }
}

struct MenuBarLabel: View {
    @ObservedObject var vm: UsageViewModel

    var body: some View {
        // Force SwiftUI to re-evaluate when stats change
        let _ = vm.stats?.claudeQuota?.sessionUsedPct
        let _ = vm.stats?.claudeQuota?.weeklyUsedPct
        let _ = vm.stats?.codexQuota?.sessionUsedPct
        let _ = vm.stats?.codexQuota?.limits?.count
        let _ = vm.isOffline

        if let img = renderIcon() {
            Image(nsImage: img)
        } else {
            Text("⏣")
        }
    }

    func renderIcon() -> NSImage? {
        guard let s = vm.stats, !vm.isOffline else { return nil }

        let claudeSession = vm.visibleBars.claudeSession ? (s.claudeQuota?.sessionUsedPct.map { $0 / 100 } ?? 0) : 0
        let claudeWeekly = vm.visibleBars.claudeWeekly ? s.claudeQuota?.weeklyUsedPct.map { $0 / 100 } : nil
        let codexSession = vm.visibleBars.codexSession ? (s.codexQuota?.sessionUsedPct.map { $0 / 100 } ?? 0) : 0
        let codexWeekly = vm.visibleBars.codexWeekly ? s.codexQuota?.weeklyUsedPct.map { $0 / 100 } : nil

        let content: AnyView
        switch vm.iconMode {
        case .reactor:
            content = AnyView(ReactorIrisIcon(cS: claudeSession, cW: claudeWeekly, xS: codexSession, xW: codexWeekly))
        case .eclipse:
            content = AnyView(EclipseOrbitalIcon(cS: claudeSession, cW: claudeWeekly, xS: codexSession, xW: codexWeekly))
        case .thermometer:
            content = AnyView(
                ThermometerIcon(
                    claudeSession: claudeSession, claudeWeekly: claudeWeekly,
                    codexSession: codexSession, codexWeekly: codexWeekly
                )
                .background(Color.clear)
            )
        case .grid:
            content = AnyView(BreathingGridIcon(
                claudeSession: claudeSession, claudeWeekly: claudeWeekly,
                codexSession: codexSession, codexWeekly: codexWeekly
            ))
        }

        let renderer = ImageRenderer(content: content)
        renderer.scale = NSScreen.main?.backingScaleFactor ?? 2.0
        guard let cgImage = renderer.cgImage else { return nil }
        let nsImage = NSImage(cgImage: cgImage, size: NSSize(
            width: cgImage.width / Int(renderer.scale),
            height: cgImage.height / Int(renderer.scale)
        ))
        nsImage.isTemplate = false
        return nsImage
    }
}

// MARK: — Shared color logic

private func usageColor(_ pct: Double, dark: Bool = true) -> Color {
    if pct >= 0.85 {
        return dark ? Color(red: 1, green: 0.40, blue: 0.36)   // #FF655C
                    : Color(red: 0.71, green: 0.19, blue: 0.17) // #B6312B
    }
    if pct >= 0.60 {
        return dark ? Color(red: 1, green: 0.70, blue: 0.30)   // #FFB24C
                    : Color(red: 0.66, green: 0.39, blue: 0)    // #A86400
    }
    return dark ? Color(red: 0.54, green: 0.89, blue: 0.82)    // #89E2D0
                : Color(red: 0.07, green: 0.49, blue: 0.45)    // #117D72
}

// MARK: — Twin Reactor Irises

struct ReactorIrisIcon: View {
    let cS: Double, cW: Double?, xS: Double, xW: Double?  // 0...1

    var body: some View {
        Canvas { context, size in
            let centers: [(x: CGFloat, session: Double, weekly: Double?)] = [
                (7.5, cS, cW),
                (20.5, xS, xW),
            ]
            let cy = size.height / 2

            for c in centers {
                let session = min(c.session, 1.0)
                let weekly = c.weekly.map { min($0, 1.0) }

                if let weekly {
                    // Outer shell: circle → rounded hexagon as weekly increases
                    let outerR: CGFloat = 5.0
                    let shellWidth: CGFloat = 1.1 + CGFloat(weekly) * 1.5  // 1.1 ... 2.6
                    let sides = 6
                    let cornerFraction = 1.0 - weekly  // 1.0 = full circle, 0.0 = hex
                    let shellColor = Color.white.opacity(0.45 + weekly * 0.15)

                    let shellPath = roundedPolygonPath(
                        center: CGPoint(x: c.x, y: cy),
                        radius: outerR,
                        sides: sides,
                        cornerRadius: outerR * cornerFraction * 0.85
                    )
                    context.stroke(shellPath, with: .color(shellColor), lineWidth: shellWidth)

                    let highlightArc = Path { p in
                        p.addArc(center: CGPoint(x: c.x, y: cy),
                                 radius: outerR - shellWidth / 2,
                                 startAngle: .degrees(-160),
                                 endAngle: .degrees(-100),
                                 clockwise: false)
                    }
                    context.stroke(highlightArc, with: .color(.white.opacity(0.18)), lineWidth: 0.8)
                }

                // Inner core
                let coreR: CGFloat = 1.2 + CGFloat(session) * 2.9  // 1.2 ... 4.1
                let coreColor = usageColor(session)
                let corePath = Path(ellipseIn: CGRect(
                    x: c.x - coreR, y: cy - coreR,
                    width: coreR * 2, height: coreR * 2
                ))
                context.fill(corePath, with: .color(coreColor.opacity(0.7 + session * 0.3)))

                // Center notch (cooling vent) — tiny dark circle
                let notchR: CGFloat = max(0.6, coreR * 0.2)
                let notch = Path(ellipseIn: CGRect(
                    x: c.x - notchR, y: cy - notchR,
                    width: notchR * 2, height: notchR * 2
                ))
                context.fill(notch, with: .color(Color(red: 0.04, green: 0.06, blue: 0.09)))
            }
        }
        .frame(width: 28, height: 22)
    }
}

// Helper: rounded polygon path
func roundedPolygonPath(center: CGPoint, radius: CGFloat, sides: Int, cornerRadius: CGFloat) -> Path {
    Path { path in
        let angleStep = (2 * .pi) / Double(sides)
        // If cornerRadius is large enough, just draw a circle
        if cornerRadius >= radius * 0.8 {
            path.addEllipse(in: CGRect(x: center.x - radius, y: center.y - radius,
                                        width: radius * 2, height: radius * 2))
            return
        }

        for i in 0..<sides {
            let angle1 = Double(i) * angleStep - .pi / 2
            let angle2 = Double(i + 1) * angleStep - .pi / 2

            let p1 = CGPoint(x: center.x + radius * cos(angle1),
                             y: center.y + radius * sin(angle1))
            let p2 = CGPoint(x: center.x + radius * cos(angle2),
                             y: center.y + radius * sin(angle2))

            let sideLen = hypot(p2.x - p1.x, p2.y - p1.y)
            let clampedR = min(cornerRadius, sideLen / 2.5)
            let t = clampedR / sideLen

            let start = CGPoint(x: p1.x + (p2.x - p1.x) * t, y: p1.y + (p2.y - p1.y) * t)
            let end = CGPoint(x: p2.x - (p2.x - p1.x) * t, y: p2.y - (p2.y - p1.y) * t)

            if i == 0 {
                path.move(to: start)
            } else {
                path.addLine(to: start)
            }
            path.addLine(to: end)
            // Round the corner at p2
            let angle3 = Double(i + 2) * angleStep - .pi / 2
            let p3 = CGPoint(x: center.x + radius * cos(angle3),
                             y: center.y + radius * sin(angle3))
            let nextStart = CGPoint(x: p2.x + (p3.x - p2.x) * t, y: p2.y + (p3.y - p2.y) * t)
            path.addQuadCurve(to: nextStart, control: p2)
        }
        path.closeSubpath()
    }
}

// MARK: — Eclipse Orbitals

struct EclipseOrbitalIcon: View {
    let cS: Double, cW: Double?, xS: Double, xW: Double?  // 0...1

    var body: some View {
        Canvas { context, size in
            let centers: [(x: CGFloat, session: Double, weekly: Double?)] = [
                (7.0, cS, cW),
                (21.0, xS, xW),
            ]
            let cy = size.height / 2

            for c in centers {
                let session = min(c.session, 1.0)
                let weekly = c.weekly.map { min($0, 1.0) }
                let center = CGPoint(x: c.x, y: cy)

                let discR: CGFloat = 4.8
                let discColor = usageColor(session)

                // Draw bright disc
                let disc = Path(ellipseIn: CGRect(
                    x: center.x - discR, y: center.y - discR,
                    width: discR * 2, height: discR * 2
                ))
                context.fill(disc, with: .color(discColor.opacity(0.85)))

                // Occluder (dark moon) carves the eclipse
                let occR: CGFloat = 1.7 + CGFloat(session) * 3.0  // 1.7 ... 4.7
                let offsetX: CGFloat = 1.8 - CGFloat(session) * 1.4  // 1.8 → 0.4
                let offsetY: CGFloat = 2.0 - CGFloat(session) * 2.0  // 2.0 → 0.0
                let occCenter = CGPoint(x: center.x + offsetX, y: center.y - offsetY)
                let occluder = Path(ellipseIn: CGRect(
                    x: occCenter.x - occR, y: occCenter.y - occR,
                    width: occR * 2, height: occR * 2
                ))
                // Dark occluder
                context.fill(occluder, with: .color(Color(red: 0.04, green: 0.05, blue: 0.07)))

                // Orbit ring
                if let weekly {
                    let orbitW: CGFloat = 10.5
                    let orbitH: CGFloat = 7.0
                    let orbitStroke: CGFloat = 1.0 + CGFloat(weekly) * 1.3  // 1.0 ... 2.3
                    let tiltDeg: Double = 15 + weekly * 20  // 15° ... 35°
                    let orbitColor = Color.white.opacity(0.4 + weekly * 0.25)

                    let tilt = c.x < 14 ? -tiltDeg : tiltDeg

                    context.drawLayer { ctx in
                        let orbitRect = CGRect(
                            x: center.x - orbitW / 2,
                            y: center.y - orbitH / 2,
                            width: orbitW,
                            height: orbitH
                        )
                        var orbitPath = Path(ellipseIn: orbitRect)
                        let transform = CGAffineTransform(translationX: center.x, y: center.y)
                            .rotated(by: tilt * .pi / 180)
                            .translatedBy(x: -center.x, y: -center.y)
                        orbitPath = orbitPath.applying(transform)
                        ctx.stroke(orbitPath, with: .color(orbitColor), lineWidth: orbitStroke)
                    }
                }

                // Crescent highlight — thin bright edge opposite the occluder
                let highlightArc = Path { p in
                    p.addArc(center: center, radius: discR - 0.5,
                             startAngle: .degrees(160), endAngle: .degrees(260), clockwise: false)
                }
                context.stroke(highlightArc, with: .color(.white.opacity(0.2 + session * 0.15)),
                               lineWidth: 0.7)
            }
        }
        .frame(width: 28, height: 22)
    }
}

// MARK: — Dual Thermometer Icon

struct ThermometerIcon: View {
    let claudeSession: Double  // 0...1
    let claudeWeekly: Double?
    let codexSession: Double
    let codexWeekly: Double?

    private let pillW: CGFloat = 7
    private let pillH: CGFloat = 18
    private let gap: CGFloat = 5

    var body: some View {
        Canvas { context, size in
            let totalW = pillW * 2 + gap
            let baseX = (size.width - totalW) / 2
            let baseY = (size.height - pillH) / 2

            let pills: [(session: Double, weekly: Double?, x: CGFloat)] = [
                (claudeSession, claudeWeekly, baseX),
                (codexSession, codexWeekly, baseX + pillW + gap),
            ]

            for pill in pills {
                let rect = CGRect(x: pill.x, y: baseY, width: pillW, height: pillH)
                let cornerR = pillW / 2

                // Track background — always visible
                let track = Path(roundedRect: rect, cornerRadius: cornerR)
                context.fill(track, with: .color(Color.white.opacity(0.18)))
                context.stroke(track, with: .color(Color.white.opacity(0.45)), lineWidth: 0.6)

                // Fill from BOTTOM up, growing with usage
                let used = min(max(pill.session, 0), 1.0)
                if used > 0 {
                    let rawFillH = pillH * used
                    // Keep low-but-real usage visually legible in the menu bar.
                    let fillH = ceil(max(rawFillH, minimumVisibleFillHeight(for: used)))
                    let fillY = floor(baseY + pillH - fillH)
                    // Draw as rounded rect at bottom of pill — bottom corners match pill, top straight
                    let fillRect = CGRect(x: pill.x, y: fillY, width: pillW, height: fillH)
                    let fillPath = Path { p in
                        let bottomR = min(cornerR, fillH / 2)
                        p.move(to: CGPoint(x: fillRect.minX, y: fillRect.minY))
                        p.addLine(to: CGPoint(x: fillRect.maxX, y: fillRect.minY))
                        p.addLine(to: CGPoint(x: fillRect.maxX, y: fillRect.maxY - bottomR))
                        p.addQuadCurve(
                            to: CGPoint(x: fillRect.maxX - bottomR, y: fillRect.maxY),
                            control: CGPoint(x: fillRect.maxX, y: fillRect.maxY)
                        )
                        p.addLine(to: CGPoint(x: fillRect.minX + bottomR, y: fillRect.maxY))
                        p.addQuadCurve(
                            to: CGPoint(x: fillRect.minX, y: fillRect.maxY - bottomR),
                            control: CGPoint(x: fillRect.minX, y: fillRect.maxY)
                        )
                        p.closeSubpath()
                    }
                    context.fill(fillPath, with: .color(usageColor(used)))
                }

                // Weekly tick mark — positioned from bottom by weekly used %
                let weeklyUsed = pill.weekly.map { min(max($0, 0), 1.0) }
                if let weeklyUsed, weeklyUsed > 0 {
                    let tickY = baseY + pillH - (pillH * weeklyUsed)
                    let tickPath = Path { p in
                        p.move(to: CGPoint(x: pill.x - 1.8, y: tickY))
                        p.addLine(to: CGPoint(x: pill.x + pillW + 1.8, y: tickY))
                    }
                    context.stroke(tickPath, with: .color(.white.opacity(0.9)), lineWidth: 1.5)
                }
            }
        }
        .frame(width: 28, height: 22)
    }

    private func minimumVisibleFillHeight(for used: Double) -> CGFloat {
        if used <= 0 { return 0 }
        if used < 0.08 { return 3.0 }
        if used < 0.20 { return 5.0 }
        return 0
    }
}

// MARK: — Breathing Grid Icon

struct BreathingGridIcon: View {
    let claudeSession: Double  // 0...1
    let claudeWeekly: Double?
    let codexSession: Double
    let codexWeekly: Double?

    var body: some View {
        Canvas { context, size in
            // 2x2 grid: [Claude Session, Codex Session] / [Claude Weekly, Codex Weekly]
            let values: [[Double?]] = [
                [claudeSession, codexSession],
                [claudeWeekly, codexWeekly],
            ]
            let centerX = size.width / 2
            let centerY = size.height / 2
            let spacing: CGFloat = 9  // distance between cell centers

            let minSize: CGFloat = 2.5
            let maxSize: CGFloat = 7.5

            for row in 0..<2 {
                for col in 0..<2 {
                    let val = values[row][col].map { min($0, 1.0) }
                    let fillValue = val ?? 0
                    let cellSize = minSize + fillValue * (maxSize - minSize)
                    let cornerR = (1 - fillValue) * (cellSize / 2)  // circle at 0%, square at 100%

                    let cx = centerX + CGFloat(col == 0 ? -1 : 1) * spacing / 2
                    let cy = centerY + CGFloat(row == 0 ? -1 : 1) * spacing / 2

                    let rect = CGRect(
                        x: cx - cellSize / 2,
                        y: cy - cellSize / 2,
                        width: cellSize,
                        height: cellSize
                    )

                    // Faint track
                    let trackRect = CGRect(x: cx - maxSize/2, y: cy - maxSize/2, width: maxSize, height: maxSize)
                    let trackShape = Path(roundedRect: trackRect, cornerRadius: maxSize * 0.15)
                    context.stroke(trackShape, with: .color(.white.opacity(0.08)), lineWidth: 0.5)

                    if let val {
                        let color = gridColorForPct(val)
                        let shape = Path(roundedRect: rect, cornerRadius: cornerR)
                        context.fill(shape, with: .color(color.opacity(0.4 + val * 0.6)))
                    }
                }
            }
        }
        .frame(width: 22, height: 22)
    }

    private func gridColorForPct(_ pct: Double) -> Color {
        if pct >= 0.85 { return Color(red: 1, green: 0.32, blue: 0.32) }    // red
        if pct >= 0.65 { return Color(red: 1, green: 0.72, blue: 0.28) }    // orange
        if pct >= 0.35 { return Color(red: 1, green: 0.84, blue: 0.31) }    // amber
        return Color(red: 0.5, green: 0.8, blue: 0.77)                       // teal-mint
    }
}

struct MenuContent: View {
    @ObservedObject var vm: UsageViewModel
    @State private var detailSectionExpansion: [String: Bool] = [:]

    var body: some View {
        VStack(spacing: 0) {
            if vm.isOffline {
                Label("API Offline", systemImage: "wifi.slash")
                    .foregroundStyle(.secondary)
                    .padding(20)
            } else if let s = vm.stats {

                // ── Header ──────────────────────────────
                HStack(spacing: 0) {
                    Text("Tool Usage")
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text(freshness(s.timestamp))
                        .foregroundStyle(.tertiary)
                }
                .font(.system(.caption2, design: .rounded).weight(.medium))
                .padding(.horizontal, 14).padding(.vertical, 8)

                WorkTrackingBar(vm: vm)
                Divider()

                Picker("Usage period", selection: $vm.selectedPeriod) {
                    ForEach(UsagePeriod.allCases) { period in
                        Text(period.label).tag(period)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .accessibilityLabel("Activity period")
                .controlSize(.small)
                .padding(.horizontal, 14)
                .padding(.bottom, 8)

                let providerCards = providerCards(from: s, period: vm.selectedPeriod)
                let selectedProviderID = providerCards.contains(where: { $0.id == vm.selectedTool })
                    ? vm.selectedTool
                    : (providerCards.first?.id ?? vm.selectedTool)
                let selectedCard = providerCards.first(where: { $0.id == selectedProviderID })

                // ── Tool rows ───────────────────────────
                VStack(spacing: 0) {
                    ForEach(Array(providerCards.enumerated()), id: \.element.id) { idx, card in
                        if idx > 0 {
                            Divider().padding(.leading, 44)
                        }
                        toolRow(
                            name: card.name,
                            selected: selectedProviderID == card.id,
                            sessionPct: card.primaryPct,
                            weeklyPct: card.secondaryPct,
                            amountUSD: card.gaugeAmountUSD,
                            amountIsEstimate: card.gaugeAmountIsEstimate,
                            amountLabel: card.gaugeAmountLabel,
                            gaugeLabel: card.gaugeLabel,
                            status: card.status,
                            reset: card.primaryReset,
                            plan: card.plan,
                            summary: card.summary,
                            onTap: { vm.selectedTool = card.id }
                        )
                    }
                }

                // ── Risk outlook (warnings always, pacing only for Claude) ──
                if let outlook = s.riskOutlook,
                   outlook.hasPrefix("⚠") || selectedProviderID == "claude" {
                    Text(outlook)
                        .font(.system(.caption2, design: .rounded))
                        .foregroundStyle(outlook.hasPrefix("⚠") ? Color.orange : Color.gray)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 14).padding(.vertical, 6)
                }

                Divider()

                // ── Focused detail ──────────────────────
                VStack(alignment: .leading, spacing: 0) {
                    if let card = selectedCard {
                        let quotaSections = sections(in: card, named: ["Quota"])
                        let activitySections = sections(in: card, named: ["Activity", "Models", "Details"])
                        let costSections = sections(in: card, named: ["Cost", "Account"])
                        let sourceSections = sections(in: card, named: ["Sources"])

                        if !quotaSections.isEmpty {
                            CollapsibleDetailSection(
                                title: "Quota",
                                systemImage: "speedometer",
                                summary: sectionSummary(quotaSections),
                                isExpanded: detailSectionBinding(
                                    providerID: selectedProviderID,
                                    period: vm.selectedPeriod,
                                    section: "quota",
                                    defaultExpanded: true
                                )
                            ) {
                                providerSectionContent(quotaSections)
                            }
                        }

                        if selectedProviderID == "claude" || selectedProviderID == "codex" {
                            let explanationKey = "\(selectedProviderID):\(vm.selectedPeriod.rawValue)"
                            let explanation = vm.usageExplanation(
                                provider: selectedProviderID,
                                period: vm.selectedPeriod
                            )
                            CollapsibleDetailSection(
                                title: "Usage Drivers",
                                systemImage: "chart.bar.xaxis",
                                summary: usageExplanationSummary(explanation),
                                isExpanded: detailSectionBinding(
                                    providerID: selectedProviderID,
                                    period: vm.selectedPeriod,
                                    section: "usage-drivers"
                                )
                            ) {
                                UsageExplanationView(
                                    explanation: explanation,
                                    isLoading: vm.usageExplanationLoading.contains(explanationKey),
                                    error: vm.usageExplanationErrors[explanationKey],
                                    showsHeader: false
                                )
                            }
                        }

                        if !activitySections.isEmpty {
                            CollapsibleDetailSection(
                                title: "Activity & Models",
                                systemImage: "bolt",
                                summary: sectionSummary(activitySections),
                                isExpanded: detailSectionBinding(
                                    providerID: selectedProviderID,
                                    period: vm.selectedPeriod,
                                    section: "activity-models"
                                )
                            ) {
                                providerSectionContent(activitySections)
                            }
                        }

                        if !costSections.isEmpty {
                            CollapsibleDetailSection(
                                title: "Cost & Account",
                                systemImage: "dollarsign.circle",
                                summary: sectionSummary(costSections),
                                isExpanded: detailSectionBinding(
                                    providerID: selectedProviderID,
                                    period: vm.selectedPeriod,
                                    section: "cost-account"
                                )
                            ) {
                                providerSectionContent(costSections)
                            }
                        }

                        let trend = s.providerTrends?[selectedProviderID]
                        if !sourceSections.isEmpty || trend != nil {
                            CollapsibleDetailSection(
                                title: "Trend & Sources",
                                systemImage: "waveform.path.ecg",
                                summary: sectionSummary(sourceSections),
                                isExpanded: detailSectionBinding(
                                    providerID: selectedProviderID,
                                    period: vm.selectedPeriod,
                                    section: "trend-sources"
                                )
                            ) {
                                providerSectionContent(sourceSections)
                                if let trend {
                                    ProviderTrendView(trend: trend)
                                }
                            }
                        }
                    } else {
                        detailLine("Status", val: "No provider data yet")
                    }
                }
                .font(.system(.caption, design: .rounded))
                .padding(.horizontal, 14).padding(.vertical, 8)
                .task(id: "\(selectedProviderID):\(vm.selectedPeriod.rawValue):\(s.timestamp ?? 0)") {
                    guard selectedProviderID == "claude" || selectedProviderID == "codex" else { return }
                    await vm.fetchUsageExplanation(
                        provider: selectedProviderID,
                        period: vm.selectedPeriod,
                        startAt: statsWindowStart(
                            stats: s,
                            providerID: selectedProviderID,
                            period: vm.selectedPeriod
                        )
                    )
                }

                Divider()

                // ── Footer ──────────────────────────────
                HStack(spacing: 8) {
                    Button(action: { vm.fetch() }) {
                        Image(systemName: "arrow.clockwise").font(.caption)
                    }
                    .keyboardShortcut("r")
                    .help("Refresh usage")
                    .accessibilityLabel("Refresh usage")

                    Button(action: { SettingsWindowController.shared.open(vm: vm) }) {
                        Image(systemName: "gearshape").font(.caption)
                    }
                    .buttonStyle(.plain).foregroundStyle(.secondary)
                    .keyboardShortcut(",")
                    .help("Open settings")
                    .accessibilityLabel("Open settings")

                    linkIcon("c.circle", "https://claude.ai/settings/usage", label: "Open Claude usage")
                    linkIcon(
                        "x.circle",
                        "https://chatgpt.com/codex/cloud/settings/analytics",
                        label: "Open Codex analytics"
                    )
                    if providerCards.contains(where: { $0.id == "cursor" }) {
                        linkIcon(
                            "cursorarrow.click",
                            "https://cursor.com/dashboard/usage",
                            label: "Open Cursor usage"
                        )
                    }

                    Spacer()

                    if let streak = s.streak, streak > 0 {
                        Text("🔥 \(streak)d").font(.caption2).foregroundStyle(.tertiary)
                    }

                    Button("Quit") { NSApplication.shared.terminate(nil) }
                        .font(.caption2).foregroundStyle(.tertiary)
                        .keyboardShortcut("q")
                }
                .padding(.horizontal, 14).padding(.vertical, 8)
            }
        }
        .frame(width: 340)
    }

    private struct ProviderCard {
        let id: String
        let name: String
        let plan: String?
        let status: String
        let primaryPct: Double?
        let secondaryPct: Double?
        let primaryReset: String?
        let gaugeAmountUSD: Double?
        let gaugeAmountIsEstimate: Bool
        let gaugeAmountLabel: String
        let gaugeLabel: String
        let summary: String
        let sections: [ProviderSection]
    }

    private struct ProviderSection {
        let title: String
        let lines: [ProviderLine]
    }

    private struct ProviderLine {
        let label: String
        let value: String
    }

    private func statsWindowStart(
        stats: UsageStats,
        providerID: String,
        period: UsagePeriod
    ) -> String? {
        stats.providerPeriods?[providerID]?.metrics(for: period)?.windowStartAt
    }

    private func sections(in card: ProviderCard, named names: Set<String>) -> [ProviderSection] {
        card.sections.filter { names.contains($0.title) && !$0.lines.isEmpty }
    }

    private func detailSectionBinding(
        providerID: String,
        period: UsagePeriod,
        section: String,
        defaultExpanded: Bool = false
    ) -> Binding<Bool> {
        let key = "\(providerID):\(period.rawValue):\(section)"
        return Binding(
            get: { detailSectionExpansion[key] ?? defaultExpanded },
            set: { detailSectionExpansion[key] = $0 }
        )
    }

    @ViewBuilder
    private func providerSectionContent(_ sections: [ProviderSection]) -> some View {
        ForEach(Array(sections.enumerated()), id: \.offset) { index, section in
            if sections.count > 1 {
                SectionHeader(title: section.title)
                    .padding(.top, index == 0 ? 0 : 4)
            }
            ForEach(Array(section.lines.enumerated()), id: \.offset) { _, line in
                detailLine(line.label, val: line.value)
            }
        }
    }

    private func sectionSummary(_ sections: [ProviderSection]) -> String? {
        guard let line = sections.lazy.flatMap(\.lines).first else { return nil }
        return "\(line.label) \(line.value)"
    }

    private func usageExplanationSummary(_ explanation: UsageExplanation?) -> String? {
        guard let totals = explanation?.totals, totals.totalTokens > 0 else { return nil }
        var parts = ["\(formatTokens(Int(totals.effectiveTokens.rounded()))) effective"]
        if let cost = totals.estimatedCostUSD {
            parts.append(cost < 0.01 ? String(format: "$%.3f", cost) : String(format: "$%.2f", cost))
        }
        return parts.joined(separator: " · ")
    }

    private func providerCards(from stats: UsageStats, period: UsagePeriod) -> [ProviderCard] {
        let defaultRegistry = coreProviderRegistry()
        let registrySource = stats.providerRegistry.flatMap { $0.isEmpty ? nil : $0 } ?? defaultRegistry
        let registry = registrySource
            .sorted {
                let lhsOrder = $0.order ?? Int.max
                let rhsOrder = $1.order ?? Int.max
                if lhsOrder == rhsOrder {
                    return $0.id < $1.id
                }
                return lhsOrder < rhsOrder
            }

        var cards: [ProviderCard] = []
        var seen = Set<String>()

        for entry in registry {
            seen.insert(entry.id)
            if let card = providerCard(for: entry.id, label: providerLabel(for: entry.id, fallback: entry.label), stats: stats, period: period) {
                cards.append(card)
            }
        }

        for entry in defaultRegistry where !seen.contains(entry.id) {
            if let card = providerCard(for: entry.id, label: providerLabel(for: entry.id, fallback: entry.label), stats: stats, period: period) {
                cards.append(card)
            }
        }

        if let latest = stats.providersLatest {
            for providerID in latest.keys.sorted() where !seen.contains(providerID) {
                if let card = providerCard(for: providerID, label: providerLabel(for: providerID, fallback: nil), stats: stats, period: period) {
                    cards.append(card)
                }
            }
        }

        return cards
    }

    private func providerCard(
        for providerID: String,
        label: String,
        stats: UsageStats,
        period: UsagePeriod
    ) -> ProviderCard? {
        if providerID == "cursor", stats.cursor?.hasDisplayData != true {
            return nil
        }
        let snapshot = stats.providersLatest?[providerID]
        let shared = snapshot?.shared
        let periodMetrics = stats.providerPeriods?[providerID]?.metrics(for: period)

        var primaryUsed = shared?.primaryUsedPct
        var primaryRemaining = shared?.primaryRemainingPct
        var primaryReset = shared?.primaryReset
        var secondaryUsed = shared?.secondaryUsedPct
        var secondaryRemaining = shared?.secondaryRemainingPct
        var secondaryReset = shared?.secondaryReset
        // Labels for the primary/secondary slots. Default to Claude/Codex semantics
        // (session + weekly buckets); providers whose slots mean something else
        // override these in their switch case below.
        var primaryLabel = "Session"
        let secondaryLabel = "Weekly"
        var tokensDay = periodMetrics?.totalTokens.map(Double.init)
        var messagesDay = periodMetrics?.messages.map(Double.init)
        var activeHoursDay = periodMetrics?.activeHours
        if period == .day {
            tokensDay = tokensDay ?? shared?.tokensTotalDay
            messagesDay = messagesDay ?? shared?.messagesTotalDay
            activeHoursDay = activeHoursDay ?? shared?.activeHoursDay
        }
        var plan = normalizedText(snapshot?.plan ?? snapshot?.unique?["plan"]?.stringValue)
        var status = normalizedStatus(snapshot?.status)

        var uniqueLines = coreProviderIDs.contains(providerID) ? [] : uniqueLines(from: snapshot?.unique)
        var modelLines: [ProviderLine] = []
        var quotaLines: [ProviderLine] = []
        var creditLines: [ProviderLine] = []
        var gaugeAmountUSD: Double?
        var gaugeAmountIsEstimate = true
        var gaugeAmountLabel = "\(period.label) activity value"
        var claudePaidUsage: ClaudePaidUsagePresentation?
        var codexPaidCreditUsage: CodexPaidCreditPresentation?

        func appendUnique(_ label: String, _ value: String?) {
            guard let value = normalizedText(value) else { return }
            guard !uniqueLines.contains(where: { $0.label.caseInsensitiveCompare(label) == .orderedSame }) else { return }
            uniqueLines.append(ProviderLine(label: label, value: value))
        }

        switch providerID {
        case "claude":
            primaryUsed = primaryUsed ?? stats.claudeQuota?.sessionUsedPct
            primaryRemaining = primaryRemaining ?? stats.claudeQuota?.sessionRemainingPct
            primaryReset = primaryReset ?? stats.claudeQuota?.sessionReset
            secondaryUsed = secondaryUsed ?? stats.claudeQuota?.weeklyUsedPct
            secondaryRemaining = secondaryRemaining ?? stats.claudeQuota?.weeklyRemainingPct
            secondaryReset = secondaryReset ?? stats.claudeQuota?.weeklyReset
            if period == .day {
                primaryLabel = quotaWindowLabel(stats.claudeQuota?.limits, windowKind: "session")
                    ?? "Session quota"
                activeHoursDay = activeHoursDay ?? stats.claudeToday?.activeHoursToday
                messagesDay = messagesDay ?? stats.claudeToday?.messagesToday.map(Double.init)
                tokensDay = tokensDay ?? stats.claudeToday.map {
                    Double(($0.inputTokensToday ?? 0) + ($0.outputTokensToday ?? 0))
                }
                secondaryUsed = nil
                secondaryRemaining = nil
                secondaryReset = nil
            } else {
                primaryUsed = secondaryUsed
                primaryRemaining = secondaryRemaining
                primaryReset = secondaryReset
                primaryLabel = quotaWindowLabel(stats.claudeQuota?.limits, windowKind: "weekly")
                    ?? "Weekly quota"
                secondaryUsed = nil
                secondaryRemaining = nil
                secondaryReset = nil
            }
            plan = stats.claudeQuota?.planLabel ?? plan
            status = status ?? toolStatus(session: primaryUsed ?? 0, burn: stats.burn)
            modelLines = modelDetailLines(
                periodMetrics?.models ?? (period == .day ? stats.claudeToday?.modelsToday : nil)
            )
            quotaLines = quotaDetailLines(
                stats.claudeQuota?.limits,
                windowKinds: period == .day ? ["session"] : ["weekly", "monthly", "account"]
            )
            gaugeAmountUSD = periodMetrics?.estimatedCostUSD
            creditLines = creditDetailLines(stats.claudeQuota?.creditPools)
            claudePaidUsage = claudePaidUsagePresentation(
                sessionUsedPct: stats.claudeQuota?.sessionUsedPct,
                limits: stats.claudeQuota?.limits,
                creditPools: stats.claudeQuota?.creditPools
            )

            if period == .day {
                appendUnique("Burn", stats.burn.map { String(format: "%.1f%%/hr · %@", $0, stats.workload ?? "") })
                appendUnique(
                    "Value",
                    stats.outputDensity.map { "\(formatTokens(Int($0)))/hr · Cache \(String(format: "%.0f%%", stats.cacheHealthPct ?? 0))" }
                )
            } else if let pace = stats.weeklyPace {
                let paceLabel = pace.paceStatus == "front_loaded" ? "front-loaded"
                    : pace.paceStatus == "under" ? "under pace" : "on track"
                appendUnique(
                    "Week Pace",
                    String(format: "%.0f%% projected · %.1fd left · %@", pace.projectedPct, pace.daysRemaining ?? 0, paceLabel)
                )
            }
            // 4-state gap rollups: focus/attention/off-hours/agent-runtime for today.
            // Sourced from /stats.gap_rollups (parity with /analytics/sessions).
            let gaps = period == .day ? stats.gapRollups?.today : stats.gapRollups?.last7d
            if let gaps {
                appendUnique("Focus", formatGapDuration(gaps.focusGapSec))
                appendUnique("Attention Idle", formatGapDuration(gaps.attentionIdleSec))
                appendUnique("Agent Runtime", formatGapDuration(gaps.agentRuntimeSec))
                appendUnique("Off-Hours", formatGapDuration(gaps.offHoursAwaySec))
            }
        case "codex":
            primaryUsed = primaryUsed ?? stats.codexQuota?.sessionUsedPct
            primaryRemaining = primaryRemaining ?? stats.codexQuota?.sessionRemainingPct
            primaryReset = primaryReset ?? stats.codexQuota?.sessionReset
            secondaryUsed = secondaryUsed ?? stats.codexQuota?.weeklyUsedPct
            secondaryRemaining = secondaryRemaining ?? stats.codexQuota?.weeklyRemainingPct
            secondaryReset = secondaryReset ?? stats.codexQuota?.weeklyReset
            if period == .day {
                primaryLabel = quotaWindowLabel(stats.codexQuota?.limits, windowKind: "session")
                    ?? "Session quota"
                activeHoursDay = activeHoursDay ?? stats.codexToday?.activeHoursToday
                messagesDay = messagesDay ?? stats.codexToday?.messagesToday.map(Double.init)
                tokensDay = tokensDay ?? stats.codexToday.map {
                    Double(($0.inputTokensToday ?? 0) + ($0.outputTokensToday ?? 0))
                }
                secondaryUsed = nil
                secondaryRemaining = nil
                secondaryReset = nil
            } else {
                primaryUsed = secondaryUsed ?? stats.codexQuota?.accountUsedPct
                primaryRemaining = secondaryRemaining ?? stats.codexQuota?.accountRemainingPct
                primaryReset = secondaryReset ?? stats.codexQuota?.accountReset
                primaryLabel = secondaryUsed != nil
                    ? (quotaWindowLabel(stats.codexQuota?.limits, windowKind: "weekly") ?? "Weekly quota")
                    : (quotaWindowLabel(stats.codexQuota?.limits, windowKind: "account")
                        ?? quotaWindowLabel(stats.codexQuota?.limits, windowKind: "monthly")
                        ?? "Account quota")
                secondaryUsed = nil
                secondaryRemaining = nil
                secondaryReset = nil
            }
            plan = stats.codexQuota?.planLabel ?? plan
            status = status ?? toolStatus(
                session: primaryUsed ?? secondaryUsed ?? stats.codexQuota?.codeReviewUsedPct ?? 0,
                burn: stats.codexBurn
            )
            modelLines = modelDetailLines(
                periodMetrics?.models ?? (period == .day ? stats.codexToday?.modelsToday : nil)
            )
            quotaLines = quotaDetailLines(
                stats.codexQuota?.limits,
                windowKinds: period == .day ? ["session"] : ["weekly", "monthly", "account"]
            )
            if period == .day, primaryUsed == nil, quotaLines.isEmpty {
                let hasSessionLimit = stats.codexQuota?.limits?.contains {
                    $0.windowKind == "session"
                } == true
                quotaLines = [ProviderLine(
                    label: "Plan limit",
                    value: hasSessionLimit ? "Temporarily unavailable" : "Quota Week only"
                )]
            }
            gaugeAmountUSD = periodMetrics?.estimatedCostUSD
            let resetCreditsUsed = period == .day
                ? stats.codexQuota?.rateLimitResetCreditsUsedDay
                : stats.codexQuota?.rateLimitResetCreditsUsedWeek
            let resetCreditsGranted = period == .day
                ? stats.codexQuota?.rateLimitResetCreditsGrantedDay
                : stats.codexQuota?.rateLimitResetCreditsGrantedWeek
            creditLines = creditDetailLines(
                stats.codexQuota?.creditPools,
                resetCreditsUsed: resetCreditsUsed,
                resetCreditsGranted: resetCreditsGranted
            )
            if let creditsUsed = stats.codexAnalyticsSummary?.creditsUsed {
                let days = stats.codexAnalyticsSummary?.creditsWindowDays
                let suffix = days.map { " · \($0)d" } ?? ""
                let dollars = stats.codexAnalyticsSummary?.creditsUsedUSD
                    .map {
                        let prefix = stats.codexAnalyticsSummary?.creditsUsedUSDIsEstimate == false
                            ? "$" : "~$"
                        return String(format: " · \(prefix)%.2f", $0)
                    } ?? ""
                creditLines.append(
                    ProviderLine(label: "Credits Used", value: "\(formatStat(creditsUsed)) cr\(dollars)\(suffix)")
                )
            }
            codexPaidCreditUsage = codexPaidCreditPresentation(
                limits: stats.codexQuota?.limits,
                creditPools: stats.codexQuota?.creditPools,
                creditsUsed: stats.codexAnalyticsSummary?.creditsUsed,
                usdValue: stats.codexAnalyticsSummary?.creditsUsedUSD,
                usdIsEstimate: stats.codexAnalyticsSummary?.creditsUsedUSDIsEstimate ?? true,
                windowDays: stats.codexAnalyticsSummary?.creditsWindowDays
            )

            if period == .day {
                appendUnique("Burn", stats.codexBurn.map {
                    let mode = $0 > 15 ? "Heavy" : $0 > 5 ? "Active" : "Light"
                    return String(format: "%.1f%%/hr · %@", $0, mode)
                })
            }
            appendUnique("Review", stats.codexQuota?.codeReviewUsedPct.map { String(format: "%.0f%% used", $0) })
            if period == .week, let analytics = stats.codexAnalyticsSummary {
                appendUnique("Turns", analytics.avgDailyTurns.map { "\(formatStat($0))/day" })
                if analytics.reviewsAvailable == true,
                   let reviews = analytics.avgDailyReviews,
                   let comments = analytics.avgDailyComments,
                   reviews > 0 || comments > 0 {
                    appendUnique("Reviews", "\(formatStat(reviews))/day · \(formatStat(comments)) comments/day")
                }
            }
        case "cursor":
            let cursor = stats.cursor
            primaryUsed = primaryUsed ?? cursor.map { $0.usagePct * 100 }
            primaryReset = primaryReset ?? cursor?.resetAt
            primaryLabel = "Monthly"
            plan = plan ?? cursor?.plan?.capitalized ?? "Free"
            status = status ?? {
                let maxRequests = cursor?.totalMaxRequests ?? 0
                if cursor?.atLimit == true || cursor?.limitHit == true { return "at_limit" }
                if maxRequests > 0 && (cursor?.remaining ?? maxRequests) <= 0 { return "at_limit" }
                return (cursor?.totalRequests ?? 0) > 0 ? "active" : "idle"
            }()

            appendUnique("Requests", {
                let maxRequests = cursor?.totalMaxRequests ?? 0
                if maxRequests > 0 {
                    return "\(cursor?.totalRequests ?? 0)/\(maxRequests)"
                }
                return cursor?.totalRequests.map { "\($0) this month" }
            }())
            if let remaining = cursor?.remainingRequests {
                appendUnique("Remaining", remaining > 0 ? "\(remaining) requests" : "Limit reached")
            } else if let cursor, cursor.totalMaxRequests > 0 {
                appendUnique("Remaining", cursor.remaining > 0 ? "\(cursor.remaining) requests" : "Limit reached")
            } else if cursor?.atLimit == true || cursor?.limitHit == true {
                appendUnique("Remaining", "Limit reached")
            }
            appendUnique("Limit", cursor?.limitMessage)
            appendUnique("Reset", cursor?.resetAt)
            appendUnique("Tokens", cursor?.totalTokens.map { formatTokens($0) })
            appendUnique("Window", cursor?.startOfMonth)
            modelLines = cursorModelDetailLines(cursor?.models)
        default:
            status = status ?? "idle"
        }

        let normalizedStatusValue = normalizedStatus(status) ?? "idle"
        let legacyUsageLines = usageDetailLines(
            primaryUsed: primaryUsed,
            primaryRemaining: primaryRemaining,
            secondaryUsed: secondaryUsed,
            secondaryRemaining: secondaryRemaining,
            primaryLabel: primaryLabel,
            secondaryLabel: secondaryLabel
        )
        var limitLines = limitDetailLines(
            status: normalizedStatusValue,
            primaryReset: primaryReset,
            secondaryReset: secondaryReset,
            primaryLabel: primaryLabel,
            secondaryLabel: secondaryLabel
        )
        var usageLines = quotaLines.isEmpty ? legacyUsageLines : quotaLines
        if !quotaLines.isEmpty {
            limitLines = []
        }
        if quotaLines.isEmpty {
            usageLines.append(contentsOf: limitLines.filter { $0.label != "Status" })
        }
        var activityLines = activityDetailLines(
            activeHoursDay: activeHoursDay,
            messagesDay: messagesDay,
            tokensDay: tokensDay,
            period: period
        )
        appendPeriodDetails(
            periodMetrics,
            includeSurfaces: providerID == "codex" || providerID == "claude",
            to: &activityLines
        )

        let selectedWindowKinds: Set<String> = period == .day
            ? ["session"]
            : ["weekly", "monthly", "account"]
        var costLines = periodCostDetailLines(periodMetrics, period: period)
        let providerEstimates = providerID == "claude"
            ? stats.claudeQuota?.costEstimates
            : providerID == "codex" ? stats.codexQuota?.costEstimates : nil
        let estimateStatus = providerID == "claude"
            ? stats.claudeQuota?.costEstimateStatus
            : providerID == "codex" ? stats.codexQuota?.costEstimateStatus : nil
        let providerBuckets = providerID == "claude"
            ? stats.claudeQuota?.limits
            : providerID == "codex" ? stats.codexQuota?.limits : nil
        costLines.append(contentsOf: quotaCostDetailLines(
            providerBuckets,
            estimates: providerEstimates,
            windowKinds: selectedWindowKinds,
            period: period
        ))
        if estimateStatus == "refreshing" {
            costLines.append(ProviderLine(label: "Estimate", value: "Updating in background"))
        }

        let health = stats.providerHealth?[providerID]
        var displayStatus = providerStatusLabel(health, fallback: normalizedStatusValue)
        let claudePaidUsageIsCurrent = claudePaidUsage != nil
            && (health?.quota == nil || health?.quota?.status == "current")
            && (health?.credits == nil || health?.credits?.status == "current")
        if claudePaidUsageIsCurrent, let paidUsage = claudePaidUsage {
            displayStatus = claudePaidUsageStatusText(paidUsage.state, compact: true)
            creditLines.removeAll { $0.label == "Credit Status" || $0.label == "Spent This Month" }
            creditLines.insert(ProviderLine(
                label: "Credit Status",
                value: claudePaidUsageStatusText(paidUsage.state)
            ), at: 0)
            if let spent = paidUsage.spentMonth {
                gaugeAmountUSD = spent
                gaugeAmountIsEstimate = false
                gaugeAmountLabel = "Usage credits spent this month"
                costLines.insert(ProviderLine(
                    label: "Usage Credits",
                    value: String(format: "$%.2f spent this month", spent)
                ), at: 0)
            }
        }
        let codexPaidCreditIsCurrent = codexPaidCreditUsage != nil
            && (health?.quota == nil || health?.quota?.status == "current")
            && (health?.credits == nil || health?.credits?.status == "current")
        if codexPaidCreditIsCurrent, let paidCredit = codexPaidCreditUsage {
            switch paidCredit.state {
            case .active:
                displayStatus = "paid credits"
            case .balanceEmpty:
                displayStatus = "credit balance empty"
            case .spendControlReached:
                displayStatus = "credit spend control reached"
            }
            creditLines.removeAll { $0.label == "Credit Status" || $0.label == "Credits Used" }
            creditLines.insert(ProviderLine(
                label: "Credit Status",
                value: codexPaidCreditStatusText(paidCredit.state)
            ), at: 0)
            if let creditsUsed = paidCredit.creditsUsed {
                var value = "\(formatCreditAmount(creditsUsed)) cr used"
                if let usdValue = paidCredit.usdValue {
                    let prefix = paidCredit.usdIsEstimate ? "~$" : "$"
                    value += String(format: " · \(prefix)%.2f", usdValue)
                }
                if let windowDays = paidCredit.windowDays {
                    value += " · \(windowDays)d"
                }
                costLines.insert(ProviderLine(label: "Purchased Credits", value: value), at: 0)
            }
            if paidCredit.state == .active, let usdValue = paidCredit.usdValue {
                gaugeAmountUSD = usdValue
                gaugeAmountIsEstimate = paidCredit.usdIsEstimate
                gaugeAmountLabel = "Purchased credit usage"
            }
        }
        var accountLines: [ProviderLine] = []
        if let plan = normalizedText(plan) {
            accountLines.append(ProviderLine(label: "Plan", value: plan))
        }
        accountLines.append(ProviderLine(label: "Status", value: displayStatus))
        accountLines.append(contentsOf: creditLines)
        let sourceLines = healthDetailLines(health)

        let hasSignal = primaryUsed != nil
            || secondaryUsed != nil
            || !usageLines.isEmpty
            || !limitLines.isEmpty
            || !activityLines.isEmpty
            || !uniqueLines.isEmpty
            || snapshot != nil

        if !hasSignal && !coreProviderIDs.contains(providerID) {
            return nil
        }

        if !hasSignal {
            uniqueLines.append(ProviderLine(label: "Status", value: "No data yet"))
        }

        var summary = rowSummary(
            period: period,
            primaryUsed: primaryUsed,
            secondaryUsed: secondaryUsed,
            primaryLabel: primaryLabel,
            secondaryLabel: secondaryLabel,
            activeHoursDay: activeHoursDay,
            messagesDay: messagesDay,
            tokensDay: tokensDay,
            primaryReset: primaryReset
        )
        if claudePaidUsageIsCurrent, let paidUsage = claudePaidUsage {
            summary = paidUsageRowSummary(paidUsage, sessionReset: stats.claudeQuota?.sessionReset)
        }
        if codexPaidCreditIsCurrent, let paidCredit = codexPaidCreditUsage {
            summary = codexPaidCreditRowSummary(paidCredit)
        }

        return ProviderCard(
            id: providerID,
            name: label,
            plan: plan,
            status: displayStatus,
            primaryPct: primaryUsed,
            secondaryPct: secondaryUsed,
            primaryReset: primaryReset,
            gaugeAmountUSD: gaugeAmountUSD,
            gaugeAmountIsEstimate: gaugeAmountIsEstimate,
            gaugeAmountLabel: gaugeAmountLabel,
            gaugeLabel: primaryUsed == nil && period == .day ? "Today activity" : primaryLabel,
            summary: summary,
            sections: [
                ProviderSection(title: "Quota", lines: usageLines),
                ProviderSection(title: "Activity", lines: activityLines),
                ProviderSection(title: "Cost", lines: costLines),
                ProviderSection(title: "Models", lines: modelLines),
                ProviderSection(title: "Account", lines: accountLines),
                ProviderSection(title: "Sources", lines: sourceLines),
                ProviderSection(title: "Details", lines: uniqueLines),
            ]
        )
    }

    private func coreProviderRegistry() -> [ProviderRegistryEntry] {
        [
            ProviderRegistryEntry(id: "claude", label: "Claude", color: nil, order: 1),
            ProviderRegistryEntry(id: "codex", label: "Codex", color: nil, order: 2),
            ProviderRegistryEntry(id: "cursor", label: "Cursor", color: nil, order: 3),
        ]
    }

    private var coreProviderIDs: Set<String> {
        Set(coreProviderRegistry().map(\.id))
    }

    private func providerLabel(for providerID: String, fallback: String?) -> String {
        if let fallback = normalizedText(fallback) {
            return fallback
        }
        switch providerID {
        case "claude": return "Claude"
        case "codex": return "Codex"
        case "cursor": return "Cursor"
        default:
            return titleCase(providerID)
        }
    }

    private func usageDetailLines(primaryUsed: Double?, primaryRemaining: Double?,
                                  secondaryUsed: Double?, secondaryRemaining: Double?,
                                  primaryLabel: String, secondaryLabel: String) -> [ProviderLine] {
        var lines: [ProviderLine] = []
        if let primaryUsed {
            lines.append(ProviderLine(label: primaryLabel, value: String(format: "%.0f%% used", primaryUsed)))
        }
        if let primaryRemaining {
            lines.append(ProviderLine(label: "\(primaryLabel) Left", value: String(format: "%.0f%%", primaryRemaining)))
        }
        if let secondaryUsed {
            lines.append(ProviderLine(label: secondaryLabel, value: String(format: "%.0f%% used", secondaryUsed)))
        }
        if let secondaryRemaining {
            lines.append(ProviderLine(label: "\(secondaryLabel) Left", value: String(format: "%.0f%%", secondaryRemaining)))
        }
        return lines
    }

    private func limitDetailLines(status: String, primaryReset: String?, secondaryReset: String?,
                                  primaryLabel: String, secondaryLabel: String) -> [ProviderLine] {
        var lines: [ProviderLine] = [
            ProviderLine(label: "Status", value: titleCase(status)),
        ]
        if let primaryReset = normalizedText(primaryReset) {
            lines.append(ProviderLine(label: "\(primaryLabel) Reset", value: vm.formatResetDisplay(for: primaryReset) ?? primaryReset))
        }
        if let secondaryReset = normalizedText(secondaryReset) {
            lines.append(ProviderLine(label: "\(secondaryLabel) Reset", value: vm.formatResetDisplay(for: secondaryReset) ?? secondaryReset))
        }
        return lines
    }

    private func quotaDetailLines(
        _ buckets: [QuotaBucket]?,
        windowKinds: Set<String>? = nil
    ) -> [ProviderLine] {
        guard let buckets, !buckets.isEmpty else { return [] }
        return buckets.compactMap { bucket in
            if let windowKinds,
               let windowKind = bucket.windowKind,
               !windowKinds.contains(windowKind) {
                return nil
            }
            var parts: [String] = []
            if let used = bucket.usedPct {
                parts.append(String(format: "%.0f%% used", used))
            }
            if let remaining = bucket.remainingPct {
                parts.append(String(format: "%.0f%% left", remaining))
            }
            if let reset = normalizedText(bucket.reset) {
                parts.append("reset \(vm.formatResetDisplay(for: reset) ?? reset)")
            }
            guard !parts.isEmpty else { return nil }
            return ProviderLine(label: quotaBucketLabel(bucket), value: parts.joined(separator: " · "))
        }
    }

    private func quotaBucketLabel(_ bucket: QuotaBucket) -> String {
        if bucket.scopeKind == nil || bucket.scopeKind == "aggregate" {
            if let minutes = bucket.windowMinutes, minutes > 0, minutes < 1_440 {
                let hours = minutes / 60
                return hours.rounded() == hours
                    ? String(format: "%.0fh quota", hours)
                    : String(format: "%.1fh quota", hours)
            }
            switch bucket.windowKind {
            case "session": return "Session quota"
            case "weekly": return "Weekly quota"
            case "monthly": return "Monthly quota"
            case "account": return "Account quota"
            default: break
            }
        }
        return normalizedText(bucket.label)
            ?? bucket.model.map(shortModelName)
            ?? bucket.feature.map(titleCase)
            ?? titleCase(bucket.windowKind ?? "Limit")
    }

    private func quotaWindowLabel(_ buckets: [QuotaBucket]?, windowKind: String) -> String? {
        guard let bucket = buckets?.first(where: {
            $0.windowKind == windowKind && ($0.scopeKind == nil || $0.scopeKind == "aggregate")
        }) else { return nil }
        if let minutes = bucket.windowMinutes, minutes > 0, minutes < 1_440 {
            let hours = minutes / 60
            return hours.rounded() == hours
                ? String(format: "%.0fh quota", hours)
                : String(format: "%.1fh quota", hours)
        }
        switch windowKind {
        case "weekly": return "Weekly quota"
        case "monthly": return "Monthly quota"
        case "account": return "Account quota"
        default: return normalizedText(bucket.label).map { "\($0) quota" }
        }
    }

    private func periodCostDetailLines(
        _ metrics: ProviderPeriodMetrics?,
        period: UsagePeriod
    ) -> [ProviderLine] {
        guard let metrics else { return [] }
        var lines: [ProviderLine] = []
        let label = "\(period.label) value"
        if let dollars = metrics.estimatedCostUSD {
            lines.append(ProviderLine(label: label, value: String(format: "~$%.2f estimated", dollars)))
        } else if !(metrics.unpricedModels ?? []).isEmpty {
            lines.append(ProviderLine(label: label, value: "Rate unavailable"))
        }
        if let credits = metrics.estimatedCredits, credits > 0 {
            lines.append(ProviderLine(label: "Credit value", value: "~\(formatStat(credits)) credits"))
        }
        if let coverage = metrics.pricingCoveragePct, coverage < 99.95 {
            lines.append(ProviderLine(label: "Pricing", value: String(format: "%.0f%% covered", coverage)))
        }
        if let unpriced = metrics.unpricedModels, !unpriced.isEmpty {
            lines.append(ProviderLine(label: "Unpriced", value: unpriced.prefix(2).map(shortModelName).joined(separator: ", ")))
        }
        return lines
    }

    private func quotaCostDetailLines(
        _ buckets: [QuotaBucket]?,
        estimates: [QuotaCostEstimate]?,
        windowKinds: Set<String>,
        period: UsagePeriod
    ) -> [ProviderLine] {
        guard let buckets, let estimates else { return [] }
        let bucketsByID = Dictionary(uniqueKeysWithValues: buckets.map { ($0.id, $0) })
        return estimates.compactMap { estimate in
            guard (estimate.observedTokens ?? 0) > 0,
                  let bucket = bucketsByID[estimate.id],
                  shouldShowQuotaCostBucket(
                    bucket,
                    selectedWindowKinds: windowKinds,
                    period: period
                  ) else { return nil }
            let rawLabel = quotaBucketLabel(bucket)
            var values: [String] = []
            if let dollars = estimate.estimatedCostUSD {
                values.append(String(format: "~$%.2f", dollars))
            }
            if let credits = estimate.estimatedCredits {
                values.append("~\(formatStat(credits)) cr")
            }
            if values.isEmpty, !(estimate.unpricedModels ?? []).isEmpty {
                values.append("Rate unavailable")
            }
            guard !values.isEmpty else { return nil }
            if let coverage = estimate.pricingCoveragePct, coverage < 99.95 {
                values.append(String(format: "%.0f%% priced", coverage))
            }
            return ProviderLine(label: "\(rawLabel) value", value: values.joined(separator: " · "))
        }
    }

    private func appendPeriodDetails(
        _ metrics: ProviderPeriodMetrics?,
        includeSurfaces: Bool,
        to lines: inout [ProviderLine]
    ) {
        guard let metrics else { return }
        appendLine(
            "Sessions",
            compactPair(
                metrics.sessions,
                distinctConversationCount(
                    sessions: metrics.sessions,
                    conversations: metrics.conversations
                ),
                secondSuffix: " conv"
            ),
            to: &lines
        )
        appendLine(
            "Input / Output",
            compactTokenPair(metrics.inputTokens, metrics.outputTokens),
            to: &lines
        )
        if let label = pairedMetricLabel(
            firstLabel: "Cache",
            firstValue: metrics.cacheTokens,
            secondLabel: "Reasoning",
            secondValue: metrics.reasoningTokens
        ) {
            appendLine(
                label,
                compactTokenPair(metrics.cacheTokens, metrics.reasoningTokens),
                to: &lines
            )
        }
        appendLine(
            "User / Requests",
            compactPair(metrics.userMessages, metrics.requests, secondSuffix: " req"),
            to: &lines
        )
        if includeSurfaces {
            let surfaces = (metrics.surfaces ?? [:]).sorted { left, right in
                let leftTokens = left.value.totalTokens ?? 0
                let rightTokens = right.value.totalTokens ?? 0
                return leftTokens == rightTokens
                    ? (left.value.sessions ?? 0) > (right.value.sessions ?? 0)
                    : leftTokens > rightTokens
            }
            for (surface, usage) in surfaces.prefix(4) {
                var values: [String] = []
                if let tokens = usage.totalTokens, tokens > 0 {
                    values.append(formatTokens(tokens))
                }
                if let requests = usage.requests, requests > 0 {
                    values.append("\(requests) req")
                } else if let sessions = usage.sessions, sessions > 0 {
                    values.append("\(sessions) sessions")
                }
                if let dollars = usage.estimatedCostUSD, dollars > 0 {
                    values.append(String(format: "~$%.2f", dollars))
                }
                if let coverage = usage.pricingCoveragePct, coverage < 99.95 {
                    values.append(String(format: "%.0f%% priced", coverage))
                }
                appendLine(surfaceDisplayName(surface), values.joined(separator: " · "), to: &lines)
            }
        }
    }

    private func surfaceDisplayName(_ surface: String) -> String {
        switch surface.lowercased() {
        case "loop": return "Loop"
        case "cli": return "CLI"
        case "desktop": return "Desktop app"
        case "vscode": return "VS Code"
        case "work": return "Desktop (Work)"
        case "exec": return "Exec"
        case "claude-code": return "Claude Code"
        case "subagent": return "Subagent"
        case "unknown": return "Unknown"
        default: return surface
        }
    }

    private func compactPair(_ first: Int?, _ second: Int?, secondSuffix: String) -> String? {
        let values = [
            first.flatMap { $0 > 0 ? String($0) : nil },
            second.flatMap { $0 > 0 ? "\($0)\(secondSuffix)" : nil },
        ].compactMap { $0 }
        return values.isEmpty ? nil : values.joined(separator: " · ")
    }

    private func compactTokenPair(_ first: Int?, _ second: Int?) -> String? {
        let values = [first, second].compactMap { value in
            value.flatMap { $0 > 0 ? formatTokens($0) : nil }
        }
        return values.isEmpty ? nil : values.joined(separator: " / ")
    }

    private func appendLine(_ label: String, _ value: String?, to lines: inout [ProviderLine]) {
        guard let value, value != "0" else { return }
        guard !lines.contains(where: { $0.label == label }) else { return }
        lines.append(ProviderLine(label: label, value: value))
    }

    private func creditDetailLines(
        _ pools: [CreditPool]?,
        resetCreditsUsed: Int? = nil,
        resetCreditsGranted: Int? = nil
    ) -> [ProviderLine] {
        guard let pools, !pools.isEmpty else { return [] }
        var lines: [ProviderLine] = []
        for pool in pools {
            let label = normalizedText(pool.label) ?? "Credits"
            if let balance = pool.balance {
                let value: String
                if pool.kind == "reset" {
                    var parts = [String(format: "%.0f left", balance)]
                    if let resetCreditsUsed, resetCreditsUsed > 0 {
                        parts.append("\(resetCreditsUsed) used")
                    }
                    if let resetCreditsGranted, resetCreditsGranted > 0 {
                        parts.append("+\(resetCreditsGranted) granted")
                    }
                    value = parts.joined(separator: " · ")
                } else {
                    let amount = pool.unit == "usd"
                        ? String(format: "$%.2f", balance)
                        : String(format: "%.0f %@", balance, pool.unit ?? "credits")
                    if pool.kind == "promotional", let expiry = pool.nextExpiryAt {
                        let date = Date(timeIntervalSince1970: TimeInterval(expiry))
                        value = "\(amount) · expires \(date.formatted(.dateTime.month(.abbreviated).day()))"
                    } else {
                        value = amount
                    }
                }
                lines.append(ProviderLine(label: label, value: value))
            }
            if let spent = pool.spentMonth {
                lines.append(ProviderLine(label: "Spent This Month", value: String(format: "$%.2f", spent)))
            }
            if let cap = pool.monthlySpendCap {
                lines.append(ProviderLine(label: "Monthly Spend Cap", value: String(format: "$%.2f", cap)))
            }
            if let headroom = pool.monthlySpendHeadroom {
                lines.append(ProviderLine(label: "Spend Headroom", value: String(format: "$%.2f", headroom)))
            }
            if let reset = normalizedText(pool.reset) {
                lines.append(ProviderLine(
                    label: "Usage Credits Reset",
                    value: vm.formatResetDisplay(for: reset) ?? reset
                ))
            }
            if pool.kind != "promotional", let expiry = pool.nextExpiryAt {
                let date = Date(timeIntervalSince1970: TimeInterval(expiry))
                lines.append(ProviderLine(
                    label: "Next Credit Expiry",
                    value: date.formatted(.dateTime.month(.abbreviated).day())
                ))
            }
            if pool.unlimited == true {
                lines.append(ProviderLine(label: label, value: "Unlimited"))
            } else if pool.spendControlReached == true {
                lines.append(ProviderLine(label: "Credit Status", value: "Monthly spend cap reached"))
            } else if pool.outOfCredits == true || (pool.balance.map { $0 <= 0 } == true) {
                lines.append(ProviderLine(label: "Credit Status", value: "No prepaid funds left"))
            } else if pool.enabled == false {
                lines.append(ProviderLine(label: "Credit Status", value: "Disabled"))
            }
            if pool.balance == nil, pool.spentMonth == nil, pool.monthlySpendCap == nil, pool.enabled == true {
                lines.append(ProviderLine(label: label, value: "Enabled"))
            }
        }
        return lines
    }

    /// Per-model breakdown rows, sorted by tokens descending.
    private func modelDetailLines(_ models: [String: ModelTokenUsage]?) -> [ProviderLine] {
        guard let models, !models.isEmpty else { return [] }
        let sorted = models
            .sorted { ($0.value.tokens ?? 0) > ($1.value.tokens ?? 0) }
        return sorted.map { name, usage in
            ProviderLine(
                label: shortModelName(name),
                value: modelUsageValue(tokens: usage.tokens, requests: usage.requests)
            )
        }
    }

    private func cursorModelDetailLines(_ models: [String: CursorModelStats]?) -> [ProviderLine] {
        guard let models, !models.isEmpty else { return [] }
        return models
            .sorted { ($0.value.tokens ?? 0) > ($1.value.tokens ?? 0) }
            .map { name, usage in
                ProviderLine(label: shortModelName(name), value: modelUsageValue(tokens: usage.tokens, requests: usage.requests))
            }
    }

    private func modelUsageValue(tokens: Int?, requests: Int?) -> String {
        var parts: [String] = []
        if let tokens, tokens > 0 {
            parts.append("\(formatTokens(tokens)) tok")
        }
        if let requests, requests > 0 {
            parts.append("\(requests) req")
        }
        return parts.isEmpty ? "—" : parts.joined(separator: " · ")
    }

    /// Compact display name: drops the "claude-" prefix and trailing
    /// date-stamp suffixes (e.g. "claude-sonnet-4-5-20250929" → "sonnet-4-5").
    private func shortModelName(_ raw: String) -> String {
        var name = raw
        if name.lowercased().hasPrefix("claude-") {
            name = String(name.dropFirst("claude-".count))
        }
        if let range = name.range(of: #"-20\d{6}$"#, options: .regularExpression) {
            name.removeSubrange(range)
        }
        return name.isEmpty ? raw : name
    }

    private func activityDetailLines(
        activeHoursDay: Double?,
        messagesDay: Double?,
        tokensDay: Double?,
        period: UsagePeriod
    ) -> [ProviderLine] {
        var lines: [ProviderLine] = []
        if let activeHoursDay, activeHoursDay > 0 {
            lines.append(ProviderLine(label: "Active", value: String(format: "%.1fh %@", activeHoursDay, period.rawValue)))
        }
        if let messagesDay, messagesDay > 0 {
            lines.append(ProviderLine(label: "Messages", value: "\(Int(messagesDay.rounded()))"))
        }
        if let tokensDay, tokensDay > 0 {
            lines.append(ProviderLine(label: "Tokens", value: formatTokens(Int(tokensDay.rounded()))))
        }
        if lines.isEmpty {
            lines.append(ProviderLine(label: period.label, value: "No activity"))
        }
        return lines
    }

    private func rowSummary(period: UsagePeriod,
                            primaryUsed: Double?, secondaryUsed: Double?,
                            primaryLabel: String, secondaryLabel: String,
                            activeHoursDay: Double?, messagesDay: Double?, tokensDay: Double?,
                            primaryReset: String?) -> String {
        var parts: [String] = []
        if let primaryUsed {
            parts.append(String(format: "%.0f%% %@", primaryUsed, primaryLabel.lowercased()))
        }
        if let secondaryUsed {
            parts.append(String(format: "%.0f%% %@", secondaryUsed, secondaryLabel.lowercased()))
        }
        if parts.isEmpty {
            return activityFirstSummary(
                period: period,
                activeHours: activeHoursDay,
                messages: messagesDay,
                tokens: tokensDay
            )
        }
        if let reset = normalizedText(primaryReset) {
            parts.append("resets \(vm.formatResetDisplay(for: reset) ?? reset)")
        }
        return parts.joined(separator: " · ")
    }

    private func paidUsageRowSummary(
        _ paidUsage: ClaudePaidUsagePresentation,
        sessionReset: String?
    ) -> String {
        var parts: [String] = []
        if paidUsage.state != .active {
            parts.append(claudePaidUsageStatusText(paidUsage.state))
        }
        if let spent = paidUsage.spentMonth {
            parts.append(String(format: "$%.2f spent this month", spent))
        } else if paidUsage.state == .active {
            parts.append("Usage credits active")
        }
        if let sessionReset = normalizedText(sessionReset) {
            parts.append("session resets \(shortTime(sessionReset))")
        }
        return parts.joined(separator: " · ")
    }

    private func claudePaidUsageStatusText(
        _ state: ClaudePaidUsageState,
        compact: Bool = false
    ) -> String {
        switch state {
        case .active:
            return compact ? "paid usage" : "Usage credits active"
        case .balanceEmpty:
            return compact ? "credit balance empty" : "No prepaid funds left"
        case .spendControlReached:
            return compact ? "monthly cap reached" : "Monthly spend cap reached"
        }
    }

    private func codexPaidCreditStatusText(_ state: CodexPaidCreditState) -> String {
        switch state {
        case .active: return "Purchased credits active"
        case .balanceEmpty: return "No purchased credits left"
        case .spendControlReached: return "Spend control reached"
        }
    }

    private func codexPaidCreditRowSummary(_ paidCredit: CodexPaidCreditPresentation) -> String {
        guard paidCredit.state == .active else {
            return codexPaidCreditStatusText(paidCredit.state)
        }
        var parts: [String] = []
        if let creditsUsed = paidCredit.creditsUsed {
            parts.append("\(formatCreditAmount(creditsUsed)) cr used")
        } else {
            parts.append("Purchased credits active")
        }
        if let usdValue = paidCredit.usdValue {
            let prefix = paidCredit.usdIsEstimate ? "~$" : "$"
            parts.append(String(format: "\(prefix)%.2f", usdValue))
        }
        if let windowDays = paidCredit.windowDays {
            parts.append("\(windowDays)d")
        }
        return parts.joined(separator: " · ")
    }

    private func uniqueLines(from unique: [String: JSONValue]?) -> [ProviderLine] {
        guard let unique else { return [] }
        return unique.keys.sorted().compactMap { key in
            guard !["plan", "limits", "credit_pools"].contains(key),
                  let value = uniqueValueText(unique[key]) else { return nil }
            return ProviderLine(label: titleCase(key), value: value)
        }
    }

    private func uniqueValueText(_ value: JSONValue?) -> String? {
        guard let value else { return nil }
        switch value {
        case .string(let text):
            return normalizedText(text)
        case .number(let number):
            if number.rounded() == number {
                return String(format: "%.0f", number)
            }
            return String(format: "%.2f", number)
        case .bool(let flag):
            return flag ? "true" : "false"
        case .array(let values):
            let rendered = values.compactMap { $0.stringValue }
            guard !rendered.isEmpty else { return "\(values.count) items" }
            return rendered.prefix(3).joined(separator: ", ")
        case .object(let object):
            return object.isEmpty ? nil : "\(object.count) fields"
        case .null:
            return nil
        }
    }

    private func providerStatusLabel(_ health: ProviderHealth?, fallback: String) -> String {
        if health?.quota?.status == "stale" { return "quota stale" }
        if health?.quota?.status == "unavailable" { return "local only" }
        if health?.activity?.status == "unavailable" { return "quota only" }
        if health?.quota?.status == "current", health?.activity?.status == "current" {
            return "current"
        }
        return titleCase(fallback)
    }

    private func healthDetailLines(_ health: ProviderHealth?) -> [ProviderLine] {
        guard let health else { return [] }
        var lines: [ProviderLine] = []
        var seenDetails = Set<String>()
        var seenRecoveries = Set<String>()
        for (label, feed) in [("Quota", health.quota), ("Activity", health.activity), ("Credits", health.credits)] {
            guard let feed else { continue }
            var value = "\(titleCase(feed.source)) · \(titleCase(feed.status))"
            if feed.status == "current", let age = feed.ageSeconds {
                value += age < 60 ? " · now" : " · \(age / 60)m ago"
            }
            lines.append(ProviderLine(label: label, value: value))
            if let detail = normalizedText(feed.detail),
               feed.status != "unavailable",
               seenDetails.insert(detail).inserted {
                lines.append(ProviderLine(label: "Feed detail", value: detail))
            }
            if let recovery = normalizedText(feed.recovery),
               seenRecoveries.insert(recovery).inserted {
                lines.append(ProviderLine(label: "Action", value: recovery))
            }
        }
        return lines
    }

    private func titleCase(_ input: String) -> String {
        input
            .replacingOccurrences(of: "_", with: " ")
            .split(separator: " ")
            .map { $0.capitalized }
            .joined(separator: " ")
    }

    private func normalizedText(_ value: String?) -> String? {
        guard let value = value?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else {
            return nil
        }
        return value
    }

    private func normalizedStatus(_ status: String?) -> String? {
        guard let status = normalizedText(status) else { return nil }
        return status.lowercased()
    }

    // ── Tool row ────────────────────────────────────

    func toolRow(name: String, selected: Bool, sessionPct: Double?, weeklyPct: Double?,
                 amountUSD: Double?, amountIsEstimate: Bool,
                 amountLabel: String, gaugeLabel: String,
                 status: String, reset: String?, plan: String? = nil, summary: String? = nil,
                 onTap: @escaping () -> Void) -> some View {
        Button(action: onTap) {
            HStack(spacing: 10) {
                // Mini gauge glyph
                MiniGauge(
                    pct: sessionPct,
                    weeklyPct: weeklyPct,
                    amountUSD: amountUSD,
                    amountIsEstimate: amountIsEstimate,
                    amountLabel: amountLabel,
                    quotaLabel: gaugeLabel
                )
                    .frame(width: 28, height: 28)

                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(name)
                            .font(.system(.callout, design: .rounded, weight: .medium))
                        if let plan, !plan.isEmpty {
                            Text(plan)
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                        }
                        Text(status)
                            .font(.caption2)
                            .foregroundStyle(statusColor(status))
                    }

                    HStack(spacing: 8) {
                        if let summary, !summary.isEmpty {
                            Text(summary)
                        } else {
                            if let sessionPct {
                                Text(String(format: "%.0f%% session", sessionPct))
                            } else if weeklyPct != nil {
                                Text("-- session")
                            }
                            if sessionPct != nil || weeklyPct != nil {
                                if let w = weeklyPct {
                                    Text(String(format: "· %.0f%% week", w))
                                }
                            }
                            if let r = reset {
                                Text("resets \(shortTime(r))")
                            }
                        }
                    }
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
                }

                Spacer()

                if selected {
                    Image(systemName: "chevron.right")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
            .padding(.horizontal, 14).padding(.vertical, 8)
            .background(selected ? Color.primary.opacity(0.06) : Color.clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(name)
        .accessibilityAddTraits(.isButton)
        .accessibilityValue(
            [plan, status, summary]
                .compactMap { $0 }
                .filter { !$0.isEmpty }
                .joined(separator: ", ")
        )
        .accessibilityHint("Show \(name) details")
    }

    func toolStatus(session: Double, burn: Double?) -> String {
        if session >= 90 { return "critical" }
        if session >= 70 || (burn ?? 0) > 20 { return "heating" }
        if session > 0 || (burn ?? 0) > 0 { return "active" }
        return "idle"
    }

    func statusColor(_ status: String) -> Color {
        let normalized = status.lowercased()
        if normalized.contains("limit reached")
            || normalized.contains("spend control")
            || normalized.contains("cap reached")
            || normalized.contains("balance empty") {
            return .red
        }
        if normalized.contains("paid") || normalized.contains("credit") {
            return .orange
        }
        if normalized.contains("stale") || normalized.contains("unavailable") || normalized.contains("only") {
            return .orange
        }
        switch normalized {
        case "critical", "at_limit", "limit", "error":
            return .red
        case "heating", "warning", "partial":
            return .orange
        case "active", "ok", "current":
            return .green
        default:
            return .gray
        }
    }
}

struct WorkTrackingBar: View {
    @ObservedObject var vm: UsageViewModel

    var body: some View {
        Button {
            if vm.workTrackingEnabled {
                WorkReportWindowController.shared.open(vm: vm)
            } else {
                SettingsWindowController.shared.open(vm: vm)
            }
        } label: {
            HStack(spacing: 9) {
                Image(systemName: vm.workTrackingEnabled ? "briefcase.fill" : "briefcase")
                    .font(.system(size: 14))
                    .foregroundStyle(vm.workTrackingEnabled ? activeColor : Color.secondary)
                    .frame(width: 20)
                VStack(alignment: .leading, spacing: 1) {
                    Text(currentWorkLabel)
                        .font(.system(.caption, design: .rounded).weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(currentWorkDetail)
                        .font(.system(.caption2, design: .rounded))
                        .foregroundStyle(reviewCount > 0 ? Color.orange : Color.secondary)
                        .lineLimit(1)
                }
                Spacer(minLength: 6)
                if vm.workStatus?.refresh.status == "running" || vm.isWorkMutationInFlight {
                    ProgressView().controlSize(.mini)
                } else if reviewCount > 0 {
                    Label("\(reviewCount)", systemImage: "exclamationmark.circle.fill")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.orange)
                } else if vm.workError != nil {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                }
                Image(systemName: "chevron.right")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.tertiary)
            }
            .contentShape(Rectangle())
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
        }
        .buttonStyle(.plain)
        .help(vm.workTrackingEnabled ? "Open work review" : "Turn on work tracking in Settings")
        .accessibilityLabel("\(currentWorkLabel), \(currentWorkDetail)")
    }

    private var reviewCount: Int {
        vm.needsReviewCount(for: vm.selectedPeriod == .day ? .day : .week)
    }

    private var currentWorkLabel: String {
        guard vm.workTrackingEnabled else { return "Work tracking is off" }
        if vm.activeWork.state == "active" {
            if let parent = vm.activeWork.parentName, let name = vm.activeWork.name {
                return "\(parent) / \(name)"
            }
            if let name = vm.activeWork.name { return name }
        }
        let labels = Set(vm.workSessions.compactMap { session -> String? in
            guard session.attributionState != .excluded,
                  session.effectiveWorkLabel != "Automatic" else { return nil }
            return session.effectiveWorkLabel
        })
        if labels.count == 1 { return labels.first ?? "Work activity" }
        if labels.count > 1 { return "\(labels.count) active projects" }
        return "No active AI work"
    }

    private var currentWorkDetail: String {
        guard vm.workTrackingEnabled else { return "Enable it in Settings" }
        if reviewCount > 0 {
            let period = vm.selectedPeriod == .day ? "today" : "this week"
            return "\(reviewCount) session\(reviewCount == 1 ? "" : "s") need review \(period)"
        }
        if vm.activeWork.state == "active" { return "Pinned project" }
        if !vm.workSessions.isEmpty { return "Matched automatically" }
        return "Recent activity is classified"
    }

    private var activeColor: Color {
        guard let hex = vm.activeWork.colorHex,
              hex.count == 7,
              let value = Int(hex.dropFirst(), radix: 16) else {
            return vm.workTrackingEnabled ? .accentColor : .secondary
        }
        return Color(
            red: Double((value >> 16) & 0xFF) / 255,
            green: Double((value >> 8) & 0xFF) / 255,
            blue: Double(value & 0xFF) / 255
        )
    }
}


struct ProviderTrendView: View {
    let trend: ProviderUsageTrend

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            SectionHeader(title: "Trend")
            SparklineRow(label: "7d", values: Array(trend.points.suffix(7)).map(\.totalTokens))
            SparklineRow(label: "30d", values: trend.points.map(\.totalTokens))
            if let advisor = trend.advisor {
                Text(advisor)
                    .font(.system(.caption2, design: .rounded))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

struct SparklineRow: View {
    let label: String
    let values: [Int]

    var body: some View {
        HStack(spacing: 8) {
            Text(label)
                .font(.system(.caption2, design: .rounded).monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 24, alignment: .leading)
            GeometryReader { geometry in
                Path { path in
                    guard !values.isEmpty else { return }
                    let maximum = max(values.max() ?? 0, 1)
                    let width = geometry.size.width
                    let height = geometry.size.height
                    for (index, value) in values.enumerated() {
                        let x = values.count == 1 ? 0 : width * CGFloat(index) / CGFloat(values.count - 1)
                        let y = height - height * CGFloat(value) / CGFloat(maximum)
                        if index == 0 { path.move(to: CGPoint(x: x, y: y)) }
                        else { path.addLine(to: CGPoint(x: x, y: y)) }
                    }
                }
                .stroke(Color.accentColor, style: StrokeStyle(lineWidth: 1.5, lineCap: .round, lineJoin: .round))
            }
            .frame(height: 18)
        }
        .frame(height: 20)
    }
}

// ── Mini gauge glyph (28×28) ────────────────────

struct MiniGauge: View {
    let pct: Double?       // 0...100 — session usage (fill)
    var weeklyPct: Double? // 0...100 — weekly usage (tick mark)
    var amountUSD: Double?
    var amountIsEstimate: Bool
    var amountLabel: String
    var quotaLabel: String

    private var compactAmount: String {
        let prefix = amountIsEstimate ? "~$" : "$"
        guard let value = amountUSD, value.isFinite, value >= 0 else { return "\(prefix)--" }
        if value >= 10_000 {
            return String(format: "\(prefix)%.0fk", value / 1_000)
        }
        if value >= 1_000 {
            return String(format: "\(prefix)%.1fk", value / 1_000)
        }
        if !amountIsEstimate, value < 100 {
            return String(format: "$%.2f", value)
        }
        if value >= 1 {
            return String(format: "\(prefix)%.0f", value)
        }
        if value == 0 {
            return "\(prefix)0"
        }
        return String(format: "\(prefix)%.1f", value)
    }

    var body: some View {
        Canvas { context, size in
            let center = CGPoint(x: size.width / 2, y: size.height / 2)
            let r: CGFloat = 11
            let startAngle: Double = 135
            let sweep: Double = 270

            // Track
            let track = Path { p in
                p.addArc(center: center, radius: r,
                         startAngle: .degrees(startAngle),
                         endAngle: .degrees(startAngle + sweep),
                         clockwise: false)
            }
            context.stroke(track, with: .color(.primary.opacity(0.12)), lineWidth: 3)

            // Session fill
            if let pct {
                let fillAngle = startAngle + (min(pct, 100) / 100) * sweep
                let fill = Path { p in
                    p.addArc(center: center, radius: r,
                             startAngle: .degrees(startAngle),
                             endAngle: .degrees(fillAngle),
                             clockwise: false)
                }
                let color: Color = pct >= 90 ? .red : pct >= 70 ? .orange : pct >= 50 ? .yellow : .green
                context.stroke(fill, with: .color(color), style: StrokeStyle(lineWidth: 3, lineCap: .round))
            }

            // Weekly tick mark
            if let wk = weeklyPct {
                let tickAngle = (startAngle + (min(wk, 100) / 100) * sweep) * .pi / 180
                let innerR = r - 3.5
                let outerR = r + 3.5
                let innerPt = CGPoint(x: center.x + innerR * cos(tickAngle),
                                       y: center.y - innerR * sin(tickAngle))
                let outerPt = CGPoint(x: center.x + outerR * cos(tickAngle),
                                       y: center.y - outerR * sin(tickAngle))
                var tick = Path()
                tick.move(to: innerPt)
                tick.addLine(to: outerPt)
                context.stroke(tick, with: .color(.primary.opacity(0.5)), lineWidth: 1.5)
            }

            // Center text
            let text = Text(compactAmount)
                .font(.system(size: compactAmount.count > 5 ? 5.5 : 6.5, weight: .bold, design: .rounded))
            context.draw(text, at: center)
        }
        .accessibilityLabel(
            amountUSD.map {
                let qualifier = amountIsEstimate ? "estimated" : "actual"
                return String(format: "%@ arc %.0f percent used; %@ %@ $%.2f", quotaLabel, pct ?? 0, qualifier, amountLabel, $0)
            } ?? "\(quotaLabel) arc; \(amountLabel) unavailable"
        )
        .help("Arc: \(quotaLabel). Center: \(amountIsEstimate ? "estimated " : "")\(amountLabel.lowercased()).")
    }
}

// ── Helpers ──────────────────────────────────────

struct CollapsibleDetailSection<Content: View>: View {
    let title: String
    let systemImage: String
    let summary: String?
    @Binding var isExpanded: Bool
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeInOut(duration: 0.14)) {
                    isExpanded.toggle()
                }
            } label: {
                HStack(spacing: 7) {
                    Image(systemName: systemImage)
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(.secondary)
                        .frame(width: 13)
                    Text(title)
                        .font(.system(size: 11, weight: .semibold, design: .rounded))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    Spacer(minLength: 6)
                    if !isExpanded, let summary, !summary.isEmpty {
                        Text(summary)
                            .font(.system(size: 9, design: .rounded))
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                            .minimumScaleFactor(0.78)
                    }
                    Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(.tertiary)
                        .frame(width: 10)
                }
                .frame(height: 28)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(isExpanded ? "Collapse \(title)" : "Expand \(title)")
            .accessibilityValue(isExpanded ? "Expanded" : "Collapsed")

            if isExpanded {
                VStack(alignment: .leading, spacing: 5) {
                    content
                }
                .padding(.bottom, 7)
                .transition(.opacity)
            }

            Divider()
        }
    }
}

struct SectionHeader: View {
    let title: String
    var body: some View {
        HStack {
            Text(title)
                .font(.system(.caption2, design: .rounded, weight: .semibold))
                .foregroundStyle(.tertiary)
                .textCase(.uppercase)
            Spacer()
        }
    }
}

@ViewBuilder
func detailLine(_ label: String, val: String?) -> some View {
    if let v = val {
        HStack(alignment: .top, spacing: 8) {
            Text(label)
                .foregroundStyle(.tertiary)
                .lineLimit(1)
                .minimumScaleFactor(0.78)
                .frame(width: 94, alignment: .trailing)
            Text(v)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

func shortTime(_ s: String) -> String {
    // "Apr 9 3:01 PM" → "3:01 PM" if today, otherwise "Apr 9 3:01 PM" unchanged
    let parts = s.split(separator: " ")
    guard parts.count >= 3, let day = Int(parts[1]) else { return s }

    let cal = Calendar.current
    let now = Date()
    let todayDay = cal.component(.day, from: now)

    let months = ["Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
                   "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12]
    let currentMonth = cal.component(.month, from: now)

    if let month = months[String(parts[0])], month == currentMonth, day == todayDay {
        // Today — show just time
        return parts.dropFirst(2).joined(separator: " ")
    }
    // Future date — keep "Apr 14 12:00 AM"
    return s
}

func freshness(_ ts: Int?) -> String {
    guard let ts = ts else { return "—" }
    let age = Int(Date().timeIntervalSince1970) - ts
    if age < 60 { return "just now" }
    if age < 3600 { return "\(age / 60)m ago" }
    return "\(age / 3600)h ago"
}

func linkIcon(_ symbol: String, _ url: String, label: String? = nil) -> some View {
    Button(action: { if let u = URL(string: url) { NSWorkspace.shared.open(u) } }) {
        Image(systemName: symbol).font(.caption)
    }
    .buttonStyle(.plain)
    .foregroundStyle(.secondary)
    .help(label ?? url)
    .accessibilityLabel(label ?? url)
}

func activityFirstSummary(
    period: UsagePeriod,
    activeHours: Double?,
    messages: Double?,
    tokens: Double?
) -> String {
    guard period == .day else { return "Quota unavailable" }
    var parts: [String] = []
    if let messages, messages > 0 {
        parts.append("\(Int(messages.rounded())) msgs")
    }
    if let tokens, tokens > 0 {
        parts.append("\(formatTokens(Int(tokens.rounded()))) tok")
    }
    if parts.count < 2, let activeHours, activeHours > 0 {
        parts.append(String(format: "%.1fh active", activeHours))
    }
    return parts.isEmpty ? "No activity today" : parts.joined(separator: " · ")
}

func formatTokens(_ n: Int) -> String {
    if n >= 1_000_000 { return String(format: "%.1fM", Double(n) / 1_000_000) }
    if n >= 1_000 { return String(format: "%.1fk", Double(n) / 1_000) }
    return "\(n)"
}

func formatStat(_ value: Double) -> String {
    if value.rounded() == value {
        return String(format: "%.0f", value)
    }
    return String(format: "%.1f", value)
}

func formatCreditAmount(_ value: Double) -> String {
    var text = String(format: "%.3f", value)
    while text.last == "0" {
        text.removeLast()
    }
    if text.last == "." {
        text.removeLast()
    }
    return text
}

func formatGapDuration(_ seconds: Int?) -> String? {
    guard let seconds, seconds > 0 else { return nil }
    if seconds >= 3600 {
        return String(format: "%.1fh", Double(seconds) / 3600)
    }
    if seconds >= 60 {
        return "\(seconds / 60)m"
    }
    return "\(seconds)s"
}
