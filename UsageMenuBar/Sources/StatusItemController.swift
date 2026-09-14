import AppKit
import Combine
import SwiftUI

@MainActor
final class UsageMenuBarApplicationDelegate: NSObject, NSApplicationDelegate {
    private let viewModel = UsageViewModel()
    private var statusItemController: UsageStatusItemController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        statusItemController = UsageStatusItemController(viewModel: viewModel)
    }
}

@MainActor
final class UsageStatusItemController: NSObject, NSPopoverDelegate {
    private static let itemWidth: CGFloat = 44

    private let viewModel: UsageViewModel
    private let statusItem: NSStatusItem
    private let popover = NSPopover()
    private var iconCancellable: AnyCancellable?
    private var lastPopoverClose = Date.distantPast

    init(viewModel: UsageViewModel) {
        self.viewModel = viewModel
        statusItem = NSStatusBar.system.statusItem(withLength: Self.itemWidth)
        super.init()

        configureStatusItem()
        configurePopover()
        observeIconChanges()
        DispatchQueue.main.async { [weak self] in
            self?.refreshIcon()
        }
    }

    deinit {
        NSStatusBar.system.removeStatusItem(statusItem)
    }

    private func configureStatusItem() {
        guard let button = statusItem.button else { return }
        button.target = self
        button.action = #selector(togglePopover(_:))
        button.sendAction(on: [.leftMouseUp])
        button.imagePosition = .imageOnly
        button.toolTip = "Tool Usage"
        button.setAccessibilityLabel("Tool Usage")
        refreshIcon()
    }

    private func configurePopover() {
        let hostingController = NSHostingController(rootView: StatusPopoverContent(vm: viewModel))
        popover.contentViewController = hostingController
        popover.contentSize = NSSize(width: 340, height: 442)
        popover.behavior = .transient
        popover.animates = true
        popover.delegate = self
    }

    private func observeIconChanges() {
        iconCancellable = viewModel.objectWillChange.sink { [weak self] _ in
            DispatchQueue.main.async {
                self?.refreshIcon()
            }
        }
    }

    private func refreshIcon() {
        guard let button = statusItem.button else { return }
        if let image = MenuBarLabel(vm: viewModel).renderIcon() {
            button.title = ""
            button.image = image
        } else {
            button.title = ""
            button.image = NSImage(
                systemSymbolName: "chart.bar.xaxis",
                accessibilityDescription: "Tool Usage"
            )
        }
    }

    @objc private func togglePopover(_ sender: Any?) {
        if popover.isShown {
            popover.performClose(sender)
            return
        }
        guard Date().timeIntervalSince(lastPopoverClose) > 0.35,
              let button = statusItem.button else { return }

        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
    }

    func popoverDidClose(_ notification: Notification) {
        lastPopoverClose = Date()
    }
}

struct StatusPopoverContent: View {
    @ObservedObject var vm: UsageViewModel

    var body: some View {
        ScrollView(.vertical) {
            MenuContent(vm: vm)
        }
        .scrollIndicators(.never)
        .frame(width: 340, height: 442, alignment: .top)
        .background(Color(nsColor: .windowBackgroundColor))
    }
}
