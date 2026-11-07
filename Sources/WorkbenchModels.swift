import Foundation
import CoreFoundation
import Combine
import SwiftUI

struct DisplayAreaPreference: Equatable, Codable {
    var mode = "full"
    var insetPercent = 7.0
    var valid: Bool {
        ["full", "safe"].contains(mode) && insetPercent.isFinite && (0...12).contains(insetPercent)
            && insetPercent * 2 == (insetPercent * 2).rounded()
    }
    var payload: [String: Any] { ["mode": mode, "inset_percent": insetPercent] }
}

struct DisplayAreaSettings: Equatable, Codable {
    var landscape = DisplayAreaPreference()
    var portrait = DisplayAreaPreference()
    init() {}
    init?(_ raw: [String: Any]) {
        guard Set(raw.keys).isSubset(of: ["landscape", "portrait"]) else { return nil }
        func read(_ key: String) -> DisplayAreaPreference? {
            guard let value = raw[key] else { return DisplayAreaPreference() }
            guard let value = value as? [String: Any],
                  Set(value.keys).isSubset(of: ["mode", "inset_percent"]) else { return nil }
            let mode: String
            if let rawMode = value["mode"] {
                guard let parsed = rawMode as? String else { return nil }; mode = parsed
            } else { mode = "full" }
            let inset: Double
            if let rawInset = value["inset_percent"] {
                guard let parsed = rawInset as? NSNumber, CFGetTypeID(parsed) != CFBooleanGetTypeID() else { return nil }
                inset = parsed.doubleValue
            } else { inset = 7 }
            let item = DisplayAreaPreference(mode: mode, insetPercent: inset)
            return item.valid ? item : nil
        }
        guard let landscape = read("landscape"), let portrait = read("portrait") else { return nil }
        self.landscape = landscape; self.portrait = portrait
    }
    func preference(_ orientation: String) -> DisplayAreaPreference { orientation == "portrait" ? portrait : landscape }
    mutating func set(_ preference: DisplayAreaPreference, for orientation: String) {
        if orientation == "portrait" { portrait = preference } else { landscape = preference }
    }
    var payload: [String: Any] { ["landscape": landscape.payload, "portrait": portrait.payload] }
    var valid: Bool { landscape.valid && portrait.valid }
}

struct WorkbenchDisplayVariant: Identifiable, Equatable, Codable {
    let id: String
    let label: String
    let width: Int
    let height: Int
    let ppi: Double
    let hardware: String
    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, let label = value["label"] as? String,
              let width = value["native_width"] as? Int, let height = value["native_height"] as? Int,
              let ppi = value["ppi"] as? Double, let hardware = value["hardware"] as? String,
              (64...16384).contains(width), (64...16384).contains(height),
              ppi.isFinite, (50...1000).contains(ppi) else { return nil }
        self.id = id; self.label = label; self.width = width; self.height = height
        self.ppi = ppi; self.hardware = hardware
    }
}

struct WorkbenchDevice: Identifiable {
    let id: String
    let label: String
    let catalogID: String?
    let width: Int
    let height: Int
    let ppi: Double
    let orientation: String
    let resolution: String
    let cursor: Double
    let presetResolutions: [String]
    let displayArea: DisplayAreaSettings?
    let hardwareKind: String?
    let displayVariants: [WorkbenchDisplayVariant]?
    let displayVariant: String?
    var isPhone: Bool { (catalogID ?? id).hasPrefix("iphone-") }
    var symbol: String {
        if isPhone { return orientation == "landscape" ? "iphone.gen3.landscape" : "iphone.gen3" }
        return orientation == "landscape" ? "ipad.landscape" : "ipad"
    }
    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, let label = value["label"] as? String,
              let width = value["native_width"] as? Int, let height = value["native_height"] as? Int,
              let ppi = value["ppi"] as? Double else { return nil }
        self.id = id; self.label = label; catalogID = value["catalog_id"] as? String
        self.width = width; self.height = height; self.ppi = ppi
        orientation = value["orientation"] as? String ?? "landscape"
        resolution = value["resolution"] as? String ?? ""
        cursor = value["cursor_scale"] as? Double ?? min(4, max(1.5, floor(3 * ppi / 264 * 2 + 0.5) / 2))
        presetResolutions = value["presets"] as? [String] ?? []
        hardwareKind = value["hardware"] as? String
        if let raw = value["display_variants"] {
            guard let raw = raw as? [[String: Any]] else { return nil }
            let variants = raw.compactMap(WorkbenchDisplayVariant.init)
            guard !variants.isEmpty, variants.count == raw.count, Set(variants.map(\.id)).count == variants.count else { return nil }
            let selected = value["display_variant"] as? String ?? variants[0].id
            guard variants.contains(where: { $0.id == selected }) else { return nil }
            displayVariants = variants; displayVariant = selected
        } else { displayVariants = nil; displayVariant = nil }
        if let raw = value["display_area"] {
            guard let raw = raw as? [String: Any], let settings = DisplayAreaSettings(raw) else { return nil }
            displayArea = settings
        } else { displayArea = nil }
    }
}

