import AppKit
import SwiftUI

/// Host-side capabilities the window may use; defaults keep isolated previews inert.
struct WorkbenchActions {
    var openStatusFolder: () -> Void = {}
    var requestPermission: () -> Void = {}
    var startupEnabled: () -> Bool? = { nil }
    var setStartup: (Bool) throws -> Void = { _ in }
    var officeSetup: OfficeCursorSetupModel? = nil
}

final class WorkbenchWindowController: NSWindowController, NSWindowDelegate {
    let model: WorkbenchModel
    private var screenObserver: NSObjectProtocol?
    init(directory: URL, actions: WorkbenchActions = WorkbenchActions(),
         initialGeneralPage: Bool = false, sendCommand: @escaping ([String: Any]) throws -> Void) {
        model = WorkbenchModel(directory: directory, sendCommand: sendCommand)
        let hosting = NSHostingController(rootView: DeviceWorkbenchView(model: model, actions: actions, initialGeneralPage: initialGeneralPage))
        hosting.sizingOptions = []
        let window = NSWindow(contentViewController: hosting)
        window.styleMask = [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]
        window.title = StatusCopy.product
        window.toolbarStyle = .unified
        window.setContentSize(NSSize(width: 940, height: 660))
        window.minSize = NSSize(width: 520, height: 360)
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("ScreenRelayWorkbench")
        super.init(window: window)
        window.delegate = self
        if window.frameAutosaveName.isEmpty || !window.setFrameUsingName("ScreenRelayWorkbench") { window.center() }
        fitToScreen()
        screenObserver = NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification,
            object: nil, queue: .main) { [weak self] _ in
                guard self?.window?.isVisible == true else { return }; self?.fitToScreen()
            }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    deinit { if let screenObserver { NotificationCenter.default.removeObserver(screenObserver) } }
    func show() {
        model.resume()
        fitToScreen()
        showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
    func windowWillClose(_ notification: Notification) { model.suspend() }
    func windowDidChangeScreen(_ notification: Notification) { fitToScreen() }
    private func fitToScreen() {
        guard let window, let screen = window.screen ?? NSScreen.main else { return }
        let visible = screen.visibleFrame
        window.minSize = WorkbenchWindowLayout.minimumSize(visible: visible.size)
        let frame = WorkbenchWindowLayout.fitting(window.frame, into: visible)
        if frame != window.frame { window.setFrame(frame, display: true) }
    }
}

private enum Page: Hashable { case device(String), general, diagnostics }

private struct DeviceWorkbenchView: View {
    @ObservedObject var model: WorkbenchModel
    let actions: WorkbenchActions
    @State private var page: Page?
    @State private var bindingConfirmation: BindingRequest?
    @State private var columns: NavigationSplitViewVisibility = .all

    init(model: WorkbenchModel, actions: WorkbenchActions, initialGeneralPage: Bool = false) {
        self.model = model
        self.actions = actions
        _page = State(initialValue: initialGeneralPage ? .general : nil)
    }

