import AppKit
import SwiftUI

/// Host-side capabilities for the menu-bar panel.
struct PopoverActions {
    var openWorkbench: () -> Void = {}
    var requestPermission: () -> Void = {}
    var quit: () -> Void = {}
    var startupEnabled: () -> Bool? = { nil }
    var setStartup: (Bool) throws -> Void = { _ in }
}

/// Owns the panel only while it is shown: the view tree and its directory
/// observer are created on open and released on close, so idle cost is zero.
final class StatusPopoverController: NSObject, NSPopoverDelegate {
    private let directory: URL
    private let sendCommand: ([String: Any]) throws -> Void
    private let actions: PopoverActions
    private var popover: NSPopover?
    private var model: WorkbenchModel?

    init(directory: URL, actions: PopoverActions, sendCommand: @escaping ([String: Any]) throws -> Void) {
        self.directory = directory; self.actions = actions; self.sendCommand = sendCommand
    }

    var isShown: Bool { popover?.isShown == true }

    func toggle(relativeTo button: NSStatusBarButton) {
        if let popover, popover.isShown { popover.performClose(nil); return }
        let model = WorkbenchModel(directory: directory, sendCommand: sendCommand)
        var actions = self.actions
        let open = actions.openWorkbench
        actions.openWorkbench = { [weak self] in self?.popover?.performClose(nil); open() }
        let hosting = NSHostingController(rootView: StatusPopoverView(model: model, actions: actions))
        hosting.sizingOptions = .preferredContentSize
        let popover = NSPopover()
        popover.behavior = .transient
        popover.animates = true
        popover.delegate = self
        popover.contentViewController = hosting
        self.model = model; self.popover = popover
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
    }

    func close() { popover?.performClose(nil) }

    func popoverDidClose(_ notification: Notification) {
        model?.suspend(); model = nil
        popover?.contentViewController = nil; popover = nil
    }
}

struct StatusPopoverView: View {
    @ObservedObject var model: WorkbenchModel
    let actions: PopoverActions
    @State private var cursor: Double = 3
    @State private var startup: Bool?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header.padding(.horizontal, 16).padding(.top, 14).padding(.bottom, 12)
            deviceCard.padding(.horizontal, 12)
            controls.padding(.horizontal, 16).padding(.vertical, 12)
            if let banner = model.banner {
                BannerView(banner: banner, action: actions.requestPermission)
                    .font(.callout).padding(.horizontal, 12).padding(.bottom, 10)
            }
            Divider().padding(.horizontal, 12)
            VStack(spacing: 2) {
                PopoverRow(title: "恢复实体屏", icon: "display", trailing: "⌃⌥⌘9") { model.core("recover") }
                PopoverRow(title: "设备工作台…", icon: "slider.horizontal.3", action: actions.openWorkbench)
            }
            .padding(.horizontal, 6).padding(.vertical, 6)
            Divider().padding(.horizontal, 12)
            footer.padding(.horizontal, 16).padding(.vertical, 10)
        }
        .frame(width: 300)
        .onAppear { syncCursor(); startup = actions.startupEnabled() }
        .onChange(of: model.currentDevice?.cursor) { syncCursor() }
        .onChange(of: model.currentDevice?.id) { syncCursor() }
        .onChange(of: model.pendingID) { if model.pendingID == nil { syncCursor() } }
        .onReceive(NotificationCenter.default.publisher(for: LoginAtStartup.didChange)) { _ in startup = actions.startupEnabled() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in startup = actions.startupEnabled() }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text(StatusCopy.product).font(.headline)
            Spacer()
            Circle().fill(model.statusTint).frame(width: 7, height: 7)
            Text(model.statusText).font(.callout).foregroundStyle(.secondary)
        }
        .help(model.presentation.detail)
    }

    private var deviceCard: some View {
        HStack(spacing: 14) {
            if let device = model.currentDevice, let size = WorkbenchDraft(device).physicalPixels {
                let canvas = WorkbenchDraft(device).canvasPixels
                DeviceFrame(width: size.0, height: size.1, fit: CGSize(width: 64, height: 46),
                            canvasWidth: canvas?.0, canvasHeight: canvas?.1,
                            hardware: device.frameHardware) { EmptyView() }
                    .frame(width: 64, height: 46)
                VStack(alignment: .leading, spacing: 3) {
                    Text(device.label).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                    Text((device.orientation == "landscape" ? "横屏 · " : "竖屏 · ") + Format.size(device.resolution))
                        .font(.system(size: 11)).monospacedDigit().opacity(0.65)
                }
                .foregroundStyle(.white)
            } else {
                Image(systemName: "display").font(.system(size: 26, weight: .light)).foregroundStyle(.white.opacity(0.6))
                Text(model.statusReady ? "尚未添加设备" : "正在等待后台…").font(.callout).foregroundStyle(.white.opacity(0.75))
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14).frame(height: 70)
        .background(Brand.stage, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private var controls: some View {
        VStack(spacing: 12) {
            HStack {
                Text("自动接力")
                Spacer()
                Toggle("自动接力", isOn: Binding(get: { model.enabled }, set: { model.core($0 ? "enable" : "pause") }))
                    .toggleStyle(.switch).controlSize(.small).labelsHidden()
                    .disabled(model.busy || !model.statusReady)
            }
            VStack(spacing: 4) {
                HStack {
                    Text("远程光标")
                    Spacer()
                    Text(Format.scale(cursor)).monospacedDigit().foregroundStyle(.secondary)
                }
                Slider(value: $cursor, in: 1...4, step: 0.5) { editing in
                    guard !editing, cursor != model.currentDevice?.cursor else { return }
                    if !model.setCursor(cursor) { syncCursor() }
                }
                .labelsHidden()
                .disabled(model.currentDevice == nil || model.busy || !model.statusReady || model.storageBlocked)
            }
        }
    }

    private var footer: some View {
        HStack {
            Toggle("开机自启", isOn: Binding(get: { startup ?? false }, set: { value in
                try? actions.setStartup(value); startup = actions.startupEnabled()
            }))
            .toggleStyle(.checkbox).disabled(startup == nil)
            Spacer()
            Button("退出", action: actions.quit).buttonStyle(.borderless).foregroundStyle(.secondary)
                .help("恢复实体屏后退出")
        }
        .font(.callout)
    }

    private func syncCursor() { if let value = model.currentDevice?.cursor { cursor = value } }
}

private struct PopoverRow: View {
    let title: String
    let icon: String
    var trailing: String?
    let action: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: icon).frame(width: 18).foregroundStyle(.secondary)
                Text(title)
                Spacer()
                if let trailing { Text(trailing).foregroundStyle(TextInk.hint) }
            }
            .padding(.horizontal, 10).frame(height: 28)
            .background(hover ? Color.primary.opacity(0.08) : .clear, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
    }
}