struct WorkbenchBinding: Identifiable {
    let key: String
    let name: String
    let profileID: String
    var id: String { key }
    init?(_ value: [String: Any]) {
        guard let key = value["key"] as? String, let name = value["name"] as? String,
              let profileID = value["profile_id"] as? String else { return nil }
        self.key = key; self.name = name; self.profileID = profileID
    }
}
struct WorkbenchReceipt: Identifiable {
    let id: String
    let status: String
    let message: String
    let timestamp: Double?
    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, let status = value["status"] as? String else { return nil }
        self.id = id; self.status = status
        message = value["message"] as? String ?? ""; timestamp = value["timestamp"] as? Double
    }
}
struct WorkbenchBanner: Equatable {
    var text: String
    var needsPermission = false
    var warning = true
}
struct BindingRequest: Identifiable {
    let id = UUID()
    let action: String
    let name: String
    let profileID: String
    let expectedProfileID: String?
}

struct WorkbenchDraft: Equatable, Codable {
    var label = "新设备"
    var catalogID = ""
    var width = ""
    var height = ""
    var ppi = ""
    var orientation = "landscape"
    var resolution = ""
    var cursor = 3.0
    var displayArea: DisplayAreaSettings?
    var hardwareKind: String?
    var displayVariants: [WorkbenchDisplayVariant]?
    var displayVariant: String?
    init() {}
    init(_ device: WorkbenchDevice) {
        label = device.label; catalogID = device.catalogID ?? ""
        width = String(device.width); height = String(device.height); ppi = Format.number(device.ppi)
        orientation = device.orientation; resolution = device.resolution; cursor = device.cursor
        displayArea = device.displayArea
        hardwareKind = device.hardwareKind
        displayVariants = device.displayVariants; displayVariant = device.displayVariant
    }
    var displayAreaMode: String { (displayArea ?? DisplayAreaSettings()).preference(orientation).mode }
    var displayAreaInset: Double { (displayArea ?? DisplayAreaSettings()).preference(orientation).insetPercent }
    var displayAreaIsSafe: Bool { displayAreaMode == "safe" }
    var supportsDisplayArea: Bool {
        (catalogID.hasPrefix("iphone-") && hardwareKind != "phone_home_button") || catalogID.isEmpty
            || displayArea?.landscape.mode == "safe" || displayArea?.portrait.mode == "safe"
    }
    var physicalPixels: (Int, Int)? {
        guard let w = Int(width), let h = Int(height), (64...16384).contains(w), (64...16384).contains(h),
              ["landscape", "portrait"].contains(orientation) else { return nil }
        return orientation == "landscape" ? (max(w, h), min(w, h)) : (min(w, h), max(w, h))
    }
    var canvasPixels: (Int, Int)? {
        guard let (w, h) = physicalPixels, (displayArea ?? DisplayAreaSettings()).valid else { return nil }
        guard displayAreaIsSafe else { return (w, h) }
        let factor = 1 - displayAreaInset / 50
        let cw = orientation == "landscape" ? Int(floor(Double(w) * factor / 2 + 0.5)) * 2 : w
        let ch = orientation == "portrait" ? Int(floor(Double(h) * factor / 2 + 0.5)) * 2 : h
        guard min(cw, ch) >= 64, orientation == "landscape" ? cw >= ch : ch >= cw else { return nil }
        return (cw, ch)
    }
    private mutating func fitResolution(shortEdge: Int?) {
        guard let (w, h) = canvasPixels, let shortEdge else { return }
        let short = min(shortEdge, Int((Double(min(w, h)) / 2).rounded(.toNearestOrEven)))
        let long = Int((Double(short) * Double(max(w, h)) / Double(min(w, h))).rounded(.toNearestOrEven))
        resolution = orientation == "landscape" ? "\(long)x\(short)" : "\(short)x\(long)"
    }
    mutating func selectDisplayVariant(_ id: String) {
        guard displayVariant != id, let variant = displayVariants?.first(where: { $0.id == id }) else { return }
        let relativeShort = pixels.flatMap { size in physicalPixels.map { Double(min(size.0, size.1)) / Double(min($0.0, $0.1)) } }
        displayVariant = id; width = String(variant.width); height = String(variant.height)
        ppi = Format.number(variant.ppi); hardwareKind = variant.hardware
        let short = relativeShort.map { Int(($0 * Double(min(variant.width, variant.height))).rounded(.toNearestOrEven)) }
        fitResolution(shortEdge: short)
    }
    mutating func setDisplayAreaMode(_ mode: String) {
        guard ["full", "safe"].contains(mode), mode != displayAreaMode else { return }
        let short = pixels.map { min($0.0, $0.1) }
        let before = canvasPixels
        var settings = displayArea ?? DisplayAreaSettings()
        var preference = settings.preference(orientation); preference.mode = mode
        settings.set(preference, for: orientation); displayArea = settings
        if before?.0 != canvasPixels?.0 || before?.1 != canvasPixels?.1 { fitResolution(shortEdge: short) }
    }
    mutating func setDisplayAreaInset(_ percent: Double) {
        guard percent.isFinite, (0...12).contains(percent), percent * 2 == (percent * 2).rounded() else { return }
        let short = pixels.map { min($0.0, $0.1) }
        let before = canvasPixels
        var settings = displayArea ?? DisplayAreaSettings()
        var preference = settings.preference(orientation); preference.insetPercent = percent
        settings.set(preference, for: orientation); displayArea = settings
        if displayAreaIsSafe, before?.0 != canvasPixels?.0 || before?.1 != canvasPixels?.1 { fitResolution(shortEdge: short) }
    }
    var pixels: (Int, Int)? {
        let parts = resolution.split(separator: "x")
        guard parts.count == 2, let w = Int(parts[0]), let h = Int(parts[1]), w > 0, h > 0 else { return nil }
        return (w, h)
    }
    var validation: String? {
        guard !label.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, label.count <= 100,
              !label.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else { return "请输入有效的设备名称（最多 100 字）。" }
        guard let w = Int(width), let h = Int(height), (64...16384).contains(w), (64...16384).contains(h),
              let density = Double(ppi), density.isFinite, (50...1000).contains(density) else { return "请填写原生宽高（64–16384）与 PPI（50–1000）。" }
        if let variants = displayVariants {
            guard let selected = variants.first(where: { $0.id == displayVariant }),
                  selected.width == w, selected.height == h, selected.ppi == density else { return "请选择有效的屏幕规格。" }
        } else if displayVariant != nil { return "请选择有效的屏幕规格。" }
        guard let (rw, rh) = pixels else { return "请选择逻辑分辨率。" }
        guard (displayArea ?? DisplayAreaSettings()).valid else { return "请选择有效的显示范围和留白比例。" }
        for direction in ["landscape", "portrait"] {
            let preference = (displayArea ?? DisplayAreaSettings()).preference(direction)
            if preference.mode == "safe" {
                let long = Int(floor(Double(max(w, h)) * (1 - preference.insetPercent / 50) / 2 + 0.5)) * 2
                guard long >= min(w, h), long >= 64 else {
                    return direction == "landscape" ? "请减小横屏留白，使桌面保持横向。" : "请减小竖屏留白，使桌面保持纵向。"
                }
            }
        }
        guard let (nw, nh) = canvasPixels else { return "请调整显示范围，使桌面保持当前方向。" }
        guard rw >= 16, rh >= 16, rw <= 8193, rh <= 8193,
              rw * 2 <= nw + 2, rh * 2 <= nh + 2,
              abs(Double(rw) / Double(rh) - Double(nw) / Double(nh)) <= 0.005,
              (orientation == "landscape" ? rw >= rh : rw <= rh) else { return "逻辑分辨率需符合显示范围比例及 HiDPI 像素范围。" }
        guard (1...4).contains(cursor), cursor.isFinite,
              cursor * 2 == (cursor * 2).rounded() else { return "光标倍率应为 1–4 倍，每档 0.5 倍。" }
        return nil
    }
    var payload: [String: Any] {
        var value: [String: Any] = ["label": label.trimmingCharacters(in: .whitespacesAndNewlines),
            "native_width": Int(width) ?? 0, "native_height": Int(height) ?? 0,
            "ppi": Double(ppi) ?? 0, "orientation": orientation, "resolution": resolution, "cursor_scale": cursor]
        if !catalogID.isEmpty { value["catalog_id"] = catalogID }
        if let displayArea { value["display_area"] = displayArea.payload }
        if let displayVariant { value["display_variant"] = displayVariant }
        return value
    }
    mutating func rotate(to value: String) {
        guard ["landscape", "portrait"].contains(value), orientation != value else { return }
        let old = pixels; let canvas = canvasPixels
        orientation = value
        if let old, let canvas, let next = canvasPixels, canvas.0 == next.1, canvas.1 == next.0 {
            resolution = "\(old.1)x\(old.0)"
        } else { fitResolution(shortEdge: old.map { min($0.0, $0.1) }) }
    }
}