    var body: some View {
        GeometryReader { geometry in
            content(compact: geometry.size.width < 760, short: geometry.size.height < 500)
                .onChange(of: geometry.size.width < 760, initial: true) { _, compact in
                    columns = compact ? .detailOnly : .all
                }
        }
    }
    private func content(compact: Bool, short: Bool) -> some View {
        NavigationSplitView(columnVisibility: $columns) {
            sidebar.navigationSplitViewColumnWidth(min: 200, ideal: 220, max: 260)
        } detail: {
            switch currentPage {
            case .general: GeneralPage(model: model, actions: actions)
            case .diagnostics: DiagnosticsPage(model: model, actions: actions)
            case .device:
                if model.selection.isEmpty {
                    emptyState
                } else {
                    DevicePage(model: model, actions: actions, bindingConfirmation: $bindingConfirmation,
                               compact: compact, short: short)
                }
            }
        }
        .toolbar {
            if compact {
                ToolbarItem(placement: .navigation) {
                    Menu {
                        Section("设备") {
                            ForEach(model.profiles) { item in
                                Button(item.label) { model.selection = item.id; page = .device(item.id) }
                                    .disabled(model.busy && model.selection != item.id)
                            }
                            ForEach(model.unsavedKeys, id: \.self) { key in
                                Button((model.drafts[key]?.label ?? "新设备") + "（未保存）") {
                                    model.selection = key; page = .device(key)
                                }
                                .disabled(model.busy && model.selection != key)
                            }
                            Button("添加设备…") { model.addDevice(); page = .device(model.selection) }
                                .disabled(model.busy || !model.statusReady)
                        }
                        Divider()
                        Button("连接与启动") { page = .general }
                        Button("诊断") { page = .diagnostics }
                    } label: { Label("设备与设置", systemImage: "sidebar.left") }
                    .labelStyle(.iconOnly)
                    .help("选择设备或打开设置")
                }
            }
            if #available(macOS 26.0, *) {
                ToolbarItem(placement: .primaryAction) { statusItem }.sharedBackgroundVisibility(.hidden)
            } else {
                ToolbarItem(placement: .primaryAction) { statusItem }
            }
            ToolbarItem(placement: .primaryAction) { automationControl }
            if #available(macOS 26.0, *) {
                ToolbarSpacer(.fixed, placement: .primaryAction)
            }
            ToolbarItem(placement: .primaryAction) {
                Button { model.core("recover") } label: {
                    Label("恢复实体屏", systemImage: "display")
                }
                .labelStyle(.titleAndIcon)
                .help("立即恢复实体屏并暂停自动接力（⌃⌥⌘9）")
            }
        }
        .alert(item: $bindingConfirmation) { request in
            if request.action == "profile_unbind" {
                return Alert(title: Text("解除这条绑定？"),
                    message: Text("下次连接时将重新按型号匹配，当前画面不变。"),
                    primaryButton: .destructive(Text("解除绑定")) { model.submitBinding(request) },
                    secondaryButton: .cancel(Text("取消")))
            }
            return Alert(title: Text("改绑到「\(model.profileLabel(request.profileID))」？"),
                message: Text("「\(request.name)」目前使用「\(model.profileLabel(request.expectedProfileID ?? ""))」，下次连接起改用新配置。"),
                primaryButton: .default(Text("改绑")) { model.submitBinding(request) },
                secondaryButton: .cancel(Text("取消")))
        }
    }

    private var automationControl: some View {
        Toggle(isOn: Binding(get: { model.enabled }, set: { model.core($0 ? "enable" : "pause") })) {
            Label("自动接力", systemImage: "arrow.triangle.2.circlepath")
        }
        .toggleStyle(.button)
        .labelStyle(.titleAndIcon)
        .disabled(model.busy || !model.statusReady)
        .help(model.enabled ? "自动接力已开启，点击暂停" : "自动接力已暂停，点击开启")
    }

    private var statusItem: some View {
        HStack(spacing: 6) {
            Circle().fill(model.statusTint).frame(width: 7, height: 7)
            Text(model.statusText).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 6)
        .help(model.presentation.detail)
    }

    private var currentPage: Page {
        switch page {
        case .general?: return .general
        case .diagnostics?: return .diagnostics
        default: return .device(model.selection)
        }
    }

    private var sidebar: some View {
        List(selection: Binding<Page?>(
            get: { currentPage == .general || currentPage == .diagnostics ? currentPage : (model.selection.isEmpty ? nil : .device(model.selection)) },
            set: { value in
                guard let value else { return }
                if case .device(let id) = value {
                    guard !model.busy || id == model.selection else { return }
                    model.selection = id
                }
                page = value
            })) {
            Section("设备") {
                ForEach(model.profiles) { item in
                    DeviceRow(icon: item.symbol,
                              title: item.label, subtitle: subtitle(for: item))
                        .tag(Page.device(item.id))
                }
                ForEach(model.unsavedKeys, id: \.self) { key in
                    DeviceRow(icon: "plus.rectangle", title: model.drafts[key]?.label ?? "新设备",
                              subtitle: ("未保存", TextInk.neutral))
                        .tag(Page.device(key))
                }
            }
            Section("通用") {
                Label("连接与启动", systemImage: "gearshape").tag(Page.general)
                Label("诊断", systemImage: "stethoscope").tag(Page.diagnostics)
            }
        }
        .listStyle(.sidebar)
        // Keep connection-status colours readable while the sidebar is focused.
        .tint(Color(nsColor: .unemphasizedSelectedContentBackgroundColor))
        .accentColor(Color(nsColor: .unemphasizedSelectedContentBackgroundColor))
        .safeAreaInset(edge: .bottom) {
            HStack {
                Button { model.addDevice(); page = .device(model.selection) } label: {
                    Label("添加设备", systemImage: "plus")
                }
                .buttonStyle(.borderless)
                .disabled(model.busy || !model.statusReady)
                Spacer()
            }
            .padding(.horizontal, 14).padding(.vertical, 10)
        }
    }

    private func subtitle(for item: WorkbenchDevice) -> (String, Color) {
        if item.id == model.activeID && model.connected { return ("远程中", TextInk.connected) }
        if let draft = model.drafts[item.id], draft != WorkbenchDraft(item) { return ("未应用的更改", TextInk.neutral) }
        let spec = (item.orientation == "landscape" ? "横屏 · " : "竖屏 · ") + Format.size(item.resolution)
        return (spec, .secondary)
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "display").font(.system(size: 34, weight: .light)).foregroundStyle(.secondary)
            Text(model.statusReady ? "添加一台设备开始使用" : "正在等待后台…").foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct DeviceRow: View {
    let icon: String
    let title: String
    let subtitle: (String, Color)
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon).font(.system(size: 15)).frame(width: 20).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).foregroundStyle(Color(nsColor: .labelColor)).lineLimit(1)
                Text(subtitle.0).font(.caption).foregroundStyle(subtitle.1).lineLimit(1)
            }
        }
        .padding(.vertical, 3)
    }
}

// MARK: - Device page

private struct DevicePage: View {
    @ObservedObject var model: WorkbenchModel
    let actions: WorkbenchActions
    @Binding var bindingConfirmation: BindingRequest?
    let compact: Bool
    let short: Bool

    private func binding<T>(_ key: WritableKeyPath<WorkbenchDraft, T>) -> Binding<T> {
        Binding(get: { model.draft[keyPath: key] }, set: { value in model.update { $0[keyPath: key] = value } })
    }

    var body: some View {
        VStack(spacing: 0) {
            if let banner = model.banner {
                BannerView(banner: banner, action: actions.requestPermission)
                    .padding(.horizontal, 20).padding(.top, 14)
            }
            DeviceStage(model: model, compact: compact, short: short)
                .padding(.horizontal, 20).padding(.top, 14)
            Form {
                if model.existing == nil { deviceSection }
                pictureSection
                pointerSection
                if model.existing != nil {
                    bindingSection
                    deviceSection
                }
            }
            .formStyle(.grouped)
            .disabled(model.busy)
        }
        .navigationTitle(model.draft.label.isEmpty ? "新设备" : model.draft.label)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if model.showsChangeBar || model.toast != nil { ChangeBar(model: model, compact: compact) }
        }
        .animation(.easeOut(duration: 0.18), value: model.showsChangeBar)
        .animation(.easeOut(duration: 0.18), value: model.toast)
    }

    private var pictureSection: some View {
        Section("画面") {
            if let variants = model.draft.displayVariants, variants.count > 1 {
                LabeledContent("屏幕") {
                    Picker("屏幕", selection: Binding(
                        get: { model.draft.displayVariant ?? variants[0].id },
                        set: { model.selectDisplayVariant($0) })) {
                        ForEach(variants) { variant in
                            Text(variant.label).tag(variant.id)
                        }
                    }
                    .pickerStyle(.segmented).labelsHidden().frame(width: 150)
                }
            }
            LabeledContent("方向") {
                Picker("方向", selection: Binding(get: { model.draft.orientation },
                                                 set: { value in model.update { $0.rotate(to: value) } })) {
                    Text("横屏").tag("landscape"); Text("竖屏").tag("portrait")
                }
                .pickerStyle(.segmented).labelsHidden().frame(width: 150)
            }
            if model.draft.supportsDisplayArea {
                LabeledContent("显示范围") {
                    Picker("显示范围", selection: Binding(get: { model.draft.displayAreaMode },
                        set: { value in model.update { $0.setDisplayAreaMode(value) } })) {
                        Text("完整屏幕").tag("full")
                        Text("避让刘海与灵动岛").tag("safe")
                    }
                    .pickerStyle(.menu).labelsHidden()
                    .help(displayAreaHelp)
                }
                if model.draft.displayAreaIsSafe {
                    LabeledContent("每边留白") {
                        HStack(spacing: 10) {
                            Slider(value: Binding(get: { model.draft.displayAreaInset },
                                set: { value in model.update { $0.setDisplayAreaInset(value) } }),
                                   in: 0...12, step: 0.5)
                                .frame(maxWidth: compact ? 120 : 200)
                                .accessibilityLabel("每边留白")
                                .accessibilityValue("\(Format.number(model.draft.displayAreaInset))%")
                            Text("\(Format.number(model.draft.displayAreaInset))%")
                                .monospacedDigit().foregroundStyle(.secondary)
                                .frame(width: 42, alignment: .trailing)
                        }
                        .help(displayAreaHelp)
                    }
                }
            }
            LabeledContent("显示大小") {
                Text(model.draft.resolution.isEmpty ? "—" : Format.size(model.draft.resolution))
                    .monospacedDigit().foregroundStyle(.secondary)
            }
            if model.scaleOptions.isEmpty {
                Text("先选择型号或填写原生分辨率").foregroundStyle(.secondary)
            } else {
                ScaleTilePicker(options: model.scaleOptions, selection: binding(\.resolution), compact: compact)
                    .padding(.vertical, 4)
            }
        }
    }

    private var displayAreaHelp: String {
        let orientation = model.draft.orientation == "landscape" ? "横屏" : "竖屏"
        let edges = model.draft.orientation == "landscape" ? "左右" : "上下"
        if model.draft.displayAreaIsSafe {
            return "\(orientation)：\(edges)每边留白 \(Format.number(model.draft.displayAreaInset))%，桌面居中显示。横竖屏分别保存。"
        }
        return "\(orientation)：桌面使用完整屏幕；选择避让后，\(edges)每边留白 \(Format.number(model.draft.displayAreaInset))%。横竖屏分别保存。"
    }

    private var pointerSection: some View {
        Section("指针") {
            LabeledContent("远程光标") {
                if compact {
                    cursorPicker.pickerStyle(.menu).labelsHidden().frame(width: 110)
                } else {
                    cursorPicker.pickerStyle(.segmented).labelsHidden().frame(width: 340)
                }
            }
        }
    }
    private var cursorPicker: some View {
        Picker("远程光标", selection: binding(\.cursor)) {
            ForEach(CursorSizeMenu.scales, id: \.self) { Text(Format.scale($0)).tag($0) }
        }
    }

    @ViewBuilder private var bindingSection: some View {
        Section {
            ForEach(model.selectedBindings) { item in
                LabeledContent {
                    Button("解除绑定…") { bindingConfirmation = model.unbindingRequest(item) }
                        .disabled(model.busy || !model.statusReady)
                } label: {
                    Text(item.name).textSelection(.enabled)
                    if model.currentBinding?.key == item.key { Text("当前连接") }
                }
            }
            if model.currentBinding?.profileID != model.selection, model.controllers.count == 1 {
                LabeledContent {
                    Button(model.currentBinding == nil ? "绑定" : "改绑…") {
                        guard let request = model.bindingRequest() else { return }
                        if request.expectedProfileID == nil { model.submitBinding(request) }
                        else { bindingConfirmation = request }
                    }
                    .disabled(!model.canBindCurrent)
                } label: {
                    Text(model.controllers[0]).textSelection(.enabled)
                    Text(model.currentBinding.map { "当前连接 · 使用「\(model.profileLabel($0.profileID))」" } ?? "当前连接 · 未绑定")
                }
            }
            if model.selectedBindings.isEmpty && model.controllers.count != 1 {
                Text("未绑定").foregroundStyle(.secondary)
            }
        } header: {
            Text("连接绑定")
        } footer: {
            if model.controllers.count > 1 { Text("多台设备同时连接时无法绑定。").foregroundStyle(.secondary) }
        }
    }

    @ViewBuilder private var deviceSection: some View {
        Section("设备") {
            TextField("名称", text: binding(\.label))
            if model.existing == nil {
                Picker("型号", selection: Binding(get: { model.draft.catalogID }, set: { model.selectCatalog($0) })) {
                    Section("iPhone") { ForEach(model.catalog.filter(\.isPhone)) { Text($0.label).tag($0.id) } }
                    Section("iPad") { ForEach(model.catalog.filter { !$0.isPhone }) { Text($0.label).tag($0.id) } }
                    Divider()
                    Text("自定义…").tag("")
                }
                if model.draft.catalogID.isEmpty {
                    numericField("原生宽度", key: \.width)
                    numericField("原生高度", key: \.height)
                    numericField("PPI", key: \.ppi)
                }
            } else {
                LabeledContent("原生分辨率") {
                    Text("\(model.draft.width) × \(model.draft.height) · \(model.draft.ppi) PPI")
                        .monospacedDigit().foregroundStyle(.secondary)
                }
            }
        }
    }

    private func numericField(_ label: String, key: WritableKeyPath<WorkbenchDraft, String>) -> some View {
        TextField(label, text: Binding(get: { model.draft[keyPath: key] }, set: { value in
            model.update { $0[keyPath: key] = value }; model.chooseDefaultResolution()
        }))
        .monospacedDigit()
    }
}