final class WorkbenchModel: ObservableObject {
    private struct DraftHandoff: Codable { var selection: String; var drafts: [String: WorkbenchDraft] }
    @Published var profiles: [WorkbenchDevice] = []
    @Published var catalog: [WorkbenchDevice] = []
    @Published var selection = ""
    @Published var drafts: [String: WorkbenchDraft] = [:]
    @Published var message = "正在读取后台状态…"
    @Published var mode = ""
    @Published var enabled = false
    @Published var automatic = false
    @Published var controllers: [String] = []
    @Published var identityStatus = "none"
    @Published var identitySource = ""
    @Published var activeID: String?
    @Published var pendingID: String?
    @Published var statusReady = false
    @Published var bindings: [WorkbenchBinding] = []
    @Published var currentBinding: WorkbenchBinding?
    @Published var bindingAvailable = false
    @Published var backendError: String?
    @Published var connection = "unknown"
    @Published var trusted = true
    @Published var sleeping = false
    @Published var statusDetail: [String: Any] = [:]
    @Published var statusTimestamp: Double?
    @Published var receipts: [WorkbenchReceipt] = []
    /// Short-lived confirmation shown after a backend receipt; cleared by a one-shot work item.
    @Published var toast: String?
    /// The last failed request, kept beside the preserved draft until the next attempt.
    @Published var failure: String?
    private var toastWork: DispatchWorkItem?
    private var baselines: [String: WorkbenchDraft] = [:]
    private var presets: [String: [String]] = [:]
    private var savedSelection: String?
    private var pendingSelection: String?
    private var pendingDraft: WorkbenchDraft?
    private var observer: StatusFileObserver?
    private var storageObserver: NSObjectProtocol?
    private var nativeObserver: NSObjectProtocol?
    private var expiryWork: DispatchWorkItem?
    private var runtime = RuntimeStatusSnapshot.resolve(status: nil, workbench: nil, raw: nil, effective: nil, now: 0)
    private var timeout: DispatchWorkItem?
    private let directory: URL
    private let clock: () -> TimeInterval
    private let sender: ([String: Any]) throws -> Void
    private var auxiliaryIDs = Set<String>()