/// The only branded surface: the device at its real aspect ratio.
private struct DeviceStage: View {
    @ObservedObject var model: WorkbenchModel
    let compact: Bool
    let short: Bool

    var body: some View {
        HStack(spacing: 24) {
            GeometryReader { geometry in
                ZStack {
                    if let size = draftSize {
                        DeviceFrame(width: size.0, height: size.1, style: model.previewIsDraft ? .draft : .solid,
                                    fit: geometry.size, canvasWidth: model.draft.canvasPixels?.0,
                                    canvasHeight: model.draft.canvasPixels?.1, hardware: draftHardware) {
                            VStack(spacing: 3) {
                                if let logical = model.draft.pixels {
                                    if logical.0 < logical.1 {
                                        VStack(spacing: 0) {
                                            Text(String(logical.0))
                                            Text("×").font(.system(size: 9))
                                            Text(String(logical.1))
                                        }
                                        .font(.system(size: 12, weight: .semibold, design: .rounded)).monospacedDigit()
                                    } else {
                                        Text(Format.size(logical.0, logical.1))
                                            .font(.system(size: 15, weight: .semibold, design: .rounded)).monospacedDigit()
                                            .lineLimit(1).minimumScaleFactor(0.6)
                                    }
                                }
                                Text("HiDPI").font(.system(size: 10, weight: .medium)).opacity(0.6)
                            }
                            .foregroundStyle(.white)
                            .padding(.horizontal, 8)
                        }
                    } else {
                        Image(systemName: "display").font(.system(size: 36, weight: .ultraLight))
                            .foregroundStyle(.white.opacity(0.5))
                    }
                    if let live = liveDraft, let physical = live.physicalPixels ?? live.pixels, model.previewIsDraft {
                        DeviceFrame(width: physical.0, height: physical.1, style: .ghost, fit: geometry.size,
                                    canvasWidth: live.canvasPixels?.0, canvasHeight: live.canvasPixels?.1,
                                    hardware: live.frameHardware) { EmptyView() }
                            .allowsHitTesting(false)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .animation(.spring(response: 0.35, dampingFraction: 0.85), value: model.draft.orientation)
                .animation(.spring(response: 0.35, dampingFraction: 0.85), value: model.draft.resolution)
                .animation(.easeOut(duration: 0.18), value: model.draft.displayAreaMode)
                .animation(.easeOut(duration: 0.18), value: model.draft.displayAreaInset)
            }
            .frame(width: compact ? 110 : 210)

            VStack(alignment: .leading, spacing: 6) {
                Text(model.draft.label.isEmpty ? "新设备" : model.draft.label)
                    .font(.title2.weight(.semibold)).foregroundStyle(.white).lineLimit(1)
                if let (w, h) = model.draft.pixels {
                    Text("渲染 \(Format.size(w * 2, h * 2))").monospacedDigit()
                        .foregroundStyle(.white.opacity(0.65))
                }
                if let tag {
                    Text(tag.0)
                        .font(.caption.weight(.medium))
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(tag.1.opacity(0.22), in: Capsule())
                        .foregroundStyle(tag.1)
                        .padding(.top, 4)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, compact ? 14 : 22).padding(.vertical, short ? 10 : 18)
        .frame(height: short ? 104 : 158)
        .background(Brand.stage, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private var draftHardware: DeviceHardware {
        if model.draft.catalogID.isEmpty, let existing = model.existing { return existing.frameHardware }
        return model.draft.frameHardware
    }
    private var draftSize: (Int, Int)? { model.draft.physicalPixels ?? model.draft.pixels }
    private var liveDraft: WorkbenchDraft? {
        guard model.connected, let active = model.profiles.first(where: { $0.id == model.activeID }) else { return nil }
        return WorkbenchDraft(active)
    }
    private var tag: (String, Color)? {
        if model.dirty { return ("待应用", Brand.cyan) }
        if model.connected && model.activeID == model.selection { return ("正在使用", Brand.cyan) }
        if model.existing != nil && model.selection == model.savedSelectionID { return ("下次使用", .white) }
        return nil
    }
}

struct BannerView: View {
    let banner: WorkbenchBanner
    var action: () -> Void = {}
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: banner.warning ? "exclamationmark.triangle.fill" : "info.circle")
                .foregroundStyle(banner.warning ? Color.orange : Color.secondary)
            Text(banner.text).lineLimit(2).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            if banner.needsPermission { Button("前往授权", action: action).controlSize(.small) }
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
        .background((banner.warning ? Color.orange.opacity(0.12) : Color.secondary.opacity(0.1)),
                    in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

private struct ChangeBar: View {
    @ObservedObject var model: WorkbenchModel
    let compact: Bool
    var body: some View {
        VStack(spacing: 0) {
            Divider()
            if compact {
                VStack(alignment: .leading, spacing: 8) {
                    summary
                    HStack { Spacer(); buttons }
                }
                .padding(.horizontal, 16).padding(.vertical, 10)
            } else {
                HStack(spacing: 10) {
                    if model.busy { ProgressView().controlSize(.small) }
                    summary
                    Spacer(minLength: 12)
                    buttons
                }
                .padding(.horizontal, 20).frame(height: 50)
            }
        }
        .background(.bar)
        .transition(.move(edge: .bottom).combined(with: .opacity))
    }
    @ViewBuilder private var buttons: some View {
        if model.showsChangeBar {
            if model.dirty {
                Button(model.existing == nil ? "取消" : "还原") { model.discard() }.disabled(model.busy)
            }
            Button(model.applyTitle) { model.apply() }
                .buttonStyle(.borderedProminent).disabled(!model.canApply)
                .keyboardShortcut(.return, modifiers: .command)
        }
    }

    @ViewBuilder private var summary: some View {
        if model.busy {
            Text(model.statusText).foregroundStyle(.secondary)
        } else if let failure = model.failure {
            Label {
                Text(failure).foregroundStyle(.primary).lineLimit(1)
            } icon: {
                Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
            }
        } else if model.dirty, let validation = model.draft.validation {
            Text(validation).foregroundStyle(.secondary).lineLimit(1)
        } else if model.showsChangeBar {
            HStack(spacing: 8) {
                Text(model.dirty ? model.pendingChanges.joined(separator: "、") : model.draftStatus).lineLimit(1)
                if model.requiresReconnect {
                    Label("远程画面会短暂重连", systemImage: "arrow.triangle.2.circlepath")
                        .foregroundStyle(.secondary).lineLimit(1)
                }
            }
        } else if let toast = model.toast {
            Label(toast, systemImage: "checkmark.circle.fill").foregroundStyle(.secondary).lineLimit(1)
        }
    }
}

// MARK: - General & diagnostics

private struct GeneralPage: View {
    @ObservedObject var model: WorkbenchModel
    let actions: WorkbenchActions
    @State private var startup: Bool?
    @State private var startupError: String?

    var body: some View {
        Form {
            Section {
                Toggle("按 UU 设备名称自动选择配置", isOn: Binding(get: { model.automatic }, set: { model.setAutomatic($0) }))
                    .disabled(model.busy || !model.statusReady)
            } header: { Text("连接") } footer: {
                Text("关闭后始终使用标记为「下次使用」的配置。").foregroundStyle(.secondary)
            }
            Section("启动") {
                Toggle("开机自启", isOn: Binding(get: { startup ?? false }, set: { value in
                    do { try actions.setStartup(value); startup = actions.startupEnabled(); startupError = nil }
                    catch { startupError = error.localizedDescription }
                }))
                .disabled(startup == nil)
                if let startupError {
                    Label {
                        Text(startupError).foregroundStyle(.primary)
                    } icon: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(.red)
                    }
                }
            }
            if let officeSetup = actions.officeSetup {
                OfficeCursorSetupSection(model: officeSetup)
            }
            Section("权限与快捷键") {
                LabeledContent("辅助功能") {
                    if model.trusted { Text("已授权").foregroundStyle(.secondary) }
                    Button(model.trusted ? "检查设置…" : "前往授权", action: actions.requestPermission)
                }
                LabeledContent("紧急恢复并暂停") { Text("⌃⌥⌘9").foregroundStyle(.secondary) }
            }
        }
        .formStyle(.grouped)
        .navigationTitle("连接与启动")
        .onAppear { startup = actions.startupEnabled(); actions.officeSetup?.begin() }
        .onDisappear { actions.officeSetup?.end() }
        .onReceive(NotificationCenter.default.publisher(for: LoginAtStartup.didChange)) { _ in startup = actions.startupEnabled() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            startup = actions.startupEnabled(); actions.officeSetup?.refresh()
        }
    }
}

private struct DiagnosticsPage: View {
    @ObservedObject var model: WorkbenchModel
    let actions: WorkbenchActions
    private static let time: DateFormatter = {
        let formatter = DateFormatter(); formatter.dateFormat = "HH:mm:ss"; return formatter
    }()

    var body: some View {
        Form {
            Section("状态") {
                LabeledContent("自动接力", value: StatusCopy.modeLabel(model.mode))
                LabeledContent("UU 连接", value: StatusCopy.connectionLabel(model.connection))
                LabeledContent("控制端", value: model.controllers.isEmpty ? "无" : model.controllers.joined(separator: "、"))
                LabeledContent("识别", value: model.recognition)
            }
            Section("最近操作") {
                if model.receipts.isEmpty { Text("暂无").foregroundStyle(.secondary) }
                ForEach(model.receipts.suffix(8).reversed()) { receipt in
                    LabeledContent {
                        Text(receipt.timestamp.map { Self.time.string(from: Date(timeIntervalSince1970: $0)) } ?? "")
                            .monospacedDigit().foregroundStyle(.secondary)
                    } label: {
                        Label(StatusCopy.localize(receipt.message) ?? receipt.message, systemImage: icon(receipt.status))
                    }
                }
            }
            if let raw = model.backendError {
                Section("后台原始信息") {
                    Text(raw).font(.callout.monospaced()).textSelection(.enabled)
                }
            }
            Section {
                Button("打开状态文件夹", action: actions.openStatusFolder)
            }
        }
        .formStyle(.grouped)
        .navigationTitle("诊断")
    }

    private func icon(_ status: String) -> String {
        switch status {
        case "ok": return "checkmark.circle"
        case "error": return "xmark.circle"
        default: return "clock"
        }
    }
}