    init(directory: URL, clock: @escaping () -> TimeInterval = { Date().timeIntervalSince1970 },
         sendCommand: @escaping ([String: Any]) throws -> Void) {
        self.directory = directory; self.clock = clock; sender = sendCommand
        resume()
    }
    deinit {
        timeout?.cancel(); toastWork?.cancel(); expiryWork?.cancel()
        if let storageObserver { NotificationCenter.default.removeObserver(storageObserver) }
        if let nativeObserver { NotificationCenter.default.removeObserver(nativeObserver) }
    }
    /// Hidden windows stop observing so status writes cost nothing while closed.
    var observing: Bool { observer != nil }
    func suspend() {
        observer?.stop(); observer = nil
        if let storageObserver { NotificationCenter.default.removeObserver(storageObserver) }
        storageObserver = nil
        if let nativeObserver { NotificationCenter.default.removeObserver(nativeObserver) }
        nativeObserver = nil
        expiryWork?.cancel(); expiryWork = nil
    }
    func resume() {
        if observer == nil { observer = StatusFileObserver(directory: directory) { [weak self] in self?.reload() } }
        if storageObserver == nil {
            storageObserver = NotificationCenter.default.addObserver(forName: StorageHealthCenter.didChange,
                object: nil, queue: .main) { [weak self] _ in self?.reload() }
        }
        if nativeObserver == nil {
            nativeObserver = NotificationCenter.default.addObserver(forName: NativeStatusSignal.didChange,
                object: nil, queue: .main) { [weak self] _ in self?.reload() }
        }
        reload()
        if observer == nil { message = "无法观察后台状态目录；请确认后台已启动。" }
    }
    var draft: WorkbenchDraft { drafts[selection] ?? WorkbenchDraft() }
    /// The configuration the backend uses for the next connection when no name rule applies.
    var savedSelectionID: String? { savedSelection }
    var existing: WorkbenchDevice? { profiles.first { $0.id == selection } }
    var dirty: Bool { baselines[selection] != draft }
    var busy: Bool { pendingID != nil || runtime.pendingID != nil || mode.uppercased() == "RESTORING" || statusDetail["exit_after_restore"] is String }
    var connected: Bool { runtime.operational && mode.uppercased() == "ACTIVE" && enabled }
    var multipleControllers: Bool { controllers.count > 1 || identityStatus == "multiple" }
    var storageBlocked: Bool { StorageHealthCenter.shared.active() != nil }
    var canApply: Bool { statusReady && !busy && !storageBlocked && !(connected && multipleControllers) && !selection.isEmpty && draft.validation == nil && (dirty || savedSelection != selection || (connected && activeID != selection)) }
    var draftStatus: String {
        if connected && multipleControllers { return "多台控制端在线，暂不能应用显示设置" }
        if let validation = draft.validation { return validation }
        if dirty { return "有未保存的显示设置" }
        if connected && activeID != selection { return "正在查看此配置；当前画面尚未切换" }
        if savedSelection != selection { return "正在查看此配置；尚未设为下次使用" }
        return "显示设置已保存"
    }
    /// Same resolver as the menu-bar icon, so every surface reports one state.
    var presentation: StatusBarPresentation {
        runtime.presentation(now: clock())
    }
    var statusText: String { presentation.label }
    var statusTint: Color { presentation.tint }
    /// One line for the window banner, or nil when nothing needs attention.
    var banner: WorkbenchBanner? {
        if !trusted && !sleeping { return WorkbenchBanner(text: "需要重新授权辅助功能，才能识别 UU 连接。", needsPermission: true) }
        if presentation.kind == .attention { return WorkbenchBanner(text: presentation.detail) }
        if runtime.operational, let localizedError { return WorkbenchBanner(text: localizedError) }
        if connected && multipleControllers { return WorkbenchBanner(text: "多台设备同时连接，保持当前画面配置。", warning: false) }
        return nil
    }
    var localizedError: String? { StorageHealthCenter.shared.active()?.message ?? StatusCopy.localize(backendError) }
    var recognition: String {
        if controllers.count > 1 || identityStatus == "multiple" { return "多控制端在线 · 当前会话配置固定" }
        if identitySource == "binding" { return "当前连接已手动绑定" }
        if identityStatus == "matched" { return "当前连接按型号自动匹配" }
        if controllers.isEmpty { return "未连接 · 可预先保存设备配置" }
        return "未知或含糊名称 · 请选择型号或自定义原生参数"
    }
    func update(_ edit: (inout WorkbenchDraft) -> Void) {
        var value = draft; edit(&value); drafts[selection] = value
    }
    func addDevice() {
        guard !busy else { return }
        let key = "draft-" + UUID().uuidString
        drafts[key] = WorkbenchDraft(); selection = key
        message = "选择已知型号或填写自定义原生参数。草稿会在切换设备时保留。"
    }
    var unsavedKeys: [String] { drafts.keys.filter { id in !profiles.contains { $0.id == id } }.sorted() }
    /// Carry only unapplied edits across a permission-service refresh, never saved settings.
    func permissionDraftState() throws -> Data {
        let edits = drafts.filter { baselines[$0.key] != $0.value }
        let data = try JSONEncoder().encode(DraftHandoff(selection: selection, drafts: edits))
        guard data.count <= 512 * 1024 else { throw CocoaError(.fileWriteOutOfSpace) }
        return data
    }
    func restorePermissionDraftState(_ data: Data) {
        guard data.count <= 512 * 1024,
              let saved = try? JSONDecoder().decode(DraftHandoff.self, from: data), saved.drafts.count <= 100 else { return }
        let ids = Set(profiles.map(\.id))
        for (id, draft) in saved.drafts {
            let newID = id.hasPrefix("draft-") && UUID(uuidString: String(id.dropFirst(6))) != nil
            guard ids.contains(id) || newID, draft.cursor.isFinite,
                  [draft.label, draft.catalogID, draft.width, draft.height, draft.ppi, draft.orientation, draft.resolution]
                    .allSatisfy({ $0.utf8.count <= 4096 }) else { continue }
            drafts[id] = draft
        }
        if ids.contains(saved.selection) || drafts[saved.selection] != nil { selection = saved.selection }
    }
    func discard() {
        failure = nil
        if let original = baselines[selection] { drafts[selection] = original; message = "已放弃显示设置修改。" }
        else {
            drafts.removeValue(forKey: selection)
            selection = savedSelection ?? profiles.first?.id ?? ""
            message = "已取消添加设备。"
        }
    }
    func selectCatalog(_ id: String) {
        update { value in
            value.catalogID = id
            if let item = catalog.first(where: { $0.id == id }) {
                value.label = item.label; value.width = String(item.width); value.height = String(item.height)
                value.ppi = String(item.ppi); value.resolution = ""
                value.displayArea = item.displayArea
                value.hardwareKind = item.hardwareKind
                value.displayVariants = item.displayVariants; value.displayVariant = item.displayVariant
                value.cursor = item.cursor
                value.resolution = item.orientation == value.orientation ? item.resolution
                    : item.resolution.split(separator: "x").reversed().joined(separator: "x")
            } else {
                value.width = ""; value.height = ""; value.ppi = ""; value.resolution = ""
                value.hardwareKind = nil; value.displayVariants = nil; value.displayVariant = nil
            }
        }
        chooseDefaultResolution()
    }
    /// Display-size tiles, smallest logical size (largest text) first.
    var scaleOptions: [ScaleTilePicker.Option] {
        guard let (w, _) = draft.canvasPixels else { return [] }
        let nativeHalf = Double(w) / 2
        return Array(Set(resolutions() + (draft.resolution.isEmpty ? [] : [draft.resolution]))).compactMap { item in
            let parts = item.split(separator: "x").compactMap { Int($0) }
            guard parts.count == 2, parts[0] > 0, parts[1] > 0 else { return nil }
            return ScaleTilePicker.Option(resolution: item, width: parts[0], height: parts[1],
                                          zoomPercent: (nativeHalf / Double(parts[0]) - 1) * 100)
        }.sorted { $0.width < $1.width }
    }
    /// Concise list of what an apply would change, for the change bar.
    var pendingChanges: [String] {
        guard let base = baselines[selection] else { return [] }
        var changes: [String] = []
        if draft.orientation != base.orientation { changes.append(draft.orientation == "landscape" ? "横屏" : "竖屏") }
        if draft.resolution != base.resolution, !draft.resolution.isEmpty { changes.append(Format.size(draft.resolution)) }
        if draft.cursor != base.cursor { changes.append("光标 " + Format.scale(draft.cursor)) }
        if draft.label != base.label { changes.append("名称") }
        if draft.displayVariant != base.displayVariant { changes.append("屏幕规格") }
        for orientation in ["landscape", "portrait"] {
            if (draft.displayArea ?? DisplayAreaSettings()).preference(orientation)
                != (base.displayArea ?? DisplayAreaSettings()).preference(orientation) {
                changes.append(orientation == "landscape" ? "横屏显示范围" : "竖屏显示范围")
            }
        }
        return changes
    }
    /// Applying can briefly reconnect the remote picture.
    var requiresReconnect: Bool {
        guard connected, let base = baselines[selection] else { return connected && existing == nil }
        if activeID != selection { return true }
        return draft.orientation != base.orientation || draft.resolution != base.resolution
            || draft.canvasPixels?.0 != base.canvasPixels?.0 || draft.canvasPixels?.1 != base.canvasPixels?.1
    }
    var showsChangeBar: Bool { busy || canApply || (dirty && draft.validation != nil) }
    private func showToast(_ text: String) {
        toastWork?.cancel(); toast = text
        let work = DispatchWorkItem { [weak self] in self?.toast = nil }
        toastWork = work; DispatchQueue.main.asyncAfter(deadline: .now() + 4, execute: work)
    }
    func resolutions() -> [String] {
        guard let canvas = draft.canvasPixels else { return [] }
        func adjusted(_ modes: [String], from item: WorkbenchDevice) -> [String] {
            let originalCanvas = WorkbenchDraft(item).canvasPixels
            let source = item.orientation == draft.orientation ? originalCanvas
                : originalCanvas.map { ($0.1, $0.0) }
            return modes.map { mode in
                let parts = mode.split(separator: "x").compactMap { Int($0) }
                guard parts.count == 2 else { return mode }
                if source?.0 == canvas.0 && source?.1 == canvas.1 {
                    return item.orientation == draft.orientation ? mode : "\(parts[1])x\(parts[0])"
                }
                let short = min(parts[0], parts[1], Int((Double(min(canvas.0, canvas.1)) / 2).rounded(.toNearestOrEven)))
                let long = Int((Double(short) * Double(max(canvas.0, canvas.1)) / Double(min(canvas.0, canvas.1))).rounded(.toNearestOrEven))
                return draft.orientation == "landscape" ? "\(long)x\(short)" : "\(short)x\(long)"
            }.reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
        }
        if let item = existing, item.displayVariant == draft.displayVariant, let saved = presets[item.id], !saved.isEmpty {
            return adjusted(saved, from: item)
        }
        if let item = catalog.first(where: { $0.id == draft.catalogID }), item.displayVariant == draft.displayVariant, !item.presetResolutions.isEmpty {
            return adjusted(item.presetResolutions, from: item)
        }
        guard let w = Int(draft.width), let h = Int(draft.height), w > 0, h > 0, w <= 16384, h <= 16384 else { return [] }
        let width = draft.orientation == "landscape" ? max(w, h) : min(w, h)
        let height = draft.orientation == "landscape" ? min(w, h) : max(w, h)
        let scales = draft.catalogID.hasPrefix("iphone-") ? [1.0, 0.9, 0.8, 0.744, 2.0/3] : [1.0, 0.93, 0.875, 0.84, 0.744]
        return scales.map {
            "\(Int((Double(width) / 2 * $0).rounded(.toNearestOrEven)))x\(Int((Double(height) / 2 * $0).rounded(.toNearestOrEven)))"
        }.map { mode in
            guard canvas.0 != width || canvas.1 != height else { return mode }
            let parts = mode.split(separator: "x").compactMap { Int($0) }
            let short = min(parts[0], parts[1])
            let long = Int((Double(short) * Double(max(canvas.0, canvas.1)) / Double(min(canvas.0, canvas.1))).rounded(.toNearestOrEven))
            return draft.orientation == "landscape" ? "\(long)x\(short)" : "\(short)x\(long)"
        }.reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
    }
    func chooseDefaultResolution() {
        let choices = resolutions()
        if !choices.contains(draft.resolution) {
            let item = catalog.first { $0.id == draft.catalogID }
            let recommended = item.map { $0.orientation == draft.orientation ? $0.resolution
                : $0.resolution.split(separator: "x").reversed().joined(separator: "x") }
            update { $0.resolution = recommended.flatMap { choices.contains($0) ? $0 : nil }
                ?? (choices.count > 1 ? choices[1] : (choices.first ?? "")) }
        }
    }
    func selectDisplayVariant(_ id: String) {
        let index = resolutions().firstIndex(of: draft.resolution)
        update { $0.selectDisplayVariant(id) }
        let choices = resolutions()
        if let index, choices.indices.contains(index) { update { $0.resolution = choices[index] } }
    }
    func apply() {
        guard canApply else { return }
        var command: [String: Any] = ["action": "profile_apply", "draft": draft.payload]
        if existing != nil { command["profile_id"] = selection }
        else { command["new_profile_id"] = selection }
        submit(command, tracksDraft: true)
    }
    var selectedBindings: [WorkbenchBinding] { bindings.filter { $0.profileID == selection } }
    var canBindCurrent: Bool {
        statusReady && !busy && !storageBlocked && existing != nil && bindingAvailable && controllers.count == 1 && currentBinding?.profileID != selection
    }
    var applyTitle: String {
        guard connected else { return "保存显示设置 · 下次连接生效" }
        if existing == nil { return "创建设备并切换" }
        if activeID != selection { return "切换到此设备配置" }
        return "应用显示设置"
    }
    var previewIsDraft: Bool { dirty || (connected && activeID != selection) }
    var feedback: String { localizedError ?? StatusCopy.localize(message) ?? message }
    func profileLabel(_ id: String) -> String { profiles.first { $0.id == id }?.label ?? "其他设备" }
    func bindingRequest() -> BindingRequest? {
        guard canBindCurrent else { return nil }
        return BindingRequest(action: "profile_bind", name: controllers[0], profileID: selection,
                              expectedProfileID: currentBinding?.profileID)
    }
    func unbindingRequest(_ binding: WorkbenchBinding) -> BindingRequest {
        BindingRequest(action: "profile_unbind", name: binding.key, profileID: binding.profileID,
                       expectedProfileID: binding.profileID)
    }
    func submitBinding(_ request: BindingRequest) {
        var body: [String: Any] = ["action": request.action, "profile_id": request.profileID, "name": request.name]
        if request.action == "profile_bind" { body["expected_profile_id"] = request.expectedProfileID.map { $0 as Any } ?? NSNull() }
        submit(body)
    }
    func core(_ action: String) { submit(["action": action], recovery: action == "recover") }
    /// Cursor size for the live device (or the next one when offline); the backend picks the target.
    @discardableResult func setCursor(_ scale: Double) -> Bool { submit(["action": "profile_cursor", "scale": scale]) }
    /// The configuration that is on screen now, or the one used next when offline.
    var currentDevice: WorkbenchDevice? {
        guard statusReady else { return nil }
        // Mirrors the backend target: active profile while ACTIVE, otherwise selected_id.
        if let active = profiles.first(where: { $0.id == activeID }) { return active }
        return profiles.first { $0.id == savedSelection } ?? profiles.first
    }
    func setAutomatic(_ value: Bool) { submit(["action": "profile_automatic", "automatic": value]) }
    @discardableResult private func submit(_ body: [String: Any], tracksDraft: Bool = false, recovery: Bool = false) -> Bool {
        guard recovery || (!busy && statusReady) else { return false }
        let id = UUID().uuidString
        var command = body; command["id"] = id
        do {
            try sender(command)
            if recovery { auxiliaryIDs.insert(id); message = "恢复请求已发送，等待后台确认。"; return true }
            pendingID = id; pendingSelection = tracksDraft ? selection : nil; pendingDraft = tracksDraft ? draft : nil
            failure = nil
            message = "请求已发送，等待后台确认。"
            timeout?.cancel()
            let work = DispatchWorkItem { [weak self] in
                guard let self, self.pendingID == id else { return }
                self.message = "后台尚未确认完成，请继续等待；仍可恢复实体屏。草稿已保留。"
            }
            timeout = work; DispatchQueue.main.asyncAfter(deadline: .now() + 30, execute: work)
            return true
        } catch {
            message = "请求未发送：\(error.localizedDescription)；草稿已保留。"
            failure = message
            return false
        }
    }
    func reload() {
        let firstRead = !statusReady
        runtime = RuntimeStatusSnapshot.read(directory: directory, now: clock())
        mode = runtime.mode; enabled = runtime.enabled ?? enabled; activeID = runtime.ready ? runtime.activeID : nil
        connection = runtime.connection; trusted = runtime.trusted; sleeping = runtime.sleeping
        controllers = runtime.controllers; backendError = runtime.error
        statusDetail = runtime.status ?? [:]; statusTimestamp = runtime.status?["timestamp"] as? Double
        statusReady = runtime.ready
        expiryWork?.cancel(); expiryWork = nil
        if observing, let deadline = runtime.expiry {
            let work = DispatchWorkItem { [weak self] in self?.reload() }
            expiryWork = work; DispatchQueue.main.asyncAfter(deadline: .now() + max(0.01, deadline - clock()), execute: work)
        }
        do {
            guard let raw = runtime.workbench, raw["version"] as? Int == 1,
                  let rows = raw["profiles"] as? [[String: Any]] else { throw CocoaError(.fileReadCorruptFile) }
            let devices = rows.compactMap(WorkbenchDevice.init)
            guard devices.count == rows.count else { throw CocoaError(.fileReadCorruptFile) }
            profiles = devices
            catalog = (raw["catalog"] as? [[String: Any]] ?? []).compactMap(WorkbenchDevice.init)
            presets = raw["presets"] as? [String: [String]] ?? [:]
            automatic = raw["automatic"] as? Bool ?? false
            bindings = (raw["bindings"] as? [[String: Any]] ?? []).compactMap(WorkbenchBinding.init)
            currentBinding = (raw["current_binding"] as? [String: Any]).flatMap(WorkbenchBinding.init)
            bindingAvailable = raw["binding_available"] as? Bool ?? false
            receipts = (raw["results"] as? [[String: Any]] ?? []).compactMap(WorkbenchReceipt.init)
            savedSelection = raw["selected_id"] as? String
            let identity = raw["identity"] as? [String: Any] ?? [:]
            identityStatus = identity["status"] as? String ?? "unknown"; identitySource = identity["source"] as? String ?? ""
            for item in profiles {
                let fresh = WorkbenchDraft(item)
                if drafts[item.id] == nil || drafts[item.id] == baselines[item.id] { drafts[item.id] = fresh }
                baselines[item.id] = fresh
            }
            if selection.isEmpty { selection = raw["selected_id"] as? String ?? profiles.first?.id ?? "" }
            if firstRead { message = "草稿仅在点击应用或保存后提交。" }
            let history = raw["results"] as? [[String: Any]] ?? []
            let lastResult = raw["last_result"] as? [String: Any]
            let waitingIDs = [pendingID].compactMap { $0 } + auxiliaryIDs.sorted()
            for id in waitingIDs {
                let terminal: ([String: Any]) -> Bool = { receipt in
                    receipt["id"] as? String == id && ["ok", "error"].contains(receipt["status"] as? String ?? "")
                }
                let result = history.reversed().first(where: terminal) ?? lastResult.flatMap { terminal($0) ? $0 : nil }
                guard let result, let status = result["status"] as? String else { continue }
                if id == pendingID {
                    if status == "ok", let key = pendingSelection {
                        let savedID = profiles.contains { $0.id == key } ? key : (raw["selected_id"] as? String ?? key)
                        if let saved = baselines[savedID] {
                            drafts[savedID] = saved
                            if key != savedID { drafts.removeValue(forKey: key); selection = savedID }
                        } else if let submitted = pendingDraft { drafts[key] = submitted }
                    }
                    message = result["message"] as? String ?? (status == "ok" ? "后台已确认。" : "操作失败，草稿已保留。")
                    if status == "ok" { failure = nil; showToast(StatusCopy.localize(message) ?? message) }
                    else { failure = StatusCopy.localize(message) ?? message }
                    pendingID = nil; pendingSelection = nil; pendingDraft = nil; timeout?.cancel()
                } else if auxiliaryIDs.remove(id) != nil {
                    message = result["message"] as? String ?? (status == "ok" ? "恢复已完成。" : "恢复失败，请重试。")
                    showToast(StatusCopy.localize(message) ?? message)
                }
            }

        } catch { statusReady = false; message = "后台状态暂时无法读取，已保留当前草稿。" }
    }
}
